# HANDOFF（交接说明）

面向 Codex 独立复验（MIS-144）与用户实机验证（MIS-145）。对应最终提交见 docs/STATUS.md；证据均对应该提交。

## 交付摘要（R1~R7 返工后）

- 版本 0.2.0（返工候选），main 分支，无远端、未 push。
- 基线 8477eba 独立验收 R1~R7 全部返工完成（见 docs/STATUS.md 返工节与 ADR-003 v2/004 v2/009/010）。
- 测试：8 套 × 4.26.0/4.28.0 各 **181 项断言全部 PASS**（含新增 tests/harness.py 真实调度链与 tests/r_rework_check.py 返工回归 37 项/版）。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（白名单 12 文件）；SHA-256 见交付报告与同目录 .sha256 文件。

## 返工要点（对应独立验收报告）

| 项 | 修复 | 证据 |
| --- | --- | --- |
| R1 | main.py 包内相对导入；A17 按宿主 data.plugins.<name>.main 真实路径导入 | p6: A17.module-importable；r: import_plugin_module |
| R2 | 终态机 v2：on_agent_done 主提交（is_stopped 判 aborted）；decorating 仅 err 文本加速；after_sent 兜底；fail-watchdog；真实调度链 harness | r: R2.*（11 项：工具轮/取消/err 快速/err watchdog/空回复/卸载） |
| R3 | 身份锁持有至轮次终态化 | r: R3.second-waits-for-first / second-sees-first-question-and-answer / different-identity-parallel |
| R4 | prompt 前缀匹配本轮边界；临时内容不入库；completed 保底配对 | r: R4.pair-complete / transient-excluded / next-request-contains-pair |
| R5 | 命令身份接入 conversation.persona_id（含 getter 未调用缺陷修复） | r: R5.command-identity-matches / reset-clears-selected-persona / off-stops-selected-persona |
| R6 | 租约运行时 ID + owner_token 归属校验；真实双子进程排他 | r: R6.*（6 项，含 multiprocess-exclusive / no-cross-release） |
| R7 | 原生 /reset、/new 联动 epoch（ADR-010；权限镜像 + 未共享原行为） | r: R7.*（5 项）；ACCEPTANCE A11 更新 |

## 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 期望：每套 FAIL=0；两版宿主各 181 项断言通过
```

## 复验建议（Codex，MIS-144）

1. 复跑 8 套测试（两版宿主）核对 181×2。
2. 重点核查：
   - R2 终态机：tests/harness.py 的调度链（PipelineScheduler + ResultDecorateStage + 逐 yield 下游）与真实宿主顺序的一致性；
   - R3 锁释放路径（on_agent_done/watchdog/terminate 三处）与死锁面；
   - R6 owner_token 边界（伪造 token 心跳/释放被拒、多进程互斥）；
   - R7 权限镜像与宿主 builtin 逻辑（conversation.py）的一致性。
3. 交付包：ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

同 0.1.0 版（HANDOFF 历史），新增两条：
9. 群聊 admin `/reset` → 提示同步清空共享历史；非 admin `/reset` → 仅宿主原提示；`/new` 同理联动；未共享成员 `/reset` 无插件提示。
10. 同数据目录误开第二个 AstrBot 实例 → 第二实例插件禁用并告警（共享不生效）；关闭后约 300 秒内重启需等旧租约过期。

## 待用户决定

- 新代码开源许可（发布前确认）。
- GitHub 仓库名称、远端与上传（本仓库无 remote）。

## 已知限制

- v1 不自动迁移既有 QQ 历史。
- 同库双实例：第二实例禁用；崩溃后重启需等旧租约心跳过期（默认 300s，有意保守）。
- fail-watchdog 默认 180s：无钩子的模型 err 轮最迟此时落 failed（不影响读取）。
- 语音/引用端到端、GUI 级停用/卸载、独立实例 GUI 安装、原生联动实机确认：待 MIS-145。
- 其他插件若直接改写 req.contexts（而非 system_prompt/extra parts），与本插件的上下文替换语义存在覆盖竞争（ADR-002 声明）。
