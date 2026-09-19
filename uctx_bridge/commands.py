"""/uctx 中文管理命令（ADR-006/008；W2/W5 修订）。

命令组（README 固定）：
    /uctx status  查看本人的共享状态（不显示他人标识与聊天正文）
    /uctx reset   清空本人的共享历史（persona=当前人格；user=跨人格整份）
    /uctx off     退出共享（persona=本人格；user=该账号跨人格）
    /uctx on      重新加入共享（定向解除对应维度的退出）
    /uctx scope   查看管理员配置的共享范围（只读）

本人只能操作自己的共享历史；范围由管理员在插件配置中维护，
个人命令不能扩大范围。退出判定与运行时共用
MembershipStore.effective_optout 同一语义；status 显示的"共享中"
必须以实际接管证据为前提，开关开启不等于已接管。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from .identity import (
    MODE_USER,
    PersonaResolutionError,
    SharedIdentity,
    build_identity,
    resolve_persona_scope,
)
from .ledger import TurnLedger
from .scope import MembershipStore, ScopeResolver


class _FakeConv:
    """仅携带 persona_id 的会话替身（供 resolve_selected_persona 同参调用）。"""

    def __init__(self, persona_id: str | None) -> None:
        self.persona_id = persona_id


def _identity_of(
    event: AstrMessageEvent, persona_scope: str | None, mode: str = "persona"
) -> SharedIdentity:
    return build_identity(
        platform_id=str(event.get_platform_id() or ""),
        self_id=str(event.get_self_id() or ""),
        persona_scope=persona_scope,
        sender_id=str(event.get_sender_id() or ""),
        mode=mode,
    )


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class CommandService:
    def __init__(
        self,
        *,
        ledger: TurnLedger,
        resolver: ScopeResolver,
        membership: MembershipStore | None,
        persona_manager_getter,
        conversation_manager_getter=None,
        provider_settings_getter=None,
        history_scope: str = "persona",
        unavailable_reason: str | None = None,
    ) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._membership = membership
        self._get_persona_manager = persona_manager_getter
        self._get_conversation_manager = conversation_manager_getter
        self._provider_settings_getter = provider_settings_getter
        self._history_scope = history_scope
        self._unavailable_reason = unavailable_reason

    def _provider_settings(self, event: AstrMessageEvent) -> dict:
        """T1：与对话轮同源的 provider_settings（4.26 默认人格读取依赖）。"""

        if self._provider_settings_getter is None:
            return {}
        try:
            result = self._provider_settings_getter(event.unified_msg_origin)
            return result if isinstance(result, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _current_persona_id(self, event: AstrMessageEvent) -> str | None:
        """读取当前 UMO 会话 conversation 的 persona_id（R5）。

        与对话轮次一致：宿主解析身份时使用 req.conversation.persona_id，
        即当前选中会话的 conversation；命令场景从 conversation_manager
        读取同一对象，保证命令与对话作用于同一生效身份。
        """

        manager = (
            self._get_conversation_manager()
            if self._get_conversation_manager is not None
            else None
        )
        if manager is None:
            return None
        try:
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                return None
            conversation = await manager.get_conversation(
                event.unified_msg_origin, cid
            )
            return getattr(conversation, "persona_id", None)
        except Exception:  # noqa: BLE001 - 读取失败退回默认人格解析
            return None

    async def _resolved_persona(self, event: AstrMessageEvent) -> str:
        """解析当前真实生效人格（persona/user 模式都解析；失败抛受控异常）。"""

        conversation_persona_id = await self._current_persona_id(event)
        return await resolve_persona_scope(
            self._get_persona_manager(),
            event,
            _FakeConv(conversation_persona_id),
            provider_settings=self._provider_settings(event),
        )

    async def _identity(self, event: AstrMessageEvent) -> tuple[SharedIdentity, str]:
        """返回 (共享身份, 当前真实人格)。

        persona 模式：共享身份 scope=当前人格；user 模式：共享身份为
        跨人格令牌（u:），当前人格仅用于显示与 source_persona 记录。
        """

        persona = await self._resolved_persona(event)
        if self._resolver.history_scope == MODE_USER:
            return _identity_of(event, None, mode=MODE_USER), persona
        return _identity_of(event, persona), persona

    def _identity_error_text(self) -> str:
        return (
            "⚠️ 暂时无法确定当前生效人格，为避免把不同人格的共享历史合并，"
            "本次操作不执行。请检查人格配置后重试。"
        )

    def _unavailable_text(self) -> str:
        return f"⚠️ 共享功能当前不可用：{self._unavailable_reason or '状态存储不可用'}"

    def _scope_line(self) -> str:
        cfg = self._resolver.config
        return (
            f"启用（模式：{self._resolver.history_scope}·"
            + (
                "跨人格共享，每窗口仍用各自人格"
                if self._resolver.history_scope == MODE_USER
                else "按人格隔离"
            )
            + f"；共享群 {len(cfg.shared_groups)} 个，"
            f"私聊{'含' if cfg.include_private else '不含'}）"
        )

    def _window_in_scope(self, event: AstrMessageEvent) -> bool:
        """命令场景的窗口范围判定（群白名单/私聊开关；不含对话轮次过滤）。"""

        cfg = self._resolver.config
        if not cfg.enabled:
            return False
        group_id = str(event.get_group_id() or "")
        if group_id:
            return group_id in cfg.shared_groups
        return cfg.include_private

    def _reset_scope_text(self, identity: SharedIdentity) -> str:
        if identity.mode == MODE_USER:
            return "你跨人格的整份共享历史（全部人格）"
        return f"人格「{identity.persona_scope}」的共享历史"

    # -- 子命令 -----------------------------------------------------------
    async def status(self, event: AstrMessageEvent) -> str:
        if self._unavailable_reason is not None or self._membership is None:
            return self._unavailable_text()
        try:
            identity, persona = await self._identity(event)
        except PersonaResolutionError:
            return self._identity_error_text()
        window = "群聊" if event.get_group_id() else "私聊"
        cfg = self._resolver.config
        if not cfg.enabled:
            return (
                "📋 跨窗口上下文共享：关闭\n"
                "当前不会采集或共享任何消息。范围由管理员在插件配置中开启。"
            )
        in_scope = self._window_in_scope(event)
        # 有效退出：与运行时 evaluate 同一语义（含继承/保护来源说明）
        opted = self._membership.effective_optout(identity)
        reason = self._membership.optout_reason(identity) if opted else ""
        if opted:
            state = "已退出（off）" if "直接退出" in reason else f"已退出（{reason}）"
        elif not in_scope:
            state = f"当前{window}窗口不在管理员允许范围内（不采集）"
        else:
            state = "符合共享条件"
        stats = self._ledger.identity_stats(identity.key)
        if stats["last_turn_at"] is None:
            capture_line = "实际接管：尚无记录（开关开启不等于已实际接管）"
        else:
            capture_line = (
                f"实际接管：最近 {_fmt_ts(stats['last_turn_at'])}"
                f"（共 {stats['total_all']} 条记录）"
            )
        persona_line = (
            f"当前人格：{persona}（共享身份：该账号跨人格合并）"
            if identity.mode == MODE_USER
            else f"当前人格：{persona}"
        )
        return (
            f"📋 跨窗口上下文共享：{self._scope_line()}\n"
            f"当前窗口：{window}（{'在' if in_scope else '不在'}管理员允许范围内）\n"
            f"{persona_line}\n"
            f"个人状态：{state}\n"
            f"{capture_line}\n"
            f"有效历史：{stats['completed_current']} 轮已完成"
            f"（当前纪元 {stats['current_epoch']}"
            f"·代次 {stats['current_mode_generation']}）\n"
            "命令：/uctx off 退出 · /uctx on 加入 · /uctx reset 清空"
            f"（范围：{self._reset_scope_text(identity)}）"
        )

    async def reset(self, event: AstrMessageEvent) -> str:
        if self._unavailable_reason is not None or self._membership is None:
            return self._unavailable_text()
        try:
            identity, persona = await self._identity(event)
        except PersonaResolutionError:
            return self._identity_error_text()
        if not self._resolver.config.enabled:
            return "⚠️ 共享未启用，无需清空。"
        new_epoch = self._ledger.bump_epoch(identity.key)
        return (
            f"🧹 已清空{self._reset_scope_text(identity)}"
            f"（epoch 切换，当前纪元 {new_epoch}；进行中的旧请求不会回写，"
            "旧记录归档可查）。\n"
            "其他用户不受影响。原生 /reset、/new 在宿主执行成功时也会"
            "同步清空（等效本命令，范围一致）。"
        )

    async def off(self, event: AstrMessageEvent) -> str:
        if self._unavailable_reason is not None or self._membership is None:
            return self._unavailable_text()
        try:
            identity, persona = await self._identity(event)
        except PersonaResolutionError:
            return self._identity_error_text()
        self._membership.opt_out(identity)
        if identity.mode == MODE_USER:
            return (
                "🚪 已退出跨人格共享：该账号的全部人格从下一轮起不再读取、"
                "不再新增共享历史。\n已有数据保留。切换到 persona 模式后"
                "退出继续生效（保护现有与新增人格），直到你主动 /uctx on。"
            )
        return (
            f"🚪 已退出人格「{persona}」的跨窗口共享：从下一轮起不再读取、"
            "不再新增你的共享历史。\n已有数据保留（/uctx on 恢复）。"
        )

    async def on(self, event: AstrMessageEvent) -> str:
        if self._unavailable_reason is not None or self._membership is None:
            return self._unavailable_text()
        try:
            identity, persona = await self._identity(event)
        except PersonaResolutionError:
            return self._identity_error_text()
        if not self._resolver.config.enabled:
            return "⚠️ 共享未启用，无法加入。范围由管理员在插件配置中开启。"
        if identity.mode == MODE_USER:
            # user on：解除 user 键退出与基础保护；人格维度显式退出保留
            self._membership.opt_in_user(identity)
        else:
            # persona on：仅解除本人格（基础保护对其他人格继续生效）
            self._membership.opt_in_persona(identity)
        if not self._window_in_scope(event):
            return (
                "⚠️ 已取消退出标记，但当前窗口不在管理员允许范围内，"
                "仍不会共享。个人命令无法扩大管理员配置的范围。"
            )
        if identity.mode == MODE_USER:
            return (
                "✅ 已重新加入跨人格共享：该账号从下一轮起继续接续共享历史"
                "（停用期间的原生会话不会导入；此前对单个人格的显式退出"
                "仍然保留，回到 persona 模式时生效）。"
            )
        return (
            f"✅ 已重新加入人格「{persona}」的跨窗口共享：从下一轮起继续"
            "接续（停用期间的原生会话不会导入）。"
        )

    def scope(self) -> str:
        cfg = self._resolver.config
        if not cfg.enabled:
            return "🔒 跨窗口上下文共享当前关闭。"
        groups = "、".join(sorted(cfg.shared_groups)) or "（无）"
        private = "包含" if cfg.include_private else "不包含"
        return (
            "🔧 管理员配置的共享范围（只读，修改请前往插件配置）：\n"
            f"模式：{self._resolver.history_scope}\n"
            f"共享群：{groups}\n私聊：{private}\n"
            "范围外来源保持原生会话行为，不采集、不注入。"
        )
