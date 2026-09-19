#!/usr/bin/env python3
"""uctx_records —— astrbot_plugin_user_context_bridge 本地只读记录工具（MIS-168 / ADR-015）。

用途：在本机（运行 AstrBot 的电脑/服务器）直接查看与导出插件账本
``uctx_ledger.db`` 中的共享轮次记录，不启动 AstrBot、不导入会注册
handler 的 main、不申请写租约、不需要 QQ 登录/模型密钥/网络。

子命令：
    list         列出库中的身份分区（仅管理元数据，不输出聊天正文）
    export-json  按筛选导出 JSON（程序读取入口，含 schema 说明）
    export-html  按筛选导出可离线浏览的自包含 HTML

只读保证：
- 以 SQLite ``mode=ro`` URI 打开（只读；尊重活跃 WAL，不用 immutable）；
- meta（当前 epoch/代次）与 turns 在同一读事务快照内读取，不混合时点；
- 不建表、不恢复 running、不改 epoch/租约；失败不写源库。

时间约定：输出为本地时区 ISO8601（含偏移）；``--from``/``--to`` 接受
ISO8601（无偏移按本地时间解释），起点包含、终点不包含。

旧库提示：0.6.0（schema v1）账本需先由新版插件启动迁移；本工具不自行
升级旧库，检测到 v1 时明确报错退出。

典型用法（Windows PowerShell / Linux / 容器挂载数据目录均适用）：
    python tools/uctx_records.py list --db data/plugin_data/astrbot_plugin_user_context_bridge/uctx_ledger.db
    python tools/uctx_records.py export-json --db .../uctx_ledger.db --platform aiocqhttp --sender 123456 --out export.json
    python tools/uctx_records.py export-html --db .../uctx_ledger.db --from 2026-09-01T00:00:00 --out export.html
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import sqlite3
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SEP = "\x1f"
"""身份键四段分隔符（与 uctx_bridge.identity 一致）。"""

# scope 段编码（与 uctx_bridge.identity / ledger 镜像；W4）
SCOPE_USER_TOKEN = "u:"
SCOPE_PERSONA_PREFIX = "p:"
SCOPE_QUARANTINE_PREFIX = "q:"

VALID_STATUSES = ("completed", "failed", "aborted", "interrupted", "running")
DEFAULT_STATUSES = ("completed",)
DEFAULT_LIMIT = 500
EXPORT_SCHEMA_VERSION = 1

_MEDIA_PART_NOTE = "[媒体/结构片段：{kind}，不内联渲染]"


class RecordsError(Exception):
    """工具级受控错误（参数/输入库/输出路径问题）。"""


# ---------------------------------------------------------------------------
# 只读快照
# ---------------------------------------------------------------------------


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise RecordsError(f"数据库不存在：{path}")
    posix = str(path.resolve()).replace("\\", "/")
    uri = "file:" + urllib.parse.quote(posix) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    conn.isolation_level = None
    return conn


def ledger_schema_version(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    if "schema_version" not in names:
        return 1
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row is None or row[0] is None:
        return 2
    return int(row[0])


@dataclass
class Snapshot:
    db_path: Path
    schema_version: int
    current: dict[str, dict[str, int]]
    turns: list[dict]


def load_snapshot(db_path: str | Path) -> Snapshot:
    """只读加载一致快照：meta（当前 epoch/代次）与 turns 同一读事务。"""

    path = Path(db_path)
    conn = open_readonly(path)
    try:
        conn.execute("BEGIN")
        try:
            version = ledger_schema_version(conn)
            if version < 2:
                raise RecordsError(
                    "该账本为 0.6.0（schema v1）结构，请先用新版插件启动一次完成"
                    "自动迁移后再查询；本工具不自行升级数据库。"
                )
            current: dict[str, dict[str, int]] = {}
            meta_rows = conn.execute(
                "SELECT identity_key, name, value FROM meta"
            ).fetchall()
            for key, name, value in meta_rows:
                # epoch 按 4 段身份键存；mode_generation 按基础身份（3 段）存，
                # 与 uctx_bridge.ledger 写侧一致。
                if name == "epoch":
                    current.setdefault(key, {})["epoch"] = int(value)
                elif name == "mode_generation":
                    current.setdefault(base_key_of(key), {})["gen"] = int(value)
            cur = conn.execute(
                "SELECT * FROM turns ORDER BY created_at ASC, id ASC"
            )
            cols = [d[0] for d in cur.description]
            turns = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()
    return Snapshot(
        db_path=path.resolve(),
        schema_version=version,
        current=current,
        turns=turns,
    )


# ---------------------------------------------------------------------------
# 身份与时间
# ---------------------------------------------------------------------------


def base_key_of(identity_key: str) -> str:
    """基础身份键（platform\x1fself\x1fsender，剥离 scope 段）。

    与 uctx_bridge.ledger.TurnLedger.base_key_of 一致：mode_generation
    按基础身份存储（模式切换作用于该用户全部人格）。
    """

    parts = identity_key.split(SEP)
    if len(parts) != 4:
        return identity_key
    return SEP.join((parts[0], parts[1], parts[3]))


def decode_identity(identity_key: str) -> dict | None:
    """身份键解码（W4 编码）：scope 段带模式前缀。

    返回 scope_mode：persona / user / quarantine（迁移隔离，不注入）/
    legacy_persona（迁移前的裸键，正常仅存在于未迁移旧库）。
    """

    parts = identity_key.split(SEP)
    if len(parts) != 4:
        return None
    platform_id, self_id, scope_token, sender_id = parts
    if scope_token == SCOPE_USER_TOKEN:
        return {
            "platform_id": platform_id,
            "self_id": self_id,
            "persona_scope": "",
            "sender_id": sender_id,
            "scope_mode": "user",
        }
    if scope_token.startswith(SCOPE_PERSONA_PREFIX):
        return {
            "platform_id": platform_id,
            "self_id": self_id,
            "persona_scope": scope_token[len(SCOPE_PERSONA_PREFIX):],
            "sender_id": sender_id,
            "scope_mode": "persona",
        }
    if scope_token.startswith(SCOPE_QUARANTINE_PREFIX):
        return {
            "platform_id": platform_id,
            "self_id": self_id,
            "persona_scope": scope_token[len(SCOPE_QUARANTINE_PREFIX):],
            "sender_id": sender_id,
            "scope_mode": "quarantine",
        }
    return {
        "platform_id": platform_id,
        "self_id": self_id,
        "persona_scope": scope_token,
        "sender_id": sender_id,
        "scope_mode": "legacy_persona",
    }


def parse_bound(text: str | None, *, which: str) -> float | None:
    if text is None:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RecordsError(
            f"--{which} 不是合法 ISO8601 时间：{text!r}"
        ) from exc
    if dt.tzinfo is None:
        # 无偏移按本地时间解释。不用 naive.timestamp()：Windows 对 1970
        # 前的本地时间会抛 OSError[22]，改用当前本地偏移的纯算术换算。
        local_offset = datetime.now().astimezone().utcoffset()
        return (dt - local_offset).replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        # Windows 对 1970 前的本地时间戳可能抛 OSError——回退 UTC 显示
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
            timespec="seconds"
        )


def is_current(row: dict, current: dict[str, dict[str, int]]) -> bool:
    info = current.get(row["identity_key"], {})
    if "epoch" in info and row["epoch"] != info["epoch"]:
        return False
    gen_info = current.get(base_key_of(row["identity_key"]), {})
    if "gen" in gen_info and row["mode_generation"] != gen_info["gen"]:
        return False
    return True


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------


@dataclass
class Filters:
    platform_id: str | None = None
    self_id: str | None = None
    sender_id: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    persona: str | None = None
    t_from: float | None = None
    t_to: float | None = None
    statuses: tuple = DEFAULT_STATUSES
    archives: bool = False
    limit: int = DEFAULT_LIMIT

    def echo(self) -> dict:
        return {
            "platform_id": self.platform_id,
            "self_id": self.self_id,
            "sender_id": self.sender_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_persona": self.persona,
            "time_from": fmt_time(self.t_from) if self.t_from else None,
            "time_to": fmt_time(self.t_to) if self.t_to else None,
            "statuses": list(self.statuses),
            "include_archives": self.archives,
            "limit": self.limit,
        }


def _decode_base(base_key: str) -> dict:
    """3 段基础键 → (platform, self, sender)。"""

    parts = base_key.split(SEP)
    if len(parts) != 3:
        return {}
    return {
        "platform_id": parts[0],
        "self_id": parts[1],
        "sender_id": parts[2],
    }


def enumerate_bases(snap: Snapshot) -> list[dict]:
    """全部基础身份（仅管理元数据，不含正文）：三维字段 + 轮次统计。"""

    bases: dict[str, dict] = {}

    def slot(bk: str) -> dict:
        if bk not in bases:
            bases[bk] = {
                **_decode_base(bk),
                "turns_total": 0,
                "turns_completed": 0,
                "first": None,
                "last": None,
                "scope_modes": set(),
            }
        return bases[bk]

    for row in snap.turns:
        s = slot(base_key_of(row["identity_key"]))
        s["turns_total"] += 1
        if row["status"] == "completed":
            s["turns_completed"] += 1
        s["first"] = (
            row["created_at"] if s["first"] is None else min(s["first"], row["created_at"])
        )
        s["last"] = (
            row["created_at"] if s["last"] is None else max(s["last"], row["created_at"])
        )
        dec = decode_identity(row["identity_key"])
        if dec:
            s["scope_modes"].add(dec["scope_mode"])
    for key in snap.current:
        slot(base_key_of(key))
    out = []
    for bk in sorted(bases):
        s = bases[bk]
        s["base_key"] = bk
        s["scope_modes"] = sorted(s["scope_modes"])
        out.append(s)
    return out


def _candidates_block(bases: list[dict]) -> str:
    lines = []
    for b in bases[:20]:
        lines.append(
            f"  - {b.get('platform_id') or '?'} / bot={b.get('self_id') or '?'}"
            f" / 用户={b.get('sender_id') or '?'}"
            f"（完成 {b.get('turns_completed', 0)}/{b.get('turns_total', 0)} 条）"
        )
    if len(bases) > 20:
        lines.append(f"  ……共 {len(bases)} 个基础身份，仅显示前 20 个")
    return "\n".join(lines)


def resolve_export_base(snap: Snapshot, f: Filters) -> dict:
    """W6：导出前解析**唯一**基础身份；歧义/缺失/无匹配均受控报错。

    - 完全未给身份：列出全部候选（不含正文）并报错，不做默认全库导出；
    - 部分指定：按已给维度过滤，命中多个 → 列出候选要求补全；
    - 唯一命中：允许推断，但必须在输出（stdout/JSON/HTML）中明示。
    """

    all_bases = enumerate_bases(snap)
    given = sum(1 for w in (f.platform_id, f.self_id, f.sender_id) if w)
    if given == 0:
        raise RecordsError(
            "必须先确定导出身份：请用 --platform/--self-id/--sender 指定，"
            "至少给到可唯一解析（推荐三维完整）。本库可用基础身份：\n"
            + _candidates_block(all_bases)
        )
    matches = [
        b
        for b in all_bases
        if (not f.platform_id or b["platform_id"] == f.platform_id)
        and (not f.self_id or b["self_id"] == f.self_id)
        and (not f.sender_id or b["sender_id"] == f.sender_id)
    ]
    if not matches:
        raise RecordsError(
            "未找到匹配的基础身份分区（platform/self/sender 组合在本库无记录）"
        )
    if len(matches) > 1:
        raise RecordsError(
            "身份筛选命中多个基础身份，为避免混合导出多用户/多机器人正文，"
            "请补全到唯一（当前候选：\n" + _candidates_block(matches) + "）"
        )
    base = matches[0]
    return {
        "base_key": base["base_key"],
        "platform_id": base["platform_id"],
        "self_id": base["self_id"],
        "sender_id": base["sender_id"],
        "inferred": given < 3,
    }


def apply_filters(
    snap: Snapshot, f: Filters, base_key: str
) -> tuple[list[dict], bool, int]:
    """返回 (命中的 turns 行, 是否截断, 截断前总命中数)。按时间升序。

    身份维度由 resolve_export_base 唯一确定后传入，这里只做基础身份内
    的来源/人格/时间/状态/归档筛选。
    """

    allowed = set(f.statuses)
    matched: list[dict] = []
    for row in snap.turns:
        if base_key_of(row["identity_key"]) != base_key:
            continue
        if row["status"] not in allowed:
            continue
        if not f.archives and not is_current(row, snap.current):
            continue
        if f.source_type and row["source_type"] != f.source_type:
            continue
        if f.source_id and row["source_id"] != f.source_id:
            continue
        if f.persona and row["source_persona"] != f.persona:
            continue
        if f.t_from is not None and row["created_at"] < f.t_from:
            continue
        if f.t_to is not None and row["created_at"] >= f.t_to:
            continue
        matched.append(row)
    truncated = len(matched) > f.limit
    return matched[: f.limit], truncated, len(matched)


# ---------------------------------------------------------------------------
# JSON 导出
# ---------------------------------------------------------------------------


def _load_json_text(text, expect):
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if expect is not None and not isinstance(value, expect):
        return None
    return value


def turn_to_record(row: dict, snap: Snapshot) -> dict:
    dec = decode_identity(row["identity_key"]) or {
        "platform_id": None,
        "self_id": None,
        "persona_scope": None,
        "sender_id": None,
        "scope_mode": "unknown",
    }
    return {
        "record_id": str(row["id"]),
        "platform_id": dec["platform_id"],
        "self_id": dec["self_id"],
        "persona_scope": dec["persona_scope"],
        "scope_mode": dec["scope_mode"],
        "sender_id": dec["sender_id"],
        "source_persona": row["source_persona"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "umo": row["umo"],
        "epoch": row["epoch"],
        "seq": row["seq"],
        "mode_generation": row["mode_generation"],
        "is_current_generation": is_current(row, snap.current),
        "status": row["status"],
        "send_state": row["send_state"],
        "created_at": fmt_time(row["created_at"]),
        "updated_at": fmt_time(row["updated_at"]),
        "user_message": _load_json_text(row["user_message"], dict),
        "reply_text": row["reply_text"],
        "trajectory": _load_json_text(row["trajectory"], list),
        "event_key": row["event_key"],
    }


def build_export(snap: Snapshot, f: Filters) -> dict:
    resolved = resolve_export_base(snap, f)
    rows, truncated, total = apply_filters(snap, f, resolved["base_key"])
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": fmt_time(datetime.now().timestamp()),
        "tool": "tools/uctx_records.py",
        "ledger_schema_version": snap.schema_version,
        "database": snap.db_path.name,
        "filters": f.echo(),
        "resolved_identity": {
            "platform_id": resolved["platform_id"],
            "self_id": resolved["self_id"],
            "sender_id": resolved["sender_id"],
            "inferred": resolved["inferred"],
            "note": (
                "由部分参数唯一推断；完整三维以本输出为准"
                if resolved["inferred"]
                else "platform/self/sender 三维完整指定"
            ),
        },
        "time_convention": "本地时区 ISO8601（含偏移）；起点包含、终点不包含。",
        "note": "单一基础身份快照导出，不是可追加的增量事件流；页面/文件只反映导出时点。",
        "total_matched": total,
        "returned": len(rows),
        "truncated": truncated,
        "records": [turn_to_record(row, snap) for row in rows],
    }


# ---------------------------------------------------------------------------
# 输出文件安全
# ---------------------------------------------------------------------------


def _normcase(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def guard_output_path(out: Path, db_path: Path) -> Path:
    """输出守卫（W7）：规范化路径后拒绝覆盖源库、WAL/SHM/journal 与
    **源库关联的备份目录**（<db 目录>/backups/，目录级整树拒绝）；
    相对/绝对、Windows 大小写、等价路径（./、..）全部归一后比较。
    """

    out = out.resolve()
    db = db_path.resolve()
    protected = {
        _normcase(db),
        _normcase(Path(str(db) + "-wal")),
        _normcase(Path(str(db) + "-shm")),
        _normcase(Path(str(db) + "-journal")),
    }
    if _normcase(out) in protected:
        raise RecordsError("输出路径不能覆盖源数据库及其 WAL/SHM/journal 文件")
    backups_dir_norm = _normcase(db.parent / "backups")
    out_norm = _normcase(out)
    if out_norm == backups_dir_norm or out_norm.startswith(
        backups_dir_norm + os.sep
    ):
        raise RecordsError(
            "输出路径不能写入源库关联的备份目录 backups/（迁移备份受保护）"
        )
    if os.path.normcase(db.parent.name) == "backups" and out_norm.startswith(
        _normcase(db.parent) + os.sep
    ):
        # 源库本身就在某个 backups 目录里：该目录整体亦视为备份目录
        raise RecordsError("输出路径不能写入备份目录（源库位于备份目录内）")
    if out.is_dir():
        raise RecordsError(f"输出路径已是目录：{out}")
    return out


def atomic_write_text(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(
        prefix=".uctx-export-", suffix=".tmp", dir=str(out.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmpname, out)
    except BaseException:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def resolve_output(args_out: str | None, db_path: Path, suffix: str) -> Path:
    if args_out:
        return Path(args_out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return db_path.parent / "exports" / f"uctx-export-{stamp}{suffix}"


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------

_HTML_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  margin: 0; background: #f5f6f8; color: #24292f; }
header { position: sticky; top: 0; background: #ffffff; border-bottom: 1px solid #d0d7de;
  padding: 12px 20px; z-index: 2; }
header h1 { font-size: 17px; margin: 0 0 6px; }
.meta { font-size: 12px; color: #57606a; line-height: 1.7; }
.searchbar { margin-top: 8px; }
.searchbar input { width: min(560px, 90%); padding: 6px 10px; font-size: 14px;
  border: 1px solid #d0d7de; border-radius: 6px; }
.searchbar .count { margin-left: 10px; font-size: 13px; color: #57606a; }
main { max-width: 960px; margin: 16px auto 48px; padding: 0 16px; }
article.turn { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
  margin-bottom: 14px; padding: 12px 16px; }
.turn-head { font-size: 12.5px; color: #57606a; margin-bottom: 8px;
  display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 12px; border: 1px solid transparent; }
.badge.completed { background: #dafbe1; border-color: #aceebb; color: #116329; }
.badge.failed { background: #ffebe9; border-color: #ff818266; color: #cf222e; }
.badge.aborted, .badge.interrupted { background: #fff8c5; border-color: #d4a72c66; color: #7d4e00; }
.badge.running { background: #ddf4ff; border-color: #54aeff66; color: #0969da; }
.badge.archived { background: #eff1f3; border-color: #d0d7de; color: #57606a; }
.badge.current { background: #e8f0fe; border-color: #a5c8ff; color: #0a3069; }
.msg { white-space: normal; word-break: break-word; font-size: 14px; line-height: 1.75; }
.msg.user { border-left: 3px solid #0969da; padding-left: 10px; margin: 6px 0; }
.msg.assistant { border-left: 3px solid #1a7f37; padding-left: 10px; margin: 6px 0; }
.msg .role { font-size: 12px; color: #57606a; display: block; margin-bottom: 2px; }
details.tools { margin-top: 8px; font-size: 13px; }
details.tools summary { cursor: pointer; color: #57606a; }
details.tools pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
  padding: 8px 10px; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
  font-size: 12px; line-height: 1.6; max-height: 320px; overflow-y: auto; }
.empty { text-align: center; color: #57606a; padding: 48px 0; font-size: 14px; }
.note { font-size: 12px; color: #9a6700; margin-top: 6px; }
"""

_HTML_SCRIPT = """
(function () {
  var input = document.getElementById('q');
  var counter = document.getElementById('count');
  var items = Array.prototype.slice.call(document.querySelectorAll('article.turn'));
  function apply() {
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    items.forEach(function (el) {
      var hay = el.getAttribute('data-text') || '';
      var hit = !q || hay.indexOf(q) !== -1;
      el.style.display = hit ? '' : 'none';
      if (hit) { shown++; }
    });
    counter.textContent = '显示 ' + shown + ' / ' + items.length + ' 条';
  }
  input.addEventListener('input', apply);
  apply();
})();
"""


def _esc(text) -> str:
    return _html.escape(str(text), quote=True)


def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                chunks.append(
                    _MEDIA_PART_NOTE.format(kind=part.get("type", "?"))
                )
            elif part is not None:
                chunks.append(str(part))
        return "\n".join(chunks)
    if content is None:
        return ""
    return str(content)


def _msg_block(role: str, content, role_label: str) -> str:
    body = _esc(_text_of_content(content)).replace("\n", "<br>")
    if not body:
        body = "<em>（空内容）</em>"
    return (
        '<div class="msg ' + _esc(role) + '"><span class="role">'
        + _esc(role_label) + "</span>" + body + "</div>"
    )


def render_record_card(index: int, record: dict) -> str:
    user_msg = record.get("user_message") or {}
    reply = record.get("reply_text")
    trajectory = record.get("trajectory") or []

    searchable = " ".join(
        [
            record.get("source_persona") or "",
            record.get("source_type") or "",
            record.get("source_id") or "",
            record.get("sender_id") or "",
            record.get("status") or "",
            _text_of_content(user_msg.get("content")),
            str(reply or ""),
        ]
    ).lower()

    head_bits = [
        f"<b>#{index}</b>",
        _esc(record.get("created_at", "")),
        f'<span class="badge {_esc(record.get("status"))}">'
        + _esc(record.get("status")) + "</span>",
        "来源：" + _esc(record.get("source_type")) + " "
        + _esc(record.get("source_id") or "-"),
        "人格：" + _esc(record.get("source_persona") or "-"),
        "用户：" + _esc(record.get("sender_id") or "-"),
        "epoch/代次：" + _esc(record.get("epoch")) + "/" + _esc(record.get("mode_generation")),
        (
            '<span class="badge current">当前代次</span>'
            if record.get("is_current_generation")
            else '<span class="badge archived">归档</span>'
        ),
    ]
    if record.get("send_state"):
        head_bits.append("发送：" + _esc(record.get("send_state")))

    parts = [
        '<article class="turn" data-text="' + _esc(searchable) + '">',
        '<div class="turn-head">' + "".join(f"<span>{b}</span>" for b in head_bits) + "</div>",
    ]
    parts.append(
        _msg_block("user", user_msg.get("content"), "用户 " + str(record.get("created_at", "")))
    )
    if reply:
        parts.append(_msg_block("assistant", reply, "助手回复"))
    elif record.get("status") == "completed":
        parts.append('<div class="msg assistant"><em>（completed 但无回复文本）</em></div>')
    else:
        parts.append(
            '<div class="msg assistant"><em>（该轮未完成，无助手回复）</em></div>'
        )
    if trajectory:
        pretty = json.dumps(trajectory, ensure_ascii=False, indent=1)
        parts.append(
            '<details class="tools"><summary>工具调用 / 轨迹（'
            + str(len(trajectory)) + ' 条消息，折叠）</summary><pre>'
            + _esc(pretty) + "</pre></details>"
        )
    parts.append("</article>")
    return "".join(parts)


def render_html(export: dict) -> str:
    records = export["records"]
    f = export["filters"]
    rid = export.get("resolved_identity") or {}
    cond_bits = []
    if rid:
        cond_bits.append(
            "身份=" + _esc(
                f"{rid.get('platform_id') or '?'}/bot={rid.get('self_id') or '?'}"
                f"/用户={rid.get('sender_id') or '?'}"
                + ("（唯一推断）" if rid.get("inferred") else "（三维明确）")
            )
        )
    for label, key in [
        ("来源类型", "source_type"), ("来源 ID", "source_id"),
        ("人格", "source_persona"), ("起", "time_from"), ("止", "time_to"),
    ]:
        if f.get(key):
            cond_bits.append(f"{label}={_esc(f[key])}")
    cond_bits.append("状态=" + _esc(",".join(f["statuses"])))
    if f.get("include_archives"):
        cond_bits.append("含归档")
    cond_bits.append("上限=" + _esc(f["limit"]))

    title = "uctx 聊天记录浏览（导出快照）"
    head = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>" + _esc(title) + "</title>\n<style>" + _HTML_STYLE + "</style>\n"
        "</head>\n<body>\n<header>\n<h1>" + _esc(title) + "</h1>\n"
        '<div class="meta">查询条件：' + _esc("；".join(cond_bits))
        + "<br>数据库：" + _esc(export["database"])
        + "　账本 schema：" + _esc(export["ledger_schema_version"])
        + "　导出时间：" + _esc(export["exported_at"])
        + "　命中：" + _esc(export["total_matched"])
        + "　返回：" + _esc(export["returned"]) + " 条"
        + ("<br><span class=\"note\">结果已按上限截断，仅显示前 "
           + _esc(export["returned"]) + " 条；如需更多请缩小筛选范围或调大 limit。</span>"
           if export["truncated"] else "")
        + "<br>本页面是导出时点的静态快照，不会实时刷新；新增记录需重新导出。</div>\n"
        '<div class="searchbar"><input id="q" type="search" '
        'placeholder="在已导出结果内搜索（人格 / 用户 / 正文 / 状态…）" '
        'autofocus><span class="count" id="count"></span></div>\n</header>\n<main>\n'
    )
    if not records:
        body = '<p class="empty">无匹配记录：当前筛选条件下没有可展示的轮次。</p>'
    else:
        body = "".join(
            render_record_card(i + 1, rec) for i, rec in enumerate(records)
        )
    tail = "</main>\n<script>" + _HTML_SCRIPT + "</script>\n</body>\n</html>\n"
    return head + body + tail


# ---------------------------------------------------------------------------
# list 子命令
# ---------------------------------------------------------------------------


def list_identities(snap: Snapshot) -> list[dict]:
    stats: dict[str, dict] = {}
    order: list[str] = []
    for row in snap.turns:
        key = row["identity_key"]
        if key not in stats:
            stats[key] = {
                "total": 0,
                "completed": 0,
                "other": 0,
                "first": row["created_at"],
                "last": row["created_at"],
                "personas": set(),
            }
            order.append(key)
        s = stats[key]
        s["total"] += 1
        if row["status"] == "completed":
            s["completed"] += 1
        else:
            s["other"] += 1
        s["first"] = min(s["first"], row["created_at"])
        s["last"] = max(s["last"], row["created_at"])
        if row.get("source_persona"):
            s["personas"].add(row["source_persona"])
    # 无任何轮次但已有 meta 的身份也列出（如清空后）；base 键（3 段，
    # 仅承载 mode_generation）不是身份，跳过
    for key in snap.current:
        if key not in stats and decode_identity(key) is not None:
            stats[key] = {
                "total": 0, "completed": 0, "other": 0,
                "first": None, "last": None, "personas": set(),
            }
            order.append(key)
    out = []
    for key in order:
        dec = decode_identity(key) or {}
        info = snap.current.get(key, {})
        gen_info = snap.current.get(base_key_of(key), {})
        s = stats[key]
        out.append({
            "identity_key": key,
            "platform_id": dec.get("platform_id"),
            "self_id": dec.get("self_id"),
            "scope_mode": dec.get("scope_mode"),
            "persona_scope": dec.get("persona_scope"),
            "sender_id": dec.get("sender_id"),
            "current_epoch": info.get("epoch"),
            "current_mode_generation": gen_info.get("gen", 0),
            "turns_total": s["total"],
            "turns_completed": s["completed"],
            "turns_other": s["other"],
            "personas_seen": sorted(s["personas"]),
            "first_turn_at": fmt_time(s["first"]) if s["first"] else None,
            "last_turn_at": fmt_time(s["last"]) if s["last"] else None,
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_statuses(text: str | None) -> tuple:
    if not text:
        return DEFAULT_STATUSES
    items = [s.strip() for s in text.split(",") if s.strip()]
    if not items:
        return DEFAULT_STATUSES
    bad = [s for s in items if s not in VALID_STATUSES]
    if bad:
        raise RecordsError(
            "非法状态值：" + ",".join(bad)
            + "（合法值：" + ",".join(VALID_STATUSES) + "）"
        )
    return tuple(items)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uctx_records.py",
        description="astrbot_plugin_user_context_bridge 本地只读记录工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True, help="uctx_ledger.db 路径")

    p_list = sub.add_parser("list", help="列出身份分区（不输出聊天正文）")
    common(p_list)
    p_list.add_argument("--json", action="store_true", help="以 JSON 输出")

    for name, suffix, help_text in [
        ("export-json", ".json", "导出 JSON"),
        ("export-html", ".html", "导出可离线浏览的 HTML"),
    ]:
        p_exp = sub.add_parser(name, help=help_text)
        common(p_exp)
        p_exp.add_argument("--platform", help="平台实例 ID（如 aiocqhttp）")
        p_exp.add_argument("--self-id", help="机器人 ID")
        p_exp.add_argument("--sender", help="QQ 用户号")
        p_exp.add_argument("--source-type", choices=["group", "private"],
                           help="来源类型：群聊/私聊")
        p_exp.add_argument("--source-id", help="来源窗口 ID（群号或私聊会话）")
        p_exp.add_argument("--persona", help="按源人格筛选")
        p_exp.add_argument("--from", dest="t_from", help="起始时间（ISO8601，含）")
        p_exp.add_argument("--to", dest="t_to", help="结束时间（ISO8601，不含）")
        p_exp.add_argument("--status", help="逗号分隔状态，默认 completed；"
                                            "全部=completed,failed,aborted,interrupted,running")
        p_exp.add_argument("--all-statuses", action="store_true",
                           help="等价于 --status 全部五种")
        p_exp.add_argument("--archives", action="store_true",
                           help="包含归档（非当前 epoch/代次）轮次")
        p_exp.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                           help=f"返回上限，默认 {DEFAULT_LIMIT}")
        p_exp.add_argument("--out", help="输出文件路径（默认 <库目录>/exports/）")

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    try:
        snap = load_snapshot(args.db)
        if args.command == "list":
            rows = list_identities(snap)
            if getattr(args, "json", False):
                print(json.dumps({
                    "database": snap.db_path.name,
                    "ledger_schema_version": snap.schema_version,
                    "identities": rows,
                }, ensure_ascii=False, indent=2))
            else:
                print(
                    f"数据库：{snap.db_path.name}　账本 schema：v{snap.schema_version}"
                    f"　身份分区：{len(rows)} 个"
                )
                for i, r in enumerate(rows, 1):
                    sm = r["scope_mode"]
                    if sm == "user":
                        mode = "user(跨人格)"
                    elif sm == "quarantine":
                        mode = "quarantine(隔离,不注入)"
                    elif sm == "legacy_persona":
                        mode = "legacy(未迁移裸键)"
                    else:
                        mode = "persona"
                    print(
                        f"[{i}] {r['platform_id']} / bot={r['self_id']}"
                        f" / 模式={mode}"
                        + (f"(scope={r['persona_scope']})" if sm in ("persona", "legacy_persona", "quarantine") else "")
                        + f" / 用户={r['sender_id']}"
                        + f" / epoch={r['current_epoch']}"
                        + f" 代次={r['current_mode_generation']}"
                        + f" / 轮次 完成{r['turns_completed']}"
                        + f" 其他{r['turns_other']}"
                        + (f" / 人格 {','.join(r['personas_seen'])}"
                           if r["personas_seen"] else "")
                        + (f" / 末次 {r['last_turn_at']}" if r["last_turn_at"] else "")
                    )
                print("说明：以上仅管理元数据，不含聊天正文。")
            return 0

        statuses = _parse_statuses(getattr(args, "status", None))
        if getattr(args, "all_statuses", False):
            statuses = tuple(VALID_STATUSES)
        filters = Filters(
            platform_id=args.platform,
            self_id=args.self_id,
            sender_id=args.sender,
            source_type=args.source_type,
            source_id=args.source_id,
            persona=args.persona,
            t_from=parse_bound(args.t_from, which="from"),
            t_to=parse_bound(args.t_to, which="to"),
            statuses=statuses,
            archives=args.archives,
            limit=max(1, args.limit),
        )
        if args.command == "export-json":
            export = build_export(snap, filters)
            out = guard_output_path(
                resolve_output(args.out, snap.db_path, ".json"), snap.db_path
            )
            atomic_write_text(
                out, json.dumps(export, ensure_ascii=False, indent=1) + "\n"
            )
            print(
                f"已导出 JSON：{out}（命中 {export['total_matched']}"
                f" 条，返回 {export['returned']} 条，"
                f"截断={export['truncated']}）"
            )
            return 0

        export = build_export(snap, filters)
        out = guard_output_path(
            resolve_output(args.out, snap.db_path, ".html"), snap.db_path
        )
        atomic_write_text(out, render_html(export))
        print(
            f"已导出 HTML：{out}（命中 {export['total_matched']}"
            f" 条，返回 {export['returned']} 条，"
            f"截断={export['truncated']}）；用浏览器直接打开即可离线浏览。"
        )
        return 0
    except RecordsError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：输出/读取失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
