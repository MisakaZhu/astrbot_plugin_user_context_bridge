"""AA2 诊断观测器：让 t1 worker 的 N04 窗口出现受控无调用或真实异常，
验证父断言拒绝能力与诊断字段完整性（不虚 PASS、不二次 KeyError）。

模式（argv[3]）：
  stop           注册真实 OnLLMRequestEvent 钩子，对 USER-MODE-2/3 返回
                 True 停止传播（宿主 call_event_hook 语义）→ model_calls
                 [1,0,0]、hook_stopped [F,T,T]，outcome=hook-stopped；
  provider-raise 在 worker 导入前替换 tests.fakes.FakeProvider，模型调用
                 真实抛 RuntimeError → 经调度边界进入 pipeline_error。

输出：worker 原样 @@RESULT@@ 行 + @@AUDIT@@（模式与观测计数）。
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
    mode = sys.argv[2] if len(sys.argv) > 2 else "stop"
    Path(root).mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))
    sys.argv = [str(REPO / "tests" / "t1_persona_worker.py"), root]

    stops = {"calls": 0}

    async def _aa2_stopper(ev, req):
        if "USER-MODE-2" in (ev.message_str or "") or (
                "USER-MODE-3" in (ev.message_str or "")):
            stops["calls"] += 1
            # 宿主 call_event_hook 忽略 handler 返回值：停止靠
            # event.stop_event()，随后 is_stopped() 返回 True
            ev.stop_event()
            return True
        return None

    if mode == "stop":
        from astrbot.core.star.star_handler import (
            StarHandlerMetadata,
            star_handlers_registry,
        )
        from astrbot.core.star.star import StarMetadata
        from astrbot.core.star.star_handler import EventType

        mod = "aa2_n04_stopper"
        star_map_entry = StarMetadata(name=mod, activated=True)
        import astrbot.core.star.star as star_mod

        setattr(star_mod, "star_map", star_mod.__dict__.get("star_map"))
        # star_map 为模块级 dict，直接注册条目
        from astrbot.core.star.star import star_map

        star_map[mod] = star_map_entry
        meta = StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name=mod + "_stop",
            handler_name="stop",
            handler_module_path=mod,
            handler=_aa2_stopper,
            event_filters=[],
            extras_configs={"priority": -100},
        )
        star_handlers_registry._handlers.append(meta)
        star_handlers_registry.star_handlers_map[meta.handler_full_name] = meta
    elif mode == "provider-raise":
        import tests.fakes as fakes_mod

        class AA2RaisingProvider(fakes_mod.FakeProvider):
            async def text_chat(self, *args, **kwargs):
                # 仅对 N04 USER-MODE 窗口抛真实异常（Runner 以 contexts
                # 传参、无 prompt kwarg）；先记 call_log 再抛——模型调用
                # 计数即"异常边界确实到达"的证据；前面 persona 场景
                # 保持原语义
                if "USER-MODE-" in str(kwargs.get("contexts") or ""):
                    self.call_log.append({"aa2_boundary": "provider-raise"})
                    raise RuntimeError("aa2 注入模型调用真实异常")
                return await super().text_chat(*args, **kwargs)

        fakes_mod.FakeProvider = AA2RaisingProvider

    code = 0
    try:
        importlib.import_module("tests.t1_persona_worker")
    except SystemExit as exc:  # t1 为模块级入口
        code = exc.code or 0
    finally:
        print("@@AUDIT@@" + json.dumps(
            {"mode": mode, "stops": stops["calls"]},
            ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
