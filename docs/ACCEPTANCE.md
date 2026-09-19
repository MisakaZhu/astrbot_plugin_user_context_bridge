# 验收矩阵映射（A01-A18，五次返工后 v6）

对应 0.6.0 五次返工候选（V1/V2 返工后）。每行给出：场景 → 落点断言
（最终模型请求 / 实际持久化 / 回复目标）→ 测试文件与断言名 → 状态。R/S/T/U 系列映射
来自 Codex 独立验收报告 04/06/08/10。

测试运行（两版宿主各跑一遍，合成数据，假模型/假传输；r/s/t_rework_check 使用真实
PipelineScheduler + ResultDecorateStage 调度链；t1_persona_worker 用真实
PersonaManager/ConversationManager/宿主新会话路径；t5_native_worker 用真实
Context/WakingCheckStage/StarRequestSubStage/builtin 命令实例；s6 worker 用真实
PluginManager 生命周期）：

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check t_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 统计（日志逐项加总）：4.26.0 / 4.28.0 各 298 项断言全部 PASS
# （17+36+27+19+22+16+8+38+46+69）
```

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check \
         p3_bridge_flow_check p4_concurrency_check p5_commands_check \
         p6_packaging_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
```

| 编号 | 场景 | 落点证据 | 测试文件 / 断言 | 状态 |
| --- | --- | --- | --- | --- |
| A01 | 同一人：群 A → 群 B | B 的**最终模型请求**（provider.call_log contexts）含 A 的已完成问答、顺序正确、各出现一次；账本 completed 计数 | p3: A01.turn2-request-contains-turn1 / turn2-order / turn2-no-duplicate / ledger-completed-count；p2: A07.* | PASS（双版本） |
| A02 | 群 → 私聊 → 另一群 | 三跳+回程的模型请求上下文连续；**回复目标**仍为当前窗口 UMO | p3: A02.private-continues-group / round-trip-back-to-A / reply-target-unchanged | PASS（双版本） |
| A03 | 同一群中的两个人 | 另一用户的模型请求与账本均不含本人历史；各自独立身份账本 | p1: A03.same-group-two-users-isolated；p3: A03.other-user-isolated / other-user-own-ledger | PASS（双版本） |
| A04 | 不同机器人/平台实例/人格 | 同 QQ 号在 self_id/platform_id/persona_scope 任一维度变化时身份键不同；**真实 4.26 默认人格隔离（T1）**：不同默认人格/显式会话人格/解析失败受控不折叠、开场白同源 | p1: A04.*；t: T1.<venv/venv426>.distinct-persona-keys / b-excludes-a-history / b-keeps-own-begin-dialogs / explicit-conversation-persona / resolution-failure-controlled（真实 PersonaManager+ConversationManager+`_get_session_conv` 新会话路径，双版各 5 项） | PASS（双版本） |
| A05 | 未启用来源、关闭共享、退出 | 判定层不采集；范围外轮次宿主原生写回；命令 off/on 生效且作用于正确身份（R5）且个人不能扩大范围 | p1: A05.*；p3: OOS.*；p5: A05.cmd-* / personal-on-cannot-expand；r: R5.off-stops-selected-persona | PASS（双版本） |
| A06 | 非隔离群 UMO、同名用户、普通闲聊 | 归属按真实 sender（昵称无关）；未唤醒/机器人自言/管理命令/空 sender 排除 | p1: A06.*（nickname-irrelevant / group-umo-attributed-by-sender / bot-self / command / not-wake / empty-sender） | PASS（双版本） |
| A07 | 重复事件、重试、重复完成通知 | event_key 唯一约束：一轮一记录，终态不翻转；闭环层每轮一对 user/assistant；重复投递释放锁并终止事件传播（S1）；**锁后取消窗口收尾（T2）：取消轮 0 模型 0 输出、interrupted、锁释放、后继轮真实完成** | p2: A07.*；p3: A01.turn2-no-duplicate；s: S1.*（6 项）；t: T2.cancel-task-controlled / cancel-no-model / no-output / stops-event / turn-finalized / lock-released / next-request-completes / pre-registration-cancel-clean（9 项，含 gather 受控检查）+ U1.initial-resolve-controlled / stops-event / no-model / no-output / no-turn / no-lock-leak + U1.queued-cancel-controlled / stops-event / no-model / no-output / waiter-present / a-unaffected / only-a-turn / lock-released-after-a / next-request-completes（15 项） | PASS（双版本） |
| A08 | 跨窗口同时发消息 | 同身份锁覆盖至终态（R3）：后继轮等待前轮完成且请求含前轮问答（屏障验证）；不同身份并行（计时）；全部落账无覆盖 | r: R3.second-waits-for-first / second-sees-first-question-and-answer / different-identity-parallel；p4: A08.*（22 项栈） | PASS（双版本） |
| A09 | 多连接并发与同库双实例 | 4 连接并发 24 轮无丢失无覆盖；双实例防护（R6）：运行时租约身份 + owner_token 归属校验（伪造 token 的跨实例释放被拒）+ 真实双子进程第二实例被拒 | p2: A09.*；r: R6.runtime-ids-distinct / no-cross-release / lease-still-active / owner-release-ok / multiprocess-exclusive | PASS（双版本） |
| A10 | 模型报错、超时、取消、发送失败 | 真实调度链：工具中间输出不提前提交、err 由 watchdog 受控失败（S4）、真实 /stop 终态 aborted（S2）、watchdog 停止旧执行无迟到输出（S3）、空回复 failed、发送状态可区分、卸载 interrupted；**缓冲正文抑制（T4）：buffer=True 超时后无新增正文/无缓冲迟到输出** | s: S2.* / S3.* / S4.*；t: T4.no-new-output-after-timeout / no-buffered-late-output / failed-finalized（真实 run_agent + 公开 buffer 参数）+ T2.*（取消窗口 9 项）+ U1.*（全等待点取消 15 项）；r: R2.*；p4: A10.*；p2: A10.failed-aborted-not-in-history | PASS（双版本） |
| A11 | reset/new 期间慢请求（含原生命令联动） | 存储层 epoch 防复活 + bridge 慢请求吸收 + 命令 reset 仅本人；原生命令**后置成功关联（T5/T6 + V1 去重）**：宿主 builtin 实际执行成功（activated_handlers 结构化证据 + 宿主本地成功标记）才联动清空，**同一成功只应用一次（插件私有 applied 状态同步原子认领）**——同事件多处理器/多次装饰不重复清空，屏障期间完成的新问答保留——真实 Context 命令分发矩阵：默认成功联动、无 provider/内置禁用（reset+new）/改名旧名/自定义过滤拒绝不清空、改名新名成功联动、4.26 真实 Context（无 async provider 接口）兼容 | s: S5.*（真实宿主 reset 拒绝/成功/权限/停用）；t: T5.<venv/venv426>.real-context-reset-syncs / no-provider-no-clear + T6.<venv/venv426>.disabled-no-clear / disabled-new-no-clear / renamed-old-name-no-clear / renamed-new-name-syncs / filter-denied-no-clear（真实 Context/WakingCheckStage/StarRequestSubStage/ResultDecorateStage/PipelineScheduler，每版 7 项）+ V1.<版>.reset-once-only / reset-new-turn-survives / new-once-only / new-new-turn-survives（屏障验证，每版 4 项）+ U2.<版>.new-with-provider-syncs / new-without-provider-syncs / recovery-no-backfill + U3.<版>.prefix-decorator-still-syncs / clean-mark-structure（结构化标记 _clean_group_context_session，每版 5 项）；p2: A11.*；p4: A11.*；r: R7.* | PASS（双版本；实机补充） |
| A12 | 重启、重载、异常中断恢复 | 已提交历史保留；遗留 running → interrupted 不入历史；恢复幂等 | p2: A12.*；p4: A12.*（4 项） | PASS（双版本） |
| A13 | 长上下文裁剪 | max_turns 尾部保留最近 N 轮（不破坏轮次完整性） | p2: A13.trim-keeps-recent / full-untrimmed | PASS（双版本） |
| A14 | 图像/语音/引用与工具结果 | base64/本地路径不入库不入请求（[图片]/[音频] 占位）；真实两步工具轮的调用/结果配对入库且下一轮可续（R2/R4）；临时 extra 内容不入库（R4） | p2: A18.tool-structure-kept；p3: A14.*；r: R2.tool-trajectory-pairs / R4.transient-excluded / R4.next-request-contains-pair | PASS（双版本；语音/引用为宿主装配路径的间接验证，实机补充） |
| A15 | 多插件共存 | 其他插件 system_prompt 动态注入在接管后保留；人格/系统提示进入最终模型请求；开场白单次注入；宿主 mark_as_temp 临时注入不永久污染共享历史 | p5: A15.other-plugin-prompt-kept；p3: A15.*；r: R4.transient-excluded | PASS（双版本） |
| A16 | 停用/卸载与重新启用 | 停用后不采集不注入、原生行为恢复；重启用后停用期间消息不回灌；卸载/重载未决轮次 interrupted；**关闭协议（T3）：真实 turn_off/turn_on/reload/uninstall 下活动轮无迟到正文、排队轮干净让出且事件终止、恢复正常无回灌** | p5: A16.*；r: R2.terminate-interrupted；s: S6.<venv/venv426>.turn-off-stops-active / queued-clean-yield / recovery-no-backfill / uninstall（4 组生命周期断言，故障注入验证关键字段 false 时全部 FAIL——U4）；s6 worker 观测字段（真实 PluginManager turn_off/turn_on/uninstall + 挂起 A + 排队 B） | PASS（双版本；GUI 级停用/卸载待实机） |
| A17 | 插件打包安装 | **真实 PluginManager 隔离实例生命周期（S6+T3 本地完成）**——安装→load（绑定 10 个 handler）→默认关闭不采集→写启用配置 reload→真实钩子群A→群B 接续→**真实 turn_off（挂起 A+排队 B 关闭协议）→turn_on 恢复→uninstall**→注册表与目录清理；两版宿主子进程通过（4.26 需预建 data/config，已适配） | s: S6.<venv/venv426>.load（绑定 10 个 handler）/ default-off / reload-enabled / shared-turn / uninstall + 4 组生命周期断言（真实 PluginManager；故障注入实调父测试）；p6: A17.module-importable / metadata-valid / config-schema-valid / zip-manifest | PASS（双版本） |
| A18 | 发布准备与隐私 | Git 跟踪清单无 db/log/zip/venv/缓存/真实配置；ZIP 同样干净 | p6: A18.git-manifest-clean / zip-manifest-clean | PASS（双版本） |

## 未覆盖 / 待实机项（v4 口径）

- **A14 语音、引用消息**：宿主装配路径（Record/Reply → 附件占位）由 P0 源码核对与请求装配共用路径间接覆盖；真实 QQ 语音/引用端到端待 MIS-145 实机验证。
- **A11 原生联动的实机确认**：与宿主 builtin /reset 在真实命令分发中的共存顺序（多 handler 依次执行）已在组件级用真实 ConversationCommands.reset 验证拒绝/成功/权限分支；实机演练确认最终用户体验与命令改名/过滤组合。
- **A16 GUI 级停用/卸载**（WebUI 开关、插件管理页卸载）与 **A17 独立实例 GUI 安装**：本地已验证宿主式包路径导入、元数据/配置经宿主真实校验器、包清单干净；完整 GUI 流程待 MIS-145 用户实机（独立离线实例）执行。
- **真实 QQ 演练**（跨群/私聊接续、重启、reset、停用恢复）：按主计划单列「待用户执行」，合成测试不冒充实机通过。
- **fail-watchdog 默认 180s**：无钩子的模型 err 轮最长 180s 后落 failed（不影响历史读取，其只认 completed）；可经构造参数调整，实机可观察是否需缩短。

---

# 0.7.0 验收矩阵（N01-N24，跨人格共享与本地记录候选）

对应分支 `feat/0.7.0-persona-records`。测试运行（两版宿主各一遍；n 系列基于真实
TurnLedger/真实调度链/S6 用真实 PluginManager；n3 经子进程驱动真实 CLI）：

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check        p3_bridge_flow_check p4_concurrency_check p5_commands_check        p6_packaging_check r_rework_check s_rework_check t_rework_check        n0_baseline_check n1_scope_migration_check n2_cross_persona_check        n3_records_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 统计（日志逐项加总）：4.26.0 / 4.28.0 各 381 项断言全部 PASS
# （旧 10 套 298：17+36+27+19+22+16+8+38+46+69；新 4 套 83：9+13+11+50）
```

N4 开发中发现并修复的回归：N2 在 main.py 传入 CommandService 不存在的
`stats_getter` 参数，真实 PluginManager 加载即 TypeError（实例被丢弃且不释放
租约，呈现为 S6 load 失败与"另一实例租约"告警链）；n 系列测试不经
main.initialize() 未覆盖该路径，由 N4 全量回归的 S6 暴露。修复后 S6 双版
46/46（load 绑定 10 handler、群A→群B 接续、生命周期闭环）。该缺口以"N4 必须
全量回归全绿"流程封堵。

| 编号 | 场景 | 落点证据 | 测试 / 断言 | 状态 |
| --- | --- | --- | --- | --- |
| N01 | 默认升级（保持 persona） | v1 旧库自动迁移，旧问答/epoch/退出不变，source_persona 自旧键还原，不重复导入 | n1: N01.migrate-runs / migrate-idempotent / backup-created / no-membership-change / source-persona-restored | PASS（双版本） |
| N02 | 跨人格接续 | user 模式群A黑→群B白→私聊→群A，最终请求含前序完整问答各一次、无重复 | n2: N02.chain-all-present / no-duplication | PASS（双版本；真实 QQ 演练待 MIS-145/N24） |
| N03 | persona 模式原人格隔离 | 升级后 persona 键行为不变；真实默认/显式人格隔离继承 0.6.0 证据 | n0: N0.persona-baseline-isolated；t: T1.*（真实 PersonaManager 链） | PASS（双版本） |
| N04 | user 模式当前人格规则 | 每轮 system_prompt 为当前窗口人格；开场白单次注入；不持久化旧系统规则 | n2: N04.current-persona-rules / system-prompt-current / begin-dialog-once | PASS（双版本） |
| N05 | 身份与路由 | 跨用户/机器人/平台隔离与回复目标继承 0.6.0（A03/A04/A02）；user 键四轮累积正确 | p1/p3: A01-A06；n2: N09.user-key-four-turns | PASS（双版本） |
| N06 | 来源与退出 | 范围外/退出不采集；user 模式 off 作用于全部人格，on 不扩范围 | p1: A05/A06；r: R5.*；n2: N07/N06 场景（user 键 optout 阻断接管） | PASS（双版本） |
| N07 | 退出转换 | persona off→user 仍退出；user on 解除；base_protected/persona_on 布局见 ADR-015 | n1: N07.persona-optout-inherits-to-user / user-on-unblocks | PASS（双版本） |
| N08 | 模式往返 | 切模式代次 +1 从空历史开始；reload 同配置不清空 | n1: N08.reload-no-reset（代次归档语义）；n3: N19/16 归档读取 | PASS（双版本；完整往返实机观察待 N24） |
| N09 | 跨人格并发 | 同基础身份跨人格互斥至终态、后轮见前轮、无锁泄漏 | n2: N09.cross-persona-mutex-serial / no-lock-leak | PASS（双版本） |
| N10 | reset/new 双模式清空范围 | user reset 清跨人格整份（3 人格 4 轮全清），persona reset 只清当前人格；原生命令联动继承 V1/T5/T6 | n2: N10.pre-reset-two / user-reset-clears-all / persona-reset-scope；t: T5/T6/V1/U2/U3 | PASS（双版本） |
| N11 | 切换与取消 | 活动轮+排队轮取消/关闭协议继承 0.6.0（S6/T2/T3/U1），N2 装配修复后 S6 全绿 | s: S6.*（46 项含生命周期）；t: T2/T3 | PASS（双版本） |
| N12 | 迁移与恢复 | 损坏输入拒绝不写；迁移中途失败原库可读可重试；重复升级幂等 | n1: N12.corrupt-input-rejected / failure-atomic / N01.migrate-idempotent | PASS（双版本） |
| N13 | 状态诊断 | /uctx status 显示模式与有效完成轮数；命令身份按 history_scope 解析（user 模式 __mode_user__） | n2: N04/N13 场景（命令接线）；p5: A05.cmd-* | PASS（双版本） |
| N14 | 只读入口 | mode=ro、单读事务快照、无写路径/租约/handler；list 不输出正文；三条 CLI rc=0 | n3: N14.list-*（7 项）；local_evidence/n3_html/evidence.json | PASS（双版本） |
| N15 | 过滤与隔离 | sender/platform/self/persona/时间边界（起含终不含）/状态白名单/空结果/未知身份报错 | n3: N15.*（9 项） | PASS（双版本） |
| N16 | 归档与异常 | 默认仅当前代次 completed；--archives 标注 is_current_generation；failed/aborted/interrupted/running 显式选择 | n3: N19.archives-included / record-archived-flag / status-failed / status-running | PASS（双版本） |
| N17 | 一致性快照 | meta+turns 同一 BEGIN 读事务；导出不写源库；ledger 事务与唯一约束继承 A09 | n3: N14/N19（快照内读取）；p2: A09.* | PASS（双版本） |
| N18 | HTML | Playwright(Chromium) 真实浏览器渲染：中文/emoji/长文本/工具折叠/徽章；搜索交互（white→1/5、reset→1/5）；控制台 0 错误；注入转义无脚本执行、无外链 | n3: N18.*（8 项）；截图 local_evidence/n3_html/render-full.png / render-filtered.png | PASS（双版本；Codex 视觉复核待 MIS-170） |
| N19 | JSON | 信封 schema_version/exported_at/filters/total_matched/truncated；记录字段与库逐项吻合（身份解码/源人格/代次/工具配对/send_state/时间 ISO）；截断 limit=1→total 3 返回 1 | n3: N19.*（13 项） | PASS（双版本） |
| N20 | 文件与限额 | 拒绝覆盖源库/WAL；坏父路径 rc=2 无半文件；OSError 统一 rc=2；v1 旧库拒绝并提示迁移；截断明示 | n3: N20.*（6 项）/ N19.truncation | PASS（双版本） |
| N21 | 数据边界 | 工具不读配置/不连网络；导出仅含 turns 已 sanitize 字段；Git/ZIP 不含 exports/备份/库（A18 继承） | n3: N14.list-no-body / N18.no-external；p6: A18.* | PASS（双版本） |
| N22 | ZIP 独立使用 | 真实 PluginManager load/reload/turn_off/turn_on/uninstall（S6，13 文件白名单含 tools/）；安装探针解包验证工具独立可运行 | s: S6.*；p6: A17.*；local_evidence install_probe（N4） | PASS（双版本） |
| N23 | 测试可信度 | n3 子进程真实 CLI 断言 rc/文件内容；S6 观测字段故障注入继承（U4/V2b）；gather/subprocess 异常必 FAIL 继承 | n3 全套；s: S6 故障注入；local_evidence/fault_inject_check.py | PASS（双版本） |
| N24 | 朋友演练 | 同 QQ 黑→白→私聊双向接续与当前人格；本地查看/JSON、reset 归档、重启与 GUI | —— | **待实机（MIS-145 扩展）** |

## 0.7.0 待实机 / 待复核项

- N24 朋友演练（跨人格实机、本地工具实操、GUI）：待 MIS-145 扩展执行，本地结果不替代。
- N18 视觉复核：截图与 DOM 检查为本地 Playwright 产物，Codex 视觉复核待 MIS-170。
- 迁移实库演练：仅合成库演练；朋友环境真实库升级由用户在备份前提下执行。


---

# 0.7.0 W 返工后验收矩阵（N01–N24 v2，2026-09-19）

**上轮口径更正（重要）**：首个候选 219cef2 的 N01–N23 "全 PASS" 结论不被独立验收支持——
N08（代次推进靠 resolver 属性切换，未走真实 PluginManager/restart，未检查最初 persona 记录）、
N07（缺 user off→persona 及未来人格证据）、N12（`has_data >= 0` 空断言，未校验结构与可重试）、
N13（矩阵声称显示模式/有效数但实现仍旧 status）、N22（把源码复制夹具描述为"13 文件 ZIP 生命周期"）、
N09（放行 A 后仅查完成数，未证明 B 阻塞期间未进入模型）均属证据缺口或行为缺陷。本轮全部修正，
不再沿用"计数全绿因此完成"的结论。

测试基座（两版各一遍；总数从完整日志加总，不预设）：

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check        p3_bridge_flow_check p4_concurrency_check p5_commands_check        p6_packaging_check r_rework_check s_rework_check t_rework_check        n0_baseline_check n1_scope_migration_check n2_cross_persona_check        n3_records_check w_rework_check w_lifecycle_check w_zip_lifecycle_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe -X utf8 tests/$t.py
done
# 统计：4.26.0 / 4.28.0 各 561 项断言全 PASS
# （旧 10 套 298：17+36+27+19+22+16+8+38+46+69；
#   n 系 4 套 113：10+30+14+59；w 系 3 套 150：34+57+59）
```

W 返工中发现并修复的新缺陷：`identity_stats` 以 4 段身份键查询 mode_generation（实际存于
3 段基础键），导致 status 有效数把归档轮计入当前——由 w_lifecycle_worker 真实宿主证据暴露。

| 编号 | 场景 | 落点证据 | 状态 |
| --- | --- | --- | --- |
| N01 | 默认升级（保持 persona） | 真实 859f18e 旧库迁移：行数/键改写 p:/source_persona/epoch 逐项对账；旧历史当前可读；幂等 | PASS（w3/n1，双版） |
| N02 | user 模式跨人格接续 | 群A黑→群B白→私聊→群A，最终请求含前序问答各一次（n2 真实调度链） | PASS（双版；实机待 N24） |
| N03 | persona 模式原人格隔离 | 升级后 persona 键 p:<id> 行为不变（n0 基线 + T1 真实 PersonaManager） | PASS（双版） |
| N04 | user 模式当前人格规则 | n2 真实调度链：每轮 system_prompt=当前窗口人格、开场白单次、不持久化旧规则（T1 真实 PersonaManager 证据保留于 t 套件） | PASS（双版） |
| N05 | 身份与路由 | v3 编码下跨用户/机器人/平台隔离（p1/p3）+ W4 键不碰撞（真实 PersonaManager 创建 __mode_user__ 人格，探针证据 + w4 结构断言） | PASS（双版） |
| N06 | 来源与退出 | 范围外/退出不采集；user off→写 user 键退出（转换），on 不扩范围（p1/r5/w2） | PASS（双版） |
| N07 | 退出转换（完整） | 真实转换函数接入：persona off→user 保持退出（运行时+status 双证据）；user off→persona 基础保护覆盖现有人格与未来人格；单人格 on 只解除该人格；显式 off 不被静默清除 | PASS（n1/w_lifecycle 双版真实命令） |
| N08 | 模式往返（真实宿主） | 真实 PluginManager 四阶段 reload：代次 [0,0,1,2,3] 持久推进；切回 persona 最初 P/S 问答不复活；再切 user BP 问答不复活；同模式 reload 不清空（w_lifecycle_worker，双版） | PASS（双版） |
| N09 | 跨人格并发 | 屏障证明 A 挂起时 B 未进入模型；释放后 B 最终请求含 A 一次完整问答且仅一次；gather 结果/异常入结论（n2 收紧版） | PASS（双版） |
| N10 | reset/new 双模式清空范围 | n2（persona=当前人格、user=跨人格整份）+ t5/t6/V1 真实命令分发成功一次联动与拒绝分支 | PASS（双版） |
| N11 | 切换与取消 | w_lifecycle Phase 7：配置变化时挂起 A（interrupted、无迟到输出）+ 排队 B（真实锁等待 1、stopped、无输出）；S6/T2/T3 继承 | PASS（双版） |
| N12 | 迁移与恢复（收紧） | 触发器注入失败→回滚后逐项核验（无新列、无 schema_version、行内容原样），去障重试完整完成（键/回填/版本到位）；损坏输入拒绝；备份不覆盖既有（同目录两份共存） | PASS（n1/w3，双版） |
| N13 | 状态诊断（实测） | w_lifecycle 真实宿主：fresh status="尚无记录（开关开启不等于已实际接管）"；接管后显示"实际接管：最近…/1 轮已完成"；继承退出显示"已退出（…继承）"；reset 文案按模式标明范围；与真实采集/有效数一致 | PASS（双版） |
| N14 | 只读入口 | mode=ro+单读事务；list 不含正文；无写路径/租约（n3） | PASS（双版） |
| N15 | 过滤与消歧 | 身份三维/唯一推断/歧义候选拒绝/未知身份报错/时间边界/状态白名单/空结果（n3 真实 CLI） | PASS（双版） |
| N16 | 归档与异常 | 默认仅当前 epoch+代次 completed；--archives 标注；failed/aborted/interrupted/running 显式选择（n3） | PASS（双版） |
| N17 | 一致性快照 | meta+turns 同读事务；w3：持读快照期间已提交 WAL 行必入备份与后续读取；ledger 事务约束继承 | PASS（双版） |
| N18 | HTML（新产物重渲染） | W6/W7 变更后重新生成样例并 Playwright 重渲染：身份行"三维明确"、搜索 white→1/5、0 控制台错误、注入转义、无外链（w-render-full.png / w-render-filtered.png） | PASS（双版产物；Codex 视觉复核待 MIS-170） |
| N19 | JSON | 信封/记录字段/工具配对/send_state/截断（total 7 返回 2）/resolved_identity 推断标注（n3） | PASS（双版） |
| N20 | 文件与限额（收紧） | 真实 CLI：拒绝覆盖源库/WAL、**backups/ 整树（大小写/相对/等价路径）且备份字节不变**、backups 目录本身、exports 合法输出保留、坏父路径无半文件、v1 拒绝（n3） | PASS（双版） |
| N21 | 数据边界 | 工具不读配置/不连网；导出仅 turns 已 sanitize 字段；Git/ZIP 无 exports/备份/库（p6 A18） | PASS（双版） |
| N22 | ZIP 独立使用（真实包） | 工作区打包 13 文件白名单 ZIP → **双版解包安装**跑真实 PluginManager 完整生命周期（load/reload/turn_off/turn_on/uninstall，w_zip_lifecycle 59 断言）+ ZIP 内工具独立运行（显式断言包含 tools/） | PASS（双版） |
| N23 | 测试可信度 | w_lifecycle 父断言被故障注入实调：12 关键字段逐个翻假全部判 FAIL（非复制断言）；n3 子进程真实 CLI；S6 故障注入继承 | PASS（双版） |
| N24 | 朋友演练 | 跨人格实机、本地工具实操、GUI | **待实机（MIS-145）** |

## W 返工后待实机 / 待复核项

- N24 朋友演练（含 user 模式切换演示、本地工具实操）：待 MIS-145。
- N18 视觉复核：本轮新样例截图待 Codex 复核（上轮通过结论不自动继承）。
- 迁移实库演练：仅合成/真实实现构造的旧库；朋友真实库升级前先备份。
