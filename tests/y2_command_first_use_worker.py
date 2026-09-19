"""Y2：首次只执行命令的身份（off/on）也登记模式事实——真实
PluginManager + 真实 CommandService worker（子进程运行）。

由 tests/y2_command_first_use_check.py 以两版宿主解释器分别 spawn。
覆盖并区分三类入口：

  v1 升级（Phase 0）：迁移后的旧退出身份，加载对账登记当前模式、
    代次不变（升级兼容，不归档旧有效语义）；
  新身份首次命令（Phase 1/2/3）：全新安装/已有实例中新用户仅执行
    /uctx off（或 on）、零对话 → 命令入口登记当前模式/0；直接切另一
    模式 + 真实 reload → 恰好 +1（双方向）；重复命令幂等；
  已生效身份配置切换：切换只推进一次，命令不推进代次。

登记/保存边界失败（Phase 4）：受控文案不假报成功、退出不失、
重载对账补登记、重试不多推进。

网络边界为合成 Provider/事件。ASTRBOT_ROOT 指向临时实例根，进程
退出即销毁。输出单行 JSON 供父进程断言。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR_NAME = "astrbot_plugin_user_context_bridge"
SOURCE_FILES = [
    "main.py",
    "metadata.yaml",
    "requirements.txt",
    "_conf_schema.json",
    "uctx_bridge/__init__.py",
    "uctx_bridge/identity.py",
    "uctx_bridge/scope.py",
    "uctx_bridge/ledger.py",
    "uctx_bridge/bridge.py",
    "uctx_bridge/commands.py",
]
BASE_SEP = "\x1f"


def setup_instance_root(root: Path) -> None:
    plug = root / "data" / "plugins" / PLUGIN_DIR_NAME
    plug.mkdir(parents=True, exist_ok=True)
    (root / "data" / "config").mkdir(parents=True, exist_ok=True)
    for rel in SOURCE_FILES:
        dst = plug / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)


def write_config(root: Path, scope: str) -> None:
    cfg_path = root / "data" / "config" / f"{PLUGIN_DIR_NAME}_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "history_scope": scope,
                "shared_groups": ["810000001", "810000002", "810000003",
                    "810000005", "810000006", "810000102"],
                "include_private": True,
                "max_history_turns": 40,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def main() -> int:
    instance_root = Path(sys.argv[1])
    setup_instance_root(instance_root)
    os.environ["ASTRBOT_ROOT"] = str(instance_root)
    sys.path.insert(0, str(instance_root))
    sys.path.insert(0, str(REPO))

    from astrbot.core import AstrBotConfig
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    from tests.fakes import FakeConversation, FakeEvent, FakeProvider
    from tests.p3_bridge_flow_check import FakePersonaManager

    out: dict = {}

    class PerWindowPersona(FakePersonaManager):
        """按窗口返回固定人格：81000000N → 第 N 个人格名。"""

        _NAMES = {
            "810000001": "black",
            "810000002": "white",
            "810000003": "third",
            "810000005": "fifth",
            "810000006": "maid",
            "810000102": "grey",
        }

        async def resolve_selected_persona(self, **kw):
            umo = str(kw.get("umo") or "")
            scope = next(
                (n for g, n in self._NAMES.items() if g in umo), "fallback"
            )
            return (
                scope,
                {"name": scope, "_begin_dialogs_processed": []},
                None,
                False,
            )

    cfg_dict = {"platform_settings": {"unique_session": False}}
    conv_mgr = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        get_conversation=AsyncMock(return_value=None),
        update_conversation=AsyncMock(),
    )
    context = SimpleNamespace(
        persona_manager=PerWindowPersona(),
        conversation_manager=conv_mgr,
        get_config=lambda umo=None, **kw: cfg_dict,
        get_using_provider_async=AsyncMock(return_value=object()),
        get_using_provider=lambda umo: object(),
        get_registered_star=lambda name: star_map.get(
            f"data.plugins.{name}.main"
        ),
        get_using_tts_provider_async=AsyncMock(return_value=None),
        get_using_tts_provider=lambda umo: None,
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda t: None, get_full_tool_set=lambda: None
        ),
    )
    astrbot_cfg = AstrBotConfig(
        config_path=str(instance_root / "data" / "cmd_config.json")
    )
    pm = PluginManager(context, astrbot_cfg)

    def meta_of(sender: str, name: str):
        base = BASE_SEP.join(("aiocqhttp", "bot_001", sender))
        db_path = (
            instance_root / "data" / "plugin_data"
            / PLUGIN_DIR_NAME / "uctx_ledger.db"
        )
        if not db_path.exists():
            return None
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT value FROM meta WHERE identity_key=? AND name=?",
                (base, name),
            ).fetchone()
        return row[0] if row else None

    def gen_of(sender: str) -> int:
        v = meta_of(sender, "mode_generation")
        return int(v) if v is not None else 0

    def active_plugin():
        meta = star_map.get(f"data.plugins.{PLUGIN_DIR_NAME}.main")
        return meta.star_cls if meta else None

    async def reload_with(scope: str | None) -> object:
        if scope is not None:
            write_config(instance_root, scope)
        await pm.reload(PLUGIN_DIR_NAME)
        return active_plugin()

    async def drive(ev, provider, prompt: str) -> bool:
        """真实调度链一轮；返回是否真实完成（未接管/停止=False）。"""
        from copy import deepcopy

        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
        from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
        from astrbot.core.astr_agent_run_util import run_agent
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType

        req = ProviderRequest()
        req.prompt = prompt
        req.contexts = []
        req.system_prompt = "y2 system"
        req.conversation = FakeConversation(user_id=ev.unified_msg_origin)
        stopped = await call_event_hook(ev, EventType.OnLLMRequestEvent, req)
        if stopped or ev.is_stopped():
            return False
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
        await scheduler._process_stages(ev)
        resp = runner.get_final_llm_resp()
        return resp is not None and (resp.completion_text or "") != ""

    async def ask(obj, group, question, answer):
        """返回 (完成, 采集增量)。采集增量以 bridge.stats.captured 为准。"""
        ev = FakeEvent(sender_id=_sender_of(group), group_id=group,
                       message_str=question)
        provider = FakeProvider([answer])
        cap0 = obj._bridge.stats.captured
        completed = await drive(ev, provider, question)
        return completed, obj._bridge.stats.captured - cap0

    _GROUP_SENDER = {
        "810000001": "20001",
        "810000002": "20002",
        "810000003": "20003",
        "810000005": "20005",
        "810000006": "20006",
        "810000102": "20001",  # 20001 的另一人格窗口（grey）
    }

    def _sender_of(group: str) -> str:
        return _GROUP_SENDER[group]

    async def command(obj, sender, group, name):
        ev = FakeEvent(sender_id=sender, group_id=group, message_str=f"/uctx {name}")
        return await getattr(obj._commands, name)(ev)

    # -- Phase 0：v1 旧退出（legacy 原始人格键）预置 -------------------------
    membership_path = (
        instance_root / "data" / "plugin_data" / PLUGIN_DIR_NAME
        / "membership.json"
    )
    membership_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_key = BASE_SEP.join(("aiocqhttp", "bot_001", "maid", "20006"))
    membership_path.write_text(
        json.dumps(
            {
                "optout": [legacy_key],
                "base_protected": [],
                "persona_on": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # -- 加载（persona 模式）：迁移 + 对账 -----------------------------------
    write_config(instance_root, "persona")
    ok, _err = await pm.load(specified_dir_name=PLUGIN_DIR_NAME)
    out["load_ok"] = bool(ok)
    out["bound_handlers"] = len(
        star_handlers_registry.get_handlers_by_module_name(
            f"data.plugins.{PLUGIN_DIR_NAME}.main"
        )
    )
    plugin = active_plugin()

    # Phase 0：v1 升级身份——加载即登记 persona/0（升级兼容不推进），
    # 旧退出在迁移后继续生效
    out["v1_scope_after_load"] = meta_of("20006", "scope_mode")
    out["v1_gen_after_load"] = gen_of("20006")
    done, cap = await ask(plugin, "810000006", "V1-问", "V1-答")
    out["v1_turn_done"] = done
    out["v1_exit_blocks_capture"] = cap == 0
    off6 = await command(plugin, "20006", "810000006", "off")
    out["v1_off_idempotent_ok"] = "已退出" in off6 and "⚠️" not in off6
    out["v1_scope_after_cmd"] = meta_of("20006", "scope_mode")
    out["v1_gen_after_cmd"] = gen_of("20006")

    # -- Phase 1：persona 模式新用户 20001 仅 off（零对话）------------------
    off1 = await command(plugin, "20001", "810000001", "off")
    out["off1_text_ok"] = "已退出" in off1 and not off1.startswith("⚠️")
    out["A_scope_after_off"] = meta_of("20001", "scope_mode")
    out["A_gen_after_off"] = gen_of("20001")
    off1b = await command(plugin, "20001", "810000001", "off")
    out["off1_repeat_ok"] = "已退出" in off1b and "⚠️" not in off1b
    out["A_gen_after_repeat"] = gen_of("20001")
    done, cap = await ask(plugin, "810000001", "A1-问", "A1-答")
    out["A_turn_done"] = done
    out["A_exit_blocks_capture"] = cap == 0

    # 直接切 user + 真实 reload：恰好 +1（Y2 核心反例）
    plugin = await reload_with("user")
    out["A_scope_after_switch"] = meta_of("20001", "scope_mode")
    out["A_gen_after_switch"] = gen_of("20001")
    done, cap = await ask(plugin, "810000001", "A2-问", "A2-答")
    out["A_user_mode_exit_blocks"] = cap == 0
    plugin = await reload_with("persona")
    out["A_scope_back"] = meta_of("20001", "scope_mode")
    out["A_gen_back"] = gen_of("20001")
    done, cap = await ask(plugin, "810000102", "A3-问", "A3-答")
    out["A_future_persona_protected"] = cap == 0
    on1 = await command(plugin, "20001", "810000102", "on")
    out["on1_text_ok"] = ("已取消" in on1 or "恢复" in on1 or "加入" in on1) and (
        "⚠️" not in on1
    )
    out["A_scope_after_on"] = meta_of("20001", "scope_mode")
    out["A_gen_after_on"] = gen_of("20001")
    done, cap = await ask(plugin, "810000102", "A4-问", "A4-答")
    out["A_captured_after_on"] = cap

    # -- Phase 2：user 模式新用户 20002 仅 off（零对话）---------------------
    plugin = await reload_with("user")
    off2 = await command(plugin, "20002", "810000002", "off")
    out["off2_text_ok"] = "已退出" in off2 and not off2.startswith("⚠️")
    out["B_scope_after_off"] = meta_of("20002", "scope_mode")
    out["B_gen_after_off"] = gen_of("20002")
    done, cap = await ask(plugin, "810000002", "B1-问", "B1-答")
    out["B_exit_blocks_capture"] = cap == 0
    plugin = await reload_with("persona")
    out["B_scope_after_switch"] = meta_of("20002", "scope_mode")
    out["B_gen_after_switch"] = gen_of("20002")

    # -- Phase 3：首次 on（无 off）也登记 + 重复命令幂等 --------------------
    on3 = await command(plugin, "20003", "810000003", "on")
    out["on3_text_ok"] = "⚠️" not in on3
    out["C_scope_after_on"] = meta_of("20003", "scope_mode")
    out["C_gen_after_on"] = gen_of("20003")
    on3b = await command(plugin, "20003", "810000003", "on")
    out["on3_repeat_ok"] = "⚠️" not in on3b
    out["C_gen_after_repeat"] = gen_of("20003")
    done, cap = await ask(plugin, "810000003", "C1-问", "C1-答")
    out["C_captured"] = cap
    plugin = await reload_with("user")
    out["C_scope_after_switch"] = meta_of("20003", "scope_mode")
    out["C_gen_after_switch"] = gen_of("20003")
    done, cap = await ask(plugin, "810000003", "C2-问", "C2-答")
    out["C_captured_user_mode"] = cap

    # -- Phase 4：登记边界失败 → 受控文案/退出不失/重载补登记/重试不推进 ----
    # 注意：实例目录与 REPO 各有一份 uctx_bridge，异常类必须取自插件自身
    # 模块，保证与 commands.py 捕获的是同一个类。
    plugin_ledger_error = sys.modules[
        type(plugin._commands).__module__
    ].LedgerError
    ledger_obj = plugin._ledger
    original_apply = ledger_obj.apply_scope_mode

    def _raising_apply(*a, **kw):
        raise plugin_ledger_error("注入：模式登记写失败")

    ledger_obj.apply_scope_mode = _raising_apply  # type: ignore[method-assign]
    off5 = await command(plugin, "20005", "810000005", "off")
    out["off5_controlled_text"] = (
        "⚠️" in off5 and "重试" in off5 and "已退出" not in off5
    )
    out["E_scope_after_failed_reg"] = meta_of("20005", "scope_mode")
    del ledger_obj.apply_scope_mode  # 恢复真实方法
    assert ledger_obj.apply_scope_mode.__func__ is original_apply.__func__
    done, cap = await ask(plugin, "810000005", "E1-问", "E1-答")
    out["E_exit_survives_failed_reg"] = cap == 0

    plugin = await reload_with("user")  # 重载（重启模拟）：对账补登记
    out["E_scope_after_reload"] = meta_of("20005", "scope_mode")
    out["E_gen_after_reload"] = gen_of("20005")
    done, cap = await ask(plugin, "810000005", "E2-问", "E2-答")
    out["E_exit_survives_reload"] = cap == 0
    off5b = await command(plugin, "20005", "810000005", "off")
    out["off5_retry_ok"] = "已退出" in off5b and "⚠️" not in off5b
    out["E_gen_after_retry"] = gen_of("20005")
    plugin = await reload_with("persona")
    out["E_scope_final"] = meta_of("20005", "scope_mode")
    out["E_gen_final"] = gen_of("20005")
    done, cap = await ask(plugin, "810000005", "E3-问", "E3-答")
    out["E_user_off_protects_persona"] = cap == 0

    # -- Phase 5：已生效身份的配置切换只推进一次 ----------------------------
    out["A_gen_final"] = gen_of("20001")
    plugin = await reload_with("user")
    out["A_gen_final_after_switch"] = gen_of("20001")
    plugin = await reload_with("user")  # 同模式 reload 不推进
    out["A_gen_same_mode_reload"] = gen_of("20001")

    print("@@RESULT@@" + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
