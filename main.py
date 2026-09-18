"""astrbot_plugin_user_context_bridge

同一 QQ 用户在同一机器人、同一人格及明确启用的群聊与私聊之间，
共享连续真实对话历史。

技术路线见 docs/ADR.md。装配：scope 判定 → 轮次账本 → ContextBridge
生命周期协调；钩子只做转发，业务全部在 uctx_bridge 模块内（可独立测试）。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.core import sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

from .uctx_bridge.bridge import ContextBridge
from .uctx_bridge.commands import CommandService
from .uctx_bridge.ledger import LeaseConflictError, TurnLedger
from .uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

_HEARTBEAT_INTERVAL_SECONDS = 60.0


@register(
    "astrbot_plugin_user_context_bridge",
    "Ewnscat-ya",
    "同一用户跨会话上下文共享（群聊/私聊连续真实对话历史）",
    "0.6.0",
)
class UserContextBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        self._data_dir = StarTools.get_data_dir()
        self._ledger = TurnLedger(self._data_dir / "uctx_ledger.db")
        self._membership = MembershipStore(self._data_dir / "membership.json")
        self._resolver = ScopeResolver(
            ScopeConfig.from_mapping(dict(config)), self._membership
        )
        self._bridge: ContextBridge | None = None
        self._commands: CommandService | None = None
        # R6：实例租约 ID 为运行时唯一（每次加载生成新 ID），不再持久化到
        # 数据目录——避免两个实例从同一文件读到同一 ID 绕过冲突检查。
        self._lease_id = uuid.uuid4().hex
        self._lease_token: str | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._sharing_active = False

    # -- 生命周期 ---------------------------------------------------------
    def _provider_settings(self, umo: str | None = None) -> dict:
        """T1：与宿主 _ensure_persona_and_skills 同源的 provider_settings。"""

        try:
            cfg = self.context.get_config(umo=umo)
            settings = cfg.get("provider_settings", {})
            return settings if isinstance(settings, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def initialize(self) -> None:
        await super().initialize()
        self._ledger.open()
        try:
            self._lease_token = self._ledger.acquire_lease(self._lease_id)
        except LeaseConflictError as exc:
            # 同库双实例防护：保持加载但禁用共享，避免并发写
            logger.warning(
                "uctx 共享已禁用：检测到另一 AstrBot 实例正在使用同一数据目录"
                f"（{exc}）。如需启用，请先停用另一个实例。"
            )
            self._bridge = ContextBridge(
                ledger=self._ledger,
                scope_resolver=ScopeResolver(
                    ScopeConfig(enabled=False), self._membership
                ),
                persona_manager_getter=lambda: self.context.persona_manager,
                provider_settings_getter=self._provider_settings,
                logger=logger,
            )
            self._commands = CommandService(
                ledger=self._ledger,
                resolver=ScopeResolver(ScopeConfig(enabled=False), self._membership),
                membership=self._membership,
                persona_manager_getter=lambda: self.context.persona_manager,
                conversation_manager_getter=lambda: self.context.conversation_manager,
                provider_settings_getter=self._provider_settings,
            )
            return

        recovered = self._ledger.recover_running(own_lease_id=self._lease_id)
        if recovered:
            logger.info(f"uctx 重启恢复：{recovered} 个挂起轮次标记为 interrupted")

        self._resolver.update_config(ScopeConfig.from_mapping(dict(self._config)))
        self._bridge = ContextBridge(
            ledger=self._ledger,
            scope_resolver=self._resolver,
            persona_manager_getter=lambda: self.context.persona_manager,
            provider_settings_getter=self._provider_settings,
            max_history_turns=int(self._config.get("max_history_turns", 0)) or None,
            logger=logger,
        )
        self._sharing_active = True
        self._commands = CommandService(
            ledger=self._ledger,
            resolver=self._resolver,
            membership=self._membership,
            persona_manager_getter=lambda: self.context.persona_manager,
            conversation_manager_getter=lambda: self.context.conversation_manager,
            provider_settings_getter=self._provider_settings,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self._config.get("enabled", False):
            logger.info(
                "uctx 已启用：共享群=%s 私聊=%s（默认关闭时无任何采集）",
                list(self._resolver.config.shared_groups),
                self._resolver.config.include_private,
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            try:
                self._ledger.heartbeat_lease(self._lease_id, self._lease_token)
            except Exception:  # noqa: BLE001
                logger.warning("uctx 租约心跳失败", exc_info=True)

    async def terminate(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat_task = None
        if self._bridge is not None:
            finalized = self._bridge.shutdown()
            if finalized:
                logger.info(
                    f"uctx 停用/卸载：{finalized} 个未决轮次已停止并标记 interrupted"
                )
        try:
            self._ledger.release_lease(self._lease_id, self._lease_token or "")
        except Exception:  # noqa: BLE001
            pass
        self._ledger.close()
        await super().terminate()

    # -- 钩子（业务转发） -------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if self._bridge is None:
            return
        try:
            await self._bridge.handle_llm_request(event, req)
        except Exception:  # noqa: BLE001 - 插件异常不得中断宿主流程
            logger.error("uctx on_llm_request 处理失败", exc_info=True)

    @filter.on_agent_done()
    async def on_agent_done(
        self, event: AstrMessageEvent, run_context, llm_response
    ):
        if self._bridge is None:
            return
        try:
            await self._bridge.handle_agent_done(event, run_context, llm_response)
        except Exception:  # noqa: BLE001
            logger.error("uctx on_agent_done 处理失败", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        if self._bridge is not None:
            try:
                await self._bridge.handle_decorating_result(event)
            except Exception:  # noqa: BLE001
                logger.error("uctx on_decorating_result 处理失败", exc_info=True)
        # T6：宿主 builtin reset/new 实际成功后置关联（结构化激活证据 +
        # 宿主固定成功文案；此时宿主处理器已执行完毕）
        try:
            await self._sync_native_reset_on_success(event)
        except Exception:  # noqa: BLE001
            logger.warning("uctx 原生命令关联检查失败", exc_info=True)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        if self._bridge is None:
            return
        try:
            await self._bridge.handle_after_message_sent(event)
        except Exception:  # noqa: BLE001
            logger.warning("uctx on_after_message_sent 处理失败", exc_info=True)

    # -- /uctx 命令组 ------------------------------------------------------
    @filter.command_group("uctx")
    def uctx_group(self):
        pass

    @uctx_group.command("status")
    async def uctx_status(self, event: AstrMessageEvent):
        if self._commands is not None:
            event.set_result(
                MessageEventResult().message(
                    await self._commands.status(event)
                )
            )

    @uctx_group.command("reset")
    async def uctx_reset(self, event: AstrMessageEvent):
        if self._commands is not None:
            event.set_result(
                MessageEventResult().message(
                    await self._commands.reset(event)
                )
            )

    @uctx_group.command("off")
    async def uctx_off(self, event: AstrMessageEvent):
        if self._commands is not None:
            event.set_result(
                MessageEventResult().message(
                    await self._commands.off(event)
                )
            )

    @uctx_group.command("on")
    async def uctx_on(self, event: AstrMessageEvent):
        if self._commands is not None:
            event.set_result(
                MessageEventResult().message(
                    await self._commands.on(event)
                )
            )

    @uctx_group.command("scope")
    async def uctx_scope(self, event: AstrMessageEvent):
        if self._commands is not None:
            event.set_result(
                MessageEventResult().message(self._commands.scope())
            )

    # -- 原生 /reset、/new 成功关联（U2/U3，ADR-013） -----------------------
    # 不注册同名单命令处理器（T6 证实与宿主执行脱节）。后置事实关联：
    # ① activated_handlers 含宿主 builtin reset/new_conv（WakingCheckStage
    #   结构化激活证据，天然涵盖权限/禁用/改名/自定义过滤）；
    # ② 事件 extra ``_clean_group_context_session`` 为真——宿主 builtin 在
    #   **本地会话 reset 清空成功 / new_conversation 创建成功**的末尾设置
    #   的结构化标记（两版字面量一致；权限拒绝/无 provider/第三方执行器
    #   分支均不设置；消费者为宿主 group_chat_context 清理）。该标记是
    #   布尔 extra，不受其他插件的文本装饰改写（U3：此前依赖成功文案
    #   startswith，前置装饰钩子加前缀即漏清）。
    # 满足双证据即联动，防御按命令真实语义区分（U2）：reset 成功必有
    # provider，保留 provider+会话复核；new 不要求 provider，只复核会话。
    # V1（一次性应用）：宿主标记在本事件内持久（after_message_sent 仍要
    # 读），同一事件的多次装饰（同事件的多个处理器各自回复时都会进入
    # 装饰阶段）不得重复清空——用插件私有的 applied 状态去重：判定成功
    # 后**先原子认领**（同步设置本 extra 再做任何 await），之后同一事件
    # 的后续装饰直接跳过；清空已提交后即使提示发送失败也不会回到可再次
    # 清空的状态；新命令是新事件，天然不受影响。
    _BUILTIN_COMMANDS_MODULE_PREFIX = "astrbot.builtin_stars.builtin_commands"
    _CLEAN_SESSION_EXTRA = "_clean_group_context_session"
    _NATIVE_SYNC_APPLIED_EXTRA = "_uctx_native_sync_applied"

    def _native_reset_executed(self, event: AstrMessageEvent) -> str | None:
        """返回本事件实际执行成功的 builtin 命令名（"reset"/"new_conv"）。

        尚未应用过联动时才返回命令名；已应用（本事件任意装饰阶段认领
        过）返回 None——同一成功只关联一次（V1）。
        """

        if event.get_extra(self._NATIVE_SYNC_APPLIED_EXTRA) is True:
            return None
        activated = event.get_extra("activated_handlers") or []
        for h in activated:
            if (
                str(getattr(h, "handler_module_path", "")).startswith(
                    self._BUILTIN_COMMANDS_MODULE_PREFIX
                )
                and getattr(h, "handler_name", "") in ("reset", "new_conv")
                and event.get_extra(self._CLEAN_SESSION_EXTRA) is True
            ):
                return getattr(h, "handler_name", "")
        return None

    async def _sync_native_reset_on_success(self, event: AstrMessageEvent) -> None:
        if self._commands is None or not self._sharing_active:
            return
        try:
            command = self._native_reset_executed(event)
            if command is None:
                return
            # V1：同步原子认领——在任何后续 await（防御复核/身份解析/
            # 发送）之前标记本事件已应用，防止异步检查期间另一装饰阶段
            # 重复认领。即使后续防御不通过或提示发送失败，本事件也不会
            # 再次触发清空（保守方向：宁可少清，不可重复清）。
            event.set_extra(self._NATIVE_SYNC_APPLIED_EXTRA, True)
            # 防御性复核（U2 按命令语义）：reset 成功必有 provider；
            # new 不要求 provider（宿主无此检查），只复核当前会话存在。
            if command == "reset":
                if not await self._host_command_defense(event):
                    return
            else:
                try:
                    cid = await (
                        self.context.conversation_manager
                        .get_curr_conversation_id(event.unified_msg_origin)
                    )
                except Exception:  # noqa: BLE001
                    cid = None
                if not cid:
                    return
            identity = await self._commands._identity(event)
            if not self._commands._window_in_scope(event):
                return
            if self._membership is not None and self._membership.is_opted_out(identity):
                return
            self._ledger.bump_epoch(identity.key)
            await event.send(
                MessageChain().message(
                    "🧹 已同步清空你的跨窗口共享历史（原生 reset/new 联动；"
                    "仅影响你本人）。"
                )
            )
        except Exception:  # noqa: BLE001 - 联动失败不影响宿主命令
            logger.warning("uctx 原生命令联动失败", exc_info=True)

    async def _host_command_defense(self, event: AstrMessageEvent) -> bool:
        """成功关联的轻量防御：可用 provider + 当前会话存在。"""

        get_async = getattr(self.context, "get_using_provider_async", None)
        try:
            if get_async is not None:
                provider = await get_async(event.unified_msg_origin)
            else:
                get_sync = getattr(self.context, "get_using_provider", None)
                provider = get_sync(event.unified_msg_origin) if get_sync else None
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            return False
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
        except Exception:  # noqa: BLE001
            cid = None
        return bool(cid)

    # 宿主第三方会话执行器键（两版一致；这些路径 v1 不支持，不联动）
    _THIRD_PARTY_RUNNERS = frozenset(
        {"dify", "coze", "dashscope", "deerflow"}
    )

    async def _host_reset_would_run(self, event: AstrMessageEvent) -> bool:
        """镜像宿主 builtin /reset 的完整执行条件（S5 补全）。

        逐项对应宿主 ConversationCommands.reset 的拒绝分支（两版逻辑一致，
        字段名差异已兼容）：权限（scene + alter_cmd + role）、第三方执行器
        （v1 不支持，宿主走远端清空分支，不联动）、可用模型提供方
        （宿主在无 provider 时拒绝并提示，不得清空共享历史）、当前会话
        存在。任一不满足即返回 False（不联动）。
        """

        cfg = self.context.get_config(umo=event.unified_msg_origin)
        is_unique_session = cfg["platform_settings"]["unique_session"]
        is_group = bool(event.get_group_id())
        if is_group:
            scene_key = (
                "group_unique_on" if is_unique_session else "group_unique_off"
            )
        else:
            scene_key = "private"
        default_perm = "admin" if is_group and not is_unique_session else "member"
        alter_cmd_cfg = await sp.get_async("global", "global", "alter_cmd", {})
        required_perm = (
            alter_cmd_cfg.get("astrbot", {}).get("reset", {}).get(
                scene_key, default_perm
            )
        )
        if required_perm == "admin" and event.role != "admin":
            return False
        # 宿主下一步检查 runner 类型：第三方执行器走远端清空分支（v1 不支持）
        agent_cfg = cfg.get("agent_runner", {})
        runner_type = agent_cfg.get("runner_type") if isinstance(
            agent_cfg, dict
        ) else None
        if runner_type is None:
            # 4.26 字段位置不同
            runner_type = (
                cfg.get("provider_settings", {}).get("agent_runner_type")
            )
        if runner_type in self._THIRD_PARTY_RUNNERS:
            return False
        # 宿主随后要求可用模型提供方（无 provider 时拒绝重置）。
        # T5：4.26 Context 只有同步 get_using_provider；按存在性选择接口，
        # 不假设两版同形（探针证实 4.26 无 async 接口）。
        get_async = getattr(self.context, "get_using_provider_async", None)
        try:
            if get_async is not None:
                provider = await get_async(event.unified_msg_origin)
            else:
                get_sync = getattr(self.context, "get_using_provider", None)
                provider = get_sync(event.unified_msg_origin) if get_sync else None
        except Exception:  # noqa: BLE001 - 接口异常按宿主拒绝处理
            provider = None
        if provider is None:
            return False
        cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        return bool(cid)
