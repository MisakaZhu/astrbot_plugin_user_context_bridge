"""P4 跨窗口并发、异常取消与清空竞态验证（MIS-140）。

覆盖 A08（同身份跨窗口并发顺序确定、不同身份不互相阻塞）、
A10（模型错误/用户中止终态、发送状态可识别、无处理锁泄漏）、
A11（reset 与慢请求竞态：新 epoch 干净、旧请求不复活）、
A12（bridge 崩溃后重启恢复）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/p4_concurrency_check.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType

from tests.fakes import FakeConversation, FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import (
    FakePersonaManager,
    cleanup,
    register_bridge,
)
from uctx_bridge.bridge import ContextBridge
from uctx_bridge.identity import build_identity
from uctx_bridge.ledger import TurnLedger
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

PASS: list[str] = []
FAIL: list[str] = []


def q(ledger: TurnLedger, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(ledger._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


async def drive_turn(
    event: FakeEvent,
    prompt: str,
    provider: FakeProvider,
    *,
    stop_before_run: bool = False,
    send_ok: bool = True,
) -> None:
    """驱动一轮（等价 internal.py 顺序）；stop_before_run 模拟清空竞态登记后慢执行。"""

    req = ProviderRequest()
    req.prompt = prompt
    req.contexts = []
    req.system_prompt = "sys"
    req.conversation = FakeConversation(user_id=event.unified_msg_origin)
    await call_event_hook(event, EventType.OnLLMRequestEvent, req)
    runner = ToolLoopAgentRunner()
    reset_coro = runner.reset(
        provider=provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=MAIN_AGENT_HOOKS,
        streaming=False,
    )
    await reset_coro
    if stop_before_run:
        event._force_stopped = True
        runner.request_stop()
    async for _ in run_agent(runner, 30, True, False, False):
        pass
    await call_event_hook(event, EventType.OnDecoratingResultEvent)
    if send_ok:
        await call_event_hook(event, EventType.OnAfterMessageSentEvent)


def make_bridge(td: str, **scope_kw) -> tuple[ContextBridge, TurnLedger, list]:
    ledger = TurnLedger(Path(td) / "l.db")
    ledger.open()
    resolver = ScopeResolver(
        ScopeConfig(
            enabled=True,
            shared_groups=frozenset({"700000001", "700000002"}),
            include_private=True,
            **scope_kw,
        ),
        MembershipStore(Path(td) / "membership.json"),
    )
    bridge = ContextBridge(
        ledger=ledger,
        scope_resolver=resolver,
        persona_manager_getter=lambda: FakePersonaManager(),
        logger=None,
    )
    metas = register_bridge(bridge)
    return bridge, ledger, metas


def completed_turns(ledger: TurnLedger, identity_key: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(ledger._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM turns WHERE identity_key=? AND status='completed' ORDER BY seq",
            (identity_key,),
        ).fetchall()
    finally:
        conn.close()


def identity_of(sender: str) -> str:
    return build_identity(
        platform_id="aiocqhttp",
        self_id="bot_001",
        persona_scope="maid",
        sender_id=sender,
    ).key


async def scenario_concurrent_windows() -> None:
    """A08：同一身份三窗口并发 + 不同身份并行不阻塞。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, ledger, metas = make_bridge(td)
        try:
            class SlowProvider(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append(
                        {"contexts": [m.model_dump() for m in kwargs.get("contexts") or []]}
                    )
                    await asyncio.sleep(0.2)
                    return await super().text_chat(**kwargs)

            provider = SlowProvider()

            async def turn(win_sender: str, group: str, tag: str):
                ev = FakeEvent(sender_id=win_sender, group_id=group, message_str=f"并发{tag}")
                await drive_turn(ev, f"并发{tag}", provider)

            # 同身份（10001）三窗口并发 + 异身份（20002）一个窗口并发
            t0 = time.perf_counter()
            await asyncio.gather(
                turn("10001", "700000001", "A"),
                turn("10001", "700000002", "B"),
                turn("10001", "", "P"),
                turn("20002", "700000001", "X"),
            )
            elapsed = time.perf_counter() - t0

            id1 = identity_of("10001")
            rows1 = completed_turns(ledger, id1)
            seqs = [r["seq"] for r in rows1]
            check(
                "A08.same-identity-all-committed",
                len(rows1) == 3 and len(set(seqs)) == 3,
                f"rows={len(rows1)} seqs={seqs}",
            )
            prompts = [json.loads(r["user_message"])["content"][0]["text"] for r in rows1]
            check(
                "A08.no-overwrite",
                sorted(prompts) == ["并发A", "并发B", "并发P"],
                f"prompts={prompts}",
            )
            check(
                "A08.cross-identity-parallel",
                elapsed < 0.55,
                f"elapsed={elapsed:.2f}s（串行将 >0.8s：身份锁不跨模型执行）",
            )
            check(
                "A08.other-identity-committed",
                len(completed_turns(ledger, identity_of("20002"))) == 1,
                "",
            )
            check(
                "A08.no-pending-leak",
                bridge.pending_count == 0,
                f"pending={bridge.pending_count}",
            )
        finally:
            cleanup(metas)
            ledger.close()


async def scenario_error_and_abort() -> None:
    """A10：模型错误→failed；中止→aborted；发送状态可识别。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, ledger, metas = make_bridge(td)
        try:
            # 模型错误
            p_err = FakeProvider()
            p_err.error_script = [RuntimeError("服务不可用")]
            ev_err = FakeEvent(sender_id="10001", group_id="700000001", message_str="会失败")
            await drive_turn(ev_err, "会失败", p_err)
            conn = sqlite3.connect(ledger._db_path)
            rows = conn.execute(
                "SELECT status, send_state, reply_text FROM turns"
            ).fetchall()
            conn.close()
            check(
                "A10.model-error-failed",
                rows and rows[0][0] == "failed" and (rows[0][2] or "") == "",
                f"rows={rows}",
            )
            check(
                "A10.error-no-fake-success",
                all(r[0] != "completed" for r in rows),
                "",
            )
            check(
                "A10.error-releases-turn",
                bridge.pending_count == 0,
                f"pending={bridge.pending_count}",
            )

            # 用户中止
            ev_abort = FakeEvent(sender_id="10001", group_id="700000001", message_str="会中止")
            await drive_turn(ev_abort, "会中止", FakeProvider(), stop_before_run=True)
            statuses = [r["status"] for r in q(ledger, "SELECT status FROM turns")]
            check(
                "A10.abort-marked",
                "aborted" in statuses,
                f"statuses={statuses}",
            )

            # 成功轮次：发送成功标记 vs 发送失败（钩子未触发）可识别
            ev_ok = FakeEvent(sender_id="10001", group_id="700000001", message_str="正常轮")
            await drive_turn(ev_ok, "正常轮", FakeProvider(), send_ok=True)
            ev_fail_send = FakeEvent(sender_id="10001", group_id="700000001", message_str="发送失败轮")
            await drive_turn(ev_fail_send, "发送失败轮", FakeProvider(), send_ok=False)
            comp_rows = q(
                ledger,
                "SELECT event_key, send_state FROM turns WHERE status='completed' ORDER BY id",
            )
            check(
                "A10.send-state-distinguishable",
                len(comp_rows) == 2
                and comp_rows[0]["send_state"] == "sent"
                and comp_rows[1]["send_state"] is None,
                f"rows={[(r['event_key'], r['send_state']) for r in comp_rows]}",
            )
        finally:
            cleanup(metas)
            ledger.close()


async def scenario_reset_race() -> None:
    """A11（bridge 级）：慢请求执行中 reset——新轮次干净、旧请求完成后不复活。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, ledger, metas = make_bridge(td)
        try:
            id1 = identity_of("10001")
            # 第一轮完成（旧历史）
            ev1 = FakeEvent(sender_id="10001", group_id="700000001", message_str="旧问题")
            await drive_turn(ev1, "旧问题", FakeProvider(), send_ok=False)
            check("A11.pre-reset-one-turn", len(completed_turns(ledger, id1)) == 1)

            # 慢请求登记（进入 running）
            ev_slow = FakeEvent(sender_id="10001", group_id="700000002", message_str="慢请求")
            req = ProviderRequest()
            req.prompt = "慢请求"
            req.contexts = []
            req.system_prompt = "sys"
            req.conversation = FakeConversation(user_id=ev_slow.unified_msg_origin)
            await call_event_hook(ev_slow, EventType.OnLLMRequestEvent, req)
            check("A11.slow-registered", bridge.pending_count == 1)

            # reset（epoch 清空）
            ledger.bump_epoch(id1)
            check("A11.new-epoch-clean", ledger.load_history(id1) == [])

            # 新轮次在新 epoch 干净开始
            ev_new = FakeEvent(sender_id="10001", group_id="700000001", message_str="新问题")
            await drive_turn(ev_new, "新问题", FakeProvider(), send_ok=False)
            new_history = ledger.load_history(id1)
            new_texts = json.dumps(new_history, ensure_ascii=False)
            check(
                "A11.new-turn-clean-context",
                "旧问题" not in new_texts and "新问题" in new_texts,
                f"history={new_texts}",
            )

            # 慢请求完成（旧 epoch）→ decorating 提交被 EpochStale 吸收，不复活
            runner = ToolLoopAgentRunner()
            reset_coro = runner.reset(
                provider=FakeProvider(reply_script=["迟到的回答"]),
                request=req,
                run_context=ContextWrapper(context=SimpleNamespace(event=ev_slow)),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=MAIN_AGENT_HOOKS,
                streaming=False,
            )
            await reset_coro
            async for _ in run_agent(runner, 30, True, False, False):
                pass
            await call_event_hook(ev_slow, EventType.OnDecoratingResultEvent)
            final_history = ledger.load_history(id1)
            final_texts = json.dumps(final_history, ensure_ascii=False)
            check(
                "A11.slow-request-not-resurrected",
                "迟到的回答" not in final_texts and "慢请求" not in final_texts,
                f"history={final_texts}",
            )
            check(
                "A11.slow-turn-absorbed",
                bridge.stats.epoch_stale_rejected == 1 and bridge.pending_count == 0,
                f"stale={bridge.stats.epoch_stale_rejected}",
            )
            # reset 仅影响本人
            check(
                "A11.reset-scope-self",
                len(completed_turns(ledger, identity_of("30003"))) == 0,
                "",
            )
        finally:
            cleanup(metas)
            ledger.close()


async def scenario_crash_recovery() -> None:
    """A12（bridge 级）：登记后未提交（崩溃）→ 重启恢复 interrupted，不重复回复。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = str(Path(td) / "l.db")
        bridge, ledger, metas = make_bridge(td)
        try:
            ev = FakeEvent(sender_id="10001", group_id="700000001", message_str="崩溃轮")
            req = ProviderRequest()
            req.prompt = "崩溃轮"
            req.contexts = []
            req.system_prompt = "sys"
            req.conversation = FakeConversation(user_id=ev.unified_msg_origin)
            await call_event_hook(ev, EventType.OnLLMRequestEvent, req)
            check("A12.pending-before-crash", bridge.pending_count == 1)
        finally:
            # 模拟崩溃：不提交、不优雅关闭
            cleanup(metas)

        # “重启”：新 ledger + recover
        ledger2 = TurnLedger(db)
        ledger2.open()
        recovered = ledger2.recover_running()
        check("A12.recovered-on-restart", recovered == 1, f"n={recovered}")
        id1 = identity_of("10001")
        texts = json.dumps(ledger2.load_history(id1), ensure_ascii=False)
        check(
            "A12.crash-not-in-history",
            "崩溃轮" not in texts,
            f"history={texts}",
        )
        # 重复恢复幂等
        check("A12.recovery-idempotent", ledger2.recover_running() == 0)
        ledger2.close()


async def main() -> int:
    await scenario_concurrent_windows()
    await scenario_error_and_abort()
    await scenario_reset_race()
    await scenario_crash_recovery()
    print(f"\n=== P4 并发与竞态验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
