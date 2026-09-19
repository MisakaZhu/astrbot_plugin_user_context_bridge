"""Z3a 工具丢弃观测器：在真实 ToolLoopAgentRunner._func_tool_for_provider
边界把工具置 None，驱动真实 t1 worker（完整宿主链），用于验证正式父
断言对"终模型工具缺失"的拒绝能力。

用法：python -X utf8 z3_tools_fault_observer.py <instance_root>
输出：worker 原样 @@RESULT@@ 行 + @@AUDIT@@（丢弃次数）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    root = sys.argv[1]
    Path(root).mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))
    # t1 worker 以模块级 sys.exit(asyncio.run(main())) 运行：
    # 先设好 argv 再导入，patch 在导入前挂载（观察器不漏挂）。
    sys.argv = [str(REPO / "tests" / "t1_persona_worker.py"), root]

    from astrbot.core.agent.runners.tool_loop_agent_runner import (
        ToolLoopAgentRunner,
    )

    original = ToolLoopAgentRunner._func_tool_for_provider
    drops = {"calls": 0}

    def dropped(self):  # noqa: ANN001
        drops["calls"] += 1
        return None

    ToolLoopAgentRunner._func_tool_for_provider = dropped  # type: ignore
    code = 0
    try:
        importlib.import_module("tests.t1_persona_worker")
    except SystemExit as exc:  # 模块级入口的正常退出形态
        code = exc.code or 0
    finally:
        print("@@AUDIT@@" + json.dumps(drops, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
