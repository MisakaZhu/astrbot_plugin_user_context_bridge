"""第三轮返工回归（T1~T6）：把三次验收独立反例转为仓库内自动断言。

T2/T4 进程内（真实调度链 harness）；T1/T5/T6 经真实宿主组件子进程 worker
（真实 PersonaManager/ConversationManager/Context/WakingCheckStage/
StarRequestSubStage/builtin commands，插件经真实 import 装配）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/t_rework_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.fakes import FakeEvent, FakeProvider
from tests.harness import drive_pipeline, make_bridge_stack
from tests.p3_bridge_flow_check import FakePersonaManager, cleanup, register_bridge
from tests.r_rework_check import identity_of
from tests.w_lifecycle_check import (
    _FakeCompleted,
    _run_worker as _real_w_run_worker,
    assert_worker_fields,
    check as _w_check,
    PASS as _W_PASS,
    FAIL as _W_FAIL,
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


# ---------------------------------------------------------------------------
# T2：锁后取消窗口（等待人格解析时取消）
# ---------------------------------------------------------------------------
async def t2_cancel_window() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=0.1
        )
        entered = asyncio.Event()

        class WaitingPersona(FakePersonaManager):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def resolve_selected_persona(self, **kw):
                self.calls += 1
                if self.calls == 2:  # 身份解析（第 1 次）后的开场白解析
                    entered.set()
                    await asyncio.Event().wait()
                return await super().resolve_selected_persona(**kw)

        waiting_persona = WaitingPersona()
        bridge._get_persona_manager = lambda: waiting_persona
        metas = register_bridge(bridge)
        try:
            a = FakeEvent(sender_id="10001", group_id="700000001", message_str="A")
            pa = FakeProvider(["A-REPLY"])
            ta = asyncio.create_task(drive_pipeline(bridge, a, pa, prompt="A"))
            await asyncio.wait_for(entered.wait(), 3)
            ta.cancel()
            t2_outcomes = await asyncio.gather(ta, return_exceptions=True)
            # U4：非预期异常（如夹具 UnboundLocalError）必须暴露为失败
            check(
                "T2.cancel-task-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and a.is_stopped())
                    for o in t2_outcomes
                ),
                f"outcomes={[type(o).__name__ for o in t2_outcomes]}",
            )
            await asyncio.sleep(0.15)

            check("T2.cancel-no-model", len(pa.call_log) == 0,
                  f"calls={len(pa.call_log)}")
            check("T2.cancel-no-output", not a.sent_chains)
            check("T2.cancel-stops-event", a.is_stopped(),
                  "取消必须终止事件传播（宿主吞取消后不再执行模型）")
            with sqlite3.connect(ledger._db_path) as db:
                states = db.execute("SELECT status FROM turns").fetchall()
            check(
                "T2.cancel-turn-finalized",
                states and states[0][0] == "interrupted",
                f"states={states}",
            )
            lock = bridge._identity_locks.get(identity_of("10001"))
            check(
                "T2.lock-released",
                lock is None or not lock.locked(),
                "取消后身份锁必须释放",
            )
            check("T2.no-pending", bridge.pending_count == 0)

            # 后继跨窗请求真实完成
            b = FakeEvent(sender_id="10001", group_id="700000002", message_str="B")
            pb = FakeProvider(["B-REPLY"])
            try:
                await asyncio.wait_for(
                    drive_pipeline(bridge, b, pb, prompt="B"), 3
                )
                done = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                done = False
            check("T2.next-request-completes", done, "B 3s 内完成")

            # 登记前取消（初始人格解析等待，未取得锁/轮次——U4 收紧断言）
            c = FakeEvent(sender_id="30003", group_id="700000001", message_str="C")
            pc3 = FakeProvider(["C-MUST-NOT-RUN"])
            resolve_entered = asyncio.Event()

            class SlowPersona(FakePersonaManager):
                async def resolve_selected_persona(self, **kw):
                    resolve_entered.set()
                    await asyncio.Event().wait()

            slow_persona = SlowPersona()
            bridge._get_persona_manager = lambda: slow_persona
            tc = asyncio.create_task(drive_pipeline(bridge, c, pc3, prompt="C"))
            await asyncio.wait_for(resolve_entered.wait(), 3)
            tc.cancel()
            t2_pre_outcomes = await asyncio.gather(tc, return_exceptions=True)
            await asyncio.sleep(0.05)
            check(
                "T2.pre-registration-cancel-clean",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and c.is_stopped())
                    for o in t2_pre_outcomes
                )
                and c.is_stopped()
                and len(pc3.call_log) == 0
                and not c.sent_chains
                and not any(l.locked() for l in bridge._identity_locks.values()),
                f"outcomes={[type(o).__name__ for o in t2_pre_outcomes]} "
                f"stopped={c.is_stopped()} calls={len(pc3.call_log)}",
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# U1：所有等待点的取消语义（初始人格解析 / 等待身份锁）
# ---------------------------------------------------------------------------
async def u1_cancellation_windows() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=5
        )
        entered = asyncio.Event()

        class WaitingPersona(FakePersonaManager):
            async def resolve_selected_persona(self, **kw):
                entered.set()
                await asyncio.Event().wait()

        wp = WaitingPersona()
        bridge._get_persona_manager = lambda: wp
        metas = register_bridge(bridge)
        try:
            ev = FakeEvent(
                sender_id="10001", group_id="700000001",
                message_str="cancel-before-lock",
            )
            provider = FakeProvider(["REPLIED-AFTER-CANCEL"])
            task = asyncio.create_task(
                drive_pipeline(bridge, ev, provider, prompt=ev.message_str)
            )
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            outcomes = await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0.05)
            check(
                "U1.initial-resolve-cancel-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and ev.is_stopped())
                    for o in outcomes
                ),
                "取消被宿主钩子吞掉后必须走受控 stopped 分支"
                f"（outcomes={[type(o).__name__ for o in outcomes]}）",
            )
            check("U1.initial-resolve-stops-event", ev.is_stopped())
            check(
                "U1.initial-resolve-no-model",
                len(provider.call_log) == 0,
                f"calls={len(provider.call_log)}",
            )
            check("U1.initial-resolve-no-output", not ev.sent_chains)
            with sqlite3.connect(ledger._db_path) as db:
                rows0 = db.execute("SELECT status FROM turns").fetchall()
            check("U1.initial-resolve-no-turn", not rows0, f"rows={rows0}")
            check(
                "U1.initial-resolve-no-lock-leak",
                not any(l.locked() for l in bridge._identity_locks.values()),
            )
        finally:
            bridge.shutdown()
            cleanup(metas)
            ledger.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=5
        )
        entered, release = asyncio.Event(), asyncio.Event()

        class Hanging(FakeProvider):
            async def text_chat(self, **kwargs):
                self.call_log.append({"contexts": []})
                entered.set()
                await release.wait()
                return await super().text_chat(**kwargs)

        metas = register_bridge(bridge)
        try:
            a = FakeEvent(sender_id="10001", group_id="700000001", message_str="A")
            b = FakeEvent(
                sender_id="10001", group_id="700000002",
                message_str="B-cancel-queued",
            )
            pa = Hanging(["A-ANSWER"])
            pb = FakeProvider(["B-REPLIED-AFTER-CANCEL"])
            ta = asyncio.create_task(drive_pipeline(bridge, a, pa, prompt="A"))
            await asyncio.wait_for(entered.wait(), 3)
            tb = asyncio.create_task(
                drive_pipeline(bridge, b, pb, prompt=b.message_str)
            )
            for _ in range(200):
                if any(
                    len(lock._waiters or [])
                    for lock in bridge._identity_locks.values()
                ):
                    break
                await asyncio.sleep(0.01)
            waiters = sum(
                len(lock._waiters or [])
                for lock in bridge._identity_locks.values()
            )
            check("U1.queued-waiter-present", waiters == 1, f"waiters={waiters}")
            tb.cancel()
            outcomes = await asyncio.gather(tb, return_exceptions=True)
            check(
                "U1.queued-cancel-controlled",
                all(
                    isinstance(o, asyncio.CancelledError)
                    or (isinstance(o, dict) and b.is_stopped())
                    for o in outcomes
                ),
                "取消被宿主钩子吞掉后必须走受控 stopped 分支"
                f"（outcomes={[type(o).__name__ for o in outcomes]}）",
            )
            check("U1.queued-cancel-stops-event", b.is_stopped())
            check(
                "U1.queued-cancel-no-model",
                len(pb.call_log) == 0,
                f"calls={len(pb.call_log)}",
            )
            check("U1.queued-cancel-no-output", not b.sent_chains)
            check(
                "U1.a-unaffected-by-b-cancel",
                not ta.done(),
                "A 不得因 B 取消受影响",
            )
            release.set()
            await asyncio.wait_for(ta, 5)
            with sqlite3.connect(ledger._db_path) as db:
                rows = db.execute("SELECT status FROM turns").fetchall()
            check(
                "U1.only-a-turn",
                len(rows) == 1 and rows[0][0] == "completed",
                f"rows={rows}",
            )
            check(
                "U1.lock-released-after-a",
                not any(l.locked() for l in bridge._identity_locks.values()),
            )
            c = FakeEvent(sender_id="10001", group_id="700000002", message_str="C-next")
            pc = FakeProvider(["C-ANSWER"])
            try:
                await asyncio.wait_for(
                    drive_pipeline(bridge, c, pc, prompt="C-next"), 3
                )
                done = True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                done = False
            check("U1.next-request-completes", done)
        finally:
            release.set()
            bridge.shutdown()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# T4：buffer 正文抑制（watchdog 后不得再发旧正文）
# ---------------------------------------------------------------------------
async def t4_buffer_suppression() -> None:
    import tests.harness as harness
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.entities import LLMResponse

    real_run_agent = harness.run_agent

    async def with_buffer(*args, **kwargs):
        kwargs["buffer_intermediate_messages"] = True
        async for result in real_run_agent(*args, **kwargs):
            yield result

    harness.run_agent = with_buffer
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            bridge, resolver, membership, ledger = make_bridge_stack(
                td, fail_watchdog_seconds=0.2
            )
            metas = register_bridge(bridge)
            entered, release = asyncio.Event(), asyncio.Event()

            class Tool(FunctionTool):
                async def call(self, context, **kwargs):
                    return "LOCAL-RESULT"

            class Provider(FakeProvider):
                async def text_chat(self, **kwargs):
                    if not self.call_log:
                        await FakeProvider.text_chat(self, **kwargs)
                        return LLMResponse(
                            role="assistant",
                            completion_text="BUFFERED-REPLY-BEFORE-TIMEOUT",
                            tools_call_name=["local_tool"],
                            tools_call_args=[{}],
                            tools_call_ids=["local-call-1"],
                        )
                    entered.set()
                    await release.wait()
                    return await super().text_chat(**kwargs)

            toolset = ToolSet(
                [
                    Tool(
                        name="local_tool",
                        description="synthetic",
                        parameters={"type": "object", "properties": {}},
                    )
                ]
            )
            ev = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="buffer test"
            )
            t = asyncio.create_task(
                harness.drive_pipeline(
                    bridge, ev, Provider(["FINAL"]), prompt="buffer test",
                    func_tool=toolset,
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            at_timeout = [c.get_plain_text() for c in ev.sent_chains]
            await asyncio.sleep(0.35)
            after_timeout = [c.get_plain_text() for c in ev.sent_chains]
            release.set()
            await asyncio.wait_for(t, 3)
            final = [c.get_plain_text() for c in ev.sent_chains]
            with sqlite3.connect(ledger._db_path) as db:
                states = db.execute("SELECT status FROM turns").fetchall()
            check(
                "T4.no-new-output-after-timeout",
                after_timeout == at_timeout,
                f"before={at_timeout} after={after_timeout}",
            )
            check(
                "T4.no-buffered-late-output",
                not any("BUFFERED-REPLY" in (x or "") for x in final),
                f"final={final}",
            )
            check(
                "T4.failed-finalized",
                states and states[0][0] == "failed",
                f"states={states}",
            )
            bridge.shutdown()
            cleanup(metas)
            ledger.close()
    finally:
        harness.run_agent = real_run_agent


# ---------------------------------------------------------------------------
# 真实宿主组件 worker（T1/T5/T6）
# ---------------------------------------------------------------------------
def _run_worker(script: str, extra_args: list[str] | None = None):
    """Z1a：统一走 tests/worker_result 判定（rc/缺行/坏 JSON/非对象/
    超时/启动失败），不再只看 RESULT 行。"""

    from tests import worker_result

    repo = Path(__file__).resolve().parent.parent
    results = {}
    for venv in (r"D:\第三方插件完善\.venv", r"D:\第三方插件完善\.venv426"):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            env = dict(os.environ)
            env.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(repo),
                }
            )
            r = worker_result.safe_run(
                [
                    venv + r"\Scripts\python.exe",
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve().parent / script),
                    td,
                ]
                + (extra_args or []),
                env=env,
                cwd=str(repo),
                timeout=300,
            )
            tag = Path(venv).name
            parsed = worker_result.read_worker_result(r)
            ev = worker_result.persist_run(
                Path(script).stem, tag, r, parsed)
            parsed["_evidence_log"] = ev["log"]
            parsed["_evidence_json"] = ev["json"]
            if "__error__" in parsed:
                parsed["__error__"] += "（留证：" + ev["json"] + "）"
            results[tag] = parsed
    return results


def worker_entry_fault_injection() -> None:
    """Z1a/N23：故障注入经**正式 t1_t5_t6_workers** 及其实际 subprocess
    处理路径（patch tests.worker_result.safe_run——入口真实使用的执行
    函数）；正常对照必须过，rc19/缺 RESULT/坏 JSON/JSON 非对象/必要
    字段缺失逐个必须判 FAIL。对照 stdout 取自本函数先前对同一 worker
    的真实运行结果（重新序列化 @@RESULT@@ 行），不复制校验逻辑。"""

    from unittest.mock import patch

    from tests import worker_result

    real = {
        script: _run_worker(script)
        for script in ("t1_persona_worker.py", "t5_native_worker.py")
    }
    for script, by_tag in real.items():
        if any("__error__" in o for o in by_tag.values()):
            check("Z1.t-entry-baseline", False,
                  f"真实基线运行失败：{script} {by_tag}")
            return
    check("Z1.t-entry-baseline", True)

    class FakeResult:
        def __init__(self, rc, stdout, stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    # (script, tag) -> doctored FakeResult；venv 由 cmd[0] 区分
    def build_results(make):
        table = {}
        for script, by_tag in real.items():
            for tag, out in by_tag.items():
                py = (r"D:\第三方插件完善\.venv" if tag == ".venv"
                      else r"D:\第三方插件完善\.venv426") + r"\Scripts\python.exe"
                table[(script, py)] = make(out)
        return table

    def patched_call(table):
        def _fake(cmd, **kwargs):
            key = next(
                ((s, cmd[0]) for (s, py) in table if s in " ".join(cmd)
                 and cmd[0] == py),
                None,
            )
            if key is None:
                return FakeResult(1, "", f"unmatched cmd: {' '.join(cmd)[:120]}")
            return table[key]
        return _fake

    def run_case(name, make, *, expect_fail: bool) -> None:
        before_pass, before_fail = len(PASS), len(FAIL)
        table = build_results(make)
        with patch.object(worker_result, "safe_run", new=patched_call(table)):
            t1_t5_t6_workers()
        detected = len(FAIL) > before_fail
        # 判定已由本 check 记录；回滚本轮新增的全局 PASS/FAIL（属注入
        # 数据而非真实运行），保持套件级 0 FAIL 语义
        del PASS[before_pass:]
        del FAIL[before_fail:]
        check(
            f"Z1.t-entry-{name}",
            detected if expect_fail else not detected,
            f"expect_fail={expect_fail} detected={detected}",
        )

    run_case("normal-passes",
             lambda out: FakeResult(
                 0, "@@RESULT@@" + json.dumps(out, ensure_ascii=False) + "\n"),
             expect_fail=False)
    run_case("rc19-detected",
             lambda out: FakeResult(
                 19, "@@RESULT@@" + json.dumps(out, ensure_ascii=False) + "\n",
                 "injected nonzero exit"),
             expect_fail=True)
    run_case("no-result-line-detected",
             lambda out: FakeResult(0, "no marker here\n"),
             expect_fail=True)
    run_case("bad-json-detected",
             lambda out: FakeResult(0, "@@RESULT@@{oops\n"),
             expect_fail=True)
    run_case("json-array-detected",
             lambda out: FakeResult(0, "@@RESULT@@[1,2]\n"),
             expect_fail=True)
    run_case("missing-field-detected",
             lambda out: FakeResult(0, "@@RESULT@@{}\n"),
             expect_fail=True)

    # AB1/AC1：合法 JSON 的功能失败也必须留证——翻假 n04_all_model_called
    # 后经正式入口+_assert_t1_version 正式断言必须 FAIL，且留证 JSON 可
    # 重开、含 T 的窗口级诊断；撤掉故障则"应检出"自检本身必须失败。
    good_t1 = real["t1_persona_worker.py"]
    if any("__error__" in v for v in good_t1.values()):
        check("AB1.t-semantic-baseline", False,
              f"真实 t1 基线失败：{str(good_t1)[:200]}")
    else:
        def _t1_semantic_case(fault: bool, label: str) -> dict:
            """对每个版本注入（或不注入）n04_all_model_called=False 后
            经正式入口+正式 _assert_t1_version 断言，返回逐版本结果。"""
            notes = []
            for tag, src in good_t1.items():
                doctored = dict(src)
                if fault:
                    doctored["n04_all_model_called"] = False
                line = ("@@RESULT@@"
                        + json.dumps(doctored, ensure_ascii=False) + "\n")
                entry_lf: list = []
                entry_detail: dict = {}

                def entry_check(n, cond, detail=""):
                    if not cond:
                        entry_lf.append(n)
                        entry_detail[n] = detail

                with patch.object(worker_result, "safe_run",
                                  return_value=FakeResult(0, line)):
                    read = _run_worker("t1_persona_worker.py")
                inner = read.get(tag, {})
                ev_json = inner.get("_evidence_json", "")
                ev_log = inner.get("_evidence_log", "")

                def ev_check(n, cond, detail=""):
                    if not cond and ev_json:
                        detail = (f"{detail} "
                                  f"[留证:{ev_json}]").strip()
                    entry_check(n, cond, detail)

                _assert_t1_version(tag, inner, check=ev_check)
                reopened = {}
                if ev_json and Path(ev_json).is_file():
                    reopened = json.loads(
                        Path(ev_json).read_text(encoding="utf-8"))
                notes.append({
                    "tag": tag, "label": label,
                    "fault": fault,
                    "detected": bool(entry_lf),
                    "failed_names": entry_lf[:5],
                    "failed_detail": {k: v for k, v in
                                      entry_detail.items() if k in entry_lf},
                    "evidence_json": ev_json,
                    "evidence_log": ev_log,
                    "has_window_diagnostics":
                        "n04_window_diagnostics" in reopened,
                    "reopened_keys": len(reopened),
                })
            return notes

        # 正常对照（无故障）→ 正式断言必须零失败
        normal_notes = _t1_semantic_case(False, "normal")
        check("AB1.t-normal-control-passes",
              not any(v["detected"] for v in normal_notes),
              f"notes={normal_notes}")

        # 故障注入（翻假）→ 必须检出 N04 目标失败 + 留证可重开
        fault_notes = _t1_semantic_case(True, "fault")
        check("AB1.t-semantic-failure-detected",
              all(v["detected"] for v in fault_notes),
              f"notes={fault_notes}")
        check("AB1.t-semantic-evidence-keeps-diagnostics",
              all(v["has_window_diagnostics"] for v in fault_notes),
              f"notes={fault_notes}")

        # AC1：撤掉故障后"应检出"自检必须失败（反向对照）
        check("AB1.t-no-fault-countercontrol",
              not any(v["detected"] for v in normal_notes),
              f"无故障时 detected 应为 False，"
              f"实际={[(v['tag'], v['detected']) for v in normal_notes]}")


def n04_tools_fault_injection() -> None:
    """Z3a/N23：在真实 Runner 的 _func_tool_for_provider 边界丢弃工具
    （tests/z3_tools_fault_observer.py，真实子进程），正式父函数
    t1_t5_t6_workers 原样消费其输出，终模型工具缺失必须判 FAIL。"""

    real_run_worker = _run_worker

    def observer_run_worker(script: str, extra_args=None):
        if script != "t1_persona_worker.py":
            return real_run_worker(script, extra_args)
        results = {}
        repo = Path(__file__).resolve().parent.parent
        observer = str(
            Path(__file__).resolve().parent / "z3_tools_fault_observer.py")
        for venv in (r"D:\第三方插件完善\.venv",
                     r"D:\第三方插件完善\.venv426"):
            tag = Path(venv).name
            with tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True) as td:
                env = dict(os.environ)
                env.update({
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(repo),
                })
                r = subprocess.run(
                    [venv + r"\Scripts\python.exe", "-X", "utf8",
                     observer, td],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=env, cwd=str(repo), timeout=300,
                )
                line = next(
                    (l for l in r.stdout.splitlines()
                     if l.startswith("@@RESULT@@")), None)
                if r.returncode != 0 or line is None:
                    results[tag] = {
                        "__error__": "observer rc=%s: %s" % (
                            r.returncode, (r.stderr or r.stdout)[-300:])
                    }
                else:
                    results[tag] = json.loads(line[len("@@RESULT@@"):])
        return results

    before_pass, before_fail = len(PASS), len(FAIL)
    with patch.object(sys.modules[__name__], "_run_worker",
                      observer_run_worker):
        t1_t5_t6_workers()
    detected = len(FAIL) > before_fail
    del PASS[before_pass:]
    del FAIL[before_fail:]
    check("Z3.n04-tools-drop-detected", detected,
          "expect_fail=True detected=%s" % detected)


import tests.worker_result as worker_result_module
from tests.w_lifecycle_check import _FakeCompleted


import tests.worker_result as worker_result_module


def _validate_n04_negative_diag(out: dict, mode: str) -> tuple:
    """AB2/AC2：对单个版本负例输出做**有意义**的逐窗诊断校验。

    stop：汇总 model_calls == [1,0,0] 且与逐窗一致；A 窗 ran、B/C
    hook-stopped 且停止信号真实；无虚构异常；_aa2_audit.stops >= 2。
    provider-raise：每窗 model_calls >= 1（先记 call_log 再抛，到达
    provider 异常边界）；异常可定位（标记在 final_text_head 或
    pipeline_error）；宿主转错误响应时 pipeline_error 为空属合理。
    """

    diag = out.get("n04_window_diagnostics")
    calls = out.get("n04_model_calls")
    if not isinstance(diag, list) or len(diag) != 3:
        return False, f"window_diagnostics 需 3 窗，实际 {diag!r}"
    if not isinstance(calls, list) or len(calls) != 3:
        return False, f"model_calls 需 3 窗，实际 {calls!r}"
    for i, d in enumerate(diag):
        if not isinstance(d, dict):
            return False, f"win{i} 非对象: {d!r}"
        for k in ("model_calls", "hook_stopped", "event_stopped", "outcome"):
            if k not in d:
                return False, f"win{i} 缺字段 {k}"
    if mode == "stop":
        if calls != [1, 0, 0]:
            return False, f"stop 需 [1,0,0]，实际 {calls}"
        # AC2.2：逐窗 model_calls 必须与汇总一致
        for i, d in enumerate(diag):
            if d.get("model_calls") != calls[i]:
                return False, (f"win{i} model_calls={d.get('model_calls')} "
                               f"与汇总 {calls[i]} 不一致")
        a, b, c = diag
        if a.get("outcome") != "ran" or a.get("hook_stopped") is not False:
            return False, f"A 窗应正常 ran 且无停止: {a!r}"
        for name, d in (("B", b), ("C", c)):
            if not (d.get("outcome") == "hook-stopped"
                    and d.get("hook_stopped") is True
                    and d.get("event_stopped") is True):
                return False, f"{name} 窗停止证据不对: {d!r}"
            if d.get("pipeline_error") or d.get("hook_error"):
                return False, f"{name} 窗虚构异常: {d!r}"
        # AC2.2：_aa2_audit 消费——确认 stop 边界真实执行
        audit = out.get("_aa2_audit")
        if not isinstance(audit, dict) or audit.get("stops", 0) < 2:
            return False, (f"_aa2_audit 停止计数不足: {audit!r}（需 >=2，"
                           "对应 B/C 两窗 stop_event 调用）")
        return True, "ok"
    if mode == "provider-raise":
        for name, d in zip("ABC", diag):
            if d.get("model_calls", 0) < 1:
                return False, (f"{name} 窗未到达 provider 异常边界 "
                               f"(model_calls={d.get('model_calls')!r})")
            marker = (str(d.get("final_text_head") or "")
                      + str(d.get("pipeline_error") or ""))
            if "aa2" not in marker:
                return False, (f"{name} 窗异常不可定位 "
                               f"(final_text_head/pipeline_error 无标记)")
        return True, "ok"
    return False, f"未知 mode {mode!r}"


def run_aa2_negative(mode: str, runner) -> dict:
    """AD1/AD2：正式 AA2 负例判定——逐版本独立验证 + 指定目标 + 证据重开。

    每个版本必须独立满足四项（不能靠另一版本补足）：
    1. 入口有效（无 __error__）；
    2. 指定目标命中：stop→all-model-called、provider-raise→
       all-completed-zero-watchdog-zero-pending（不能由任意 N04.* 替代）；
    3. 诊断有意义（_validate_n04_negative_diag）；
    4. 证据重开验证通过（实际读回 JSON/log 核对诊断与原始输出）。
    两个版本都合格才可整体通过。
    """

    # AD1：逐模式指定必须命中的正式目标断言名
    required_target = {
        "stop": "all-model-called",
        "provider-raise": "all-completed-zero-watchdog-zero-pending",
    }.get(mode)

    before_pass, before_fail = len(PASS), len(FAIL)
    fail_names: list = []
    tag_fail_names: dict = {".venv": [], ".venv426": []}
    outs: dict = {}
    _impl = check

    def recording_check(n, cond, detail=""):
        if not cond:
            fail_names.append(n)
            # 先匹配长 tag（.venv426 包含 .venv 子串）
            for t in (".venv426", ".venv"):
                if t + "." in n + ".":
                    tag_fail_names[t].append(n)
                    break
        _impl(n, cond, detail)

    def recording_runner(script: str, extra_args=None):
        results = runner(script, extra_args)
        if "t1_persona_worker" in script:
            for tag, out in results.items():
                if isinstance(out, dict) and "__error__" not in out:
                    outs[tag] = out
        return results

    with patch.object(sys.modules[__name__], "_run_worker",
                      recording_runner), \
            patch.object(sys.modules[__name__], "check", recording_check):
        t1_t5_t6_workers()
    detected = len(FAIL) > before_fail
    collateral = [n for n in fail_names if not n.startswith("N04.")]
    del PASS[before_pass:]
    del FAIL[before_fail:]

    # AD2：证据重开验证函数（实际读回文件核对内容）
    def _verify_evidence(out: dict, mode: str) -> tuple:
        """实际读回 _evidence_json/_evidence_log 并核对内容。"""
        ev_json_path = out.get("_evidence_json")
        ev_log_path = out.get("_evidence_log")
        if not ev_json_path or not ev_log_path:
            return False, "缺少 _evidence_json/_evidence_log 引用"
        if not Path(ev_json_path).is_file():
            return False, f"证据 JSON 不存在：{ev_json_path}"
        if not Path(ev_log_path).is_file():
            return False, f"证据 log 不存在：{ev_log_path}"
        try:
            reopened = json.loads(
                Path(ev_json_path).read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"证据 JSON 解析失败：{exc}"
        if not isinstance(reopened, dict) or len(reopened) < 5:
            return False, (f"证据 JSON 内容过少 "
                           f"({len(reopened)} keys)，疑似空/损坏")
        raw = Path(ev_log_path).read_text(encoding="utf-8",
                                          errors="replace")
        if "returncode=" not in raw:
            return False, "证据 log 缺 returncode 行"
        if "@@RESULT@@" not in raw:
            return False, "证据 log 缺 @@RESULT@@ 原文"
        # 模式特定内容核对
        diag = reopened.get("n04_window_diagnostics")
        if not isinstance(diag, list) or len(diag) != 3:
            return False, (f"证据 JSON 窗口诊断异常: "
                           f"{type(diag).__name__} len={len(diag) if isinstance(diag, list) else 'N/A'}")
        if mode == "provider-raise":
            # 宿主转错误响应时 pipeline_error 可为空，但标记须在
            combined = str(reopened) + raw
            if "aa2" not in combined:
                return False, "provider-raise 注入标记不可定位"
        return True, "ok"

    # AC2.1 + AD1：逐版本独立验证（指定目标 + 证据重开）
    per_version: dict = {}
    per_version_ok = True
    evidence_ok = True
    evidence_notes: dict = {}
    for tag in (".venv", ".venv426"):
        # AD1：指定目标必须命中（不能由任意 N04.* 替代）
        specific_target_hit = False
        if required_target:
            target_pattern = f"N04.{tag}.{required_target}"
            specific_target_hit = any(
                target_pattern in n
                for n in tag_fail_names.get(tag, []))
        else:
            specific_target_hit = bool(tag_fail_names.get(tag, []))

        tag_targets = [n for n in tag_fail_names.get(tag, [])
                       if n.startswith("N04.")]
        out = outs.get(tag)
        entry_ok = out is not None and "__error__" not in out
        diag_ok_v = False
        diag_note_v = ""
        evidence_note_v = ""
        evidence_ok_v = False
        if entry_ok:
            diag_ok_v, diag_note_v = _validate_n04_negative_diag(out, mode)
            # AD2：证据重开验证
            evidence_ok_v, evidence_note_v = _verify_evidence(out, mode)
        else:
            diag_note_v = f"入口错误: {str(out.get('__error__'))[:120]}" \
                if out else "缺少该版本结果"
            evidence_note_v = "入口错误，无证据可验"
        v_ok = (entry_ok and specific_target_hit and diag_ok_v
                and evidence_ok_v)
        per_version[tag] = {
            "entry_ok": entry_ok,
            "specific_target": required_target,
            "specific_target_hit": specific_target_hit,
            "target_hit": bool(tag_targets),
            "target_fail_names": tag_targets[:5],
            "diag_ok": diag_ok_v,
            "diag_note": diag_note_v,
            "evidence_ok": evidence_ok_v,
            "evidence_note": evidence_note_v,
            "evidence_json": out.get("_evidence_json", "") if out else "",
            "ok": v_ok,
        }
        if not evidence_ok_v:
            evidence_ok = False
            evidence_notes[tag] = evidence_note_v
        if not v_ok:
            per_version_ok = False

    diag_notes = {t: per_version[t]["diag_note"] for t in per_version}

    return {
        "mode": mode,
        "detected": detected,
        "per_version": per_version,
        "per_version_ok": per_version_ok,
        "no_collateral": not collateral,
        "collateral": collateral[:5],
        "evidence_ok": evidence_ok,
        "evidence_notes": evidence_notes,
        "pass": (detected and per_version_ok and not collateral
                 and evidence_ok),
    }


def n04_diag_negative_injection() -> None:
    """AC2/AC3：负例判定收紧 + 观察器留证。

    - 正常对照先通过相同正式父断言（无故障 → 无失败）；
    - 真实 stop / provider-raise 负例必须逐版本命中 N04 目标失败、
      诊断有意义、无无关失败，才记 accepted；
    - 四种原坏观测 + 三种 AC2 新坏观测反向检验均 rejected；
    - 观察器每次运行 persist_run 落盘，manifest 关联 verdict/证据。
    """

    real_run_worker = _run_worker
    evidence_dir = Path(__file__).resolve().parent.parent / (
        "local_evidence") / "ac_logs"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def save_verdict(name: str, verdict: dict) -> str:
        path = evidence_dir / f"ac2_{name}.json"
        path.write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2,
                       default=str),
            encoding="utf-8")
        return str(path)

    def observer_runner(mode: str):
        """AC3：每次观察器运行都 persist_run 落盘（raw stdout/stderr/rc、
        完整解析 JSON、AUDIT），返回结果附带证据路径。"""
        def runner(script: str, extra_args=None):
            if script != "t1_persona_worker.py":
                return real_run_worker(script, extra_args)
            results = {}
            repo = Path(__file__).resolve().parent.parent
            observer = str(Path(__file__).resolve().parent / (
                "aa2_n04_diag_observer.py"))
            for venv in (r"D:\第三方插件完善\.venv",
                         r"D:\第三方插件完善\.venv426"):
                tag = Path(venv).name
                with tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True) as td:
                    env = dict(os.environ)
                    env.update({
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONPATH": str(repo),
                    })
                    completed = worker_result_module.safe_run(
                        [venv + r"\Scripts\python.exe", "-X", "utf8",
                         observer, td, mode],
                        env=env, cwd=str(repo), timeout=300)
                    parsed = worker_result_module.read_worker_result(
                        completed)
                    audit = next(
                        (l for l in (completed.stdout or "").splitlines()
                         if l.startswith("@@AUDIT@@")), None)
                    if "__error__" not in parsed and audit:
                        parsed["_aa2_audit"] = json.loads(
                            audit[len("@@AUDIT@@"):])
                    # AC3：每次观察器运行都 persist_run（临时目录清理前）
                    ev = worker_result_module.persist_run(
                        f"aa2_observer_{mode}", tag, completed, parsed)
                    parsed["_evidence_log"] = ev["log"]
                    parsed["_evidence_json"] = ev["json"]
                    results[tag] = parsed
            return results
        return runner

    # 1) 正常对照：无故障经相同正式父断言必须零失败
    before_pass, before_fail = len(PASS), len(FAIL)
    with patch.object(sys.modules[__name__], "_run_worker",
                      real_run_worker):
        t1_t5_t6_workers()
    control_detected = len(FAIL) > before_fail
    del PASS[before_pass:]
    del FAIL[before_fail:]
    check("AA2.normal-control-passes", not control_detected,
          f"control_detected={control_detected}")

    # 2) 真实负例：逐版本必须被判定接受
    for mode in ("stop", "provider-raise"):
        verdict = run_aa2_negative(mode, observer_runner(mode))
        vp = save_verdict(f"{mode}-good", verdict)
        verdict["_verdict_path"] = vp
        check(f"AC2.{mode}-negative-accepted",
              verdict["pass"],
              f"verdict={ {k: v for k, v in verdict.items() if k != 'per_version'} }")

    # 3) 捕获一份真实 stop 负例结果
    neg_capture: dict = {}

    def capture_runner(mode: str):
        base = observer_runner(mode)

        def runner(script: str, extra_args=None):
            if script == "t1_persona_worker.py":
                neg_capture.update(base("t1_persona_worker.py"))
                return dict(neg_capture)
            return real_run_worker(script, extra_args)
        return runner

    with patch.object(sys.modules[__name__], "_run_worker",
                      capture_runner("stop")):
        run_aa2_negative("stop", capture_runner("stop"))
    good_neg_428 = dict(neg_capture.get(".venv", {}))
    good_neg_426 = dict(neg_capture.get(".venv426", {}))
    for d in (good_neg_428, good_neg_426):
        d.pop("_evidence_log", None)
        d.pop("_evidence_json", None)

    def json_line(d):
        return "@@RESULT@@" + json.dumps(d, ensure_ascii=False) + "\n"

    # 4) 四种原坏观测反向检验（保留，不重新实现）
    real_t1 = real_run_worker("t1_persona_worker.py")
    real_t5 = real_run_worker("t5_native_worker.py")

    def rc19_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            results = {}
            for tag, line in ((".venv", json_line(good_neg_428)),
                              (".venv426", json_line(good_neg_426))):
                fr = _FakeCompleted(19, line, "injected rc19")
                parsed = worker_result_module.read_worker_result(fr)
                ev = worker_result_module.persist_run(
                    "ac2-rc19", tag, fr, parsed)
                parsed["_evidence_log"] = ev["log"]
                parsed["_evidence_json"] = ev["json"]
                results[tag] = parsed
            return results
        return real_run_worker(script, extra_args)

    def empty_diag_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            payload = {"n04_window_diagnostics": [],
                       "n04_model_calls": []}
            results = {}
            for tag in (".venv", ".venv426"):
                fr = _FakeCompleted(0, json_line(payload))
                parsed = worker_result_module.read_worker_result(fr)
                ev = worker_result_module.persist_run(
                    "ac2-empty-diag", tag, fr, parsed)
                parsed["_evidence_log"] = ev["log"]
                parsed["_evidence_json"] = ev["json"]
                results[tag] = parsed
            return results
        return real_run_worker(script, extra_args)

    def missing_version_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            return {".venv426": dict(real_t1[".venv426"])}
        return real_run_worker(script, extra_args)

    def unrelated_t5_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            return dict(real_t1)
        if script == "t5_native_worker.py":
            return {".venv": {"__error__": "无关 T5 注入失败"},
                    ".venv426": {"__error__": "无关 T5 注入失败"}}
        return real_run_worker(script, extra_args)

    old_bad = (
        ("old-bad1-rc19", rc19_runner),
        ("old-bad2-empty-diag", empty_diag_runner),
        ("old-bad3-missing-version", missing_version_runner),
        ("old-bad4-unrelated-t5", unrelated_t5_runner),
    )
    for name, runner in old_bad:
        verdict = run_aa2_negative("stop", runner)
        vp = save_verdict(name, verdict)
        check(f"AA2.{name}-rejected", not verdict["pass"],
              f"坏观测未被拒绝：verdict_path={vp}")

    # 5) AC2 新增三种坏观测反向检验（逐项只变对应观测，其余保留）
    def stops_zero_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            d428 = dict(good_neg_428)
            d426 = dict(good_neg_426)
            for d in (d428, d426):
                d["_aa2_audit"] = {"stops": 0}
            return {".venv": d428, ".venv426": d426}
        return real_run_worker(script, extra_args)

    def b_window_mismatch_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            d428 = json.loads(json.dumps(good_neg_428))
            d426 = json.loads(json.dumps(good_neg_426))
            for d in (d428, d426):
                if (isinstance(d.get("n04_window_diagnostics"), list)
                        and len(d["n04_window_diagnostics"]) > 1):
                    d["n04_window_diagnostics"][1]["model_calls"] = 1
            return {".venv": d428, ".venv426": d426}
        return real_run_worker(script, extra_args)

    def one_version_target_runner(script, extra_args=None):
        if script == "t1_persona_worker.py":
            # 4.28 保留真实负例（有 N04 失败）；4.26 用正常结果（无失败）
            normal_426 = dict(real_t1[".venv426"])
            return {".venv": dict(good_neg_428), ".venv426": normal_426}
        return real_run_worker(script, extra_args)

    ac2_bad = (
        ("ac2-bad1-stops-zero", stops_zero_runner),
        ("ac2-bad2-b-window-mismatch", b_window_mismatch_runner),
        ("ac2-bad3-one-version-target", one_version_target_runner),
    )
    for name, runner in ac2_bad:
        verdict = run_aa2_negative("stop", runner)
        vp = save_verdict(name, verdict)
        check(f"AC2.{name}-rejected", not verdict["pass"],
              f"AC2 坏观测未被拒绝：verdict_path={vp}")

    # -- AD1：非指定 N04 失败不能替代指定目标 ------------------------------
    # 从真实负例取诊断/调用/AUDIT，从正常结果取其余正式观测，
    # 仅翻假 n04_final_tool_schema_serializable——指定目标全部通过，
    # 但存在不相关 N04 失败。旧判定会误接受，新判定必须拒绝。

    # 先捕获真实负例（provider-raise 也需要）
    neg_prov_capture: dict = {}
    prov_capture_runner = (lambda mode: (
        lambda script, extra_args=None:
            (dict(neg_prov_capture.update(
                observer_runner(mode)("t1_persona_worker.py")) or {})
             or dict(neg_prov_capture))
            if script == "t1_persona_worker.py"
            else real_run_worker(script, extra_args)
    ))("provider-raise")

    with patch.object(sys.modules[__name__], "_run_worker",
                      prov_capture_runner):
        run_aa2_negative("provider-raise", prov_capture_runner)
    good_prov_428 = dict(neg_prov_capture.get(".venv", {}))
    good_prov_426 = dict(neg_prov_capture.get(".venv426", {}))

    def schema_only_runner_factory(neg_by_tag: dict, mode: str):
        """从正常结果取正式观测、从负例取诊断/AUDIT/调用，仅翻假 schema。"""
        def runner(script, extra_args=None):
            if script == "t1_persona_worker.py":
                normal = real_run_worker(script, extra_args)
                results = {}
                for tag in (".venv", ".venv426"):
                    combined = dict(normal.get(tag, {}))
                    # 诊断/AUDIT/调用来自负例（维持 stop/raise 模式特征）
                    for k in ("n04_window_diagnostics", "n04_model_calls",
                              "_aa2_audit"):
                        if k in neg_by_tag.get(tag, {}):
                            combined[k] = neg_by_tag[tag][k]
                    # AD1 反例：仅翻假 schema（指定目标全部通过）
                    combined["n04_final_tool_schema_serializable"] = False
                    # 证据引用来自负例（有效）
                    for k in ("_evidence_log", "_evidence_json"):
                        if k in neg_by_tag.get(tag, {}):
                            combined[k] = neg_by_tag[tag][k]
                    results[tag] = combined
                return results
            return real_run_worker(script, extra_args)
        return runner

    with patch.object(sys.modules[__name__], "_run_worker",
                      real_run_worker):
        normal_t1_full = real_run_worker("t1_persona_worker.py")

    ad1_bad = (
        ("ad1-stop-schema-only",
         schema_only_runner_factory(good_neg_428 and
                                    {".venv": good_neg_428,
                                     ".venv426": good_neg_426}, "stop")),
        ("ad1-provider-schema-only",
         schema_only_runner_factory({".venv": good_prov_428,
                                     ".venv426": good_prov_426},
                                    "provider-raise")),
    )
    for name, runner in ad1_bad:
        verdict = run_aa2_negative("stop", runner)
        vp = save_verdict(name, verdict)
        # AD1：指定目标缺失→ rejected；失败理由不应为无关新异常
        check(f"AD1.{name}-rejected", not verdict["pass"],
              f"AD1 非指定 N04 失败不应替代指定目标：verdict_path={vp}")

    # -- AD2：证据重开验证 + 证据损坏/缺失/错引用反向检验 -----------------
    # 从上面真实负例捕获的 _evidence_json/_evidence_log 引用带入正式
    # run_aa2_negative，验证实际读回内容而非只数文件数。

    def evidence_bad_runner_factory(evidence_corruptor):
        """构造一个 runner：功能/诊断观测用真实有效负例（good_neg），
        但证据文件/引用按 corruption 类型注入。"""
        def runner(script, extra_args=None):
            if script == "t1_persona_worker.py":
                results = {}
                for tag in (".venv", ".venv426"):
                    src = (good_neg_428 if tag == ".venv"
                           else good_neg_426)
                    combined = dict(src)
                    results[tag] = combined
                # evidence_corruptor 在 results 上修改证据引用/文件
                evidence_corruptor(results)
                return results
            return real_run_worker(script, extra_args)
        return runner

    def corrupt_empty_evidence(results):
        for tag in results:
            results[tag]["_evidence_json"] = str(
                evidence_dir / f"ad2_empty_{tag}.json")
            results[tag]["_evidence_log"] = str(
                evidence_dir / f"ad2_empty_{tag}.log")
            Path(results[tag]["_evidence_json"]).write_text("{}", encoding="utf-8")
            Path(results[tag]["_evidence_log"]).write_text("", encoding="utf-8")

    def corrupt_missing_evidence(results):
        # 不写文件、指向不存在路径——但目录有旧文件可作占位
        for tag in results:
            results[tag]["_evidence_json"] = str(
                evidence_dir / f"ad2_nonexistent_{tag}.json")
            results[tag]["_evidence_log"] = str(
                evidence_dir / f"ad2_nonexistent_{tag}.log")

    def corrupt_wrong_reference(results):
        # 串到另一版本/模式的真实文件（验证引用关联检查）
        for tag in results:
            other_tag = ".venv426" if tag == ".venv" else ".venv"
            other_mode = "provider-raise"
            src = evidence_dir.parent / (
                "worker_evidence")
            candidates = sorted(src.glob(f"*aa2_observer_{other_mode}*{other_tag.replace('.', '')}*"), key=lambda p: p.stat().st_mtime)
            if candidates:
                if candidates[0].suffix == ".json":
                    results[tag]["_evidence_json"] = str(candidates[0])
                    log_cand = candidates[0].with_suffix(".log")
                    if log_cand.is_file():
                        results[tag]["_evidence_log"] = str(log_cand)
                else:
                    results[tag]["_evidence_log"] = str(candidates[0])

    ad2_bad = (
        ("ad2-empty-evidence", corrupt_empty_evidence),
        ("ad2-missing-evidence", corrupt_missing_evidence),
        ("ad2-wrong-reference", corrupt_wrong_reference),
    )
    for name, corruptor in ad2_bad:
        verdict = run_aa2_negative(
            "stop", evidence_bad_runner_factory(corruptor))
        vp = save_verdict(name, verdict)
        # AD2：证据无效→ rejected（功能判定可能仍检出，但证据不合格）
        check(f"AD2.{name}-rejected", not verdict["pass"],
              f"AD2 证据无效不应算成功负例：verdict_path={vp}")

    # -- AC3：从 manifest 实际重开证据验证（非目录计数） -------------------
    for mode in ("stop", "provider-raise"):
        vpath = evidence_dir / f"ac2_{mode}-good.json"
        check(f"AC3.{mode}.verdict-file-exists", vpath.is_file(),
              f"path={vpath}")
        if not vpath.is_file():
            continue
        reopened = json.loads(vpath.read_text(encoding="utf-8"))
        for tag in (".venv", ".venv426"):
            pv = reopened.get("per_version", {}).get(tag, {})
            ev_json_path = pv.get("evidence_json", "")
            if not ev_json_path or not Path(ev_json_path).is_file():
                # 尝试从 observer persist_run 的输出中找
                ev_candidates = sorted(
                    evidence_dir.parent.glob(
                        "worker_evidence/*aa2_observer_*"),
                    key=lambda p: p.stat().st_mtime)
                check(f"AC3.{mode}.{tag}.observer-evidence-exists",
                      len(ev_candidates) >= 2,
                      f"observer_evidence_count={len(ev_candidates)}")
                continue
            ev_data = json.loads(
                Path(ev_json_path).read_text(encoding="utf-8"))
            check(
                f"AC3.{mode}.{tag}.evidence-reopened-diag-valid",
                isinstance(ev_data.get("n04_window_diagnostics"), list)
                and len(ev_data["n04_window_diagnostics"]) == 3,
                f"diag={ev_data.get('n04_window_diagnostics')}")
        check(
            f"AC3.{mode}.manifest-entry",
            reopened.get("mode") == mode,
            f"mode={reopened.get('mode')}")

    _ = good_prov_428  # 防 unused 告警
    _ = good_prov_426


def _assert_t1_version(tag: str, out: dict, *, check) -> None:
    """AC1：T1 逐版本正式断言（供正常父测试与 AB1 语义负例共同调用）。

    从 t1_t5_t6_workers 中提取，保持断言内容完全一致；
    check 参数可替换（留证包装/负例记录用）。
    """
    if "__error__" in out:
        check(f"T1.{tag}.worker", False, out["__error__"])
        return
    check(
        f"T1.{tag}.distinct-persona-keys",
        out.get("a_scope") != out.get("b_scope")
        and out.get("a_scope") not in (None, "__default__"),
        f"out={out}",
    )
    check(
        f"T1.{tag}.b-excludes-a-history",
        out.get("b_received_a_private") is False,
        "",
    )
    check(
        f"T1.{tag}.b-keeps-own-begin-dialogs",
        out.get("b_begin_dialog") is True,
        "",
    )
    check(
        f"T1.{tag}.explicit-conversation-persona",
        out.get("explicit_scope")
        in ("persona_b", "p:persona_b"),
        f"got={out.get('explicit_scope')}",
    )
    check(
        f"T1.{tag}.resolution-failure-controlled",
        out.get("failure_not_captured") is True
        and out.get("failure_key_absent") is True,
        "",
    )
    check(
        f"N04.{tag}.user-single-u-key",
        out.get("n04_single_u_key") is True,
        f"keys={out.get('n04_user_identity_keys')}",
    )
    check(
        f"N04.{tag}.source-personas",
        out.get("n04_source_personas")
        == ["persona_a", "persona_b", "persona_c"],
        f"got={out.get('n04_source_personas')}",
    )
    check(
        f"N04.{tag}.all-model-called",
        out.get("n04_all_model_called") is True,
        f"calls={out.get('n04_model_calls')}",
    )
    check(
        f"N04.{tag}.all-completed-zero-watchdog-zero-pending",
        out.get("n04_all_completed") is True
        and out.get("n04_watchdog_zero") is True
        and out.get("n04_pending_zero") is True,
        f"completed={out.get('n04_all_completed')} "
        f"watchdog={out.get('n04_watchdog_zero')} "
        f"pending={out.get('n04_pending_zero')}",
    )
    check(
        f"N04.{tag}.system-current-persona",
        out.get("n04_system_current_persona") is True,
        f"sp={out.get('n04_system_current_persona')}",
    )
    check(
        f"N04.{tag}.begin-dialog-current",
        out.get("n04_begin_dialog_current") is True,
        "",
    )
    check(
        f"N04.{tag}.tool-preserved",
        out.get("n04_tool_preserved") is True,
        f"ft={out.get('n04_final_tool_names')}",
    )
    check(
        f"N04.{tag}.final-tools-current-persona",
        out.get("n04_final_tools_current_persona") is True,
        f"names={out.get('n04_final_tool_names')}",
    )
    check(
        f"N04.{tag}.final-tools-no-cross-persona",
        out.get("n04_final_tools_no_cross_persona") is True,
        f"names={out.get('n04_final_tool_names')}",
    )
    check(
        f"N04.{tag}.final-tool-schema-serializable",
        out.get("n04_final_tool_schema_serializable") is True,
        "",
    )
    check(
        f"N04.{tag}.dynamic-injection-preserved",
        out.get("n04_dynamic_injection_preserved") is True,
        "",
    )
    check(
        f"N04.{tag}.dynamic-not-persisted",
        out.get("n04_dynamic_not_in_ledger") is True,
        "",
    )
    check(
        f"N04.{tag}.cross-persona-chain",
        out.get("n04_cross_persona_chain") is True,
        f"ctx_b={out.get('n04_debug_bctx')}",
    )


def t1_t5_t6_workers() -> None:
    # T1：真实 PersonaManager/ConversationManager 新会话 + 不同默认人格
    res = _run_worker("t1_persona_worker.py")
    for tag, out in res.items():
        # AC1：正式 T1 断言失败 detail 附留证路径
        _impl = check
        _ev_json = out.get("_evidence_json", "")

        def _ev_check(n, cond, detail=""):
            if not cond and _ev_json:
                detail = f"{detail} [留证:{_ev_json}]".strip()
            _impl(n, cond, detail)

        _assert_t1_version(tag, out, check=_ev_check)

    # T5/T6：真实 Context + 命令分发矩阵
    res = _run_worker("t5_native_worker.py")
    for tag, out in res.items():
        if "__error__" in out:
            check(f"T5T6.{tag}.worker", False, out["__error__"])
            continue
        # T5：真实 Context（两版接口差异）下的 default-reset
        dr = out.get("default-reset", {})
        check(
            f"T5.{tag}.real-context-reset-syncs",
            dr.get("native_update_calls") == 1
            and dr.get("history_after") == 0
            and dr.get("epoch_bumped") is True,
            f"dr={dr}",
        )
        # X5/N10：user 模式真实分发矩阵
        udr = out.get("user-default-reset", {})
        check(
            f"X5.{tag}.user-real-context-reset-syncs",
            udr.get("native_update_calls") == 1
            and udr.get("history_after") == 0
            and udr.get("epoch_bumped") is True,
            f"udr={udr}",
        )
        check(
            f"X5.{tag}.user-new-with-provider-syncs",
            out.get("user-new-with-provider", {}).get("history_after") == 0
            and out.get("user-new-with-provider", {}).get("plugin_notified") is True,
            f"unp={out.get('user-new-with-provider')}",
        )
        # U2 语义：new 不要求 provider——无 provider 的 new 成功仍联动
        check(
            f"X5.{tag}.user-no-provider-new-syncs",
            out.get("user-no-provider-new", {}).get("history_after") == 0
            and out.get("user-no-provider-new", {}).get("plugin_notified") is True,
            f"unp2={out.get('user-no-provider-new')}",
        )
        check(
            f"X5.{tag}.user-renamed-old-name-no-clear",
            out.get("user-renamed-old-name", {}).get("history_after") == 2,
            f"ur={out.get('user-renamed-old-name')}",
        )
        check(
            f"X5.{tag}.user-filter-denied-no-clear",
            out.get("user-custom-filter-denied", {}).get("history_after") == 2,
            f"uf={out.get('user-custom-filter-denied')}",
        )
        # X5：继承保护下原生 new 不联动、不误提示
        ip = out.get("persona-inherit-protected-new", {})
        check(
            f"X5.{tag}.inherit-protected-new-no-sync",
            ip.get("history_after") == 2
            and ip.get("plugin_notified") is False,
            f"ip={ip}",
        )
        # N10/Y3：范围外/权限拒绝（persona 方向）
        oos = out.get("out-of-scope-reset", {})
        check(
            f"N10.{tag}.out-of-scope-no-sync",
            oos.get("native_update_calls") == 1
            and oos.get("history_after") == 2
            and oos.get("epoch_bumped") is False
            and oos.get("plugin_notified") is False,
            f"oos={oos}",
        )
        prm = out.get("permission-denied-reset", {})
        check(
            f"N10.{tag}.permission-denied-no-clear",
            prm.get("native_update_calls") == 0
            and prm.get("history_after") == 2
            and prm.get("plugin_notified") is False,
            f"prm={prm}",
        )
        # N10/Y3：user 模式补齐原定分支
        unpr = out.get("user-no-provider-reset", {})
        check(
            f"N10.{tag}.user-no-provider-reset-denied",
            unpr.get("history_after") == 2
            and unpr.get("plugin_notified") is False,
            f"unpr={unpr}",
        )
        ubd = out.get("user-builtins-disabled-reset", {})
        check(
            f"N10.{tag}.user-disabled-no-clear",
            ubd.get("native_update_calls") == 0
            and ubd.get("history_after") == 2,
            f"ubd={ubd}",
        )
        ubdn = out.get("user-builtins-disabled-new", {})
        check(
            f"N10.{tag}.user-disabled-new-no-clear",
            ubdn.get("native_new_calls") == 0
            and ubdn.get("history_after") == 2,
            f"ubdn={ubdn}",
        )
        urnn = out.get("user-renamed-new-name", {})
        check(
            f"N10.{tag}.user-renamed-new-name-syncs",
            urnn.get("native_update_calls") == 1
            and urnn.get("history_after") == 0
            and urnn.get("epoch_bumped") is True,
            f"urnn={urnn}",
        )
        uoos = out.get("user-out-of-scope-reset", {})
        check(
            f"N10.{tag}.user-out-of-scope-no-sync",
            uoos.get("native_update_calls") == 1
            and uoos.get("history_after") == 2
            and uoos.get("epoch_bumped") is False
            and uoos.get("plugin_notified") is False,
            f"uoos={uoos}",
        )
        uprm = out.get("user-permission-denied", {})
        check(
            f"N10.{tag}.user-permission-denied-no-clear",
            uprm.get("native_update_calls") == 0
            and uprm.get("history_after") == 2,
            f"uprm={uprm}",
        )
        # N10/Y3：user 模式 once-only follower 屏障（一次成功只 bump/
        # 提示一次；命令挂起期间同账号另一人格新问答不被二次装饰清掉）
        for _ulabel, _uscen in (
            ("reset", out.get("user-reset-two-handlers", {})),
            ("new", out.get("user-new-two-handlers", {})),
        ):
            _ufo = _uscen.get("follow_observations", {})
            check(
                f"N10.{tag}.user-{_ulabel}-once-only",
                _uscen.get("epoch_delta") == 1
                and _uscen.get("notify_count") == 1
                and _ufo.get("history_before_notice") == 2
                and _uscen.get("history_after") == 2
                and (_uscen.get("native_update_calls", 0)
                     + _uscen.get("native_new_calls", 0)) == 1,
                f"epoch={_uscen.get('epoch_delta')} "
                f"notify={_uscen.get('notify_count')} "
                f"hist={_ufo.get('history_before_notice')}"
                f"/{_uscen.get('history_after')}",
            )
            check(
                f"N10.{tag}.user-{_ulabel}-new-turn-survives",
                _ufo.get("command_waiting_during_new_turn") is True
                and _ufo.get("new_turn_model_calls") == 1
                and _ufo.get("observer_key_scope") == "u:",
                f"fo={ {k: v for k, v in _ufo.items() if k != 'new_turn_result'} }",
            )
            # Z3b：命令与 follower 为确实不同的人格（实际解析链证据），
            # 同平台/机器人/发送者，新轮写入同一 u: 共享键
            _cmd_persona = _uscen.get("command_resolved_persona")
            _cmd_base = _uscen.get("command_base") or []
            _new_persona = _ufo.get("new_turn_source_persona")
            _new_key = _ufo.get("new_turn_identity_key") or ""
            _key_parts = _new_key.split(chr(31))
            check(
                f"N10.{tag}.user-{_ulabel}-distinct-personas-same-ukey",
                _cmd_persona == "maid"
                and _new_persona == "second"
                and _cmd_persona != _new_persona
                and len(_key_parts) == 4
                and _key_parts[2] == "u:"
                and [_key_parts[0], _key_parts[1], _key_parts[3]]
                == list(_cmd_base),
                f"cmd_persona={_cmd_persona!r} new_persona={_new_persona!r} "
                f"key={_new_key!r} base={_cmd_base}",
            )
        check(
            f"T5.{tag}.no-provider-no-clear",
            out.get("no-provider", {}).get("history_after") == 2
            and out.get("no-provider", {}).get("plugin_notified") is False,
            f"np={out.get('no-provider')}",
        )
        # T6：禁用/改名/过滤矩阵
        bd = out.get("builtins-disabled-reset", {})
        check(
            f"T6.{tag}.disabled-no-clear",
            bd.get("native_update_calls") == 0 and bd.get("history_after") == 2,
            f"bd={bd}",
        )
        bdn = out.get("builtins-disabled-new", {})
        check(
            f"T6.{tag}.disabled-new-no-clear",
            bdn.get("native_new_calls") == 0 and bdn.get("history_after") == 2,
            f"bdn={bdn}",
        )
        rn_old = out.get("builtin-reset-renamed-old-name", {})
        check(
            f"T6.{tag}.renamed-old-name-no-clear",
            rn_old.get("native_update_calls") == 0
            and rn_old.get("history_after") == 2,
            f"rn_old={rn_old}",
        )
        rn_new = out.get("builtin-reset-renamed-new-name", {})
        check(
            f"T6.{tag}.renamed-new-name-syncs",
            rn_new.get("native_update_calls") == 1
            and rn_new.get("history_after") == 0
            and rn_new.get("epoch_bumped") is True,
            f"rn_new={rn_new}",
        )
        fd = out.get("builtin-reset-custom-filter-denied", {})
        check(
            f"T6.{tag}.filter-denied-no-clear",
            fd.get("native_update_calls") == 0 and fd.get("history_after") == 2,
            f"fd={fd}",
        )
        # U2：new 按自身成功语义联动（无 provider 也成功）
        nwp = out.get("new-with-provider", {})
        nwo = out.get("new-without-provider", {})
        check(
            f"U2.{tag}.new-with-provider-syncs",
            nwp.get("native_new_calls") == 1
            and nwp.get("history_after") == 0
            and nwp.get("epoch_bumped") is True,
            f"nwp={nwp}",
        )
        check(
            f"U2.{tag}.new-without-provider-syncs",
            nwo.get("native_new_calls") == 1
            and nwo.get("history_after") == 0
            and nwo.get("epoch_bumped") is True
            and nwo.get("plugin_notified") is True,
            f"nwo={nwo}",
        )
        rec = out.get("recovery_no_backfill_after_new", {})
        check(
            f"U2.{tag}.recovery-no-backfill",
            rec.get("old_leak") is False and rec.get("new_turn_present") is True,
            f"rec={rec}",
        )
        # V1：一次成功只应用一次（同事件多处理器多次装饰）
        r2h = out.get("reset-two-handlers", {})
        n2h = out.get("new-two-handlers", {})
        for label, scenario in (("reset", r2h), ("new", n2h)):
            fo = scenario.get("follow_observations", {})
            check(
                f"V1.{tag}.{label}-once-only",
                scenario.get("epoch_delta") == 1
                and scenario.get("notify_count") == 1
                and fo.get("history_before_notice") == 2
                and scenario.get("history_after") == 2
                and (scenario.get("native_update_calls", 0)
                     + scenario.get("native_new_calls", 0)) == 1,
                f"epoch={scenario.get('epoch_delta')} notify={scenario.get('notify_count')} "
                f"hist_notice={fo.get('history_before_notice')} after={scenario.get('history_after')}",
            )
            check(
                f"V1.{tag}.{label}-new-turn-survives",
                fo.get("command_waiting_during_new_turn") is True
                and fo.get("new_turn_model_calls") == 1,
                f"fo={ {k: v for k, v in fo.items() if k != 'new_turn_result'} }",
            )
        # U3：结构化成功标记（_clean_group_context_session）替代文本前缀
        pfx = out.get("reset-prefix-decorator", {})
        check(
            f"U3.{tag}.prefix-decorator-still-syncs",
            pfx.get("native_update_calls") == 1
            and pfx.get("history_after") == 0
            and pfx.get("epoch_bumped") is True
            and pfx.get("plugin_notified") is True,
            f"pfx={pfx}",
        )
        dr2 = out.get("default-reset", {})
        check(
            f"U3.{tag}.clean-mark-structure",
            dr2.get("clean_marked") is True
            and out.get("no-provider", {}).get("clean_marked") is None,
            "成功路径设置结构化标记，拒绝路径不设",
        )


async def main() -> int:
    await u1_cancellation_windows()
    await t2_cancel_window()
    await t4_buffer_suppression()
    t1_t5_t6_workers()
    worker_entry_fault_injection()
    n04_tools_fault_injection()
    n04_diag_negative_injection()
    print(f"\n=== T1~T6 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
