# 版本兼容矩阵

本地验证环境（不代表云端实际版本）：

| AstrBot 版本 | Python 要求 | P0 生命周期验证 | 备注 |
| --- | --- | --- | --- |
| 4.26.0 | >= 3.12 | PASS 17/17（2026-09-17） | fallback 层无停止检查；中止依赖 provider 协作 abort_signal；aborted 回调可能为完整回复文本 |
| 4.28.0 | >= 3.12 | PASS 17/17（2026-09-17） | 中止注入 USER_INTERRUPTION 标记对；fallback 层有停止检查 |

支持边界（v1 声明）：

- 适配器：QQ OneBot v11 / aiocqhttp。
- Agent 路径：AstrBot 普通内置 Agent（InternalAgentSubStage / ToolLoopAgentRunner）。
- 明确不支持：QQ 官方适配器、Dify/Coze 等第三方会话执行器（third_party 路径）、WebChat 专用行为。未实测的宿主版本不宣称兼容。

两版宿主共用同一插件设计的技术依据（ADR-001）：`build_main_agent → on_llm_request → reset_coro → run_agent → _save_to_history` 结构一致；`_save_to_history` 均以 `req.conversation` None 短路；Runner 均以 `req.contexts` 为消息基底。

复现命令：

```bash
# 4.28.0
PYTHONPATH=. <venv428>/Scripts/python.exe tests/p0_lifecycle_check.py
# 4.26.0
PYTHONPATH=. <venv426>/Scripts/python.exe tests/p0_lifecycle_check.py
```
