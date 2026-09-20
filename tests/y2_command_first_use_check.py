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
    from tests import worker_result

    tests_dir = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO)
    r = worker_result.safe_run(
        [venv + r"\Scripts\python.exe",
         str(tests_dir / "y2_command_first_use_worker.py"), td],
        env=env,
        cwd=str(REPO),
        timeout=300,
    )
    out = worker_result.read_worker_result(r)
    if "__error__" in out:
        out["__error__"] += "（失败留证：" + worker_result.persist_failure(
            "y2_command_first_use_worker", r, out) + "）"
    return out


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
    # Phase 4（Z2）：真实 SQLite 失败协议——语句级失败（TEMP TRIGGER）
    # 与第二连接写锁（BEGIN IMMEDIATE 冲突）；登记先行，失败即整体未生效
    check(f"Z2.{tag}.A-failed-off-controlled",
          out.get("off5_controlled_text") is True,
          f"text={str(out.get('off5_controlled_text'))}")
    check(f"Z2.{tag}.A-nothing-saved",
          out.get("Z2A_scope_after_failed_off") is None
          and out.get("Z2A_exit_not_saved") is True,
          f"scope={out.get('Z2A_scope_after_failed_off')!r} "
          f"exit={out.get('Z2A_exit_not_saved')!r}")
    check(f"Z2.{tag}.A-retry-registers",
          out.get("Z2A_retry_ok") is True
          and out.get("Z2A_scope_after_retry") == "user"
          and out.get("Z2A_gen_after_retry") == 0)
    check(f"Z2.{tag}.B-pre-off-ok",
          out.get("Z2B_pre_off_ok") is True
          and out.get("Z2B_scope_pre") == "user")
    check(f"Z2.{tag}.B-locked-on-controlled",
          out.get("Z2B_on_locked_controlled") is True,
          f"text={str(out.get('Z2B_on_locked_controlled'))}")
    check(f"Z2.{tag}.B-failed-on-keeps-exit",
          out.get("Z2B_exit_retained_memory") is True
          and out.get("Z2B_exit_retained_disk") is True,
          f"mem={out.get('Z2B_exit_retained_memory')!r} "
          f"disk={out.get('Z2B_exit_retained_disk')!r}")
    check(f"Z2.{tag}.B-exit-blocks-after-failed-on",
          out.get("Z2B_exit_blocks_capture_after_failed_on") is True)
    check(f"Z2.{tag}.B-retry-on-captures",
          out.get("Z2B_retry_on_ok") is True
          and out.get("Z2B_captured_after_successful_on") == 1)
    check(f"Z2.{tag}.C-failed-off-controlled",
          out.get("Z2C_off_controlled") is True)
    check(f"Z2.{tag}.C-nothing-saved",
          out.get("Z2C_scope_none") is True
          and out.get("Z2C_exit_not_saved") is True)
    check(f"Z2.{tag}.C-direct-switch-no-ghost",
          out.get("Z2C_scope_after_switch") is None
          and out.get("Z2C_gen_after_switch") == 0,
          f"scope={out.get('Z2C_scope_after_switch')!r} "
          f"gen={out.get('Z2C_gen_after_switch')!r}")
    check(f"Z2.{tag}.C-retry-registers-current-mode",
          out.get("Z2C_retry_ok") is True
          and out.get("Z2C_scope_after_retry") == "persona"
          and out.get("Z2C_gen_after_retry") == 0)
    check(f"Z2.{tag}.C-switch-then-bumps-once",
          out.get("Z2C_gen_after_switch_back") == 1
          and out.get("Z2C_exit_blocks_capture") is True)
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
