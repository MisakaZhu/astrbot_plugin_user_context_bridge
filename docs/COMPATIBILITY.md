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

> Y 轮更正（2026-09-20）：本节 W 轮文本曾写"pre-migrate-v2 备份（含 WAL
> checkpoint）"与"0.6.0 直接读 v3 会列冲突报错"，均与实现和真实 0.6.0
> 代码行为不符，已按下方现状更正；回滚步骤已用真实 0.6.0（859f18e）
> 代码在合成库上逐步验证（local_evidence/y_logs/y4_rollback_verify.json）。

## 升级（0.6.0 → 0.7.0 Y 轮版）

- 首次以新版启动时自动把账本从 schema v1（0.6.0）或 v2（0.7.0 首个候选）迁移到 v3：迁移事务开始前先用 **SQLite backup API** 从只读连接整库复制出一致性快照 `backups/pre-migrate-v3-<时间戳>-<库名>`（WAL 中已提交事务包含在快照内，不是 checkpoint+复制主库）；目标名唯一递增，绝不覆盖既有备份；数据结构变更、键改写、回填与版本标记在**单个 IMMEDIATE 事务**内完成，失败回滚、原库与备份不变；重复启动幂等跳过。
- 保持 `history_scope=persona`（默认）升级：旧有效问答、epoch、个人退出状态全部保留——升级不是隐式清空。迁移同时把身份键改写为 v3 编码，membership.json 退出键同步改写（改写前自动备份 `membership.json.pre-scheme3.bak`）。
- 键改写按**输入来源版本解码**（X3/ADR-017）：v1（0.6.0）来源的 scope 段一律是原始人格名字面值——含 `maid`、`p:maid`、`u:`、`q:foo`、`__mode_user__` 等——全部按字面值加 `p:` 前缀保留（如 `p:maid`、`p:__mode_user__`），不做隔离；只有来源确定为 v2 的 `__mode_user__` 才无法区分"真实同名人格"与"user 模式记录"，按安全方向改写为 `q:` 隔离段保留原数据，并把对应退出转为基础身份保护（宁可过度保护不可误采集）。
- 切换 `history_scope`（persona↔user）：该基础身份开启新代次，从空历史开始；旧记录归档可查（工具 `--archives`）不自动合并；仅 reload 同配置、重启或人格轮换不清空。
- 模式事实登记（Y2）：升级对账、首次参与轮次、以及首次 `/uctx off`/`/uctx on` 命令参与都会把当前生效模式登记到该基础身份；三类入口区分——v1 升级登记不推进代次，新身份首次命令登记不推进代次，显式配置切换恰好推进一次。
- 0.6.0 旧库在未迁移前不可用旧版工具直接查看：`tools/uctx_records.py` 检测 v1 会明确拒绝并提示先运行新版插件完成迁移；工具自身不升级数据库。

## 回滚（0.7.0 Y 轮版 → 0.6.0 旧包）

以下步骤已用**真实 0.6.0（859f18e）代码**在合成库上验证（探针
`local_evidence/y4_rollback_verify.py`，结果
`local_evidence/y_logs/y4_rollback_verify.json`）：

1. 停用/卸载新版插件。
2. 恢复账本：把数据目录 `uctx_ledger.db`（含 `-wal`/`-shm` 侧文件，若存在则一并删除）替换为 `backups/pre-migrate-v3-<时间戳>-uctx_ledger.db`。已验证：真实 0.6.0 代码对恢复后的库可正常打开、读出全部旧问答、并继续写入新轮次。
3. 恢复退出文件：把 `membership.json` 替换为 `membership.json.pre-scheme3.bak`。已验证：恢复后真实 0.6.0 代码按裸键命中退出；而 v3 前缀键退出文件在真实 0.6.0 代码下**不再命中裸键**（前缀键不等于裸键）——因此只要曾在新版下产生过退出状态，回滚必须连同恢复 `.pre-scheme3.bak`，否则退出保护会静默失效。

**不要**不恢复备份直接把旧代码指回 v3 库。实测行为是**静默键分裂而非报错**：真实 0.6.0 代码可以正常打开 v3 库（其建表为 `IF NOT EXISTS`，不会因多余列失败），但它按裸身份键读写——迁移后的旧记录（`p:` 前缀键）全部不可见，新写入以裸键落盘形成第二套并行历史。这比报错更危险：不损坏文件、没有任何警告，但历史"消失"且数据分叉。该行为与 W 轮文档声称的"列冲突错误"不符，特此更正。

## 0.7.0 新增配置与命令面

- `history_scope`：`persona`（默认）/ `user`；非法值保守回退 persona 并输出警告，不静默扩大共享范围。
- 个人命令语义按模式区分（reset/off 的清空与退出范围），详见 README；原生命令 /reset /new 联动语义与 0.6.0 一致（群聊非管理员发送 `/reset` 由宿主自身拒绝，插件不联动——该宿主行为已在真实分发链中验证）。
- 新增 `tools/uctx_records.py`（纯标准库、只读），随安装包交付；Windows / Linux / 容器挂载数据目录均可独立运行，不启动 AstrBot、不需要凭据与网络。
