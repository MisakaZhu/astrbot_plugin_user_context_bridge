# 状态与进度（可恢复断点）

维护规则：每阶段记录当前提交、实际改动、验证命令与结果、失败项、下一步。证据必须对应提交。

## 当前阶段：P1 / MIS-137（准备开始）

### P0 / MIS-136 已完成（In Review）

改动与证据：

- 仓库初始化：main 分支、.gitignore、仓库级 Git 身份（复用已有作者身份，未改全局）。
- 宿主源码核对（4.26.0 + 4.28.0，只读）：
  - internal.py 调用链（build → on_llm_request → reset_coro → run_agent → _save_to_history）；
  - `_save_to_history` 以 `req.conversation` None 短路；写回目标 `req.conversation.cid`；
  - Runner.reset 消费 `req.contexts`；`req.conversation` 仅 token_usage 引用且 None 防护；
  - 人格 begin_dialogs 在 build 阶段插入 req.contexts 头部。
- P0 最小脱网生命周期验证 `tests/p0_lifecycle_check.py`（真实 Runner/run_agent/MainAgentHooks/call_event_hook/_save_to_history + 假 Provider/事件）：
  - 4.28.0：PASS 17/17（2026-09-17）
  - 4.26.0：PASS 17/17（2026-09-17）
  - 实证修正：模型 err 两版均不触发 on_agent_done（fallback 层转 err 响应）；aborted 两版均触发 on_agent_done 但形态版本各异（4.28 marker / 4.26 空或完整文本）且 `agent_user_aborted` 旗标在钩子后才写入——写侧兜底轨（on_decorating_result）为必须项（ADR-003）。
- ADR-001 ~ ADR-007、COMPATIBILITY.md、README/CHANGELOG/HANDOFF/PLAN 基线。
- Linear：MIS-136 置 In Review（附提交 SHA）。

复现命令：

```bash
PYTHONPATH=. <venv428>/Scripts/python.exe tests/p0_lifecycle_check.py
PYTHONPATH=. <venv426>/Scripts/python.exe tests/p0_lifecycle_check.py
```

### P1 待办

- [ ] 身份模块（platform_id, self_id, persona_scope, sender_id；默认人格稳定常量；persona_manager 解析复用宿主同参调用）
- [ ] 范围模块（显式启用、群号白名单+私聊开关、默认关闭；个人开关不扩大范围）
- [ ] 归属判定（真实 sender、排除未唤醒闲聊/机器人自言/管理命令；UMO 不变）
- [ ] A03-A06 场景测试

### 下一步

P1（MIS-137）实现与测试；Linear 同步。

## 阶段历史

| 阶段 | 提交 | 结果 |
| --- | --- | --- |
| P0 / MIS-136 | 1c080d1 | 双版本 17/17 PASS；MIS-136 In Review |
