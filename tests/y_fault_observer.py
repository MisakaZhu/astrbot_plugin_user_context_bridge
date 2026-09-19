"""Y1 故障观测器：在真实 worker 的真实 asyncio.wait_for 等待边界注入故障。

worker 进程内运行，不改 worker 源码；观测器以正确入口驱动 worker.main()
（不触发其 __main__ 分支），因此不漏挂观察器。真实等待结果先记录再按
fault 说明注入替代异常，用于验证父套件对任务异常的拒绝能力。

用法：python -X utf8 y_fault_observer.py <worker_module> <instance_root> \
        <task:fault> [zip_path]

- worker_module：tests/ 下 worker 文件名（不带 .py），如
  s6_plugin_lifecycle_worker / w_lifecycle_worker；
- task:fault：none，或 t6:RuntimeError / t3:TimeoutError / t4:RuntimeError /
  t_a:RuntimeError / t_b:RuntimeError / t_b:KeyErrorOther 等；
- 输出：worker 原样 @@RESULT@@ 行 + @@AUDIT@@ 观测行（真实返回值或真实
  异常的完整调用栈 + 注入记录）。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_INJECTED = {
    "RuntimeError": RuntimeError("独立注入任务失败"),
    "TimeoutError": TimeoutError("独立注入任务超时"),
    "KeyErrorOther": KeyError("injected-unrelated-key"),
}


def _make_injected(fault: str, cause: BaseException | None):
    exc = _INJECTED[fault]
    if cause is not None:
        return exc.__class__(*exc.args)
    return exc


async def _wait_observed(awaitable, timeout):  # noqa: ANN001
    raise NotImplementedError  # 由 main() 内闭包替换，占位说明结构


def main() -> int:
    worker_name, root, fault_spec = sys.argv[1:4]
    zip_arg = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "none" else None
    Path(root).mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))
    worker = importlib.import_module("tests." + worker_name)
    # 以 worker 的正式命令行入口参数驱动 main()
    sys.argv = [str(REPO / "tests" / (worker_name + ".py")), root] + (
        [zip_arg] if zip_arg else []
    )
    target_task, _, fault = fault_spec.partition(":")
    audit: list = []
    original_wait = asyncio.wait_for

    async def wait_observed(awaitable, timeout):  # noqa: ANN001
        caller = inspect.currentframe().f_back
        targeted = (
            caller is not None
            and caller.f_code.co_filename.endswith(worker_name + ".py")
            and caller.f_locals.get(target_task) is awaitable
            and fault in _INJECTED
        )
        try:
            returned = await original_wait(awaitable, timeout)
        except BaseException as exc:  # noqa: BLE001
            if targeted:
                audit.append(
                    {
                        "task": target_task,
                        "phase": "real-raise",
                        "type": type(exc).__name__,
                        "traceback": traceback.format_exc(),
                    }
                )
                # 用注入异常替代真实异常：验证"任意其他异常也须失败"
                raise _make_injected(fault, exc) from exc
            raise
        if targeted:
            audit.append(
                {
                    "task": target_task,
                    "phase": "real-returned",
                    "returned": repr(returned)[:200],
                }
            )
            audit.append(
                {
                    "task": target_task,
                    "phase": "injected",
                    "type": fault,
                }
            )
            raise _make_injected(fault, None)
        return returned

    asyncio.wait_for = wait_observed
    try:
        code = asyncio.run(worker.main())
    finally:
        asyncio.wait_for = original_wait
        print("@@AUDIT@@" + json.dumps(audit, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
