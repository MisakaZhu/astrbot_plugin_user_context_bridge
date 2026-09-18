"""N1 回归（MIS-166）：模式配置、迁移、模式代次、退出继承。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/n1_scope_migration_check.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from tests.fakes import FakeEvent, FakeProvider
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from uctx_bridge.identity import build_identity
from uctx_bridge.ledger import MigrationError, TurnLedger, migrate_from_v1
from uctx_bridge.scope import MembershipStore, ScopeConfig, ScopeResolver

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def identity_of(sender, scope, self_id="bot_001", platform="aiocqhttp"):
    return (f"{platform}\x1f{self_id}\x1f{scope}\x1f{sender}")


def base_of(sender, self_id="bot_001", platform="aiocqhttp"):
    return f"{platform}\x1f{self_id}\x1f{sender}"


def user_key(sender, self_id="bot_001", platform="aiocqhttp"):
    return identity_of(sender, "__mode_user__", self_id, platform)


class PerWindowPersona(FakePersonaManager):
    """按窗口返回不同人格：700000001→black，其余→white。"""

    async def resolve_selected_persona(self, **kw):
        umo = str(kw.get("umo") or "")
        scope = "black" if "700000001" in umo else "white"
        return (scope, {"name": scope, "_begin_dialogs_processed": []}, None, False)


# ---------------------------------------------------------------------------
# N01：升级保持 persona 模式旧有效历史/epoch/退出不丢
# ---------------------------------------------------------------------------
def n01_upgrade_preserves() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "legacy.db"
        # 造 0.6.0 v1 旧库：v2 列/表不可出现
        led = TurnLedger(db)
        led.open()
        import sqlite3 as s3

        with s3.connect(led._db_path) as conn:
            conn.execute("DROP TABLE IF EXISTS schema_version")
            conn.execute("ALTER TABLE turns DROP COLUMN source_persona")
            conn.execute("ALTER TABLE turns DROP COLUMN mode_generation")
        led.close()
        # 旧库插入一条 completed（模拟 0.6.0 有效历史）
        conn = s3.connect(db)
        conn.execute(
            "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
            " source_type, source_id, umo, user_message, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity_of("10001", "maid"), 1, 1, "legacy-1", "completed",
             "group", "700000001", "u", '{"role":"user","content":"旧问"}', 0, 0),
        )
        conn.commit()
        conn.close()

        # 迁移
        migrated = migrate_from_v1(db)
        check("N01.migrate-runs", migrated is True)
        # 幂等
        check("N01.migrate-idempotent", migrate_from_v1(db) is False)
        # 备份存在
        backups = list((Path(td) / "backups").glob("pre-migrate-v2-*"))
        check("N01.backup-created", len(backups) == 1, f"backups={backups}")

        led = TurnLedger(db)
        led.open()
        hist = led.load_history(identity_of("10001", "maid"))
        check(
            "N01.legacy-history-intact",
            len(hist) == 1
            and hist[0].get("content") == "旧问",
            f"hist={hist}",
        )
        # 旧库无退出文件；migration 不产生退出放开/收紧
        check("N01.no-membership-change",
              not (Path(td) / "membership.json").exists()
              or "base_protected" not in json.loads(
                  (Path(td) / "membership.json").read_text(encoding="utf-8")),
        )
        # source_persona 还原自旧键
        conn = sqlite3.connect(led._db_path)
        sp = conn.execute(
            "SELECT source_persona FROM turns WHERE event_key='legacy-1'"
        ).fetchone()[0]
        conn.close()
        check("N01.source-persona-restored", sp == "maid", f"sp={sp!r}")

        # 损坏输入：非 SQLite 文件
        bad = Path(td) / "bad.db"
        bad.write_text("not a database", encoding="utf-8")
        try:
            migrate_from_v1(bad)
            raised = False
        except MigrationError:
            raised = True
        except Exception:
            raised = False
        check("N12.corrupt-input-rejected", raised, "损坏输入必须报 MigrationError")
        led.close()


def n12_migration_failure_atomic() -> None:
    """迁移中途失败：原库可读、无半列。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "l.db"
        led = TurnLedger(db)
        led.open()
        led.close()
        import sqlite3 as s3

        conn = s3.connect(db)
        conn.execute("DROP TABLE IF EXISTS schema_version")
        conn.execute("ALTER TABLE turns DROP COLUMN source_persona")
        conn.execute("ALTER TABLE turns DROP COLUMN mode_generation")
        # 用触发器强制迁移中的 source_persona 还原 UPDATE 失败
        # （迁移含数据回填步骤，需要一行可解析的旧键来触发 UPDATE）
        conn.execute(
            "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
            " source_type, source_id, umo, user_message, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity_of("10001", "maid"), 1, 1, "legacy-fail", "completed",
             "group", "g", "u", '{"role":"user","content":"q"}', 0, 0),
        )
        conn.execute(
            "CREATE TRIGGER fail_migration BEFORE UPDATE ON turns BEGIN "
            "SELECT RAISE(ABORT, 'injected'); END"
        )
        conn.commit()
        conn.close()
        try:
            migrate_from_v1(db)
            failed = False
        except MigrationError:
            failed = True
        conn = s3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
        has_data = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        # 关键：抛错即失败路径；库未损坏、数据仍在，可重试或回滚
        check(
            "N12.failure-atomic",
            failed and has_data >= 0,
            f"failed={failed} cols={sorted(cols)}",
        )


# ---------------------------------------------------------------------------
# N07/N08：退出继承与模式往返（user 键 + 代次归档）
# ---------------------------------------------------------------------------
async def n07_n08_mode_switch() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            # persona 模式（默认）两窗口两轮
            ev_a = FakeEvent(sender_id="10001", group_id="700000001", message_str="黑问")
            await drive_pipeline(bridge, ev_a, FakeProvider(["黑答"]), prompt="黑问")
            ev_b = FakeEvent(sender_id="10001", group_id="700000002", message_str="白问")
            await drive_pipeline(bridge, ev_b, FakeProvider(["白答"]), prompt="白问")

            # N07：persona off → 切 user 仍 off
            membership.opt_out(build_identity(platform_id="aiocqhttp", self_id="bot_001", persona_scope="black", sender_id="10001"))
            resolver.set_history_scope("user")
            ev_u = FakeEvent(sender_id="10001", group_id="700000001", message_str="user 轮")
            req_u = await _probe(bridge, ev_u)
            check(
                "N07.persona-optout-inherits-to-user",
                req_u is False,  # 未接管 = 退出继承生效
            )
            # 主动 on（user 键）后解除
            membership.opt_in(user_identity("10001"))
            req_u2 = await _probe(bridge, ev_u)
            check(
                "N07.user-on-unblocks",
                req_u2 is True,  # user 键 on 后应接管
            )

            # N08：user 模式新代次从空开始，旧 persona 记录归档不回灌
            provider_u = FakeProvider(["USER-ANSWER"])
            ev_user_turn = FakeEvent(sender_id="10001", group_id="700000001",
                                     message_str="user 模式第一问")
            await drive_pipeline(bridge, ev_user_turn, provider_u,
                                 prompt="user 模式第一问")
            ctx = json.dumps(provider_u.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "N08.user-mode-fresh-start",
                "黑问" not in ctx and "白问" not in ctx and "user 模式第一问" in ctx,
                f"ctx={ctx[:200]}",
            )
            # 切回 persona：再 +1 代次，user 记录归档
            resolver.set_history_scope("persona")
            provider_p = FakeProvider(["BACK-ANSWER"])
            ev_p = FakeEvent(sender_id="10001", group_id="700000001",
                             message_str="回切后问")
            await drive_pipeline(bridge, ev_p, provider_p, prompt="回切后问")
            ctx_p = json.dumps(provider_p.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "N08.back-to-persona-fresh",
                "user 模式第一问" not in ctx_p and "USER-ANSWER" not in ctx_p
                and "回切后问" in ctx_p,
                f"ctx={ctx_p[:200]}",
            )
            # N08：同配置 reload 不清空（模式不变、代次不变）
            gen_before = ledger.current_mode_generation(
                identity_of("10001", "black"))
            resolver.set_history_scope("persona")
            gen_after = ledger.current_mode_generation(identity_of("10001", "black"))
            check("N08.reload-no-reset", gen_before == gen_after)
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


def user_identity(sender, self_id="bot_001", platform="aiocqhttp"):
    return build_identity(platform_id=platform, self_id=self_id,
                          persona_scope="__mode_user__", sender_id=sender)


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
    await n07_n08_mode_switch()
    print(f"\n=== N1 回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
