"""SQLite 连接、建表与带重试的写事务封装。

并发补录的正确性依赖两点：

1. 所有写操作使用 ``BEGIN IMMEDIATE`` 立即获取 RESERVED 锁，
   同一展品的并发批次在 SQLite 层串行化，读-改-写不会丢更新；
2. ``busy_timeout`` + 应用层重试让锁等待对调用方透明。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

DEFAULT_DB_PATH = "/data/labels.db"
DEFAULT_BUSY_TIMEOUT_MS = 15000

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_state (
    exhibit_no   TEXT PRIMARY KEY,
    storage_mode TEXT NOT NULL CHECK (storage_mode IN ('dense', 'sparse')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS dense_plates (
    exhibit_no TEXT NOT NULL,
    plate_no   TEXT NOT NULL,
    PRIMARY KEY (exhibit_no, plate_no)
);

CREATE TABLE IF NOT EXISTS dense_languages (
    exhibit_no TEXT NOT NULL,
    language   TEXT NOT NULL,
    PRIMARY KEY (exhibit_no, language)
);

CREATE TABLE IF NOT EXISTS sparse_pairs (
    exhibit_no TEXT NOT NULL,
    plate_no   TEXT NOT NULL,
    language   TEXT NOT NULL,
    PRIMARY KEY (exhibit_no, plate_no, language)
);

CREATE TABLE IF NOT EXISTS batch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibit_no   TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    rows_in_file INTEGER NOT NULL,
    unique_rows  INTEGER NOT NULL,
    new_pairs    INTEGER NOT NULL,
    total_pairs  INTEGER NOT NULL,
    storage_mode TEXT NOT NULL,
    switched_from TEXT,
    status       TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    message      TEXT
);

-- 巡展分站：分站只存自己的差异（增补 add / 排除 exclude），
-- 可印搭配始终由「主展已登记数据 + 本站差异」在查询时即时合成。
-- parent_exhibit_no 刻意不加外键级联：主展补录/切换存法由存储层透明兼容，
-- 主展暂缺时分站差异仍保留，主展恢复登记后自动重新跟上。
CREATE TABLE IF NOT EXISTS stations (
    station_no        TEXT PRIMARY KEY,
    parent_exhibit_no TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS station_diffs (
    station_no TEXT NOT NULL,
    plate_no   TEXT NOT NULL,
    language   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('add', 'exclude')),
    PRIMARY KEY (station_no, plate_no, language),
    FOREIGN KEY (station_no) REFERENCES stations(station_no) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS station_diff_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station_no   TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    rows_in_file INTEGER NOT NULL,
    unique_rows  INTEGER NOT NULL,
    add_count    INTEGER NOT NULL,
    exclude_count INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sparse_exhibit   ON sparse_pairs(exhibit_no);
CREATE INDEX IF NOT EXISTS idx_dense_plate_ex   ON dense_plates(exhibit_no);
CREATE INDEX IF NOT EXISTS idx_dense_lang_ex    ON dense_languages(exhibit_no);
CREATE INDEX IF NOT EXISTS idx_batch_exhibit    ON batch_log(exhibit_no);
CREATE INDEX IF NOT EXISTS idx_station_parent   ON stations(parent_exhibit_no);
CREATE INDEX IF NOT EXISTS idx_station_diffs_no ON station_diffs(station_no);
CREATE INDEX IF NOT EXISTS idx_station_log_no   ON station_diff_log(station_no);
"""


def db_path() -> str:
    return os.environ.get("LABEL_DB_PATH", DEFAULT_DB_PATH)


def busy_timeout_ms() -> int:
    return int(os.environ.get("LABEL_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS)))


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开一个 autocommit 模式连接（事务由本模块显式管理）。"""
    conn = sqlite3.connect(
        path or db_path(),
        timeout=busy_timeout_ms() / 1000.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms()}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def open_db(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def run_with_immediate(
    conn: sqlite3.Connection,
    fn: Callable[[sqlite3.Connection], T],
    retries: int = 6,
) -> T:
    """在 ``BEGIN IMMEDIATE`` 事务中执行 ``fn``，锁忙时指数退避重试。

    每个 HTTP 请求 / 每个并发线程使用独立连接；拿不到写锁时重试，
    拿到锁后的读-改-写在单事务内完成，保证已提交资料不丢。
    """
    delay = 0.02
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
                return result
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:  # database is locked / busy
            last_error = exc
            if not _is_busy(exc):
                raise
            time.sleep(min(delay * (2**attempt), 1.0))
    assert last_error is not None
    raise last_error


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def main() -> None:
    """支持 ``python -m app.db`` 初始化数据库（容器入口调用）。"""
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    init_db(path)
    print(f"database initialized at {path}")


if __name__ == "__main__":
    main()
