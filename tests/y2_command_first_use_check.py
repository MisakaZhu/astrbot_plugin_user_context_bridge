"""Y2 父套件：首次只执行命令的身份（off/on）也登记模式事实。

双版 spawn tests/y2_command_first_use_worker.py（真实 PluginManager +
真实 CommandService），输出 JSON 逐字段断言。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/y2_command_first_use_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def _run_worker(venv: str, td: str) -> dict:
    tests_dir = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO)
    r = subprocess.run(
        [venv + r"\Scripts\python.exe",
         str(tests_dir / "y2_command_first_use_worker.py"), td],
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
    # Y1 同口径：非零退出码与缺失/损坏 JSON 进入判定
    if r.returncode != 0:
        return {"__error__": f"worker rc={r.returncode}: "
                + (r.stderr or r.stdout)[-400:]}
    if line is None:
        return {"__error__": "worker 输出缺少 @@RESULT@@ 行："
                + (r.stderr or r.stdout)[-400:]}
    try:
        return json.loads(line[len("@@RESULT@@"):])
    except json.JSONDecodeError as exc:
        return {"__error__": f"worker 结果 JSON 损坏：{exc}"}


def assert_y2_fields(tag: str, out: dict, *, check=check) -> None:
    """Y2 字段断言（故障注入可实调本函数，不复制断言）。"""

    if "__error__" in out:
        check(f"Y2.{tag}.worker-runs", False, out["__error__"])
        return
    check(f"Y2.{tag}.worker-runs", True)
    check(f"Y2.{tag}.load",
          out.get("load_ok") is True
          and (out.get("bound_handlers") or 0) >= 5, f"out={out}")
    # Phase 0：v1 旧退出升级——加载对账登记 persona/0（不推进），旧退出
    # 迁移后继续生效；命令幂等不推进
    check(f"Y2.{tag}.v1-upgrade-registered-no-bump",
          out.get("v1_scope_after_load") == "persona"
          and out.get("v1_gen_after_load") == 0,
          f"scope={out.get('v1_scope_after_load')!r} "
          f"gen={out.get('v1_gen_after_load')!r}")
    check(f"Y2.{tag}.v1-legacy-exit-effective",
          out.get("v1_turn_done") is True
          and out.get("v1_exit_blocks_capture") is True)
    check(f"Y2.{tag}.v1-command-keeps-registration",
          out.get("v1_off_idempotent_ok") is True
          and out.get("v1_scope_after_cmd") == "persona"
          and out.get("v1_gen_after_cmd") == 0)
    # Phase 1：persona 模式新用户首次 off 登记 persona/0；直接切 user
    # 恰好 +1（Y2 核心反例）；往返与保护贯穿
    check(f"Y2.{tag}.first-off-registers-persona",
          out.get("off1_text_ok") is True
          and out.get("A_scope_after_off") == "persona"
          and out.get("A_gen_after_off") == 0,
          f"text={str(out.get('off1_text_ok'))} "
          f"scope={out.get('A_scope_after_off')!r} "
          f"gen={out.get('A_gen_after_off')!r}")
    check(f"Y2.{tag}.repeat-off-idempotent",
          out.get("off1_repeat_ok") is True
          and out.get("A_gen_after_repeat") == 0)
    check(f"Y2.{tag}.persona-off-blocks-capture",
          out.get("A_turn_done") is True
          and out.get("A_exit_blocks_capture") is True)
    check(f"Y2.{tag}.direct-switch-bumps-once",
          out.get("A_scope_after_switch") == "user"
          and out.get("A_gen_after_switch") == 1,
          f"scope={out.get('A_scope_after_switch')!r} "
          f"gen={out.get('A_gen_after_switch')!r}")
    check(f"Y2.{tag}.user-mode-exit-inherited",
          out.get("A_user_mode_exit_blocks") is True)
    check(f"Y2.{tag}.switch-back-bumps",
          out.get("A_scope_back") == "persona"
          and out.get("A_gen_back") == 2)
    check(f"Y2.{tag}.future-persona-protected",
          out.get("A_future_persona_protected") is True)
    check(f"Y2.{tag}.on-no-bump",
          out.get("on1_text_ok") is True
          and out.get("A_scope_after_on") == "persona"
          and out.get("A_gen_after_on") == 2)
    check(f"Y2.{tag}.captured-after-on",
          out.get("A_captured_after_on") == 1)
    # Phase 2：user 模式方向
    check(f"Y2.{tag}.first-off-registers-user",
          out.get("off2_text_ok") is True
          and out.get("B_scope_after_off") == "user"
          and out.get("B_gen_after_off") == 0,
          f"scope={out.get('B_scope_after_off')!r} "
          f"gen={out.get('B_gen_after_off')!r}")
    check(f"Y2.{tag}.user-off-blocks-capture",
          out.get("B_exit_blocks_capture") is True)
    check(f"Y2.{tag}.user-direction-switch-bumps",
          out.get("B_scope_after_switch") == "persona"
          and out.get("B_gen_after_switch") == 1)
    # Phase 3：首次 on（无 off）登记；重复 on 幂等；切换推进一次
    check(f"Y2.{tag}.first-on-registers",
          out.get("on3_text_ok") is True
          and out.get("C_scope_after_on") == "persona"
          and out.get("C_gen_after_on") == 0,
          f"scope={out.get('C_scope_after_on')!r} "
          f"gen={out.get('C_gen_after_on')!r}")
    check(f"Y2.{tag}.repeat-on-idempotent",
          out.get("on3_repeat_ok") is True
          and out.get("C_gen_after_repeat") == 0)
    check(f"Y2.{tag}.on-captured-persona",
          out.get("C_captured") == 1)
    check(f"Y2.{tag}.on-switch-bumps",
          out.get("C_scope_after_switch") == "user"
          and out.get("C_gen_after_switch") == 1)
    check(f"Y2.{tag}.on-captured-user-mode",
          out.get("C_captured_user_mode") == 1)
    # Phase 4：登记失败 → 受控文案/退出不失/重载补登记/重试不多推进
    check(f"Y2.{tag}.register-failure-controlled",
          out.get("off5_controlled_text") is True,
          f"text={str(out.get('off5_controlled_text'))}")
    check(f"Y2.{tag}.register-failure-no-record",
          out.get("E_scope_after_failed_reg") is None)
    check(f"Y2.{tag}.exit-survives-failed-registration",
          out.get("E_exit_survives_failed_reg") is True)
    check(f"Y2.{tag}.reload-reconciles-once",
          out.get("E_scope_after_reload") == "user"
          and out.get("E_gen_after_reload") == 0,
          f"scope={out.get('E_scope_after_reload')!r} "
          f"gen={out.get('E_gen_after_reload')!r}")
    check(f"Y2.{tag}.exit-survives-reload",
          out.get("E_exit_survives_reload") is True)
    check(f"Y2.{tag}.retry-no-extra-bump",
          out.get("off5_retry_ok") is True
          and out.get("E_gen_after_retry") == 0)
    check(f"Y2.{tag}.retry-then-switch-bumps-once",
          out.get("E_scope_final") == "persona"
          and out.get("E_gen_final") == 1)
    check(f"Y2.{tag}.user-off-protects-persona",
          out.get("E_user_off_protects_persona") is True)
    # Phase 5：已生效身份配置切换推进一次；同模式重载不动
    check(f"Y2.{tag}.effective-identity-switch-once",
          out.get("A_gen_final_after_switch")
          == (out.get("A_gen_final") or 0) + 1,
          f"{out.get('A_gen_final')} -> {out.get('A_gen_final_after_switch')}")
    check(f"Y2.{tag}.same-mode-reload-no-bump",
          out.get("A_gen_same_mode_reload")
          == out.get("A_gen_final_after_switch"))


def main() -> int:
    for venv in (
        r"D:\第三方插件完善\.venv",
        r"D:\第三方插件完善\.venv426",
    ):
        tag = Path(venv).name
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            out = _run_worker(venv, td)
            assert_y2_fields(tag, out)
    print(f"\n=== Y2 首次命令登记回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
