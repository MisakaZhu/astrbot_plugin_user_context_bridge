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
            if "__error__" in parsed:
                parsed["__error__"] += "（失败留证：" + (
                    worker_result.persist_failure(script, r, parsed) + "）")
            results[tag] = parsed
    return results


def worker_entry_fault_injection() -> None:
    """Z1a/N23：故障注入经**正式 t1_t5_t6_workers** 及其实际 subprocess
    处理路径（patch tests.worker_result.safe_run——入口真实使用的执行
    函数）；正常对照必须过，rc19/缺 RESULT/坏 JSON/JSON 非对象/必要
    字段缺失逐个必须判 FAIL。对照 stdout 取自本函数先前对同一 worker
    的真实运行结果（重新序列化 @@RESULT@@ 行），不复制校验逻辑。"""

    from unittest.mock import patch

    from tests import worker_result as wr

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
        with patch.object(wr, "safe_run", new=patched_call(table)):
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


def n04_diag_negative_injection() -> None:
    """AA2：受控无调用（真实请求钩子停止）与真实异常（provider 抛错）
    两种负例经正式 t1_t5_t6_workers 必须 FAIL；worker 输出含每窗机器
    可读诊断（不虚 PASS、不二次 KeyError），完整输出留存证据目录。"""

    real_run_worker = _run_worker
    evidence_dir = Path(__file__).resolve().parent.parent / (
        "local_evidence") / "aa_logs"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def observer_run_worker(script: str, extra_args=None):
        if script != "t1_persona_worker.py":
            return real_run_worker(script, extra_args)
        raise AssertionError("mode must be bound via make_runner")

    def make_runner(mode: str):
        collected: dict = {}

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
                    r = subprocess.run(
                        [venv + r"\Scripts\python.exe", "-X", "utf8",
                         observer, td, mode],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, cwd=str(repo),
                        timeout=300,
                    )
                    line = next(
                        (l for l in r.stdout.splitlines()
                         if l.startswith("@@RESULT@@")), None)
                    parsed = (json.loads(line[len("@@RESULT@@"):])
                              if line else None)
                    # AA2.4：完整 worker 输出持久留存（含诊断字段值）
                    (evidence_dir / (
                        f"aa2_{mode}_{tag}.log")).write_text(
                        r.stdout + "\n===STDERR===\n" + r.stderr,
                        encoding="utf-8", errors="replace")
                    if parsed is not None:
                        collected[tag] = parsed
                        (evidence_dir / (
                            f"aa2_{mode}_{tag}.json")).write_text(
                            json.dumps(parsed, ensure_ascii=False,
                                       indent=2, default=str),
                            encoding="utf-8")
                    results[tag] = parsed if parsed is not None else {
                        "__error__": f"observer rc={r.returncode}: "
                        + (r.stderr or r.stdout)[-300:]
                    }
            return results
        return runner, collected

    for mode in ("stop", "provider-raise"):
        before_pass, before_fail = len(PASS), len(FAIL)
        runner, outs = make_runner(mode)
        with patch.object(sys.modules[__name__], "_run_worker", runner):
            t1_t5_t6_workers()
        detected = len(FAIL) > before_fail
        # 诊断字段完整性：负例 worker 必须输出窗口诊断而非二次崩溃
        diag_ok = bool(outs) and all(
            isinstance(o, dict) and "n04_window_diagnostics" in o
            and "n04_model_calls" in o
            for o in outs.values()
        )
        del PASS[before_pass:]
        del FAIL[before_fail:]
        check(
            f"AA2.{mode}-negative-detected",
            detected,
            f"expect_fail=True detected={detected}",
        )
        check(
            f"AA2.{mode}-diagnostics-preserved",
            diag_ok,
            f"diag_ok={diag_ok} tags={sorted(outs)}",
        )


def t1_t5_t6_workers() -> None:
    # T1：真实 PersonaManager/ConversationManager 新会话 + 不同默认人格
    res = _run_worker("t1_persona_worker.py")
    for tag, out in res.items():
        if "__error__" in out:
            check(f"T1.{tag}.worker", False, out["__error__"])
            continue
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
        # N04（Y3）：user 模式真实链——宿主装配 → 注册请求钩子 →
        # Runner/假模型实际调用 → 真实终态钩子 → 下一窗口请求
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
        # Z3a：终模型实参中的工具集合——真实 ToolSet 类型、当前人格专属
        # 工具、无跨人格串入、schema 可序列化（负例见 n04 工具丢弃注入）
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
