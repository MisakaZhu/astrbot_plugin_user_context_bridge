# HANDOFF（交接说明）

面向 Codex 复验（MIS-144）与用户实机验证（MIS-145）。对应最终提交见 docs/STATUS.md；证据均对应该提交。

## 交付摘要（U1~U4 四次返工后，0.5.0）

- 版本 0.5.0（四次返工候选），main 分支，无远端、未 push。
- 基线链：8477eba → d8a7147 → 169a88a → 5f662d0（T1~T6）→ 本次（U1~U4，ADR-013）。
- 测试：10 套 × 4.26.0/4.28.0 各 **290 项断言全部 PASS**（日志逐项加总：P0=17 P1=36 P2=27 P3=19 P4=22 P5=16 P6=8 R=38 S=46 T=61）。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（12 文件白名单）；SHA-256 见交付报告与同目录 .sha256 文件。

## 四次返工要点（对应四次验收报告 U1~U4）

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| U1 初始解析/等锁取消仍执行 | 取消保护未覆盖锁前与等锁等待点 | 三个可等待点全纳入（初始解析/锁获取/锁后）；宿主吞取消后 stop_event 在下一 yield 检查点截断 | t: U1.initial-resolve-*（6 项：controlled/stops/no-model/no-output/no-turn/no-lock-leak）+ U1.queued-*（8 项：waiter-present/controlled/stops/no-model/no-output/a-unaffected/only-a-turn/lock-released/next-completes）；T2 保留并收紧 |
| U2 无 provider 时 new 漏清 | 防御统一要求 provider，但宿主 new 不要求 | 分命令防御：new 只复核会话存在 | t5 worker: U2.<版>.new-with-provider-syncs / new-without-provider-syncs / recovery-no-backfill（双版各 3 项） |
| U3 文本前缀依赖 | 成功判定用 startswith，前置装饰即漏清 | 结构化标记 `_clean_group_context_session`（宿主本地成功末尾设置的布尔 extra）+ activated_handlers 双证据 | t5 worker: U3.<版>.prefix-decorator-still-syncs / clean-mark-structure（双版各 2 项）；真实 ResultDecorateStage + 真实 bound 处理器 + PipelineScheduler |
| U4 测试吞异常/断言不足 | harness trace 未初始化；gather 结果未检查；S6 新字段未断言 | trace 提前初始化；gather 结果纳入断言；T2 登记前取消完整断言；S6 生命周期 4 组断言（故障注入验证 FAIL） | t: T2.cancel-task-controlled / pre-registration-cancel-clean（收紧）；s: S6.<版>.turn-off-stops-active / queued-clean-yield / recovery-no-backfill / uninstall（4 组新增）；local_evidence/fault_inject_check.py（10 字段全 false → 4 组全 FAIL） |

## 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check t_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 期望：每套 FAIL=0；两版宿主各 290 项断言通过
```

## 复验建议（Codex，MIS-144）

1. 复跑 10 套测试（两版宿主）核对 290×2（日志逐项加总）。
2. 重点核查：
   - U1 的三个取消注入点（初始解析/等锁/锁后）与「未取得锁不释放他人锁」的归属边界；
   - U3 的 `_clean_group_context_session` 产生/消费时机核对（宿主 builtin conversation.py 两版；权限拒绝/provider 缺失/第三方分支确实不设置）；
   - U2 分命令防御与宿主 new 真实语义的对齐；
   - U4 故障注入（local_evidence/fault_inject_check.py）与父测试断言的对应。
3. 交付包：ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

同前版。

## 待用户决定

- 新代码开源许可；GitHub 仓库名称/远端/上传。

## 已知限制

- v1 不自动迁移既有 QQ 历史；同库双实例第二实例禁用（崩溃重启等旧租约 300s 过期）；fail-watchdog 默认 180s。
- 语音/引用端到端、GUI 演练、真实 QQ 演练：待 MIS-145。
- 其他插件若直接改写 req.contexts 的覆盖竞争（ADR-002 声明）。
