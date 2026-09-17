"""S6：真实 PluginManager 隔离实例生命周期 worker（子进程运行）。

由 tests/s_rework_check.py 以两版宿主解释器分别 spawn。流程：

  安装（源码落位 = ZIP 内容）→ 真实 PluginManager.load → 默认关闭运行 →
  写启用配置 → reload → 真实钩子驱动一轮（群A→群B 接续）→ 活动轮挂起时
  uninstall（terminate 释放）→ 目录与注册表清理核验。

网络边界为合成 Provider/事件；不连真实 QQ/模型。ASTRBOT_ROOT 指向临时
实例根，进程退出即销毁。输出单行 JSON 供父进程断言。
"""

from __future__ import annotations

import asyncio
import importlib
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


def setup_instance_root(root: Path) -> None:
    plug = root / "data" / "plugins" / PLUGIN_DIR_NAME
    plug.mkdir(parents=True, exist_ok=True)
    # 4.26 的 AstrBotConfig 保存不自动创建 data/config（FileNotFoundError）
    (root / "data" / "config").mkdir(parents=True, exist_ok=True)
    for rel in SOURCE_FILES:
        src = REPO / rel
        dst = plug / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


async def main() -> int:
    instance_root = Path(sys.argv[1])
    setup_instance_root(instance_root)
    os.environ["ASTRBOT_ROOT"] = str(instance_root)
    sys.path.insert(0, str(instance_root))
    sys.path.insert(0, str(REPO))  # tests.fakes / tests.harness 复用

    out: dict = {}

    # -- 宿主导入（ASTRBOT_ROOT 已定） --------------------------------------
    from astrbot.core import AstrBotConfig, logger
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.star.star_handler import EventType

    from tests.fakes import FakeConversation, FakeEvent, FakeProvider
    from tests.p3_bridge_flow_check import FakePersonaManager

    # -- 隔离宿主 context（真实插件方法所需的最小面） -----------------------
    cfg_dict = {"platform_settings": {"unique_session": False}}

    conv_mgr = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        get_conversation=AsyncMock(return_value=None),
        update_conversation=AsyncMock(),
    )
    context = SimpleNamespace(
        persona_manager=FakePersonaManager(),
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

    # -- 1) 真实加载 --------------------------------------------------------
    ok, err = await pm.load(specified_dir_name=PLUGIN_DIR_NAME)
    metadata = star_map.get(f"data.plugins.{PLUGIN_DIR_NAME}.main")
    out["load_ok"] = bool(ok and metadata and metadata.star_cls is not None)
    out["plugin_activated"] = bool(metadata and metadata.activated)
    bound_handlers = (
        star_handlers_registry.get_handlers_by_module_name(
            f"data.plugins.{PLUGIN_DIR_NAME}.main"
        )
        if metadata
        else []
    )
    out["bound_handlers"] = len(bound_handlers)
    plugin_obj = metadata.star_cls if metadata else None

    # -- 2) 默认关闭：驱动一轮不得接管 ---------------------------------------
    from astrbot.core.provider.entities import ProviderRequest

    ev0 = FakeEvent(sender_id="10001", group_id="700000001", message_str="默认关闭轮")
    req0 = ProviderRequest()
    req0.prompt = "默认关闭轮"
    req0.contexts = []
    req0.system_prompt = "sys"
    req0.conversation = FakeConversation(user_id=ev0.unified_msg_origin)
    await call_event_hook(ev0, EventType.OnLLMRequestEvent, req0)
    out["default_off_not_captured"] = req0.conversation is not None
    out["default_off_bridge_captured"] = (
        plugin_obj._bridge.stats.captured if plugin_obj and plugin_obj._bridge else None
    )

    # -- 3) 写启用配置 + reload ---------------------------------------------
    cfg_path = instance_root / "data" / "config" / f"{PLUGIN_DIR_NAME}_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "shared_groups": ["700000001", "700000002"],
                "include_private": True,
                "max_history_turns": 40,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    await pm.reload(PLUGIN_DIR_NAME)
    metadata2 = star_map.get(f"data.plugins.{PLUGIN_DIR_NAME}.main")
    plugin_obj = metadata2.star_cls if metadata2 else None
    out["reloaded"] = plugin_obj is not None and metadata2.activated
    out["enabled_after_reload"] = bool(
        plugin_obj and plugin_obj._resolver.config.enabled
    )

    # -- 4) 真实钩子驱动：群A → 群B 接续（真实注册的 bound handlers） --------
    p1 = FakeProvider(["S6-FIRST-ANSWER"])
    ev1 = FakeEvent(sender_id="10001", group_id="700000001", message_str="S6问题一")
    res1 = await _drive_with_real_hooks(ev1, p1, "S6问题一")
    out["first_turn_completed"] = res1

    p2 = FakeProvider(["S6-SECOND-ANSWER"])
    ev2 = FakeEvent(sender_id="10001", group_id="700000002", message_str="S6问题二")
    await _drive_with_real_hooks(ev2, p2, "S6问题二")
    ctx2 = json.dumps(p2.call_log[0]["contexts"], ensure_ascii=False)
    out["second_sees_first"] = (
        "S6问题一" in ctx2 and "S6-FIRST-ANSWER" in ctx2
    )

    ledger = plugin_obj._ledger
    with sqlite3.connect(ledger._db_path) as db:
        n = db.execute("SELECT COUNT(*) FROM turns WHERE status='completed'").fetchone()[0]
    out["ledger_completed"] = n

    # -- 5) 活动轮挂起时卸载（terminate 释放排队/活动轮） --------------------
    entered, release = asyncio.Event(), asyncio.Event()

    class Hanging(FakeProvider):
        async def text_chat(self, **kwargs):
            self.call_log.append({"contexts": []})
            entered.set()
            await release.wait()
            return await super().text_chat(**kwargs)

    ev3 = FakeEvent(sender_id="10001", group_id="700000001", message_str="挂起轮")
    t3 = asyncio.create_task(_drive_with_real_hooks(ev3, Hanging(["LATE"]), "挂起轮"))
    await asyncio.wait_for(entered.wait(), 3)

    from astrbot.core.utils.active_event_registry import active_event_registry

    active_event_registry.register(ev3)
    await pm.uninstall_plugin(PLUGIN_DIR_NAME)
    release.set()
    try:
        await asyncio.wait_for(t3, 3)
    except Exception:
        pass
    with sqlite3.connect(ledger._db_path) as db:
        hanging = db.execute(
            "SELECT status FROM turns WHERE event_key LIKE '%' || ? || '%'",
            (ev3.message_obj.message_id,),
        ).fetchall()
    out["uninstall_interrupted_pending"] = bool(
        hanging and hanging[0][0] == "interrupted"
    )
    out["uninstall_removed_from_registry"] = (
        star_map.get(f"data.plugins.{PLUGIN_DIR_NAME}.main") is None
    )
    out["uninstall_dir_removed"] = not (
        instance_root / "data" / "plugins" / PLUGIN_DIR_NAME
    ).exists()

    print("@@RESULT@@" + json.dumps(out, ensure_ascii=False))
    return 0


async def _drive_with_real_hooks(event, provider, prompt: str) -> bool:
    """真实调度链，钩子全部来自 PluginManager 绑定注册的真实插件 handler。"""

    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
    from astrbot.core.astr_agent_run_util import run_agent
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.star_handler import EventType
    from copy import deepcopy
    from astrbot.core.config.default import DEFAULT_CONFIG
    from tests.fakes import FakeConversation

    req = ProviderRequest()
    req.prompt = prompt
    req.contexts = []
    req.system_prompt = "s6 system"
    req.conversation = FakeConversation(user_id=event.unified_msg_origin)
    await call_event_hook(event, EventType.OnLLMRequestEvent, req)

    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=req,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=MAIN_AGENT_HOOKS,
        streaming=False,
    )

    class AgentStage:
        async def process(self, ev):
            async for _ in run_agent(runner, 30, True, False, False):
                yield

    class Transport:
        async def process(self, ev):
            result = ev.get_result()
            if result is not None and getattr(result, "chain", None):
                await ev.send(result)
                await call_event_hook(ev, EventType.OnAfterMessageSentEvent)

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
    scheduler.stages = [AgentStage(), decorator, Transport()]
    await scheduler._process_stages(event)
    resp = runner.get_final_llm_resp()
    return resp is not None and (resp.completion_text or "") != ""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
