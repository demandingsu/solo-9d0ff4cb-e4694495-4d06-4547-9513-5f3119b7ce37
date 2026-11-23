"""巡展分站的差异登记与生效视图。

分站沿用主展（已登记展品）的展签，只保存本站差异：

* ``add``：在主展可印搭配之外增补（当地语种等）；
* ``exclude``：从主展搭配中提前排除（本站不印）。

生效搭配始终在查询时由「同一时点的主展已登记数据 + 本站差异」即时合成，
因此主展补录或 dense/sparse 存法切换后分站自动跟上，分站无需任何迁移。
规则：

* 新差异文件 **整份替换** 旧差异；空清单（空文件/仅表头）清空差异，
  恢复直接沿用主展；
* 同一搭配写了相反操作的文件在 CSV 层即整单拒收，旧差异继续生效；
* 提前排除的搭配后来补录进主展，仍被排除（排除持续生效）；
* 两处都有的搭配算分站增补、只计一次；
* 主展为清单存法（dense）时，搭配总数用清单基数做集合算术，
  **不展开全部笛卡尔积配对**。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .db import run_with_immediate


@dataclass
class DiffReplaceResult:
    station_no: str
    parent_exhibit_no: str
    rows_in_file: int
    unique_rows: int
    add_count: int
    exclude_count: int


@dataclass
class _ParentSnapshot:
    """主展在某一连接时点的已登记数据（按当前存法读取，不做存法转换）。"""

    exhibit_no: str
    storage_mode: str
    plates: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    dense_pairs: set[tuple[str, str]] | None = None  # dense 时才物化，仅供缺项核对
    sparse_pairs: set[tuple[str, str]] | None = None


def _load_parent(conn: sqlite3.Connection, exhibit_no: str) -> _ParentSnapshot | None:
    mode_row = conn.execute(
        "SELECT storage_mode FROM storage_state WHERE exhibit_no = ?", (exhibit_no,)
    ).fetchone()
    if mode_row is None:
        return None
    snap = _ParentSnapshot(exhibit_no=exhibit_no, storage_mode=mode_row["storage_mode"])
    if snap.storage_mode == "dense":
        snap.plates = [
            r["plate_no"]
            for r in conn.execute(
                "SELECT plate_no FROM dense_plates WHERE exhibit_no = ? ORDER BY plate_no",
                (exhibit_no,),
            )
        ]
        snap.languages = [
            r["language"]
            for r in conn.execute(
                "SELECT language FROM dense_languages WHERE exhibit_no = ? ORDER BY language",
                (exhibit_no,),
            )
        ]
    else:
        snap.sparse_pairs = {
            (r["plate_no"], r["language"])
            for r in conn.execute(
                "SELECT plate_no, language FROM sparse_pairs WHERE exhibit_no = ?",
                (exhibit_no,),
            )
        }
    return snap


def _parent_plate_languages(snap: _ParentSnapshot) -> dict[str, set[str]]:
    """按图版列出主展可印语种。

    dense 主展只在详情明细需要时按清单逐图版生成视图（页数级数据），
    总数统计走 :func:`_effective_pair_count` 的基数算术，不在此展开。
    """
    if snap.storage_mode == "dense":
        langs = set(snap.languages)
        return {p: set(langs) for p in snap.plates}
    by_plate: dict[str, set[str]] = {}
    for plate, lang in snap.sparse_pairs or set():
        by_plate.setdefault(plate, set()).add(lang)
    return by_plate


def _effective_pair_count(
    snap: _ParentSnapshot,
    adds: set[tuple[str, str]],
    excludes: set[tuple[str, str]],
) -> int:
    """生效搭配总数 = 主展搭配 − 被排除且仍在主展的 + 主展外的增补。

    dense 主展仅用图版/语种清单的基数计算，不展开笛卡尔积：
    被排除的搭配按其图版、语种是否仍在清单内判定是否落在主展集合中。
    """
    if snap.storage_mode == "dense":
        plate_set = set(snap.plates)
        lang_set = set(snap.languages)
        excluded_in_parent = sum(
            1 for p, lang in excludes if p in plate_set and lang in lang_set
        )
        parent_total = len(plate_set) * len(lang_set)
    else:
        parent_pairs = snap.sparse_pairs or set()
        excluded_in_parent = len(excludes & parent_pairs)
        parent_total = len(parent_pairs)
    adds_outside_parent = len(adds) - len(adds & _parent_pair_set(snap, adds))
    return parent_total - excluded_in_parent + adds_outside_parent


def _parent_pair_set(
    snap: _ParentSnapshot, adds: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """仅在需要与增补集合做交集时物化主展配对。

    sparse 主展本就是配对表；dense 主展只物化与 ``adds`` 相关的部分，
    避免把完整笛卡尔积全部展开。
    """
    if snap.storage_mode != "dense":
        return set(snap.sparse_pairs or set())
    plate_set = set(snap.plates)
    lang_set = set(snap.languages)
    return {(p, lang) for p, lang in adds if p in plate_set and lang in lang_set}


def register_station(
    conn: sqlite3.Connection, station_no: str, parent_exhibit_no: str
) -> None:
    """登记分站并选定一个已登记的主展。

    主展必须已在库内登记；分站号重复登记时沿用原记录（幂等），
    已保存的本站差异不受影响。
    """
    station_no = station_no.strip()
    parent_exhibit_no = parent_exhibit_no.strip()
    if not station_no:
        raise ValueError("分站号不能为空")
    if not parent_exhibit_no:
        raise ValueError("主展展品号不能为空")

    def work(tx: sqlite3.Connection) -> None:
        parent = tx.execute(
            "SELECT 1 FROM storage_state WHERE exhibit_no = ?", (parent_exhibit_no,)
        ).fetchone()
        if parent is None:
            raise LookupError(f"主展 {parent_exhibit_no} 尚未登记，无法为分站选用")
        tx.execute(
            "INSERT INTO stations(station_no, parent_exhibit_no) VALUES (?, ?) "
            "ON CONFLICT(station_no) DO UPDATE SET "
            "parent_exhibit_no = excluded.parent_exhibit_no, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (station_no, parent_exhibit_no),
        )

    run_with_immediate(conn, work)


def replace_diffs(
    conn: sqlite3.Connection,
    station_no: str,
    incoming,
) -> DiffReplaceResult:
    """用新文件的差异 **整份替换** 分站旧差异（单事务）。

    ``incoming`` 为 :class:`app.csvio.ParsedDiffCsv`（也兼容三元组列表）。
    空清单会清空差异，分站恢复直接沿用主展。任一异常整事务回滚，
    旧差异原样保留。
    """
    raw_data_rows = getattr(incoming, "raw_data_rows", None)
    diff_list = incoming.diffs if hasattr(incoming, "diffs") else list(incoming)
    rows_in_file = raw_data_rows if raw_data_rows is not None else len(diff_list)
    unique_diffs = sorted(set(diff_list))
    add_count = sum(1 for _, _, action in unique_diffs if action == "add")
    exclude_count = len(unique_diffs) - add_count

    def work(tx: sqlite3.Connection) -> DiffReplaceResult:
        row = tx.execute(
            "SELECT parent_exhibit_no FROM stations WHERE station_no = ?", (station_no,)
        ).fetchone()
        if row is None:
            raise LookupError(f"分站 {station_no} 尚未登记")
        parent_no = row["parent_exhibit_no"]
        # 先删后插在同一事务内完成，外界永远看不到中间的空窗状态。
        tx.execute("DELETE FROM station_diffs WHERE station_no = ?", (station_no,))
        tx.executemany(
            "INSERT INTO station_diffs(station_no, plate_no, language, action) "
            "VALUES (?, ?, ?, ?)",
            [(station_no, p, lang, action) for p, lang, action in unique_diffs],
        )
        tx.execute(
            "UPDATE stations SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE station_no = ?",
            (station_no,),
        )
        result = DiffReplaceResult(
            station_no=station_no,
            parent_exhibit_no=parent_no,
            rows_in_file=rows_in_file,
            unique_rows=len(unique_diffs),
            add_count=add_count,
            exclude_count=exclude_count,
        )
        tx.execute(
            "INSERT INTO station_diff_log(station_no, rows_in_file, unique_rows, "
            "add_count, exclude_count, status, message) "
            "VALUES (?, ?, ?, ?, ?, 'committed', NULL)",
            (station_no, rows_in_file, len(unique_diffs), add_count, exclude_count),
        )
        return result

    return run_with_immediate(conn, work)


def log_rejected_diff(
    conn: sqlite3.Connection, station_no: str, message: str
) -> None:
    """差异文件拒收留痕（不触碰任何已生效差异）。"""
    try:
        conn.execute(
            "INSERT INTO station_diff_log(station_no, rows_in_file, unique_rows, "
            "add_count, exclude_count, status, message) "
            "VALUES (?, 0, 0, 0, 0, 'rejected', ?)",
            (station_no, message),
        )
    except sqlite3.Error:
        pass


def list_stations(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT s.station_no, s.parent_exhibit_no, s.created_at, s.updated_at, "
            "(SELECT COUNT(*) FROM station_diffs d WHERE d.station_no = s.station_no "
            " AND d.action = 'add') AS add_count, "
            "(SELECT COUNT(*) FROM station_diffs d WHERE d.station_no = s.station_no "
            " AND d.action = 'exclude') AS exclude_count "
            "FROM stations s ORDER BY s.station_no"
        )
    ]


def _load_diffs(
    conn: sqlite3.Connection, station_no: str
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], list[dict]]:
    adds: set[tuple[str, str]] = set()
    excludes: set[tuple[str, str]] = set()
    for r in conn.execute(
        "SELECT plate_no, language, action FROM station_diffs WHERE station_no = ?",
        (station_no,),
    ):
        (adds if r["action"] == "add" else excludes).add(
            (r["plate_no"], r["language"])
        )
    logs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, received_at, rows_in_file, unique_rows, add_count, "
            "exclude_count, status, message FROM station_diff_log "
            "WHERE station_no = ? ORDER BY id DESC",
            (station_no,),
        )
    ]
    return adds, excludes, logs


def get_station(conn: sqlite3.Connection, station_no: str) -> dict | None:
    """分站页：按图版查可印语种并标明来源（主展 / 分站增补）。

    全部读取发生在同一个连接、同一个时间点，总数与明细口径一致。
    """
    station = conn.execute(
        "SELECT station_no, parent_exhibit_no, created_at, updated_at "
        "FROM stations WHERE station_no = ?",
        (station_no,),
    ).fetchone()
    if station is None:
        return None
    parent_no = station["parent_exhibit_no"]
    snap = _load_parent(conn, parent_no)
    adds, excludes, logs = _load_diffs(conn, station_no)

    parent_view = _parent_plate_languages(snap) if snap is not None else {}

    # 合成生效视图：主展搭配先去掉排除，再并入增补。
    effective: dict[str, set[str]] = {}
    sources: dict[tuple[str, str], str] = {}
    for plate, langs in parent_view.items():
        for lang in langs:
            if (plate, lang) in excludes:
                continue  # 提前排除：即便后来补录进主展，本站仍不印
            effective.setdefault(plate, set()).add(lang)
            sources[(plate, lang)] = "parent"
    for plate, lang in sorted(adds):
        # 两处都有算分站增补、只计一次。
        effective.setdefault(plate, set()).add(lang)
        sources[(plate, lang)] = "station_add"

    by_plate = {
        plate: [
            {"language": lang, "source": sources[(plate, lang)]}
            for lang in sorted(effective.get(plate, set()))
        ]
        for plate in sorted(effective)
    }
    pair_count = (
        _effective_pair_count(snap, adds, excludes)
        if snap is not None
        else len(adds - excludes)
    )
    # 防御性自检：明细与总数必须同源同口径（dense 基数算术的回归保护）。
    assert pair_count == sum(len(v) for v in by_plate.values())

    return {
        "station_no": station_no,
        "parent_exhibit_no": parent_no,
        "parent_registered": snap is not None,
        "parent_storage_mode": snap.storage_mode if snap is not None else None,
        "created_at": station["created_at"],
        "updated_at": station["updated_at"],
        "plates": sorted(effective),
        "by_plate": by_plate,
        "pair_count": pair_count,
        "adds": sorted([list(p) for p in adds]),
        "excludes": sorted([list(p) for p in excludes]),
        "diff_logs": logs,
    }
