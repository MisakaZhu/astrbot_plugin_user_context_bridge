"""第三轮返工回归（T1~T6）：把三次验收独立反例转为仓库内自动断言。

T2/T4 进程内（真实调度链 harness）；T1/T5/T6 经真实宿主组件子进程 worker
（真实 PersonaManager/ConversationManager/Context/WakingCheckStage/
StarRequestSubStage/builtin commands，插件经真实 import 装配）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/t_rework_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.fakes import FakeEvent, FakeProvider
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from tests.r_rework_check import identity_of

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


# ---------------------------------------------------------------------------
# T2：锁后取消窗口（等待人格解析时取消）
# ---------------------------------------------------------------------------
async def t2_cancel_window() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=0.1
        )
        entered = asyncio.Event()

        class WaitingPersona(FakePersonaManager):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def resolve_selected_persona(self, **kw):
                self.calls += 1
                if self.calls == 2:  # 身份解析（第 1 次）后的开场白解析
                    entered.set()
                    await asyncio.Event().wait()
                return await super().resolve_selected_persona(**kw)

        waiting_persona = WaitingPersona()
        bridge._get_persona_manager = lambda: waiting_persona
        metas = register_bridge(bridge)
        try:
            a = FakeEvent(sender_id="10001", group_id="700000001", message_str="A")
            pa = FakeProvider(["A-REPLY"])
            ta = asyncio.create_task(drive_pipeline(bridge, a, pa, prompt="A"))
            await asyncio.wait_for(entered.wait(), 3)
            ta.cancel()
            t2_outcomes = await asyncio.gather(ta, return_exceptions=True)
            # U4：非预期异常（如夹具 UnboundLocalError）必须暴露为失败
            check(
                "T2.cancel-task-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and a.is_stopped())
                    for o in t2_outcomes
                ),
                f"outcomes={[type(o).__name__ for o in t2_outcomes]}",
            )
            await asyncio.sleep(0.15)

            check("T2.cancel-no-model", len(pa.call_log) == 0,
                  f"calls={len(pa.call_log)}")
            check("T2.cancel-no-output", not a.sent_chains)
            check("T2.cancel-stops-event", a.is_stopped(),
                  "取消必须终止事件传播（宿主吞取消后不再执行模型）")
            with sqlite3.connect(ledger._db_path) as db:
                states = db.execute("SELECT status FROM turns").fetchall()
            check(
                "T2.cancel-turn-finalized",
                states and states[0][0] == "interrupted",
                f"states={states}",
            )
            lock = bridge._identity_locks.get(identity_of("10001"))
            check(
                "T2.lock-released",
                lock is None or not lock.locked(),
                "取消后身份锁必须释放",
            )
            check("T2.no-pending", bridge.pending_count == 0)

            # 后继跨窗请求真实完成
            b = FakeEvent(sender_id="10001", group_id="700000002", message_str="B")
            pb = FakeProvider(["B-REPLY"])
            try:
                await asyncio.wait_for(
                    drive_pipeline(bridge, b, pb, prompt="B"), 3
                )
                done = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                done = False
            check("T2.next-request-completes", done, "B 3s 内完成")

            # 登记前取消（初始人格解析等待，未取得锁/轮次——U4 收紧断言）
            c = FakeEvent(sender_id="30003", group_id="700000001", message_str="C")
            pc3 = FakeProvider(["C-MUST-NOT-RUN"])
            resolve_entered = asyncio.Event()

            class SlowPersona(FakePersonaManager):
                async def resolve_selected_persona(self, **kw):
                    resolve_entered.set()
                    await asyncio.Event().wait()

            slow_persona = SlowPersona()
            bridge._get_persona_manager = lambda: slow_persona
            tc = asyncio.create_task(drive_pipeline(bridge, c, pc3, prompt="C"))
            await asyncio.wait_for(resolve_entered.wait(), 3)
            tc.cancel()
            t2_pre_outcomes = await asyncio.gather(tc, return_exceptions=True)
            await asyncio.sleep(0.05)
            check(
                "T2.pre-registration-cancel-clean",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and c.is_stopped())
                    for o in t2_pre_outcomes
                )
                and c.is_stopped()
                and len(pc3.call_log) == 0
                and not c.sent_chains
                and not any(l.locked() for l in bridge._identity_locks.values()),
                f"outcomes={[type(o).__name__ for o in t2_pre_outcomes]} "
                f"stopped={c.is_stopped()} calls={len(pc3.call_log)}",
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# U1：所有等待点的取消语义（初始人格解析 / 等待身份锁）
# ---------------------------------------------------------------------------
async def u1_cancellation_windows() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=5
        )
        entered = asyncio.Event()

        class WaitingPersona(FakePersonaManager):
            async def resolve_selected_persona(self, **kw):
                entered.set()
                await asyncio.Event().wait()

        wp = WaitingPersona()
        bridge._get_persona_manager = lambda: wp
        metas = register_bridge(bridge)
        try:
            ev = FakeEvent(
                sender_id="10001", group_id="700000001",
                message_str="cancel-before-lock",
            )
            provider = FakeProvider(["REPLIED-AFTER-CANCEL"])
            task = asyncio.create_task(
                drive_pipeline(bridge, ev, provider, prompt=ev.message_str)
            )
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            outcomes = await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0.05)
            check(
                "U1.initial-resolve-cancel-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and ev.is_stopped())
                    for o in outcomes
                ),
                "取消被宿主钩子吞掉后必须走受控 stopped 分支"
                f"（outcomes={[type(o).__name__ for o in outcomes]}）",
            )
            check("U1.initial-resolve-stops-event", ev.is_stopped())
            check(
                "U1.initial-resolve-no-model",
                len(provider.call_log) == 0,
                f"calls={len(provider.call_log)}",
            )
            check("U1.initial-resolve-no-output", not ev.sent_chains)
            with sqlite3.connect(ledger._db_path) as db:
                rows0 = db.execute("SELECT status FROM turns").fetchall()
            check("U1.initial-resolve-no-turn", not rows0, f"rows={rows0}")
            check(
                "U1.initial-resolve-no-lock-leak",
                not any(l.locked() for l in bridge._identity_locks.values()),
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=5
        )
        entered, release = asyncio.Event(), asyncio.Event()

        class Hanging(FakeProvider):
            async def text_chat(self, **kwargs):
                self.call_log.append({"contexts": []})
                entered.set()
                await release.wait()
                return await super().text_chat(**kwargs)

        metas = register_bridge(bridge)
        try:
            a = FakeEvent(sender_id="10001", group_id="700000001", message_str="A")
            b = FakeEvent(
                sender_id="10001", group_id="700000002",
                message_str="B-cancel-queued",
            )
            pa = Hanging(["A-ANSWER"])
            pb = FakeProvider(["B-REPLIED-AFTER-CANCEL"])
            ta = asyncio.create_task(drive_pipeline(bridge, a, pa, prompt="A"))
            await asyncio.wait_for(entered.wait(), 3)
            tb = asyncio.create_task(
                drive_pipeline(bridge, b, pb, prompt=b.message_str)
            )
            for _ in range(200):
                if any(
                    len(lock._waiters or [])
                    for lock in bridge._identity_locks.values()
                ):
                    break
                await asyncio.sleep(0.01)
            waiters = sum(
                len(lock._waiters or [])
                for lock in bridge._identity_locks.values()
            )
            check("U1.queued-waiter-present", waiters == 1, f"waiters={waiters}")
            tb.cancel()
            outcomes = await asyncio.gather(tb, return_exceptions=True)
            check(
                "U1.queued-cancel-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and ev.is_stopped())
                    for o in outcomes
                ),
                "取消被宿主钩子吞掉后必须走受控 stopped 分支"
                f"（outcomes={[type(o).__name__ for o in outcomes]}）",
            )
            check("U1.queued-cancel-stops-event", b.is_stopped())
            check(
                "U1.queued-cancel-no-model",
                len(pb.call_log) == 0,
                f"calls={len(pb.call_log)}",
            )
            check("U1.queued-cancel-no-output", not b.sent_chains)
            check(
                "U1.a-unaffected-by-b-cancel",
                not ta.done(),
                "A 不得因 B 取消受影响",
            )
            release.set()
            await asyncio.wait_for(ta, 5)
            with sqlite3.connect(ledger._db_path) as db:
                rows = db.execute("SELECT status FROM turns").fetchall()
            check(
                "U1.only-a-turn",
                len(rows) == 1 and rows[0][0] == "completed",
                f"rows={rows}",
            )
            check(
                "U1.lock-released-after-a",
                not any(l.locked() for l in bridge._identity_locks.values()),
            )
            c = FakeEvent(sender_id="10001", group_id="700000002", message_str="C-next")
            pc = FakeProvider(["C-ANSWER"])
            try:
                await asyncio.wait_for(
                    drive_pipeline(bridge, c, pc, prompt="C-next"), 3
                )
                done = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                done = False
            check("U1.next-request-completes", done)
        finally:
            release.set()
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# T4：buffer 正文抑制（watchdog 后不得再发旧正文）
# ---------------------------------------------------------------------------
async def t4_buffer_suppression() -> None:
    import tests.harness as harness
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.entities import LLMResponse

    real_run_agent = harness.run_agent

    async def with_buffer(*args, **kwargs):
        kwargs["buffer_intermediate_messages"] = True
        async for result in real_run_agent(*args, **kwargs):
            yield result

    harness.run_agent = with_buffer
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            bridge, resolver, membership, ledger = make_bridge_stack(
                td, fail_watchdog_seconds=0.2
            )
            metas = register_bridge(bridge)
            entered, release = asyncio.Event(), asyncio.Event()

            class Tool(FunctionTool):
                async def call(self, context, **kwargs):
                    return "LOCAL-RESULT"

            class Provider(FakeProvider):
                async def text_chat(self, **kwargs):
                    if not self.call_log:
                        await FakeProvider.text_chat(self, **kwargs)
                        return LLMResponse(
                            role="assistant",
                            completion_text="BUFFERED-REPLY-BEFORE-TIMEOUT",
                            tools_call_name=["local_tool"],
                            tools_call_args=[{}],
                            tools_call_ids=["local-call-1"],
                        )
                    entered.set()
                    await release.wait()
                    return await super().text_chat(**kwargs)

            toolset = ToolSet(
                [
                    Tool(
                        name="local_tool",
                        description="synthetic",
                        parameters={"type": "object", "properties": {}},
                    )
                ]
            )
            ev = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="buffer test"
            )
            t = asyncio.create_task(
                harness.drive_pipeline(
                    bridge, ev, Provider(["FINAL"]), prompt="buffer test",
                    func_tool=toolset,
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            at_timeout = [c.get_plain_text() for c in ev.sent_chains]
            await asyncio.sleep(0.35)
            after_timeout = [c.get_plain_text() for c in ev.sent_chains]
            release.set()
            await asyncio.wait_for(t, 3)
            final = [c.get_plain_text() for c in ev.sent_chains]
            with sqlite3.connect(ledger._db_path) as db:
                states = db.execute("SELECT status FROM turns").fetchall()
            check(
                "T4.no-new-output-after-timeout",
                after_timeout == at_timeout,
                f"before={at_timeout} after={after_timeout}",
            )
            check(
                "T4.no-buffered-late-output",
                not any("BUFFERED-REPLY" in (x or "") for x in final),
                f"final={final}",
            )
            check(
                "T4.failed-finalized",
                states and states[0][0] == "failed",
                f"states={states}",
            )
            bridge.shutdown()
            cleanup(metas)
            ledger.close()
    finally:
        harness.run_agent = real_run_agent


# ---------------------------------------------------------------------------
# 真实宿主组件 worker（T1/T5/T6）
# ---------------------------------------------------------------------------
def _run_worker(script: str, extra_args: list[str] | None = None):
    repo = Path(__file__).resolve().parent.parent
    results = {}
    for venv in (r"D:\第三方插件完善\.venv", r"D:\第三方插件完善\.venv426"):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            env = dict(os.environ)
            env.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(repo),
                }
            )
            r = subprocess.run(
                [
                    venv + r"\Scripts\python.exe",
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve().parent / script),
                    td,
                ]
                + (extra_args or []),
                capture_output=True,
                text=True,
                env=env,
                cwd=str(repo),
                timeout=300,
            )
            tag = Path(venv).name
            line = next(
                (l for l in r.stdout.splitlines() if l.startswith("@@RESULT@@")),
                None,
            )
            if line is None:
                results[tag] = {"__error__": (r.stderr or r.stdout)[-400:]}
            else:
                results[tag] = json.loads(line[len("@@RESULT@@") :])
    return results


def t1_t5_t6_workers() -> None:
    # T1：真实 PersonaManager/ConversationManager 新会话 + 不同默认人格
    res = _run_worker("t1_persona_worker.py")
    for tag, out in res.items():
        if "__error__" in out:
            check(f"T1.{tag}.worker", False, out["__error__"])
            continue
        check(
            f"T1.{tag}.distinct-persona-keys",
            out.get("a_scope") != out.get("b_scope")
            and out.get("a_scope") not in (None, "__default__"),
            f"out={out}",
        )
        check(
            f"T1.{tag}.b-excludes-a-history",
            out.get("b_received_a_private") is False,
            "",
        )
        check(
            f"T1.{tag}.b-keeps-own-begin-dialogs",
            out.get("b_begin_dialog") is True,
            "",
        )
        check(
            f"T1.{tag}.explicit-conversation-persona",
            out.get("explicit_scope") == "persona_b",
            f"got={out.get('explicit_scope')}",
        )
        check(
            f"T1.{tag}.resolution-failure-controlled",
            out.get("failure_not_captured") is True
            and out.get("failure_key_absent") is True,
            "",
        )

    # T5/T6：真实 Context + 命令分发矩阵
    res = _run_worker("t5_native_worker.py")
    for tag, out in res.items():
        if "__error__" in out:
            check(f"T5T6.{tag}.worker", False, out["__error__"])
            continue
        # T5：真实 Context（两版接口差异）下的 default-reset
        dr = out.get("default-reset", {})
        check(
            f"T5.{tag}.real-context-reset-syncs",
            dr.get("native_update_calls") == 1
            and dr.get("history_after") == 0
            and dr.get("epoch_bumped") is True,
            f"dr={dr}",
        )
        check(
            f"T5.{tag}.no-provider-no-clear",
            out.get("no-provider", {}).get("history_after") == 2
            and out.get("no-provider", {}).get("plugin_notified") is False,
            f"np={out.get('no-provider')}",
        )
        # T6：禁用/改名/过滤矩阵
        bd = out.get("builtins-disabled-reset", {})
        check(
            f"T6.{tag}.disabled-no-clear",
            bd.get("native_update_calls") == 0 and bd.get("history_after") == 2,
            f"bd={bd}",
        )
        bdn = out.get("builtins-disabled-new", {})
        check(
            f"T6.{tag}.disabled-new-no-clear",
            bdn.get("native_new_calls") == 0 and bdn.get("history_after") == 2,
            f"bdn={bdn}",
        )
        rn_old = out.get("builtin-reset-renamed-old-name", {})
        check(
            f"T6.{tag}.renamed-old-name-no-clear",
            rn_old.get("native_update_calls") == 0
            and rn_old.get("history_after") == 2,
            f"rn_old={rn_old}",
        )
        rn_new = out.get("builtin-reset-renamed-new-name", {})
        check(
            f"T6.{tag}.renamed-new-name-syncs",
            rn_new.get("native_update_calls") == 1
            and rn_new.get("history_after") == 0
            and rn_new.get("epoch_bumped") is True,
            f"rn_new={rn_new}",
        )
        fd = out.get("builtin-reset-custom-filter-denied", {})
        check(
            f"T6.{tag}.filter-denied-no-clear",
            fd.get("native_update_calls") == 0 and fd.get("history_after") == 2,
            f"fd={fd}",
        )
        # U2：new 按自身成功语义联动（无 provider 也成功）
        nwp = out.get("new-with-provider", {})
        nwo = out.get("new-without-provider", {})
        check(
            f"U2.{tag}.new-with-provider-syncs",
            nwp.get("native_new_calls") == 1
            and nwp.get("history_after") == 0
            and nwp.get("epoch_bumped") is True,
            f"nwp={nwp}",
        )
        check(
            f"U2.{tag}.new-without-provider-syncs",
            nwo.get("native_new_calls") == 1
            and nwo.get("history_after") == 0
            and nwo.get("epoch_bumped") is True
            and nwo.get("plugin_notified") is True,
            f"nwo={nwo}",
        )
        rec = out.get("recovery_no_backfill_after_new", {})
        check(
            f"U2.{tag}.recovery-no-backfill",
            rec.get("old_leak") is False and rec.get("new_turn_present") is True,
            f"rec={rec}",
        )
        # U3：结构化成功标记（_clean_group_context_session）替代文本前缀
        pfx = out.get("reset-prefix-decorator", {})
        check(
            f"U3.{tag}.prefix-decorator-still-syncs",
            pfx.get("native_update_calls") == 1
            and pfx.get("history_after") == 0
            and pfx.get("epoch_bumped") is True
            and pfx.get("plugin_notified") is True,
            f"pfx={pfx}",
        )
        dr2 = out.get("default-reset", {})
        check(
            f"U3.{tag}.clean-mark-structure",
            dr2.get("clean_marked") is True
            and out.get("no-provider", {}).get("clean_marked") is None,
            "成功路径设置结构化标记，拒绝路径不设",
        )


async def main() -> int:
    await u1_cancellation_windows()
    await t2_cancel_window()
    await t4_buffer_suppression()
    t1_t5_t6_workers()
    print(f"\n=== T1~T6 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
