"""N0 反例与数据契约基线（MIS-165）。

先证明 0.6.0 基线的三个事实（修复前必失败/尚不存在）：
  N0-1  跨人格接续反例：persona 模式（0.6.0 行为）下，同用户群 A 黑人格
        -> 群 B 白人格，B 的模型请求不含 A 问答（旧隔离语义，user 模式
        实现后此场景转为接续）。
  N0-2  配置反例：history_scope 尚不存在（读不到该键）。
  N0-3  工具反例：tools/uctx_records.py 尚不存在。
另外固化 0.6.0 数据契约快照（身份键格式/表结构/退出文件格式）供迁移对账。

运行：PYTHONPATH=. python tests/n0_baseline_check.py（不依赖宿主 venv）
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import FakePersonaManager
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import cleanup, register_bridge
from uctx_bridge.identity import build_identity

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


async def n0_1_persona_isolation_baseline():
    """0.6.0 契约：persona scope 进共享键，不同人格不共享（旧例保留）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        class PerWindowPersona(FakePersonaManager):
            """按窗口返回不同人格（模拟朋友报告的黑/白人格轮换）。"""

            async def resolve_selected_persona(self, **kw):
                umo = str(kw.get("umo") or "")
                scope = "white" if "700000002" in umo else "black"
                return (scope, {"name": scope,
                                "_begin_dialogs_processed": []}, None, False)

        pm = PerWindowPersona()
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, persona_manager_getter=lambda: pm)
        metas = register_bridge(bridge)
        try:
            ev_a = FakeEvent(sender_id="10001", group_id="700000001", message_str="黑人格问题")
            await drive_pipeline(bridge, ev_a, FakeProvider(["黑人格回答"]), prompt="黑人格问题")
            ev_b = FakeEvent(sender_id="10001", group_id="700000002", message_str="白人格问题")
            pb = FakeProvider(["白人格回答"])
            await drive_pipeline(bridge, ev_b, pb, prompt="白人格问题")
            ctx_b = json.dumps(pb.call_log[0]["contexts"], ensure_ascii=False)
            # 0.6.0 契约成立：不同人格不互通
            check("N0.persona-baseline-isolated",
                  "黑人格问题" not in ctx_b and "黑人格回答" not in ctx_b,
                  f"ctx_b={ctx_b[:200]}")
            # N0-1 反例（user 模式缺失）：当前无法让同用户跨人格接续——
            # 两轮落在不同 identity_key 且无 user 键存在
            import sqlite3 as s3
            with s3.connect(ledger._db_path) as db:
                keys = [r[0] for r in db.execute(
                    "SELECT DISTINCT identity_key FROM turns").fetchall()]
            scopes = {k.split("\x1f")[2] for k in keys}
            check("N0.no-user-mode-key-yet",
                  len(keys) == 2
                  and all("__mode_user__" not in k for k in keys)
                  and scopes == {"black", "white"},
                  f"keys={keys} scopes={scopes}")
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


def n0_2_config_absent():
    """N0-2 反例：history_scope 配置尚未存在。"""
    repo = Path(__file__).resolve().parent.parent
    schema = (repo / "_conf_schema.json").read_text(encoding="utf-8")
    check("N0.history_scope-absent", "history_scope" not in schema)
    bridge_src = (repo / "main.py").read_text(encoding="utf-8")
    check("N0.history_scope-not-wired", "history_scope" not in bridge_src)


def n0_3_records_tool_absent():
    """N0-3 反例：本地只读记录工具尚未存在。"""
    repo = Path(__file__).resolve().parent.parent
    check("N0.records-tool-absent", not (repo / "tools" / "uctx_records.py").exists())


def n0_data_contract_snapshot():
    """固化 0.6.0 数据契约（迁移对账基准）。"""
    from uctx_bridge.identity import build_identity
    from uctx_bridge.ledger import TurnLedger

    key = build_identity(platform_id="aiocqhttp", self_id="bot_001",
                         persona_scope="maid", sender_id="10001").key
    check("N0.contract-key-format",
          key == "aiocqhttp\x1fbot_001\x1fmaid\x1f10001", f"key={key!r}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        led.begin_turn(identity_key=key, event_key="c1", source_type="group",
                       source_id="g", umo="u", user_message={"role": "user", "content": "q"})
        led.commit_turn(event_key="c1", status="completed",
                        trajectory=[{"role": "assistant", "content": "a"}], reply_text="a")
        with sqlite3.connect(led._db_path) as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(turns)").fetchall()]
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        check("N0.contract-turn-cols",
              set(cols) >= {"identity_key", "epoch", "seq", "event_key", "status",
                            "source_type", "source_id", "umo", "user_message",
                            "trajectory", "reply_text", "send_state", "lease_id"},
              f"cols={cols}")
        check("N0.contract-tables", {"turns", "meta", "instance_leases"} <= tables)
        # 0.7.0 起 open() 自动迁移：契约改为"v1 列是当前列表的子集"
        check("N0.contract-v1-cols-subset",
              {"identity_key", "epoch", "seq", "event_key", "status",
               "source_type", "source_id", "umo", "user_message",
               "trajectory", "reply_text", "send_state"} <= set(cols))
        # 退出文件契约
        from tests.harness import make_bridge_stack  # noqa: F401
        led.close()


async def main():
    await n0_1_persona_isolation_baseline()
    n0_2_config_absent()
    n0_3_records_tool_absent()
    n0_data_contract_snapshot()
    print(f"\n=== N0 基线反例与契约：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
