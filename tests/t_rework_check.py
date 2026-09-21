"""第三轮返工回归（T1~T6）：把三次验收独立反例转为仓库内自动断言。

T2/T4 进程内（真实调度链 harness）；T1/T5/T6 经真实宿主组件子进程 worker
（真实 PersonaManager/ConversationManager/Context/WakingCheckStage/
StarRequestSubStage/builtin commands，插件经真实 import 装配）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/t_rework_check.py
"""

from __future__ import annotations

import asyncio
import hashlib
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

# AE1：正式负例流程唯一 run ID 序号（跨调用不重复；跨进程由时间戳区分）
_AA2_RUN_SEQ = __import__("itertools").count(1)

# AF2.A：采集绑定注册表——一次真实捕获（observer/worker 调用）登记
# 唯一 capture_id，按 key 归档；正式判定消费调用侧注册的采集身份，
# 不从文件名或被检文件推断 worker 归属。
_AA2_CAPTURE_SEQ = __import__("itertools").count(1)
_AA2_CAPTURE_REGISTRY: dict = {}


def _register_capture(key: str) -> str:
    capture_id = "cap-{}-{:04d}".format(
        key, next(_AA2_CAPTURE_SEQ))
    _AA2_CAPTURE_REGISTRY.setdefault(key, []).append(capture_id)
    return capture_id


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
            # AF1/AF2：正式 writer——raw 摘要与采集绑定由本次
            # CompletedProcess 产生（正常 worker 形态 mode=normal）
            persist_observation(
                Path(script).stem, tag, r, parsed,
                mode="normal",
                capture_id=_register_capture(
                    "worker-" + Path(script).stem),
                cmd=[
                    venv + r"\Scripts\python.exe", "-X", "utf8",
                    str(Path(__file__).resolve().parent / script),
                    "<td>",
                ] + (extra_args or []),
            )
            if "__error__" in parsed:
                parsed["__error__"] += "（留证：" + parsed[
                    "_run_binding"]["json"] + "）"
            results[tag] = parsed
    return results


def persist_observation(where, tag, completed, parsed, *, mode="",
                        capture_id="", cmd=None, injected=False):
    """AF1/AF2：正式观测留证 writer——raw 摘要（stdout/stderr sha、
    原始 AUDIT 行、traceback 段数）与采集绑定（tag/mode/capture_id/
    启动命令/injected 标记）全部来自**本次 CompletedProcess**，随
    persist_run 落盘并写入 `_run_binding`；预期由写侧登记，读回时
    不从被检文件反推。合成观测（injected=True）经同一 writer 生成
    与自身一致的新证据，不复用真实捕获文件。"""

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    audit_line = next(
        (l for l in stdout.splitlines()
         if l.startswith("@@AUDIT@@")), None)
    tb = (stdout.count("Traceback (most recent call last)")
          + stderr.count("Traceback (most recent call last)"))
    ev = worker_result_module.persist_run(where, tag, completed, parsed)
    parsed["_evidence_log"] = ev["log"]
    parsed["_evidence_json"] = ev["json"]
    parsed["_run_binding"] = {
        "json": ev["json"], "log": ev["log"],
        "rc": getattr(completed, "returncode", None),
        "where": where, "tag": tag,
        "mode": mode, "capture_id": capture_id,
        "cmd": list(cmd or []),
        "injected": bool(injected),
        "stdout_sha256": hashlib.sha256(
            stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            stderr.encode("utf-8")).hexdigest(),
        "audit_raw_line": audit_line,
        "traceback_count": tb,
    }
    return parsed


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


def _split_log_sections(raw: str) -> tuple:
    """按 persist_run 写侧格式拆出 (rc 行, stdout 段, stderr 段)。"""
    lines = raw.split(chr(10))
    rc_line = lines[0] if lines else ""
    try:
        i = lines.index("===STDOUT===")
        j = lines.index("===STDERR===")
        stdout_sec = chr(10).join(lines[i + 1:j])
        # persist 写侧在 stderr 后补一个文件尾换行，重建时去掉
        stderr_sec = chr(10).join(lines[j + 1:])
        if stderr_sec.endswith(chr(10)):
            stderr_sec = stderr_sec[:-1]
    except ValueError:
        return rc_line, None, None
    return rc_line, stdout_sec, stderr_sec


def verify_run_evidence(out, mode: str, snap, *, expect_tag=None,
                        expect_capture_key=None) -> tuple:
    """AF1/AF2：正式证据读回校验（正常流程与负例共用）。

    实际重开 `_run_binding` 绑定的 JSON/log，逐项核对与**本次调用侧**
    一致：
    1. 绑定完整性 + 版本/采集绑定消费——binding.tag==expect_tag、
       binding.mode==本次 mode、capture_id 已在调用侧注册表对应 key
       下登记（不从文件名或被检文件推断归属）；
    2. 引用==写侧绑定（关联校验）、文件存在、rc 行==本次 rc；
    3. log RESULT 行与保存 JSON 自洽；
    4. 保存 JSON 与本次完整核心 payload 快照（含目标布尔值）一致；
    5. 原始 @@AUDIT@@ 行存在、与写侧捕获行一致、可解析且 mode 绑定
       本次 mode（正常 T1 无 observer AUDIT 属正常形态）；
    6. 原始 traceback 段数与本次捕获一致；provider-raise 本次真实
       注入要求 >=3 段完整异常链（摘要不替代栈）；
    7. stdout/stderr 段 sha256 与本次 CompletedProcess 一致（兜底）。
    所有失败信息以「证据」开头并可指认维度。
    """

    if not isinstance(out, dict):
        return False, "证据核对失败：结果不是对象"
    binding = out.get("_run_binding")
    if not isinstance(binding, dict):
        return False, "证据核对失败：缺少本次运行绑定（_run_binding）"
    for k in ("json", "log", "rc", "where", "tag", "mode",
              "capture_id", "injected", "stdout_sha256",
              "stderr_sha256", "audit_raw_line", "traceback_count"):
        if k not in binding:
            return False, f"证据核对失败：绑定缺字段 {k}"
    if expect_tag is not None and binding["tag"] != expect_tag:
        return False, ("证据核对失败：写侧 tag 与本次预期版本不符 "
                       f"（期望 {expect_tag}，写侧 {binding['tag']!r}）")
    if binding["mode"] != mode:
        return False, ("证据核对失败：写侧 mode 与本次场景不符 "
                       f"（期望 {mode}，写侧 {binding['mode']!r}）")
    if expect_capture_key is not None:
        registered = _AA2_CAPTURE_REGISTRY.get(expect_capture_key, [])
        if binding["capture_id"] not in registered:
            return False, ("证据核对失败：capture_id 未在调用侧注册表 "
                           f"{expect_capture_key} 下登记（写侧 "
                           f"{binding['capture_id']!r}）")
    ref_json = out.get("_evidence_json")
    ref_log = out.get("_evidence_log")
    if not ref_json or not ref_log:
        return False, "证据核对失败：缺少 _evidence_json/_evidence_log 引用"
    exp_json = binding["json"]
    exp_log = binding["log"]
    if ref_json != exp_json or ref_log != exp_log:
        return False, (
            "证据核对失败：引用与本次运行绑定不符（关联校验）："
            f"期望json={exp_json} 期望log={exp_log} "
            f"实际json={ref_json} 实际log={ref_log}")
    pj, pl = Path(ref_json), Path(ref_log)
    if not pj.is_file():
        return False, f"证据核对失败：JSON 不存在：{ref_json}"
    if not pl.is_file():
        return False, f"证据核对失败：log 不存在：{ref_log}"
    try:
        reopened = json.loads(pj.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"证据核对失败：JSON 解析失败：{exc}"
    if not isinstance(reopened, dict) or not reopened:
        return False, "证据核对失败：JSON 为空/非对象"
    raw = pl.read_text(encoding="utf-8", errors="replace")
    rc_line, stdout_sec, stderr_sec = _split_log_sections(raw)
    if stdout_sec is None:
        return False, "证据核对失败：log 缺 ===STDOUT===/===STDERR=== 段"
    if rc_line != f"returncode={binding['rc']}":
        return False, (f"证据核对失败：log rc 行不符：{rc_line!r} "
                       f"vs 本次写侧 rc={binding['rc']!r}")
    result_line = next(
        (l for l in stdout_sec.split(chr(10))
         if l.startswith("@@RESULT@@")), None)
    if result_line is None:
        return False, "证据核对失败：log 缺 @@RESULT@@ 原文"
    try:
        from_log = json.loads(result_line[len("@@RESULT@@"):])
    except Exception as exc:
        return False, f"证据核对失败：log RESULT 行解析失败：{exc}"
    # 保存 JSON = RESULT 原文 + 观察器后补的 _aa2_audit；二者必须一致
    reopened_core = {k: v for k, v in reopened.items()
                     if k != "_aa2_audit"}
    if from_log != reopened_core:
        return False, "证据核对失败：log RESULT 行与保存 JSON 内容不一致"
    if not isinstance(snap, dict):
        return False, "证据核对失败：缺少本次调用侧内容快照"
    # AF1：完整核心 payload（含目标布尔值）与本次输入一致——
    # 快照去除路径/运行元信息后整体比较，磁盘与内存矛盾即拒绝
    if reopened != snap:
        diff_keys = [k for k in set(reopened) | set(snap)
                     if reopened.get(k) != snap.get(k)]
        return False, (
            "证据核对失败：保存内容与本次完整输入不一致（完整 payload）"
            f" diff_keys={sorted(diff_keys)[:8]}")
    # AF1：原始 @@AUDIT@@ 行对账——存在性、与写侧捕获行一致、可解析、
    # mode 绑定；正常 T1（mode=normal）无 observer AUDIT 属正常形态
    raw_audit_lines = [l for l in stdout_sec.split(chr(10))
                       if l.startswith("@@AUDIT@@")]
    if binding["audit_raw_line"] is not None:
        if raw_audit_lines != [binding["audit_raw_line"]]:
            return False, (
                "证据核对失败：原始 AUDIT 行缺失或与本次捕获不一致 "
                f"（期望 {binding['audit_raw_line'][:80]}，"
                f"实际 {[l[:80] for l in raw_audit_lines]}）")
        try:
            audit_parsed = json.loads(
                binding["audit_raw_line"][len("@@AUDIT@@"):])
        except Exception as exc:
            return False, f"证据核对失败：原始 AUDIT 行解析失败：{exc}"
        if not isinstance(audit_parsed, dict) or (
                mode != "normal"
                and audit_parsed.get("mode") != mode):
            return False, ("证据核对失败：原始 AUDIT mode 与本次场景不符 "
                           f"（audit={audit_parsed!r}，期望 mode={mode}）")
    else:
        if raw_audit_lines:
            return False, ("证据核对失败：本次捕获无 AUDIT 行但 log 出现 "
                           f"{len(raw_audit_lines)} 行 AUDIT")
    # AF1：原始 traceback 段数与本次捕获一致；provider 真实注入链 >=3
    file_tb = (stdout_sec.count("Traceback (most recent call last)")
               + stderr_sec.count("Traceback (most recent call last)"))
    if file_tb != binding["traceback_count"]:
        return False, ("证据核对失败：原始 traceback 段数与本次捕获不符 "
                       f"（本次 {binding['traceback_count']}，文件 "
                       f"{file_tb}）")
    if mode == "provider-raise" and binding["traceback_count"] < 3:
        return False, ("证据核对失败：provider-raise 本次真实注入链不足 "
                       f"3 段（本次捕获 {binding['traceback_count']} 段）")
    # 兜底：raw 段 sha 与本次 CompletedProcess 一致
    if (hashlib.sha256(stdout_sec.encode("utf-8")).hexdigest()
            != binding["stdout_sha256"]
            or hashlib.sha256(stderr_sec.encode("utf-8")).hexdigest()
            != binding["stderr_sha256"]):
        return False, ("证据核对失败：raw 日志段 sha 与本次捕获不一致"
                       "（stdout/stderr 被改动）")
    return True, "ok"


def run_aa2_negative(mode: str, runner, *, scenario: str = "",
                     expect_capture_key: str | None = None,
                     manifest_mutator=None) -> dict:
    """AD1/AD2/AE1/AF1/AF2：正式 AA2 负例判定——逐版本独立验证 +
    指定目标 + 证据完整对账 + 版本/采集绑定消费。

    每个版本必须独立满足四项（不能靠另一版本补足）：
    1. 入口有效（无 __error__）；
    2. 指定目标命中：stop→all-model-called、provider-raise→
       all-completed-zero-watchdog-zero-pending（不能由任意 N04.* 替代）；
    3. 诊断有意义（_validate_n04_negative_diag）；
    4. 证据读回验证通过（verify_run_evidence：写侧 tag/mode/
       capture_id 与调用侧预期一致、引用与本次写侧绑定一致、
       raw AUDIT 行/rc/traceback/sha/完整核心 payload 全部对账）。
    两个版本都合格才可整体通过。

    AE1：每次调用生成唯一 run ID 与专属输出目录 local_evidence/
    aa2_runs/<run_id>/，写入唯一命名 manifest（run/scenario/tag/mode、
    JSON/log 双引用、run_binding 与核心 payload sha），不覆盖上一
    调用，也无目录计数/mtime 兜底。manifest_mutator 仅用于正式
    manifest 写入边界的注入演示。
    """

    from datetime import datetime as _dt

    # AE1：唯一 run ID / 输出目录（进程内序号 + 时间戳，跨进程不重名）
    run_id = "aa2run-{}-{:04d}".format(
        _dt.now().strftime("%H%M%S%f"), next(_AA2_RUN_SEQ))
    run_dir = (Path(__file__).resolve().parent.parent
               / "local_evidence" / "aa2_runs" / run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    scen = scenario or mode

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

    # AF1：预期绑定来自**当前调用侧**——对本流程捕获的结果做**完整
    # 核心 payload 快照**（去除路径/运行元信息，含全部目标布尔值），
    # 证据文件必须与该快照整体一致；被替换文件的自报内容不构成预期。
    snapshots = {
        tag: {k: v for k, v in out.items()
              if k not in ("_evidence_json", "_evidence_log",
                           "_run_binding")}
        for tag, out in outs.items() if isinstance(out, dict)
    }

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
            # AD2/AE1/AF1/AF2：证据完整对账（正式共用函数，消费调用侧
            # 版本/采集绑定预期）
            evidence_ok_v, evidence_note_v = verify_run_evidence(
                out, mode, snapshots.get(tag),
                expect_tag=tag,
                expect_capture_key=expect_capture_key)
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
            "evidence_log": out.get("_evidence_log", "") if out else "",
            "run_binding": out.get("_run_binding") if out else None,
            # AF2.B：核心输入快照及其 sha（verdict 与 manifest 双处绑定）
            "payload": snapshots.get(tag),
            "payload_sha256": hashlib.sha256(json.dumps(
                snapshots.get(tag), ensure_ascii=False, sort_keys=True,
                default=str).encode("utf-8")).hexdigest()
            if snapshots.get(tag) is not None else "",
            "ok": v_ok,
        }
        if not evidence_ok_v:
            evidence_ok = False
            evidence_notes[tag] = evidence_note_v
        if not v_ok:
            per_version_ok = False

    diag_notes = {t: per_version[t]["diag_note"] for t in per_version}

    verdict = {
        "run_id": run_id,
        "scenario": scen,
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

    # AE1/AF2.B：唯一命名 manifest——run/scenario/tag/mode、JSON/log
    # 双引用、run_binding 与核心 payload sha；保存错误也留下受控失败
    # 信息（evidence_note/入口 note），不覆盖。manifest_mutator 仅供
    # 正式写入边界注入演示使用。
    manifest = {
        "run_id": run_id,
        "scenario": scen,
        "mode": mode,
        "pass": verdict["pass"],
        "versions": {
            tag: {
                "run_id": run_id,
                "scenario": scen,
                "tag": tag,
                "mode": mode,
                "entry_ok": per_version[tag]["entry_ok"],
                "diag_ok": per_version[tag]["diag_ok"],
                "evidence_ok": per_version[tag]["evidence_ok"],
                "evidence_json": per_version[tag]["evidence_json"],
                "evidence_log": per_version[tag]["evidence_log"],
                "run_binding": per_version[tag]["run_binding"],
                "payload_sha256": per_version[tag]["payload_sha256"],
            }
            for tag in per_version
        },
    }
    if manifest_mutator is not None:
        manifest = manifest_mutator(manifest)
    manifest_path = run_dir / f"{run_id}_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    verdict["run_dir"] = str(run_dir)
    verdict["manifest_path"] = str(manifest_path)

    return verdict


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
    # AE1：已保存 verdict 清单（唯一命名），供收尾沿 manifest 重开验证
    saved_entries: list = []

    def save_verdict(name: str, verdict: dict, *,
                     valid_evidence: bool = False,
                     capture_key: str | None = None,
                     register: bool = True) -> str:
        run_id = verdict.get("run_id") or "norun"
        path = evidence_dir / f"ac2_{name}-{run_id}.json"
        path.write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2,
                       default=str),
            encoding="utf-8")
        entry = {
            "scenario": name,
            "mode": verdict.get("mode"),
            "run_id": run_id,
            "verdict_path": str(path),
            "manifest_path": verdict.get("manifest_path", ""),
            "capture_key": capture_key,
            "versions": {
                tag: {
                    "scenario": name,
                    "tag": tag,
                    "mode": verdict.get("mode"),
                    "run_id": run_id,
                    "evidence_json": pv.get("evidence_json", ""),
                    "evidence_log": pv.get("evidence_log", ""),
                    "payload_sha256": pv.get("payload_sha256", ""),
                }
                for tag, pv in verdict.get("per_version", {}).items()
            },
            "valid_evidence": valid_evidence,
        }
        if register:
            saved_entries.append(entry)
        return str(path)

    def observer_runner(mode: str):
        """AC3：每次观察器运行都 persist_run 落盘（raw stdout/stderr/rc、
        完整解析 JSON、AUDIT）；AF2.A 一次调用登记唯一 capture_id，
        每版结果经正式 writer 附 raw 摘要与采集绑定。"""
        def runner(script: str, extra_args=None):
            if script != "t1_persona_worker.py":
                return real_run_worker(script, extra_args)
            results = {}
            repo = Path(__file__).resolve().parent.parent
            observer = str(Path(__file__).resolve().parent / (
                "aa2_n04_diag_observer.py"))
            capture_id = _register_capture(f"observer-{mode}")
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
                    cmd = [venv + r"\Scripts\python.exe", "-X", "utf8",
                           observer, td, mode]
                    completed = worker_result_module.safe_run(
                        cmd, env=env, cwd=str(repo), timeout=300)
                    parsed = worker_result_module.read_worker_result(
                        completed)
                    audit = next(
                        (l for l in (completed.stdout or "").splitlines()
                         if l.startswith("@@AUDIT@@")), None)
                    if "__error__" not in parsed and audit:
                        parsed["_aa2_audit"] = json.loads(
                            audit[len("@@AUDIT@@"):])
                    # AC3：每次观察器运行都 persist（临时目录清理前）；
                    # AF1/AF2：正式 writer 登记 raw 摘要与采集绑定
                    persist_observation(
                        f"aa2_observer_{mode}", tag, completed, parsed,
                        mode=mode, capture_id=capture_id, cmd=cmd)
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
        verdict = run_aa2_negative(
            mode, observer_runner(mode), scenario=f"{mode}-good",
            expect_capture_key=f"observer-{mode}")
        vp = save_verdict(f"{mode}-good", verdict, valid_evidence=True,
                          capture_key=f"observer-{mode}")
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
        run_aa2_negative("stop", capture_runner("stop"),
                         scenario="capture-stop",
                         expect_capture_key="observer-stop")
    # AE2.A：保留完整引用与写侧绑定（不再摘除）——错引用注入需要从
    # 本次捕获映射显式取真实路径
    good_neg_428 = dict(neg_capture.get(".venv", {}))
    good_neg_426 = dict(neg_capture.get(".venv426", {}))

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
                persist_observation(
                    "ac2-rc19", tag, fr, parsed, mode="stop",
                    capture_id=_register_capture("synth-ac2-rc19"),
                    cmd=["synthetic", "ac2-rc19", tag], injected=True)
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
                persist_observation(
                    "ac2-empty-diag", tag, fr, parsed, mode="stop",
                    capture_id=_register_capture("synth-ac2-empty-diag"),
                    cmd=["synthetic", "ac2-empty-diag", tag],
                    injected=True)
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
        verdict = run_aa2_negative("stop", runner, scenario=name,
                                   expect_capture_key="observer-stop")
        vp = save_verdict(name, verdict, capture_key="observer-stop")
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
        verdict = run_aa2_negative("stop", runner, scenario=name,
                                   expect_capture_key="observer-stop")
        vp = save_verdict(name, verdict, capture_key="observer-stop")
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
        run_aa2_negative("provider-raise", prov_capture_runner,
                         scenario="capture-provider-raise",
                         expect_capture_key="observer-provider-raise")
    # AE2.A：同样保留完整引用与写侧绑定
    good_prov_428 = dict(neg_prov_capture.get(".venv", {}))
    good_prov_426 = dict(neg_prov_capture.get(".venv426", {}))

    def schema_only_runner_factory(neg_by_tag: dict, mode: str,
                                   scenario: str):
        """AF1.4：合成观测（正常观测 + 负例诊断/AUDIT + 仅翻假 schema）
        必须经正式 writer 生成**新的、与该观测一致**的 JSON/log/AUDIT
        与绑定（injected=True 标记注入用途），不复用真实负例路径。"""

        def runner(script, extra_args=None):
            if script == "t1_persona_worker.py":
                normal = real_run_worker(script, extra_args)
                results = {}
                capture_id = _register_capture(f"synth-{scenario}")
                for tag in (".venv", ".venv426"):
                    payload = dict(normal.get(tag, {}))
                    for k in ("_evidence_json", "_evidence_log",
                              "_run_binding"):
                        payload.pop(k, None)
                    # 诊断/AUDIT/调用来自负例（维持 stop/raise 模式特征）
                    for k in ("n04_window_diagnostics", "n04_model_calls",
                              "_aa2_audit"):
                        if k in neg_by_tag.get(tag, {}):
                            payload[k] = neg_by_tag[tag][k]
                    # AD1 反例：仅翻假 schema（指定目标全部通过）
                    payload["n04_final_tool_schema_serializable"] = False
                    audit = payload.pop("_aa2_audit", None) or {
                        "mode": mode}
                    # 正式 writer：合成 raw（RESULT+AUDIT+provider 标记
                    # 栈）与绑定自洽，不引用真实捕获文件
                    synth_stdout = (
                        "@@RESULT@@" + json.dumps(payload,
                                                  ensure_ascii=False)
                        + "\n@@AUDIT@@" + json.dumps(audit,
                                                     ensure_ascii=False)
                        + "\n")
                    if mode == "provider-raise":
                        synth_stdout += "".join(
                            "Traceback (most recent call last):\n"
                            "  File \"<aa2-synthetic>\", line %d, in aa2\n"
                            "RuntimeError: aa2 注入模型调用真实异常\n" % i
                            for i in range(3))
                    payload["_aa2_audit"] = audit
                    synth = _FakeCompleted(0, synth_stdout, "")
                    persist_observation(
                        f"aa2synth-{scenario}", tag, synth, payload,
                        mode=mode, capture_id=capture_id,
                        cmd=["synthetic", scenario, tag], injected=True)
                    results[tag] = payload
                return results
            return real_run_worker(script, extra_args)
        return runner

    normal_t1_full = real_run_worker("t1_persona_worker.py")

    # AE1：正常流程同样走正式证据读回函数（无 observer AUDIT 属正常形态）
    for tag in (".venv", ".venv426"):
        nout = normal_t1_full.get(tag, {})
        ok, note = verify_run_evidence(
            nout, "normal",
            {k: v for k, v in nout.items()
             if k not in ("_evidence_json", "_evidence_log",
                          "_run_binding")},
            expect_tag=tag,
            expect_capture_key="worker-t1_persona_worker")
        check(f"AC3.normal-t1.{tag}.evidence-readback", ok, note)

    # AE2.B：场景条目显式携带 mode——provider 场景传 provider-raise，
    # 复用同一 AD1 核心判定
    ad1_bad = (
        ("ad1-stop-schema-only",
         schema_only_runner_factory({".venv": good_neg_428,
                                     ".venv426": good_neg_426}, "stop",
                                    "ad1-stop-schema-only"),
         "stop"),
        ("ad1-provider-schema-only",
         schema_only_runner_factory({".venv": good_prov_428,
                                     ".venv426": good_prov_426},
                                    "provider-raise",
                                    "ad1-provider-schema-only"),
         "provider-raise"),
    )
    for name, runner, sc_mode in ad1_bad:
        verdict = run_aa2_negative(sc_mode, runner, scenario=name,
                                   expect_capture_key=f"synth-{name}")
        vp = save_verdict(name, verdict, capture_key=f"synth-{name}")
        # AD1/AE2.B：rejected 且拒绝维度必须精确——正确 mode、两版入口
        # 有效、指定目标缺失、诊断合格、证据合格
        req = ("all-model-called" if sc_mode == "stop"
               else "all-completed-zero-watchdog-zero-pending")
        pv = verdict["per_version"]
        dims = {t: (
            pv[t]["entry_ok"]
            and pv[t]["specific_target"] == req
            and not pv[t]["specific_target_hit"]
            and pv[t]["diag_ok"]
            and pv[t]["evidence_ok"])
            for t in (".venv", ".venv426")}
        check(f"AD1.{name}-rejected",
              (not verdict["pass"]) and all(dims.values()),
              f"AD1 非指定 N04 失败不应替代指定目标：dims={dims} "
              f"verdict_path={vp}")

    # -- AD2：证据重开验证 + 证据损坏/缺失/错引用反向检验 -----------------
    # 从上面真实负例捕获的 _evidence_json/_evidence_log 引用带入正式
    # run_aa2_negative，验证实际读回内容而非只数文件数。

    def evidence_bad_runner_factory(evidence_corruptor, source=None):
        """构造一个 runner：功能/诊断观测用真实有效负例（默认 stop
        good_neg，可指定 provider 捕获），证据按 corruption 类型注入。"""
        src_by_tag = source or {".venv": good_neg_428,
                                ".venv426": good_neg_426}

        def runner(script, extra_args=None):
            if script == "t1_persona_worker.py":
                results = {}
                for tag in (".venv", ".venv426"):
                    results[tag] = dict(src_by_tag.get(tag, {}))
                    # 绑定深拷贝：腐蚀注入不得泄漏回源捕获
                    results[tag]["_run_binding"] = dict(
                        src_by_tag[tag].get("_run_binding") or {})
                # evidence_corruptor 在 results 上修改证据引用/文件
                evidence_corruptor(results)
                return results
            return real_run_worker(script, extra_args)
        return runner

    def corrupt_empty_evidence(results):
        from datetime import datetime as _dtc
        stamp = _dtc.now().strftime("%H%M%S%f")
        cap_id = _AA2_CAPTURE_REGISTRY.get("observer-stop", [None])[-1]
        for tag in results:
            results[tag]["_evidence_json"] = str(
                evidence_dir / f"ad2_empty_{stamp}_{tag}.json")
            results[tag]["_evidence_log"] = str(
                evidence_dir / f"ad2_empty_{stamp}_{tag}.log")
            Path(results[tag]["_evidence_json"]).write_text("{}", encoding="utf-8")
            Path(results[tag]["_evidence_log"]).write_text("", encoding="utf-8")
            # 绑定同步指向空文件——关联合法，拒绝维度落在空内容本身
            results[tag]["_run_binding"] = {
                "json": results[tag]["_evidence_json"],
                "log": results[tag]["_evidence_log"], "rc": 0,
                "where": f"ad2_empty_{stamp}", "tag": tag,
                "mode": "stop", "capture_id": cap_id, "cmd": [],
                "injected": True,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "audit_raw_line": None, "traceback_count": 0}

    def corrupt_missing_evidence(results):
        # 不写文件、指向不存在路径（AE1：无引用兜底，直接按缺文件拒绝）
        stamp = next(_AA2_RUN_SEQ)
        for tag in results:
            results[tag]["_evidence_json"] = str(
                evidence_dir / f"ad2_nonexistent_{stamp}_{tag}.json")
            results[tag]["_evidence_log"] = str(
                evidence_dir / f"ad2_nonexistent_{stamp}_{tag}.log")

    # 空文件/缺文件对照保留（AE1 要求逐维度记录，不只看总 pass=false）
    ad2_bad = (
        ("ad2-empty-evidence", corrupt_empty_evidence),
        ("ad2-missing-evidence", corrupt_missing_evidence),
    )

    def corrupt_wrong_reference_disabled(results):
        # AE2.A 撤注入对照占位：不做任何事（保留原函数名便于对照阅读）
        return None

    # AE2.A：错引用从**本次捕获映射**显式取另一版/另一模式/正常 T1 的
    # 真实路径实际替换；不用 glob/mtime，找不到路径直接抛错（不允许
    # "找不到就不注入"使自检空转）。
    ref_map: dict = {}

    def collect_ref(mode_name: str, capture: dict):
        for tag in (".venv", ".venv426"):
            d = capture.get(tag, {})
            if d.get("_evidence_json") and d.get("_evidence_log"):
                ref_map[(mode_name, tag)] = {
                    "json": d["_evidence_json"],
                    "log": d["_evidence_log"],
                }

    collect_ref("stop", neg_capture)
    collect_ref("provider-raise", neg_prov_capture)
    collect_ref("normal", real_t1)
    check("AD2.wrongref-refmap-complete",
          all(k in ref_map for k in (
              ("stop", ".venv"), ("stop", ".venv426"),
              ("provider-raise", ".venv"),
              ("provider-raise", ".venv426"),
              ("normal", ".venv"), ("normal", ".venv426"))),
          f"ref_map_keys={sorted(str(k) for k in ref_map)}")

    def swap_factory(replacements: dict, record: dict):
        def corruptor(results):
            for tag, key in replacements.items():
                ref = ref_map.get(key)
                if not ref:
                    raise AssertionError(
                        f"AE2.A 注入缺路径：{key}——不允许找不到就不注入")
                record[tag] = {
                    "replace_key": list(key),
                    "before": {
                        "json": results[tag].get("_evidence_json"),
                        "log": results[tag].get("_evidence_log")},
                    "after": {"json": ref["json"], "log": ref["log"]},
                    "expected_binding": results[tag].get("_run_binding"),
                }
                results[tag]["_evidence_json"] = ref["json"]
                results[tag]["_evidence_log"] = ref["log"]
        return corruptor

    # 无注入对照：同一 runner、同一正式父判定，必须 accepted 且
    # evidence_ok=true（撤掉引用交换时"应检出错引用"的自检必须 FAIL）
    ctrl_verdict = run_aa2_negative(
        "stop", evidence_bad_runner_factory(
            corrupt_wrong_reference_disabled),
        scenario="ad2-wrongref-control-noop",
        expect_capture_key="observer-stop")
    save_verdict("ad2-wrongref-control-noop", ctrl_verdict,
                 valid_evidence=True, capture_key="observer-stop")
    check("AD2.wrongref-control-accepted", ctrl_verdict["pass"],
          f"无注入对照未被接受：notes={ctrl_verdict['evidence_notes']}")

    wrong_ref_cases = (
        ("ad2-wrong-reference-swap-version",
         {".venv": ("stop", ".venv426"),
          ".venv426": ("stop", ".venv")}),
        ("ad2-wrong-reference-cross-mode",
         {".venv": ("provider-raise", ".venv"),
          ".venv426": ("provider-raise", ".venv426")}),
        ("ad2-wrong-reference-normal-t1",
         {".venv": ("normal", ".venv"),
          ".venv426": ("normal", ".venv426")}),
    )
    wrongref_verdicts: dict = {}
    wrongref_records: dict = {}
    for name, repl in wrong_ref_cases:
        rec: dict = {}
        verdict = run_aa2_negative(
            "stop",
            evidence_bad_runner_factory(swap_factory(repl, rec)),
            scenario=name, expect_capture_key="observer-stop")
        vp = save_verdict(name, verdict, capture_key="observer-stop")
        wrongref_verdicts[name] = verdict
        wrongref_records[name] = rec
        # 拒绝维度必须是证据关联（引用与本次运行绑定不符），不能是
        # "原本就没有引用"或 JSON 解析失败
        assoc = {t: (
            not verdict["per_version"][t]["evidence_ok"]
            and "关联" in verdict["per_version"][t]["evidence_note"])
            for t in (".venv", ".venv426")}
        check(f"AD2.{name}-rejected", (not verdict["pass"])
              and all(assoc.values()),
              f"错引用未按关联维度拒绝：assoc={assoc} "
              f"notes={verdict['evidence_notes']} verdict_path={vp}")

    # AE2.A 三方结果：撤注入对照 accepted + 注入后 rejected + 拒绝维度
    # 记录为证据关联；三者缺一即自检未真正检验其声称的故障
    three_way = {
        name: {
            "injected_rejected": not wrongref_verdicts[name]["pass"],
            "evidence_assoc_notes": {
                t: wrongref_verdicts[name]["per_version"][t][
                    "evidence_note"]
                for t in (".venv", ".venv426")},
            "replacements": wrongref_records[name],
            "control_noop_accepted": ctrl_verdict["pass"],
        }
        for name in wrongref_verdicts
    }
    tw_path = evidence_dir / "ae2a_wrong_reference_three_way.json"
    tw_path.write_text(
        json.dumps(three_way, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    check("AD2.wrong-reference-counter-three-way",
          ctrl_verdict["pass"]
          and all(not wrongref_verdicts[n]["pass"]
                  for n in wrongref_verdicts),
          f"三方结果不齐：control={ctrl_verdict['pass']} path={tw_path}")

    for name, corruptor in ad2_bad:
        verdict = run_aa2_negative(
            "stop", evidence_bad_runner_factory(corruptor),
            scenario=name, expect_capture_key="observer-stop")
        vp = save_verdict(name, verdict, capture_key="observer-stop")
        # AD2：证据无效→ rejected（功能判定可能仍检出，但证据不合格）
        check(f"AD2.{name}-rejected", not verdict["pass"],
              f"AD2 证据无效不应算成功负例：verdict_path={vp}")

    # -- AE1：仅改保存证据内容（引用/绑定自洽），本次输入不变 ------------
    # 写侧引用合法但读回内容与本次调用侧快照不符；功能目标/诊断仍合格，
    # 拒绝必须来自 evidence 维度而非功能维度。
    def rewrite_evidence_copy(results, mutate, where: str):
        """AE1/AF1：合成"仅改保存内容"证据——按段重建 raw（仅替换
        RESULT 行，保留 AUDIT 行/原始栈/框架日志），绑定按重建后的
        raw 完整重算并标记 injected；引用自洽，拒绝维度落在内容。"""
        from datetime import datetime as _dts
        stamp = _dts.now().strftime("%H%M%S%f")
        cap_key = ("observer-provider-raise"
                   if "provider" in where else "observer-stop")
        cap_id = _AA2_CAPTURE_REGISTRY.get(cap_key, [None])[-1]
        for tag in results:
            jp = Path(results[tag]["_evidence_json"])
            lp = Path(results[tag]["_evidence_log"])
            data = json.loads(jp.read_text(encoding="utf-8"))
            rc_line, out_sec, err_sec = _split_log_sections(
                lp.read_text(encoding="utf-8", errors="replace"))
            mutate(data)
            new_out = "\n".join(
                ("@@RESULT@@" + json.dumps(
                    {k: v for k, v in data.items() if k != "_aa2_audit"},
                    ensure_ascii=False))
                if l.startswith("@@RESULT@@") else l
                for l in out_sec.split("\n"))
            new_log_content = (
                f"returncode={results[tag]['_run_binding']['rc']}"
                "\n===STDOUT===\n" + new_out + "\n===STDERR===\n"
                + err_sec + "\n")
            new_json = evidence_dir / f"{where}_{stamp}_{tag}.json"
            new_log = evidence_dir / f"{where}_{stamp}_{tag}.log"
            new_json.write_text(
                json.dumps(data, ensure_ascii=False, indent=2,
                           default=str),
                encoding="utf-8")
            new_log.write_text(new_log_content, encoding="utf-8")
            audit_line = next(
                (l for l in new_out.split("\n")
                 if l.startswith("@@AUDIT@@")), None)
            tb = (new_out.count("Traceback (most recent call last)")
                  + err_sec.count("Traceback (most recent call last)"))
            results[tag]["_evidence_json"] = str(new_json)
            results[tag]["_evidence_log"] = str(new_log)
            results[tag]["_run_binding"] = {
                "json": str(new_json), "log": str(new_log), "rc": 0,
                "where": where, "tag": tag,
                "mode": results[tag]["_run_binding"].get("mode"),
                "capture_id": cap_id, "cmd": ["synthetic", where, tag],
                "injected": True,
                "stdout_sha256": hashlib.sha256(
                    new_out.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(
                    err_sec.encode("utf-8")).hexdigest(),
                "audit_raw_line": audit_line,
                "traceback_count": tb}

    def empty_window_diag_mutate(data):
        data["n04_window_diagnostics"] = [{}, {}, {}]

    def strip_provider_stack_mutate(data):
        # 移除真实异常栈但保留 "aa2" 标记文本——功能诊断仍合格，
        # 拒绝必须来自证据读回（真实 RuntimeError 栈缺失）
        def _strip(text):
            return str(text).replace(
                "RuntimeError: aa2 注入模型调用真实异常",
                "aa2（原始异常栈已移除）") if text else text
        for d in (data.get("n04_window_diagnostics") or []):
            if isinstance(d, dict):
                d["final_text_head"] = _strip(d.get("final_text_head"))
                d["pipeline_error"] = _strip(d.get("pipeline_error"))
        errs = data.get("n04_pipeline_errors")
        if isinstance(errs, list):
            data["n04_pipeline_errors"] = [
                _strip(e) if isinstance(e, str) else e for e in errs]

    ae1_bad = (
        ("ae1-saved-empty-window-diagnostics", "stop",
         empty_window_diag_mutate, None),
        ("ae1-provider-stack-removed", "provider-raise",
         strip_provider_stack_mutate,
         {".venv": good_prov_428, ".venv426": good_prov_426}),
    )
    for name, sc_mode, mutate, src in ae1_bad:
        cap_key = ("observer-provider-raise"
                   if sc_mode == "provider-raise" else "observer-stop")
        verdict = run_aa2_negative(
            sc_mode,
            evidence_bad_runner_factory(
                lambda rs, _m=mutate, _w=name: rewrite_evidence_copy(
                    rs, _m, _w),
                source=src),
            scenario=name, expect_capture_key=cap_key)
        vp = save_verdict(name, verdict, capture_key=cap_key)
        pv = verdict["per_version"]
        # 功能维度仍合格（目标命中+诊断有效），拒绝只来自证据读回，
        # 且 note 必须指认被改的具体内容键
        dims = {t: (
            pv[t]["entry_ok"] and pv[t]["specific_target_hit"]
            and pv[t]["diag_ok"] and not pv[t]["evidence_ok"]
            and "证据" in pv[t]["evidence_note"])
            for t in (".venv", ".venv426")}
        check(f"AE1.{name}-rejected",
              (not verdict["pass"]) and all(dims.values()),
              f"仅改保存内容未按证据维度拒绝：dims={dims} "
              f"notes={verdict['evidence_notes']} verdict_path={vp}")

    # -- AF1.5：只改文件反例（功能输入不变，就地改写绑定指向的文件） ----
    # 删除原始 AUDIT 行 / AUDIT 错值 / 只删 raw traceback（保留
    # RESULT/AUDIT）/ 磁盘 JSON+RESULT 一起反转目标布尔。各版目标/
    # 诊断仍合格，拒绝必须来自证据对账；改后立即恢复原字节。
    def file_only_case(name, sc_mode, cap_key, src, *,
                       mutate_log=None, mutate_json=None):
        results = {tag: dict(src[tag]) for tag in (".venv", ".venv426")}
        originals = {}
        for tag in (".venv", ".venv426"):
            jp = Path(results[tag]["_evidence_json"])
            lp = Path(results[tag]["_evidence_log"])
            jt = jp.read_text(encoding="utf-8")
            lt = lp.read_text(encoding="utf-8", errors="replace")
            originals[(jp, lp)] = (jt, lt)
            if mutate_json is not None:
                data = json.loads(jt)
                mutate_json(data)
                jp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            if mutate_log is not None:
                rc_line, out_sec, err_sec = _split_log_sections(lt)
                new_out = mutate_log(out_sec)
                lt = (f"returncode={results[tag]['_run_binding']['rc']}"
                      "\n===STDOUT===\n" + new_out
                      + "\n===STDERR===\n" + err_sec + "\n")
                lp.write_text(lt, encoding="utf-8")
        try:
            verdict = run_aa2_negative(
                sc_mode,
                evidence_bad_runner_factory(lambda rs: None, source=src),
                scenario=name, expect_capture_key=cap_key)
        finally:
            for (jp, lp), (jt, lt) in originals.items():
                jp.write_text(jt, encoding="utf-8")
                lp.write_text(lt, encoding="utf-8")
        vp = save_verdict(name, verdict, capture_key=cap_key)
        pv = verdict["per_version"]
        dims = {t: (pv[t]["entry_ok"] and pv[t]["specific_target_hit"]
                    and pv[t]["diag_ok"] and not pv[t]["evidence_ok"]
                    and "证据" in pv[t]["evidence_note"])
                for t in (".venv", ".venv426")}
        check(f"AF1.{name}-rejected", (not verdict["pass"])
              and all(dims.values()),
              f"只改文件未按证据维度拒绝：dims={dims} "
              f"notes={verdict['evidence_notes']} verdict_path={vp}")

    def drop_audit_line(sec):
        return "\n".join(l for l in sec.split("\n")
                         if not l.startswith("@@AUDIT@@"))

    def wrong_audit_line(sec):
        return "\n".join(
            ("@@AUDIT@@" + json.dumps({"mode": "normal", "stops": 0}))
            if l.startswith("@@AUDIT@@") else l
            for l in sec.split("\n"))

    def drop_traceback_blocks(sec):
        lines = sec.split("\n")
        headers = [i for i, l in enumerate(lines)
                   if "Traceback (most recent call last)" in l]
        stop_at = next(
            (i for i, l in enumerate(lines)
             if l.startswith("@@RESULT@@")
             or l.startswith("@@AUDIT@@")), len(lines))
        bounds = headers + [stop_at]
        drop = set()
        for a, b in zip(bounds, bounds[1:]):
            drop.update(range(a, b))
        return "\n".join(
            l for i, l in enumerate(lines) if i not in drop)

    def flip_result_and_json_bool(key):
        def _fix_json(data):
            data[key] = True

        def _fix_log(sec):
            def fix(l):
                if l.startswith("@@RESULT@@"):
                    d = json.loads(l[len("@@RESULT@@"):])
                    d[key] = True
                    return "@@RESULT@@" + json.dumps(d,
                                                     ensure_ascii=False)
                return l
            return "\n".join(fix(l) for l in sec.split("\n"))
        return _fix_json, _fix_log

    stop_src = {".venv": good_neg_428, ".venv426": good_neg_426}
    prov_src = {".venv": good_prov_428, ".venv426": good_prov_426}
    file_only_case("af1-audit-line-missing", "stop", "observer-stop",
                   stop_src, mutate_log=drop_audit_line)
    file_only_case("af1-audit-line-wrong", "stop", "observer-stop",
                   stop_src, mutate_log=wrong_audit_line)
    file_only_case("af1-raw-stack-removed", "provider-raise",
                   "observer-provider-raise", prov_src,
                   mutate_log=drop_traceback_blocks)
    fj = flip_result_and_json_bool("n04_all_model_called")
    file_only_case("af1-stop-target-bool-flipped", "stop",
                   "observer-stop", stop_src,
                   mutate_log=fj[1], mutate_json=fj[0])
    fp = flip_result_and_json_bool("n04_all_completed")
    file_only_case("af1-provider-target-bool-flipped", "provider-raise",
                   "observer-provider-raise", prov_src,
                   mutate_log=fp[1], mutate_json=fp[0])

    # -- AF2.A：版本/采集绑定消费——整对象互换、错 tag、写侧 mode 错值 --
    # 绑定维度拒绝，不冒充目标/诊断维度。
    def af2_binding_case(name, corruptor, hit_tag: str = ".venv",
                         *, both_hit: bool = False):
        verdict = run_aa2_negative(
            "stop", evidence_bad_runner_factory(corruptor),
            scenario=name, expect_capture_key="observer-stop")
        vp = save_verdict(name, verdict, capture_key="observer-stop")
        pv = verdict["per_version"]
        other = ".venv426" if hit_tag == ".venv" else ".venv"
        # 被改版本必须按绑定维度拒绝；未改版本保持证据合格（整对象
        # 互换两版都被换，两版都必须拒绝）
        def _hit(t):
            return (pv[t]["entry_ok"] and not pv[t]["evidence_ok"]
                    and "证据" in pv[t]["evidence_note"])
        hit_ok = _hit(hit_tag)
        other_ok = _hit(other) if both_hit else (
            pv[other]["entry_ok"] and pv[other]["evidence_ok"])
        check(f"AF2.{name}-rejected",
              (not verdict["pass"]) and hit_ok and other_ok,
              f"绑定错接未按绑定维度拒绝：hit={hit_ok} "
              f"other_ok={other_ok} "
              f"notes={verdict['evidence_notes']} verdict_path={vp}")

    def swap_whole_versions(results):
        a = dict(results[".venv"])
        b = dict(results[".venv426"])
        results[".venv"] = b
        results[".venv426"] = a

    def wrong_binding_tag(results):
        results[".venv"]["_run_binding"]["tag"] = "unrelated-host"

    def wrong_binding_mode(results):
        results[".venv"]["_run_binding"]["mode"] = "normal"

    af2_binding_case("af2-whole-version-swap", swap_whole_versions,
                     both_hit=True)
    af2_binding_case("af2-binding-tag-wrong", wrong_binding_tag)
    af2_binding_case("af2-binding-mode-normal", wrong_binding_mode)

    # -- AC3/AF2.B：正式 manifest 收尾——实际打开并解析 manifest ----
    # 与调用侧预期（saved_entries）、verdict 文件核对 run/scenario/
    # tag/mode、JSON/log 双引用与核心输入快照；沿 manifest 引用调用
    # 同一正式读回 verify_run_evidence。无 glob/mtime/目录计数兜底。
    closing_failures: list = []
    manifest_closing_checks(saved_entries,
                            failures=closing_failures)
    check("AC3.manifest-closing-clean", not closing_failures,
          f"failures={closing_failures[:6]}")
    check("AC3.saved-entries-unique",
          len({e["verdict_path"] for e in saved_entries})
          == len(saved_entries),
          f"entries={len(saved_entries)}")

    # AF2.B.3：正式 manifest 写入边界注入——内容 {}、错误版本/模式、
    # 指向另一场景的路径，正式收尾必须 FAIL；撤掉破坏后对照通过。
    def _af2_manifest_wrong_meta(m):
        m["mode"] = "provider-raise"
        vs = m.get("versions") or {}
        if ".venv" in vs and isinstance(vs[".venv"], dict):
            vs[".venv"]["mode"] = "provider-raise"
        return m

    def _af2_manifest_wrong_paths(m):
        vs = m.get("versions") or {}
        v, v426 = vs.get(".venv") or {}, vs.get(".venv426") or {}
        if v and v426:
            v["evidence_json"] = v426.get("evidence_json")
            v["evidence_log"] = v426.get("evidence_log")
            b = v.get("run_binding") or {}
            if b:
                b["json"] = v426.get("evidence_json")
                b["log"] = v426.get("evidence_log")
        return m

    def _mini_entry(name, verdict, vpath):
        return {
            "scenario": name, "mode": verdict["mode"],
            "run_id": verdict["run_id"], "verdict_path": vpath,
            "manifest_path": verdict.get("manifest_path", ""),
            "capture_key": "observer-stop",
            "versions": {
                tag: {
                    "scenario": name, "tag": tag,
                    "mode": verdict["mode"],
                    "run_id": verdict["run_id"],
                    "evidence_json": verdict["per_version"][tag][
                        "evidence_json"],
                    "evidence_log": verdict["per_version"][tag][
                        "evidence_log"],
                    "payload_sha256": verdict["per_version"][tag][
                        "payload_sha256"],
                }
                for tag in (".venv", ".venv426")
            },
            "valid_evidence": True,
        }

    af2_manifest_cases = (
        ("af2-manifest-emptied", lambda m: {}),
        ("af2-manifest-wrong-meta", _af2_manifest_wrong_meta),
        ("af2-manifest-wrong-paths", _af2_manifest_wrong_paths),
    )
    for mname, mut in af2_manifest_cases:
        v = run_aa2_negative(
            "stop", evidence_bad_runner_factory(
                corrupt_wrong_reference_disabled),
            scenario=mname, expect_capture_key="observer-stop",
            manifest_mutator=mut)
        vpath = save_verdict(mname, v, capture_key="observer-stop",
                             register=False)
        fails_m: list = []
        manifest_closing_checks([_mini_entry(mname, v, vpath)],
                                failures=fails_m)
        check(f"AF2.{mname}-closing-fails", len(fails_m) > 0,
              f"manifest 破坏未触发正式收尾失败：fails={fails_m[:3]}")

    v_ctrl = run_aa2_negative(
        "stop", evidence_bad_runner_factory(
            corrupt_wrong_reference_disabled),
        scenario="af2-manifest-control",
        expect_capture_key="observer-stop")
    vpath_ctrl = save_verdict("af2-manifest-control", v_ctrl,
                              capture_key="observer-stop",
                              register=False)
    fails_c: list = []
    manifest_closing_checks(
        [_mini_entry("af2-manifest-control", v_ctrl, vpath_ctrl)],
        failures=fails_c)
    check("AF2.manifest-control-closing-clean", not fails_c,
          f"撤破坏对照收尾未通过：fails={fails_c[:3]}")


def manifest_closing_checks(entries, *, failures: list) -> None:
    """AF2.B：正式 manifest 收尾检查——实际打开并解析 manifest_path
    文件内容，与调用侧预期（entries）、verdict 文件三方核对
    run/scenario/tag/mode、JSON/log 双引用与核心输入快照 sha；对
    valid_evidence 条目沿 manifest 引用调用同一正式读回
    verify_run_evidence。失败追加到 failures（scen:name:note），不
    直接注册全局 check；无 glob/mtime/目录计数兜底。
    """

    for entry in entries:
        scen = entry["scenario"]
        vpath = Path(entry["verdict_path"])
        mpath = (Path(entry["manifest_path"])
                 if entry.get("manifest_path") else None)

        def _f(name, cond, note=""):
            if not cond:
                failures.append(f"{scen}:{name}:{note}")

        _f("verdict-file-exists", vpath.is_file(), f"path={vpath}")
        _f("manifest-file-exists",
           bool(mpath and mpath.is_file()), f"path={mpath}")
        verdict = {}
        if vpath.is_file():
            try:
                verdict = json.loads(
                    vpath.read_text(encoding="utf-8"))
            except Exception as exc:
                _f("verdict-parses", False, str(exc)[:80])
        manifest = None
        if mpath and mpath.is_file():
            try:
                manifest = json.loads(
                    mpath.read_text(encoding="utf-8"))
            except Exception as exc:
                _f("manifest-parses", False, str(exc)[:80])
        _f("manifest-parses", isinstance(manifest, dict),
           str(manifest)[:80])
        if not isinstance(manifest, dict):
            continue
        _f("manifest-binding",
           manifest.get("run_id") == entry["run_id"]
           and manifest.get("scenario") == scen
           and manifest.get("mode") == entry["mode"]
           and manifest.get("run_id") == verdict.get("run_id")
           and manifest.get("scenario") == verdict.get("scenario")
           and manifest.get("mode") == verdict.get("mode"),
           f"m=({manifest.get('run_id')},{manifest.get('scenario')},"
           f"{manifest.get('mode')}) v=({verdict.get('run_id')},"
           f"{verdict.get('scenario')},{verdict.get('mode')})")
        for tag in (".venv", ".venv426"):
            mv = (manifest.get("versions") or {}).get(tag) or {}
            pv = (verdict.get("per_version") or {}).get(tag) or {}
            valid = bool(entry.get("valid_evidence"))
            _f(f"{tag}.manifest-fields",
               mv.get("run_id") == entry["run_id"]
               and mv.get("scenario") == scen
               and mv.get("tag") == tag
               and mv.get("mode") == entry["mode"]
               and (not valid or (
                   bool(mv.get("evidence_json"))
                   and bool(mv.get("evidence_log"))
                   and isinstance(mv.get("run_binding"), dict))),
               f"mv=({mv.get('run_id')},{mv.get('scenario')},"
               f"{mv.get('tag')},{mv.get('mode')})")
            _f(f"{tag}.refs-match-verdict",
               mv.get("evidence_json") == pv.get("evidence_json")
               and mv.get("evidence_log") == pv.get("evidence_log"),
               f"m=({str(mv.get('evidence_json'))[-40:]},"
               f"{str(mv.get('evidence_log'))[-40:]}) "
               f"v=({str(pv.get('evidence_json'))[-40:]},"
               f"{str(pv.get('evidence_log'))[-40:]})")
            _f(f"{tag}.payload-bound",
               mv.get("payload_sha256") == pv.get("payload_sha256"),
               f"m={mv.get('payload_sha256')} "
               f"v={pv.get('payload_sha256')}")
            if not valid:
                continue
            binding = mv.get("run_binding") or {}
            out = {"_run_binding": binding,
                   "_evidence_json": mv.get("evidence_json"),
                   "_evidence_log": mv.get("evidence_log")}
            ok, note = verify_run_evidence(
                out, mv.get("mode"), pv.get("payload"),
                expect_tag=tag,
                expect_capture_key=entry.get("capture_key"))
            _f(f"{tag}.evidence-readback", ok, note[:180])


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
