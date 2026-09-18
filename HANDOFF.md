# HANDOFF（交接说明）

## 0.7.0 候选交付（2026-09-19，待 Codex 独立验收 MIS-170）

**分支 `feat/0.7.0-persona-records`（基于 main/859f18e）。N0-N4 本地开发与自测闭环；未 push、未发布、实机未验证。**

### 版本与证据对应

| 项 | 值 |
| --- | --- |
| 交付提交（实现+文档） | 见 `git log`（N4 提交，含 main.py 回归修复/文档/版本 0.7.0） |
| 候选包 | `release/astrbot_plugin_user_context_bridge-<sha>.zip`（13 文件白名单，含 `tools/uctx_records.py`） |
| 包 SHA-256 | 见 `release/<包名>.sha256` 与交付报告 |
| 旧 0.6.0 包 | `release/astrbot_plugin_user_context_bridge-ac51bde.zip`（78e64560…9af）**未触碰** |
| 全量回归 | 14 套 × 4.26.0/4.28.0 各 **381 项断言全 PASS**（旧 10 套 298 + 新 4 套 83）；日志 `local_evidence/n4_logs/` |
| 浏览器验证 | `local_evidence/n3_html/render-full.png` / `render-filtered.png`（Playwright/Chromium，控制台 0 错误；Codex 视觉复核待 MIS-170） |
| N 系列矩阵 | `docs/ACCEPTANCE.md` 0.7.0 节（N01-N23 PASS，N24 待实机） |

### N0-N4 摘要

- **N0（MIS-165）`a560856`**：基线核对、ADR-015、数据契约快照。
- **N1（MIS-166）`4ced8a8`**：账本 v1→v2 迁移（备份/幂等/失败回滚）、mode_generation 代次、MembershipStore v2、退出状态继承。
- **N2（MIS-167）`75b0a26`**：bridge source_persona、main 装配 history_scope、commands user 模式身份、原生命令双模式清空范围。
- **N3（MIS-168）`b22f99f`**：`tools/uctx_records.py` 只读记录工具（list/export-json/export-html；mode=ro 一致快照；筛选/归档/截断/转义/输出守卫/v1 拒绝）；`tests/n3_records_check.py` 50 断言双版全过；清理误跟踪的 `_tmp_m.json`。
- **N4（MIS-169）本轮**：
  - **修复真实加载回归**：main.py 传给 CommandService 不存在的 `stats_getter`（N2 引入），真实 PluginManager 加载即 TypeError、实例丢弃后租约残留（表现为 S6 load 失败 + "另一实例租约"告警）。n 系列不经 main.initialize() 未覆盖，由 N4 全量回归 S6 暴露；修复并给冲突分支补 `history_scope`。修复后 S6 双版 46/46。
  - n0 基线反例按计划"修复后同一反例转通过"（absent → present/wired）。
  - 文档：README（跨人格/本地工具/升级说明）、CHANGELOG 0.7.0、STATUS、ACCEPTANCE N 矩阵、COMPATIBILITY（升级/回滚：回滚须先恢复升级前备份再装旧包）、metadata 0.7.0。
  - 打包：`local_evidence/package_070.py`（白名单 ZIP + SHA-256 + 双版导入探针 + ZIP 解包后工具独立运行验证）。

### 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check t_rework_check          n0_baseline_check n1_scope_migration_check n2_cross_persona_check          n3_records_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe -X utf8 tests/$t.py
done
# 逐套 PASS 数加总 = 381/版本（17+36+27+19+22+16+8+38+46+69+9+13+11+50）
# 打包：python local_evidence/package_070.py
```

### 边界与待办

- 本轮不 push、不建 Release、不动旧包；许可由用户决定。
- MIS-170 Codex 独立验收；MIS-145 朋友实机（含 N24 跨人格演练与本地工具实操）继续，本地全过不替代实机。
- 迁移仅在合成库演练；真实库升级前先备份。

---

## 历史交接（0.6.0，已被上方取代，保留）

<details>
<summary>0.6.0 暂停断点与五次返工交接（历史）</summary>



- 版本 0.6.0（五次返工候选），main 分支，无远端、未 push。
- 基线链：8477eba → d8a7147 → 169a88a → 5f662d0 → 06d5598（U1~U4）→ 本次（V1/V2，ADR-014）。
- 测试：10 套 × 4.26.0/4.28.0 各 **298 项断言全部 PASS**（日志逐项加总：P0=17 P1=36 P2=27 P3=19 P4=22 P5=16 P6=8 R=38 S=46 T=69）。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（12 文件白名单）；SHA-256 见交付报告与同目录 .sha256 文件。

## 五次返工要点（对应五次验收报告 V1/V2）

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| V1 同一成功多次联动 | 宿主标记持久于本事件，同事件多处理器各自回复都再进装饰阶段，每次都 bump_epoch | 插件私有 applied 状态（event extra `_uctx_native_sync_applied`）判定成功后同步原子认领（任何 await 前）；后续装饰跳过；清空已提交后提示发送失败不回退；新命令=新事件不受影响 | 基线复现（v1_baseline_probe，基线 main.py 双版 notify=2）→ 修复后 t: V1.<版>.reset/new-once-only + new-turn-survives（epoch_delta=1、notify=1、屏障期间新问答保留、native 仅 1 次调用，每版 4 项） |
| V2a 假恢复链 | `_recovery_check` 独立栈直接 bump_epoch | worker `new-without-provider-then-recovery` 场景：同命令事件同账本，恢复 provider 后真实新轮 | old_leak=False / new_present=True（双版） |
| V2b 复制断言的故障注入 | fault_inject_check 只对构造字典求值 | monkeypatch `_run_s6_worker` 子进程实调父测试 `s6_plugin_lifecycle` | 正常 16 PASS；10 字段逐个 false 各触发（每字段 2 FAIL）；全 false 8 FAIL/8 PASS |
| V2c U1 笔误 | 排队取消断言检查前一场景 `ev` | 改查当前事件 `b` | U1.queued-cancel-controlled |
| V2d 文档口径 | 固定成功文案过期描述、T2/A17 数字不一致 | 结构化标记描述统一；T2=9、U1=15、A17 handler=10；MIS-141 上轮"一次成功只关联一次"表述更正 | 本轮 Linear 评论 |

## 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check t_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# V1 基线缺陷复现：python local_evidence/v1_baseline_probe.py
# V2b 故障注入：python local_evidence/fault_inject_check.py
```

## 复验建议（Codex，MIS-144）

1. 复跑 10 套测试（两版）核对 298×2（日志逐项加总）。
2. 重点核查：
   - V1：applied 状态的认领时机（同步、await 前）、发送失败不回退、新命令不受影响；基线复现探针的隔离性（基线 main.py 来自 git show，不触碰工作区）；
   - V2a：worker recover 场景与独立 final 探针 `new-without-provider-then-recovery` 的链路一致性；
   - V2b：故障注入实际调用父测试的 monkeypatch 边界。
3. 交付包：ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

同前版。

## 待用户决定

- 新代码开源许可；GitHub 仓库名称/远端/上传。

## 已知限制

- v1 不自动迁移既有 QQ 历史；同库双实例第二实例禁用（崩溃重启等旧租约 300s 过期）；fail-watchdog 默认 180s。
- 语音/引用端到端、GUI 演练、真实 QQ 演练：待 MIS-145。
- 其他插件若直接改写 req.contexts 的覆盖竞争（ADR-002 声明）。

</details>
