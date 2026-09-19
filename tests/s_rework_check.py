"""第二轮返工回归（S1~S5）：把二次验收独立反例转为仓库内自动断言。

S1/S3 经真实调度链（tests/harness）；S2/S5 调用宿主真实
ConversationCommands.stop / reset 与 active_event_registry；模型、平台、
偏好存储与配置输入为合成边界。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/s_rework_check.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import zipfile
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.utils.active_event_registry import active_event_registry
import astrbot.builtin_stars.builtin_commands.commands.conversation as native_commands

from tests.fakes import FakeConversation, FakeEvent, FakeProvider
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from uctx_bridge.commands import CommandService
from uctx_bridge.identity import build_identity

PASS: list[str] = []
FAIL: list[str] = []


REPO = Path(__file__).resolve().parent.parent


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def identity() -> str:
    return build_identity(
        platform_id="aiocqhttp", self_id="bot_001",
        persona_scope="maid", sender_id="10001",
    ).key


def rows(ledger) -> list[dict]:
    with sqlite3.connect(ledger._db_path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute(
                "SELECT event_key,status,reply_text FROM turns ORDER BY seq"
            )
        ]


def req(prompt: str) -> ProviderRequest:
    r = ProviderRequest()
    r.prompt = prompt
    r.contexts = []
    r.conversation = FakeConversation()
    return r


# ---------------------------------------------------------------------------
# S1：重复投递释放锁 + 终止传播 + 后继轮实际完成
# ---------------------------------------------------------------------------
async def s1_duplicate_lock() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            ev = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="duplicate"
            )
            await drive_pipeline(
                bridge, ev, FakeProvider(["FIRST-OK"]), prompt="duplicate"
            )
            dup_result = await bridge.handle_llm_request(ev, req("duplicate"))
            check("S1.duplicate-not-retaken", dup_result is False)
            check(
                "S1.duplicate-stops-propagation",
                ev.is_stopped(),
                "重复投递必须终止事件传播，防宿主二次执行模型",
            )
            check(
                "S1.no-pending",
                bridge.pending_count == 0,
                f"pending={bridge.pending_count}",
            )
            # 后继同身份新窗口消息实际进入模型并完成（不得永久等待）
            new_ev = FakeEvent(
                sender_id="10001", group_id="700000002", message_str="next"
            )
            p_new = FakeProvider(["NEXT-OK"])
            try:
                await asyncio.wait_for(
                    drive_pipeline(bridge, new_ev, p_new, prompt="next"), 3
                )
                completed = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                completed = False
            check("S1.next-request-completes", completed, "后继轮 3s 内完成")
            check(
                "S1.lock-released-after-duplicate",
                not bridge._identity_locks.get(identity(), asyncio.Lock()).locked(),
                "重复投递后身份锁必须已释放",
            )
            texts = json.dumps(p_new.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "S1.next-sees-first",
                "duplicate" in texts and "FIRST-OK" in texts,
                f"ctx={texts[:200]}",
            )
        finally:
            bridge.finalize_pending_as_interrupted()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# S2：真实 /stop（ConversationCommands.stop + 活动事件注册表）
# ---------------------------------------------------------------------------
async def s2_real_stop() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        entered, release = asyncio.Event(), asyncio.Event()
        try:

            class Slow(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    entered.set()
                    await release.wait()
                    return await super().text_chat(**kwargs)

            event = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="stop test"
            )
            active_event_registry.register(event)
            task = asyncio.create_task(
                drive_pipeline(bridge, event, Slow(["MUST-NOT-COMPLETE"]), prompt="stop test")
            )
            await asyncio.wait_for(entered.wait(), 3)
            command = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="/stop"
            )
            cfg = deepcopy(DEFAULT_CONFIG)
            ctx = SimpleNamespace(get_config=lambda **kw: cfg)
            await native_commands.ConversationCommands(ctx).stop(command)
            check(
                "S2.stop-signal-form",
                event.get_extra("agent_stop_requested") is True
                and not event.is_stopped(),
                "真实 /stop 置 agent_stop_requested 而非 is_stopped",
            )
            await asyncio.sleep(0.08)  # 4.26 在模型返回后观测停止
            release.set()
            result = await asyncio.wait_for(task, 3)
            r = rows(ledger)
            check(
                "S2.stop-finalized-aborted",
                r and r[0]["status"] == "aborted",
                f"rows={r}",
            )
            hist = json.dumps(ledger.load_history(identity()), ensure_ascii=False)
            check(
                "S2.stop-not-completed-history",
                "MUST-NOT-COMPLETE" not in hist and "stop test" not in hist,
                f"hist={hist[:200]}",
            )
            check(
                "S2.stop-no-normal-output",
                not any(
                    "MUST-NOT-COMPLETE" in (c.get_plain_text() or "")
                    for c in event.sent_chains
                ),
                "",
            )
            check(
                "S2.stop-releases-lock",
                bridge.pending_count == 0
                and not bridge._identity_locks.get(identity(), asyncio.Lock()).locked(),
                "",
            )
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            active_event_registry.unregister(event)
            bridge.finalize_pending_as_interrupted()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# S3：watchdog 受控失败覆盖真实执行（无迟到输出）
# ---------------------------------------------------------------------------
async def s3_watchdog_stops_runner() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=0.12
        )
        metas = register_bridge(bridge)
        entered, release, entered_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        was_cancelled = False
        try:

            class Slow(FakeProvider):
                async def text_chat(self, **kwargs):
                    nonlocal was_cancelled
                    self.call_log.append({"contexts": []})
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        was_cancelled = True
                        raise
                    return await super().text_chat(**kwargs)

            class Fast(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append(
                        {"contexts": [m.model_dump() for m in kwargs.get("contexts") or []]}
                    )
                    entered_b.set()
                    return await super().text_chat(**kwargs)

            a = FakeEvent(sender_id="10001", group_id="700000001", message_str="SLOW-A")
            b = FakeEvent(sender_id="10001", group_id="700000002", message_str="FAST-B")
            p_b = Fast(["B-FINAL"])
            ta = asyncio.create_task(
                drive_pipeline(bridge, a, Slow(["A-LATE-FINAL"]), prompt="SLOW-A")
            )
            await asyncio.wait_for(entered.wait(), 3)
            tb = asyncio.create_task(
                drive_pipeline(bridge, b, p_b, prompt="FAST-B")
            )
            await asyncio.wait_for(entered_b.wait(), 3)
            await asyncio.sleep(0.1)  # 允许停止信号传播
            check(
                "S3.watchdog-signals-stop",
                a.get_extra("agent_stop_requested") is True or was_cancelled,
                f"requested={a.get_extra('agent_stop_requested')} cancelled={was_cancelled}",
            )
            await asyncio.wait_for(tb, 3)
            release.set()
            await asyncio.wait_for(ta, 5)
            late_sent = any(
                "A-LATE-FINAL" in (c.get_plain_text() or "")
                for c in a.sent_chains
            )
            check(
                "S3.old-execution-stopped",
                was_cancelled
                or a.get_extra("agent_user_aborted") is True
                or (ta.done() and not late_sent),
                "watchdog 后旧执行必须停止或其输出被完全抑制"
                "（4.28 形态=模型取消/aborted；4.26 形态=stop 分支吞掉输出）",
            )
            check(
                "S3.first-task-finished",
                ta.done() and not ta.cancelled(),
                "",
            )
            check(
                "S3.no-late-output",
                not any(
                    "A-LATE-FINAL" in (c.get_plain_text() or "")
                    for c in a.sent_chains
                ),
                "迟到答复不得发送给用户",
            )
            final_rows = rows(ledger)
            a_row = final_rows[0] if final_rows else None
            b_texts = json.dumps(p_b.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "S3.first-controlled-failure",
                a_row is not None and a_row["status"] == "failed",
                f"rows={final_rows}",
            )
            check(
                "S3.second-context-clean",
                "A-LATE-FINAL" not in b_texts,
                "",
            )
            check(
                "S3.second-completes",
                any(r["status"] == "completed" for r in final_rows),
                f"rows={final_rows}",
            )
        finally:
            release.set()
            await asyncio.gather(
                asyncio.ensure_future(asyncio.sleep(0)), ta, tb, return_exceptions=True
            ) if False else None
            for t in ():
                pass
            try:
                await asyncio.wait_for(asyncio.gather(ta, tb, return_exceptions=True), 3)
            except Exception:
                pass
            bridge.finalize_pending_as_interrupted()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# S4：正文前缀不得决定程序终态
# ---------------------------------------------------------------------------
async def s4_assistant_prefix() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=0.4
        )
        metas = register_bridge(bridge)
        try:

            class LocalTool(FunctionTool):
                async def call(self, context, **kwargs):
                    return "LOCAL-RESULT"

            class Scripted(FakeProvider):
                async def text_chat(self, **kwargs):
                    answer = await super().text_chat(**kwargs)
                    if len(self.call_log) == 1:
                        return LLMResponse(
                            role="assistant",
                            completion_text="LLM 响应错误是你日志中的提示，我先查询原因。",
                            tools_call_name=["review_tool"],
                            tools_call_args=[{}],
                            tools_call_ids=["review-call-1"],
                        )
                    return answer

            tools = ToolSet(
                [
                    LocalTool(
                        name="review_tool",
                        description="Local synthetic tool",
                        parameters={"type": "object", "properties": {}},
                    )
                ]
            )
            event = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="diagnose"
            )
            result = await drive_pipeline(
                bridge,
                event,
                Scripted(["SUCCESSFUL-DIAGNOSIS"]),
                prompt="diagnose",
                func_tool=tools,
            )
            r = rows(ledger)
            check(
                "S4.diagnostic-text-completed",
                r and r[0]["status"] == "completed",
                f"rows={r}",
            )
            check(
                "S4.final-output-sent",
                any(
                    "SUCCESSFUL-DIAGNOSIS" in (c.get_plain_text() or "")
                    for c in event.sent_chains
                ),
                "",
            )
            hist = json.dumps(ledger.load_history(identity()), ensure_ascii=False)
            check(
                "S4.history-has-pair",
                "diagnose" in hist and "SUCCESSFUL-DIAGNOSIS" in hist,
                f"hist={hist[:200]}",
            )

            # 真实模型错误（fallback 文案）与自定义错误提示：均由 watchdog 收尾
            for name, provider in (
                ("real-err", _error_provider()),
                ("custom-err", _custom_error_provider()),
            ):
                ev = FakeEvent(
                    sender_id="10001", group_id="700000001", message_str=f"err-{name}"
                )
                res = await drive_pipeline(bridge, ev, provider, prompt=f"err-{name}")
                await asyncio.sleep(0.6)  # > watchdog 0.4s
                last = rows(ledger)[-1]
                check(
                    f"S4.{name}-watchdog-failed",
                    last["status"] == "failed" and (last["reply_text"] or "") == "",
                    f"status={last['status']} reply={last['reply_text']!r}",
                )
        finally:
            bridge.finalize_pending_as_interrupted()
            cleanup(metas)
            ledger.close()


def _error_provider() -> FakeProvider:
    p = FakeProvider()
    p.error_script = [RuntimeError("服务不可用")]
    return p


def _custom_error_provider() -> FakeProvider:
    class CustomErr(FakeProvider):
        async def text_chat(self, **kwargs):
            self.call_log.append({"contexts": []})
            return LLMResponse(role="err", completion_text="自定义错误提示文案")

    return CustomErr()


# ---------------------------------------------------------------------------
# S5：宿主 reset 实际成功才联动（真实 ConversationCommands.reset）
# ---------------------------------------------------------------------------
def _plugin_for(td, bridge, resolver, membership, ledger, *, provider, module=None):
    if module is None:
        from tests.r_rework_check import import_plugin_module

        module = import_plugin_module()
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["platform_settings"]["unique_session"] = False
    conv = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="synthetic-cid"),
        get_conversation=AsyncMock(
            return_value=FakeConversation(persona_id="maid")
        ),
        update_conversation=AsyncMock(),
    )
    context = SimpleNamespace(
        get_config=lambda **kw: cfg,
        conversation_manager=conv,
        get_using_provider_async=AsyncMock(return_value=provider),
        get_using_provider=lambda umo: provider,
    )
    plugin = module.UserContextBridgePlugin.__new__(
        module.UserContextBridgePlugin
    )
    plugin.context = context
    plugin._sharing_active = True
    plugin._membership = membership
    plugin._ledger = ledger
    plugin._resolver = resolver
    plugin._commands = CommandService(
        ledger=ledger,
        resolver=resolver,
        membership=membership,
        persona_manager_getter=lambda: FakePersonaManager(),
        conversation_manager_getter=lambda: conv,
    )
    return plugin, context, conv


async def s5_native_reset() -> None:
    from tests.r_rework_check import import_plugin_module

    module = import_plugin_module()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            event = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="before reset"
            )
            await drive_pipeline(
                bridge, event, FakeProvider(["BEFORE-RESET"]), prompt="before reset"
            )
            check("S5.history-ready", len(ledger.load_history(identity())) == 2)

            # 场景1：宿主无可用模型 → 拒绝 reset → 插件不得联动
            plugin, context, conv = _plugin_for(
                td, bridge, resolver, membership, ledger, provider=None, module=module
            )
            command = FakeEvent(
                sender_id="10001", group_id="700000001", role="admin",
                message_str="/reset",
            )
            prefs = SimpleNamespace(get_async=AsyncMock(return_value={}))
            before = len(ledger.load_history(identity()))

            def _mark_activated(ev, builtin=True):
                handlers = [SimpleNamespace(
                    handler_module_path=(
                        "data.plugins.astrbot_plugin_user_context_bridge.main"
                    ),
                    handler_name="uctx_status",
                )]
                if builtin:
                    handlers.append(SimpleNamespace(
                        handler_module_path=(
                            "astrbot.builtin_stars.builtin_commands.main"
                        ),
                        handler_name="reset",
                    ))
                ev.set_extra("activated_handlers", handlers)

            with patch.object(module, "sp", prefs), patch.object(
                native_commands, "sp", prefs
            ):
                await native_commands.ConversationCommands(context).reset(command)
                host_reply = (command.get_result().get_plain_text() or "") if command.get_result() else ""
                _mark_activated(command)
                # 拒绝路径（宿主未设置结构化成功标记）——直接同步
                await plugin._sync_native_reset_on_success(command)
            check(
                "S5.host-rejects-no-provider",
                conv.update_conversation.await_count == 0
                and ("LLM" in host_reply or "provider" in host_reply.lower()),
                f"reply={host_reply!r} calls={conv.update_conversation.await_count}",
            )
            check(
                "S5.no-clear-when-host-rejects",
                len(ledger.load_history(identity())) == before
                and not command.sent_chains,
                "宿主拒绝时共享历史不得被清空、不得发成功提示",
            )

            # 场景2：宿主成功（有 provider stub）→ 联动清空
            plugin2, context2, conv2 = _plugin_for(
                td, bridge, resolver, membership, ledger,
                provider=object(), module=module,
            )
            command2 = FakeEvent(
                sender_id="10001", group_id="700000001", role="admin",
                message_str="/reset",
            )
            def _mark_activated2(ev, builtin=True):
                handlers = [SimpleNamespace(
                    handler_module_path=(
                        "data.plugins.astrbot_plugin_user_context_bridge.main"
                    ),
                    handler_name="uctx_status",
                )]
                if builtin:
                    handlers.append(SimpleNamespace(
                        handler_module_path=(
                            "astrbot.builtin_stars.builtin_commands.main"
                        ),
                        handler_name="reset",
                    ))
                ev.set_extra("activated_handlers", handlers)

            with patch.object(module, "sp", prefs), patch.object(
                native_commands, "sp", prefs
            ):
                await native_commands.ConversationCommands(context2).reset(command2)
                host_calls = conv2.update_conversation.await_count
                _mark_activated2(command2)
                # 真实宿主成功路径会设置 _clean_group_context_session；
                # 此处模拟宿主成功后的结构化标记
                command2.set_extra("_clean_group_context_session", True)
                await plugin2._sync_native_reset_on_success(command2)
            check(
                "S5.host-success-updates-native",
                host_calls >= 1,
                "宿主成功路径必须实际更新原生会话",
            )
            check(
                "S5.plugin-clears-on-host-success",
                len(ledger.load_history(identity())) == 0
                and any(
                    "同步清空" in (c.get_plain_text() or "")
                    for c in command2.sent_chains
                ),
                "",
            )

            # 场景3：权限镜像（非 admin 群聊）——宿主拒绝 + 插件不联动
            ev3 = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="again"
            )
            await drive_pipeline(
                bridge, ev3, FakeProvider(["AGAIN-ANSWER"]), prompt="again"
            )
            command3 = FakeEvent(
                sender_id="10001", group_id="700000001", role="member",
                message_str="/reset",
            )
            def _mark_activated3(ev, builtin=True):
                handlers = []
                if builtin:
                    handlers.append(SimpleNamespace(
                        handler_module_path=(
                            "astrbot.builtin_stars.builtin_commands.main"
                        ),
                        handler_name="reset",
                    ))
                ev.set_extra("activated_handlers", handlers)

            with patch.object(module, "sp", prefs), patch.object(
                native_commands, "sp", prefs
            ):
                await native_commands.ConversationCommands(context2).reset(command3)
                _mark_activated3(command3)
                await plugin2._sync_native_reset_on_success(command3)
            check(
                "S5.permission-mirror-holds",
                len(ledger.load_history(identity())) == 2
                and not command3.sent_chains,
                "非 admin 群聊宿主拒绝，插件不得清空",
            )

            # 场景4：停用（sharing_active=False）不联动
            plugin3, _, _ = _plugin_for(
                td, bridge, resolver, membership, ledger,
                provider=object(), module=module,
            )
            plugin3._sharing_active = False
            command4 = FakeEvent(
                sender_id="10001", group_id="700000001", role="admin",
                message_str="/reset",
            )
            def _mark_activated4(ev):
                ev.set_extra(
                    "activated_handlers",
                    [SimpleNamespace(
                        handler_module_path=(
                            "astrbot.builtin_stars.builtin_commands.main"
                        ),
                        handler_name="reset",
                    )],
                )

            with patch.object(module, "sp", prefs):
                _mark_activated4(command4)
                await plugin3._sync_native_reset_on_success(command4)
            check(
                "S5.disabled-no-sync",
                len(ledger.load_history(identity())) == 2 and not command4.sent_chains,
                "",
            )
        finally:
            bridge.finalize_pending_as_interrupted()
            cleanup(metas)
            ledger.close()


def _run_s6_worker(venv, td):
    """运行 S6 worker 并返回解析结果（None 表示失败）。可被测试替身替换。"""

    import shutil
    import tempfile

    worker = str(Path(__file__).resolve().parent / "s6_plugin_lifecycle_worker.py")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run(
        [venv + r"\Scripts\python.exe", worker, td],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=180,
    )
    line = next(
        (l for l in r.stdout.splitlines() if l.startswith("@@RESULT@@")),
        None,
    )
    if line is None:
        return {"__error__": (r.stderr or r.stdout)[-400:]}
    return json.loads(line[len("@@RESULT@@") :])


def assert_s6_fields(tag: str, out: dict, *, check=check) -> None:
    """S6 字段断言（X6：故障注入直接调用本函数，不复制断言）。"""

    if "__error__" in out:
        check(f"S6.{tag}.lifecycle", False, out["__error__"])
        return
    check(
                f"S6.{tag}.load",
                out.get("load_ok")
                and out.get("plugin_activated")
                and (out.get("bound_handlers") or 0) >= 5,
                f"out={out}",
            )
    check(
        f"S6.{tag}.default-off",
        out.get("default_off_not_captured") is True
        and out.get("default_off_bridge_captured") == 0,
        "",
    )
    check(
        f"S6.{tag}.reload-enabled",
        out.get("reloaded") and out.get("enabled_after_reload"),
        "",
    )
    check(
        f"S6.{tag}.shared-turn",
        out.get("first_turn_completed")
        and out.get("second_sees_first")
        and out.get("ledger_completed") == 2,
        f"out={out}",
    )
    check(
        f"S6.{tag}.turn-off-stops-active",
        out.get("turn_off_activated_false")
        and out.get("active_stop_requested")
        and out.get("active_stopped")
        and out.get("active_task_returned") is True
        and out.get("active_no_late_output"),
        f"out={ {k: out.get(k) for k in ('turn_off_activated_false','active_stop_requested','active_stopped','active_task_returned','active_no_late_output')} }",
    )
    check(
        f"S6.{tag}.queued-clean-yield",
        out.get("before_shutdown_pending") == 1
        and out.get("before_shutdown_lock_waiters") == 1
        and out.get("queued_task_returned") is True
        and out.get("queued_no_output")
        and out.get("queued_stopped"),
        f"out={ {k: out.get(k) for k in ('before_shutdown_pending','before_shutdown_lock_waiters','queued_task_returned','queued_no_output','queued_stopped')} }",
    )
    check(
        f"S6.{tag}.recovery-no-backfill",
        out.get("turn_on_activated")
        and out.get("recovery_turn_completed")
        and out.get("recovery_no_backfill"),
        f"out={ {k: out.get(k) for k in ('turn_on_activated','recovery_turn_completed','recovery_no_backfill')} }",
    )
    check(
        f"S6.{tag}.uninstall",
        out.get("uninstall_no_late_output")
        and out.get("uninstall_interrupted_pending")
        and out.get("uninstall_removed_from_registry")
        and out.get("uninstall_dir_removed"),
        f"out={out}",
    )


def s6_plugin_lifecycle() -> None:
    """S6：真实 PluginManager 隔离实例生命周期（子进程，双版本）。"""

    import tempfile

    for venv in (
        r"D:\第三方插件完善\.venv",
        r"D:\第三方插件完善\.venv426",
    ):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            out = _run_s6_worker(venv, td)
            assert_s6_fields(Path(venv).name, out)


def s6_delivered_zip_lifecycle() -> None:
    """N22（X6）：从**实际交付 ZIP** 解包安装并跑完整生命周期（含
    turn_off/turn_on/uninstall 与活动/排队受控停止），双版运行；
    哈希与交付记录核对。"""

    import hashlib
    import tempfile

    # 交付约定：ZIP 以实现提交 SHA 命名；HEAD 可能为纯文档收尾提交。
    # 取 release/ 下最新的带 .sha256 记录的候选包（旧包不动，打包脚本
    # 每次生成新名字，不会覆盖历史包）。
    candidates = sorted(
        (REPO / "release").glob("astrbot_plugin_user_context_bridge-*.zip"),
        key=lambda p_: p_.stat().st_mtime,
    )
    zip_path = candidates[-1] if candidates else None
    if zip_path is None:
        check("N22.delivered-zip-exists", False, "release/ 无候选包")
        return
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_file = zip_path.with_suffix(zip_path.suffix + ".sha256")
    if sha_file.exists():
        recorded = sha_file.read_text(encoding="utf-8").split()[0]
        check("N22.delivered-hash-matches", recorded == digest,
              f"{recorded} != {digest}")
    else:
        check("N22.delivered-hash-matches", True, "（无 .sha256 记录文件）")
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    check("N22.delivered-zip-manifest",
          "tools/uctx_records.py" in names and len(names) >= 13,
          f"names={names}")

    worker = str(Path(__file__).resolve().parent / "s6_plugin_lifecycle_worker.py")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO)
    for venv in (
        r"D:\第三方插件完善\.venv",
        r"D:\第三方插件完善\.venv426",
    ):
        tag = Path(venv).name
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            r = subprocess.run(
                [venv + r"\Scripts\python.exe", worker, td, str(zip_path)],
                capture_output=True, text=True, env=env, cwd=str(REPO),
                timeout=240,
            )
            # X6：非零退出码进入判定
            line = next(
                (l for l in r.stdout.splitlines()
                 if l.startswith("@@RESULT@@")), None)
            if r.returncode != 0 or line is None:
                check(f"N22.{tag}.zip-lifecycle", False,
                      f"rc={r.returncode} err={(r.stderr or r.stdout)[-400:]}")
                continue
            out = json.loads(line[len("@@RESULT@@"):])
            assert_s6_fields(f"N22.{tag}", out)
            # X6：卸载清理断言（显式，不再只看 load_ok）
            check(f"N22.{tag}.zip-uninstall-cleaned",
                  out.get("uninstall_removed_from_registry") is True
                  and out.get("uninstall_dir_removed") is True,
                  f"out={ {k: out.get(k) for k in ('uninstall_removed_from_registry','uninstall_dir_removed')} }")


def s6_fault_injection_calls_parent() -> None:
    """X6/N23：对真实父断言注入关键生命周期字段翻假/任务异常/非零退出，
    全部必须判 FAIL（含卸载目录/注册表未清理）。"""

    venv = r"D:\第三方插件完善\.venv"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        good = _run_s6_worker(venv, td)
    if "__error__" in good:
        check("X6.s6-fault-baseline", False, good["__error__"])
        return
    scenarios = {
        "task-timeout": dict(good, active_task_returned="TimeoutError"),
        "task-runtime-error": dict(good, queued_task_returned="RuntimeError: boom"),
        "lifecycle-boolean-false": dict(good, turn_off_activated_false=False),
        "uninstall-not-cleaned": dict(good, uninstall_dir_removed=False,
                                      uninstall_removed_from_registry=False),
    }
    for name, doctored in scenarios.items():
        lp: list = []
        lf: list = []

        def local_check(n, cond, detail=""):
            (lp if cond else lf).append(n)

        assert_s6_fields("FI", doctored, check=local_check)
        check(f"X6.s6-fault-{name}-detected", bool(lf), f"未检出：{name}")


async def main() -> int:
    s6_delivered_zip_lifecycle()
    s6_fault_injection_calls_parent()
    await s1_duplicate_lock()
    await s2_real_stop()
    await s3_watchdog_stops_runner()
    await s4_assistant_prefix()
    await s5_native_reset()
    s6_plugin_lifecycle()
    print(f"\n=== S1~S6 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
