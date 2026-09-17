# HANDOFF（交接说明）

面向 Codex 复验（MIS-144）与用户实机验证（MIS-145）。对应最终提交见 docs/STATUS.md；证据均对应该提交。

## 交付摘要（T1~T6 三次返工后，0.4.0）

- 版本 0.4.0（三次返工候选），main 分支，无远端、未 push。
- 基线链：8477eba → d8a7147 → 169a88a → 本次（T1~T6，ADR-012）。
- 测试：10 套 × 4.26.0/4.28.0 各 **258 项断言全部 PASS**（日志逐项加总：P0=17 P1=36 P2=27 P3=19 P4=22 P5=16 P6=8 R=38 S=40 T=35）；新增 tests/t_rework_check.py（35 项/版）与 t1_persona_worker.py / t5_native_worker.py（真实宿主组件子进程）。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（12 文件白名单）；SHA-256 见交付报告与同目录 .sha256 文件。

## 三次返工要点（对应三次验收报告 T1~T6）

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| T1 4.26 默认人格共享同一历史 | resolve 未传 provider_settings（4.26 仅从该参数读默认人格） | 同参解析 + provider_settings_getter 注入；失败抛 PersonaResolutionError 受控（不折叠 __default__）；开场白同源 | t: T1.<版>.distinct-persona-keys / b-excludes-a-history / b-keeps-own-begin-dialogs / explicit-conversation-persona / resolution-failure-controlled（真实 PersonaManager+ConversationManager+新会话路径，双版各 5 项） |
| T2 锁后取消泄锁 | CancelledError 逃过 except Exception；pending/watchdog 未登记 | pending/watchdog 提前登记；CancelledError 分支收尾 interrupted + 停止传播 + 释放锁 | t: T2.cancel-no-model / no-output / stops-event / turn-finalized / lock-released / next-request-completes / pre-registration-cancel-clean |
| T3 卸载后活动/排队轮继续 | terminate 只标账本；无关闭入口/停止/排队管理 | shutdown() 关闭协议（_closing 双检、活动轮全套停止、释放锁唤醒排队者）；terminate 顺序 | s6 worker：turn_off（活动轮停止无迟到输出、排队干净让出事件终止）+ turn_on（恢复正常无回灌）+ uninstall；before_shutdown_pending/waiters 观测 |
| T4 watchdog 后缓冲正文仍发送 | 只置 agent_stop_requested，buffer 正文经 aborted 分支 yield | 全套停止信号（stop_event→scheduler yield 断链）+ decorating 对已终态轮 clear_result | t: T4.no-new-output-after-timeout / no-buffered-late-output / failed-finalized（buffer=True 真实 run_agent） |
| T5 真实 4.26 Context 不联动 | 无条件调 get_using_provider_async（4.26 无此接口） | 按真实 Context 接口存在性选择 async/同步 | t: T5.<版>.real-context-reset-syncs（真实 Context 构造；4.26 实测 has_async=False 仍联动）+ no-provider-no-clear |
| T6 命令与宿主执行脱节 | 同名单处理器独立于宿主分发（禁用误清/旧名误清/新名漏清） | 删同名处理器；decorating 后置事实关联（activated_handlers + 宿主固定成功文案 + 轻量防御） | t: T6.<版> 全矩阵（真实 WakingCheckStage+StarRequestSubStage+真实 Context：默认成功联动、disabled/renamed-old/filter/no-provider 不清、renamed-new 联动，双版各 6 项）+ commands.py 文案统一 |

## 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check t_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 期望：每套 FAIL=0；两版宿主各 258 项断言通过（t_rework 含真实宿主
# 组件子进程 worker，需可创建临时目录）
```

## 复验建议（Codex，MIS-144）

1. 复跑 10 套测试（两版宿主）核对 258×2（日志逐项加总）。
2. 重点核查：
   - T1 的同参面（provider_settings 来源与宿主 _ensure_persona_and_skills 完全一致；4.26/4.28 解析器差异）；受控失败不产生共享写入；
   - T2/T3 的全部退出路径（重复/登记前后取消/关闭/卸载）锁与轮次收尾完备性；
   - T4 双保险与真实 scheduler 的 is_stopped 断链语义；
   - T5/T6 worker 的真实面（真实 Context/WakingCheckStage/StarRequestSubStage/builtin 命令实例；manager 双方法等价独立探针——Context 层版本差异才是验证点）；改名/过滤经 CommandFilter 真实对象操作。
3. 交付包：ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

同前版 +：不同默认人格的两窗口隔离确认；宿主无可用模型时 `/reset`（只见宿主拒绝提示，共享不清空）；改名后的原生命令成功时联动确认。

## 待用户决定

- 新代码开源许可；GitHub 仓库名称/远端/上传。

## 已知限制

- v1 不自动迁移既有 QQ 历史；同库双实例第二实例禁用（崩溃重启等旧租约 300s 过期）；fail-watchdog 默认 180s。
- 语音/引用端到端、GUI 演练、真实 QQ 演练：待 MIS-145。
- 其他插件若直接改写 req.contexts 的覆盖竞争（ADR-002 声明）。
