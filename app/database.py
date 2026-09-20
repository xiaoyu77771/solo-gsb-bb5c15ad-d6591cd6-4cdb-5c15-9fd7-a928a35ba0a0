"""SQLite 存储层：连接管理与建表。

数据库路径通过环境变量 DATABASE_PATH 配置，默认 ./names.db。
表结构对 display_names (namespace, normalized_name) 做唯一约束，在数据库
层面兜底规范名重复；同形字符（confusable）判定在应用层完成。
revision 为乐观锁版本号：初始为 1，每次原子改名成功后递增，PATCH 改名
接口以 expected_revision 做条件更新。

name_reservations 保存“预占后确认注册”的预占记录：token 不可猜测且唯一，
expires_at 为 UTC 过期时刻，consumed_at 在确认注册成功后回写（NULL 表示
尚未消费）。未过期、未消费的预占与 display_names 一样参与冲突判定。

建表语句全部幂等（IF NOT EXISTS），应用启动时执行即自动完成数据库迁移：
老版本 display_names 缺少 revision 列时，会以 ALTER TABLE ... ADD COLUMN
（默认值 1，既包含在表定义中也在迁移语句中显式给出）幂等补齐，既有数据库
无需额外迁移步骤。

namespace_script_policies 保存命名空间级混脚本策略：每个命名空间至多一行，
blocked_script_pairs 以 JSON 文本保存规范对列表（无序、去重、字典序排列），
revision 为乐观锁版本号（初始为 1，每次成功 PUT 递增；未配置的命名空间
逻辑版本视为 0，首次 PUT 必须携带 expected_revision=0）。未配置策略的
命名空间沿用内建默认策略（Latin/Cyrillic/Greek 任意两种混用即违规）。

name_cooldowns 保存“名称释放冷却”记录：DELETE 已注册名称时在同一事务内
删除 display_names 行并写入本表，expires_at 为 UTC 冷却到期时刻。未过期的
冷却记录与 display_names / 有效预占一样参与全部冲突与混脚本判定；到期记录
在后续写事务中惰性物理删除。建表同样幂等（IF NOT EXISTS），老版本数据库
启动时自动获得该表，无需手工迁移。
"""
from __future__ import annotations

import os
import sqlite3

DATABASE_PATH = os.environ.get("DATABASE_PATH", "names.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS display_names (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (namespace, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_display_names_namespace
    ON display_names (namespace);

CREATE TABLE IF NOT EXISTS name_reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    token           TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 冲突检测的热路径：按命名空间取未消费预占，再按 expires_at 过滤
CREATE INDEX IF NOT EXISTS idx_name_reservations_namespace_active
    ON name_reservations (namespace, consumed_at, expires_at);

CREATE TABLE IF NOT EXISTS namespace_script_policies (
    namespace            TEXT PRIMARY KEY,
    blocked_script_pairs TEXT NOT NULL,
    revision             INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS name_cooldowns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace        TEXT NOT NULL,
    original_name    TEXT NOT NULL,
    normalized_name  TEXT NOT NULL,
    cooldown_seconds INTEGER NOT NULL,
    expires_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 冲突检测的热路径：按命名空间取冷却记录，再按 expires_at 过滤
CREATE INDEX IF NOT EXISTS idx_name_cooldowns_namespace
    ON name_cooldowns (namespace, expires_at);
"""


# 既有数据库的列迁移：老版本 display_names 没有 revision 列。
# ALTER TABLE ADD COLUMN 带 NOT NULL 必须给出非 NULL 默认值；以
# "SELECT ... FROM pragma_table_info" 检查列是否存在，使迁移幂等可重入。
MIGRATIONS = (
    """
    ALTER TABLE display_names
        ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
    """,
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """幂等补齐缺失列：已存在则跳过，重复执行不报错。"""
    columns = {
        row["name"]
        for row in conn.execute("SELECT name FROM pragma_table_info('display_names')")
    }
    if "revision" not in columns:
        conn.execute(MIGRATIONS[0])


def init_db(path: str | None = None) -> str:
    """建库建表并补齐迁移列（幂等），返回实际使用的数据库路径。"""
    target = path or DATABASE_PATH
    if target != ":memory:":
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    return target


def get_connection() -> sqlite3.Connection:
    """新建一个连接（按行名字典化返回），由调用方负责关闭。

    check_same_thread=False：FastAPI 依赖与端点可能运行在不同线程池
    线程中；每个连接仍只属一个请求、串行使用，跨线程访问是安全的。
    """
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # 写事务（BEGIN IMMEDIATE）竞争时等待对端提交，而不是立刻 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


if __name__ == "__main__":
    print(f"database initialized: {init_db()}")
