# 状态与进度（可恢复断点）

维护规则：每阶段记录当前提交、实际改动、验证命令与结果、失败项、下一步。证据必须对应提交。

## 当前阶段：P7 / MIS-143（交付打包）→ 本地候选完成，待 Codex 独立验收

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

### P1-P6 已完成

- P1（MIS-137）`ffeec5d`：身份/范围/归属，35/35。
- P2（MIS-138）`bcec73d`：SQLite 轮次账本（去重/顺序/epoch/重启恢复/双实例防护），27/27。
- P3（MIS-139）`dd650e7`：完整闭环（读侧接管 + 写侧三轨 + 原窗口回复），19/19。
- P4（MIS-140）`414b7a3`：跨窗口并发/异常取消/清空竞态/发送状态，21/21。
- P5（MIS-141）`a389f3a`：/uctx 命令组、停用重载语义、插件共存（ADR-008 原生命令边界），16/16。
- P6（MIS-142）`d9d55e9`：A01-A18 验收映射 + 打包/隐私验证，8/8。

测试统计：七套 × 两版宿主（4.26.0/4.28.0）各 151 项断言全部通过。复现命令见 docs/ACCEPTANCE.md。

### P7 待办

- [x] README/CHANGELOG/HANDOFF/STATUS 更新
- [x] 可安装 ZIP + SHA-256（release/ 忽略目录，不入库）
- [x] Git 与 ZIP 清单终审
- [ ] 最终提交与交付报告（MIS-143 In Review）

### 下一步

Codex 独立验收（MIS-144）；用户实机验证（MIS-145）。

## 阶段历史

| 阶段 | 提交 | 结果 |
| --- | --- | --- |
| P0 / MIS-136 | 1c080d1 | 双版本 17/17 PASS；In Review |
| P1 / MIS-137 | ffeec5d | 双版本 35/35 PASS；In Review |
| P2 / MIS-138 | bcec73d | 双版本 27/27 PASS；In Review |
| P3 / MIS-139 | dd650e7 | 双版本 19/19 PASS；In Review |
| P4 / MIS-140 | 414b7a3 | 双版本 21/21 PASS；In Review |
| P5 / MIS-141 | a389f3a | 双版本 16/16 PASS；In Review |
| P6 / MIS-142 | d9d55e9 | 双版本 8/8 PASS；In Review |
