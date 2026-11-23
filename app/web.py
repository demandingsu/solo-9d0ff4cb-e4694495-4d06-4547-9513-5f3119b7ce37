"""Flask 网页：主展图版-语种搭配登记、巡展分站差异登记与可印语种查询。"""

from __future__ import annotations

import sqlite3

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import csvio, stations, storage
from .db import connect, init_db

_SCHEMA_READY: set[str] = set()


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "label-printing-dev-key"
    app.config["DB_PATH"] = db_path

    @app.before_request
    def _open_connection() -> None:
        g.db = connect(app.config["DB_PATH"])
        # 兼容未通过入口脚本初始化的部署/测试：首请求幂等建表
        target = app.config["DB_PATH"] or "default"
        if target not in _SCHEMA_READY:
            init_db(app.config["DB_PATH"])
            _SCHEMA_READY.add(target)

    @app.teardown_request
    def _close_connection(exc) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.get("/")
    def index():
        exhibits = storage.list_exhibits(g.db)
        return render_template("index.html", exhibits=exhibits)

    @app.get("/exhibits/<path:exhibit_no>")
    def exhibit_detail(exhibit_no: str):
        data = storage.get_exhibit(g.db, exhibit_no)
        if data is None:
            abort(404)
        return render_template("detail.html", data=data)

    @app.post("/upload")
    def upload():
        exhibit_no = (request.form.get("exhibit_no") or "").strip()
        if not exhibit_no:
            flash("请填写展品号", "error")
            return redirect(url_for("index"))

        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            flash("请选择图版-语种 CSV 文件", "error")
            return redirect(url_for("exhibit_detail", exhibit_no=exhibit_no))

        try:
            pairs = csvio.parse_pairs(upload.read())
        except csvio.CsvFormatError as exc:
            _log_rejected(g.db, exhibit_no, str(exc))
            flash(f"CSV 校验失败，本批未写入：{exc}", "error")
            return redirect(url_for("exhibit_detail", exhibit_no=exhibit_no))

        try:
            result = storage.merge_and_store(g.db, exhibit_no, pairs)
        except sqlite3.OperationalError as exc:
            app.logger.exception("write failed")
            flash(f"数据库繁忙，本批未写入，请稍后重试：{exc}", "error")
            return redirect(url_for("exhibit_detail", exhibit_no=exhibit_no))

        mode_text = "清单存法（图版×语种任意搭配）" if result.storage_mode == "dense" else "配对存法（仅实际配对）"
        switched = f"，存法由{'清单存法' if result.switched_from == 'dense' else '配对存法'}切换为{mode_text.split('（')[0]}" if result.switched_from else ""
        flash(
            f"登记成功：文件原始 {result.rows_in_file} 行 / 文件内去重后 {result.unique_rows} 种搭配，"
            f"相对库内新增 {result.new_pairs} 种，展品现有 {result.total_pairs} 种可印搭配，"
            f"当前为{mode_text}{switched}。",
            "success",
        )
        return redirect(url_for("exhibit_detail", exhibit_no=exhibit_no))

    # ---------- 巡展分站（网页） ----------

    @app.get("/stations")
    def station_index():
        data = stations.list_stations(g.db)
        exhibits = storage.list_exhibits(g.db)
        return render_template("stations.html", stations=data, exhibits=exhibits)

    @app.post("/stations")
    def station_create():
        station_no = (request.form.get("station_no") or "").strip()
        parent_exhibit_no = (request.form.get("parent_exhibit_no") or "").strip()
        if not station_no or not parent_exhibit_no:
            flash("请同时填写分站号并选择已登记的主展", "error")
            return redirect(url_for("station_index"))
        try:
            stations.register_station(g.db, station_no, parent_exhibit_no)
        except LookupError as exc:
            flash(f"分站未创建：{exc}", "error")
            return redirect(url_for("station_index"))
        except sqlite3.OperationalError as exc:
            flash(f"数据库繁忙，分站未写入，请稍后重试：{exc}", "error")
            return redirect(url_for("station_index"))
        flash(f"分站 {station_no} 已登记，沿用主展 {parent_exhibit_no} 的展签。", "success")
        return redirect(url_for("station_detail", station_no=station_no))

    @app.get("/stations/<path:station_no>")
    def station_detail(station_no: str):
        data = stations.get_station(g.db, station_no)
        if data is None:
            abort(404)
        return render_template("station_detail.html", data=data)

    @app.post("/stations/<path:station_no>/diffs")
    def station_upload_diff(station_no: str):
        target = stations.get_station(g.db, station_no)
        if target is None:
            abort(404)
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            flash("请选择分站差异 CSV 文件", "error")
            return redirect(url_for("station_detail", station_no=station_no))
        try:
            parsed = csvio.parse_diffs(upload.read())
        except csvio.CsvFormatError as exc:
            stations.log_rejected_diff(g.db, station_no, str(exc))
            flash(f"差异 CSV 校验失败，整单拒收，原差异继续生效：{exc}", "error")
            return redirect(url_for("station_detail", station_no=station_no))
        try:
            result = stations.replace_diffs(g.db, station_no, parsed)
        except sqlite3.OperationalError as exc:
            app.logger.exception("write failed")
            flash(f"数据库繁忙，差异未写入，请稍后重试：{exc}", "error")
            return redirect(url_for("station_detail", station_no=station_no))
        if result.unique_rows == 0:
            flash("已上传空清单：分站差异全部清空，恢复直接沿用主展。", "success")
        else:
            flash(
                f"差异已整份替换：文件原始 {result.rows_in_file} 行 / 去重后 "
                f"{result.unique_rows} 条（增补 {result.add_count} 条、排除 "
                f"{result.exclude_count} 条）。",
                "success",
            )
        return redirect(url_for("station_detail", station_no=station_no))

    # ---------- JSON API（便于自测与系统对接） ----------

    @app.get("/api/exhibits")
    def api_list():
        return jsonify(storage.list_exhibits(g.db))

    @app.get("/api/exhibits/<path:exhibit_no>")
    def api_detail(exhibit_no: str):
        data = storage.get_exhibit(g.db, exhibit_no)
        if data is None:
            return jsonify({"error": "exhibit not found"}), 404
        return jsonify(data)

    @app.post("/api/exhibits/<path:exhibit_no>/batches")
    def api_upload(exhibit_no: str):
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            return jsonify({"error": "缺少 csv_file 文件字段"}), 400
        try:
            pairs = csvio.parse_pairs(upload.read())
        except csvio.CsvFormatError as exc:
            _log_rejected(g.db, exhibit_no, str(exc))
            return jsonify({"error": str(exc), "status": "rejected"}), 422
        try:
            result = storage.merge_and_store(g.db, exhibit_no, pairs)
        except sqlite3.OperationalError as exc:
            return jsonify({"error": f"database busy: {exc}"}), 503
        return jsonify(
            {
                "status": "committed",
                "exhibit_no": result.exhibit_no,
                "storage_mode": result.storage_mode,
                "switched_from": result.switched_from,
                "rows_in_file": result.rows_in_file,
                "unique_rows": result.unique_rows,
                "new_pairs": result.new_pairs,
                "total_pairs": result.total_pairs,
            }
        )

    # ---------- 巡展分站（JSON API） ----------

    @app.get("/api/stations")
    def api_station_list():
        return jsonify(stations.list_stations(g.db))

    @app.post("/api/stations")
    def api_station_create():
        payload = request.get_json(silent=True) or request.form
        station_no = (payload.get("station_no") or "").strip()
        parent_exhibit_no = (payload.get("parent_exhibit_no") or "").strip()
        if not station_no or not parent_exhibit_no:
            return jsonify({"error": "需要 station_no 与 parent_exhibit_no"}), 400
        try:
            stations.register_station(g.db, station_no, parent_exhibit_no)
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        except sqlite3.OperationalError as exc:
            return jsonify({"error": f"database busy: {exc}"}), 503
        return jsonify(
            {"status": "registered", "station_no": station_no,
             "parent_exhibit_no": parent_exhibit_no}
        ), 201

    @app.get("/api/stations/<path:station_no>")
    def api_station_detail(station_no: str):
        data = stations.get_station(g.db, station_no)
        if data is None:
            return jsonify({"error": "station not found"}), 404
        return jsonify(data)

    @app.put("/api/stations/<path:station_no>/diffs")
    def api_station_replace_diffs(station_no: str):
        if stations.get_station(g.db, station_no) is None:
            return jsonify({"error": "station not found"}), 404
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            return jsonify({"error": "缺少 csv_file 文件字段"}), 400
        try:
            parsed = csvio.parse_diffs(upload.read())
        except csvio.CsvFormatError as exc:
            stations.log_rejected_diff(g.db, station_no, str(exc))
            return jsonify({"error": str(exc), "status": "rejected"}), 422
        try:
            result = stations.replace_diffs(g.db, station_no, parsed)
        except sqlite3.OperationalError as exc:
            return jsonify({"error": f"database busy: {exc}"}), 503
        return jsonify(
            {
                "status": "committed",
                "station_no": result.station_no,
                "parent_exhibit_no": result.parent_exhibit_no,
                "rows_in_file": result.rows_in_file,
                "unique_rows": result.unique_rows,
                "add_count": result.add_count,
                "exclude_count": result.exclude_count,
            }
        )

    @app.get("/healthz")
    def healthz():
        g.db.execute("SELECT 1")
        return "ok"

    @app.cli.command("init-db")
    def init_db_command():
        init_db(app.config["DB_PATH"])

    return app


def _log_rejected(db: sqlite3.Connection, exhibit_no: str, message: str) -> None:
    """拒收也留痕，但不影响任何已提交资料。"""
    try:
        db.execute(
            "INSERT INTO batch_log(exhibit_no, rows_in_file, unique_rows, new_pairs, "
            "total_pairs, storage_mode, switched_from, status, message) "
            "VALUES (?, 0, 0, 0, 0, COALESCE((SELECT storage_mode FROM storage_state "
            "WHERE exhibit_no = ?), 'sparse'), NULL, 'rejected', ?)",
            (exhibit_no, exhibit_no, message),
        )
    except sqlite3.Error:
        pass
