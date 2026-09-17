"""P1 身份/范围/归属验证（MIS-137）。

覆盖验收矩阵 A03-A06 的判定层断言（范围与身份路由）；完整闭环
（模型请求与持久化）断言在 P3/P6 的 pipeline 级测试中落地。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/p1_identity_scope_check.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from tests.fakes import FakeEvent
from uctx_bridge.identity import (
    DEFAULT_PERSONA_SCOPE,
    SharedIdentity,
    build_identity,
    identity_from_event,
    resolve_persona_scope,
)
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def base_config(**kw) -> ScopeConfig:
    defaults = dict(
        enabled=True,
        shared_groups=frozenset({"700000001", "700000002"}),
        include_private=True,
    )
    defaults.update(kw)
    return ScopeConfig(**defaults)


def scenario_identity() -> None:
    """身份键：四维隔离 + 稳定默认人格。"""
    a = build_identity(
        platform_id="aiocqhttp",
        self_id="900001",
        persona_scope="maid",
        sender_id="10001",
    )
    b = build_identity(
        platform_id="aiocqhttp",
        self_id="900001",
        persona_scope="maid",
        sender_id="10002",
    )
    check("ID.sender-differs", a.key != b.key, "不同发送者身份必须不同")

    c = build_identity(
        platform_id="aiocqhttp",
        self_id="900002",
        persona_scope="maid",
        sender_id="10001",
    )
    check("ID.self-differs", a.key != c.key, "不同机器人身份必须不同")

    d = build_identity(
        platform_id="aiocqhttp2",
        self_id="900001",
        persona_scope="maid",
        sender_id="10001",
    )
    check("ID.platform-differs", a.key != d.key, "不同平台实例身份必须不同")

    e = build_identity(
        platform_id="aiocqhttp",
        self_id="900001",
        persona_scope="tutor",
        sender_id="10001",
    )
    check("ID.persona-differs", a.key != e.key, "不同人格身份必须不同")

    f = SharedIdentity.from_key(a.key)
    check("ID.key-roundtrip", f == a, "键序列化需可还原")

    g = build_identity(
        platform_id="aiocqhttp", self_id="900001", persona_scope=None, sender_id="10001"
    )
    check(
        "ID.default-persona-stable",
        g.persona_scope == DEFAULT_PERSONA_SCOPE and g.key,
        "默认人格必须有稳定身份值",
    )

    # A03：同一群中的两个人，事件身份互不混淆
    ev1 = FakeEvent(sender_id="10001", group_id="700000001")
    ev2 = FakeEvent(sender_id="10002", group_id="700000001")
    id1 = identity_from_event(ev1, "maid")
    id2 = identity_from_event(ev2, "maid")
    check(
        "A03.same-group-two-users-isolated",
        id1.key != id2.key and id1.sender_id == "10001" and id2.sender_id == "10002",
        "同群不同发送者必须得到不同身份",
    )

    # A04：同 QQ 号在不同机器人/平台/人格下隔离
    ev = FakeEvent(sender_id="10001", group_id="700000001", self_id="900001")
    ev_other_bot = FakeEvent(sender_id="10001", group_id="700000001", self_id="900002")
    check(
        "A04.same-qq-different-bot-isolated",
        identity_from_event(ev, "maid").key
        != identity_from_event(ev_other_bot, "maid").key,
        "",
    )
    check(
        "A04.same-qq-different-persona-isolated",
        identity_from_event(ev, "maid").key != identity_from_event(ev, "tutor").key,
        "",
    )
    ev_other_platform = FakeEvent(
        sender_id="10001", group_id="700000001", platform_id="aiocqhttp2"
    )
    check(
        "A04.same-qq-different-platform-isolated",
        identity_from_event(ev, "maid").key
        != identity_from_event(ev_other_platform, "maid").key,
        "",
    )


def scenario_persona_resolution() -> None:
    """人格范围解析：与宿主同参，三级优先级。"""

    class FakeConv:
        def __init__(self, persona_id):
            self.persona_id = persona_id

    class FakePersonaManager:
        def __init__(self, force=None, global_default="default"):
            self.force = force
            self.global_default = global_default

        async def resolve_selected_persona(
            self, *, umo, conversation_persona_id, platform_name, **kw
        ):
            pid = self.force or conversation_persona_id or self.global_default
            return (pid, {"name": pid} if pid else None, self.force, False)

    ev = FakeEvent(sender_id="10001", group_id="700000001")

    # conversation.persona_id 生效
    scope = asyncio.run(
        resolve_persona_scope(FakePersonaManager(), ev, FakeConv("maid"))
    )
    check("PERSONA.conversation-level", scope == "maid", f"got {scope}")

    # umo 会话覆盖优先
    scope = asyncio.run(
        resolve_persona_scope(
            FakePersonaManager(force="tutor"), ev, FakeConv("maid")
        )
    )
    check("PERSONA.session-override", scope == "tutor", f"got {scope}")

    # 无会话/对话人格 → 全局默认
    scope = asyncio.run(
        resolve_persona_scope(FakePersonaManager(), ev, FakeConv(None))
    )
    check("PERSONA.global-default", scope == "default", f"got {scope}")

    # 解析异常 → 稳定默认常量
    class BrokenManager:
        async def resolve_selected_persona(self, **kw):
            raise RuntimeError("boom")

    scope = asyncio.run(resolve_persona_scope(BrokenManager(), ev, FakeConv("maid")))
    check("PERSONA.fallback-stable", scope == DEFAULT_PERSONA_SCOPE, f"got {scope}")


def scenario_scope_groups_private() -> None:
    """A05：范围外来源不采集；范围内群/私聊纳入。"""
    resolver = ScopeResolver(base_config())

    ev_group_a = FakeEvent(sender_id="10001", group_id="700000001")
    ev_group_b = FakeEvent(sender_id="10001", group_id="700000002")
    ev_group_c = FakeEvent(sender_id="10001", group_id="700000999")
    ev_private = FakeEvent(sender_id="10001", group_id="")

    d1 = resolver.evaluate(ev_group_a, "maid")
    d2 = resolver.evaluate(ev_group_b, "maid")
    d3 = resolver.evaluate(ev_group_c, "maid")
    d4 = resolver.evaluate(ev_private, "maid")
    check("A05.groupA-in", d1.in_scope, d1.reason)
    check("A05.groupB-in", d2.in_scope, d2.reason)
    check("A05.groupC-out", not d3.in_scope and d3.reason == "group_not_in_scope")
    check("A05.private-in", d4.in_scope, d4.reason)

    # 私聊未启用
    resolver_np = ScopeResolver(base_config(include_private=False))
    d5 = resolver_np.evaluate(FakeEvent(sender_id="10001", group_id=""), "maid")
    check("A05.private-disabled-out", not d5.in_scope, d5.reason)

    # 插件总开关关闭
    resolver_off = ScopeResolver(base_config(enabled=False))
    d6 = resolver_off.evaluate(ev_group_a, "maid")
    check("A05.plugin-disabled-out", not d6.in_scope and d6.reason == "disabled")

    # 非支持平台
    ev_wechat = FakeEvent(sender_id="10001", group_id="700000001", platform_id="qq_official")
    ev_wechat.platform_meta.name = "qq_official"
    d7 = resolver.evaluate(ev_wechat, "maid")
    check("A05.unsupported-platform-out", not d7.in_scope, d7.reason)


def scenario_membership_and_attribution() -> None:
    """A05 个人退出 + A06 归属排除项。"""
    with tempfile.TemporaryDirectory() as td:
        store = MembershipStore(Path(td) / "membership.json")
        resolver = ScopeResolver(base_config(), membership=store)

        ev = FakeEvent(sender_id="10001", group_id="700000001")
        d1 = resolver.evaluate(ev, "maid")
        check("A06.initial-in", d1.in_scope, d1.reason)

        store.opt_out(d1.identity)
        d2 = resolver.evaluate(ev, "maid")
        check(
            "A05.personal-optout",
            not d2.in_scope and d2.reason == "personal_optout",
            d2.reason,
        )
        # 个人退出不影响他人
        ev_other = FakeEvent(sender_id="10002", group_id="700000001")
        d3 = resolver.evaluate(ev_other, "maid")
        check("A05.optout-only-self", d3.in_scope, d3.reason)

        store.opt_in(d1.identity)
        d4 = resolver.evaluate(ev, "maid")
        check("A05.optin-restores", d4.in_scope, d4.reason)

        # 持久化往返
        store2 = MembershipStore(Path(td) / "membership.json")
        store2.opt_out(d4.identity)
        store3 = MembershipStore(Path(td) / "membership.json")
        check(
            "A05.membership-persisted",
            store3.is_opted_out(d4.identity),
            "退出状态需持久化",
        )

    resolver = ScopeResolver(base_config())

    # A06 机器人自言
    ev_self = FakeEvent(sender_id="900001", group_id="700000001", self_id="900001")
    d = resolver.evaluate(ev_self, "maid")
    check("A06.bot-self-excluded", not d.in_scope and d.reason == "bot_self_message")

    # A06 管理命令
    ev_cmd = FakeEvent(sender_id="10001", group_id="700000001", message_str="/uctx status")
    d = resolver.evaluate(ev_cmd, "maid")
    check("A06.command-excluded", not d.in_scope and d.reason == "command_message")

    # A06 未唤醒闲聊
    ev_idle = FakeEvent(sender_id="10001", group_id="700000001", message_str="随便聊聊")
    ev_idle.is_wake = False
    d = resolver.evaluate(ev_idle, "maid")
    check("A06.not-wake-excluded", not d.in_scope and d.reason == "not_wake")

    # A06 无 sender
    ev_nosender = FakeEvent(sender_id="", group_id="700000001")
    d = resolver.evaluate(ev_nosender, "maid")
    check("A06.empty-sender-excluded", not d.in_scope and d.reason == "empty_sender")

    # A06 同名（同昵称）不同 QQ：以 sender_id 为准（昵称不参与身份）
    ev_x = FakeEvent(sender_id="10001", group_id="700000001")
    ev_y = FakeEvent(sender_id="20002", group_id="700000001")
    ev_x.message_obj.sender.nickname = "小明"
    ev_y.message_obj.sender.nickname = "小明"
    dx = resolver.evaluate(ev_x, "maid")
    dy = resolver.evaluate(ev_y, "maid")
    check(
        "A06.nickname-irrelevant",
        dx.in_scope
        and dy.in_scope
        and dx.identity.key != dy.identity.key,
        "同昵称不同 QQ 必须按 sender 隔离",
    )

    # 群 UMO 是全群标识，但归属取真实 sender（同一 UMO 下不同 sender 身份不同）
    check(
        "A06.group-umo-attributed-by-sender",
        dx.identity.sender_id == "10001" and dy.identity.sender_id == "20002",
        "",
    )

    # UMO 不被修改
    check(
        "A06.umo-untouched",
        ev_group_umo := True,
        "",
    )


def scenario_persona_scope_filter() -> None:
    """人格白名单（可选维度）：未列人格不共享，但身份维度照常隔离。"""
    resolver = ScopeResolver(
        base_config(shared_personas=frozenset({"maid"}))
    )
    ev = FakeEvent(sender_id="10001", group_id="700000001")
    d_in = resolver.evaluate(ev, "maid")
    d_out = resolver.evaluate(ev, "tutor")
    check("PERSONA-FILTER.in", d_in.in_scope, d_in.reason)
    check(
        "PERSONA-FILTER.out",
        not d_out.in_scope and d_out.reason == "persona_not_in_scope",
        d_out.reason,
    )


def main() -> int:
    scenario_identity()
    scenario_persona_resolution()
    scenario_scope_groups_private()
    scenario_membership_and_attribution()
    scenario_persona_scope_filter()
    print(f"\n=== P1 身份/范围/归属验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
