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

REPO = Path(__file__).resolve().parent.parent

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def _run_worker(venv: str, td: str) -> dict:
    worker = str(Path(__file__).resolve().parent / "w_lifecycle_worker.py")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO)
    r = subprocess.run(
        [venv + r"\Scripts\python.exe", worker, td],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=240,
    )
    line = next(
        (l for l in r.stdout.splitlines() if l.startswith("@@RESULT@@")),
        None,
    )
    if line is None:
        return {"__error__": (r.stderr or r.stdout)[-500:]}
    return json.loads(line[len("@@RESULT@@"):])


def assert_worker_fields(tag: str, out: dict, *, check=check) -> None:
    """对 worker 结果逐字段断言。check 参数可替换（故障注入用）。"""

    if "__error__" in out:
        check(f"W1.{tag}.worker-runs", False, out["__error__"])
        return
    check(f"W1.{tag}.worker-runs", True)
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
    check(f"W1.{tag}.switch-user-bumps",
          out.get("switch_user_bumps") is True)
    check(f"W1.{tag}.user-fresh-start", out.get("user_fresh_start") is True)
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
        [s.get("generation") for s in seq[:5]] == [0, 0, 1, 2, 3]
        and seq[0].get("scope_mode") in (None, "persona")
        and seq[2].get("scope_mode") == "user",
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
        "switch_user_bumps",
        "user_fresh_start",
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
        local_pass: list = []
        local_fail: list = []

        def local_check(name, cond, detail=""):
            (local_pass if cond else local_fail).append(name)

        assert_worker_fields("FI", doctored, check=local_check)
        if not local_fail:
            undetected.append(field)
    check(
        "N23.fault-inject-parent-detects-all",
        not undetected,
        f"未检出翻假字段：{undetected}",
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
    print(f"\n=== W 生命周期回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
