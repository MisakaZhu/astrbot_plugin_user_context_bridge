"""来源范围与归属判定（ADR-006；W2 重定退出语义）。

原则：
- 插件默认关闭：未启用时不采集、不注入。
- 范围由管理员显式配置（受控来源）：群号白名单 + 是否含私聊。
- 个人退出（opt-out）只收窄自己的共享，永远不能扩大管理员范围。
- 退出判定只有**一份有效语义**（MembershipStore.effective_optout），
  运行时（evaluate）与命令/状态（commands）共用，不得各写一套。
- 归属以本轮事件的真实 sender 为准；以下一律不进入共享轮次：
  - 未唤醒的普通闲聊（is_wake 为假——正常不会走到 LLM 钩子，防御性排除）；
  - 机器人自身消息（sender == self_id）；
  - 管理命令（消息以 "/" 开头的命令语义）；
  - 无法识别发送者的事件。
- event.unified_msg_origin 与回复目标不在此处、也不在插件任何位置被修改。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType

from .identity import (
    MODE_USER,
    SCOPE_PERSONA_PREFIX,
    SCOPE_QUARANTINE_PREFIX,
    SCOPE_USER_TOKEN,
    SharedIdentity,
    build_identity,
    identity_from_event,
)

SUPPORTED_PLATFORM_NAMES = frozenset({"aiocqhttp"})
"""v1 支持的平台适配器类型（QQ OneBot v11）。其他类型不采集。"""

MEMBERSHIP_KEY_SCHEME = 3
"""membership.json 键编码版本：3 = scope 段带 p:/u: 前缀（W4）。"""


class MembershipError(Exception):
    """退出状态存储不可用（文件损坏/写入失败）。

    不得按"空退出集合"继续采集：调用方应禁用共享并给出可见诊断。
    """


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


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmpname, path)
    except BaseException:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def encode_membership_key(key: str) -> tuple[str, bool]:
    """旧 membership 键 → 新编码；返回 (新键或 None, 是否不可辨认)。

    - 裸人格 scope：加 ``p:`` 前缀（0.6.0 只有 persona 键）；
    - ``__mode_user__``：0.6.0 不存在 user 模式，该键在 0.6.0 文件中
      只可能是同名人格；但在 0.7.0 首个候选文件中又可能是 user 退出
      ——归属不可辨认。按安全方向处理：保留为同名 persona 的 ``p:``
      退出**并**追加基础身份保护（宁过度保护不误采集），
      由调用方追加 base_protected。
    """

    parts = key.split("\x1f")
    if len(parts) != 4:
        return key, False
    scope = parts[2]
    if (
        scope.startswith(SCOPE_PERSONA_PREFIX)
        or scope == SCOPE_USER_TOKEN
        or scope.startswith(SCOPE_QUARANTINE_PREFIX)
    ):
        return key, False  # 已是新编码
    if scope == "__mode_user__":
        return "\x1f".join((parts[0], parts[1], SCOPE_PERSONA_PREFIX + scope, parts[3])), True
    return "\x1f".join((parts[0], parts[1], SCOPE_PERSONA_PREFIX + scope, parts[3])), False


class MembershipStore:
    """个人退出状态的持久化（JSON 文件，线程安全；W2/W4 修订）。

    退出语义（唯一权威：:meth:`effective_optout`）：
    - persona 模式：本人格键退出，或基础身份被保护且本人格未显式 on；
    - user 模式：user 键退出，或基础身份存在任何 persona 退出/保护
      （转换写入 + 防御兜底）。

    转换只做**加法**（optout/保护只增不删），崩溃后重放幂等；
    主动 on 才做定向解除（user on 解除 user 键退出与基础保护；
    persona on 仅给本人格记 persona_on）。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._optout: set[str] = set()
        self._base_protected: set[str] = set()
        self._persona_on: set[str] = set()
        self._ambiguous: list[str] = []
        self._load()

    # -- 载入与持久化 ------------------------------------------------------
    def _load(self) -> None:
        self._base_protected = set()
        self._persona_on = set()
        self._ambiguous = []
        if not self._path.exists():
            self._optout = set()
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("membership.json 顶层不是对象")
            self._optout = {str(k) for k in data.get("optout", [])}
            self._base_protected = {str(k) for k in data.get("base_protected", [])}
            self._persona_on = {str(k) for k in data.get("persona_on", [])}
        except Exception as exc:  # noqa: BLE001 - 损坏文件必须受控失败
            raise MembershipError(
                f"退出状态文件损坏或不可读：{self._path}（{exc}）。"
                "为避免按空退出集合误采集，已拒绝加载；请人工检查或删除该文件"
                "（删除后所有用户视为未退出，请谨慎操作）。"
            ) from exc

    def _save(self) -> None:
        payload = json.dumps(
            {
                "key_scheme": MEMBERSHIP_KEY_SCHEME,
                "optout": sorted(self._optout),
                "base_protected": sorted(self._base_protected),
                "persona_on": sorted(self._persona_on),
                "ambiguous_legacy": sorted(self._ambiguous),
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            _atomic_write_text(self._path, payload)
        except OSError as exc:
            raise MembershipError(f"退出状态写入失败：{exc}") from exc

    def migrate_legacy_keys(self) -> bool:
        """0.6.0/旧候选键 → scheme 3（幂等；先备份再原子改写）。

        返回是否发生了改写。不可辨认键的处置见 encode_membership_key。
        """

        with self._lock:
            try:
                data = (
                    json.loads(self._path.read_text(encoding="utf-8"))
                    if self._path.exists()
                    else {}
                )
            except Exception as exc:  # noqa: BLE001
                raise MembershipError(
                    f"退出状态文件损坏或不可读：{self._path}（{exc}）"
                ) from exc
            if isinstance(data, dict) and data.get("key_scheme") == MEMBERSHIP_KEY_SCHEME:
                return False
            new_optout: set[str] = set()
            new_base: set[str] = set(self._base_protected)
            new_on: set[str] = set()
            ambiguous: list[str] = []
            for key in self._optout:
                new_key, is_ambiguous = encode_membership_key(key)
                if is_ambiguous:
                    ambiguous.append(key)
                    new_optout.add(new_key)
                    new_base.add(_base_of_raw_key(key))
                else:
                    new_optout.add(new_key)
            for key in self._persona_on:
                new_key, is_ambiguous = encode_membership_key(key)
                if is_ambiguous:
                    # on 是解除标记：不可辨认时丢弃（欠解除安全，可重新 on）
                    ambiguous.append(key)
                    continue
                new_on.add(new_key)
            self._optout = new_optout
            self._base_protected = new_base
            self._persona_on = new_on
            self._ambiguous = ambiguous
            if self._path.exists():
                backup = self._path.with_name(
                    self._path.name + ".pre-scheme3.bak"
                )
                if not backup.exists():
                    backup.write_bytes(self._path.read_bytes())
            self._save()
            return True

    # -- 基础键 ------------------------------------------------------------
    @staticmethod
    def base_key_of(identity: "SharedIdentity") -> str:
        """基础身份键（platform\\x1fself\\x1fsender，剥离 scope）。"""

        return "\x1f".join(
            (identity.platform_id, identity.self_id, identity.sender_id)
        )

    # -- 查询（运行时与命令唯一语义） ---------------------------------------
    def _has_any_persona_optout_locked(self, identity: SharedIdentity) -> bool:
        platform, self_id, sender = (
            identity.platform_id,
            identity.self_id,
            identity.sender_id,
        )
        for k in self._optout:
            parts = k.split("\x1f")
            if (
                len(parts) == 4
                and parts[0] == platform
                and parts[1] == self_id
                and parts[3] == sender
                and parts[2] != SCOPE_USER_TOKEN
            ):
                return True
        return False

    def has_any_persona_optout(self, identity: "SharedIdentity") -> bool:
        with self._lock:
            return self._has_any_persona_optout_locked(identity)

    def is_base_protected(self, identity: "SharedIdentity") -> bool:
        with self._lock:
            return self.base_key_of(identity) in self._base_protected

    def persona_explicit_on(self, identity: "SharedIdentity") -> bool:
        with self._lock:
            return identity.key in self._persona_on

    def is_opted_out(self, identity: SharedIdentity) -> bool:
        """直接退出标记查询（仅查键，不含继承/保护推导；有效判定用
        :meth:`effective_optout`）。"""

        with self._lock:
            return identity.key in self._optout

    def effective_optout(self, identity: SharedIdentity) -> bool:
        """有效退出判定（evaluate/status/命令唯一语义，W2/W5）。

        - persona 模式：本人格键退出，或基础身份被保护且本人格未显式 on
          （user off→persona 的保护覆盖该账号全部人格，单人格 on 只
          解除本人格）；
        - user 模式：user 键退出，或基础身份被保护。**不**直接按
          "存在 persona 退出"阻断——persona→user 转换会把退出写成
          user 键（命中直接退出支）；否则 persona 显式 off 的用户在
          user 模式主动 on 后将永远无法恢复。
        """

        with self._lock:
            if identity.key in self._optout:
                return True
            if not self.base_key_of(identity) in self._base_protected:
                return False
            if identity.mode == MODE_USER:
                return True
            return identity.key not in self._persona_on

    def optout_reason(self, identity: SharedIdentity) -> str:
        """有效退出的来源（status 诊断用；未退出返回空串）。"""

        with self._lock:
            if identity.key in self._optout:
                return (
                    "user-key 直接退出" if identity.mode == MODE_USER else "本人格退出"
                )
            if identity.mode == MODE_USER:
                if self._has_any_persona_optout_locked(identity):
                    return "该账号存在人格维度退出（继承）"
                if self.base_key_of(identity) in self._base_protected:
                    return "该账号受基础身份退出保护（继承）"
                return ""
            if self.base_key_of(identity) in self._base_protected:
                if identity.key not in self._persona_on:
                    return "该账号受基础身份退出保护（user 退出继承）"
            return ""

    # -- 写入（命令与转换） -------------------------------------------------
    def opt_out(self, identity: SharedIdentity) -> None:
        with self._lock:
            self._optout.add(identity.key)
            self._save()

    def opt_in_persona(self, identity: SharedIdentity) -> None:
        """persona 模式主动 on：解除本人格退出，并记录显式 on
        （从基础保护中释放**仅本人格**，不影响其他人格与既有保护）。"""

        with self._lock:
            self._optout.discard(identity.key)
            self._persona_on.add(identity.key)
            self._save()

    def opt_in_user(self, identity: SharedIdentity) -> None:
        """user 模式主动 on：解除 user 键退出与该基础身份保护。

        人格维度的显式退出（p: 键）保留——回到 persona 模式时其本人
        的显式 off 依然生效，不被静默清除。
        """

        with self._lock:
            self._optout.discard(identity.key)
            self._base_protected.discard(self.base_key_of(identity))
            self._save()

    # 兼容别名（旧测试/调用逐步迁移）
    def opt_in(self, identity: SharedIdentity) -> None:
        if identity.mode == MODE_USER:
            self.opt_in_user(identity)
        else:
            self.opt_in_persona(identity)

    def protect_base(self, identity: "SharedIdentity") -> None:
        """user→persona 转换：基础身份整体进入退出保护（幂等加法）。"""

        with self._lock:
            self._base_protected.add(self.base_key_of(identity))
            self._save()

    def release_base(self, identity: "SharedIdentity") -> None:
        """该 persona 显式 on：仅解除该人格（其余人格保护保留）。"""

        with self._lock:
            self._persona_on.add(identity.key)
            self._save()

    def raw_sets(self) -> tuple[set[str], set[str], set[str]]:
        """测试/迁移自省用：(optout, base_protected, persona_on)。"""

        with self._lock:
            return (set(self._optout), set(self._base_protected), set(self._persona_on))


def _base_of_raw_key(key: str) -> str:
    parts = key.split("\x1f")
    if len(parts) != 4:
        return key
    return "\x1f".join((parts[0], parts[1], parts[3]))


class ScopeResolver:
    """判定一轮事件是否纳入共享，并给出共享身份。"""

    def __init__(
        self,
        config: ScopeConfig,
        membership: MembershipStore | None = None,
        history_scope: str = "persona",
    ) -> None:
        self._config = config
        self._membership = membership
        self._history_scope = MODE_USER if history_scope == MODE_USER else "persona"

    @property
    def config(self) -> ScopeConfig:
        return self._config

    @property
    def history_scope(self) -> str:
        return self._history_scope

    def set_history_scope(self, scope: str) -> None:
        self._history_scope = MODE_USER if scope == MODE_USER else "persona"

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

        identity = identity_from_event(event, persona_scope, mode=self._history_scope)

        if cfg.shared_personas and (
            identity.persona_scope not in cfg.shared_personas
            and self._history_scope != MODE_USER
        ):
            return out("persona_not_in_scope", identity)

        if self._membership is not None and self._membership.effective_optout(identity):
            return out("personal_optout", identity)

        return ScopeDecision(in_scope=True, identity=identity, reason="ok")
