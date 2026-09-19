## 当前：0.7.0 X1–X6 返工完成（待 Codex 复验 MIS-170）

- 分支 `feat/0.7.0-persona-records`；W 返工基线 4f542fe；X 轮交付提交 **760f647**，
  补遗 **4cee8cf**；候选包 `astrbot_plugin_user_context_bridge-4cee8cf.zip`
  （SHA-256 `6581762dfd2e112a53ccbdb52e7e3925e66d212f439ed6cd9fe384b405941e4d`）。
- 修复独立复验确认的 X1–X6：首次参与模式登记（X1）、user off 撤销旧 persona_on（X2）、
  迁移按输入版本解码（X3）、持久化失败不发布内存（X4）、原生联动有效退出（X5）、
  失败检测收紧与真实链补齐（X6：N04 user 真实装配、N10 双模式分发、N22 交付 ZIP
  全生命周期、故障注入实调父断言含 rc/异常/缺字段）。
- 全量回归：18 套 × 4.26.0/4.28.0 各 **745** 项断言全 PASS（298+113+170+64），
  日志 local_evidence/x_logs/。
- Linear：MIS-165/166/167/169 完成→In Review；MIS-168 保持 In Review（W6/W7 通过，
  X3 合同变更已跑 n3 回归）；MIS-170 In Progress 等 Codex；MIS-145 实机不变。

---

## 前轮：V1/V2 五次返工（基线 06d5598）→ 0.6.0 候选完成（已关闭）

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


## 独立验收返工（2026-09-17，Codex 验收报告 04）

基线 8477eba 独立验收未通过（R1~R7）。返工内容（详见 docs/ADR.md ADR-003 v2 /
ADR-004 v2 / ADR-009 / ADR-010）：

- **R1**：main.py 改包内相对导入；p6 A17 改为宿主 `data.plugins.<name>.main`
  真实路径导入（不允许注入插件根目录）。
- **R2**：终态机 v2——on_agent_done 为主提交点（is_stopped 判 aborted）；decorating
  仅做 err 文本加速；after_message_sent 兜底；fail-watchdog（180s 可配）收尾无钩子
  路径。新增 tests/harness.py（真实 PipelineScheduler + ResultDecorateStage 逐
  yield 下游），r_rework R2 覆盖工具中间输出/取消/err 快速/err watchdog/空回复/卸载。
- **R3**：身份锁持有至轮次终态化；r_rework R3 屏障验证（B 等待 A 且请求含 A 问答；
  异身份并行计时）。
- **R4**：本轮 user 边界改 prompt 前缀匹配（extra parts 追加在 prompt 后）；临时
  内容入库剔除；completed 保底配对；下一轮请求含完整问答回归。
- **R5**：命令身份解析接入 conversation_manager 当前会话 persona_id（修复 getter
  未调用 bug）；真实 PersonaManager 验证命令与对话身份一致。
- **R6**：租约运行时 ID（删除持久化文件）+ owner_token 归属校验（heartbeat/release
  拒绝伪造）；双子进程排他验证。
- **R7**：原生 /reset、/new 联动实现（ADR-010）：同名单命令共存 + 权限镜像 +
  范围内 bump epoch；未共享原行为；A11 不再以 /uctx reset 冒充全通过。

测试统计（返工后）：8 套 × 4.26.0/4.28.0 各 **181 项断言全部 PASS**
（P0=17 P1=35 P2=27 P3=19 P4=22 P5=16 P6=8 R=37）。


## 二次验收返工（2026-09-17，Codex 二次验收报告 06）

基线 d8a7147 二次验收 S1~S6 返工完成（ADR-011）：

- **S1**：重复投递分支释放身份锁 + `event.stop_event()` 终止传播（宿主 internal 阶段在 is_stopped 后直接返回，防二次执行模型）；回归验证后继新窗口轮实际完成且含前轮问答。
- **S2**：停止判定取宿主 `_should_stop_agent` 同源并集（is_stopped / agent_stop_requested / agent_user_aborted）；真实 ConversationCommands.stop + active_event_registry 验证：终态 aborted、历史与输出干净、锁释放。
- **S3**：watchdog 先设 `agent_stop_requested`（宿主 /stop 同款信号）再落账释放——4.28 模型被取消/aborted 收尾、4.26 迟到 resp 被停止分支吞掉；两版均无迟到输出、后继轮干净。
- **S4**：删除「LLM 响应错误」正文前缀判失败（模型可生成任意开头正文）；err 统一 watchdog 收尾；诊断文本/真实 err/自定义文案三对照。
- **S5**：`_host_reset_would_run` 补 provider 与第三方执行器检查（4.26/4.28 字段兼容）；真实 reset 拒绝（无 provider）→ 不清空不发提示；成功 → 联动；权限/停用维持。
- **S6**：tests/s6_plugin_lifecycle_worker.py——真实 PluginManager 隔离子进程实例完整生命周期（load/默认关闭/配置 reload/真实钩子接续/挂起轮卸载/清理），两版通过。

测试统计（从日志加总）：9 套 × 4.26.0/4.28.0 各 **221 项断言全部 PASS**
（P0=17 P1=35 P2=27 P3=19 P4=22 P5=16 P6=8 R=37 S=40）。上轮 151 汇总误差已按更正口径（143）处理，不再沿用。


## 三次验收返工（2026-09-17，Codex 三次验收报告 08）

基线 169a88a 三次验收 T1~T6 返工完成（ADR-012；实现提交 5f662d0，ACCEPTANCE v4 补充提交 e0c95dd）：

- **T1**：resolve_persona_scope 增加 provider_settings 参数（4.26 默认人格隔离修复）；bridge/commands 经 getter 注入宿主 provider_settings；解析失败抛 PersonaResolutionError（对话轮受控跳过、命令报错，不折叠 __default__）；开场白同一 resolver 同一参数。真实 PersonaManager + ConversationManager + `_get_session_conv` 新会话路径两版验证（t1_persona_worker：不同默认人格身份隔离、B 不含 A 历史、B 保留自身开场白、显式会话人格跟随、失败受控）。
- **T2**：pending/watchdog 登记提前到锁后可等待点之前；CancelledError 分支收尾 interrupted + 停止传播 + 释放锁（t_rework T2：取消轮 0 模型调用、0 输出、事件终止、轮次 interrupted、锁释放、后继轮完成）。
- **T3**：shutdown() 关闭协议（_closing 入口双检、活动轮全套停止信号、释放锁唤醒排队者）；main.terminate 顺序 shutdown→release→close。S6 worker 扩展：真实 turn_off/turn_on + 挂起 A + 排队 B + uninstall（活动轮无迟到输出、排队轮干净让出且事件终止、恢复后新轮正常且无回灌）。
- **T4**：停止信号全套（agent_stop_requested + stop_event→scheduler yield 断链）+ decorating 对已终态轮 clear_result 双保险（t_rework T4：buffer=True 超时后无新增正文、无缓冲迟到输出、failed 落账）。
- **T5**：provider 检查按真实 Context 接口存在性（t5_native_worker 用真实 Context 构造，4.26 无 async 接口实测走同步；default-reset 两版均联动清空）。
- **T6**：删除同名单 /reset、/new 处理器；decorating 后置成功关联（activated_handlers 结构化证据 + 宿主固定成功文案 + 轻量防御）；真实 WakingCheckStage+StarRequestSubStage 命令分发矩阵两版验证（默认成功联动、无 provider/禁用/旧名/过滤拒绝不清、新名成功联动）；commands.py 文案修正与 README 一致。

测试统计（日志加总）：10 套 × 4.26.0/4.28.0 各 **258 项断言全部 PASS**
（P0=17 P1=36 P2=27 P3=19 P4=22 P5=16 P6=8 R=38 S=40 T=35）。


## 四次验收返工（2026-09-18，Codex 四次验收报告 10）

基线 0a915d3 四次验收 U1~U4 返工完成（ADR-013）：

- **U1**：`handle_llm_request` 三个可等待点全部纳入取消保护——初始人格解析取消（停止传播+re-raise，不触碰锁）；锁等待/获取（`lock_acquired` 标志界定归属）；后续点（T2 原路径）。宿主吞取消后事件在下一个 yield 检查点截断（0 模型 0 输出）。验证（t: U1.* 15 项，双版）：初始解析取消（stopped/0 模型/0 输出/无轮次/无锁泄漏）；等锁取消（B stopped/0 模型/0 输出，A 不受影响并正常完成，锁随后释放，后继请求完成）。
- **U2**：new/reset 分命令防御——new 只复核会话存在（宿主不要求 provider），无 provider 的 new 成功也联动（t5 worker `new-without-provider` 双版 2→0）；恢复后不回灌（`recovery_no_backfill_after_new`）。
- **U3**：结构化标记 `_clean_group_context_session`（宿主 builtin 本地成功末尾设置的布尔 extra，两版一致，权限拒绝/provider 缺失/第三方分支不设置）替代成功文案 startswith——前置文本装饰（`[NOTICE] ` 前缀）不再影响判定（t5 worker `reset-prefix-decorator` 双版联动 2→0）。
- **U4**：harness trace 初始化修复；取消场景 gather 结果纳入断言；T2 登记前取消收紧为完整断言；S6 worker 的 T3 生命周期关键字段进入父测试 4 组断言（`turn-off-stops-active`/`queued-clean-yield`/`recovery-no-backfill`/`uninstall` 增强），故障注入验证 10 个字段全 false 时 4 组全部按预期 FAIL。

测试统计（日志加总）：10 套 × 4.26.0/4.28.0 各 **290 项断言全部 PASS**
（P0=17 P1=36 P2=27 P3=19 P4=22 P5=16 P6=8 R=38 S=46 T=61）。
ACCEPTANCE 校准：handler 数 10、T2 8 项、T5/T6 每版 7 项（矩阵另行对齐）。


## 五次验收返工（2026-09-18，Codex 五次验收报告 12）

基线 06d5598 五次验收 V1/V2 返工完成（ADR-014）：

- **V1 一次性成功关联**：`_sync_native_reset_on_success` 判定成功后同步原子认领（event extra `_uctx_native_sync_applied`，任何 await 前设置）——同一事件的后续装饰（同事件多处理器各自回复时再次进入装饰阶段）直接跳过。失败转通过：基线复现探针（local_evidence/v1_baseline_probe.py，基线 main.py 双版）reset/new 两场景 notify=2；修复后 t5 worker follower 场景 epoch_delta=1、notify=1、新问答（屏障期间完成）在放行后保留、再下一轮可读（t: V1.* 4 项/版）。
- **V2a 真实恢复链**：worker `new-without-provider-then-recovery`（同命令事件同账本，无 provider 原生 new 联动清空 → 恢复 provider → 真实新轮，旧问答不回灌）；删除假 `_recovery_check`。
- **V2b 故障注入实调父测试**：fault_inject_check monkeypatch `_run_s6_worker` 在子进程实际运行 `s6_plugin_lifecycle`——正常 16 PASS、10 字段逐个 false 各触发（每字段 2 FAIL）、全 false 8 FAIL/8 PASS。
- **V2c**：U1 排队取消断言 ev→b 笔误修正。
- **V2d 文档口径**：T2=9（含 cancel-task-controlled）、U1=15（初始 6+排队 9）、A17 handler=10 单口径、删除"固定成功文案"过期描述（ADR-010 历史保留并标后继决策）。


## 六次验收文档收尾（2026-09-18，Codex 六次验收报告 15）

Codex 六次验收确认 V1 与 V2a/V2b/V2c 通过（两版各 298 PASS、ZIP 逐文件一致）；仅剩两处
上一轮已要求但遗漏的 ACCEPTANCE 数字口径，本提交为纯文档补齐（无运行代码/测试/包变化）：

- A10 证据列：`T2.*（取消窗口 8 项）` → `9 项`（与 A07 及真实日志一致）。
- A17 行为描述列：`load（绑定 12 handler）` → `绑定 10 个 handler`（与同一行证据列及真实
  PluginManager 一致）。

**对应关系口径**：实现与测试证据仍对应实现提交 `ac51bde`（0.6.0，两版各 298 PASS、
ZIP `astrbot_plugin_user_context_bridge-ac51bde.zip` SHA-256 `78e64560334d26bb1b29949967fdb6dea337c764da06a8148021f1aa9b6449af`、12 文件白名单）；
本次最终 HEAD 仅补文档（ACCEPTANCE/STATUS），未重新执行测试、未重打包——白名单 ZIP 不含
这两份文档，包无需重打、哈希不变。验收矩阵保持 v6。
