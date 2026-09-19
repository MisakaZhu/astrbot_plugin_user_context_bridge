"""T1 worker：真实 PersonaManager/ConversationManager + 宿主新会话路径。

子进程运行（ASTRBOT_ROOT 隔离 db）。真实 API 语义，合成配置输入按 UMO
返回不同 default_personality（模拟两窗口不同默认人格）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT_ARG = Path(sys.argv[1]).resolve()
(ROOT_ARG / "data" / "config").mkdir(parents=True, exist_ok=True)
os.environ["ASTRBOT_ROOT"] = str(ROOT_ARG)
sys.path.insert(0, r"D:\第三方插件完善\astrbot_plugin_user_context_bridge")

from astrbot.core import db_helper  # noqa: E402
from astrbot.core.astr_main_agent import _get_session_conv  # noqa: E402
from astrbot.core.conversation_mgr import ConversationManager  # noqa: E402
from astrbot.core.persona_mgr import PersonaManager  # noqa: E402
from astrbot.core.pipeline.context_utils import call_event_hook  # noqa: E402
from astrbot.core.provider.entities import ProviderRequest  # noqa: E402
from astrbot.core.star.star_handler import EventType  # noqa: E402

from tests.fakes import FakeEvent, FakeProvider  # noqa: E402
from tests.p3_bridge_flow_check import cleanup, register_bridge  # noqa: E402
from tests.harness import make_bridge_stack  # noqa: E402


async def main() -> int:
    await db_helper.initialize()
    a = FakeEvent(
        sender_id="10001", group_id="700000001", message_str="PERSONA-A-CONFIDENTIAL-Q"
    )
    b = FakeEvent(
        sender_id="10001", group_id="700000002", message_str="PERSONA-B-QUESTION"
    )

    def config_for(umo=None):
        persona = "persona_b" if str(umo) == b.unified_msg_origin else "persona_a"
        return {
            "provider_settings": {
                "default_personality": persona,
                "computer_use_runtime": "none",
            },
            "agent_runner": {
                "runner_type": "local",
                "config": {"persona": {"persona_id": persona}},
            },
        }

    acm = SimpleNamespace(default_conf=config_for(), get_conf=config_for)
    pm = PersonaManager(db_helper, acm)
    await pm.initialize()
    await pm.create_persona(
        "persona_a", "PERSONA-A-SYSTEM",
        ["A-BEGIN-Q", "A-BEGIN-A"], tools=["n04_tool_a"], skills=[],
    )
    await pm.create_persona(
        "persona_b", "PERSONA-B-SYSTEM",
        ["B-BEGIN-Q", "B-BEGIN-A"], tools=["n04_tool_b"], skills=[],
    )
    cm = ConversationManager(db_helper)

    def provider_settings_for(umo):
        return config_for(umo)["provider_settings"]

    bridge, resolver, membership, ledger = make_bridge_stack(
        str(ROOT_ARG),
        persona_manager_getter=lambda: pm,
        provider_settings_getter=provider_settings_for,
    )
    metas = register_bridge(bridge)
    observed: dict = {}

    async def drive(ev, label, answer):
        from tests.harness import drive_pipeline

        req = ProviderRequest()
        req.prompt = ev.message_str
        req.contexts = []
        req.system_prompt = "PERSONA-SYSTEM"
        req.conversation = await _get_session_conv(
            ev, SimpleNamespace(
                persona_manager=pm, conversation_manager=cm,
                get_config=lambda umo=None, **kw: config_for(umo),
            )
        )
        observed[label + "_new_conv_persona"] = req.conversation.persona_id
        # 插件身份解析（同 provider_settings）
        from uctx_bridge.identity import (
            PersonaResolutionError,
            resolve_persona_scope,
        )

        observed[label + "_scope"] = await resolve_persona_scope(
            pm, ev, req.conversation,
            provider_settings=provider_settings_for(ev.unified_msg_origin),
        )
        result = await drive_pipeline(bridge, ev, FakeProvider([answer]), prompt=ev.message_str)
        return result

    try:
        await drive(a, "A", "PERSONA-A-PRIVATE-ANSWER")
        await drive(b, "B", "PERSONA-B-ANSWER")
        from tests.r_rework_check import identity_of

        b_key = identity_of("10001", persona=observed.get("B_scope") or "x")
        hist_b = json.dumps(
            ledger.load_history(identity_of("10001", observed["B_scope"])),
            ensure_ascii=False,
        )
        observed["b_received_a_private"] = (
            "PERSONA-A-CONFIDENTIAL-Q" in hist_b
            and "PERSONA-A-PRIVATE-ANSWER" in hist_b
        )
        observed["a_scope"] = observed.get("A_scope")
        observed["b_scope"] = observed.get("B_scope")
        # 开场白保留：B 的下一轮 contexts 应含 B 人格开场白
        p_next = FakeProvider(["NEXT"])
        ev_next = FakeEvent(
            sender_id="10001", group_id="700000002", message_str="B-SECOND"
        )
        from tests.harness import drive_pipeline

        await drive_pipeline(bridge, ev_next, p_next, prompt="B-SECOND")
        ctx = json.dumps(
            p_next.call_log[0]["contexts"], ensure_ascii=False
        )
        observed["b_begin_dialog"] = "B-BEGIN-Q" in ctx and "A-BEGIN-Q" not in ctx

        # 显式会话人格：A 窗口选中 persona_b 后身份跟随
        conv_a = await cm.get_conversation(
            a.unified_msg_origin,
            await cm.get_curr_conversation_id(a.unified_msg_origin),
        )
        conv_a.persona_id = "persona_b"
        await cm.update_conversation(
            a.unified_msg_origin,
            await cm.get_curr_conversation_id(a.unified_msg_origin),
            persona_id="persona_b",
        )
        req_exp = ProviderRequest()
        req_exp.prompt = "explicit-check"
        req_exp.contexts = []
        req_exp.conversation = conv_a
        captured = await bridge.handle_llm_request(
            FakeEvent(sender_id="10001", group_id="700000001", message_str="explicit-check"),
            req_exp,
        )
        with sqlite3.connect(ledger._db_path) as db:
            keys = [
                r[0] for r in db.execute("SELECT DISTINCT identity_key FROM turns").fetchall()
            ]
        observed["explicit_scope"] = (
            [k for k in keys if k.endswith("\x1f10001")][-1].split("\x1f")[2]
            if keys else None
        )

        # 解析失败受控：manager 抛错 → 本轮不接管、不折叠 __default__ 身份
        class BrokenManager:
            async def resolve_selected_persona(self, **kw):
                raise RuntimeError("persona backend down")

        bridge._get_persona_manager = lambda: BrokenManager()
        ev_fail = FakeEvent(
            sender_id="10001", group_id="700000001", message_str="resolution-fail"
        )
        # 夹具注意：失败场景结束后必须恢复真实 manager（N04 场景依赖）
        req_fail = ProviderRequest()
        req_fail.prompt = "resolution-fail"
        req_fail.contexts = []
        req_fail.conversation = None
        observed["failure_not_captured"] = (
            await bridge.handle_llm_request(ev_fail, req_fail) is False
        )
        observed["failure_key_absent"] = (
            "__default__" not in json.dumps(
                [r[0] for r in sqlite3.connect(ledger._db_path).execute(
                    "SELECT DISTINCT identity_key FROM turns"
                ).fetchall()]
            )
        )
        # ---- N04（Y3）：user 模式真实 PersonaManager/ConversationManager/
        # 宿主请求装配 → 注册请求钩子 → Runner/假模型实际调用 → 真实终态
        # 钩子（on_agent_done 提交 completed）→ 下一窗口请求。
        # 群A(persona_a) → 群B(persona_b) → 私聊(persona_c) 链式继承。
        # 先受控收尾 explicit-check 场景遗留的挂起轮（其终态本就在
        # shutdown 时落 interrupted），使 N04 从 0 pending 起步。
        bridge.finalize_pending_as_interrupted()
        resolver.set_history_scope("user")
        # 恢复真实人格管理器（T1 失败场景曾替换为 BrokenManager）
        bridge._get_persona_manager = lambda: pm
        await pm.create_persona(
            "persona_u", "PERSONA-U-SYSTEM",
            ["U-BEGIN-Q", "U-BEGIN-A"], tools=[], skills=[],
        )
        # Z3a：真实 FunctionTool/ToolSet + 宿主人格工具选择链——三个人格
        # 各配专属工具，工具经 _ensure_persona_and_skills 的 ToolSet 路径
        # 装配，最终模型实参按 FakeProvider._tool_repr 断言。
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        async def _noop_tool_handler(*args, **kwargs):
            return None

        def _make_tool(name: str) -> FunctionTool:
            return FunctionTool(
                name=name,
                description=f"{name} for n04",
                parameters={"type": "object", "properties": {}},
                handler=_noop_tool_handler,
                active=True,
            )

        persona_tools = {
            "persona_a": "n04_tool_a",
            "persona_b": "n04_tool_b",
            "persona_c": "n04_tool_c",
        }
        all_tools = ToolSet()
        for _pn, _tn in persona_tools.items():
            all_tools.add_tool(_make_tool(_tn))

        class _N04ToolManager:
            """最小宿主工具管理器：真实 ToolSet 语义（get_full_tool_set/
            get_func），供 _ensure_persona_and_skills 的人格工具选择。"""

            def get_full_tool_set(self) -> ToolSet:
                return all_tools

            def get_func(self, name: str):
                return all_tools.get_tool(name)

            def get_builtin_tool(self, t):
                return None

        tool_mgr = _N04ToolManager()
        await pm.create_persona(
            "persona_c", "PERSONA-C-SYSTEM",
            ["C-BEGIN-Q", "C-BEGIN-A"],
            tools=[persona_tools["persona_c"]], skills=[],
        )
        dyn_marker = "DYNAMIC-INJECTION-MARKER"

        # Z3a：动态注入注册为**真实 OnLLMRequestEvent 钩子**（等价另一
        # 插件在请求阶段的注入），不再预先手拼 system_prompt。
        from astrbot.core.star.star_handler import (
            StarHandlerMetadata,
            star_handlers_registry,
        )
        from astrbot.core.star.star import StarMetadata, star_map

        dyn_module = "uctx_t1_dyn_inject"
        star_map[dyn_module] = StarMetadata(
            name=dyn_module, activated=True)

        async def _dyn_inject(ev, req):
            req.system_prompt = (req.system_prompt or "") + chr(10) + dyn_marker

        dyn_meta = StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name=dyn_module + "_inject",
            handler_name="inject",
            handler_module_path=dyn_module,
            handler=_dyn_inject,
            event_filters=[],
            extras_configs={"priority": 5},
        )
        star_handlers_registry._handlers.append(dyn_meta)
        star_handlers_registry.star_handlers_map[dyn_meta.handler_full_name] = (
            dyn_meta)

        async def _drive_real_chain(ev, provider, req) -> dict:
            """宿主装配后的同一 req 走完整宿主链：注册请求钩子 →
            ToolLoopAgentRunner/假模型 → 真实终态钩子 → 装饰与投递。"""
            from copy import deepcopy

            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )
            from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
            from astrbot.core.astr_agent_run_util import run_agent
            from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
            from astrbot.core.config.default import DEFAULT_CONFIG
            from astrbot.core.pipeline.result_decorate.stage import (
                ResultDecorateStage,
            )
            from astrbot.core.pipeline.scheduler import PipelineScheduler

            stopped = await call_event_hook(
                ev, EventType.OnLLMRequestEvent, req
            )
            info = {"hook_stopped": bool(stopped or ev.is_stopped())}
            if info["hook_stopped"]:
                info["model_calls"] = 0
                return info
            runner = ToolLoopAgentRunner()
            await runner.reset(
                provider=provider,
                request=req,
                run_context=ContextWrapper(context=SimpleNamespace(event=ev)),
                tool_executor=FunctionToolExecutor(),
                agent_hooks=MAIN_AGENT_HOOKS,
                streaming=False,
            )

            class AgentStage:
                async def process(self, ev_inner):
                    async for _ in run_agent(runner, 30, True, False, False):
                        yield

            class Transport:
                async def process(self, ev_inner):
                    result = ev_inner.get_result()
                    if result is not None and getattr(result, "chain", None):
                        await ev_inner.send(result)
                        await call_event_hook(
                            ev_inner, EventType.OnAfterMessageSentEvent
                        )

            cfg = deepcopy(DEFAULT_CONFIG)
            cfg["t2i"] = False
            cfg["content_safety"]["also_use_in_response"] = False
            cfg["provider_tts_settings"]["enable"] = False
            cfg["platform_settings"]["reply_with_mention"] = False
            cfg["platform_settings"]["reply_with_quote"] = False
            cfg["platform_settings"]["segmented_reply"]["enable"] = False
            stage_ctx = SimpleNamespace(
                astrbot_config=cfg,
                plugin_manager=SimpleNamespace(
                    context=SimpleNamespace(
                        get_using_tts_provider_async=AsyncMock(return_value=None),
                        get_using_tts_provider=lambda umo: None,
                    )
                ),
            )
            decorator = ResultDecorateStage()
            await decorator.initialize(stage_ctx)
            scheduler = PipelineScheduler.__new__(PipelineScheduler)
            scheduler.ctx = stage_ctx
            scheduler.stages = [AgentStage(), decorator, Transport()]
            try:
                await scheduler._process_stages(ev)
            except BaseException as exc:  # noqa: BLE001 - 留诊断证据
                info["pipeline_error"] = (
                    f"{type(exc).__name__}: {exc}"[:300]
                )
            info["model_calls"] = len(provider.call_log)
            return info

        async def drive_user_window(label, answer, persona_default,
                                    private=False):
            window_group = "" if private else f"70000000{label}"
            ev = FakeEvent(
                sender_id="10001", group_id=window_group,
                message_str="USER-MODE-" + label)
            req = ProviderRequest()
            req.prompt = ev.message_str
            req.contexts = []
            req.system_prompt = ""
            req.conversation = await _get_session_conv(
                ev, SimpleNamespace(
                    persona_manager=pm, conversation_manager=cm,
                    get_config=lambda umo=None, **kw: config_for(umo),
                )
            )
            # 会话人格指向当前窗口人格（真实 ConversationManager 路径）
            if req.conversation is not None:
                await cm.update_conversation(
                    ev.unified_msg_origin,
                    await cm.get_curr_conversation_id(ev.unified_msg_origin),
                    persona_id=persona_default,
                )
                req.conversation = await cm.get_conversation(
                    ev.unified_msg_origin,
                    await cm.get_curr_conversation_id(ev.unified_msg_origin),
                )
            # 宿主真实人格装配（_ensure_persona_and_skills）
            from astrbot.core.astr_main_agent import (
                _ensure_persona_and_skills,
            )

            host_ctx = SimpleNamespace(
                persona_manager=pm,
                get_llm_tool_manager=lambda: tool_mgr,
                get_using_provider=lambda umo: None,
                get_config=lambda: config_for(None),
                subagent_orchestrator=None,
            )
            cfg_ps = provider_settings_for(ev.unified_msg_origin)
            await _ensure_persona_and_skills(
                req, cfg_ps, host_ctx, ev)
            # 动态注入由注册的真实 OnLLMRequestEvent 钩子在链上追加；
            # 工具全部由宿主人格选择链装配（Z3a），不再手填。
            provider = FakeProvider([answer])
            info = await _drive_real_chain(ev, provider, req)
            return ev, req, provider, info

        _ev_a, req_a, prov_a, info_a = await drive_user_window(
            "1", "UA-ANSWER", "persona_a")
        _ev_b, req_b, prov_b, info_b = await drive_user_window(
            "2", "UB-ANSWER", "persona_b")
        _ev_c, req_c, prov_c, info_c = await drive_user_window(
            "3", "UC-ANSWER", "persona_c", private=True)

        with sqlite3.connect(ledger._db_path) as db:
            u_rows = db.execute(
                "SELECT identity_key, source_persona, status FROM turns"
                " WHERE user_message LIKE '%USER-MODE-%'"
                " ORDER BY id").fetchall()
        observed["n04_user_identity_keys"] = [r[0] for r in u_rows]
        observed["n04_single_u_key"] = (
            len({r[0] for r in u_rows}) == 1
            and "u:" in (u_rows[0][0] if u_rows else "")
        )
        observed["n04_source_personas"] = [r[1] for r in u_rows]
        # 真实终态：三窗全部经 on_agent_done 提交 completed；
        # 0 watchdog、0 pending、每窗恰好一次真实模型调用
        observed["n04_model_calls"] = [
            info_a.get("model_calls"), info_b.get("model_calls"),
            info_c.get("model_calls")]
        observed["n04_all_model_called"] = all(
            i.get("model_calls") == 1 and not i.get("hook_stopped")
            for i in (info_a, info_b, info_c)
        )
        observed["n04_all_completed"] = (
            len(u_rows) == 3 and all(r[2] == "completed" for r in u_rows))
        observed["n04_pending_zero"] = bridge.pending_count == 0
        observed["n04_watchdog_zero"] = bridge.stats.watchdog_failures == 0

        # 终模型实参（假模型 call_log）：Runner 会把 system_prompt 折入
        # contexts 首条 system 消息，故以"system_prompt + contexts 中
        # system 消息"的合并视图断言。当前窗口人格 system、当前开场白
        # 恰好一次、动态注入到达模型；不含旧人格 system/开场白；后继窗口
        # 含前一窗口完整问答；动态临时内容不落共享账本。
        def _model_view(log):
            parts = [log.get("system_prompt") or ""]
            for m in log["contexts"]:
                if isinstance(m, dict) and m.get("role") == "system":
                    parts.append(str(m.get("content") or ""))
            return "\n".join(parts)

        # 偶发无模型调用时不得崩溃——让字段断言失败并保留诊断信息
        log_a = prov_a.call_log[0] if prov_a.call_log else {}
        log_b = prov_b.call_log[0] if prov_b.call_log else {}
        log_c = prov_c.call_log[0] if prov_c.call_log else {}
        observed["n04_pipeline_errors"] = [
            info_a.get("pipeline_error"), info_b.get("pipeline_error"),
            info_c.get("pipeline_error")]
        view_b = _model_view(log_b)
        view_c = _model_view(log_c)
        ctx_b = json.dumps(log_b.get("contexts") or [],
                           ensure_ascii=False, default=str)
        ctx_c = json.dumps(log_c.get("contexts") or [],
                           ensure_ascii=False, default=str)
        observed["n04_system_current_persona"] = (
            "PERSONA-B-SYSTEM" in view_b
            and "PERSONA-A-SYSTEM" not in view_b
            and "PERSONA-C-SYSTEM" in view_c
            and "PERSONA-B-SYSTEM" not in view_c
        )
        observed["n04_begin_dialog_current"] = (
            ctx_b.count("B-BEGIN-Q") == 1 and ctx_b.count("B-BEGIN-A") == 1
            and "A-BEGIN-Q" not in ctx_b
            and ctx_c.count("C-BEGIN-Q") == 1
        )
        # Z3a：终模型实参（假模型 call_log）的工具集合——真实 ToolSet
        # 类型、当前人格专属工具存在、旧/他人格工具不串入、schema 可序列化
        ft_a = log_a.get("func_tool") or {}
        ft_b = log_b.get("func_tool") or {}
        ft_c = log_c.get("func_tool") or {}
        observed["n04_tool_preserved"] = (
            ft_b.get("type") == "ToolSet" and ft_c.get("type") == "ToolSet"
        )
        observed["n04_final_tool_names"] = {
            "A": ft_a.get("names"), "B": ft_b.get("names"),
            "C": ft_c.get("names")}
        observed["n04_final_tools_current_persona"] = (
            ft_a.get("names") == ["n04_tool_a"]
            and ft_b.get("names") == ["n04_tool_b"]
            and ft_c.get("names") == ["n04_tool_c"]
        )
        observed["n04_final_tools_no_cross_persona"] = (
            "n04_tool_a" not in (ft_b.get("names") or [])
            and "n04_tool_c" not in (ft_b.get("names") or [])
            and "n04_tool_b" not in (ft_c.get("names") or [])
            and "n04_tool_a" not in (ft_c.get("names") or [])
        )
        observed["n04_final_tool_schema_serializable"] = bool(
            ft_b.get("openai_schema")) and bool(ft_c.get("openai_schema"))
        observed["n04_dynamic_injection_preserved"] = (
            dyn_marker in view_b and dyn_marker in view_c
        )
        with sqlite3.connect(ledger._db_path) as db:
            blobs = db.execute(
                "SELECT IFNULL(user_message,'') || ',' ||"
                " IFNULL(trajectory,'') || ',' || IFNULL(reply_text,'')"
                " FROM turns WHERE identity_key LIKE '%"
                + chr(31) + "u:" + chr(31) + "%'"
            ).fetchall()
        observed["n04_dynamic_not_in_ledger"] = all(
            dyn_marker not in (r[0] or "") for r in blobs
        )
        observed["n04_cross_persona_chain"] = (
            "USER-MODE-1" in ctx_b and "UA-ANSWER" in ctx_b
            and "PERSONA-A-SYSTEM" not in view_b
            and "USER-MODE-2" in ctx_c and "UB-ANSWER" in ctx_c
        )
        observed["n04_debug_bctx"] = ctx_b[:300]

        print("@@RESULT@@" + json.dumps(observed, ensure_ascii=False))
    finally:
        try:
            star_handlers_registry._handlers.remove(dyn_meta)
            star_handlers_registry.star_handlers_map.pop(
                dyn_meta.handler_full_name, None)
            star_map.pop(dyn_module, None)
        except Exception:  # noqa: BLE001 - 清理尽力而为
            pass
        bridge.shutdown()
        cleanup(metas)
        ledger.close()
        await db_helper.engine.dispose()
    return 0


import asyncio  # noqa: E402

sys.exit(asyncio.run(main()))
