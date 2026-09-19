"""Z1a：所有实际 worker 子进程入口共用的唯一结果读取/判定。

收敛 w_lifecycle / s_rework / w_zip / t_rework / y2 各套件执行入口的
失败判定，避免各处复制检查而漏检：

- 调用方统一经 safe_run 执行 worker（命令为固定参数列表、shell=False；
  超时/启动失败不抛出，转 ``{"__error__": ...}``）；
- read_worker_result 判定：非零退出码、缺 @@RESULT@@ 行、JSON 损坏、
  JSON 不是对象，全部转 ``{"__error__": 可定位信息}``；
- 必要字段缺失由各套件的 assert_*_fields 断言兜底（字段缺失不等于
  False，逐字段 is True 断言必然 FAIL），入口只负责形状。

故障注入必须经过正式父函数及其实际 subprocess 处理路径
（patch tests.worker_result.safe_run 后调用真实入口函数），不接受
只测 validator。
"""

from __future__ import annotations

import json
import subprocess

RESULT_MARKER = "@@RESULT@@"


class _FailedResult:
    """safe_run 失败分支的最小 CompletedProcess 替身。"""

    def __init__(self, returncode: int, stderr: str, stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def safe_run(cmd: list, *, env=None, cwd=None, timeout: int = 300):
    """执行 worker（固定列表命令、shell=False）。

    超时返回 rc=-2、启动失败返回 rc=-1 的结果对象，均带可定位 stderr，
    由 read_worker_result 统一转 __error__。
    """

    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc_text(exc)
        return _FailedResult(-2, f"worker 超时（timeout={timeout}s）",
                             stdout=partial)
    except OSError as exc:
        return _FailedResult(-1, f"worker 启动失败：{exc}")


def exc_text(exc: BaseException | None) -> str:
    if exc is not None and getattr(exc, "stdout", None):
        raw = exc.stdout
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return ""


def read_worker_result(completed) -> dict:
    """对真实子进程完成结果做唯一判定；失败形态一律转 __error__。"""

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    tail = (stderr or stdout)[-400:]
    rc = getattr(completed, "returncode", None)
    if rc != 0:
        return {"__error__": f"worker rc={rc}: {tail}"}
    line = next(
        (l for l in stdout.splitlines() if l.startswith(RESULT_MARKER)),
        None,
    )
    if line is None:
        return {"__error__": "worker 输出缺少 @@RESULT@@ 行: " + tail}
    payload = line[len(RESULT_MARKER):]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {
            "__error__": f"worker 结果 JSON 损坏：{exc}: {payload[:200]}"
        }
    if not isinstance(parsed, dict):
        return {
            "__error__": f"worker 结果 JSON 不是对象"
            f"（{type(parsed).__name__}）: {payload[:200]}"
        }
    return parsed
