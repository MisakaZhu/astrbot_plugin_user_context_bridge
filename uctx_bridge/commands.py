"""/uctx 中文管理命令（ADR-006/008）。

命令组（README 固定）：
    /uctx status  查看本人的共享状态（不显示他人标识与聊天正文）
    /uctx reset   清空本人的共享历史（epoch 切换；旧慢请求不回写）
    /uctx off     退出共享（停止读取与新增；不删除已有数据）
    /uctx on      重新加入共享（不导入停用期间的原生会话）
    /uctx scope   查看管理员配置的共享范围（只读）

本人只能操作自己的共享历史；范围由管理员在插件配置中维护，
个人命令不能扩大范围。原生 /reset、/new 只作用于宿主窗口会话，
与共享历史的关系见 README「原生命令语义」。
"""

from __future__ import annotations

import sqlite3

from astrbot.api.event import AstrMessageEvent

from .identity import SharedIdentity, build_identity, resolve_persona_scope
from .ledger import TurnLedger
from .scope import MembershipStore, ScopeResolver


class _FakeConv:
    """仅携带 persona_id 的会话替身（供 resolve_selected_persona 同参调用）。"""

    def __init__(self, persona_id: str | None) -> None:
        self.persona_id = persona_id


def _identity_of(event: AstrMessageEvent, persona_scope: str | None) -> SharedIdentity:
    return build_identity(
        platform_id=str(event.get_platform_id() or ""),
        self_id=str(event.get_self_id() or ""),
        persona_scope=persona_scope,
        sender_id=str(event.get_sender_id() or ""),
    )


class CommandService:
    def __init__(
        self,
        *,
        ledger: TurnLedger,
        resolver: ScopeResolver,
        membership: MembershipStore,
        persona_manager_getter,
        conversation_manager_getter=None,
    ) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._membership = membership
        self._get_persona_manager = persona_manager_getter
        self._get_conversation_manager = conversation_manager_getter

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

    async def _identity(self, event: AstrMessageEvent) -> SharedIdentity:
        conversation_persona_id = await self._current_persona_id(event)
        persona_scope = await resolve_persona_scope(
            self._get_persona_manager(), event, _FakeConv(conversation_persona_id)
        )
        return _identity_of(event, persona_scope)

    def _turn_stats(self, identity: SharedIdentity) -> dict[str, int]:
        conn = sqlite3.connect(self._ledger._db_path)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM turns WHERE identity_key=?"
                " GROUP BY status",
                (identity.key,),
            ).fetchall()
            return {status: count for status, count in rows}
        finally:
            conn.close()

    def _window_in_scope(self, event: AstrMessageEvent) -> bool:
        """命令场景的窗口范围判定（群白名单/私聊开关；不含对话轮次过滤）。"""

        cfg = self._resolver.config
        if not cfg.enabled:
            return False
        group_id = str(event.get_group_id() or "")
        if group_id:
            return group_id in cfg.shared_groups
        return cfg.include_private

    # -- 子命令 -----------------------------------------------------------
    async def status(self, event: AstrMessageEvent) -> str:
        identity = await self._identity(event)
        stats = self._turn_stats(identity)
        window = "群聊" if event.get_group_id() else "私聊"
        enabled = self._resolver.config.enabled
        scope_desc = (
            f"启用（共享群 {len(self._resolver.config.shared_groups)} 个，"
            f"私聊{'含' if self._resolver.config.include_private else '不含'}）"
            if enabled
            else "关闭"
        )
        if not enabled:
            return (
                f"📋 跨窗口上下文共享：{scope_desc}\n"
                "当前不会采集或共享任何消息。范围由管理员在插件配置中开启。"
            )
        if self._membership.is_opted_out(identity):
            state = "已退出（off）"
        elif self._window_in_scope(event):
            state = "共享中"
        else:
            state = f"当前{window}窗口不在管理员允许范围内"
        total = sum(stats.values())
        completed = stats.get("completed", 0)
        return (
            f"📋 跨窗口上下文共享：{scope_desc}\n"
            f"你的状态：{state}（当前窗口：{window}）\n"
            f"你的共享历史：{completed} 轮已完成 / 共 {total} 条轮次记录\n"
            "命令：/uctx off 退出 · /uctx on 加入 · /uctx reset 清空"
        )

    async def reset(self, event: AstrMessageEvent) -> str:
        identity = await self._identity(event)
        if not self._resolver.config.enabled:
            return "⚠️ 共享未启用，无需清空。"
        new_epoch = self._ledger.bump_epoch(identity.key)
        return (
            "🧹 已清空你的跨窗口共享历史（epoch 切换，进行中的旧请求不会回写）。\n"
            f"影响范围仅限你本人（当前纪元 {new_epoch}）；其他用户不受影响。\n"
            "注意：原生 /reset、/new 只重置当前窗口的宿主会话，"
            "不会清空共享历史；清空共享请使用 /uctx reset。"
        )

    async def off(self, event: AstrMessageEvent) -> str:
        identity = await self._identity(event)
        self._membership.opt_out(identity)
        return (
            "🚪 已退出跨窗口共享：从下一轮起不再读取、不再新增你的共享历史。\n"
            "已有数据保留（可用 /uctx on 恢复接续；重新启用不会导入"
            "停用期间各窗口的原生会话）。"
        )

    async def on(self, event: AstrMessageEvent) -> str:
        identity = await self._identity(event)
        if not self._resolver.config.enabled:
            return "⚠️ 共享未启用，无法加入。范围由管理员在插件配置中开启。"
        self._membership.opt_in(identity)
        if not self._window_in_scope(event):
            return (
                "⚠️ 已取消退出标记，但当前窗口不在管理员允许范围内，"
                "仍不会共享。个人命令无法扩大管理员配置的范围。"
            )
        return (
            "✅ 已重新加入跨窗口共享：从下一轮起继续接续你之前的共享历史"
            "（停用期间的原生会话不会导入）。"
        )

    def scope(self) -> str:
        cfg = self._resolver.config
        if not cfg.enabled:
            return "🔒 跨窗口上下文共享当前关闭。"
        groups = "、".join(sorted(cfg.shared_groups)) or "（无）"
        private = "包含" if cfg.include_private else "不包含"
        return (
            "🔧 管理员配置的共享范围（只读，修改请前往插件配置）：\n"
            f"共享群：{groups}\n私聊：{private}\n"
            "范围外来源保持原生会话行为，不采集、不注入。"
        )
