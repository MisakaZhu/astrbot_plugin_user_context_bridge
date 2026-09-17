"""真实宿主调度链测试 harness（R2 返工后所有闭环测试共用）。

复刻宿主 pipeline 的真实顺序：run_agent 每个 yield 立即流经
ResultDecorateStage（真实初始化）与传输层（发送 + OnAfterMessageSentEvent），
由真实 PipelineScheduler._process_stages 驱动——不做「排空后手动补钩子」。
模型/平台/配置存储为可控合成替身。
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import EventType

from tests.fakes import FakeConversation, FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import FakePersonaManager
from uctx_bridge.bridge import ContextBridge
from uctx_bridge.ledger import TurnLedger
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver


def make_bridge_stack(
    td: str,
    *,
    fail_watchdog_seconds: float = 5.0,
    persona_manager_getter=None,
    provider_settings_getter=None,
    **scope_kw,
):
    """构造 ledger/resolver/bridge 栈（R6 后租约由 main 装配，此处不涉及）。"""

    ledger = TurnLedger(str(__import__("pathlib").Path(td) / "l.db"))
    ledger.open()
    membership = MembershipStore(
        __import__("pathlib").Path(td) / "membership.json"
    )
    resolver = ScopeResolver(
        ScopeConfig(
            enabled=True,
            shared_groups=frozenset({"700000001", "700000002"}),
            include_private=True,
            **scope_kw,
        ),
        membership,
    )
    bridge = ContextBridge(
        ledger=ledger,
        scope_resolver=resolver,
        persona_manager_getter=persona_manager_getter
        or (lambda: FakePersonaManager()),
        provider_settings_getter=provider_settings_getter,
        logger=None,
        fail_watchdog_seconds=fail_watchdog_seconds,
    )
    return bridge, resolver, membership, ledger


async def drive_pipeline(
    bridge: ContextBridge,
    event: FakeEvent,
    provider: FakeProvider,
    *,
    prompt: str = "review-question",
    persona: str | None = None,
    extra_parts=None,
    func_tool=None,
    abort_before_run: bool = False,
) -> dict:
    """驱动一轮完整宿主链路：钩子 → reset → 真实调度（yield 即下游）。"""

    req = ProviderRequest()
    req.prompt = prompt
    req.contexts = []
    req.system_prompt = "harness system prompt"
    req.conversation = FakeConversation(
        user_id=event.unified_msg_origin, persona_id=persona
    )
    if extra_parts:
        req.extra_user_content_parts = list(extra_parts)
    if func_tool is not None:
        req.func_tool = func_tool
    hook_stopped = await call_event_hook(event, EventType.OnLLMRequestEvent, req)
    if hook_stopped or event.is_stopped():
        # 宿主 internal.py 语义：钩子终止事件后不再执行模型
        trace.append({"phase": "hook-stopped", "aborted": None})
        return {
            "trace": trace,
            "pending": bridge.pending_count,
            "model_calls": 0,
            "sent": len(event.sent_chains),
        }

    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=MAIN_AGENT_HOOKS,
        streaming=False,
    )
    if abort_before_run:
        event._force_stopped = True
        runner.request_stop()

    trace: list[dict] = []

    class AgentStage:
        async def process(self, event_inner):
            async for _ in run_agent(runner, 30, True, False, False):
                pending = bridge._pending.get(bridge.event_key_for(event_inner))
                trace.append(
                    {
                        "phase": "yield-to-real-downstream",
                        "done_seen": getattr(pending, "done_seen", None),
                        "aborted": event_inner.get_extra("agent_user_aborted"),
                    }
                )
                yield
            trace.append(
                {
                    "phase": "run-agent-returned",
                    "aborted": event_inner.get_extra("agent_user_aborted"),
                }
            )

    class FakeTransport:
        async def process(self, event_inner):
            result = event_inner.get_result()
            if result is not None and getattr(result, "chain", None):
                await event_inner.send(result)
                await call_event_hook(
                    event_inner, EventType.OnAfterMessageSentEvent
                )

    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["t2i"] = False
    cfg["content_safety"]["also_use_in_response"] = False
    cfg["provider_tts_settings"]["enable"] = False
    cfg["platform_settings"]["reply_with_mention"] = False
    cfg["platform_settings"]["reply_with_quote"] = False
    cfg["platform_settings"]["segmented_reply"]["enable"] = False
    ctx = SimpleNamespace(
        astrbot_config=cfg,
        plugin_manager=SimpleNamespace(
            context=SimpleNamespace(
                get_using_tts_provider_async=AsyncMock(return_value=None),
                get_using_tts_provider=lambda umo: None,
            )
        ),
    )
    decorator = ResultDecorateStage()
    await decorator.initialize(ctx)
    scheduler = PipelineScheduler.__new__(PipelineScheduler)
    scheduler.ctx = ctx
    scheduler.stages = [AgentStage(), decorator, FakeTransport()]
    await scheduler._process_stages(event)

    return {
        "trace": trace,
        "pending": bridge.pending_count,
        "model_calls": len(provider.call_log),
        "sent": len(event.sent_chains),
    }
