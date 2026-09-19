"""X1–X5 返工回归（进程内部分；真实宿主部分在 w_lifecycle/t5/t1 worker）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/x_rework_check.py

- X1：身份首次参与（begin_turn）同事务登记生效模式；真实显式切换恰好
  推进一次并归档旧模式；仅退出无记录身份在 initialize 对账登记；
- X2：新 user off 撤销既有 persona_on（先后关系）；此后单人格 on 才
  解除该人格；显式 off/其他人格不受影响；多次往返；
- X3：迁移按输入版本解码（v1 全部裸 scope 原样加 p:，含 __mode_user__/
  u:/q:foo/p:maid；仅 v2 的 __mode_user__ 歧义隔离）；成对名称不碰撞；
  source_persona 保留字面值；membership 同源；
- X4：持久化失败不发布内存新状态（off/两种 on/保护转换），磁盘与内存
  一致，成功重试后一致；
- X5：原生成功关联使用有效退出语义（生产函数边界，合成宿主标记）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uctx_bridge.scope as scope_mod
from uctx_bridge.identity import (
    MODE_USER,
    SCOPE_PERSONA_PREFIX,
    build_identity,
)
from uctx_bridge.ledger import (
    SCHEMA_VERSION,
    MigrationError,
    TurnLedger,
    _migrate_identity_key,
    migrate_ledger,
)
from uctx_bridge.scope import (
    MembershipError,
    MembershipStore,
    encode_membership_key,
)

REPO = Path(__file__).resolve().parent.parent
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def identity_of(sender, scope, self_id="bot_001", platform="aiocqhttp"):
    token = "u:" if scope == "user" else f"p:{scope}"
    return f"{platform}\x1f{self_id}\x1f{token}\x1f{sender}"


def base_of(sender, self_id="bot_001", platform="aiocqhttp"):
    return f"{platform}\x1f{self_id}\x1f{sender}"


def persona_ident(sender, scope, self_id="bot_001", platform="aiocqhttp"):
    return build_identity(platform_id=platform, self_id=self_id,
                          persona_scope=scope, sender_id=sender)


def user_ident(sender, self_id="bot_001", platform="aiocqhttp"):
    return build_identity(platform_id=platform, self_id=self_id,
                          persona_scope=None, sender_id=sender, mode=MODE_USER)


def load_legacy_ledger_module():
    """从 git 历史载入 0.6.0 真实 ledger/identity/scope 模块（X3）。"""

    work = Path(tempfile.mkdtemp()) / "legacy_v060"
    work.mkdir(parents=True, exist_ok=True)
    for filename in ("__init__.py", "ledger.py", "identity.py", "scope.py"):
        content = subprocess.check_output(
            ["git", "show", f"859f18e:uctx_bridge/{filename}"], cwd=str(REPO))
        (work / filename).write_bytes(content)
    spec = importlib.util.spec_from_file_location(
        "legacy_v060_x", work / "__init__.py",
        submodule_search_locations=[str(work)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_v060_x"] = mod
    spec.loader.exec_module(mod)
    # 显式加载子模块（scope.py 使用相对导入，需要包上下文）
    mod.ledger = importlib.import_module("legacy_v060_x.ledger")
    mod.identity = importlib.import_module("legacy_v060_x.identity")
    mod.scope = importlib.import_module("legacy_v060_x.scope")
    return mod


# ---------------------------------------------------------------------------
# X1：首次参与即登记生效模式
# ---------------------------------------------------------------------------
def x1_first_participation_registers() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        base = base_of("10001")
        k_black = identity_of("10001", "black")
        # 全新安装：persona 模式第一轮（bridge 会传 scope_mode）
        led.begin_turn(
            identity_key=k_black, event_key="x1-1", source_type="group",
            source_id="g", umo="u",
            user_message={"role": "user", "content": "P1"},
            source_persona="black", scope_mode="persona")
        led.commit_turn(event_key="x1-1", status="completed", reply_text="A1")
        check("X1.first-turn-registers-mode",
              led.get_scope_mode(base) == "persona",
              f"scope_mode={led.get_scope_mode(base)!r}")
        check("X1.gen0", led.current_mode_generation(base) == 0)
        # 直接切 user（无同模式预热）：真实显式变化恰好推进一次
        gen, changed = led.apply_scope_mode(base, "user")
        check("X1.direct-switch-bumps-once",
              (gen, changed) == (1, True), f"gen={gen} changed={changed}")
        check("X1.old-persona-archived", led.load_history(k_black) == [])
        # user 轮（记录 user），切回 persona 再切 user：各推进一次
        led.begin_turn(
            identity_key=user_ident("10001").key, event_key="x1-2",
            source_type="group", source_id="g", umo="u",
            user_message={"role": "user", "content": "U1"},
            source_persona="black", scope_mode="user")
        led.commit_turn(event_key="x1-2", status="completed", reply_text="UA1")
        gen, changed = led.apply_scope_mode(base, "persona")
        check("X1.roundtrip-back-bumps", (gen, changed) == (2, True))
        check("X1.user-history-archived",
              led.load_history(user_ident("10001").key) == [])
        gen, changed = led.apply_scope_mode(base, "user")
        check("X1.roundtrip-forth-bumps", (gen, changed) == (3, True))
        check("X1.no-resurrect-any",
              led.load_history(k_black) == []
              and led.load_history(user_ident("10001").key) == [])
        # 归档可读：显式低代次查询
        with sqlite3.connect(led._db_path) as db:
            gens = dict(db.execute(
                "SELECT mode_generation, COUNT(*) FROM turns"
                " GROUP BY mode_generation").fetchall())
        check("X1.archived-data-still-queryable",
              gens.get(0) == 1 and gens.get(1) == 1,
              f"gens={gens}")
        # 同模式重复登记不动
        check("X1.same-mode-noop",
              led.apply_scope_mode(base, "user") == (3, False))
        led.close()


def x1_legacy_upgrade_and_membership_only() -> None:
    """v1 升级路径（迁移后轮次无 scope_mode）与仅有退出的身份。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        # v1 升级：手工构造 v3 库但 identity 行不带 scope_mode 登记
        # （等价迁移后状态：turns 存在、scope_mode 缺失）
        db = Path(td) / "legacy_like.db"
        led = TurnLedger(db)
        led.open()
        k_maid = identity_of("10001", "maid")
        base = base_of("10001")
        with sqlite3.connect(led._db_path) as conn:
            conn.execute(
                "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
                " source_type, source_id, umo, user_message, created_at, updated_at,"
                " source_persona, mode_generation)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'0')",
                (k_maid, 1, 1, "v1-old", "completed", "group", "g", "u",
                 '{"role":"user","content":"v1 旧问"}', 0, 0, "maid"))
            conn.execute(
                "INSERT INTO meta (identity_key, name, value) VALUES (?, 'epoch', '1')",
                (k_maid,))
        # initialize 对账（recorded=None → 登记当前模式，不改代次）
        gen, changed = led.apply_scope_mode(base, "persona")
        check("X1.v1-upgrade-register-no-bump",
              (gen, changed) == (0, False) and led.get_scope_mode(base) == "persona")
        hist = led.load_history(k_maid)
        check("X1.v1-history-current", len(hist) == 1
              and hist[0].get("content") == "v1 旧问", f"hist={hist}")
        # 真实配置变化仍推进
        gen, changed = led.apply_scope_mode(base, "user")
        check("X1.v1-real-switch-bumps", (gen, changed) == (1, True))
        check("X1.v1-history-archived", led.load_history(k_maid) == [])
        # 新身份（已有实例新增用户）：首轮即登记
        led.begin_turn(
            identity_key=identity_of("30003", "newp"), event_key="n1",
            source_type="group", source_id="g", umo="u",
            user_message={"role": "user", "content": "Q"},
            source_persona="newp", scope_mode="user")
        check("X1.new-user-registered-on-first-turn",
              led.get_scope_mode(base_of("30003")) == "user")
        led.close()

        # 仅退出无记录的身份：对账登记（无轮次可归档，登记不推进）
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
            led2 = TurnLedger(Path(td2) / "m.db")
            led2.open()
            base_only = base_of("40004")
            gen, changed = led2.apply_scope_mode(base_only, "user")
            check("X1.exit-only-identity-registered",
                  (gen, changed) == (0, False)
                  and led2.get_scope_mode(base_only) == "user")
            # 重复恢复/重复对账不多推进
            check("X1.reconcile-idempotent",
                  led2.apply_scope_mode(base_only, "user") == (0, False))
            led2.close()


# ---------------------------------------------------------------------------
# X2：新 user off 压过旧 persona_on
# ---------------------------------------------------------------------------
def x2_user_off_supersedes_persona_on() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = MembershipStore(Path(td) / "m.json")
        maid = persona_ident("10001", "maid")
        white = persona_ident("10001", "white")
        future = persona_ident("10001", "futurep")
        uident = user_ident("10001")
        other = persona_ident("20002", "maid")

        # on(maid) → 切 user → user off → 切 persona：maid 必须仍退出
        m.opt_in_persona(maid)
        check("X2.pre-on-records", m.persona_explicit_on(maid))
        m.opt_out(uident)  # user off（X2：应撤销 maid 的 persona_on）
        m.protect_base(uident)
        check("X2.user-off-clears-stale-persona-on",
              not m.persona_explicit_on(maid),
              f"persona_on={m.raw_sets()[2]}")
        check("X2.maid-captured-zero-after-user-off",
              m.effective_optout(maid) is True)
        check("X2.future-persona-also-protected",
              m.effective_optout(future) is True)
        st_reason = m.optout_reason(maid)
        check("X2.reason-inherited", "继承" in st_reason or "保护" in st_reason,
              f"reason={st_reason!r}")

        # 此后单人格 on 只解除该人格；其他人格与显式 off 不受影响
        m.opt_in_persona(maid)
        check("X2.explicit-on-releases-only-maid",
              not m.effective_optout(maid) and m.effective_optout(white)
              and m.effective_optout(future))
        # 重复 off / on→off→on 多次
        for _ in range(2):
            m.opt_out(uident)
        check("X2.repeat-user-off-reprotects-all",
              m.effective_optout(maid) and m.effective_optout(future))
        m.opt_in_persona(maid)
        m.opt_out(uident)
        m.opt_in_persona(maid)
        check("X2.on-off-on-cycles-consistent",
              not m.effective_optout(maid) and m.effective_optout(future))
        # 其他用户/机器人/平台隔离不受牵连
        m.opt_out(uident)  # 最终态：10001 user off
        check("X2.other-identity-unaffected",
              not m.effective_optout(persona_ident("20002", "maid", self_id="bot_002"))
              and not m.effective_optout(persona_ident("10001", "maid", self_id="bot_002"))
              and not m.effective_optout(user_ident("10001", platform="otherpl")))
        # 磁盘回读一致
        m2 = MembershipStore(Path(td) / "m.json")
        check("X2.disk-roundtrip-consistent",
              m2.effective_optout(maid) == m.effective_optout(maid)
              and m2.effective_optout(future) == m.effective_optout(future))


# ---------------------------------------------------------------------------
# X3：迁移按输入版本解码
# ---------------------------------------------------------------------------
def x3_version_aware_migration() -> None:
    legacy = load_legacy_ledger_module()
    personas = ["maid", "p:maid", "u:", "q:foo", "__mode_user__", "__special__"]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        expected_keys = {}
        for index, persona in enumerate(personas):
            folder = tdp / str(index)
            folder.mkdir()
            db = folder / "uctx_ledger.db"
            old = legacy.ledger.TurnLedger(str(db))
            old.open()
            old_ident = legacy.identity.build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope=persona, sender_id="10001")
            old.begin_turn(
                identity_key=old_ident.key, event_key="old", source_type="group",
                source_id="700000001", umo="u",
                user_message={"role": "user", "content": "OLD-Q"})
            old.commit_turn(event_key="old", status="completed",
                            reply_text="OLD-A")
            old.close()
            old_m = legacy.scope.MembershipStore(folder / "membership.json")
            old_m.opt_out(old_ident)

            migrate_ledger(db)
            m = MembershipStore(folder / "membership.json")
            m.migrate_legacy_keys(source_version=1)

            # v1：scope 是原始人格 ID 字面值 → p:+字面值
            wanted = f"aiocqhttp\x1fbot_001\x1fp:{persona}\x1f10001"
            expected_keys[persona] = wanted
            cur = TurnLedger(db)
            cur.open()
            hist = cur.load_history(wanted)
            with sqlite3.connect(db) as conn:
                actual = conn.execute(
                    "SELECT identity_key, source_persona FROM turns").fetchall()
            cur.close()
            check(f"X3.v1-history-preserved[{persona}]",
                  len(hist) == 1 and hist[0].get("content") == "OLD-Q"
                  and actual[0][0] == wanted,
                  f"wanted={wanted!r} actual={actual}")
            check(f"X3.v1-source-persona-verbatim[{persona}]",
                  actual[0][1] == persona,
                  f"source_persona={actual[0][1]!r}")
            check(f"X3.v1-exit-preserved[{persona}]",
                  m.effective_optout(build_identity(
                      platform_id="aiocqhttp", self_id="bot_001",
                      persona_scope=persona, sender_id="10001")),
                  f"membership={json.loads((folder / 'membership.json').read_text(encoding='utf-8'))}")

        # v1 的 __mode_user__ 不被隔离：按普通人格迁移
        check("X3.v1-mode-user-not-quarantined",
              expected_keys["__mode_user__"].endswith("\x1fp:__mode_user__\x1f10001"),
              f"key={expected_keys['__mode_user__']!r}")

        # 成对碰撞：maid 与 p:maid 同库迁移不再 UNIQUE 冲突
        db2 = tdp / "two_personas.db"
        old = legacy.ledger.TurnLedger(str(db2))
        old.open()
        for persona, ek in (("maid", "e1"), ("p:maid", "e2")):
            oi = legacy.identity.build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope=persona, sender_id="10001")
            old.begin_turn(
                identity_key=oi.key, event_key=ek, source_type="group",
                source_id="g", umo="u",
                user_message={"role": "user", "content": "Q-" + persona})
            old.commit_turn(event_key=ek, status="completed",
                            reply_text="A-" + persona)
        old.close()
        error = None
        cur = TurnLedger(db2)
        try:
            cur.open()
        except MigrationError as exc:
            error = str(exc)
        finally:
            if cur.is_open:
                cur.close()
        check("X3.pair-no-unique-collision", error is None, f"error={error}")
        conn = sqlite3.connect(db2)
        pairs = sorted(conn.execute(
            "SELECT identity_key, source_persona FROM turns").fetchall())
        conn.close()
        check("X3.pair-distinct-keys",
              pairs[0][0] == identity_of("10001", "maid")
              and pairs[1][0] == identity_of("10001", "p:maid")
              and pairs[0][1] == "maid" and pairs[1][1] == "p:maid",
              f"pairs={pairs}")

        # v2 来源：仅 __mode_user__ 歧义隔离
        check("X3.v2-mode-user-quarantined",
              _migrate_identity_key(
                  f"aiocqhttp\x1fbot\x1f__mode_user__\x1f10001", 2)
              == f"aiocqhttp\x1fbot\x1fq:__mode_user__\x1f10001")
        check("X3.v2-ordinary-persona-prefixed",
              _migrate_identity_key(f"aiocqhttp\x1fbot\x1fu:\x1f10001", 2)
              == f"aiocqhttp\x1fbot\x1fp:u:\x1f10001")
        # membership 编码矩阵同源
        mk_v1 = f"aiocqhttp\x1fbot\x1f__mode_user__\x1f10001"
        check("X3.membership-v1-not-ambiguous",
              encode_membership_key(mk_v1, 1)
              == (f"aiocqhttp\x1fbot\x1fp:__mode_user__\x1f10001", False))
        check("X3.membership-v2-ambiguous",
              encode_membership_key(mk_v1, 2)[1] is True)

        # 迁移来源版本可查询（membership 同源依据）
        db3 = tdp / "src.db"
        _build_v1(legacy, db3)
        migrate_ledger(db3)
        led = TurnLedger(db3)
        led.open()
        check("X3.migration-source-version-recorded",
              led.migration_source_version() == 1)
        led.close()


def _build_v1(legacy, db: Path) -> None:
    old = legacy.ledger.TurnLedger(str(db))
    old.open()
    oi = legacy.identity.build_identity(
        platform_id="aiocqhttp", self_id="bot_001",
        persona_scope="maid", sender_id="10001")
    old.begin_turn(
        identity_key=oi.key, event_key="e", source_type="group",
        source_id="g", umo="u",
        user_message={"role": "user", "content": "Q"})
    old.commit_turn(event_key="e", status="completed", reply_text="A")
    old.close()


# ---------------------------------------------------------------------------
# X4：持久化失败不发布内存状态
# ---------------------------------------------------------------------------
def x4_persist_failure_never_publishes() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        path = tdp / "m.json"
        m = MembershipStore(path)
        maid = persona_ident("10001", "maid")
        white = persona_ident("10001", "white")
        uident = user_ident("10001")
        m.opt_out(maid)  # 先成功持久化 off
        disk_before = hashlib.sha256(path.read_bytes()).hexdigest()

        import uctx_bridge.scope as scope_mod

        def inject(method_name, target):
            orig = scope_mod._atomic_write_text

            def broken(p, text):
                if target(p, text):
                    raise OSError("synthetic disk full")
                return orig(p, text)

            scope_mod._atomic_write_text = broken
            return orig

        def any_write(p, text):
            return True

        cases = [
            ("opt_in_persona", lambda: m.opt_in_persona(maid), maid),
            ("opt_in_user", lambda: m.opt_in_user(uident), uident),
            ("opt_out", lambda: m.opt_out(white), white),
            ("protect_base", lambda: m.protect_base(white), white),
        ]
        for name, fn, ident in cases:
            orig = inject(name, any_write)
            try:
                raised = False
                try:
                    fn()
                except MembershipError:
                    raised = True
            finally:
                scope_mod._atomic_write_text = orig
            # 内存状态与磁盘状态一致：失败操作未发布
            memory_effective = m.effective_optout(ident)
            disk_effective = MembershipStore(path).effective_optout(ident)
            check(f"X4.{name}-failure-no-memory-publish",
                  raised and memory_effective == disk_effective,
                  f"raised={raised} mem={memory_effective} disk={disk_effective}")

        # 旧 off/基础保护未因失败被释放（对照；retry 之前断言）
        check("X4.older-state-intact-after-failures",
              m.is_opted_out(maid) and not m.is_opted_out(uident)
              and not m.is_base_protected(uident))
        # 失败后重试成功：状态一致
        m.opt_in_persona(maid)
        check("X4.retry-success-consistent",
              not m.effective_optout(maid)
              and MembershipStore(path).effective_optout(maid) is False)
        # migrate 失败：用无 scheme 标记的旧格式文件（迁移才会真正写盘）
        legacy_file = tdp / "legacy_m.json"
        legacy_key = chr(31).join(("aiocqhttp", "bot_001", "maid", "10001"))
        legacy_file.write_text(json.dumps({
            "optout": ["aiocqhttpbot_001maid10001"],
            "base_protected": [], "persona_on": [],
        }, ensure_ascii=False), encoding="utf-8")
        disk_before = hashlib.sha256(legacy_file.read_bytes()).hexdigest()
        m_leg = MembershipStore.__new__(MembershipStore)
        m_leg._path = legacy_file
        m_leg._lock = __import__("threading").Lock()
        m_leg._optout = {"aiocqhttpbot_001maid10001"}
        m_leg._base_protected = set()
        m_leg._persona_on = set()
        m_leg._ambiguous = []
        orig = inject("migrate", any_write)
        try:
            raised = False
            try:
                m_leg.migrate_legacy_keys(source_version=1)
            except MembershipError:
                raised = True
        finally:
            scope_mod._atomic_write_text = orig
        # v1 内存键即原始裸键（scope=maid 无前缀）
        mem_now = legacy_key in m_leg._optout
        disk_now = (hashlib.sha256(legacy_file.read_bytes()).hexdigest()
                    == disk_before)
        check("X4.migrate-failure-no-publish",
              raised and mem_now and disk_now,
              f"raised={raised} mem={mem_now} disk={disk_now} "
              f"content={legacy_file.read_text(encoding='utf-8')[:120]!r}")
        # 损坏文件受控拒绝（不按空集合继续）
        bad = tdp / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        raised = False
        try:
            MembershipStore(bad)
        except MembershipError:
            raised = True
        check("X4.corrupt-file-controlled", raised)


# ---------------------------------------------------------------------------
# X5：原生成功关联使用有效退出语义（生产函数边界）
# ---------------------------------------------------------------------------
def x5_native_sync_effective_optout() -> None:
    from tests.r_rework_check import import_plugin_module

    plugin_main = import_plugin_module()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        ledger = TurnLedger(tdp / "l.db")
        ledger.open()
        membership = MembershipStore(tdp / "membership.json")
        plugin = plugin_main.UserContextBridgePlugin.__new__(
            plugin_main.UserContextBridgePlugin)
        plugin._membership = membership
        plugin._ledger = ledger
        plugin._sharing_active = True
        conv_mgr = SimpleNamespace(
            get_curr_conversation_id=None)  # 非 AsyncMock：new 分支走异常容错
        plugin._commands = None
        # 直接构造最小 context/commands 面（生产函数边界，同 Codex 探针）
        from unittest.mock import AsyncMock

        plugin._commands = SimpleNamespace(_identity=None)
        plugin.context = SimpleNamespace(
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid")),
            get_using_provider_async=AsyncMock(return_value=object()),
            get_using_provider=lambda umo: object())

        future = persona_ident("10002", "future-persona")
        # user off → persona：基础保护继承（无直接键退出）
        membership.opt_out(user_ident("10002"))
        membership.protect_base(user_ident("10002"))
        # _identity 桩：返回受保护的未来人格
        plugin._commands = SimpleNamespace(
            _identity=AsyncMock(return_value=(future, "future-persona")),
            _window_in_scope=lambda ev: True)
        ev = SimpleNamespace(unified_msg_origin="u", sent_chains=[])

        import asyncio

        async def run():
            captured = []

            async def send(chain):
                captured.append(chain)

            ev.send = send
            await plugin._sync_native_reset_on_success(ev)
            return captured

        # 合成宿主成功事实：activated_handlers + 结构化 clean 标记；
        # applied 去重标记未设置（None），允许本次关联执行
        def host_marker(key):
            if key == "activated_handlers":
                return [SimpleNamespace(
                    handler_module_path="astrbot.builtin_stars.builtin_commands.main",
                    handler_name="new_conv")]
            if key == "_clean_group_context_session":
                return True
            return None

        ev.get_extra = host_marker
        ev.set_extra = lambda k, v: None

        captured = asyncio.run(run())
        check("X5.native-new-respects-inherited-protection",
              not captured,
              f"sent={[c.get_plain_text() for c in captured]}")
        # 对照：无保护时正常联动（一次性）
        membership.opt_in_user(user_ident("10002"))
        check("X5.pre-condition-effective-cleared",
              not membership.effective_optout(future))
        captured2 = asyncio.run(run())
        check("X5.native-new-normal-success-associates",
              len(captured2) == 1
              and "同步清空" in (captured2[0].get_plain_text() or ""),
              f"sent={[c.get_plain_text() for c in captured2]}")
        ledger.close()


def main() -> int:
    x1_first_participation_registers()
    x1_legacy_upgrade_and_membership_only()
    x2_user_off_supersedes_persona_on()
    x3_version_aware_migration()
    x4_persist_failure_never_publishes()
    x5_native_sync_effective_optout()
    print(f"\n=== X 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
