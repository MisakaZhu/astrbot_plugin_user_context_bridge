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
        ["A-BEGIN-Q", "A-BEGIN-A"], tools=[], skills=[],
    )
    await pm.create_persona(
        "persona_b", "PERSONA-B-SYSTEM",
        ["B-BEGIN-Q", "B-BEGIN-A"], tools=[], skills=[],
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
        # ---- N04（X6）：user 模式真实 PersonaManager/ConversationManager/
        # 宿主请求装配 —— 当前窗口人格 system/begin_dialogs 经宿主
        # _ensure_persona_and_skills 注入，工具/动态注入保留，跨人格共享
        from astrbot.core.star import Context as HostContext

        resolver.set_history_scope("user")
        # 恢复真实人格管理器（T1 失败场景曾替换为 BrokenManager）
        bridge._get_persona_manager = lambda: pm
        await pm.create_persona(
            "persona_u", "PERSONA-U-SYSTEM",
            ["U-BEGIN-Q", "U-BEGIN-A"], tools=[], skills=[],
        )
        tool_marker = {"type": "function", "function": {"name": "u_tool"}}
        dyn_marker = "DYNAMIC-INJECTION-MARKER"

        async def drive_user_window(window_group, answer, persona_default):
            ev = FakeEvent(
                sender_id="10001", group_id=window_group,
                message_str="USER-MODE-" + window_group)
            req = ProviderRequest()
            req.prompt = ev.message_str
            req.contexts = []
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
                get_llm_tool_manager=lambda: SimpleNamespace(
                    get_full_tool_set=lambda: {},
                    get_builtin_tool=lambda t: None,
                ),
                get_using_provider=lambda umo: None,
                get_config=lambda: config_for(None),
                subagent_orchestrator=None,
            )
            cfg_ps = provider_settings_for(ev.unified_msg_origin)
            await _ensure_persona_and_skills(
                req, cfg_ps, host_ctx, ev)
            # 工具/动态注入（宿主装配之后、插件接管之前）
            req.func_tool = [tool_marker]
            req.system_prompt = (req.system_prompt or "") + chr(10) + dyn_marker
            cap0 = ledger.stats_captured() if hasattr(ledger, "stats_captured") else None
            await bridge.handle_llm_request(ev, req)
            return req

        req_a = await drive_user_window("700000001", "UA-ANSWER", "persona_a")
        # 直接 handle 只登记 running 轮——模拟 on_agent_done 提交，B 才能在
        # 历史中读到 A（真实提交点见 bridge 终态机）
        ev_key_a = bridge.event_key_for(
            FakeEvent(sender_id="10001", group_id="700000001",
                      message_str="USER-MODE-700000001"))
        with sqlite3.connect(ledger._db_path) as conn:
            row = conn.execute(
                "SELECT event_key FROM turns WHERE status='running'"
                " ORDER BY id DESC LIMIT 1").fetchone()
        ek = row[0] if row else ev_key_a
        ledger.commit_turn(
            event_key=ek, status="completed",
            trajectory=[{"role": "assistant", "content": "UA-ANSWER"}],
            reply_text="UA-ANSWER")
        req_b = await drive_user_window("700000002", "UB-ANSWER", "persona_b")

        with sqlite3.connect(ledger._db_path) as db:
            all_rows = db.execute(
                "SELECT identity_key, user_message FROM turns ORDER BY id"
            ).fetchall()
        observed["n04_debug_rows"] = [
            (r[0][-20:], r[1][:60]) for r in all_rows]
        with sqlite3.connect(ledger._db_path) as db:
            u_rows = db.execute(
                "SELECT identity_key, source_persona, user_message FROM turns"
                " WHERE user_message LIKE '%USER-MODE%'"
                " ORDER BY id").fetchall()
        observed["n04_user_identity_keys"] = [
            r[0] for r in u_rows]
        observed["n04_single_u_key"] = (
            len({r[0] for r in u_rows}) == 1
            and "u:" in u_rows[0][0]
        )
        observed["n04_source_personas"] = [r[1] for r in u_rows]
        # 最终请求：当前窗口人格 system/begin_dialogs + 工具/动态注入保留
        ctx_b = json.dumps(req_b.call_log if hasattr(req_b, "call_log") else [],
                           ensure_ascii=False)
        observed["n04_system_current_persona"] = (
            "PERSONA-B-SYSTEM" in (req_b.system_prompt or "")
            and "PERSONA-A-SYSTEM" not in (req_b.system_prompt or "")
        )
        observed["n04_begin_dialog_current"] = (
            "B-BEGIN-Q" in json.dumps(req_b.contexts, ensure_ascii=False)
        )
        observed["n04_tool_preserved"] = (
            json.dumps(tool_marker, ensure_ascii=False)
            in json.dumps(getattr(req_b, "func_tool", []) or [],
                          ensure_ascii=False, default=str)
        )
        observed["n04_dynamic_injection_preserved"] = (
            dyn_marker in (req_b.system_prompt or "")
        )
        observed["n04_cross_persona_chain"] = (
            "USER-MODE-700000001" in json.dumps(req_b.contexts,
                                                ensure_ascii=False,
                                                default=str)
        )
        observed["n04_debug_bctx"] = json.dumps(
            req_b.contexts, ensure_ascii=False, default=str)[:400]

        print("@@RESULT@@" + json.dumps(observed, ensure_ascii=False))
    finally:
        bridge.shutdown()
        cleanup(metas)
        ledger.close()
        await db_helper.engine.dispose()
    return 0


import asyncio  # noqa: E402

sys.exit(asyncio.run(main()))
