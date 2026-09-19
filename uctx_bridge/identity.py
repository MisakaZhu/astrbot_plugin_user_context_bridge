"""共享身份：键计算与人格范围解析（ADR-006；W4 重定键编码）。

共享身份 = (platform_id, self_id, scope 段, sender_id)。
- platform_id：平台适配器实例 ID（同类型多实例互不混淆）。
- self_id：机器人自身 QQ 号（同一 QQ 平台上不同机器人互不混淆）。
- scope 段：**带模式前缀的结构化编码**（W4，取代 0.7.0 首个候选的
  ``__mode_user__`` 保留字方案——该方案已被独立验收证实会与真实人格
  ID 碰撞）：
    - persona 模式：``p:<persona_id>``
    - user 模式：``u:``（无 payload）
    - ``q:<原值>``：迁移无法安全辨认归属的旧候选数据（隔离，不注入）
  两个前缀首字符不同（p/u/q 互异且必带冒号），user 令牌为定长两字符，
  任何 persona_id 的编码都以 ``p:`` 开头——三集合两两不相交，与宿主
  允许的任意人格 ID（含下划线/冒号等特殊字符）结构性无碰撞。
  0.6.0 的裸人格键由迁移统一改写为 ``p:`` 前缀（见 ledger.migrate_legacy）。
- sender_id：本轮消息的真实发送者 QQ 号（绝不由昵称或全群历史推断）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api.event import AstrMessageEvent

# 分隔符选用控制字符，避免与 QQ 号 / 平台 ID / 人格名冲突
_KEY_SEP = "\x1f"

DEFAULT_PERSONA_SCOPE = "__default__"

SCOPE_USER_TOKEN = "u:"
"""user 模式 scope 段定长令牌（W4）。任何 persona 编码都以 ``p:`` 开头，
与之结构性不相交；不再使用可被真实人格占用的普通字符串。"""

SCOPE_PERSONA_PREFIX = "p:"
"""persona 模式 scope 段前缀：``p:<persona_id>``。"""

SCOPE_QUARANTINE_PREFIX = "q:"
"""迁移隔离前缀：0.7.0 首个候选库中 ``__mode_user__`` 键无法区分
"真实同名人格"与"user 模式记录"，一律改写为 q: 保留原数据但不注入
任何模式的有效历史（ADR-015 W4 恢复规则）。"""

MODE_PERSONA = "persona"
MODE_USER = "user"


@dataclass(frozen=True)
class SharedIdentity:
    """一个可共享对话历史的身份（不同用户/机器人/人格天然隔离）。"""

    platform_id: str
    self_id: str
    persona_scope: str
    sender_id: str
    mode: str = MODE_PERSONA
    """共享模式：``persona``（按人格隔离）| ``user``（跨人格）。
    ``quarantine``/``legacy_persona`` 仅出现在 from_key 解码结果中，
    不由 build_identity 构造。"""

    @property
    def scope_token(self) -> str:
        if self.mode == MODE_USER:
            return SCOPE_USER_TOKEN
        return SCOPE_PERSONA_PREFIX + self.persona_scope

    @property
    def key(self) -> str:
        """存储层主键形态（scope 段带模式前缀，W4）。"""

        return _KEY_SEP.join(
            (self.platform_id, self.self_id, self.scope_token, self.sender_id)
        )

    @classmethod
    def from_key(cls, key: str) -> "SharedIdentity":
        parts = key.split(_KEY_SEP)
        if len(parts) != 4:
            raise ValueError(f"非法共享身份键：{key!r}")
        token = parts[2]
        if token == SCOPE_USER_TOKEN:
            return cls(parts[0], parts[1], "", parts[3], mode=MODE_USER)
        if token.startswith(SCOPE_PERSONA_PREFIX):
            return cls(
                parts[0], parts[1], token[len(SCOPE_PERSONA_PREFIX):], parts[3],
                mode=MODE_PERSONA,
            )
        if token.startswith(SCOPE_QUARANTINE_PREFIX):
            return cls(
                parts[0], parts[1], token[len(SCOPE_QUARANTINE_PREFIX):], parts[3],
                mode="quarantine",
            )
        # 迁移前的裸人格键（v1/旧候选时代）；迁移后正常不再出现，
        # 保留解码以便导出工具对未迁移库给出明确标注
        return cls(parts[0], parts[1], token, parts[3], mode="legacy_persona")


def build_identity(
    *,
    platform_id: str,
    self_id: str,
    persona_scope: str | None,
    sender_id: str,
    mode: str = MODE_PERSONA,
) -> SharedIdentity:
    """构建共享身份；人格范围为空时落稳定默认值。

    user 模式忽略 persona_scope（scope 段为定长 ``u:`` 令牌）。
    """

    if mode == MODE_USER:
        return SharedIdentity(
            platform_id=platform_id or "",
            self_id=self_id or "",
            persona_scope="",
            sender_id=sender_id or "",
            mode=MODE_USER,
        )
    scope = (persona_scope or "").strip() or DEFAULT_PERSONA_SCOPE
    return SharedIdentity(
        platform_id=platform_id or "",
        self_id=self_id or "",
        persona_scope=scope,
        sender_id=sender_id or "",
        mode=MODE_PERSONA,
    )


def base_key_of_key(identity_key: str) -> str:
    """基础身份键（platform\\x1fself\\x1fsender，剥离 scope 段）。

    与 uctx_bridge.ledger / scope.MembershipStore 的基础键一致：
    mode_generation、scope_mode、base_protected 均按基础身份存取。
    """

    parts = identity_key.split(_KEY_SEP)
    if len(parts) != 4:
        return identity_key
    return _KEY_SEP.join((parts[0], parts[1], parts[3]))


def identity_from_event(
    event: AstrMessageEvent,
    persona_scope: str | None,
    mode: str = MODE_PERSONA,
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
        mode=mode,
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
