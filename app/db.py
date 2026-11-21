"""SQLite 存储层：连接、建表。"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
-- 有效工单（每号仅保留最高修订号的一版）
CREATE TABLE IF NOT EXISTS work_orders (
    order_no  TEXT PRIMARY KEY,
    revision  INTEGER NOT NULL,
    ts        TEXT NOT NULL,          -- UTC ISO8601，微秒精度，字典序即时间序
    car_no    TEXT NOT NULL,
    mileage   INTEGER NOT NULL,
    content   TEXT NOT NULL           -- 规范化 JSON，用于同版同内容幂等判定
);

-- 工单轴位明细
CREATE TABLE IF NOT EXISTS order_changes (
    order_no  TEXT NOT NULL REFERENCES work_orders(order_no) ON DELETE CASCADE,
    position  TEXT NOT NULL,
    off_part  TEXT,
    on_part   TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_order ON order_changes(order_no);

-- 由全部有效工单重放得到的装车段（派生表，每次成功提交后整体重建）
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    part_no       TEXT NOT NULL,
    car_no        TEXT NOT NULL,
    position      TEXT NOT NULL,
    start_order   TEXT NOT NULL,
    start_ts      TEXT NOT NULL,
    start_mileage INTEGER NOT NULL,
    end_order     TEXT,               -- NULL 表示在役段
    end_ts        TEXT,
    end_mileage   INTEGER,
    generation    INTEGER             -- NULL=未分代；1=旧件一代；2=新件二代
);
CREATE INDEX IF NOT EXISTS idx_segments_part ON segments(part_no);
CREATE INDEX IF NOT EXISTS idx_segments_car ON segments(car_no);

-- 分代登记：同一铭牌号对应两件实物时，登记旧件最后一次卸车工单
-- 与新件第一次装车工单；每个件号至多一条，登记后不可改写（换一对即冲突）
CREATE TABLE IF NOT EXISTS part_generations (
    part_no         TEXT PRIMARY KEY,
    old_end_order   TEXT NOT NULL,    -- 旧件最后一次卸车工单号
    new_start_order TEXT NOT NULL,    -- 新件第一次装车工单号
    old_end_ts      TEXT NOT NULL,    -- 登记时刻快照，便于查询展示
    new_start_ts    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or db_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # isolation_level=None：自动提交，事务由 core 显式 BEGIN/COMMIT 控制
    conn = sqlite3.connect(p, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # 读写不互斥，查询只读最近完整提交
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 轻量迁移：旧库的 segments 表没有 generation 列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
    if "generation" not in cols:
        conn.execute("ALTER TABLE segments ADD COLUMN generation INTEGER")
