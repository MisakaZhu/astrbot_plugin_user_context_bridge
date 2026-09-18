"""T5/T6 worker：真实 Context + WakingCheckStage + StarRequestSubStage 命令分发。

子进程运行。真实宿主 Context（两版接口差异自然呈现——4.26 无
get_using_provider_async 时用同步接口）、真实 builtin commands 实例与
alter_cmd/改名/禁用/自定义过滤；插件经真实 import 装配。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(sys.argv[1]).resolve()
(ROOT / "data" / "config").mkdir(parents=True, exist_ok=True)
os.environ["ASTRBOT_ROOT"] = str(ROOT)

REPO = Path(r"D:\第三方插件完善\astrbot_plugin_user_context_bridge")
dest = ROOT / "data" / "plugins" / "astrbot_plugin_user_context_bridge"
dest.mkdir(parents=True, exist_ok=True)
for rel in ("main.py", "metadata.yaml", "requirements.txt", "_conf_schema.json"):
    shutil.copy2(REPO / rel, dest / rel)
shutil.copytree(
    REPO / "uctx_bridge", dest / "uctx_bridge", dirs_exist_ok=True,
    ignore=shutil.ignore_patterns("__pycache__"),
)
for pkg in ("data", "data/plugins", "data/plugins/astrbot_plugin_user_context_bridge"):
    (ROOT / Path(*pkg.split(".")) / "__init__.py").write_text("", encoding="utf-8")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))

import astrbot.builtin_stars.builtin_commands.main as native  # noqa: E402
import astrbot.builtin_stars.builtin_commands.commands.conversation as conversation  # noqa: E402
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star import Context  # noqa: E402
from astrbot.core.config.default import DEFAULT_CONFIG  # noqa: E402
from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage  # noqa: E402
from astrbot.core.pipeline.process_stage.stage import (  # noqa: E402
    ProcessStage,
)
from astrbot.core.pipeline.process_stage.method.star_request import (  # noqa: E402
    StarRequestSubStage,
)
import astrbot.core.star.session_plugin_manager as session_plugins  # noqa: E402
from astrbot.core.star.star import star_map  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402

from tests.fakes import FakeEvent, FakeProvider  # noqa: E402
from tests.p3_bridge_flow_check import FakePersonaManager  # noqa: E402
from tests.harness import make_bridge_stack  # noqa: E402
from tests.p3_bridge_flow_check import cleanup, register_bridge  # noqa: E402
from uctx_bridge.commands import CommandService  # noqa: E402

plugin_module = importlib.import_module(
    "data.plugins.astrbot_plugin_user_context_bridge.main"
)
# 清理未绑定注册（探针同款），保留模块供装配
stale = [
    h for h in list(star_handlers_registry._handlers)
    if h.handler_module_path == plugin_module.__name__
]
for h in stale:
    star_handlers_registry._handlers.remove(h)
    star_handlers_registry.star_handlers_map.pop(h.handler_full_name, None)
star_map.pop(plugin_module.__name__, None)

OUT: dict = {}


async def scenario(name, *, disabled=False, renamed=False, filtered=False,
                   cmd="reset", provider=True, prefix=False, follower=False,
                   recover=False):
    state_dir = ROOT / ("s-" + name)
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge, resolver, membership, ledger = make_bridge_stack(str(state_dir))
    metas = register_bridge(bridge)
    try:
        conv = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="synthetic-cid"),
            get_conversation=AsyncMock(return_value=None),
            update_conversation=AsyncMock(),
            new_conversation=AsyncMock(return_value="new-synthetic-cid"),
        )
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["disable_builtin_commands"] = disabled
        cfg["wake_prefix"] = ["/"]
        cfg["admins_id"] = ["10001"]
        prov = FakeProvider() if provider else None
        # manager 层双方法（等价独立探针）：Context 类层面按宿主版本暴露
        # 不同接口（4.26 Context 无 get_using_provider_async），插件的版本
        # 兼容检查针对真实 Context 实例——这正是 T5 的验证点。
        provider_manager = SimpleNamespace(
            get_using_provider=lambda **kw: prov,
            get_using_provider_async=AsyncMock(return_value=prov),
        )
        context = Context(
            event_queue=asyncio.Queue(), config=cfg, db=None,
            provider_manager=provider_manager,
            platform_manager=None, conversation_manager=conv,
            message_history_manager=None,
            persona_manager=FakePersonaManager(),
            astrbot_config_mgr=SimpleNamespace(get_conf=lambda umo: cfg),
            knowledge_base_manager=None, cron_manager=None,
        )
        plugin = plugin_module.UserContextBridgePlugin.__new__(
            plugin_module.UserContextBridgePlugin
        )
        plugin.context = context
        plugin._bridge = bridge  # 真实 bound on_decorating_result 需要
        plugin._sharing_active = True
        plugin._membership = membership
        plugin._ledger = ledger
        plugin._resolver = resolver
        plugin._commands = CommandService(
            ledger=ledger, resolver=resolver, membership=membership,
            persona_manager_getter=lambda: FakePersonaManager(),
            conversation_manager_getter=lambda: conv,
        )
        builtin_instance = native.Main.__new__(native.Main)
        builtin_instance.context = context
        builtin_instance.conversation_c = conversation.ConversationCommands(context)
        star_map[native.__name__] = SimpleNamespace(
            name="astrbot", activated=True, reserved=True
        )
        for handler in star_handlers_registry:
            handler.enabled = False
            if handler.handler_module_path == native.__name__ and handler.handler_name in ("reset", "new_conv"):
                handler.enabled = True
                handler.handler = getattr(builtin_instance, handler.handler_name)
                for f in handler.event_filters:
                    if isinstance(f, CommandFilter):
                        f.command_name = (
                            ("clear-history" if renamed else "reset")
                            if handler.handler_name == "reset" else "new"
                        )
                        f._cmpl_cmd_names = None
                        f.custom_filter_list = []
                        if filtered and handler.handler_name == "reset":
                            f.custom_filter_list.append(
                                SimpleNamespace(filter=lambda event, cfg: False)
                            )
        followers = []
        follow_obs = {}
        follower_ready = asyncio.Event()
        follower_release = asyncio.Event()
        if follower:
            from astrbot.core.star.star_handler import StarHandlerMetadata, EventType
            from astrbot.core.star.star import StarMetadata
            from astrbot.core.message.message_event_result import MessageEventResult
            from tests.r_rework_check import identity_of as _ident_of

            async def after_command_notice(ev):
                follow_obs["epoch_after_native"] = ledger.current_epoch(
                    _ident_of("10001")
                )
                follow_obs["history_after_native"] = len(
                    ledger.load_history(_ident_of("10001"))
                )
                follower_ready.set()
                await follower_release.wait()
                ev.set_result(
                    MessageEventResult().message("OBSERVER-FOLLOWUP-NOTICE")
                )

            for registered in star_handlers_registry._handlers:
                if not hasattr(registered, "extras_configs"):
                    registered.extras_configs = {}
            obs_module = "uctx_t5_observer"
            star_map[obs_module] = StarMetadata(name=obs_module, activated=True)
            obs_meta = StarHandlerMetadata(
                event_type=EventType.AdapterMessageEvent,
                handler_full_name=obs_module + "_notice",
                handler_name="notice",
                handler_module_path=obs_module,
                handler=after_command_notice,
                event_filters=[],
                extras_configs={"priority": -10},
            )
            obs_meta.event_filters = [CommandFilter(cmd, handler_md=obs_meta)]
            star_handlers_registry._handlers.append(obs_meta)
            star_handlers_registry.star_handlers_map[
                obs_meta.handler_full_name
            ] = obs_meta
            followers.append(obs_meta)
        event = FakeEvent(
            sender_id="10001", group_id="700000001", role="admin",
            message_str="/" + cmd,
        )
        event.message_obj.message = [Plain("/" + cmd)]
        identity = await plugin._commands._identity(event)
        ledger.begin_turn(
            identity_key=identity.key, event_key="seed-" + name,
            source_type="group", source_id="700000001",
            umo=event.unified_msg_origin,
            user_message={"role": "user", "content": "SYNTHETIC-BEFORE-COMMAND"},
        )
        ledger.commit_turn(
            event_key="seed-" + name, status="completed",
            trajectory=[{"role": "assistant", "content": "SYNTHETIC-ANSWER"}],
            reply_text="SYNTHETIC-ANSWER",
        )
        old_epoch = ledger.current_epoch(identity.key)
        before = len(ledger.load_history(identity.key))
        wake = WakingCheckStage()
        wake.ctx = SimpleNamespace(astrbot_config=cfg)
        wake.no_permission_reply = True
        wake.friend_message_needs_wake_prefix = False
        wake.ignore_bot_self_message = False
        wake.ignore_at_all = False
        wake.disable_builtin_commands = disabled
        wake.unique_session = False
        wake._umo_auto_name_recorder = SimpleNamespace(schedule=lambda event: None)
        prefs = SimpleNamespace(get_async=AsyncMock(return_value={}))
        with patch.object(plugin_module, "sp", prefs), \
             patch.object(conversation, "sp", prefs), \
             patch.object(session_plugins, "sp", prefs):
            await wake.process(event)
            active = [
                h.handler_name
                for h in event.get_extra("activated_handlers") or []
                if h.handler_module_path == native.__name__
            ]
            starstage = StarRequestSubStage()
            starstage.ctx = SimpleNamespace(astrbot_config=cfg)

            # U3/U4：真实 ResultDecorateStage + 注册的装饰钩子（可选前置
            # 文本装饰 + 插件真实 bound 处理器），经真实 PipelineScheduler
            # 洋葱调度——不再直接调用 helper 冒充共存链。
            from astrbot.core.pipeline.result_decorate.stage import (
                ResultDecorateStage,
            )
            from astrbot.core.pipeline.scheduler import PipelineScheduler
            from astrbot.core.star.star_handler import StarHandlerMetadata
            from astrbot.core.star.star import StarMetadata
            from astrbot.core.star.star import star_map as smap_local
            from astrbot.core.star.star_handler import EventType

            module_name = "uctx_t5_decorators"
            smap_local[module_name] = StarMetadata(name=module_name, activated=True)
            added = []
            for registered in star_handlers_registry._handlers:
                if not hasattr(registered, "extras_configs"):
                    registered.extras_configs = {}

            async def add_prefix(ev):
                result = ev.get_result()
                if result:
                    for part in result.chain:
                        if isinstance(part, Plain):
                            part.text = "[NOTICE] " + part.text
                            break

            hooks = (
                [(add_prefix, "prefix", 10)] if prefix else []
            ) + [
                (plugin.on_decorating_result, "plugin", 0),
            ]
            for handler, label, priority in hooks:
                meta = StarHandlerMetadata(
                    event_type=EventType.OnDecoratingResultEvent,
                    handler_full_name=module_name + "_" + label,
                    handler_name=label,
                    handler_module_path=module_name,
                    handler=handler,
                    event_filters=[],
                    extras_configs={"priority": priority},
                )
                star_handlers_registry._handlers.append(meta)
                star_handlers_registry.star_handlers_map[
                    meta.handler_full_name
                ] = meta
                added.append(meta)
            cfg["t2i"] = False
            cfg["content_safety"]["also_use_in_response"] = False
            cfg["provider_tts_settings"]["enable"] = False
            cfg["platform_settings"]["reply_with_mention"] = False
            cfg["platform_settings"]["reply_with_quote"] = False
            cfg["platform_settings"]["segmented_reply"]["enable"] = False
            dctx = SimpleNamespace(
                astrbot_config=cfg,
                plugin_manager=SimpleNamespace(
                    context=SimpleNamespace(
                        get_using_tts_provider_async=AsyncMock(return_value=None),
                        get_using_tts_provider=lambda umo: None,
                    )
                ),
            )
            decorator = ResultDecorateStage()
            await decorator.initialize(dctx)

            class Transport:
                async def process(self, ev):
                    result = ev.get_result()
                    if result is not None and getattr(result, "chain", None):
                        await ev.send(result)

            scheduler = PipelineScheduler.__new__(PipelineScheduler)
            scheduler.ctx = dctx
            scheduler.stages = [starstage, decorator, Transport()]
            try:
                if follower:
                    # V1 反例：命令任务挂起于后置通知屏障期间，同身份
                    # 另一群完成新问答；放行后观测 epoch/历史/提示次数。
                    from tests.harness import drive_pipeline

                    command_task = asyncio.create_task(
                        scheduler._process_stages(event)
                    )
                    try:
                        await asyncio.wait_for(follower_ready.wait(), 5)
                        for meta in metas:
                            meta.enabled = True
                        follow_event = FakeEvent(
                            sender_id="10001",
                            group_id="700000002",
                            message_str="AFTER-RESET-QUESTION",
                        )
                        follow_provider = FakeProvider(["AFTER-RESET-ANSWER"])
                        follow_obs["command_waiting_during_new_turn"] = (
                            not command_task.done()
                        )
                        follow_obs["new_turn_result"] = await asyncio.wait_for(
                            drive_pipeline(
                                bridge,
                                follow_event,
                                follow_provider,
                                prompt=follow_event.message_str,
                            ),
                            5,
                        )
                        follow_obs["new_turn_model_calls"] = len(
                            follow_provider.call_log
                        )
                        follow_obs["history_before_notice"] = len(
                            ledger.load_history(identity.key)
                        )
                    finally:
                        for meta in metas:
                            meta.enabled = False
                        follower_release.set()
                    await asyncio.wait_for(command_task, 5)
                else:
                    await scheduler._process_stages(event)
                    if recover:
                        # V2a 真实恢复链：从本命令事件/账本继续，恢复
                        # provider 后跑真实新轮——不直接 bump_epoch。
                        from tests.harness import drive_pipeline

                        prov_rec = FakeProvider(["RECOVERY-ANSWER"])
                        provider_manager.get_using_provider_async = AsyncMock(
                            return_value=prov_rec
                        )
                        provider_manager.get_using_provider = (
                            lambda **kw: prov_rec
                        )
                        for meta in metas:
                            meta.enabled = True
                        after_ev = FakeEvent(
                            sender_id="10001",
                            group_id="700000002",
                            message_str="RECOVERY-QUESTION",
                        )
                        await drive_pipeline(
                            bridge, after_ev, prov_rec, prompt=after_ev.message_str
                        )
                        after_ctx = (
                            json.dumps(
                                prov_rec.call_log[0]["contexts"],
                                ensure_ascii=False,
                            )
                            if prov_rec.call_log
                            else ""
                        )
                        follow_obs["recovery_old_leak"] = (
                            "SYNTHETIC-BEFORE-COMMAND" in after_ctx
                            or "SYNTHETIC-ANSWER" in after_ctx
                        )
                        follow_obs["recovery_new_present"] = (
                            "RECOVERY-QUESTION" in after_ctx
                        )
                        for meta in metas:
                            meta.enabled = False
            finally:
                for meta in added:
                    star_handlers_registry._handlers.remove(meta)
                    star_handlers_registry.star_handlers_map.pop(
                        meta.handler_full_name, None
                    )
                smap_local.pop(module_name, None)
        OUT[name] = dict(
            builtin_activated=active,
            follow_observations=follow_obs,
            epoch_delta=ledger.current_epoch(identity.key) - old_epoch,
            notify_count=sum(
                "跨窗口共享历史" in (c.get_plain_text() or "")
                for c in event.sent_chains
            ),
            real_context_has_async_provider=hasattr(
                context, "get_using_provider_async"
            ),
            native_update_calls=conv.update_conversation.await_count,
            native_new_calls=conv.new_conversation.await_count,
            epoch_bumped=ledger.current_epoch(identity.key) > old_epoch,
            history_before=before,
            history_after=len(ledger.load_history(identity.key)),
            plugin_notified=any(
                "跨窗口共享历史" in (c.get_plain_text() or "")
                for c in event.sent_chains
            ),
            sent=[c.get_plain_text() for c in event.sent_chains],
            clean_marked=event.get_extra("_clean_group_context_session"),
        )
    finally:
        for meta in followers:
            star_handlers_registry._handlers.remove(meta)
            star_handlers_registry.star_handlers_map.pop(
                meta.handler_full_name, None
            )
        star_map.pop("uctx_t5_observer", None)
        bridge.shutdown()
        cleanup(metas)
        ledger.close()


async def main() -> int:
    await scenario("default-reset")
    await scenario("no-provider", provider=False)
    await scenario("builtins-disabled-reset", disabled=True)
    await scenario("builtins-disabled-new", disabled=True, cmd="new")
    await scenario("builtin-reset-renamed-old-name", renamed=True)
    await scenario("builtin-reset-renamed-new-name", renamed=True, cmd="clear-history")
    await scenario("builtin-reset-custom-filter-denied", filtered=True)
    await scenario("new-with-provider", cmd="new")
    await scenario("new-without-provider", cmd="new", provider=False)
    await scenario("reset-prefix-decorator", prefix=True)
    await scenario("reset-two-handlers", follower=True)
    await scenario("new-two-handlers", cmd="new", follower=True)
    # V2a 真实恢复链：无 provider 原生 new 成功联动清空后，恢复 provider
    # 跑真实新轮——同一命令事件、同一账本，不直接 bump_epoch
    await scenario(
        "new-without-provider-then-recovery", cmd="new", provider=False,
        recover=True,
    )
    OUT["recovery_no_backfill_after_new"] = {
        "old_leak": OUT["new-without-provider-then-recovery"]
        .get("follow_observations", {})
        .get("recovery_old_leak"),
        "new_turn_present": OUT["new-without-provider-then-recovery"]
        .get("follow_observations", {})
        .get("recovery_new_present"),
    }
    print("@@RESULT@@" + json.dumps(OUT, ensure_ascii=False))
    return 0


sys.exit(asyncio.run(main()))
