"""N3 回归（MIS-168；W1-W8 返工后 v2）：本地只读查询、HTML/JSON 导出。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/n3_records_check.py

W 返工语义：身份键 v3 编码（p:/u:/q:）；export 必须先解析唯一基础身份
（W6：缺失/歧义报候选错误且不写文件；唯一推断在输出中明示）；输出守卫
保护源库关联的 backups/ 目录（W7）。
真实浏览器渲染与截图在 local_evidence 单独留证（N18 视觉部分）。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from uctx_bridge.ledger import TurnLedger

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "uctx_records.py"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def identity_of(sender, scope, self_id="bot_001", platform="aiocqhttp"):
    """v3 编码：scope=u: 为 user 模式；否则 p:<scope>。"""

    token = "u:" if scope == "user" else f"p:{scope}"
    return f"{platform}\x1f{self_id}\x1f{token}\x1f{sender}"


def cli(*args):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )


INJECT = "<script>alert('xss')</script> <img src=x onerror=alert(2)>"
T1 = 1758240000.0
T2 = T1 + 60.0
T1_ISO = datetime.fromtimestamp(T1).astimezone().isoformat()
T2_ISO = datetime.fromtimestamp(T2).astimezone().isoformat()


def begin(led, key, ek, persona, msg):
    return led.begin_turn(
        identity_key=key, event_key=ek, source_type="group",
        source_id="700000001", umo="aiocqhttp:GroupMessage:700000001",
        user_message={"role": "user", "content": msg},
        source_persona=persona,
    )


def build_fixture(db: Path) -> None:
    led = TurnLedger(db)
    led.open()
    k_black = identity_of("10001", "black")
    k_white = identity_of("10001", "white")
    k_user = identity_of("10002", "user")
    k_b2 = identity_of("10001", "black", self_id="bot_002")

    begin(led, k_black, "ev-b1", "black", "你好 🌏 " + INJECT + " " + "长文本" * 40)
    led.commit_turn(
        event_key="ev-b1", status="completed",
        trajectory=[
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "type": "function",
                             "function": {"name": "calc", "arguments": "1+1"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "42"},
        ],
        reply_text="答案是 42 ✅",
    )
    led.mark_turn_sent("ev-b1")

    begin(led, k_black, "ev-b2", "black", "第二问 😄")
    led.commit_turn(event_key="ev-b2", status="completed", reply_text="第二答")

    begin(led, k_black, "ev-bf", "black", "会失败吗")
    led.commit_turn(event_key="ev-bf", status="failed", reply_text=None)

    begin(led, k_black, "ev-br", "black", "还在跑")  # 保持 running

    begin(led, k_white, "ev-w1", "white", "white 人格问")
    led.commit_turn(event_key="ev-w1", status="completed", reply_text="white 答")

    begin(led, k_b2, "ev-x1", "black", "bot2 问")
    led.commit_turn(event_key="ev-x1", status="completed", reply_text="bot2 答")

    begin(led, k_user, "ev-u1", "black", "user 模式黑问")
    led.commit_turn(event_key="ev-u1", status="completed", reply_text="user 黑答")
    begin(led, k_user, "ev-u2", "white", "user 模式白问")
    led.commit_turn(event_key="ev-u2", status="completed", reply_text="user 白答")

    # reset（bump_epoch）后旧 epoch 归档；再 bump_mode_generation 后 gen0 全归档
    led.bump_epoch(k_black)
    begin(led, k_black, "ev-b3", "black", "reset 后新问")
    led.commit_turn(event_key="ev-b3", status="completed", reply_text="新答")
    led.bump_mode_generation(k_black)
    begin(led, k_black, "ev-b4", "black", "切模式后新问")
    led.commit_turn(event_key="ev-b4", status="completed", reply_text="模式答")

    led.close()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE turns SET created_at=?, updated_at=? WHERE event_key='ev-b1'",
            (T1, T1),
        )
        conn.execute(
            "UPDATE turns SET created_at=?, updated_at=? WHERE event_key='ev-b2'",
            (T2, T2),
        )


def make_legacy_v1(td: Path) -> Path:
    """复制 n01 的方式构造 0.6.0（schema v1）库。"""

    db = Path(td) / "legacy.db"
    led = TurnLedger(db)
    led.open()
    with sqlite3.connect(led._db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_version")
        conn.execute("ALTER TABLE turns DROP COLUMN source_persona")
        conn.execute("ALTER TABLE turns DROP COLUMN mode_generation")
    led.close()
    return db


def by_event(export: dict) -> dict:
    return {r["event_key"]: r for r in export["records"]}


def keys(export: dict) -> set:
    return {r["event_key"] for r in export["records"]}


# ---------------------------------------------------------------------------
def n14_list() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)

        r = cli("list", "--db", str(db))
        check("N14.list-exit0", r.returncode == 0, f"rc={r.returncode} err={r.stderr}")
        # 基础身份（3 段）：bot_001/10001、bot_001/10002、bot_002/10001 = 3 个
        check("N14.list-bases", "身份分区：4 个" in r.stdout, r.stdout[:300])
        check("N14.list-user-mode", "模式=user(跨人格)" in r.stdout)
        check("N14.list-persona-mode", "scope=black" in r.stdout)
        check("N14.list-no-body", "长文本" not in r.stdout and INJECT not in r.stdout)

        r = cli("list", "--db", str(db), "--json")
        data = json.loads(r.stdout)
        check("N14.list-json-count", len(data["identities"]) == 4)
        black = next(i for i in data["identities"]
                     if i["persona_scope"] == "black" and i["self_id"] == "bot_001")
        check("N14.list-black-meta",
              black["current_epoch"] == 2 and black["current_mode_generation"] == 1
              and black["turns_completed"] == 4 and black["turns_other"] == 2
              and black["scope_mode"] == "persona"
              and black["personas_seen"] == ["black"],
              json.dumps(black, ensure_ascii=False))
        user = next(i for i in data["identities"] if i["scope_mode"] == "user")
        check("N14.list-user-meta",
              user["sender_id"] == "10002" and user["turns_completed"] == 2
              and sorted(user["personas_seen"]) == ["black", "white"]
              and user["current_epoch"] == 1
              and user["current_mode_generation"] == 0,
              json.dumps(user, ensure_ascii=False))


def n19_json_envelope() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)
        out = Path(td) / "out.json"

        # W6：无身份参数 → rc=2 候选列表（不含正文），不写文件
        r = cli("export-json", "--db", str(db), "--out", str(out))
        check("N19.no-identity-rejected",
              r.returncode == 2 and "基础身份" in r.stderr
              and "bot_002" in r.stderr and not out.exists(),
              f"rc={r.returncode} err={r.stderr[:200]}")

        # 三维完整：base bot_001/10001
        r = cli("export-json", "--db", str(db), "--platform", "aiocqhttp",
                "--self-id", "bot_001", "--sender", "10001", "--out", str(out))
        check("N19.export-exit0", r.returncode == 0, f"err={r.stderr}")
        data = json.loads(out.read_text(encoding="utf-8"))
        check("N19.envelope-fields",
              data["schema_version"] == 1
              and data["ledger_schema_version"] == 3
              and data["database"] == "uctx_ledger.db"
              and "exported_at" in data and "filters" in data
              and data["time_convention"] and data["note"],
              json.dumps({k: data.get(k) for k in
                          ("schema_version", "ledger_schema_version", "database")},
                         ensure_ascii=False))
        rid = data.get("resolved_identity") or {}
        check("N19.resolved-explicit",
              rid.get("platform_id") == "aiocqhttp"
              and rid.get("self_id") == "bot_001"
              and rid.get("sender_id") == "10001"
              and rid.get("inferred") is False,
              json.dumps(rid, ensure_ascii=False))
        # 代次按基础身份生效：bump_mode_generation 后该 base 的全部 gen0
        # （black+white）记录归档，仅 gen1 的 ev-b4 为当前
        check("N19.default-current-completed-only",
              keys(data) == {"ev-b4"}, str(keys(data)))
        check("N19.not-truncated",
              data["truncated"] is False and data["returned"] == 1
              and data["total_matched"] == 1)

        u1 = by_event(data).get("ev-u1")
        check("N19.archived-excluded-default", u1 is None)

        # 唯一推断：platform+sender → bot_001/10002（库中该组合唯一）
        out_u = Path(td) / "user.json"
        r = cli("export-json", "--db", str(db), "--platform", "aiocqhttp",
                "--sender", "10002", "--out", str(out_u))
        data_u = json.loads(out_u.read_text(encoding="utf-8"))
        check("N19.unique-inference",
              r.returncode == 0
              and data_u["resolved_identity"]["inferred"] is True
              and data_u["resolved_identity"]["self_id"] == "bot_001",
              r.stderr[:200])
        u1 = by_event(data_u)["ev-u1"]
        check("N19.record-user-mode",
              u1["scope_mode"] == "user" and u1["persona_scope"] == ""
              and u1["source_persona"] == "black" and u1["sender_id"] == "10002"
              and u1["is_current_generation"] is True
              and u1["status"] == "completed",
              json.dumps(u1, ensure_ascii=False)[:300])
        check("N19.record-messages",
              isinstance(u1["user_message"], dict)
              and u1["user_message"]["content"] == "user 模式黑问"
              and u1["reply_text"] == "user 黑答")
        check("N19.record-time-iso",
              "T" in u1["created_at"] and ("+" in u1["created_at"][10:]
                                          or "Z" in u1["created_at"]),
              u1["created_at"])
        check("N19.record-gen",
              u1["epoch"] == 1 and u1["mode_generation"] == 0 and u1["seq"] >= 1)

        # 归档导出：工具配对、send_state、转义原文在 JSON 中保持
        out2 = Path(td) / "arch.json"
        r = cli("export-json", "--db", str(db), "--archives", "--all-statuses",
                "--platform", "aiocqhttp", "--self-id", "bot_001",
                "--sender", "10001", "--out", str(out2))
        data2 = json.loads(out2.read_text(encoding="utf-8"))
        b1 = by_event(data2)["ev-b1"]
        check("N19.archives-included",
              {"ev-b1", "ev-b2", "ev-b3", "ev-b4", "ev-w1", "ev-bf", "ev-br"}
              == set(by_event(data2)), str(keys(data2)))
        check("N19.record-tool-pairing",
              isinstance(b1["trajectory"], list) and len(b1["trajectory"]) == 2
              and b1["trajectory"][0].get("tool_calls")
              and b1["trajectory"][1].get("tool_call_id") == "t1"
              and b1["trajectory"][1]["content"] == "42",
              json.dumps(b1.get("trajectory"), ensure_ascii=False)[:300])
        check("N19.record-send-state",
              bool(b1["send_state"])
              and not by_event(data2)["ev-b2"]["send_state"],
              f"b1={b1['send_state']} b2={by_event(data2)['ev-b2']['send_state']}")
        check("N19.record-archived-flag",
              b1["is_current_generation"] is False
              and by_event(data2)["ev-b4"]["is_current_generation"] is True)
        check("N19.json-keeps-raw-inject", INJECT in out2.read_text(encoding="utf-8"))

        # 截断（base 10001 全量 7 条，上限 2）
        out3 = Path(td) / "trunc.json"
        cli("export-json", "--db", str(db), "--archives", "--all-statuses",
            "--platform", "aiocqhttp", "--self-id", "bot_001", "--sender", "10001",
            "--limit", "2", "--out", str(out3))
        data3 = json.loads(out3.read_text(encoding="utf-8"))
        check("N19.truncation",
              data3["truncated"] is True and data3["returned"] == 2
              and data3["total_matched"] == 7,
              json.dumps({k: data3[k] for k in
                          ("truncated", "returned", "total_matched")}))


def n15_filters() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)
        ID = ["--platform", "aiocqhttp", "--self-id", "bot_001", "--sender", "10001"]

        def export(*extra):
            out = Path(td) / "f.json"
            r = cli("export-json", "--db", str(db), "--out", str(out), *ID, *extra)
            if r.returncode != 0:
                return r, None
            return r, json.loads(out.read_text(encoding="utf-8"))

        _, data = export()
        check("N15.base-filter", keys(data) == {"ev-b4"}, str(keys(data)))
        _, data = export("--archives", "--persona", "white")
        check("N15.persona-filter", keys(data) == {"ev-w1"}, str(keys(data)))

        r, data = export("--source-type", "private")
        check("N15.empty-result-ok",
              r.returncode == 0 and data["records"] == []
              and data["total_matched"] == 0, r.stderr)
        r, data_nm = export("--archives", "--status", "completed",
                            "--to", "1970-01-01T00:00:00")
        check("N15.filter-no-match-ok",
              r.returncode == 0 and data_nm is not None
              and data_nm["records"] == [] and data_nm["total_matched"] == 0,
              f"rc={r.returncode} err={r.stderr[:200]}")
        r = cli("export-json", "--db", str(db), "--platform", "aiocqhttp",
                "--self-id", "bot_001", "--sender", "99999",
                "--out", str(Path(td) / "bad.json"))
        check("N15.unknown-identity-error",
              r.returncode == 2 and "基础身份" in r.stderr, r.stderr)
        # 歧义：仅 sender=10001 命中 bot_001 与 bot_002 两个基础身份
        r = cli("export-json", "--db", str(db), "--sender", "10001",
                "--out", str(Path(td) / "amb.json"))
        check("N15.ambiguous-rejected",
              r.returncode == 2 and "多个基础身份" in r.stderr
              and "bot_002" in r.stderr
              and not (Path(td) / "amb.json").exists(), r.stderr[:300])

        # 时间边界：起点包含、终点不包含（固定 ev-b1=T1、ev-b2=T1+60）
        _, data = export("--archives", "--status", "completed", "--from", T1_ISO)
        check("N15.time-from-inclusive",
              {"ev-b1", "ev-b2"}.issubset(keys(data)), str(keys(data)))
        _, data = export("--archives", "--status", "completed", "--to", T2_ISO)
        check("N15.time-to-exclusive",
              keys(data) == {"ev-b1"}, str(keys(data)))
        _, data = export("--archives", "--status", "completed", "--from", T2_ISO)
        check("N15.time-from-exclusive-of-b1",
              "ev-b1" not in keys(data) and "ev-b2" in keys(data),
              str(keys(data)))

        # 状态筛选
        _, data = export("--archives", "--status", "failed")
        check("N15.status-failed", keys(data) == {"ev-bf"}, str(keys(data)))
        _, data = export("--archives", "--status", "running")
        check("N15.status-running", keys(data) == {"ev-br"}, str(keys(data)))
        r = cli("export-json", "--db", str(db), *ID, "--status", "bogus",
                "--out", str(Path(td) / "bad2.json"))
        check("N15.status-invalid-rejected",
              r.returncode == 2 and "非法状态" in r.stderr, r.stderr)


def n18_html() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)
        out = Path(td) / "out.html"
        ID = ["--platform", "aiocqhttp", "--self-id", "bot_001", "--sender", "10001"]

        r = cli("export-html", "--db", str(db), "--archives",
                "--status", "completed", *ID, "--out", str(out))
        check("N18.export-exit0", r.returncode == 0, f"err={r.stderr}")
        html = out.read_text(encoding="utf-8")
        check("N18.charset", 'charset="utf-8"' in html)
        check("N18.search-input", '<input id="q"' in html
              and "getElementById('q')" in html)
        check("N18.record-count",
              html.count('<article class="turn"') == 5, str(
                  html.count('<article class="turn"')))
        check("N18.identity-line", "身份=aiocqhttp/bot=bot_001/用户=10001" in html)
        check("N18.injection-escaped",
              "<script>alert" not in html and "&lt;script&gt;" in html)
        check("N18.no-external",
              "http://" not in html and "https://" not in html)
        check("N18.emoji-readable", "🌏" in html and "✅" in html)
        check("N18.meta-header",
              "导出时间" in html and "静态快照" in html and "命中：" in html)
        check("N18.snapshot-note", "重新导出" in html)

        out_e = Path(td) / "empty.html"
        r = cli("export-html", "--db", str(db), "--source-type", "private",
                *ID, "--out", str(out_e))
        check("N18.empty-state",
              r.returncode == 0 and "无匹配记录" in out_e.read_text(encoding="utf-8"),
              r.stderr)


def n20_guards() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)
        ID = ["--platform", "aiocqhttp", "--self-id", "bot_001", "--sender", "10001"]

        r = cli("export-json", "--db", str(db), *ID, "--out", str(db))
        check("N20.refuse-db-overwrite",
              r.returncode == 2 and "不能覆盖" in r.stderr, r.stderr)
        r = cli("export-html", "--db", str(db), *ID, "--out", str(db) + "-wal")
        check("N20.refuse-wal-overwrite",
              r.returncode == 2 and "不能覆盖" in r.stderr, r.stderr)

        # W7：源库关联 backups/ 目录内的既有备份必须受保护（含大小写/等价路径）
        backups = Path(td) / "backups"
        backups.mkdir(exist_ok=True)
        legacy_backup = backups / "pre-migrate-v3-sample.db"
        legacy_backup.write_bytes(b"SQLITE-BACKUP-BYTES")
        original = legacy_backup.read_bytes()
        r = cli("export-json", "--db", str(db), *ID, "--out", str(legacy_backup))
        check("N20.refuse-backup-overwrite",
              r.returncode == 2 and "备份" in r.stderr
              and legacy_backup.read_bytes() == original, r.stderr[:200])
        r = cli("export-json", "--db", str(db), *ID,
                "--out", str(Path(td) / "BACKUPS" / "pre-migrate-v3-sample.db"))
        check("N20.refuse-backup-case-insensitive",
              r.returncode == 2 and legacy_backup.read_bytes() == original,
              r.stderr[:200])
        r = cli("export-json", "--db", str(db), *ID,
                "--out", str(backups / "sub" / ".." / "pre-migrate-v3-sample.db"))
        check("N20.refuse-backup-equivalent-path",
              r.returncode == 2 and legacy_backup.read_bytes() == original,
              r.stderr[:200])
        # backups 目录本身作为输出目标也拒绝
        r = cli("export-json", "--db", str(db), *ID, "--out", str(backups))
        check("N20.refuse-backups-dir-itself",
              r.returncode == 2, r.stderr[:200])
        # 合法 exports 输出不受影响
        ok_out = Path(td) / "exports" / "ok.json"
        r = cli("export-json", "--db", str(db), *ID, "--out", str(ok_out))
        check("N20.exports-still-allowed", r.returncode == 0 and ok_out.exists(),
              r.stderr[:200])

        block = Path(td) / "blockfile"
        block.write_text("x", encoding="utf-8")
        r = cli("export-json", "--db", str(db), *ID,
                "--out", str(block / "x.json"))
        check("N20.bad-parent-no-half-file",
              r.returncode == 2 and not (block / "x.json").exists(), r.stderr)

        legacy = make_legacy_v1(Path(td) / "v1dir")
        r = cli("export-json", "--db", str(legacy), *ID,
                "--out", str(Path(td) / "v1.json"))
        check("N20.v1-db-refused",
              r.returncode == 2 and "迁移" in r.stderr, r.stderr)
        r = cli("list", "--db", str(legacy))
        check("N20.v1-list-refused",
              r.returncode == 2 and "迁移" in r.stderr, r.stderr)


def main():
    n14_list()
    n19_json_envelope()
    n15_filters()
    n18_html()
    n20_guards()
    print(f"\n=== N3 回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
