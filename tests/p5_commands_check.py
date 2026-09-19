"""P5 中文管理命令、停用重载与插件共存验证（MIS-141）。

覆盖 A05（命令层退出/恢复）、A16（停用/重载/不回灌）、A15（其他插件
system_prompt 动态注入共存）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/p5_commands_check.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core import logger as astrbot_logger

from tests.fakes import FakeConversation, FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from uctx_bridge.bridge import ContextBridge
from uctx_bridge.commands import CommandService
from uctx_bridge.identity import build_identity
from uctx_bridge.ledger import TurnLedger
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

PASS: list[str] = []
FAIL: list[str] = []
TEST_MODULE = "tests.p5_commands_check"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def identity_of(sender: str) -> str:
    return build_identity(
        platform_id="aiocqhttp",
        self_id="bot_001",
        persona_scope="maid",
        sender_id=sender,
    ).key


def make_stack(td: str, enabled: bool = True):
    ledger = TurnLedger(Path(td) / "l.db")
    ledger.open()
    membership = MembershipStore(Path(td) / "membership.json")
    resolver = ScopeResolver(
        ScopeConfig(
            enabled=enabled,
            shared_groups=frozenset({"700000001"}),
            include_private=True,
        ),
        membership,
    )
    bridge = ContextBridge(
        ledger=ledger,
        scope_resolver=resolver,
        persona_manager_getter=lambda: FakePersonaManager(),
        logger=None,
    )
    commands = CommandService(
        ledger=ledger,
        resolver=resolver,
        membership=membership,
        persona_manager_getter=lambda: FakePersonaManager(),
    )
    metas = register_bridge(bridge)
    return bridge, commands, resolver, membership, ledger, metas


async def drive_turn(event: FakeEvent, prompt: str, provider: FakeProvider) -> None:
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
    async for _ in run_agent(runner, 30, True, False, False):
        pass
    await call_event_hook(event, EventType.OnDecoratingResultEvent)


async def scenario_commands() -> None:
    """A05 命令层：status/reset/off/on/scope。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, commands, resolver, membership, ledger, metas = make_stack(td)
        try:
            ev = FakeEvent(sender_id="10001", group_id="700000001", message_str="/uctx status")
            # 先完成一轮产生历史
            await drive_turn(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="第一问"),
                "第一问",
                FakeProvider(reply_script=["第一答"]),
            )
            status_text = await commands.status(ev)
            check(
                "CMD.status-shows-state",
                "符合共享条件" in status_text and "1 轮已完成" in status_text
                and "实际接管" in status_text,
                f"text={status_text}",
            )
            check(
                "CMD.status-no-others-content",
                "第一答" not in status_text,
                "状态输出不得包含聊天正文",
            )

            # off → 判定不采集（用普通对话事件验证，命令消息本身不参与对话轮次）
            chat_ev = FakeEvent(sender_id="10001", group_id="700000001", message_str="普通消息")
            off_text = await commands.off(ev)
            check("CMD.off-text", "退出" in off_text)
            decision = resolver.evaluate(chat_ev, "maid")
            check(
                "A05.cmd-off-stops-capture",
                not decision.in_scope and decision.reason == "personal_optout",
                decision.reason,
            )
            on_text = await commands.on(ev)
            check("CMD.on-text", "重新加入" in on_text, on_text)
            check(
                "A05.cmd-on-restores",
                resolver.evaluate(chat_ev, "maid").in_scope,
                "",
            )

            # reset：epoch 切换 + 历史清空
            reset_text = await commands.reset(ev)
            check(
                "CMD.reset-text",
                "清空" in reset_text and "其他用户不受影响" in reset_text,
                reset_text,
            )
            check(
                "A11.cmd-reset-clears",
                ledger.load_history(identity_of("10001")) == [],
                "",
            )
            # 他人不受影响
            await drive_turn(
                FakeEvent(sender_id="20002", group_id="700000001", message_str="别人的问"),
                "别人的问",
                FakeProvider(reply_script=["别人的答"]),
            )
            await commands.reset(ev)
            check(
                "A11.cmd-reset-not-affect-others",
                len(ledger.load_history(identity_of("20002"))) == 2,
                "",
            )

            # scope 只读展示
            scope_text = commands.scope()
            check(
                "CMD.scope-text",
                "700000001" in scope_text and "只读" in scope_text,
                scope_text,
            )

            # 个人 on 不能扩大范围：范围外群用户 on 后仍不共享
            ev_out = FakeEvent(sender_id="30003", group_id="700000999", message_str="/uctx on")
            await commands.on(ev_out)
            decision_out = resolver.evaluate(ev_out, "maid")
            check(
                "A05.personal-on-cannot-expand",
                not decision_out.in_scope,
                decision_out.reason,
            )
        finally:
            cleanup(metas)
            ledger.close()


async def scenario_disable_reload() -> None:
    """A16：停用（enabled=false）→ 恢复原生；重启用后停用期间消息不回灌。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, commands, resolver, membership, ledger, metas = make_stack(td)
        try:
            # 阶段1：共享启用，一轮入库
            await drive_turn(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="共享期问题"),
                "共享期问题",
                FakeProvider(reply_script=["共享期回答"]),
            )
            # 阶段2：管理员停用（等效配置关闭后的插件重载）
            resolver.update_config(ScopeConfig(enabled=False, shared_groups=frozenset({"700000001"}), include_private=True))
            provider_off = FakeProvider()
            await drive_turn(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="停用期问题"),
                "停用期问题",
                provider_off,
            )
            off_texts = [
                m.get("content") if isinstance(m.get("content"), str) else ""
                for m in provider_off.call_log[0]["contexts"]
            ]
            check(
                "A16.disabled-not-injecting",
                all("共享期问题" not in t for t in off_texts),
                f"texts={off_texts}",
            )
            check(
                "A16.disabled-native-behavior",
                len(provider_off.call_log[0]["contexts"]) == 2,
                "停用后按原生窗口（空历史）行为",
            )
            # 阶段3：重新启用——共享历史不含停用期间的原生会话
            resolver.update_config(ScopeConfig(enabled=True, shared_groups=frozenset({"700000001"}), include_private=True))
            provider_re = FakeProvider()
            await drive_turn(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="重启后问题"),
                "重启后问题",
                provider_re,
            )
            def flat(m):
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(
                        p.get("text", "") for p in c if isinstance(p, dict)
                    )
                return ""

            re_texts = "".join(flat(m) for m in provider_re.call_log[0]["contexts"])
            check(
                "A16.re-enabled-no-backfill",
                "共享期问题" in re_texts and "停用期问题" not in re_texts,
                f"texts={re_texts}",
            )
        finally:
            cleanup(metas)
            ledger.close()


async def scenario_plugin_coexistence() -> None:
    """A15：其他插件 on_llm_request 动态注入共存（system_prompt 追加）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, commands, resolver, membership, ledger, metas = make_stack(td)
        other_metas = []
        try:
            # 模拟另一插件：在 on_llm_request 中追加 system_prompt 与 extra part
            async def other_plugin(event, req):
                req.system_prompt = (req.system_prompt or "") + "\n# OtherPlugin\n记住你们的关系设定。\n"

            star_map["tests.p5_other_plugin"] = StarMetadata(
                name="p5_other_plugin", activated=True
            )
            meta = StarHandlerMetadata(
                event_type=EventType.OnLLMRequestEvent,
                handler_full_name="tests.p5_other_plugin_on_llm_request",
                handler_name="on_llm_request",
                handler_module_path="tests.p5_other_plugin",
                handler=other_plugin,
                event_filters=[],
            )
            star_handlers_registry._handlers.append(meta)
            other_metas.append(meta)

            provider = FakeProvider()
            await drive_turn(
                FakeEvent(sender_id="10001", group_id="700000001", message_str="共存测试"),
                "共存测试",
                provider,
            )
            msgs = provider.call_log[0]["contexts"]
            head = msgs[0]["content"] if isinstance(msgs[0].get("content"), str) else ""
            check(
                "A15.other-plugin-prompt-kept",
                head.startswith("sys") and "OtherPlugin" in head and "关系设定" in head,
                f"head={head!r}",
            )
            check(
                "A15.persona-also-kept",
                "sys" in head,
                "",
            )
        finally:
            for m in other_metas:
                star_handlers_registry._handlers = [
                    h for h in star_handlers_registry._handlers if h != m
                ]
            star_map.pop("tests.p5_other_plugin", None)
            cleanup(metas)
            ledger.close()


async def main() -> int:
    await scenario_commands()
    await scenario_disable_reload()
    await scenario_plugin_coexistence()
    print(f"\n=== P5 命令/停用/共存验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
