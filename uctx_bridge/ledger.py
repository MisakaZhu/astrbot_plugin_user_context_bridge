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
"""


class LedgerError(Exception):
    """账本错误基类。"""


class EpochStaleError(LedgerError):
    """提交时轮次所属 epoch 已不是当前 epoch（清空后的慢请求，拒绝写入）。"""


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
    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
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
            conn.executescript(_SCHEMA)
            self._conn = conn

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
                    " send_state, lease_id, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
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
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM turns WHERE identity_key=? AND epoch=? AND status=?"
                " ORDER BY seq ASC",
                (identity_key, epoch, STATUS_COMPLETED),
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
        )
