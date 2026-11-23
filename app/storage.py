"""展品图版-语种搭配的登记与查询。

存法（每批按与库内已登记数据 *合并后* 的结果重新选择）：

* dense（清单存法）：当某展品的图版集合与语种集合构成完整笛卡尔积，
  即任意图版都能配任意语种时，只保留去重后的图版清单与语种清单两张表；
* sparse（配对存法）：存在缺项时，只保留实际登记的 (图版, 语种) 配对。

两种存法随时可以互换：批次合并后重新判定，由单事务完成清理与重写，
互换前后已登记资料（可印搭配集合）不丢失。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .db import run_with_immediate


@dataclass
class MergeResult:
    exhibit_no: str
    storage_mode: str
    switched_from: str | None
    rows_in_file: int
    unique_rows: int
    new_pairs: int
    total_pairs: int
    plates: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


def _registered_pairs(conn: sqlite3.Connection, exhibit_no: str) -> set[tuple[str, str]]:
    """只依据已登记数据返回当前全部可印搭配。"""
    mode_row = conn.execute(
        "SELECT storage_mode FROM storage_state WHERE exhibit_no = ?", (exhibit_no,)
    ).fetchone()
    if mode_row is None:
        return set()
    if mode_row["storage_mode"] == "dense":
        plates = [
            r["plate_no"]
            for r in conn.execute(
                "SELECT plate_no FROM dense_plates WHERE exhibit_no = ? ORDER BY plate_no",
                (exhibit_no,),
            )
        ]
        languages = [
            r["language"]
            for r in conn.execute(
                "SELECT language FROM dense_languages WHERE exhibit_no = ? ORDER BY language",
                (exhibit_no,),
            )
        ]
        return {(p, lang) for p in plates for lang in languages}
    return {
        (r["plate_no"], r["language"])
        for r in conn.execute(
            "SELECT plate_no, language FROM sparse_pairs WHERE exhibit_no = ?",
            (exhibit_no,),
        )
    }


def merge_and_store(
    conn: sqlite3.Connection,
    exhibit_no: str,
    incoming,
) -> MergeResult:
    """把本批配对去重后并入库内数据，并按合并结果重选存法。

    ``incoming`` 为 :class:`app.csvio.ParsedCsv`（也兼容纯 list 元组，
    便于单元测试）。整个读-改-写放在一个 BEGIN IMMEDIATE 事务里，
    并发批次串行化，任一失败（异常）都回滚，已提交的其它批次资料不受影响。
    """
    raw_data_rows = getattr(incoming, "raw_data_rows", None)
    pair_list = incoming.pairs if hasattr(incoming, "pairs") else list(incoming)
    rows_in_file = raw_data_rows if raw_data_rows is not None else len(pair_list)
    unique_incoming = sorted(set(pair_list))

    def work(tx: sqlite3.Connection) -> MergeResult:
        before = _registered_pairs(tx, exhibit_no)
        merged = before | set(unique_incoming)
        # rows_in_file 为文件原始数据行数，unique_rows 为文件内去重后行数，
        # new_pairs 为相对已登记数据的净增，total_pairs 为合并后累计搭配数。

        mode_row = tx.execute(
            "SELECT storage_mode FROM storage_state WHERE exhibit_no = ?", (exhibit_no,)
        ).fetchone()
        previous_mode = mode_row["storage_mode"] if mode_row else None

        plates = sorted({p for p, _ in merged})
        languages = sorted({lang for _, lang in merged})
        complete = bool(merged) and merged == {
            (p, lang) for p in plates for lang in languages
        }
        new_mode = "dense" if complete else "sparse"

        # 先清空旧存法数据，再按新存法写入（同事务，互换不丢资料）。
        tx.execute("DELETE FROM dense_plates WHERE exhibit_no = ?", (exhibit_no,))
        tx.execute("DELETE FROM dense_languages WHERE exhibit_no = ?", (exhibit_no,))
        tx.execute("DELETE FROM sparse_pairs WHERE exhibit_no = ?", (exhibit_no,))

        if new_mode == "dense":
            tx.executemany(
                "INSERT INTO dense_plates(exhibit_no, plate_no) VALUES (?, ?)",
                [(exhibit_no, p) for p in plates],
            )
            tx.executemany(
                "INSERT INTO dense_languages(exhibit_no, language) VALUES (?, ?)",
                [(exhibit_no, lang) for lang in languages],
            )
        else:
            tx.executemany(
                "INSERT INTO sparse_pairs(exhibit_no, plate_no, language) "
                "VALUES (?, ?, ?)",
                [(exhibit_no, p, lang) for p, lang in sorted(merged)],
            )

        tx.execute(
            "INSERT INTO storage_state(exhibit_no, storage_mode, updated_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
            "ON CONFLICT(exhibit_no) DO UPDATE SET "
            "storage_mode = excluded.storage_mode, "
            "updated_at = excluded.updated_at",
            (exhibit_no, new_mode),
        )

        result = MergeResult(
            exhibit_no=exhibit_no,
            storage_mode=new_mode,
            switched_from=previous_mode if previous_mode != new_mode else None,
            rows_in_file=rows_in_file,
            unique_rows=len(unique_incoming),
            new_pairs=len(merged - before),
            total_pairs=len(merged),
            plates=plates,
            languages=languages,
        )
        tx.execute(
            "INSERT INTO batch_log(exhibit_no, rows_in_file, unique_rows, new_pairs, "
            "total_pairs, storage_mode, switched_from, status, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', NULL)",
            (
                exhibit_no,
                result.rows_in_file,
                result.unique_rows,
                result.new_pairs,
                result.total_pairs,
                result.storage_mode,
                result.switched_from,
            ),
        )
        return result

    return run_with_immediate(conn, work)


def get_exhibit(conn: sqlite3.Connection, exhibit_no: str) -> dict | None:
    """网页 / API 查询：按展品和图版列出可印语种及当前存法。"""
    mode_row = conn.execute(
        "SELECT storage_mode, updated_at FROM storage_state WHERE exhibit_no = ?",
        (exhibit_no,),
    ).fetchone()
    if mode_row is None:
        return None

    mode = mode_row["storage_mode"]
    plates: list[str] = []
    languages: list[str] = []
    by_plate: dict[str, list[str]] = {}

    if mode == "dense":
        plates = [
            r["plate_no"]
            for r in conn.execute(
                "SELECT plate_no FROM dense_plates WHERE exhibit_no = ? ORDER BY plate_no",
                (exhibit_no,),
            )
        ]
        languages = [
            r["language"]
            for r in conn.execute(
                "SELECT language FROM dense_languages WHERE exhibit_no = ? ORDER BY language",
                (exhibit_no,),
            )
        ]
        by_plate = {p: list(languages) for p in plates}
    else:
        lang_set: set[str] = set()
        for r in conn.execute(
            "SELECT plate_no, language FROM sparse_pairs "
            "WHERE exhibit_no = ? ORDER BY plate_no, language",
            (exhibit_no,),
        ):
            by_plate.setdefault(r["plate_no"], []).append(r["language"])
            lang_set.add(r["language"])
        plates = sorted(by_plate)
        languages = sorted(lang_set)

    all_pairs = {(p, lang) for p, langs in by_plate.items() for lang in langs}
    missing: list[list[str]] = []
    if mode == "sparse":
        for p in plates:
            for lang in languages:
                if lang not in by_plate[p]:
                    missing.append([p, lang])

    batches = [
        dict(r)
        for r in conn.execute(
            "SELECT id, received_at, rows_in_file, unique_rows, new_pairs, "
            "total_pairs, storage_mode, switched_from, status, message "
            "FROM batch_log WHERE exhibit_no = ? ORDER BY id DESC",
            (exhibit_no,),
        )
    ]
    return {
        "exhibit_no": exhibit_no,
        "storage_mode": mode,
        "updated_at": mode_row["updated_at"],
        "plates": plates,
        "languages": languages,
        "by_plate": by_plate,
        "pair_count": len(all_pairs),
        "is_complete": not missing,
        "missing_pairs": missing,
        "batches": batches,
    }


def list_exhibits(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT s.exhibit_no, s.storage_mode, s.updated_at, "
            "(SELECT COUNT(*) FROM dense_plates d WHERE d.exhibit_no = s.exhibit_no) "
            "AS plate_count_dense, "
            "(SELECT COUNT(*) FROM dense_languages d WHERE d.exhibit_no = s.exhibit_no) "
            "AS language_count_dense, "
            "(SELECT COUNT(DISTINCT p.plate_no) FROM sparse_pairs p "
            " WHERE p.exhibit_no = s.exhibit_no) AS plate_count_sparse, "
            "(SELECT COUNT(DISTINCT p.language) FROM sparse_pairs p "
            " WHERE p.exhibit_no = s.exhibit_no) AS language_count_sparse "
            "FROM storage_state s ORDER BY s.exhibit_no"
        )
    ]
