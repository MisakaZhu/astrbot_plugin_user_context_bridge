"""N3 回归（MIS-168）：本地只读查询、HTML 浏览与 JSON 导出。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/n3_records_check.py

覆盖：list 元数据、JSON 信封与记录字段、筛选（身份/人格/来源/时间边界/状态/
归档/截断）、HTML 转义与无外部依赖、输出路径守卫、v1 旧库拒绝。
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
    return f"{platform}\x1f{self_id}\x1f{scope}\x1f{sender}"


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
    k_user = identity_of("10002", "__mode_user__")

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
        check("N14.list-three-ids", "身份分区：3 个" in r.stdout, r.stdout[:200])
        check("N14.list-user-mode", "模式=user(跨人格)" in r.stdout)
        check("N14.list-persona-mode", "scope=black" in r.stdout)
        check("N14.list-no-body", "长文本" not in r.stdout and INJECT not in r.stdout)

        r = cli("list", "--db", str(db), "--json")
        data = json.loads(r.stdout)
        check("N14.list-json-count", len(data["identities"]) == 3)
        black = next(i for i in data["identities"] if i["persona_scope"] == "black")
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

        r = cli("export-json", "--db", str(db), "--out", str(out))
        check("N19.export-exit0", r.returncode == 0, f"err={r.stderr}")
        data = json.loads(out.read_text(encoding="utf-8"))
        check("N19.envelope-fields",
              data["schema_version"] == 1
              and data["ledger_schema_version"] == 2
              and data["database"] == "uctx_ledger.db"
              and "exported_at" in data and "filters" in data
              and data["time_convention"] and data["note"],
              json.dumps({k: data.get(k) for k in
                          ("schema_version", "ledger_schema_version", "database")},
                         ensure_ascii=False))
        # 代次按基础身份生效：bump_mode_generation 后 black 与 white（同一
        # 基础身份 platform+self+sender）的全部 gen0 记录都归档，仅 gen1 的
        # ev-b4 与不受影响的基础身份 10002（u1/u2）为当前
        check("N19.default-current-completed-only",
              keys(data) == {"ev-b4", "ev-u1", "ev-u2"},
              str(keys(data)))
        check("N19.not-truncated",
              data["truncated"] is False and data["returned"] == 3
              and data["total_matched"] == 3)

        u1 = by_event(data)["ev-u1"]
        check("N19.record-user-mode",
              u1["scope_mode"] == "user" and u1["persona_scope"] == "__mode_user__"
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

        b1 = by_event(data).get("ev-b1")
        check("N19.archived-excluded-default", b1 is None)

        # 归档导出：工具配对、send_state、转义原文在 JSON 中保持
        out2 = Path(td) / "arch.json"
        r = cli("export-json", "--db", str(db), "--archives", "--all-statuses",
                "--sender", "10001", "--out", str(out2))
        data2 = json.loads(out2.read_text(encoding="utf-8"))
        b1 = by_event(data2)["ev-b1"]
        check("N19.archives-included",
              {"ev-b1", "ev-b2", "ev-b3", "ev-b4", "ev-w1", "ev-bf", "ev-br"}
              .issubset(set(by_event(data2))), str(keys(data2)))
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

        # 截断
        out3 = Path(td) / "trunc.json"
        cli("export-json", "--db", str(db), "--limit", "1", "--out", str(out3))
        data3 = json.loads(out3.read_text(encoding="utf-8"))
        check("N19.truncation",
              data3["truncated"] is True and data3["returned"] == 1
              and data3["total_matched"] == 3,
              json.dumps({k: data3[k] for k in
                          ("truncated", "returned", "total_matched")}))



def n15_filters() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)

        def export(*extra):
            out = Path(td) / "f.json"
            r = cli("export-json", "--db", str(db), "--out", str(out), *extra)
            if r.returncode != 0:
                return r, None
            return r, json.loads(out.read_text(encoding="utf-8"))

        _, data = export("--sender", "10001")
        check("N15.sender-filter",
              keys(data) == {"ev-b4"}, str(keys(data)))
        _, data = export("--sender", "10001", "--archives", "--persona", "white")
        check("N15.persona-filter", keys(data) == {"ev-w1"}, str(keys(data)))
        _, data = export("--sender", "10001", "--platform", "aiocqhttp",
                         "--self-id", "bot_001")
        check("N15.platform-self-filter",
              keys(data) == {"ev-b4"}, str(keys(data)))

        r, data = export("--sender", "10001", "--source-type", "private")
        check("N15.empty-result-ok",
              r.returncode == 0 and data["records"] == []
              and data["total_matched"] == 0, r.stderr)
        r, _ = export("--sender", "99999")
        check("N15.unknown-identity-error",
              r.returncode == 2 and "身份分区" in r.stderr, r.stderr)

        # 时间边界：起点包含、终点不包含（固定 ev-b1=T1、ev-b2=T1+60）
        _, data = export("--sender", "10001", "--archives",
                         "--status", "completed", "--from", T1_ISO)
        check("N15.time-from-inclusive",
              {"ev-b1", "ev-b2"}.issubset(keys(data)), str(keys(data)))
        _, data = export("--sender", "10001", "--archives",
                         "--status", "completed", "--to", T2_ISO)
        check("N15.time-to-exclusive",
              keys(data) == {"ev-b1"}, str(keys(data)))
        _, data = export("--sender", "10001", "--archives",
                         "--status", "completed", "--from", T2_ISO)
        check("N15.time-from-exclusive-of-b1",
              "ev-b1" not in keys(data) and "ev-b2" in keys(data),
              str(keys(data)))

        # 状态筛选
        _, data = export("--sender", "10001", "--archives", "--status", "failed")
        check("N15.status-failed", keys(data) == {"ev-bf"}, str(keys(data)))
        _, data = export("--sender", "10001", "--archives", "--status", "running")
        check("N15.status-running", keys(data) == {"ev-br"}, str(keys(data)))
        r = cli("export-json", "--db", str(db), "--status", "bogus",
                "--out", str(Path(td) / "bad.json"))
        check("N15.status-invalid-rejected",
              r.returncode == 2 and "非法状态" in r.stderr, r.stderr)


def n18_html() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)
        out = Path(td) / "out.html"

        r = cli("export-html", "--db", str(db), "--sender", "10001",
                "--archives", "--status", "completed", "--out", str(out))
        check("N18.export-exit0", r.returncode == 0, f"err={r.stderr}")
        html = out.read_text(encoding="utf-8")
        check("N18.charset", 'charset="utf-8"' in html)
        check("N18.search-input", '<input id="q"' in html
              and "getElementById('q')" in html)
        check("N18.record-count",
              html.count('<article class="turn"') == 5, str(
                  html.count('<article class="turn"')))
        check("N18.injection-escaped",
              "<script>alert" not in html and "&lt;script&gt;" in html)
        check("N18.no-external",
              "http://" not in html and "https://" not in html)
        check("N18.emoji-readable", "🌏" in html and "✅" in html)
        check("N18.meta-header",
              "导出时间" in html and "静态快照" in html and "命中：" in html)
        check("N18.snapshot-note", "重新导出" in html)

        out_t = Path(td) / "trunc.html"
        cli("export-html", "--db", str(db), "--limit", "1", "--out", str(out_t))
        check("N18.truncated-note", "截断" in out_t.read_text(encoding="utf-8"))

        out_e = Path(td) / "empty.html"
        r = cli("export-html", "--db", str(db), "--sender", "10001",
                "--source-type", "private", "--out", str(out_e))
        check("N18.empty-state",
              r.returncode == 0 and "无匹配记录" in out_e.read_text(encoding="utf-8"),
              r.stderr)


def n20_guards() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "uctx_ledger.db"
        build_fixture(db)

        r = cli("export-json", "--db", str(db), "--out", str(db))
        check("N20.refuse-db-overwrite",
              r.returncode == 2 and "不能覆盖" in r.stderr, r.stderr)
        r = cli("export-html", "--db", str(db), "--out", str(db) + "-wal")
        check("N20.refuse-wal-overwrite",
              r.returncode == 2 and "不能覆盖" in r.stderr, r.stderr)

        block = Path(td) / "blockfile"
        block.write_text("x", encoding="utf-8")
        r = cli("export-json", "--db", str(db), "--out", str(block / "x.json"))
        check("N20.bad-parent-no-half-file",
              r.returncode == 2 and not (block / "x.json").exists(), r.stderr)

        legacy = make_legacy_v1(Path(td) / "v1dir")
        r = cli("export-json", "--db", str(legacy),
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
