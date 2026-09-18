# HANDOFF（交接说明）

面向 Codex 复验（MIS-144）与用户实机验证（MIS-145）。对应最终提交见 docs/STATUS.md；证据均对应该提交。

## 交付摘要（V1/V2 五次返工后，0.6.0）

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
