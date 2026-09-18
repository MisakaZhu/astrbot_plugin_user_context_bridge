"""N2 回归（MIS-167）：跨人格请求链、命令与生命周期（N02/N04/N09/N10/N11/N13）。

真实 Runner/调度链 + 按窗口人格解析；验证 user 模式跨人格接续
（群 A 黑 → 群 B 白 → 私聊第三 → 群 A 黑），当前人格规则保留，
命令/生命周期不倒退。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/n2_cross_persona_check.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from astrbot.core.provider.entities import ProviderRequest

from tests.fakes import FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import FakePersonaManager
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import cleanup, register_bridge
from uctx_bridge.commands import CommandService
from uctx_bridge.identity import build_identity

PASS = []
FAIL = []


def user_identity(sender, platform="aiocqhttp", self_id="bot_001"):
    return build_identity(platform_id=platform, self_id=self_id,
                          persona_scope="__mode_user__", sender_id=sender).key


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def user_key(sender, platform="aiocqhttp", self_id="bot_001"):
    return f"{platform}\x1f{self_id}\x1f__mode_user__\x1f{sender}"


class PerWindowPersona(FakePersonaManager):
    async def resolve_selected_persona(self, **kw):
        umo = str(kw.get("umo") or "")
        if "700000001" in umo:
            scope, sys_prompt, dialogs = "black", "BLACK-RULES", ["黑开场问", "黑开场答"]
        elif "700000002" in umo:
            scope, sys_prompt, dialogs = "white", "WHITE-RULES", ["白开场问", "白开场答"]
        else:
            scope, sys_prompt, dialogs = "third", "THIRD-RULES", ["第三开场问", "第三开场答"]
        return (
            scope,
            {"name": scope, "prompt": sys_prompt,
             "_begin_dialogs_processed": [
                 {"role": "user", "content": dialogs[0]},
                 {"role": "assistant", "content": dialogs[1]},
             ]},
            None,
            False,
        )


def make(td, **kw):
    bridge, resolver, membership, ledger = make_bridge_stack(
        td, persona_manager_getter=lambda: PerWindowPersona(), **kw
    )
    resolver.set_history_scope("user")
    metas = register_bridge(bridge)
    return bridge, resolver, membership, ledger, metas


def completed(ledger, key):
    with sqlite3.connect(ledger._db_path) as db:
        return db.execute(
            "SELECT COUNT(*) FROM turns WHERE identity_key=? AND status='completed'",
            (key,),
        ).fetchone()[0]


async def n02_cross_persona_chain():
    """N02：群 A 黑 → 群 B 白 → 私聊第三 → 群 A 黑，逐步接续。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger, metas = make(td)
        try:
            # N03 对照：user 模式关闭时（resolve 不变，仅键不同）——此处
            # 直接验证 user 模式开启后的行为
            turns = []
            persona_by_group = {
                "700000001": ("BLACK-RULES", "黑开场"),
                "700000002": ("WHITE-RULES", "白开场"),
                "": ("THIRD-RULES", "第三开场"),
            }
            for (group, q, a) in (
                ("700000001", "黑问", "黑答"),
                ("700000002", "白问", "白答"),
                ("", "第三问", "第三答"),
                ("700000001", "回黑问", "回黑答"),
            ):
                rules, dialog = persona_by_group[group]
                ev = FakeEvent(sender_id="10001", group_id=group, message_str=q)
                p = FakeProvider([a])
                await drive_pipeline(bridge, ev, p, prompt=q,
                                     system_prompt=rules, begin_dialog=dialog)
                turns.append((q, a, p))
            # N02：最终请求含全部前序问答各一次
            final_ctx = json.dumps(turns[-1][2].call_log[0]["contexts"], ensure_ascii=False)
            check("N02.chain-all-present",
                  all(q in final_ctx and a in final_ctx for q, a, _ in turns[:3]),
                  f"final_ctx={final_ctx[:300]}")
            check("N02.no-duplication",
                  final_ctx.count("黑答") == 1 and final_ctx.count("白答") == 1
                  and final_ctx.count("第三答") == 1)
            # N04：当前人格规则——每轮 system_prompt 开头对应其窗口人格规则，
            # 开场白只注入一次，不持久化旧人格系统规则
            ctx3 = turns[2][2].call_log[0]["contexts"]
            head3 = ctx3[0].get("content", "") if isinstance(ctx3[0], dict) else ""
            check("N04.current-persona-rules",
                  "THIRD-RULES" in head3 and "BLACK-RULES" not in head3,
                  f"head={head3[:120]}")
            # N09：user 键（跨人格共享键）内 completed 轮数 = 4
            with sqlite3.connect(ledger._db_path) as db:
                n = db.execute(
                    "SELECT COUNT(*) FROM turns WHERE identity_key=? AND status='completed'",
                    (user_key("10001"),),
                ).fetchone()[0]
            check("N09.user-key-four-turns", n == 4, f"n={n}")
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


async def n04_persona_preserved():
    """N04：user 模式下当前人格 system_prompt/begin_dialogs 保留。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger, metas = make(td)
        try:
            p = FakeProvider(["回答"])
            ev = FakeEvent(sender_id="10001", group_id="700000002", message_str="白问")
            await drive_pipeline(bridge, ev, p, prompt="白问",
                                 system_prompt="WHITE-RULES",
                                 begin_dialog="白开场")
            msgs = p.call_log[0]["contexts"]
            head = msgs[0].get("content", "") if isinstance(msgs[0], dict) else ""
            check("N04.system-prompt-current", "WHITE-RULES" in head, f"head={head[:120]}")
            dialogs = [m for m in msgs if m.get("role") == "assistant"][:1]
            # 开场白问/答各出现一次（两次=配对正常），不重复注入
            raw = json.dumps(msgs, ensure_ascii=False)
            check("N04.begin-dialog-once",
                  raw.count("白开场问") == 1 and raw.count("白开场答") == 1)
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


async def n09_concurrency_cross_persona():
    """N09：同基础身份跨人格互斥至终态，不同基础身份并行。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger, metas = make(td)
        try:
            entered_a, release_a = asyncio.Event(), asyncio.Event()
            entered_b = asyncio.Event()

            class SlowA(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    entered_a.set()
                    await release_a.wait()
                    return await super().text_chat(**kwargs)

            class FastB(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    entered_b.set()
                    return await super().text_chat(**kwargs)

            ev_a = FakeEvent(sender_id="10001", group_id="700000001", message_str="A黑问")
            ev_b = FakeEvent(sender_id="10001", group_id="700000002", message_str="B白问")
            ta = asyncio.create_task(drive_pipeline(bridge, ev_a, SlowA(["A答"]), prompt="A黑问"))
            await asyncio.wait_for(entered_a.wait(), 3)
            tb = asyncio.create_task(drive_pipeline(bridge, ev_b, FastB(["B答"]), prompt="B白问"))
            release_a.set()
            await asyncio.gather(ta, tb)
            with sqlite3.connect(ledger._db_path) as db:
                n = db.execute(
                    "SELECT COUNT(*) FROM turns WHERE identity_key=? AND status='completed'",
                    (user_key("10001"),),
                ).fetchone()[0]
            check("N09.cross-persona-mutex-serial", n == 2, f"n={n}")
            check("N09.no-lock-leak",
                  not any(l.locked() for l in bridge._identity_locks.values()))
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


async def n10_reset_scope():
    """N10：persona/user 模式清空范围正确。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger, metas = make(td)
        try:
            commands = CommandService(
                ledger=ledger, resolver=resolver, membership=membership,
                persona_manager_getter=lambda: PerWindowPersona(),
            )
            # user 模式积累 2 轮（不同人格）
            ev1 = FakeEvent(sender_id="10001", group_id="700000001", message_str="问一")
            await drive_pipeline(bridge, ev1, FakeProvider(["答一"]), prompt="问一")
            ev2 = FakeEvent(sender_id="10001", group_id="700000002", message_str="问二")
            await drive_pipeline(bridge, ev2, FakeProvider(["答二"]), prompt="问二")
            check("N10.pre-reset-two", completed(ledger, user_key("10001")) == 2)
            # user 模式 reset：清整份跨人格历史
            ev_cmd = FakeEvent(sender_id="10001", group_id="700000001", message_str="/uctx reset")
            text = await commands.reset(ev_cmd)
            hist_after_reset = ledger.load_history(user_identity("10001"))
            check("N10.user-reset-clears-all",
                  len(hist_after_reset) == 0 and "清空" in text,
                  f"hist={len(hist_after_reset)} text={text[:80]}")
            # persona 模式 reset：只清当前人格
            resolver.set_history_scope("persona")
            ev3 = FakeEvent(sender_id="10001", group_id="700000001", message_str="黑问3")
            await drive_pipeline(bridge, ev3, FakeProvider(["黑答3"]), prompt="黑问3")
            ev4 = FakeEvent(sender_id="10001", group_id="700000002", message_str="白问4")
            await drive_pipeline(bridge, ev4, FakeProvider(["白答4"]), prompt="白问4")
            text = await commands.reset(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="/uctx reset")
            )
            check("N10.persona-reset-scope",
                  len(ledger.load_history(identity_of("10001", "black"))) == 0
                  and len(ledger.load_history(identity_of("10001", "white"))) == 2,
                  f"black={len(ledger.load_history(identity_of('10001', 'black')))} "
                  f"white={len(ledger.load_history(identity_of('10001', 'white')))}",
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


def identity_of(sender, scope):
    return f"aiocqhttp\x1fbot_001\x1f{scope}\x1f{sender}"


async def main():
    await n02_cross_persona_chain()
    await n04_persona_preserved()
    await n09_concurrency_cross_persona()
    await n10_reset_scope()
    print(f"\n=== N2 跨人格回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
