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
import sqlite3
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


def user_ident_of(identity):
    """同基础账号的 user 维度身份（X5 继承保护场景用）。"""

    parts = identity.key.split("")
    from uctx_bridge.identity import build_identity

    return build_identity(
        platform_id=parts[0], self_id=parts[1], persona_scope=None,
        sender_id=parts[3], mode="user",
    )


async def scenario(name, *, mode="persona", disabled=False, renamed=False,
                   filtered=False, cmd="reset", provider=True, prefix=False,
                   follower=False, recover=False, inherit_protected=False,
                   group="700000001", role="admin", sender="10001",
                   follower_persona: str | None = None):
    state_dir = ROOT / ("s-" + name)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Z3b：follower_persona 给定时，命令窗口（700000001）解析为 maid、
    # follower 新轮窗口（700000002）解析为另一人格——经真实
    # resolve_selected_persona/provider_settings 链，替换默认单人格替身。
    per_window_pm = None
    ps_getter = None

    if follower_persona:

        class _PerWindowPM(FakePersonaManager):
            async def resolve_selected_persona(
                self, *, umo, conversation_persona_id, platform_name, **kw
            ):
                scope = (follower_persona
                         if "700000002" in str(umo) else "maid")
                persona = {
                    "name": scope,
                    "prompt": "T5-PERSONA-" + scope,
                    "_begin_dialogs_processed": [],
                }
                return (scope, persona, None, False)

        def ps_getter(umo):  # noqa: ANN001 - 闭包局部
            persona = (follower_persona
                       if "700000002" in str(umo) else "maid")
            return {"provider_settings": {"default_personality": persona}}

        per_window_pm = _PerWindowPM()

    pmgr = per_window_pm if per_window_pm is not None else FakePersonaManager()
    bridge, resolver, membership, ledger = make_bridge_stack(
        str(state_dir),
        persona_manager_getter=lambda: pmgr,
        provider_settings_getter=ps_getter,
    )
    resolver.set_history_scope(mode)
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
            persona_manager=pmgr,
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
            persona_manager_getter=lambda: pmgr,
            provider_settings_getter=ps_getter,
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

            async def after_command_notice(ev):
                # N10/Y3：follower 查询键随命令事件身份走（user 模式即
                # u: 键），不得固定查 persona 键冒充 user 证据
                follow_obs["observer_key_scope"] = identity.key.split(
                    chr(31))[2][:2]
                follow_obs["epoch_after_native"] = ledger.current_epoch(
                    identity.key
                )
                follow_obs["history_after_native"] = len(
                    ledger.load_history(identity.key)
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
        # sender 不得在 admins_id（否则 wake 阶段会提权为 admin，
        # 权限拒绝分支无法真实触达）
        event = FakeEvent(
            sender_id=sender, group_id=group, role=role,
            message_str="/" + cmd,
        )
        event.message_obj.message = [Plain("/" + cmd)]
        identity, _persona = await plugin._commands._identity(event)
        ledger.begin_turn(
            identity_key=identity.key, event_key="seed-" + name,
            source_type="group", source_id=group,
            umo=event.unified_msg_origin,
            user_message={"role": "user", "content": "SYNTHETIC-BEFORE-COMMAND"},
        )
        ledger.commit_turn(
            event_key="seed-" + name, status="completed",
            trajectory=[{"role": "assistant", "content": "SYNTHETIC-ANSWER"}],
            reply_text="SYNTHETIC-ANSWER",
        )
        if inherit_protected:
            # X5：user off→persona 基础保护（模拟切换协议产物）——
            # 身份本身无直接键退出，但有效退出为 True
            membership.opt_out(user_ident_of(identity))
            membership.protect_base(user_ident_of(identity))
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
                        # Z3b：新轮真实人格与共享键证据（读账本，非推断）
                        with sqlite3.connect(ledger._db_path) as db:
                            row = db.execute(
                                "SELECT source_persona, identity_key"
                                " FROM turns WHERE user_message LIKE"
                                " '%AFTER-RESET-QUESTION%'"
                                " ORDER BY id DESC LIMIT 1"
                            ).fetchone()
                        follow_obs["new_turn_source_persona"] = (
                            row[0] if row else None)
                        follow_obs["new_turn_identity_key"] = (
                            row[1] if row else None)
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
        _cmd_parts = identity.key.split(chr(31))
        # Z3b：命令窗口人格经真实解析链记录（user 模式 identity 不携带
        # 人格，须用与 build 阶段同参的 resolve_persona_scope）
        command_resolved_persona = None
        try:
            from uctx_bridge.identity import resolve_persona_scope

            command_resolved_persona = await resolve_persona_scope(
                pmgr, event, None,
                provider_settings=(
                    ps_getter(event.unified_msg_origin) if ps_getter else {}
                ),
            )
        except Exception:  # noqa: BLE001 - 记录失败即证据缺失
            command_resolved_persona = None
        OUT[name] = dict(
            command_persona_scope=_cmd_parts[2],
            command_resolved_persona=command_resolved_persona,
            command_base=[_cmd_parts[0], _cmd_parts[1], _cmd_parts[3]],
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
    # N10/Y3：范围外与权限拒绝（persona 方向）
    await scenario("out-of-scope-reset", group="999999999")
    await scenario(
        "permission-denied-reset", role="member", sender="30003")
    # X5/N10：user 模式真实分发矩阵（关键行）
    await scenario("user-default-reset", mode="user")
    await scenario("user-new-with-provider", mode="user", cmd="new")
    await scenario("user-no-provider-new", mode="user", cmd="new", provider=False)
    await scenario("user-renamed-old-name", mode="user", renamed=True)
    await scenario("user-custom-filter-denied", mode="user", filtered=True)
    # N10/Y3：user 模式补齐原定分支
    await scenario("user-no-provider-reset", mode="user", provider=False)
    await scenario("user-builtins-disabled-reset", mode="user", disabled=True)
    await scenario(
        "user-builtins-disabled-new", mode="user", disabled=True, cmd="new")
    await scenario(
        "user-renamed-new-name", mode="user", renamed=True, cmd="clear-history")
    await scenario("user-out-of-scope-reset", mode="user", group="999999999")
    await scenario(
        "user-permission-denied", mode="user", role="member",
        sender="30003")
    # N10/Z3b：user 模式 once-only follower 屏障（follower 为另一个人格）
    await scenario("user-reset-two-handlers", mode="user", follower=True,
                   follower_persona="second")
    await scenario("user-new-two-handlers", mode="user", cmd="new",
                   follower=True, follower_persona="second")
    # X5：继承保护（user off→persona）下原生 new 不得联动/误提示
    await scenario(
        "persona-inherit-protected-new", mode="persona", cmd="new",
        inherit_protected=True,
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
