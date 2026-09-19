"""W 生命周期父套件（W1/W2/W5/W8）：双版 spawn 真实宿主 worker + 故障注入。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/w_lifecycle_check.py

- 每版解释器各 spawn 一次 tests/w_lifecycle_worker.py（真实 PluginManager
  load/reload、真实调度链），输出 JSON 逐字段断言；
- W8 故障注入：直接调用本套件的断言函数，对关键字段逐个翻假——断言必须
  判 FAIL（故障注入实调父测试，不另复制一份断言自证）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def _run_worker(venv: str, td: str, *, observer: str | None = None,
                fault: str = "none") -> dict:
    """运行 W worker 并解析结果（__error__ 表示失败）。observer 传入
    y_fault_observer.py 时在真实 wait_for 边界注入 fault（task:type）；
    非观测路径命令行与原直跑完全一致。"""

    tests_dir = Path(__file__).resolve().parent
    py = venv + r"\Scripts\python.exe"
    if observer:
        cmd = [py, "-X", "utf8", str(tests_dir / observer),
               "w_lifecycle_worker", td, fault]
    else:
        cmd = [py, str(tests_dir / "w_lifecycle_worker.py"), td]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO)
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=300,
    )
    line = next(
        (l for l in r.stdout.splitlines() if l.startswith("@@RESULT@@")),
        None,
    )
    # X6：非零退出码与缺失/损坏 JSON 进入判定
    if r.returncode != 0:
        return {"__error__": f"worker rc={r.returncode}: "
                + (r.stderr or r.stdout)[-400:]}
    if line is None:
        return {"__error__": "worker 输出缺少 @@RESULT@@ 行："
                + (r.stderr or r.stdout)[-400:]}
    try:
        return json.loads(line[len("@@RESULT@@"):])
    except json.JSONDecodeError as exc:
        return {"__error__": f"worker 结果 JSON 损坏：{exc}："
                + line[len("@@RESULT@@"):][:200]}


def _validate_worker_output(result, td: Path) -> dict:
    """X6：对子进程完成结果（返回码+输出）做判定；异常/非零/坏 JSON 均
    转为 __error__，供父断言判 FAIL。"""

    line = next(
        (l for l in result.stdout.splitlines() if l.startswith("@@RESULT@@")),
        None,
    )
    if result.returncode != 0:
        return {"__error__": f"worker rc={result.returncode}: "
                + (result.stderr or result.stdout)[-400:]}
    if line is None:
        return {"__error__": "worker 输出缺少 @@RESULT@@ 行："
                + (result.stderr or result.stdout)[-400:]}
    try:
        return json.loads(line[len("@@RESULT@@"):])
    except json.JSONDecodeError as exc:
        return {"__error__": f"worker 结果 JSON 损坏：{exc}"}


def assert_worker_fields(tag: str, out: dict, *, check=check) -> None:
    """对 worker 结果逐字段断言。check 参数可替换（故障注入用）。"""

    if "__error__" in out:
        check(f"W1.{tag}.worker-runs", False, out["__error__"])
        return
    check(f"W1.{tag}.worker-runs", True)
    # Y1：任务结果显式归类。活动任务必须正常返回；排队任务正常返回或
    # 精确命中宿主 call_event_hook 卸载插件日志 KeyError 边界（完整栈、
    # 异常类型/键/来源、已停止、0 模型调用、无共享回写、锁无残留）。
    check(f"X6.{tag}.switch-active-task",
          out.get("switch_active_outcome") == "returned",
          f"outcome={out.get('switch_active_outcome')!r} "
          f"tb={str(out.get('switch_active_traceback'))[:200]}")
    queued_outcome = out.get("switch_queued_outcome")
    boundary_evidence = (
        out.get("switch_queued_exc_type") == "KeyError"
        and out.get("switch_queued_exc_key")
        == "data.plugins.astrbot_plugin_user_context_bridge.main"
        and out.get("switch_queued_exc_source")
        == "astrbot/core/pipeline/context_utils.py:call_event_hook"
        and isinstance(out.get("switch_queued_full_traceback"), str)
        and out.get("switch_queued_stopped") is True
        and out.get("switch_queued_model_calls") == 0
        and out.get("switch_queued_ledger_no_completed") is True
        and out.get("switch_queued_no_output") is True
    )
    check(
        f"X6.{tag}.switch-queued-task",
        queued_outcome == "returned"
        or (queued_outcome == "host_boundary_keyerror" and boundary_evidence),
        f"outcome={queued_outcome!r} "
        f"exc_type={out.get('switch_queued_exc_type')!r} "
        f"exc_key={out.get('switch_queued_exc_key')!r} "
        f"exc_source={out.get('switch_queued_exc_source')!r} "
        f"model_calls={out.get('switch_queued_model_calls')!r} "
        f"ledger_no_completed={out.get('switch_queued_ledger_no_completed')!r} "
        f"no_output={out.get('switch_queued_no_output')!r} "
        f"stopped={out.get('switch_queued_stopped')!r}",
    )
    # 加载与 Phase 0（status 无接管证据不得宣称共享）
    check(f"W1.{tag}.load", out.get("load_ok") is True
          and (out.get("bound_handlers") or 0) >= 5, f"out={out}"[:200])
    check(f"W5.{tag}.status-fresh-no-evidence",
          out.get("status_fresh_no_evidence") is True)
    check(f"W5.{tag}.status-after-turn",
          out.get("status_after_turn") is True)
    # W1：模式持久化与代次推进
    check(f"W1.{tag}.gen0-persona-recorded",
          out.get("gen0_persona_recorded") is True)
    check(f"W1.{tag}.same-mode-reload-preserves",
          out.get("same_mode_reload_preserves") is True
          and out.get("same_mode_gen_unchanged") is True)
    check(f"W1.{tag}.first-direct-switch-bumps",
          out.get("first_direct_switch_bumps") is True)
    check(f"W1.{tag}.first-switch-archives-old",
          out.get("first_switch_archives_old") is True)
    check(f"W1.{tag}.archived-rows-queryable",
          out.get("archived_rows_queryable") is True)
    check(f"W1.{tag}.user-history-chains",
          out.get("user_history_chains") is True)
    check(f"W1.{tag}.switch-back-bumps",
          out.get("switch_back_bumps") is True)
    check(f"W1.{tag}.back-to-persona-fresh",
          out.get("back_to_persona_fresh") is True)
    check(f"W1.{tag}.second-user-bumps",
          out.get("second_user_bumps") is True)
    check(f"W1.{tag}.second-user-no-resurrect",
          out.get("second_user_no_resurrect") is True)
    seq = out.get("generation_sequence") or []
    check(
        f"W1.{tag}.generation-sequence",
        [s.get("generation") for s in seq[:5]] == [0, 1, 1, 2, 3]
        and seq[0].get("scope_mode") == "persona"
        and seq[1].get("scope_mode") == "user"
        and seq[2].get("scope_mode") == "user"
        and seq[3].get("scope_mode") == "persona",
        f"seq={seq}",
    )
    # W1：reset 后切换不复活
    check(f"W1.{tag}.reset-scope-text", out.get("reset_text_scope") is True)
    check(f"W1.{tag}.post-reset-no-resurrect",
          out.get("post_reset_no_resurrect") is True)
    # W2：退出继承矩阵
    check(f"W2.{tag}.persona-off-blocks", out.get("persona_off_blocks") is True)
    check(f"W2.{tag}.status-shows-inherited-off",
          out.get("status_shows_inherited_off") is True)
    check(f"W2.{tag}.user-inherits-off-runtime",
          out.get("user_inherits_off_runtime") is True)
    check(f"W2.{tag}.user-on-unblocks", out.get("user_on_unblocks") is True)
    check(f"W2.{tag}.persona-off-after-user-off",
          out.get("persona_off_after_user_off") is True)
    check(f"W2.{tag}.future-persona-protected",
          out.get("future_persona_protected") is True)
    check(f"W2.{tag}.status-persona-inherited",
          out.get("status_persona_inherited") is True)
    check(f"W2.{tag}.single-on-releases-only-that",
          out.get("single_on_releases_only_that") is True)
    # W1：配置变化时挂起 A+排队 B 受控停止
    check(f"W1.{tag}.switch-active-no-late",
          out.get("switch_active_no_late") is True)
    check(f"W1.{tag}.switch-active-stopped",
          out.get("switch_active_stopped") is True)
    check(f"W1.{tag}.switch-active-interrupted",
          out.get("switch_active_interrupted") is True)
    check(f"W1.{tag}.switch-queued-no-output",
          out.get("switch_queued_no_output") is True)
    check(f"W1.{tag}.switch-new-config-turn",
          out.get("switch_new_config_turn_done") is True
          and out.get("switch_new_config_fresh") is True)


def fault_injection_calls_parent() -> None:
    """W8/N23：对断言函数注入翻假字段，验证其必须判 FAIL（实调父断言）。"""

    venv = r"D:\第三方插件完善\.venv"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        good = _run_worker(venv, td)
    if "__error__" in good:
        check("N23.fault-inject-baseline", False, good["__error__"])
        return
    key_fields = [
        "same_mode_reload_preserves",
        "first_direct_switch_bumps",
        "first_switch_archives_old",
        "back_to_persona_fresh",
        "second_user_no_resurrect",
        "persona_off_blocks",
        "user_inherits_off_runtime",
        "future_persona_protected",
        "single_on_releases_only_that",
        "switch_active_no_late",
        "switch_queued_no_output",
        "switch_new_config_fresh",
    ]
    undetected = []
    for field in key_fields:
        doctored = dict(good)
        doctored[field] = False
        lp: list = []
        lf: list = []

        def local_check(n, cond, detail=""):
            (lp if cond else lf).append(n)

        assert_worker_fields("FI", doctored, check=local_check)
        if not lf:
            undetected.append(field)
    # X6/Y1：任务异常（TimeoutError）/缺失字段也必须被父断言检出
    doctored = dict(good)
    doctored["switch_active_outcome"] = "failed:TimeoutError"
    doctored["switch_active_traceback"] = "Traceback ... TimeoutError"
    doctored.pop("switch_new_config_turn_done", None)
    lp: list = []
    lf: list = []
    assert_worker_fields("FI", doctored, check=local_check)
    if not lf:
        undetected.append("task-exception+missing-field")
    check(
        "N23.fault-inject-parent-detects-all",
        not undetected,
        f"未检出翻假字段：{undetected}",
    )
    # X6：子进程边界——rc=19 且带合法结果行，必须判失败；rc=0 对照通过
    class FakeResult:
        def __init__(self, rc, stdout):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = ""

    good_line = "@@RESULT@@" + json.dumps(good, ensure_ascii=False) + "\n"
    bad = _validate_worker_output(FakeResult(19, good_line), Path(td))
    check("X6.worker-rc19-fails", "__error__" in bad, f"out={bad}")
    ok = _validate_worker_output(FakeResult(0, good_line), Path(td))
    check("X6.worker-rc0-passes", "__error__" not in ok)


def fault_injection_real_paths() -> None:
    """Y1/N23：故障注入作用于真实路径——y_fault_observer 在真实 worker
    进程的真实 wait_for 边界注入，输出交由真实 _run_worker 解析、真实
    assert_worker_fields 判定；正常对照必须过，逐个故障必须判 FAIL。
    另对真实 _run_worker 子进程入口注入 rc=19 / 缺 RESULT 行 / 损坏 JSON。
    （观测运行只取单版解释器：检测机制与解释器版本无关。）"""

    venv = r"D:\第三方插件完善\.venv"
    scenarios = [
        "t_a:RuntimeError",
        "t_a:TimeoutError",
        "t_b:RuntimeError",
        "t_b:KeyErrorOther",
    ]
    for spec in scenarios:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            out = _run_worker(venv, td, observer="y_fault_observer.py",
                              fault=spec)
        lp: list = []
        lf: list = []

        def local_check(n, cond, detail=""):
            (lp if cond else lf).append(n)

        if "__error__" in out:
            lf.append("worker-error")
        else:
            assert_worker_fields("FI", out, check=local_check)
        check(
            f"Y1.w-realpath-{spec.replace(':', '-')}-detected",
            bool(lf),
            f"未检出：spec={spec} pass={len(lp)} lf={lf}",
        )

    # 正常对照：观测器驱动（fault=none）必须与直跑同样全过
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        good = _run_worker(venv, td, observer="y_fault_observer.py",
                           fault="none")
    lp: list = []
    lf: list = []

    def local_check2(n, cond, detail=""):
        (lp if cond else lf).append(n)

    if "__error__" in good:
        lf.append("worker-error")
    else:
        assert_worker_fields("OBS", good, check=local_check2)
    check("Y1.w-observer-normal-passes", not lf,
          f"lf={lf} err={str(good.get('__error__'))[:300]}")

    if "__error__" in good:
        return

    # 真实子进程入口：rc=19 / 缺 RESULT 行 / 损坏 JSON → 真实 _run_worker
    # 必须转 __error__，并由真实父断言判 FAIL
    class FakeResult:
        def __init__(self, rc, stdout, stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    good_line = "@@RESULT@@" + json.dumps(good, ensure_ascii=False) + "\n"
    entry_cases = {
        "rc19": FakeResult(19, good_line, "injected nonzero exit"),
        "no-result-line": FakeResult(0, "no marker here\n", ""),
        "bad-json": FakeResult(0, "@@RESULT@@{oops", ""),
    }
    for name, result in entry_cases.items():
        with patch.object(subprocess, "run", return_value=result):
            read = _run_worker("synthetic-venv", "synthetic-td")
        entry_lf: list = []

        def entry_check(n, cond, detail=""):
            if not cond:
                entry_lf.append(n)

        assert_worker_fields("ENTRY", read, check=entry_check)
        check(
            f"Y1.w-entry-{name}-detected",
            "__error__" in read and bool(entry_lf),
            f"read={str(read)[:150]} lf={entry_lf}",
        )


def main() -> int:
    for venv in (
        r"D:\第三方插件完善\.venv",
        r"D:\第三方插件完善\.venv426",
    ):
        tag = Path(venv).name
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            out = _run_worker(venv, td)
            assert_worker_fields(tag, out)
    fault_injection_calls_parent()
    fault_injection_real_paths()
    print(f"\n=== W 生命周期回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
