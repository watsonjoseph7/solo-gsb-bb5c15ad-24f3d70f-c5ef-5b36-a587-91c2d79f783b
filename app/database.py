"""SQLite 数据库连接与表结构初始化。"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '',   -- 逗号分隔的过敏原
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    version         TEXT NOT NULL,
    name            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(code, version)
);

CREATE TABLE IF NOT EXISTS recipe_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    ingredient_code TEXT NOT NULL,
    quantity        REAL,
    unit            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS substitutions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL,
    target_code TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE(source_code, target_code)
);

-- 配方变更审计：只追加（append-only）的不可变事件流。
-- 业务侧仅允许 INSERT；UPDATE/DELETE 由下方触发器在数据库层直接拒绝，
-- 因此即使应用代码有缺陷、或导入 replace 清空业务表，审计事件也不会被改写或抹除。
-- 不设指向 recipes(id) 的外键：删除配方后事件仍须保留，recipe_id 仅作快照引用。
CREATE TABLE IF NOT EXISTS recipe_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL CHECK (event_type IN ('created', 'updated', 'deleted')),
    recipe_id     INTEGER,                -- 删除后该 id 不再存在于 recipes，故仅快照保留
    recipe_code   TEXT NOT NULL,          -- 冗余代码列，便于按配方查询（更新改码时取旧码）
    occurred_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),  -- UTC ISO8601
    before_snapshot TEXT,                 -- 变更前完整配料快照 JSON，创建时为 NULL
    after_snapshot  TEXT,                 -- 变更后完整配料快照 JSON，删除时为 NULL
    summary       TEXT NOT NULL DEFAULT ''  -- 服务端生成的可读差异摘要
);

CREATE INDEX IF NOT EXISTS idx_items_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_sub_source ON substitutions(source_code);
CREATE INDEX IF NOT EXISTS idx_sub_target ON substitutions(target_code);
CREATE INDEX IF NOT EXISTS idx_events_recipe_code ON recipe_events(recipe_code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON recipe_events(occurred_at);

-- 不可变保护：任何更新/删除审计事件的尝试都直接失败
CREATE TRIGGER IF NOT EXISTS trg_recipe_events_no_update
BEFORE UPDATE ON recipe_events
BEGIN
    SELECT RAISE(ABORT, 'recipe_events 是不可变审计表，禁止 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_recipe_events_no_delete
BEFORE DELETE ON recipe_events
BEGIN
    SELECT RAISE(ABORT, 'recipe_events 是不可变审计表，禁止 DELETE');
END;
"""


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：FastAPI 的同步生成器依赖（创建连接 / 端点使用 /
    # 关闭连接）会被分别调度到线程池的不同工作线程，而 sqlite3 默认禁止
    # 跨线程使用同一连接。每个请求都在 conn_dep 中新建独立连接，不存在共享。
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
