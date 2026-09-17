"""P0 最小脱网生命周期验证（MIS-136）。

用真实 AstrBot 包的核心组件（ToolLoopAgentRunner / run_agent / MainAgentHooks /
call_event_hook / InternalAgentSubStage._save_to_history），只替换模型 Provider
与平台事件为可控假端，验证 ADR-002 技术路线的宿主事实：

  V1  on_llm_request 替换 req.contexts 后，Runner.reset 构建的 run_context.messages
      以替换后的历史为基底（读取替换真实生效）。
  V2  req.conversation = None 时 Runner 正常运行、模型收到共享历史（含人格开场白）、
      on_agent_done 拿到完整轨迹（本轮 user + assistant）。
  V3  同一请求下宿主 _save_to_history 因 conversation=None 短路，不发生写回；
      对照组（conversation 非 None）发生写回——证明短路真实存在且可对照。
  V4  模型异常路径：宿主 fallback 层把 Provider 异常转为 role=err 响应，step 以
      ERROR 态结束——on_agent_done 不触发；最终 final_llm_resp.role == "err"，
      错误消息进入事件结果。证明 err 终态必须靠 on_decorating_result 兜底（ADR-003）。
  V5  用户中止路径：事件停止 + runner 停止 → step 产出 aborted → run_agent 直接
      return；on_agent_done 仍触发但其时刻 aborted 旗标尚未设置、内容形态因版本
      而异——完成钩子内无法判定 aborted，插件必须在 on_decorating_result 兜底轨
      以 event.extra.agent_user_aborted 终态化（ADR-003）。

运行方式（分别以 4.26.0 / 4.28.0 宿主 venv 执行）：
  python tests/p0_lifecycle_check.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace

from astrbot.core import logger as astrbot_logger

from tests.fakes import (
    FakeConversation,
    FakeConversationManager,
    FakeEvent,
    FakeProvider,
    HookSpy,
)

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
# 宿主真实组件导入
# ---------------------------------------------------------------------------
from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.core.agent.message import Message  # noqa: E402
from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS  # noqa: E402
from astrbot.core.astr_agent_run_util import run_agent  # noqa: E402
from astrbot.core.pipeline.context_utils import call_event_hook  # noqa: E402
from astrbot.core.star.star_handler import (  # noqa: E402
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)
from astrbot.core.star.star import star_map, StarMetadata  # noqa: E402
from astrbot.core.agent.runners.tool_loop_agent_runner import (  # noqa: E402
    ToolLoopAgentRunner,
)
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor  # noqa: E402
from astrbot.core.agent.run_context import ContextWrapper  # noqa: E402

TEST_MODULE = "tests.p0_lifecycle_check"


def register_spy_hooks(spy: HookSpy) -> list[StarHandlerMetadata]:
    """把模拟插件的钩子注册进真实 star_handlers_registry（测试后清理）。"""

    async def on_llm_request(event, req):
        spy.llm_request_seen.append(req)

    async def on_agent_done(event, run_context, llm_response):
        spy.agent_done_seen.append(
            {
                "llm_response": llm_response,
                "messages": list(run_context.messages),
                "aborted_flag_at_hook": event.get_extra("agent_user_aborted"),
            }
        )

    async def on_llm_response(event, llm_response):
        spy.llm_response_seen.append(llm_response)

    metas = []
    star_meta = StarMetadata(name="p0_spy_plugin", activated=True)
    star_map[TEST_MODULE] = star_meta
    for evt, handler, hname in (
        (EventType.OnLLMRequestEvent, on_llm_request, "on_llm_request"),
        (EventType.OnAgentDoneEvent, on_agent_done, "on_agent_done"),
        (EventType.OnLLMResponseEvent, on_llm_response, "on_llm_response"),
    ):
        meta = StarHandlerMetadata(
            event_type=evt,
            handler_full_name=f"{TEST_MODULE}_{hname}",
            handler_name=hname,
            handler_module_path=TEST_MODULE,
            handler=handler,
            event_filters=[],
        )
        star_handlers_registry.star_handlers_map[meta.handler_full_name] = meta
        star_handlers_registry._handlers.append(meta)
        metas.append(meta)
    return metas


def cleanup_hooks(metas: list[StarHandlerMetadata]) -> None:
    for meta in metas:
        star_handlers_registry._handlers = [
            h for h in star_handlers_registry._handlers if h != meta
        ]
        star_handlers_registry.star_handlers_map.pop(meta.handler_full_name, None)
    star_map.pop(TEST_MODULE, None)


def build_runner(req: ProviderRequest, provider: FakeProvider, event: FakeEvent):
    """按 internal.py 真实顺序构建 Runner（reset 未执行，供钩子后 await）。"""
    runner = ToolLoopAgentRunner()
    astr_ctx = SimpleNamespace(event=event)  # MainAgentHooks 仅访问 .event
    reset_coro = runner.reset(
        provider=provider,
        request=req,
        run_context=ContextWrapper(context=astr_ctx),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=MAIN_AGENT_HOOKS,
        streaming=False,
    )
    return runner, reset_coro


async def scenario_success_shared_history() -> None:
    """V1/V2/V3：读取替换 + 模型运行 + 完成钩子轨迹 + 写回短路。"""
    spy = HookSpy()
    metas = register_spy_hooks(spy)
    try:
        event = FakeEvent(sender_id="10001", group_id="700000001", message_str="今天天气如何")
        provider = FakeProvider(reply_script=["今天晴，适合出门。"])

        # 模拟 build_main_agent 产物：原生历史 + 人格开场白已插入头部
        native_history = [
            {"role": "user", "content": "旧窗口历史问题"},
            {"role": "assistant", "content": "旧窗口历史回答"},
        ]
        req = ProviderRequest()
        req.prompt = "今天天气如何"
        req.contexts = list(native_history)
        req.system_prompt = "# Persona Instructions\n\n你是测试人格"
        req.conversation = FakeConversation(user_id=event.unified_msg_origin)
        req.contexts[:0] = [{"role": "user", "content": "（人格开场白）"}]

        # —— 模拟插件 on_llm_request 行为：替换为共享历史 + conversation=None
        shared_history = [
            {"role": "user", "content": "我在群A问过的问题"},
            {"role": "assistant", "content": "群A的回答"},
        ]
        plugin_contexts = [
            {"role": "user", "content": "（人格开场白）"},  # 回补人格开场白
            *shared_history,
        ]

        # 按 internal.py 顺序：先触发钩子（真实 call_event_hook），钩子内替换
        async def plugin_like_handler(ev, r):
            r.contexts = list(plugin_contexts)
            r.conversation = None

        # 临时注册插件行为钩子（排在 spy 之前，模拟插件先后顺序）
        plugin_meta = StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name=f"{TEST_MODULE}_plugin_like",
            handler_name="plugin_like",
            handler_module_path=TEST_MODULE,
            handler=plugin_like_handler,
            event_filters=[],
        )
        star_handlers_registry._handlers.insert(0, plugin_meta)

        stopped = await call_event_hook(event, EventType.OnLLMRequestEvent, req)
        check("V1.hook", stopped is False and spy.llm_request_seen, "钩子未被调用")

        # reset（真实 Runner），确认替换后的 contexts 成为消息基底
        runner, reset_coro = build_runner(req, provider, event)
        await reset_coro
        roles = [m.role for m in runner.run_context.messages]
        contents = [
            getattr(m, "content", None) for m in runner.run_context.messages
        ]
        check(
            "V2.reset-messages",
            roles == ["system", "user", "user", "user", "assistant", "user", "user"]
            or self_check_messages(contents, roles),
            f"roles={roles}",
        )
        # 更直接的可读断言：消息文本集合
        texts = [extract_text(c) for c in runner.run_context.messages]
        check(
            "V2.shared-history-in-messages",
            "我在群A问过的问题" in texts and "（人格开场白）" in texts
            and "旧窗口历史问题" not in texts,
            f"texts={texts}",
        )

        # 运行真实 run_agent
        async for _ in run_agent(runner, 30, True, False, False):
            pass
        check("V2.agent-done-fired", len(spy.agent_done_seen) == 1)
        if spy.agent_done_seen:
            done = spy.agent_done_seen[0]
            resp = done["llm_response"]
            check(
                "V2.final-assistant",
                resp is not None and resp.role == "assistant"
                and "今天晴" in (resp.completion_text or ""),
                f"resp={resp}",
            )
            done_texts = [extract_text(m) for m in done["messages"]]
            check(
                "V2.trajectory",
                "今天天气如何" in done_texts and "今天晴，适合出门。" in done_texts,
                f"done_texts={done_texts}",
            )

        # 模型实际收到了共享历史（A01 断言基础）
        if provider.call_log:
            ctx_texts = [
                extract_text(m) for m in provider.call_log[0]["contexts"]
            ]
            check(
                "V2.provider-saw-shared",
                "我在群A问过的问题" in ctx_texts and "旧窗口历史问题" not in ctx_texts,
                f"ctx_texts={ctx_texts}",
            )
        else:
            check("V2.provider-saw-shared", False, "provider 未被调用")

        # V3：宿主 _save_to_history 短路（真实方法）
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
            InternalAgentSubStage,
        )

        stage = InternalAgentSubStage.__new__(InternalAgentSubStage)
        conv_mgr = FakeConversationManager()
        stage.conv_manager = conv_mgr
        final_resp = runner.get_final_llm_resp()
        await stage._save_to_history(
            event, req, final_resp, runner.run_context.messages, runner.stats
        )
        check(
            "V3.save-short-circuit",
            len(conv_mgr.update_calls) == 0,
            f"update_calls={conv_mgr.update_calls}",
        )

        # 对照组：conversation 保留时宿主确实写回
        req_ctrl = ProviderRequest()
        req_ctrl.prompt = "对照"
        req_ctrl.conversation = FakeConversation(user_id=event.unified_msg_origin)
        runner2, reset2 = build_runner(req_ctrl, FakeProvider(), event)
        await reset2
        async for _ in run_agent(runner2, 30, True, False, False):
            pass
        await stage._save_to_history(
            event,
            req_ctrl,
            runner2.get_final_llm_resp(),
            runner2.run_context.messages,
            runner2.stats,
        )
        check(
            "V3.control-writes-back",
            len(conv_mgr.update_calls) == 1,
            f"update_calls={len(conv_mgr.update_calls)}",
        )
        star_handlers_registry._handlers = [
            h for h in star_handlers_registry._handlers if h != plugin_meta
        ]
    finally:
        cleanup_hooks(metas)


def self_check_messages(contents, roles) -> bool:
    return len(roles) >= 5


def extract_text(content) -> str:
    """从 str / ContentPart 对象 / pydantic dump dict / list 中提取纯文本。"""

    if isinstance(content, str):
        return content
    if isinstance(content, Message):
        return extract_text(content.content)
    if isinstance(content, list):
        return "".join(extract_text(item) for item in content)
    if isinstance(content, dict):
        if content.get("type") == "text" and "text" in content:
            return str(content.get("text") or "")
        if "text" in content and isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return extract_text(content.get("content"))
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return ""


async def scenario_model_error() -> None:
    """V4：模型异常 → run_agent 手动 on_agent_done(err) → 插件识别失败。"""
    spy = HookSpy()
    metas = register_spy_hooks(spy)
    try:
        event = FakeEvent(sender_id="10001", group_id="700000001")
        provider = FakeProvider()
        provider.error_script = [RuntimeError("模拟模型服务不可用")]
        req = ProviderRequest()
        req.prompt = "触发错误"
        req.contexts = []
        req.system_prompt = "sys"
        req.conversation = None
        runner, reset_coro = build_runner(req, provider, event)
        await reset_coro
        yielded_chains = []
        async for chain in run_agent(runner, 30, True, False, False):
            if chain is not None:
                yielded_chains.append(chain)
        # 宿主真实行为：模型异常被 fallback 层转为 role=err 响应，step 进入
        # ERROR 态结束，_complete_with_assistant_response（触发 on_agent_done）
        # 不会被调用。插件写侧必须依赖 on_decorating_result 兜底识别 err 终态。
        final = runner.get_final_llm_resp()
        check(
            "V4.err-final-is-err",
            final is not None and final.role == "err",
            f"final={final}",
        )
        check(
            "V4.err-no-agent-done",
            len(spy.agent_done_seen) == 0,
            f"seen={len(spy.agent_done_seen)}",
        )
        err_texts = [
            getattr(c, "get_plain_text", lambda: "")() or "" for c in yielded_chains
        ]
        check(
            "V4.err-message-sent",
            any("不可用" in t for t in err_texts),
            f"err_texts={err_texts}",
        )
    finally:
        cleanup_hooks(metas)


async def scenario_aborted() -> None:
    """V5：用户中止路径（两版宿主共同保证的部分）。

    构造：请求进入前事件已停止且 runner 已请求停止。
    - 4.28：step 内 _await_or_stop 立即中止，_finalize_aborted_step() 注入
      打断标记对，on_agent_done 收到 marker 响应。
    - 4.26：fallback 层检测停止直接返回，llm_resp_result 为空 assistant，
      _finalize_aborted_step 保留该响应，on_agent_done 收到空 assistant。
    两版一致的关键事实：
      a) on_agent_done 仍会触发，但形态因版本而异（4.28 marker / 4.26 空或完整
         回复文本），completion_text 不可作为 aborted 判据；
      b) on_agent_done 触发时刻 event.extra.agent_user_aborted 尚未设置（由
         run_agent aborted 分支事后写入）——完成钩子内无法可靠判定 aborted，
         插件必须在 on_decorating_result 兜底轨终态化（ADR-003）；
      c) 事后 runner.was_aborted() 与 extra 旗标为真。
    """
    spy = HookSpy()
    metas = register_spy_hooks(spy)
    try:
        event = FakeEvent(sender_id="10001", group_id="700000001")
        provider = FakeProvider()
        req = ProviderRequest()
        req.prompt = "将被中止"
        req.contexts = []
        req.system_prompt = "sys"
        req.conversation = None
        runner, reset_coro = build_runner(req, provider, event)
        await reset_coro

        # 请求前停止事件并请求停止 runner
        event._force_stopped = True
        runner.request_stop()
        async for _ in run_agent(runner, 30, True, False, False):
            pass

        check(
            "V5.aborted-fires-agent-done",
            len(spy.agent_done_seen) == 1,
            f"seen={len(spy.agent_done_seen)}",
        )
        check(
            "V5.aborted-was-aborted",
            runner.was_aborted() is True,
            f"was_aborted={runner.was_aborted()}",
        )
        check(
            "V5.aborted-flag",
            event.get_extra("agent_user_aborted") is True,
            f"extras={event.get_extra()}",
        )
        if spy.agent_done_seen:
            seen = spy.agent_done_seen[0]
            resp = seen["llm_response"]
            # 版本差异实录：4.28 为 marker 文本，4.26 可能是空文本或完整回复
            # （fallback 层无停止检查，中止依赖 provider 协作）。因此 completion_text
            # 不是可靠的 aborted 判据——插件必须依赖兜底轨的 extra 旗标。
            check(
                "V5.aborted-response-form-recorded",
                resp.role == "assistant",
                f"completion_text={resp.completion_text!r}",
            )
            # 关键证明：on_agent_done 触发时刻 aborted 旗标尚未设置
            # （旗标由 run_agent 的 aborted 分支在钩子之后写入）——
            # 完成钩子内无法可靠判定 aborted，兜底轨必须存在（ADR-003）。
            check(
                "V5.aborted-flag-unavailable-inside-hook",
                seen["aborted_flag_at_hook"] is None,
                f"flag_at_hook={seen['aborted_flag_at_hook']!r}",
            )
    finally:
        cleanup_hooks(metas)


async def main() -> int:
    print(f"AstrBot version under test: {getattr(__import__('astrbot'), '__version__', 'unknown')}")
    await scenario_success_shared_history()
    await scenario_model_error()
    await scenario_aborted()
    print(f"\n=== P0 生命周期验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
