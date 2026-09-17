"""来源范围与归属判定（ADR-006）。

原则：
- 插件默认关闭：未启用时不采集、不注入。
- 范围由管理员显式配置（受控来源）：群号白名单 + 是否含私聊。
- 个人退出（opt-out）只收窄自己的共享，永远不能扩大管理员范围。
- 归属以本轮事件的真实 sender 为准；以下一律不进入共享轮次：
  - 未唤醒的普通闲聊（is_wake 为假——正常不会走到 LLM 钩子，防御性排除）；
  - 机器人自身消息（sender == self_id）；
  - 管理命令（消息以 "/" 开头的命令语义）；
  - 无法识别发送者的事件。
- event.unified_msg_origin 与回复目标不在此处、也不在插件任何位置被修改。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType

from .identity import SharedIdentity, identity_from_event

SUPPORTED_PLATFORM_NAMES = frozenset({"aiocqhttp"})
"""v1 支持的平台适配器类型（QQ OneBot v11）。其他类型不采集。"""


@dataclass(frozen=True)
class ScopeConfig:
    """管理员配置的受控来源范围。"""

    enabled: bool = False
    shared_groups: frozenset[str] = frozenset()
    include_private: bool = False
    shared_personas: frozenset[str] = frozenset()
    """可选：限定参与共享的人格；空集表示不按人格过滤（人格仍是身份维度）。"""

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "ScopeConfig":
        data = data or {}
        groups = data.get("shared_groups") or []
        personas = data.get("shared_personas") or []
        return cls(
            enabled=bool(data.get("enabled", False)),
            shared_groups=frozenset(str(g).strip() for g in groups if str(g).strip()),
            include_private=bool(data.get("include_private", False)),
            shared_personas=frozenset(
                str(p).strip() for p in personas if str(p).strip()
            ),
        )


@dataclass(frozen=True)
class ScopeDecision:
    """范围判定的结论与原因（原因用于日志与测试，不进入回复）。"""

    in_scope: bool
    identity: SharedIdentity | None = None
    reason: str = ""

    @property
    def umo_source_type(self) -> str:
        return self.reason


class MembershipStore:
    """个人退出状态的持久化（JSON 文件，线程安全）。

    记录按共享身份键退出共享的用户集合。P2 引入 SQLite 后此状态
    迁移至账本 meta 表；接口保持不变。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._optout: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._optout = {str(k) for k in data.get("optout", [])}
        except Exception:  # noqa: BLE001 - 损坏文件按空处理
            self._optout = set()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"optout": sorted(self._optout)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_opted_out(self, identity: SharedIdentity) -> bool:
        with self._lock:
            return identity.key in self._optout

    def opt_out(self, identity: SharedIdentity) -> None:
        with self._lock:
            self._optout.add(identity.key)
            self._save()

    def opt_in(self, identity: SharedIdentity) -> None:
        with self._lock:
            self._optout.discard(identity.key)
            self._save()


class ScopeResolver:
    """判定一轮事件是否纳入共享，并给出共享身份。"""

    def __init__(
        self,
        config: ScopeConfig,
        membership: MembershipStore | None = None,
    ) -> None:
        self._config = config
        self._membership = membership

    @property
    def config(self) -> ScopeConfig:
        return self._config

    def update_config(self, config: ScopeConfig) -> None:
        self._config = config

    def evaluate(
        self,
        event: AstrMessageEvent,
        persona_scope: str | None,
    ) -> ScopeDecision:
        cfg = self._config

        def out(reason: str, identity: SharedIdentity | None = None) -> ScopeDecision:
            return ScopeDecision(in_scope=False, identity=identity, reason=reason)

        if not cfg.enabled:
            return out("disabled")
        if event.get_platform_name() not in SUPPORTED_PLATFORM_NAMES:
            return out("unsupported_platform")

        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not sender_id:
            return out("empty_sender")
        if sender_id == self_id:
            return out("bot_self_message")

        message_type = event.get_message_type()
        is_group = message_type == MessageType.GROUP_MESSAGE
        if is_group:
            group_id = str(event.get_group_id() or "")
            if not group_id:
                return out("group_without_id")
            if group_id not in cfg.shared_groups:
                return out("group_not_in_scope", None)
        elif not cfg.include_private:
            return out("private_not_in_scope")

        # 管理命令不进入共享轮次（命令由宿主/插件命令路径处理，不应成为个人历史）
        message_str = (event.message_str or "").strip()
        if message_str.startswith("/"):
            return out("command_message")

        # 未唤醒的普通闲聊防御性排除（正常不达 LLM 钩子）
        if not event.is_wake:
            return out("not_wake")

        identity = identity_from_event(event, persona_scope)

        if cfg.shared_personas and identity.persona_scope not in cfg.shared_personas:
            return out("persona_not_in_scope", identity)

        if self._membership is not None and self._membership.is_opted_out(identity):
            return out("personal_optout", identity)

        return ScopeDecision(in_scope=True, identity=identity, reason="ok")
