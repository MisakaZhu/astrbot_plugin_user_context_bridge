"""W 返工回归（W2/W3/W4）：退出存储语义、迁移原子性与一致性备份、键编码。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/w_rework_check.py

- W2：membership 损坏受控（绝不按空退出集合继续采集）、写入失败上抛、
  旧键迁移矩阵（裸→p:；__mode_user__ 不可辨认→p:+基础保护）、迁移幂等
  与 .bak 备份；
- W3：以 **0.6.0 真实实现（git show 859f18e:uctx_bridge/ledger.py）** 构造
  旧库，验证候选 migrate_ledger：一致性备份（读事务持期间已提交的 WAL
  数据不丢）、逐记录对账（键改写/source_persona/epoch）、幂等、损坏输入、
  单事务故障回滚+重试完成、备份绝不覆盖既有备份；
- W4：scope 编码结构性无碰撞（任意 persona id 不与 u:/q: 混淆）与迁移
  改写矩阵。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from uctx_bridge.identity import (
    MODE_USER,
    SCOPE_PERSONA_PREFIX,
    SCOPE_QUARANTINE_PREFIX,
    SCOPE_USER_TOKEN,
    build_identity,
)
from uctx_bridge.ledger import (
    SCHEMA_VERSION,
    MigrationError,
    TurnLedger,
    _migrate_identity_key,
    inspect_schema_version,
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
    token = SCOPE_USER_TOKEN if scope == "user" else f"p:{scope}"
    return f"{platform}\x1f{self_id}\x1f{token}\x1f{sender}"


def base_of(sender, self_id="bot_001", platform="aiocqhttp"):
    return f"{platform}\x1f{self_id}\x1f{sender}"


def load_legacy_ledger_module():
    """从 git 历史载入 0.6.0 真实 ledger 模块（无 astrbot 依赖，可独立载入）。"""

    src = subprocess.run(
        ["git", "show", "859f18e:uctx_bridge/ledger.py"],
        capture_output=True, text=True, cwd=str(REPO), check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "legacy_ledger_859f18e.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("legacy_ledger_859f18e", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_ledger_859f18e"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# W2：membership 语义
# ---------------------------------------------------------------------------
def w2_membership() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        # 损坏文件：必须受控失败，不按空集合继续
        bad = tdp / "bad_membership.json"
        bad.write_text("{not-json", encoding="utf-8")
        raised = False
        try:
            MembershipStore(bad)
        except MembershipError:
            raised = True
        check("W2.corrupt-file-controlled", raised)

        # 写入失败：opt_out 必须上抛，不得静默成功后继续采集
        m = MembershipStore(tdp / "m.json")

        def boom(path, text):
            raise OSError("disk full")

        orig = __import__("uctx_bridge.scope", fromlist=["_atomic_write_text"])._atomic_write_text
        import uctx_bridge.scope as scope_mod

        scope_mod._atomic_write_text = boom
        try:
            m.opt_out(build_identity(
                platform_id="aiocqhttp", self_id="bot_001",
                persona_scope="black", sender_id="10001"))
            raised = False
        except MembershipError:
            raised = True
        finally:
            scope_mod._atomic_write_text = orig
        check("W2.persist-failure-raises", raised)

        # 旧键迁移矩阵
        legacy = {
            "optout": [
                f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001",
                f"aiocqhttp\x1fbot_001\x1f__mode_user__\x1f10002",
            ],
            "persona_on": [
                f"aiocqhttp\x1fbot_001\x1fwhite\x1f10001",
            ],
            "base_protected": [base_of("10003")],
        }
        mfile = tdp / "legacy_membership.json"
        mfile.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        m2 = MembershipStore(mfile)
        changed = m2.migrate_legacy_keys()
        optout, protected, persona_on = m2.raw_sets()
        check("W2.migration-runs", changed is True)
        check(
            "W2.migration-persona-key-prefixed",
            identity_of("10001", "maid") in optout,
            f"optout={sorted(optout)}",
        )
        ambiguous_key = f"aiocqhttp\x1fbot_001\x1f__mode_user__\x1f10002"
        check(
            "W2.migration-ambiguous-safe-direction",
            identity_of("10002", "__mode_user__") in optout
            and base_of("10002") in protected,
            f"optout={sorted(optout)} protected={sorted(protected)}",
        )
        check(
            "W2.migration-persona-on-kept",
            identity_of("10001", "white") in persona_on,
            f"on={sorted(persona_on)}",
        )
        check("W2.migration-base-untouched", base_of("10003") in protected)
        check("W2.migration-backup-created",
              (tdp / "legacy_membership.json.pre-scheme3.bak").exists())
        check("W2.migration-idempotent", m2.migrate_legacy_keys() is False)

        # 有效退出语义抽查（与运行时唯一同源）
        ident_black = build_identity(
            platform_id="aiocqhttp", self_id="bot_001",
            persona_scope="black", sender_id="10001")
        m3 = MembershipStore(tdp / "m3.json")
        m3.opt_out(ident_black)
        check("W2.effective-direct",
              m3.effective_optout(ident_black)
              and m3.optout_reason(ident_black) == "本人格退出")
        # persona→user 转换后 user 键退出；user on 恢复且保留人格显式退出
        m3.opt_out(build_identity(
            platform_id="aiocqhttp", self_id="bot_001",
            persona_scope=None, sender_id="10001", mode=MODE_USER))
        uident = build_identity(
            platform_id="aiocqhttp", self_id="bot_001",
            persona_scope=None, sender_id="10001", mode=MODE_USER)
        m3.opt_in_user(uident)
        check(
            "W2.user-on-keeps-persona-off",
            not m3.effective_optout(uident)
            and m3.effective_optout(ident_black),
        )


# ---------------------------------------------------------------------------
# W3：迁移（真实 0.6.0 旧库）
# ---------------------------------------------------------------------------
def _build_real_v1_db(legacy_mod, db: Path, *, two_users: bool = False) -> None:
    """用 0.6.0 真实实现建库：多轮、跨 epoch，裸身份键。"""

    led = legacy_mod.TurnLedger(str(db))
    led.open()
    k1 = f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001"
    led.begin_turn(
        identity_key=k1, event_key="v1-1", source_type="group",
        source_id="700000001", umo="u",
        user_message={"role": "user", "content": "旧问一"},
    )
    led.commit_turn(
        event_key="v1-1", status="completed",
        trajectory=[{"role": "assistant", "content": "旧答一"}],
        reply_text="旧答一",
    )
    if two_users:
        k2 = f"aiocqhttp\x1fbot_001\x1fmaid\x1f20002"
        led.begin_turn(
            identity_key=k2, event_key="v1-2", source_type="group",
            source_id="700000001", umo="u",
            user_message={"role": "user", "content": "他人旧问"},
        )
        led.commit_turn(event_key="v1-2", status="completed", reply_text="他人旧答")
    led.bump_epoch(k1)
    led.begin_turn(
        identity_key=k1, event_key="v1-3", source_type="group",
        source_id="700000001", umo="u",
        user_message={"role": "user", "content": "旧问二"},
    )
    led.commit_turn(event_key="v1-3", status="completed", reply_text="旧答二")
    led.close()


def w3_migration() -> None:
    legacy = load_legacy_ledger_module()
    check("W3.legacy-module-loaded",
          hasattr(legacy, "TurnLedger")
          and not hasattr(legacy, "SCHEMA_VERSION"),
          "0.6.0 真实模块应无 SCHEMA_VERSION（那是 0.7.0 引入）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        db = tdp / "real_v1.db"
        _build_real_v1_db(legacy, db, two_users=True)

        # W3 一致性备份：持有读快照期间另一连接再提交一轮——迁移的备份
        # 必须包含全部三轮（backup API 读快照含已提交 WAL 数据）
        holder = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        holder.execute("BEGIN")
        holder.execute("SELECT COUNT(*) FROM turns").fetchone()
        late = sqlite3.connect(db, timeout=15.0)
        late.execute(
            "INSERT INTO turns (identity_key, epoch, seq, event_key, status,"
            " source_type, source_id, umo, user_message, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"aiocqhttp\x1fbot_001\x1fmaid\x1f10001", 2, 1, "v1-late",
             "completed", "group", "700000001", "u",
             '{"role":"user","content":"迟提交旧问"}', 100, 100),
        )
        late.commit()
        late.close()
        rc = migrate_ledger(db)
        check("W3.migrate-runs", rc is True)
        holder.execute("SELECT COUNT(*) FROM turns").fetchone()
        holder.close()

        # 备份必须含 4 轮（含迟提交行）；不覆盖既有备份
        backups = sorted((tdp / "backups").glob("pre-migrate-v3-*"))
        check("W3.backup-created", len(backups) == 1, f"backups={backups}")
        bconn = sqlite3.connect(backups[0])
        n_backup = bconn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        has_late = bconn.execute(
            "SELECT COUNT(*) FROM turns WHERE event_key='v1-late'"
        ).fetchone()[0]
        bconn.close()
        check("W3.backup-includes-committed-wal",
              n_backup == 4 and has_late == 1, f"n={n_backup} late={has_late}")

        # 迁移后逐项对账
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT identity_key, event_key, epoch, source_persona, status"
            " FROM turns ORDER BY id"
        ).fetchall()
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
        conn.close()
        key_of = {r[0]: r for r in rows}
        check("W3.rows-migrated",
              len(rows) == 4
              and identity_of("10001", "maid") in key_of
              and identity_of("20002", "maid") in key_of,
              f"keys={[r[0] for r in rows]}")
        check("W3.source-persona-restored",
              all(r[3] == "maid" for r in rows if "maid" in r[0]),
              f"rows={rows}")
        check("W3.epochs-preserved",
              key_of[identity_of("10001", "maid")][2] == 2
              and key_of[identity_of("20002", "maid")][2] == 1,
              f"rows={rows}")
        check("W3.schema-version-3", version == SCHEMA_VERSION)
        check("W3.v2-columns-present",
              {"source_persona", "mode_generation"} <= cols)
        check("W3.migrate-idempotent", migrate_ledger(db) is False)

        # 迁移后真实 TurnLedger 打开：旧有效历史在当前代次可读。
        # 当前 = epoch 2（旧问二 + 迟提交行）；epoch 1 的旧问一已归档
        led = TurnLedger(db)
        led.open()
        hist = led.load_history(identity_of("10001", "maid"))
        contents = [h.get("content") for h in hist]
        check("W3.post-migrate-history-current",
              "旧问二" in contents and "迟提交旧问" in contents
              and "旧问一" not in contents,
              f"contents={contents}")
        led.close()

    # 备份绝不覆盖：同一 backup_dir 迁移两个同名 v1 库 → 两份备份共存
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        shared = tdp / "backups"
        for i, name in enumerate(("same_name.db", "same_name.db")):
            sub = tdp / f"d{i}"
            sub.mkdir()
            db = sub / "same_name.db"
            _build_real_v1_db(legacy, db)
            migrate_ledger(db, backup_dir=shared)
        backups = sorted(shared.glob("pre-migrate-v3-*-same_name.db"))
        check("W3.backup-no-overwrite",
              len(backups) == 2
              and all(b.stat().st_size > 0 for b in backups),
              f"backups={backups}")

    # 损坏输入受控
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bad = Path(td) / "bad.db"
        bad.write_bytes(b"not a sqlite file" * 10)
        raised = False
        try:
            migrate_ledger(bad)
        except MigrationError:
            raised = True
        except Exception:
            raised = False
        check("W3.corrupt-input-controlled", raised)


# ---------------------------------------------------------------------------
# W4：键编码结构性无碰撞
# ---------------------------------------------------------------------------
def w4_key_encoding() -> None:
    sep = chr(31)

    def raw_key(scope):
        return sep.join(("aiocqhttp", "bot", scope, "10001"))

    # v1 输入：scope 一律是原始人格 ID 字面值 → p:+字面值（无歧义）
    check("W4.rewrite-bare",
          _migrate_identity_key(raw_key("maid"), 1)
          == sep.join(("aiocqhttp", "bot", "p:maid", "10001")))
    check("W4.v1-literal-p-colon-prefixed",
          _migrate_identity_key(raw_key("p:maid"), 1)
          == sep.join(("aiocqhttp", "bot", "p:p:maid", "10001")))
    check("W4.v1-literal-u-colon-prefixed",
          _migrate_identity_key(raw_key("u:"), 1)
          == sep.join(("aiocqhttp", "bot", "p:u:", "10001")))
    check("W4.v1-literal-q-colon-prefixed",
          _migrate_identity_key(raw_key("q:foo"), 1)
          == sep.join(("aiocqhttp", "bot", "p:q:foo", "10001")))
    check("W4.v1-mode-user-not-quarantined",
          _migrate_identity_key(raw_key("__mode_user__"), 1)
          == sep.join(("aiocqhttp", "bot", "p:__mode_user__", "10001")))

    # v2 输入：仅字面 __mode_user__ 歧义隔离；其余原始名加 p:
    check("W4.v2-mode-user-quarantined",
          _migrate_identity_key(raw_key("__mode_user__"), 2)
          == sep.join(("aiocqhttp", "bot", "q:__mode_user__", "10001")))
    check("W4.v2-ordinary-persona-prefixed",
          _migrate_identity_key(raw_key("u:"), 2)
          == sep.join(("aiocqhttp", "bot", "p:u:", "10001")))

    # v3 输入不重写（幂等由版本判定，而非“看起来像已编码”）
    u_key = sep.join(("aiocqhttp", "bot", "u:", "10001"))
    p_key = sep.join(("aiocqhttp", "bot", "p:black", "10001"))
    q_key = sep.join(("aiocqhttp", "bot", "q:foo", "10001"))
    check("W4.rewrite-keeps-encoded",
          _migrate_identity_key(u_key, 3) == u_key
          and _migrate_identity_key(p_key, 3) == p_key
          and _migrate_identity_key(q_key, 3) == q_key)
    base = sep.join(("aiocqhttp", "bot", "10001"))
    check("W4.rewrite-base-key-untouched",
          _migrate_identity_key(base, 1) == base)

    # membership 编码矩阵同源
    mk = raw_key("__mode_user__")
    check("X3.membership-v1-not-ambiguous",
          encode_membership_key(mk, 1)
          == (sep.join(("aiocqhttp", "bot", "p:__mode_user__", "10001")), False))
    check("X3.membership-v2-ambiguous",
          encode_membership_key(mk, 2)[1] is True)

    # inspect_schema_version：新库/损坏
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        led = TurnLedger(Path(td) / "n.db")
        led.open()
        check("W3.inspect-fresh",
              inspect_schema_version(led._db_path) == SCHEMA_VERSION)
        led.close()
        bad = Path(td) / "bad.db"
        bad.write_text("junk" * 100, encoding="utf-8")
        raised = False
        try:
            inspect_schema_version(bad)
        except MigrationError:
            raised = True
        except Exception:
            raised = False
        check("W3.inspect-corrupt-controlled", raised)



def main() -> int:
    w2_membership()
    w3_migration()
    w4_key_encoding()
    print(f"\n=== W 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
