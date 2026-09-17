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
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

from uctx_bridge.bridge import ContextBridge
from uctx_bridge.ledger import LeaseConflictError, TurnLedger
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

_HEARTBEAT_INTERVAL_SECONDS = 60.0
_LEASE_ID_FILENAME = "instance_lease_id"


@register(
    "astrbot_plugin_user_context_bridge",
    "Ewnscat-ya",
    "同一用户跨会话上下文共享（群聊/私聊连续真实对话历史）",
    "0.1.0",
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
        self._lease_id = self._load_or_create_lease_id()
        self._heartbeat_task: asyncio.Task | None = None
        self._sharing_active = False

    # -- 生命周期 ---------------------------------------------------------
    def _load_or_create_lease_id(self) -> str:
        path = self._data_dir / _LEASE_ID_FILENAME
        try:
            if path.exists():
                lease_id = path.read_text(encoding="utf-8").strip()
                if lease_id:
                    return lease_id
            path.parent.mkdir(parents=True, exist_ok=True)
            lease_id = uuid.uuid4().hex
            path.write_text(lease_id, encoding="utf-8")
            return lease_id
        except Exception:  # noqa: BLE001 - 文件异常时退化为进程内租约
            return uuid.uuid4().hex

    async def initialize(self) -> None:
        await super().initialize()
        self._ledger.open()
        try:
            self._ledger.acquire_lease(self._lease_id)
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
                logger=logger,
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
            max_history_turns=int(self._config.get("max_history_turns", 0)) or None,
            logger=logger,
        )
        self._sharing_active = True
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
                self._ledger.heartbeat_lease(self._lease_id)
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
            finalized = self._bridge.finalize_pending_as_interrupted()
            if finalized:
                logger.info(f"uctx 卸载：{finalized} 个未决轮次标记为 interrupted")
        try:
            self._ledger.release_lease(self._lease_id)
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
        if self._bridge is None:
            return
        try:
            await self._bridge.handle_decorating_result(event)
        except Exception:  # noqa: BLE001
            logger.error("uctx on_decorating_result 处理失败", exc_info=True)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        if self._bridge is None:
            return
        try:
            await self._bridge.handle_after_message_sent(event)
        except Exception:  # noqa: BLE001
            logger.warning("uctx on_after_message_sent 处理失败", exc_info=True)
