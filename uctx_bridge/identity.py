"""共享身份：键计算与人格范围解析（ADR-006）。

共享身份 = (platform_id, self_id, persona_scope, sender_id)。
- platform_id：平台适配器实例 ID（同类型多实例互不混淆）。
- self_id：机器人自身 QQ 号（同一 QQ 平台上不同机器人互不混淆）。
- persona_scope：宿主解析出的最终生效人格 ID；无人格时使用稳定常量
  ``__default__``（宿主 resolve_selected_persona 总会给出 "default"，
  常量仅作极端兜底，保证默认人格也有稳定身份值）。
- sender_id：本轮消息的真实发送者 QQ 号（绝不由昵称或全群历史推断）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api.event import AstrMessageEvent

# 分隔符选用控制字符，避免与 QQ 号 / 平台 ID / 人格名冲突
_KEY_SEP = "\x1f"

DEFAULT_PERSONA_SCOPE = "__default__"


@dataclass(frozen=True)
class SharedIdentity:
    """一个可共享对话历史的身份（不同用户/机器人/人格天然隔离）。"""

    platform_id: str
    self_id: str
    persona_scope: str
    sender_id: str

    @property
    def key(self) -> str:
        """存储层主键形态。"""

        return _KEY_SEP.join(
            (self.platform_id, self.self_id, self.persona_scope, self.sender_id)
        )

    @classmethod
    def from_key(cls, key: str) -> "SharedIdentity":
        parts = key.split(_KEY_SEP)
        if len(parts) != 4:
            raise ValueError(f"非法共享身份键：{key!r}")
        return cls(*parts)


def build_identity(
    *,
    platform_id: str,
    self_id: str,
    persona_scope: str | None,
    sender_id: str,
) -> SharedIdentity:
    """构建共享身份；人格范围为空时落稳定默认值。"""

    scope = (persona_scope or "").strip() or DEFAULT_PERSONA_SCOPE
    return SharedIdentity(
        platform_id=platform_id or "",
        self_id=self_id or "",
        persona_scope=scope,
        sender_id=sender_id or "",
    )


MODE_USER_SCOPE = "__mode_user__"
"""user 模式共享键的保留 scope 字面量（ADR-015）：第三段为模式标记而非
人格名；双下划线保留样式，真实人格不应使用（若使用将在 user 模式下
与跨人格共享键合并，属管理员配置错误，文档已声明）。"""


def identity_from_event(
    event: AstrMessageEvent,
    persona_scope: str | None,
) -> SharedIdentity:
    """从本轮事件提取共享身份。

    只读取事件的平台实例、机器人 self_id 与真实 sender；不修改
    event.unified_msg_origin（消息路由永不改变）。
    """

    return build_identity(
        platform_id=str(event.get_platform_id() or ""),
        self_id=str(event.get_self_id() or ""),
        persona_scope=persona_scope,
        sender_id=str(event.get_sender_id() or ""),
    )


class PersonaResolutionError(Exception):
    """人格解析失败（T1：受控失败，不得折叠成默认共享身份掩盖异常）。"""


async def resolve_persona_scope(
    persona_manager: Any,
    event: AstrMessageEvent,
    conversation: Any,
    provider_settings: dict | None = None,
) -> str:
    """与宿主 build 阶段同参调用 persona_manager，取最终生效人格 ID。

    与 astrbot.core.astr_main_agent._ensure_persona_and_skills 一致：
    umo 会话覆盖 → conversation.persona_id → provider_settings.default_personality
    （4.26 在 conversation.persona_id 为 None 时**只**从该参数读取默认人格，
    漏传会把不同人格全部折叠为 None——T1 根因）。
    解析失败或结果为空时抛 :class:`PersonaResolutionError`，由调用方
    决定受控行为（对话轮不接管、命令报错），不得静默合并身份。
    """

    conversation_persona_id = getattr(conversation, "persona_id", None)
    try:
        result = await persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=conversation_persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=provider_settings,
        )
        persona_id = result[0] if result else None
    except Exception as exc:  # noqa: BLE001
        raise PersonaResolutionError(f"人格解析异常：{exc}") from exc
    persona_id = (persona_id or "").strip()
    if not persona_id or persona_id in ("None", "[%None]"):
        raise PersonaResolutionError(
            "人格解析结果为空（检查 provider_settings.default_personality "
            "与当前会话人格配置）"
        )
    return persona_id
