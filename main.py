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
    "0.4.0",
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

    # -- 原生 /reset、/new 成功关联（T6，ADR-012） --------------------------
    # 不再注册同名单命令处理器（三次验收 T6 证实其与宿主实际执行脱节：
    # 内置命令禁用后仍误清、改名后旧名误清/新名漏清、自定义过滤拒绝误清）。
    # 改为后置事实关联：宿主 builtin reset/new_conv 处理器在本事件的
    # activated_handlers 中（WakingCheckStage 的结构化激活证据，已含权限/
    # 禁用/改名/自定义过滤的过滤结果）**且** 事件结果为宿主 builtin 的
    # 固定成功文案（程序生成字面量，两版一致，非模型正文）时，才切换
    # 发送者共享身份的 epoch。宿主拒绝/异常/被过滤 → 无成功文案 → 不联动。
    _BUILTIN_COMMANDS_MODULE_PREFIX = "astrbot.builtin_stars.builtin_commands"
    _NATIVE_RESET_SUCCESS_PREFIX = "✅ Conversation reset successfully"
    _NATIVE_NEW_SUCCESS_PREFIX = "✅ Switched to new conversation"

    def _native_reset_executed(self, event: AstrMessageEvent) -> bool:
        """宿主 builtin reset/new_conv 实际执行成功的双证据。"""

        activated = event.get_extra("activated_handlers") or []
        builtin_activated = any(
            str(getattr(h, "handler_module_path", "")).startswith(
                self._BUILTIN_COMMANDS_MODULE_PREFIX
            )
            and getattr(h, "handler_name", "") in ("reset", "new_conv")
            for h in activated
        )
        if not builtin_activated:
            return False
        result = event.get_result()
        if result is None:
            return False
        try:
            text = (result.get_plain_text() or "").strip()
        except Exception:  # noqa: BLE001
            return False
        return text.startswith(
            self._NATIVE_RESET_SUCCESS_PREFIX
        ) or text.startswith(self._NATIVE_NEW_SUCCESS_PREFIX)

    async def _sync_native_reset_on_success(self, event: AstrMessageEvent) -> None:
        if self._commands is None or not self._sharing_active:
            return
        try:
            if not self._native_reset_executed(event):
                return
            # 防御性前置（宿主已有 activated+成功文案双证据，此处只复核
            # provider 与当前会话存在；不得套用 reset 的权限场景——宿主
            # /new 无权限门槛，成功文案已按命令区分）
            if not await self._host_command_defense(event):
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
