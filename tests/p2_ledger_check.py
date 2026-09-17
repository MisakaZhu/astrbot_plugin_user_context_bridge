"""P2 轮次账本验证（MIS-138）。

覆盖 A07（去重）、A09（多连接并发与同库双实例防护）、A12（重启恢复）、
A13（长上下文裁剪）、A18（敏感内容边界）的存储层断言。

运行：PYTHONPATH=. python tests/p2_ledger_check.py（不依赖宿主 venv）
"""

from __future__ import annotations

import concurrent.futures
import json
import tempfile
import time
from pathlib import Path

from uctx_bridge.ledger import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    EpochStaleError,
    LeaseConflictError,
    TurnLedger,
    sanitize_messages,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def user_msg(text: str) -> dict:
    return {"role": "user", "content": text}


def asst_msg(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def scenario_dedup() -> None:
    """A07：一轮只写一次；重复事件/重复完成通知幂等。"""
    with tempfile.TemporaryDirectory() as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        t1 = led.begin_turn(
            identity_key="ID1",
            event_key="evt-1",
            source_type="group",
            source_id="700000001",
            umo="aiocqhttp:GroupMessage:700000001",
            user_message=user_msg("第一问"),
        )
        t2 = led.begin_turn(  # 重复事件
            identity_key="ID1",
            event_key="evt-1",
            source_type="group",
            source_id="700000001",
            umo="aiocqhttp:GroupMessage:700000001",
            user_message=user_msg("第一问"),
        )
        check("A07.dup-event-same-turn", t1.id == t2.id and t2.seq == t1.seq)

        c1 = led.commit_turn(
            event_key="evt-1",
            status=STATUS_COMPLETED,
            trajectory=[asst_msg("第一答")],
            reply_text="第一答",
        )
        c2 = led.commit_turn(  # 重复完成通知
            event_key="evt-1",
            status=STATUS_COMPLETED,
            trajectory=[asst_msg("第一答")],
            reply_text="第一答",
        )
        check(
            "A07.dup-commit-idempotent",
            c1.status == STATUS_COMPLETED
            and c2.status == STATUS_COMPLETED
            and led.get_turn("evt-1").status == STATUS_COMPLETED,
        )
        # 完成后再以失败提交：仍保持首个终态，不翻转
        c3 = led.commit_turn(event_key="evt-1", status=STATUS_FAILED)
        check("A07.terminal-not-flipped", c3.status == STATUS_COMPLETED)

        history = led.load_history("ID1")
        check(
            "A07.single-pair",
            len(history) == 2
            and history[0] == user_msg("第一问")
            and history[1] == asst_msg("第一答"),
            f"history={history}",
        )
        led.close()


def scenario_concurrent_multi_connection() -> None:
    """A09：多连接并发 begin/commit——顺序确定、无覆盖、唯一约束有效。"""
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "l.db")
        writers = [TurnLedger(db) for _ in range(4)]
        for w in writers:
            w.open()

        def write_one(i: int):
            led = writers[i % len(writers)]
            turn = led.begin_turn(
                identity_key="IDC",
                event_key=f"evt-c-{i}",
                source_type="group",
                source_id="700000001",
                umo="aiocqhttp:GroupMessage:700000001",
                user_message=user_msg(f"问题{i}"),
            )
            led.commit_turn(
                event_key=f"evt-c-{i}",
                status=STATUS_COMPLETED,
                trajectory=[asst_msg(f"回答{i}")],
                reply_text=f"回答{i}",
            )
            return turn.seq

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            seqs = sorted(ex.map(write_one, range(24)))

        check(
            "A09.concurrent-seq-unique",
            len(seqs) == 24 and len(set(seqs)) == 24 and seqs == list(range(1, 25)),
            f"seqs={seqs}",
        )
        reader = TurnLedger(db)
        reader.open()
        history = reader.load_history("IDC")
        pairs = [
            (history[i], history[i + 1]) for i in range(0, len(history), 2)
        ]
        check(
            "A09.no-loss-no-overwrite",
            len(history) == 48
            and all(p[0]["role"] == "user" and p[1]["role"] == "assistant" for p in pairs),
            f"len={len(history)}",
        )
        for w in writers + [reader]:
            w.close()


def scenario_dual_instance_guard() -> None:
    """A09：同库双实例——新鲜租约冲突，过期租约可接管。"""
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "l.db")
        a = TurnLedger(db)
        a.open()
        a.acquire_lease("lease-A", pid=111)
        b = TurnLedger(db)
        b.open()
        try:
            b.acquire_lease("lease-B", pid=222)
            dual_conflict = False
        except LeaseConflictError:
            dual_conflict = True
        check("A09.dual-instance-conflict", dual_conflict, "新鲜租约必须冲突")

        a.close()
        # 模拟过期：直接改心跳
        import sqlite3

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE instance_leases SET heartbeat_at=? WHERE lease_id='lease-A'",
            (time.time() - 3600,),
        )
        conn.commit()
        conn.close()
        try:
            b.acquire_lease("lease-B", pid=222)
            takeover = True
        except LeaseConflictError:
            takeover = False
        check("A09.stale-lease-takeover", takeover, "过期租约应可接管")
        b.close()


def scenario_epoch_and_reset() -> None:
    """A11 存储层：epoch 清空后旧轮次不可见、旧慢请求提交被拒。"""
    with tempfile.TemporaryDirectory() as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        led.begin_turn(
            identity_key="IDE",
            event_key="e1",
            source_type="group",
            source_id="g",
            umo="u",
            user_message=user_msg("旧问题"),
        )
        led.commit_turn(
            event_key="e1", status=STATUS_COMPLETED, trajectory=[asst_msg("旧回答")]
        )
        check("A11.before-reset-visible", len(led.load_history("IDE")) == 2)

        new_epoch = led.bump_epoch("IDE")
        check("A11.epoch-bumped", new_epoch == 2)
        check("A11.after-reset-empty", led.load_history("IDE") == [])

        # 清空前登记的慢请求提交 → EpochStaleError，不复活
        led.begin_turn(
            identity_key="IDE",
            event_key="e-slow",
            source_type="group",
            source_id="g",
            umo="u",
            user_message=user_msg("清空前的慢请求"),
        )
        # 手动把该轮挪回旧 epoch 模拟清空前登记
        import sqlite3 as s3

        conn = led._require_conn()
        conn.execute(
            "UPDATE turns SET epoch=1 WHERE event_key='e-slow'"
        )
        try:
            led.commit_turn(
                event_key="e-slow",
                status=STATUS_COMPLETED,
                trajectory=[asst_msg("迟到的回答")],
            )
            stale_rejected = False
        except EpochStaleError:
            stale_rejected = True
        check("A11.stale-commit-rejected", stale_rejected, "旧 epoch 提交必须被拒")
        check("A11.history-still-empty", led.load_history("IDE") == [])

        # 其他身份不受 reset 影响
        led.begin_turn(
            identity_key="OTHER",
            event_key="o1",
            source_type="group",
            source_id="g",
            umo="u",
            user_message=user_msg("别人的问题"),
        )
        led.commit_turn(
            event_key="o1", status=STATUS_COMPLETED, trajectory=[asst_msg("别人的回答")]
        )
        led.bump_epoch("IDE")
        check(
            "A11.reset-only-self",
            len(led.load_history("OTHER")) == 2,
            "reset 只影响本人身份",
        )
        led.close()


def scenario_restart_recovery() -> None:
    """A12：重启后已提交历史保留，遗留 running 恢复为 interrupted。"""
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "l.db")
        led = TurnLedger(db)
        led.open()
        led.acquire_lease("lease-X", pid=1)
        led.begin_turn(
            identity_key="IDR",
            event_key="r1",
            source_type="group",
            source_id="g",
            umo="u",
            user_message=user_msg("已完成的问"),
        )
        led.commit_turn(
            event_key="r1", status=STATUS_COMPLETED, trajectory=[asst_msg("已完成的答")]
        )
        led.begin_turn(  # 挂起轮次（进程“崩溃”）
            identity_key="IDR",
            event_key="r2",
            source_type="group",
            source_id="g",
            umo="u",
            user_message=user_msg("崩溃时的问"),
        )
        led.close()

        # 模拟重启：新实例恢复
        led2 = TurnLedger(db)
        led2.open()
        recovered = led2.recover_running(own_lease_id="lease-X")
        check("A12.running-recovered", recovered == 1, f"recovered={recovered}")
        t2 = led2.get_turn("r2")
        check("A12.running-now-interrupted", t2.status == STATUS_INTERRUPTED)
        check(
            "A12.committed-preserved",
            len(led2.load_history("IDR")) == 2,
            "已提交历史必须保留",
        )
        # interrupted 轮次不进入历史
        check(
            "A12.interrupted-not-in-history",
            all("崩溃时的问" != m.get("content") for m in led2.load_history("IDR")),
        )
        led2.close()


def scenario_trim_and_sanitize() -> None:
    """A13 裁剪尾部保留；A18 敏感内容边界（思考/多模态降级）。"""
    with tempfile.TemporaryDirectory() as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        for i in range(10):
            ek = f"t{i}"
            led.begin_turn(
                identity_key="IDT",
                event_key=ek,
                source_type="group",
                source_id="g",
                umo="u",
                user_message=user_msg(f"问{i}"),
            )
            led.commit_turn(
                event_key=ek,
                status=STATUS_COMPLETED,
                trajectory=[asst_msg(f"答{i}")],
            )
        full = led.load_history("IDT")
        trimmed = led.load_history("IDT", max_turns=3)
        check(
            "A13.trim-keeps-recent",
            len(trimmed) == 6
            and trimmed[0] == user_msg("问7")
            and trimmed[-1] == asst_msg("答9"),
            f"trimmed_head={trimmed[0]}",
        )
        check("A13.full-untrimmed", len(full) == 20)

        # A18：think 与多模态 base64 不入库
        dirty = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "think", "think": "隐藏思考不该存"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                    {"type": "audio_url", "audio_url": {"url": "file:///tmp/x.wav"}},
                    {"type": "text", "text": "临时", "_no_save": True},
                ],
            },
            {"role": "_checkpoint", "content": {"id": "ckpt"}},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
                "content": None,
            },
            {
                "role": "tool",
                "tool_call_id": "call1",
                "content": "工具结果文本",
            },
        ]
        cleaned = sanitize_messages(dirty)
        texts = json.dumps(cleaned, ensure_ascii=False)
        check(
            "A18.think-removed",
            all(p.get("type") != "think" for m in cleaned for p in (m.get("content") or []) if isinstance(m.get("content"), list)),
            f"cleaned={texts}",
        )
        check("A18.base64-removed", "base64" not in texts and "AAAA" not in texts)
        check("A18.image-placeholder", "[图片]" in texts and "[音频]" in texts)
        check("A18.checkpoint-dropped", all(m.get("role") != "_checkpoint" for m in cleaned))
        check("A18.no-save-dropped", "临时" not in texts)
        tool_asst = next(m for m in cleaned if m["role"] == "assistant")
        check(
            "A18.tool-structure-kept",
            tool_asst["tool_calls"][0]["function"]["name"] == "search"
            and cleaned[-1]["role"] == "tool",
            "工具调用配对结构必须保留",
        )
        led.close()


def scenario_status_variants() -> None:
    """终态变体：failed / aborted 不进入共享历史。"""
    with tempfile.TemporaryDirectory() as td:
        led = TurnLedger(Path(td) / "l.db")
        led.open()
        for ek, st in (("f1", STATUS_FAILED), ("a1", STATUS_ABORTED)):
            led.begin_turn(
                identity_key="IDS",
                event_key=ek,
                source_type="group",
                source_id="g",
                umo="u",
                user_message=user_msg(f"{ek}问"),
            )
            led.commit_turn(event_key=ek, status=st, trajectory=[])
        check(
            "A10.failed-aborted-not-in-history",
            led.load_history("IDS") == [],
            "失败/中止轮次不得进入共享历史",
        )
        led.close()


def main() -> int:
    scenario_dedup()
    scenario_concurrent_multi_connection()
    scenario_dual_instance_guard()
    scenario_epoch_and_reset()
    scenario_restart_recovery()
    scenario_trim_and_sanitize()
    scenario_status_variants()
    print(f"\n=== P2 轮次账本验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
