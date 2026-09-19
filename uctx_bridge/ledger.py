"""共享轮次账本（ADR-002/003/004/005）：唯一权威历史源。

SQLite（标准库）+ WAL + ``BEGIN IMMEDIATE`` 事务：

- ``turns``：一轮一记录，``event_key`` 唯一约束吸收重复事件/重试/重复完成通知；
  ``(identity_key, epoch, seq)`` 唯一约束保证身份内顺序确定；
- ``meta``：身份级 ``current_epoch`` 与 ``seq`` 计数（事务内分配）；
- ``instance_leases``：同库双实例防护（启动租约 + 心跳）。

状态机（一轮只允许 running → 终态 一次）：
    running → completed | failed | aborted | interrupted

消息规范化（入库前 sanitize）：
- 丢弃隐藏思考（think parts）与 checkpoint 消息、``_no_save`` 标记部分；
- image_url/audio_url 内容降级为文本占位，杜绝 base64 与失效本地路径入库。

本模块不依赖 astrbot 运行时（消息为纯 dict），可独立单测。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"
STATUS_INTERRUPTED = "interrupted"
_TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED, STATUS_INTERRUPTED}
)

LEASE_FRESH_SECONDS = 300.0
"""其他实例租约心跳在该窗口内视为存活（双实例防护阈值）。"""

SCHEMA_VERSION = 3
"""0.7.0 W 返工结构：v3 = v2 列 + scope 段键编码（p:/u:/q: 前缀，
与 uctx_bridge.identity 的 SCOPE_* 常量镜像；本模块不导入 astrbot，
故此处独立定义，一致性由 tests/w4 键对拍保证）+ schema_version 表。
0.6.0（v1）与 0.7.0 首个候选（v2）账本由 migrate_ledger 原子迁移。"""

# —— scope 段编码（与 uctx_bridge.identity 镜像，勿单边修改）——
_SCOPE_USER_TOKEN = "u:"
_SCOPE_PERSONA_PREFIX = "p:"
_SCOPE_QUARANTINE_PREFIX = "q:"
_LEGACY_USER_TOKEN = "__mode_user__"

UNKNOWN_PERSONA = "__unknown__"


def _migrate_identity_key(raw: str) -> str:
    """旧身份键 → v3 编码；已是新编码原样返回。

    裸 scope：``__mode_user__`` → q:（0.7.0 首个候选键无法区分"真实
    同名人格"与 user 模式记录，按 W4 恢复规则隔离保留、不注入）；
    其余裸值 → p:<scope>（0.6.0 只有 persona 键）。
    """

    parts = raw.split("\x1f")
    if len(parts) != 4:
        return raw
    token = parts[2]
    if (
        token == _SCOPE_USER_TOKEN
        or token.startswith(_SCOPE_PERSONA_PREFIX)
        or token.startswith(_SCOPE_QUARANTINE_PREFIX)
    ):
        return raw
    if token == _LEGACY_USER_TOKEN:
        new_token = _SCOPE_QUARANTINE_PREFIX + token
    else:
        new_token = _SCOPE_PERSONA_PREFIX + token
    return "\x1f".join((parts[0], parts[1], new_token, parts[3]))


def inspect_schema_version(db_path) -> int:
    """只读检测账本 schema 版本（0.6.0=1，首个候选=2，当前=SCHEMA_VERSION）。

    全新/空库返回 SCHEMA_VERSION（无需迁移）；无法识别的结构抛
    MigrationError。只读打开，不写入。
    """

    db = Path(str(db_path))
    posix = urllib.parse.quote(str(db.resolve()).replace("\\", "/"))
    ro = sqlite3.connect(f"file:{posix}?mode=ro", uri=True, timeout=30.0)
    try:
        tables = {
            r[0]
            for r in ro.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not tables:
            return SCHEMA_VERSION  # 全新/空库
        if "schema_version" in tables:
            row = ro.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row is not None and row[0] is not None:
                return int(row[0])
            return 2
        if "turns" in tables and "meta" in tables:
            return 1
        raise MigrationError("无法识别的账本结构（缺 turns/meta 表）")
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"账本不可读（可能损坏）：{exc}") from exc
    finally:
        try:
            ro.close()
        except Exception:  # noqa: BLE001
            pass


def migrate_ledger(db_path, *, backup_dir=None):
    """把 0.6.0（schema v1）或 0.7.0 首个候选（v2）账本迁移到 v3。

    已迁移（schema_version >= 3）返回 False（幂等）。

    W3 修订后的流程：
    1. 只读连接校验输入（非 SQLite/缺表/结构异常 → MigrationError）；
    2. **一致性备份**：SQLite backup API 从只读连接整库复制（读快照
       包含全部已提交数据，含活跃 WAL 中已提交的事务；不使用
       checkpoint+复制主库的旧方案）；先写临时文件再原子改名，目标名
       唯一不覆盖既有备份；备份失败中止，原库不变；
    3. **单事务变更**：一个 BEGIN IMMEDIATE 内完成加列（v1）、全表
       identity_key 改写（p:/q: 编码）、source_persona 回填、
       schema_version=3 写入并提交；任一步失败 ROLLBACK——加列/回填/
       版本标记要么全部生效要么全部不存在，重试从头完整执行。
    """

    import time as _time

    db = Path(str(db_path))
    if not db.exists():
        raise MigrationError(f"账本不存在：{db}")
    posix = urllib.parse.quote(str(db.resolve()).replace("\\", "/"))
    ro = None
    try:
        ro = sqlite3.connect(f"file:{posix}?mode=ro", uri=True, timeout=30.0)
        ro.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in ro.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "schema_version" in tables:
            row = ro.execute("SELECT MAX(version) FROM schema_version").fetchone()
            source_version = int(row[0]) if row is not None and row[0] is not None else 2
        else:
            if "turns" not in tables or "meta" not in tables:
                raise MigrationError("输入不是 0.6.0 账本（缺 turns/meta 表）")
            cols = {r[1] for r in ro.execute("PRAGMA table_info(turns)").fetchall()}
            if "mode_generation" in cols or "source_persona" in cols:
                raise MigrationError("turns 已含 v2 列但缺 schema_version 表，结构异常")
            source_version = 1

        # -- 2) 一致性备份（backup API，覆盖 WAL 中已提交数据） -------------
        bdir = Path(backup_dir) if backup_dir else db.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        final = bdir / f"pre-migrate-v3-{stamp}-{db.name}"
        counter = 1
        while final.exists():
            final = bdir / f"pre-migrate-v3-{stamp}-{counter}-{db.name}"
            counter += 1
        tmp = final.with_name(final.name + ".tmp")
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                ro.backup(dst)  # 锁冲突/IO 失败抛 OperationalError，不静默
            finally:
                dst.close()
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"迁移失败：备份阶段出错，原库未变更（{exc}）") from exc
    finally:
        if ro is not None:
            try:
                ro.close()
            except Exception:  # noqa: BLE001
                pass

    # -- 3) 单事务变更 ------------------------------------------------------
    conn = sqlite3.connect(db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if source_version == 1:
                conn.execute(
                    "ALTER TABLE turns ADD COLUMN source_persona TEXT"
                    " NOT NULL DEFAULT '" + UNKNOWN_PERSONA + "'"
                )
                conn.execute(
                    "ALTER TABLE turns ADD COLUMN mode_generation INTEGER"
                    " NOT NULL DEFAULT 0"
                )
            # 键改写（v1/v2 都执行；已编码键原样保留）
            rows = conn.execute(
                "SELECT id, identity_key FROM turns"
            ).fetchall()
            for row in rows:
                new_key = _migrate_identity_key(row["identity_key"])
                if new_key != row["identity_key"]:
                    conn.execute(
                        "UPDATE turns SET identity_key=? WHERE id=?",
                        (new_key, row["id"]),
                    )
            meta_keys = conn.execute(
                "SELECT DISTINCT identity_key FROM meta"
            ).fetchall()
            for mrow in meta_keys:
                old = mrow["identity_key"]
                if len(old.split("\x1f")) != 4:
                    continue  # 3 段基础键（mode_generation 等）不含 scope
                new_key = _migrate_identity_key(old)
                if new_key != old:
                    conn.execute(
                        "UPDATE meta SET identity_key=? WHERE identity_key=?",
                        (new_key, old),
                    )
            # source_persona 回填：仅补缺失/未知（v2 已回填不覆盖）
            rows = conn.execute(
                "SELECT id, identity_key FROM turns WHERE source_persona IS NULL"
                " OR source_persona='' OR source_persona='" + UNKNOWN_PERSONA + "'"
            ).fetchall()
            for row in rows:
                persona = UNKNOWN_PERSONA
                parts = row["identity_key"].split("\x1f")
                if len(parts) == 4 and parts[2].startswith(_SCOPE_PERSONA_PREFIX):
                    candidate = parts[2][len(_SCOPE_PERSONA_PREFIX):]
                    if candidate and not candidate.startswith("__"):
                        persona = candidate
                conn.execute(
                    "UPDATE turns SET source_persona=? WHERE id=?",
                    (persona, row["id"]),
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, migrated_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, migrated_at)"
                " VALUES (?, ?)",
                (SCHEMA_VERSION, _time.time()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"迁移失败：已回滚，原库未变更（{exc}）") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def migrate_from_v1(db_path, *, backup_dir=None):
    """兼容别名（旧测试/文档引用）：等价 :func:`migrate_ledger`。"""

    return migrate_ledger(db_path, backup_dir=backup_dir)



_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    status TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    umo TEXT NOT NULL,
    user_message TEXT NOT NULL,
    trajectory TEXT,
    reply_text TEXT,
    send_state TEXT,
    lease_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    source_persona TEXT NOT NULL DEFAULT '',
    mode_generation INTEGER NOT NULL DEFAULT 0,
    UNIQUE (identity_key, epoch, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_event_key ON turns (event_key);
CREATE INDEX IF NOT EXISTS idx_turns_identity_epoch_seq
    ON turns (identity_key, epoch, seq);
CREATE TABLE IF NOT EXISTS meta (
    identity_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (identity_key, name)
);
CREATE TABLE IF NOT EXISTS instance_leases (
    lease_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    boot_id TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    migrated_at REAL NOT NULL
);
"""


class LedgerError(Exception):
    """账本错误基类。"""


class EpochStaleError(LedgerError):
    """提交时轮次所属 epoch 已不是当前 epoch（清空后的慢请求，拒绝写入）。"""


class MigrationError(LedgerError):
    """schema 迁移失败（输入损坏/中断），原库保持不变。"""


class LeaseConflictError(LedgerError):
    """另一存活实例持有同库租约（不支持同库双实例并发写）。"""


@dataclass(frozen=True)
class TurnRecord:
    id: int
    identity_key: str
    epoch: int
    seq: int
    event_key: str
    status: str
    source_type: str
    source_id: str
    umo: str
    user_message: dict[str, Any]
    trajectory: list[dict[str, Any]]
    reply_text: str | None
    source_persona: str = ""
    mode_generation: int = 0


@dataclass(frozen=True)
class LeaseInfo:
    lease_id: str
    pid: int
    heartbeat_at: float
    age_seconds: float


def sanitize_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """把一条 OpenAI/宿主形态消息规范化为可安全持久化的形态。

    返回 None 表示该消息不应入库（checkpoint / 全部内容被剔除）。
    """

    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role == "_checkpoint":
        return None
    content = message.get("content")
    if isinstance(content, list):
        kept: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str):
                    kept.append({"type": "text", "text": part})
                continue
            if part.get("_no_save"):
                continue
            ptype = part.get("type")
            if ptype == "think":
                continue  # 不保存隐藏思考
            if ptype == "image_url":
                kept.append({"type": "text", "text": "[图片]"})
                continue
            if ptype == "audio_url":
                kept.append({"type": "text", "text": "[音频]"})
                continue
            kept.append(part)
        if not kept and not message.get("tool_calls"):
            return None
        message = {**message, "content": kept}
    message.pop("_checkpoint_after", None)
    if "_no_save" in message:
        message = {k: v for k, v in message.items() if k != "_no_save"}
    return message


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        cleaned = sanitize_message(m)
        if cleaned is not None:
            out.append(cleaned)
    return out


class TurnLedger:
    """轮次账本。同一实例内线程安全；跨进程依赖租约防护。"""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- 生命周期 ---------------------------------------------------------
    def open(self, *, auto_migrate: bool = True) -> None:
        """打开账本。

        auto_migrate=True（默认）：旧库先迁移再连接（独立工具/测试路径）。
        auto_migrate=False：连接但**不写 schema**，若为旧库则置
        ``_legacy_pending``，由 :meth:`ensure_migrated` 在外部时序（如
        main 先取租约）下补迁移与建 schema——防止 executescript 把 v1
        库误标成当前版本。
        """

        with self._lock:
            if self._conn is not None:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            legacy_pending = False
            if Path(self._db_path).exists():
                version = inspect_schema_version(self._db_path)
                if version < SCHEMA_VERSION:
                    if auto_migrate:
                        migrate_ledger(self._db_path)
                    else:
                        legacy_pending = True
            conn = sqlite3.connect(
                self._db_path,
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,  # 显式事务控制
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._conn = conn
            self._legacy_pending = legacy_pending
            if not legacy_pending:
                self._finish_schema_locked()

    def _finish_schema_locked(self) -> None:
        """在已连接账本上补建 schema 与版本标记（持锁调用）。"""

        conn = self._require_conn()
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO schema_version (version, migrated_at) "
            "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION, time.time()),
        )
        self._legacy_pending = False

    def ensure_migrated(self) -> None:
        """补执行旧库迁移与 schema 收尾（main：取租约之后调用，W3）。

        迁移使用独立连接（含一致性备份）；完成后再在本连接上补建
        schema/版本。已是当前版本时为幂等空操作。
        """

        with self._lock:
            pending = getattr(self, "_legacy_pending", False)
            opened = self._conn is not None
        if not opened:
            raise LedgerError("账本未打开（先调用 open()）")
        if not pending:
            return
        migrate_ledger(self._db_path)
        with self._lock:
            self._finish_schema_locked()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:  # noqa: BLE001 - 尽力释放 WAL 文件
                    pass
                self._conn.close()
                self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise LedgerError("账本未打开（先调用 open()）")
        return self._conn

    def _tx(self) -> sqlite3.Connection:
        """进入 IMMEDIATE 事务（配合 with self._lock 串行）。"""

        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """容错回滚：事务已不在时忽略（异常路径可能先行回滚）。"""

        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # -- 模式代次（0.7.0） -------------------------------------------------
    @staticmethod
    def base_key_of(identity_key: str) -> str:
        """基础身份键（platformselfsender，剥离 scope 段）。"""

        parts = identity_key.split("")
        if len(parts) != 4:
            return identity_key
        return "".join((parts[0], parts[1], parts[3]))

    def current_mode_generation(self, identity_key: str) -> int:
        """读取基础身份的模式代次（缺省 0=v1 遗留代次）。"""

        with self._lock:
            base = self.base_key_of(identity_key)
            row = self._require_conn().execute(
                "SELECT value FROM meta WHERE name='mode_generation' AND identity_key=?",
                (base,),
            ).fetchone()
            return int(row["value"]) if row else 0

    def bump_mode_generation(self, identity_key: str) -> int:
        """模式切换：基础身份代次 +1（该基础身份全部旧代次记录变归档）。"""

        with self._lock:
            conn = self._tx()
            try:
                base = self.base_key_of(identity_key)
                row = conn.execute(
                    "SELECT value FROM meta WHERE name='mode_generation'"
                    " AND identity_key=?",
                    (base,),
                ).fetchone()
                new_gen = (int(row["value"]) + 1) if row else 1
                conn.execute(
                    "INSERT INTO meta (identity_key, name, value)"
                    " VALUES (?, 'mode_generation', ?)"
                    " ON CONFLICT(identity_key, name) DO UPDATE SET value=excluded.value",
                    (base, str(new_gen)),
                )
                conn.execute("COMMIT")
                return new_gen
            except Exception:
                self._rollback(conn)
                raise

    # -- 模式持久化（W1：真实 reload/restart 识别模式变化） ----------------
    def get_scope_mode(self, base_key: str) -> str | None:
        """读取基础身份已生效（持久化）的共享模式；未记录返回 None。"""

        with self._lock:
            row = self._require_conn().execute(
                "SELECT value FROM meta WHERE identity_key=? AND name='scope_mode'",
                (base_key,),
            ).fetchone()
            return row["value"] if row else None

    def apply_scope_mode(self, base_key: str, mode: str) -> tuple[int, bool]:
        """记录基础身份的生效模式；显式模式变化时代次 +1。

        单事务完成"读旧 → 判定 → 写 scope_mode + mode_generation"：
        - 未记录（0.6.0/新基础身份）：写入当前模式，代次**不变**（升级
          兼容：旧有效历史保持当前）；
        - 已记录且相同：不写不动（同配置 reload/重启不清空）；
        - 已记录且不同：代次 +1（该基础身份旧代次记录全部归档）。
        返回 (生效代次, 是否发生显式切换)。
        """

        if mode not in ("persona", "user"):
            raise LedgerError(f"非法共享模式：{mode!r}")
        with self._lock:
            conn = self._tx()
            try:
                gen_row = conn.execute(
                    "SELECT value FROM meta WHERE identity_key=?"
                    " AND name='mode_generation'",
                    (base_key,),
                ).fetchone()
                gen = int(gen_row["value"]) if gen_row else 0
                mode_row = conn.execute(
                    "SELECT value FROM meta WHERE identity_key=?"
                    " AND name='scope_mode'",
                    (base_key,),
                ).fetchone()
                recorded = mode_row["value"] if mode_row else None
                if recorded == mode:
                    conn.execute("COMMIT")
                    return gen, False
                # 首次记录（recorded is None）只登记模式不改代次——升级兼容，
                # 不算显式切换；已记录且不同才算切换
                changed = recorded is not None
                new_gen = gen + 1 if changed else gen
                for name, value in (
                    ("scope_mode", mode),
                    ("mode_generation", str(new_gen)),
                ):
                    conn.execute(
                        "INSERT INTO meta (identity_key, name, value)"
                        " VALUES (?, ?, ?)"
                        " ON CONFLICT(identity_key, name) DO UPDATE"
                        " SET value=excluded.value",
                        (base_key, name, value),
                    )
                conn.execute("COMMIT")
                return new_gen, changed
            except Exception:
                self._rollback(conn)
                raise

    def enumerate_base_keys(self) -> list[str]:
        """全部基础身份键（turns 与 meta 的并集；模式对账/转换枚举用）。"""

        with self._lock:
            conn = self._require_conn()
            bases: set[str] = set()
            for (k,) in conn.execute(
                "SELECT DISTINCT identity_key FROM turns"
            ).fetchall():
                bases.add(self.base_key_of(k))
            for (k,) in conn.execute(
                "SELECT DISTINCT identity_key FROM meta"
            ).fetchall():
                parts = k.split("\x1f")
                if len(parts) == 3:
                    bases.add(k)
                elif len(parts) == 4:
                    bases.add(self.base_key_of(k))
            return sorted(bases)

    def identity_stats(self, identity_key: str) -> dict:
        """身份维度统计（W5 status 用；与读取同一连接，时点一致）。

        completed_current 按该身份当前 epoch + 当前模式代次的有效口径
        统计；total_all 为全部 epoch/代次/状态的轮次总数；last_turn_at
        为最近一轮创建时间（无记录为 None——"尚无接管证据"的依据）。
        有效口径按有无 epoch 元数据二选一，使用两条固定字面量查询，
        不做 SQL 文本拼接。
        """

        with self._lock:
            conn = self._require_conn()
            cur_epoch: int | None = None
            cur_gen = 0
            for row in conn.execute(
                "SELECT name, value FROM meta WHERE identity_key=?",
                (identity_key,),
            ).fetchall():
                if row["name"] == "epoch":
                    cur_epoch = int(row["value"])
                elif row["name"] == "mode_generation":
                    cur_gen = int(row["value"])
            total_row = conn.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS last_at"
                " FROM turns WHERE identity_key=?",
                (identity_key,),
            ).fetchone()
            if cur_epoch is not None:
                completed_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM turns WHERE identity_key=?"
                    " AND status='completed' AND epoch=? AND mode_generation=?",
                    (identity_key, cur_epoch, cur_gen),
                ).fetchone()
            else:
                completed_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM turns WHERE identity_key=?"
                    " AND status='completed'",
                    (identity_key,),
                ).fetchone()
            return {
                "completed_current": int(completed_row["c"]),
                "total_all": int(total_row["c"]),
                "last_turn_at": (
                    float(total_row["last_at"])
                    if total_row["last_at"] is not None
                    else None
                ),
                "current_epoch": cur_epoch,
                "current_mode_generation": cur_gen,
            }

    # -- epoch / seq ------------------------------------------------------
    def current_epoch(self, identity_key: str) -> int:
        with self._lock:
            row = self._require_conn().execute(
                "SELECT value FROM meta WHERE identity_key=? AND name='epoch'",
                (identity_key,),
            ).fetchone()
            return int(row["value"]) if row else 1

    def bump_epoch(self, identity_key: str) -> int:
        """身份级清空：epoch+1。旧 epoch 数据保留可审计，读取只认当前 epoch。"""

        with self._lock:
            conn = self._tx()
            try:
                cur = int(
                    conn.execute(
                        "SELECT value FROM meta WHERE identity_key=? AND name='epoch'",
                        (identity_key,),
                    ).fetchone()["value"]
                ) if conn.execute(
                    "SELECT 1 FROM meta WHERE identity_key=? AND name='epoch'",
                    (identity_key,),
                ).fetchone() else 0
                new_epoch = cur + 1
                conn.execute(
                    "INSERT INTO meta (identity_key, name, value) VALUES (?, 'epoch', ?) "
                    "ON CONFLICT(identity_key, name) DO UPDATE SET value=excluded.value",
                    (identity_key, str(new_epoch)),
                )
                conn.execute("COMMIT")
                return new_epoch
            except Exception:
                self._rollback(conn)
                raise

    # -- 轮次写入 ---------------------------------------------------------
    def begin_turn(
        self,
        *,
        identity_key: str,
        event_key: str,
        source_type: str,
        source_id: str,
        umo: str,
        user_message: dict[str, Any],
        lease_id: str | None = None,
        source_persona: str = "",
    ) -> TurnRecord:
        """登记 running 轮次；``event_key`` 重复时幂等返回既有记录（去重）。"""

        cleaned_user = sanitize_message(user_message)
        if cleaned_user is None:
            cleaned_user = {"role": "user", "content": ""}
        now = time.time()
        with self._lock:
            conn = self._tx()
            try:
                existing = conn.execute(
                    "SELECT * FROM turns WHERE event_key=?", (event_key,)
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return self._row_to_record(existing)
                # 确保身份 epoch 行存在（首次为 1），bump_epoch 才能正确递增
                conn.execute(
                    "INSERT INTO meta (identity_key, name, value) VALUES (?, 'epoch', '1') "
                    "ON CONFLICT(identity_key, name) DO NOTHING",
                    (identity_key,),
                )
                epoch = int(
                    conn.execute(
                        "SELECT value FROM meta WHERE identity_key=? AND name='epoch'",
                        (identity_key,),
                    ).fetchone()["value"]
                )
                seq_row = conn.execute(
                    "SELECT value FROM meta WHERE identity_key=? AND name='seq'",
                    (identity_key,),
                ).fetchone()
                seq = (int(seq_row["value"]) + 1) if seq_row else 1
                conn.execute(
                    "INSERT INTO meta (identity_key, name, value) VALUES (?, 'seq', ?) "
                    "ON CONFLICT(identity_key, name) DO UPDATE SET value=excluded.value",
                    (identity_key, str(seq)),
                )
                conn.execute(
                    "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
                    " source_type, source_id, umo, user_message, trajectory, reply_text,"
                    " send_state, lease_id, created_at, updated_at,"
                    " source_persona, mode_generation)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?)",
                    (
                        identity_key,
                        epoch,
                        seq,
                        event_key,
                        STATUS_RUNNING,
                        source_type,
                        source_id,
                        umo,
                        json.dumps(cleaned_user, ensure_ascii=False),
                        None,
                        lease_id,
                        now,
                        now,
                        source_persona,
                        self.current_mode_generation(identity_key),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM turns WHERE event_key=?", (event_key,)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_record(row)
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                # 并发下唯一约束兜底：再读一次（幂等吸收）
                row = self._require_conn().execute(
                    "SELECT * FROM turns WHERE event_key=?", (event_key,)
                ).fetchone()
                if row is not None:
                    return self._row_to_record(row)
                raise LedgerError(f"begin_turn 唯一约束冲突且无法回读：{exc}") from exc
            except Exception:
                self._rollback(conn)
                raise

    def commit_turn(
        self,
        *,
        event_key: str,
        status: str,
        trajectory: list[dict[str, Any]] | None = None,
        reply_text: str | None = None,
    ) -> TurnRecord | None:
        """把 running 轮次终态化。

        - 状态机：仅 running 可提交；重复完成通知返回当前记录（幂等）。
        - epoch 防复活：提交事务内校验轮次 epoch 仍为该身份当前 epoch，
          否则抛 :class:`EpochStaleError`（清空后的慢请求被丢弃，不复活旧历史）。
        """

        if status not in _TERMINAL_STATUSES:
            raise LedgerError(f"非法终态：{status}")
        cleaned_traj = sanitize_messages(trajectory or [])
        now = time.time()
        with self._lock:
            conn = self._tx()
            try:
                row = conn.execute(
                    "SELECT * FROM turns WHERE event_key=?", (event_key,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                if row["status"] != STATUS_RUNNING:
                    record = self._row_to_record(row)
                    conn.execute("COMMIT")
                    return record  # 幂等：一轮只写一次
                epoch_row = conn.execute(
                    "SELECT value FROM meta WHERE identity_key=? AND name='epoch'",
                    (row["identity_key"],),
                ).fetchone()
                current = int(epoch_row["value"]) if epoch_row else 1
                if row["epoch"] != current:
                    raise EpochStaleError(
                        f"轮次 epoch={row['epoch']} 已过期（当前 {current}），拒绝复活"
                    )
                conn.execute(
                    "UPDATE turns SET status=?, trajectory=?, reply_text=?, updated_at=? "
                    "WHERE id=?",
                    (
                        status,
                        json.dumps(cleaned_traj, ensure_ascii=False),
                        reply_text,
                        now,
                        row["id"],
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM turns WHERE id=?", (row["id"],)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_record(updated)
            except Exception:
                self._rollback(conn)
                raise

    def mark_turn_sent(self, event_key: str) -> bool:
        """发送成功标记（OnAfterMessageSentEvent）。NULL=未确认/发送失败可识别。"""

        with self._lock:
            conn = self._tx()
            try:
                cur = conn.execute(
                    "UPDATE turns SET send_state='sent', updated_at=? WHERE event_key=?",
                    (time.time(), event_key),
                )
                conn.execute("COMMIT")
                return cur.rowcount > 0
            except Exception:
                self._rollback(conn)
                raise

    # -- 读取 -------------------------------------------------------------
    def get_turn(self, event_key: str) -> TurnRecord | None:
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM turns WHERE event_key=?", (event_key,)
            ).fetchone()
            return self._row_to_record(row) if row else None

    def load_history(
        self,
        identity_key: str,
        *,
        max_turns: int | None = None,
    ) -> list[dict[str, Any]]:
        """当前 epoch 已完成轮次的消息序列（按 seq 升序展开 user+轨迹）。

        ``max_turns`` 从尾部保留最近 N 轮（长上下文裁剪，A13）。
        """

        epoch = self.current_epoch(identity_key)
        mode_gen = self.current_mode_generation(identity_key)
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM turns WHERE identity_key=? AND epoch=? AND status=?"
                " AND mode_generation=? ORDER BY seq ASC",
                (identity_key, epoch, STATUS_COMPLETED, mode_gen),
            ).fetchall()
        if max_turns is not None and max_turns >= 0 and len(rows) > max_turns:
            rows = rows[-max_turns:]
        messages: list[dict[str, Any]] = []
        for row in rows:
            record = self._row_to_record(row)
            messages.append(record.user_message)
            messages.extend(record.trajectory)
        return messages

    def running_turns(self, identity_key: str | None = None) -> list[TurnRecord]:
        with self._lock:
            if identity_key is None:
                rows = self._require_conn().execute(
                    "SELECT * FROM turns WHERE status=?", (STATUS_RUNNING,)
                ).fetchall()
            else:
                rows = self._require_conn().execute(
                    "SELECT * FROM turns WHERE identity_key=? AND status=?",
                    (identity_key, STATUS_RUNNING),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def recover_running(
        self,
        *,
        own_lease_id: str | None = None,
        stale_before: float | None = None,
    ) -> int:
        """重启恢复：把遗留 running 轮次终态化为 interrupted。

        - 自己旧租约（own_lease_id）的 running：全部恢复；
        - 其他租约：heartbeat 早于 stale_before 的恢复；新鲜的保留（实例可能存活）。
        """

        now = time.time()
        if stale_before is None:
            stale_before = now - LEASE_FRESH_SECONDS
        recovered = 0
        with self._lock:
            conn = self._tx()
            try:
                rows = conn.execute(
                    "SELECT t.*, l.heartbeat_at FROM turns t"
                    " LEFT JOIN instance_leases l ON l.lease_id = t.lease_id"
                    " WHERE t.status=?",
                    (STATUS_RUNNING,),
                ).fetchall()
                for row in rows:
                    lease_id = row["lease_id"]
                    if own_lease_id is not None and lease_id == own_lease_id:
                        recoverable = True
                    elif lease_id is None:
                        recoverable = True  # 无租约归属的遗留轮次
                    else:
                        hb = row["heartbeat_at"]
                        recoverable = hb is None or hb <= stale_before
                    if recoverable:
                        conn.execute(
                            "UPDATE turns SET status=?, updated_at=? WHERE id=?",
                            (STATUS_INTERRUPTED, now, row["id"]),
                        )
                        recovered += 1
                conn.execute("COMMIT")
                return recovered
            except Exception:
                self._rollback(conn)
                raise

    # -- 实例租约 ---------------------------------------------------------
    def acquire_lease(
        self,
        lease_id: str,
        *,
        pid: int | None = None,
        owner_token: str | None = None,
    ) -> str:
        """登记本实例写租约，返回本次调用的 owner_token。

        存在其他**新鲜**租约（心跳在 LEASE_FRESH_SECONDS 内）时抛
        LeaseConflictError——同库双实例并发写不受支持；过期租钥被清理。
        owner_token 用于租约归属校验：只有持有 token 的实例能对本租约
        心跳或释放（R6：活跃实例不能被另一实例覆盖或释放）。
        """

        now = time.time()
        pid = pid if pid is not None else os.getpid()
        boot_id = uuid.uuid4().hex
        if owner_token is None:
            owner_token = uuid.uuid4().hex
        with self._lock:
            conn = self._tx()
            try:
                rows = conn.execute(
                    "SELECT * FROM instance_leases WHERE lease_id != ?", (lease_id,)
                ).fetchall()
                for row in rows:
                    if row["heartbeat_at"] > now - LEASE_FRESH_SECONDS:
                        conflict = LeaseInfo(
                            lease_id=row["lease_id"],
                            pid=row["pid"],
                            heartbeat_at=row["heartbeat_at"],
                            age_seconds=now - row["heartbeat_at"],
                        )
                        conn.execute("ROLLBACK")
                        raise LeaseConflictError(
                            f"另一实例租约仍新鲜（pid={conflict.pid}, "
                            f"心跳 {conflict.age_seconds:.0f}s 前）；"
                            "同库双实例并发写不受支持"
                        )
                # 清理过期租约与本租钥旧记录
                conn.execute(
                    "DELETE FROM instance_leases WHERE heartbeat_at <= ?",
                    (now - LEASE_FRESH_SECONDS,),
                )
                conn.execute(
                    "DELETE FROM instance_leases WHERE lease_id=?", (lease_id,)
                )
                conn.execute(
                    "INSERT INTO instance_leases (lease_id, pid, boot_id, owner_token,"
                    " acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
                    (lease_id, pid, boot_id, owner_token, now, now),
                )
                conn.execute("COMMIT")
                return owner_token
            except LeaseConflictError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def heartbeat_lease(self, lease_id: str, owner_token: str) -> None:
        with self._lock:
            conn = self._tx()
            try:
                cur = conn.execute(
                    "UPDATE instance_leases SET heartbeat_at=?"
                    " WHERE lease_id=? AND owner_token=?",
                    (time.time(), lease_id, owner_token),
                )
                if cur.rowcount == 0:
                    conn.execute("COMMIT")
                    raise LedgerError("租约不存在或归属校验失败，拒绝心跳")
                conn.execute("COMMIT")
            except Exception:
                self._rollback(conn)
                raise

    def release_lease(self, lease_id: str, owner_token: str) -> None:
        """释放本实例租约；owner_token 不匹配（他人租约）时拒绝。"""

        with self._lock:
            conn = self._tx()
            try:
                cur = conn.execute(
                    "DELETE FROM instance_leases WHERE lease_id=? AND owner_token=?",
                    (lease_id, owner_token),
                )
                if cur.rowcount == 0:
                    conn.execute("COMMIT")
                    raise LedgerError("租约不存在或归属校验失败，拒绝释放")
                conn.execute("COMMIT")
            except Exception:
                self._rollback(conn)
                raise

    def active_leases(self) -> list[LeaseInfo]:
        now = time.time()
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM instance_leases"
            ).fetchall()
            return [
                LeaseInfo(
                    lease_id=r["lease_id"],
                    pid=r["pid"],
                    heartbeat_at=r["heartbeat_at"],
                    age_seconds=now - r["heartbeat_at"],
                )
                for r in rows
            ]

    # -- 内部 -------------------------------------------------------------
    def _row_to_record(self, row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            id=row["id"],
            identity_key=row["identity_key"],
            epoch=row["epoch"],
            seq=row["seq"],
            event_key=row["event_key"],
            status=row["status"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            umo=row["umo"],
            user_message=json.loads(row["user_message"]),
            trajectory=json.loads(row["trajectory"]) if row["trajectory"] else [],
            reply_text=row["reply_text"],
            source_persona=(
                row["source_persona"] if "source_persona" in row.keys() else ""
            ),
            mode_generation=(
                row["mode_generation"] if "mode_generation" in row.keys() else 0
            ),
        )
