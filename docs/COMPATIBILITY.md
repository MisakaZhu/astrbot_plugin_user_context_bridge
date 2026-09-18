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

---

# 0.7.0 兼容与升级说明（待 Codex 验收）

本地验证环境不变：AstrBot 4.26.0 / 4.28.0，Python 3.12.10（两 venv 实测），QQ OneBot v11 / aiocqhttp，普通内置 Agent。

## 升级（0.6.0 → 0.7.0）

- 首次以新版启动时自动把账本从 schema v1 迁移到 v2：`backups/pre-migrate-v2-<时间戳>-<库名>` 先行备份（含 WAL checkpoint），变更在单个 IMMEDIATE 事务内，失败回滚、原库与备份不变；重复启动幂等跳过。
- 保持 `history_scope=persona`（默认）升级：旧有效问答、epoch、个人退出状态全部保留——升级不是隐式清空，不需要任何数据操作。
- 切换 `history_scope`（persona↔user）：该基础身份开启新代次，从空历史开始；旧记录归档可查（工具 `--archives`）不自动合并；仅 reload 同配置、重启或人格轮换不清空。
- 0.6.0 旧库在未迁移前不可用旧版工具直接查看：`tools/uctx_records.py` 检测 v1 会明确拒绝并提示先运行新版插件完成迁移；工具自身不升级数据库。

## 回滚（0.7.0 → 0.6.0 旧包）

- 未证明旧版代码可直接读取 v2 结构：**回滚前必须先停用新版并保留升级前备份**（`backups/pre-migrate-v2-*` 或用户自备副本）。把数据目录中的账本恢复为备份后，再装回 0.6.0 包，方可保证历史无损。直接换回旧代码而不恢复备份，0.6.0 会把 v2 库当作旧库再次"迁移"，产生列冲突错误——这是受保护行为，不损坏数据，但不构成回滚路径。
- membership.json v2 新增键（base_protected / persona_on）在 0.6.0 中被忽略（按空集合兼容读取），退出状态不丢失。

## 0.7.0 新增配置与命令面

- `history_scope`：`persona`（默认）/ `user`；非法值保守回退 persona 并输出警告，不静默扩大共享范围。
- 个人命令语义按模式区分（reset/off 的清空与退出范围），详见 README；原生命令 /reset /new 联动语义与 0.6.0 一致。
- 新增 `tools/uctx_records.py`（纯标准库、只读），随安装包交付；Windows / Linux / 容器挂载数据目录均可独立运行，不启动 AstrBot、不需要凭据与网络。

## 0.7.0 双版回归

14 套测试 × 两版各 381 项断言全 PASS（逐项计数见 docs/ACCEPTANCE.md）；复现命令同上节，n 系列为：

```bash
for t in n0_baseline_check n1_scope_migration_check n2_cross_persona_check n3_records_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
```
