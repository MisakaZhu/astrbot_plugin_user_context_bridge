"""N1 回归（MIS-166；W1-W8 返工后 v2）：迁移、模式持久化、退出继承。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/n1_scope_migration_check.py

W 返工语义：迁移=原子单事务（失败回滚+可重试完成）；persona→user 退出
继承由真实转换函数（main._convert_exit_for_mode_change）写入 user 键，
运行时不再按 persona 退出直接阻断 user 模式。真实 PluginManager 四阶段
reload 见 tests/w_lifecycle_worker.py（N08 真实宿主证据）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tests.fakes import FakeEvent, FakeProvider
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from uctx_bridge.identity import MODE_USER, build_identity
from uctx_bridge.ledger import (
    SCHEMA_VERSION,
    MigrationError,
    TurnLedger,
    migrate_ledger,
)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def identity_of(sender, scope, self_id="bot_001", platform="aiocqhttp"):
    """v3 编码身份键（scope 段带 p: 前缀）。"""

    return f"{platform}\x1f{self_id}\x1fp:{scope}\x1f{sender}"


def base_of(sender, self_id="bot_001", platform="aiocqhttp"):
    return f"{platform}\x1f{self_id}\x1f{sender}"


def user_key(sender, self_id="bot_001", platform="aiocqhttp"):
    return f"{platform}\x1f{self_id}\x1fu:\x1f{sender}"


def user_identity(sender, self_id="bot_001", platform="aiocqhttp"):
    return build_identity(platform_id=platform, self_id=self_id,
                          persona_scope=None, sender_id=sender, mode=MODE_USER)


def _make_v1_db(db: Path, *, event_key: str = "legacy-1") -> None:
    """构造 0.6.0 v1 旧库（经真实 TurnLedger 建表后降级）。"""

    led = TurnLedger(db)
    led.open()
    with sqlite3.connect(led._db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_version")
        conn.execute("ALTER TABLE turns DROP COLUMN source_persona")
        conn.execute("ALTER TABLE turns DROP COLUMN mode_generation")
    led.close()


# ---------------------------------------------------------------------------
# N01：升级保持 persona 模式旧有效历史/epoch/退出不丢；键改写到 v3 编码
# ---------------------------------------------------------------------------
def n01_upgrade_preserves() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "legacy.db"
        _make_v1_db(db)
        # 旧库插入一条 completed（模拟 0.6.0 有效历史，裸键）
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
            " source_type, source_id, umo, user_message, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001", 1, 1, "legacy-1",
             "completed", "group", "700000001", "u",
             '{"role":"user","content":"旧问"}', 0, 0),
        )
        conn.commit()
        conn.close()

        migrated = migrate_ledger(db)
        check("N01.migrate-runs", migrated is True)
        check("N01.migrate-idempotent", migrate_ledger(db) is False)
        backups = list((Path(td) / "backups").glob("pre-migrate-v3-*"))
        check("N01.backup-created", len(backups) == 1, f"backups={backups}")

        led = TurnLedger(db)
        led.open()
        hist = led.load_history(identity_of("10001", "maid"))
        check(
            "N01.legacy-history-intact",
            len(hist) == 1 and hist[0].get("content") == "旧问",
            f"hist={hist}",
        )
        check(
            "N01.no-membership-change",
            not (Path(td) / "membership.json").exists()
            or "base_protected" not in json.loads(
                (Path(td) / "membership.json").read_text(encoding="utf-8")),
        )
        conn = sqlite3.connect(led._db_path)
        sp, key = conn.execute(
            "SELECT source_persona, identity_key FROM turns"
            " WHERE event_key='legacy-1'"
        ).fetchone()
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        conn.close()
        check("N01.source-persona-restored", sp == "maid", f"sp={sp!r}")
        check(
            "N01.key-rewritten-to-v3",
            key == identity_of("10001", "maid"),
            f"key={key!r}",
        )
        check("N01.schema-version-3", version == SCHEMA_VERSION, f"v={version}")

        # 损坏输入：非 SQLite 文件
        bad = Path(td) / "bad.db"
        bad.write_text("not a database", encoding="utf-8")
        try:
            migrate_ledger(bad)
            raised = False
        except MigrationError:
            raised = True
        except Exception:
            raised = False
        check("N12.corrupt-input-rejected", raised, "损坏输入必须报 MigrationError")
        led.close()


def n12_migration_failure_atomic() -> None:
    """N12 收紧（W8）：迁移中途失败=完全回滚；原结构/行内容/版本标记
    逐项核验；去掉注入后重试真正完成（回填不再缺失）。"""

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "l.db"
        _make_v1_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
            " source_type, source_id, umo, user_message, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001", 1, 1, "legacy-fail",
             "completed", "group", "g", "u",
             '{"role":"user","content":"q"}', 0, 0),
        )
        # 触发器：任何 UPDATE 一律中止（键改写/回填都会触发）
        conn.execute(
            "CREATE TRIGGER fail_migration BEFORE UPDATE ON turns BEGIN "
            "SELECT RAISE(ABORT, 'injected'); END"
        )
        conn.commit()
        conn.close()

        failed = False
        try:
            migrate_ledger(db)
        except MigrationError:
            failed = True
        check("N12.failure-raises", failed)

        # 逐项核验回滚后的真实状态（不能只看异常与非负计数）
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT event_key, identity_key, user_message FROM turns"
        ).fetchall()
        conn.close()
        check(
            "N12.rollback-schema-intact",
            "source_persona" not in cols
            and "mode_generation" not in cols
            and "schema_version" not in tables,
            f"cols={sorted(cols)} tables={sorted(tables)}",
        )
        check(
            "N12.rollback-rows-intact",
            len(rows) == 1
            and rows[0][0] == "legacy-fail"
            and rows[0][1] == f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001"
            and "旧问" in rows[0][2] or '"q"' in rows[0][2],
            f"rows={rows}",
        )

        # 去掉注入后重试：真正完成（列/键/回填/版本全部到位）
        conn = sqlite3.connect(db)
        conn.execute("DROP TRIGGER fail_migration")
        conn.commit()
        conn.close()
        retry_ok = False
        try:
            retry_ok = migrate_ledger(db) is True
        except MigrationError:
            retry_ok = False
        check("N12.retry-completes", retry_ok)
        led = TurnLedger(db)
        led.open()
        conn = sqlite3.connect(led._db_path)
        sp, key, version = conn.execute(
            "SELECT source_persona, identity_key,"
            " (SELECT MAX(version) FROM schema_version) FROM turns"
        ).fetchone()
        conn.close()
        check(
            "N12.retry-state-correct",
            sp == "maid" and key == identity_of("10001", "maid")
            and version == SCHEMA_VERSION,
            f"sp={sp!r} key={key!r} v={version}",
        )
        led.close()


# ---------------------------------------------------------------------------
# N07：退出继承走真实转换函数（W2 调用链落实）
# ---------------------------------------------------------------------------
async def n07_optout_conversion() -> None:
    from tests.r_rework_check import import_plugin_module

    plugin_main = import_plugin_module()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            fake_self = SimpleNamespace(_membership=membership)
            base = base_of("10001")
            conv = plugin_main.UserContextBridgePlugin._convert_exit_for_mode_change

            ev_a = FakeEvent(sender_id="10001", group_id="700000001", message_str="黑问")
            await drive_pipeline(bridge, ev_a, FakeProvider(["黑答"]), prompt="黑问")

            # persona off（black）
            membership.opt_out(build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope="black", sender_id="10001"))
            check(
                "N07.persona-off-blocks-persona",
                resolver.evaluate(FakeEvent(
                    sender_id="10001", group_id="700000001",
                    message_str="再问"), "black").in_scope is False,
            )

            # 真实转换 persona→user：写入 user 键退出（加法）
            conv(fake_self, base, "persona", "user")
            optout, protected, _ = membership.raw_sets()
            check(
                "N07.conversion-writes-user-key",
                user_key("10001") in optout,
                f"optout={sorted(optout)}",
            )
            resolver.set_history_scope("user")
            req_u = await _probe(bridge, FakeEvent(
                sender_id="10001", group_id="700000001", message_str="user 轮"))
            check("N07.persona-optout-inherits-to-user", req_u is False)
            # user on 解除（user 键 + 基础保护）
            membership.opt_in_user(user_identity("10001"))
            req_u2 = await _probe(bridge, FakeEvent(
                sender_id="10001", group_id="700000001", message_str="user 轮"))
            check("N07.user-on-unblocks", req_u2 is True)

            # user off → 转换回 persona：基础保护覆盖现有人格与未来人格
            membership.opt_out(user_identity("10001"))
            conv(fake_self, base, "user", "persona")
            _, protected2, _ = membership.raw_sets()
            check(
                "N07.user-off-protects-base",
                base in protected2,
                f"protected={sorted(protected2)}",
            )
            resolver.set_history_scope("persona")
            probe_ev = FakeEvent(sender_id="10001", group_id="700000001",
                                 message_str="保护期问")
            check(
                "N07.base-protects-existing-persona",
                resolver.evaluate(probe_ev, "black").in_scope is False,
            )
            check(
                "N07.base-protects-future-persona",
                resolver.evaluate(probe_ev, "brand_new_persona").in_scope is False,
            )
            # 单人格 on 只解除该人格：black 解除、white 仍受保护
            membership.opt_in_persona(build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope="black", sender_id="10001"))
            check(
                "N07.single-persona-on-releases-only-that",
                resolver.evaluate(probe_ev, "black").in_scope is True
                and resolver.evaluate(probe_ev, "white").in_scope is False,
            )
            # 以前的显式 off 不被静默删除：white 的显式 off 在解除保护后仍生效
            membership.opt_out(build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope="white", sender_id="10001"))
            membership.opt_in_persona(build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope="white2", sender_id="10001"))
            check(
                "N07.explicit-persona-off-kept",
                resolver.evaluate(probe_ev, "white").in_scope is False,
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# N08：模式代次（ledger 层：apply_scope_mode 驱动；真实 reload 见 w 套件）
# ---------------------------------------------------------------------------
async def n08_generation_progression() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        ledger = TurnLedger(Path(td) / "l.db")
        ledger.open()
        try:
            base = base_of("10001")
            k_black = identity_of("10001", "black")
            ledger.begin_turn(
                identity_key=k_black, event_key="e1", source_type="group",
                source_id="g", umo="u",
                user_message={"role": "user", "content": "PERSONA-QUESTION"},
                source_persona="black")
            ledger.commit_turn(event_key="e1", status="completed",
                               reply_text="PERSONA-ANSWER")
            # 首次登记 persona：代次不变，旧历史保持当前
            gen, changed = ledger.apply_scope_mode(base, "persona")
            check("N08.first-record-no-bump", (gen, changed) == (0, False))
            hist = ledger.load_history(k_black)
            check("N08.legacy-history-current", len(hist) == 1)

            # 显式切换 user：代次 +1，persona 记录归档；user 键从空开始
            gen, changed = ledger.apply_scope_mode(base, "user")
            check("N08.switch-user-bumps", (gen, changed) == (1, True))
            check("N08.persona-archived", ledger.load_history(k_black) == [])
            ledger.begin_turn(
                identity_key=user_key("10001"), event_key="e2",
                source_type="group", source_id="g", umo="u",
                user_message={"role": "user", "content": "USER-QUESTION"},
                source_persona="black")
            ledger.commit_turn(event_key="e2", status="completed",
                               reply_text="USER-ANSWER")
            # 切回 persona：再 +1，user 记录归档，persona 记录不复活
            gen, changed = ledger.apply_scope_mode(base, "persona")
            check("N08.switch-back-bumps", (gen, changed) == (2, True))
            check(
                "N08.no-resurrection",
                ledger.load_history(k_black) == []
                and ledger.load_history(user_key("10001")) == [],
            )
            # 同模式重复登记不动
            check(
                "N08.same-mode-noop",
                ledger.apply_scope_mode(base, "persona") == (2, False),
            )
        finally:
            ledger.close()


async def _probe(bridge, ev):
    """探测式接管检查：接管返回 True，未接管返回 False。"""
    from astrbot.core.provider.entities import ProviderRequest

    req = ProviderRequest()
    req.prompt = ev.message_str
    req.contexts = []
    req.conversation = None
    return await bridge.handle_llm_request(ev, req)


async def main():
    n01_upgrade_preserves()
    n12_migration_failure_atomic()
    await n07_optout_conversion()
    await n08_generation_progression()
    print(f"\n=== N1 回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
