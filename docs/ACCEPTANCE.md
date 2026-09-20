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


---

# 0.7.0 Z1–Z3 返工后矩阵（N01–N24 v5，2026-09-20）

**对 v4 的更正**：Codex 对 ebf0144 的独立复验（报告 29 号）确认——
w_zip 安装链与 T1/T5 worker 入口只看 RESULT 行（rc=19 仍 69/98 PASS）；
W 的 `switch_queued_lock_waiters_after` 已输出但父断言未使用（翻成 17
仍 32 PASS）；命令保存/登记缺失败协议（真实 SQLite 语句失败原样抛
IntegrityError、失败 on 解除退出、登记失败后直接切模式 0→0）；N04 工具
仍是手填 list 且只查 req 对象（真实边界丢弃工具后模型三次 None 仍宣称
保留）；N10 user follower 命令与新轮同为 maid；"所有入口拒绝 rc""工具
到最终模型""另一人格 follower"此前声称超出证据。全量回归与 Y 轮 814
计数本身准确，予以保留。

### AB 轮增补（2026-09-20，留证与负例判定；实现/包仍为 acabbf7）

AB1（功能失败留证）与 AB2（负例判定收紧）均为测试代码变更，产品代码
（uctx_bridge/）零改动。全部 20 套 × 双版各 **882** PASS / 0 FAIL
（40 次 rc=0；t 152 + w 92 + aa1 5，其余分套同 AA 表）。

| 项目 | 变更 | 证据 | 状态 |
| --- | --- | --- | --- |
| AB1 留证 | `worker_result.persist_run` 每次 worker 运行都保存原始 stdout/stderr/rc 与解析 JSON（唯一命名不覆盖）；三个 assert 函数失败 detail 自动附留证 JSON 路径；AB1 验证（W 套件 ab1_* 断言 + T 套件 AB1.t-semantic-*）覆盖 正常对照 / 功能字段翻假 / 必要字段缺失 / rc19 不倒退 | PASS（双版） |
| AB2 负例判定 | `run_aa2_negative` 正式判定要求 detected + target_hit（仅 N04.* 前缀失败）+ no_collateral（无无关 T5/入口失败）+ diag_ok（双版 `_validate_n04_negative_diag` 每窗有意义校验）；正常对照通过；两种真实负例（stop / provider-raise） accepted；四种坏观测（rc19 合法负例 / 空数组诊断 / 缺一版本 / N04 正常但无关 T5 失败）反向检验均 rejected | PASS（双版） |
| AB2 反向 | Codex 四种坏观测在新判定下全部 rejected（local_evidence/ab_logs/ab2_bad*.json），真实负例对照 accepted（ab2_stop-good / ab2_provider-raise-good） | PASS（双版） |

### AA 轮增补（2026-09-20，测试稳定性与留证；实现/包仍为 acabbf7）

夹具缺陷与修复（AA1）：

- `tests/fakes.py` 的消息 ID 原为 `id(abm)`（对象地址，释放后可复用）。
  Codex 探针 1,000 条仅 10 个唯一键（`fake_id_426/428.json`）。修复后
  新消息使用进程内单调序号，显式 `message_id=` 覆盖保留给真正的重复
  投递用例；S1 重复投递用例本就以同一事件对象复用原 ID，语义未变。
- 新增 `tests/aa1_event_key_check.py`：修复前双版基线同窗 199/200、
  跨窗 197/200 重复（`local_evidence/aa_logs/aa1_baseline_426|428_oldfakes.log`）；
  修复后双版 5/5 PASS（`aa1_fixed_426|428.log`）。
- W 单人格 on 场景（Phase 6）新增 RA/RF 分项诊断：命令返回、各自
  done/captured/model_calls/stopped、事件键、同 event_key 此前终态。
- 因果边界保持诚实：本次全量 W 失败的原始 RA/RF 分项未留存，**原
  失败的确切原因未能证实**；本轮证实并消除的是夹具 ID 复用缺陷本身。

诊断与留证（AA2）：

- `t1_persona_worker` 的 `_model_view` 空调用不再二次 KeyError；每窗
  机器可读诊断 `n04_window_diagnostics`（model_calls/hook_stopped/
  event_stopped/outcome/pipeline_error/final_text_head），请求钩子真实
  异常记 `hook_error`（完整栈）。受控停止如实记 `hook-stopped`，不虚构
  pipeline_error；无调用仍由 `all-model-called` 等正式断言判 FAIL。
- 负例经正式父入口验证（`aa2_n04_diag_observer.py`）：真实请求钩子
  停止 USER-MODE-2/3（model_calls [1,0,0]）与 provider 真实抛错两种
  负例均被正式 t1_t5_t6_workers 判 FAIL（AA2.*-negative-detected），
  诊断字段完整（AA2.*-diagnostics-preserved），完整输出留存
  `local_evidence/aa_logs/aa2_<mode>_<tag>.log/.json`。
- 五个 worker 入口失败时自动留证 stdout/stderr/解析 JSON 到
  `local_evidence/worker_failures/`（路径并入 __error__）。

稳定性与全量（实现/包仍为 acabbf7，ZIP 原字节保持，SHA 复核一致）：

- 固定次数稳定性（事先声明，全结果保留于
  `local_evidence/aa_logs/stability/`）：T1 worker 与 W 套件各双版 3 次
  独立目录运行，**12/12 全部正常**（T1 每次 rc=0、n04_model_calls
  [1,1,1]、RESULT 行在；W 每次 78 PASS / 0 FAIL）。
- 正式全量：**20 套 × 双版各 863 PASS / 0 FAIL**（40 次 rc=0；新增
  aa1_event_key_check 5 项/版；w_lifecycle 双版 78/78——上一轮 426 的
  W 失败未再现；分套其余同 v5 表：p0–p6 17/36/27/19/22/16/8 +
  r/s/t 38/84/143 + n0–n3 10/30/14/59 + w_rework/w_zip 37/78 +
  x 64 + y2 74）。N22 以显式路径+SHA 指定 acabbf7 包复跑
  （s 84/0、w_zip 78/0）。

全量回归（实现提交 **acabbf7**；候选包
`astrbot_plugin_user_context_bridge-acabbf7.zip`，SHA-256
`c21956febfda41e3baeb0b611734c9b907ea060fc8583aeb1ad3491e18aad728`；
日志 local_evidence/y_logs/，逐套 rc/PASS/FAIL 见
regression_summary.json）：**19 套 × 4.26.0/4.28.0，每版 854 项
断言全 PASS（0 FAIL，38 次运行 rc 全 0）**。分套：p0–p6
17/36/27/19/22/16/8 + r/s/t 38/84/143 +
n0–n3 10/30/14/59 + w_lifecycle/w_rework/w_zip
78/37/78 + x 64 + y2 74。计数口径同 v4（父套件内部跨
双版 spawn worker，同一条 PASS 只计一次；故障注入用例注入的是 doctored
子进程结果，其 PASS/FAIL 已回滚不入总数，检测结论由 Z1./Z3. 前缀检查
行承载）。

v4 表逐行状态以本轮证据继续成立，以下仅列 Z 轮实质变化行：

| 编号 | Z 轮变化 | 本轮证据 | 状态 |
| --- | --- | --- | --- |
| N22 | ZIP 安装链入口收敛到 tests/worker_result.py（rc/缺行/坏 JSON/JSON 非对象/超时/启动失败）；正式入口注入经 patch worker_result.safe_run 后调真实安装链函数：正常对照过，rc19/缺行/坏 JSON/数组/缺字段逐个 FAIL（w_zip 78→含 Z1 行） | PASS（双版） |
| N23 | 故障注入扩展到 T1/T5 正式入口（t1_t5_t6_workers + patch safe_run，基线取同进程先前真实运行结果）；N04 新增工具丢弃负例（真实 Runner._func_tool_for_provider 边界置 None，正式父断言 FAIL）；Z1b 锁/挂起清理（旧实例+新实例）翻成 17 必 FAIL | PASS（双版） |
| N02/N07 | Z2 失败协议：登记先行；真实 TEMP TRIGGER 语句失败 → 受控文案+整体未生效（退出未保存、scope=None）→ 去障重试登记成功；第二连接 BEGIN IMMEDIATE 持锁 → 失败 on 受控且退出保留（磁盘/内存/captured=0）→ 去障重试成功（captured=1）；登记失败后**直接**切另一模式无 ghost（scope=None/gen=0）→ 重试登记当前模式 → 再切恰好 +1（y2 套件 Z2.* 断言，双版） | PASS（双版） |
| N04 | 工具经真实 FunctionTool/ToolSet+宿主人格选择链装配；终模型实参断言 类型=ToolSet、A/B/C 各自专属工具、无跨人格串入、OpenAI schema 可序列化；动态注入挂真实 OnLLMRequestEvent 钩子（抵达模型、不落账本）；负例=真实边界丢工具 → 父断言 FAIL | PASS（双版） |
| N10 | user follower 双人格：命令窗口真实解析 maid、follower 新轮解析 second（账本 source_persona 证据）、同平台/机器人/发送者、同一 u: 键；once-only（epoch+1、提示 1 次、新问答不被二次清掉）与权限/禁用/改名/无 provider/范围外矩阵保留 | PASS（双版） |
| 回滚 | y4_rollback_verify"直接换旧代码"段改在含旧记录的迁移库副本上验证：裸键 0 条/编码键 1 条/写入后 2 条并存，verified_silent_key_split 双版 true | PASS（双版） |

---

# 0.7.0 Y1–Y4 返工后矩阵（N01–N24 v4，2026-09-20；历史记录，其中"工具到最终模型/另一人格 follower/全部入口拒绝 rc"的表述已被 v5 更正）

**对 v3 矩阵与 X 轮统计的更正**：Codex 对 4d73c7a 的独立复验（报告 27 号）
确认——S6 卸载等待吞异常且父断言 8 PASS、`_run_s6_worker` 不检查 rc
（rc=19+合法 JSON 仍 8 PASS）、W 排队 KeyError 以子串匹配放宽分类且
未留完整栈、N22 按 mtime 选包且缺 `.sha256` 记 PASS；仅执行命令的新
身份不登记模式事实（首次切换两方向漏推进）；N04 user 场景 0 模型调用
（watchdog failed/interrupted 而布尔全 true）；N10 缺权限拒绝/禁用/
改名新名成功/无 provider reset/范围外与 user follower 屏障；X 轮
"745 项"统计不成立（实际每版 708=348+113+183+64，"298+113+170+64"
算式本身也不成立）。

全量回归（实现提交 1efc0d2；候选包
`astrbot_plugin_user_context_bridge-1efc0d2.zip`，SHA-256
`9f6299eab0bab7b6fb6547418880d71ede1597b166c64a7f4a03c2d64842ec57`；
日志 local_evidence/y_logs/，逐套 rc/PASS/FAIL 见
regression_summary.json）：**19 套 × 4.26.0/4.28.0，每版 814 项断言
全 PASS（0 FAIL，38 次运行 rc 全 0）**。分套：p0–p6 17/36/27/19/22/16/8
+ r/s/t 38/84/125 + n0–n3 10/30/14/59 + w_lifecycle/w_rework/w_zip
75/37/69 + x 64 + y2 64。计数口径：每套父运行日志行，w_lifecycle、
w_zip、s6、y2 父套件内部各自跨双版 spawn worker，同一条 PASS 只计
一次；S 套 84 条含 ZIP 生命周期、故障注入与 S1–S5 原生链，不全部
归为 ZIP 断言。

v3 表逐行状态以本轮证据继续成立，以下仅列 Y 轮实质变化行：

| 编号 | Y 轮变化 | 本轮证据 | 状态 |
| --- | --- | --- | --- |
| N04 | 真实链补全：群A→群B→私聊三窗走 注册请求钩子→ToolLoopAgentRunner/假模型实际调用→真实 on_agent_done 终态提交；每窗恰好 1 次模型调用、账本 3 行全部 completed、0 watchdog、0 pending；后继窗口终模型实参=当前人格 system+当前开场白恰好一次+工具+动态注入，不含旧人格 system/开场白，含前一窗完整问答；动态临时内容不落账本（t1 N04 段重写+父套件新断言 11 项/版） | PASS（双版） |
| N10 | 两模式矩阵补齐：新增 权限拒绝（真实宿主群聊 /reset 默认需 admin，非 admin 发送=宿主拒绝且插件不联动）、禁用（reset/new）、改名新名成功（user）、无 provider reset 拒绝（user）、范围外（双模式：宿主命令成功但插件不 bump 不提示）、user follower 屏障（once-only：epoch_delta=1、提示 1 次、挂起期间同账号另一人格新问答不被二次装饰清掉）；follower 观察键改随命令事件身份（user 模式查 u: 键，断言 observer_key_scope="u:"） | PASS（双版） |
| N22 | 正式入口改为显式 --delivered-zip/--delivered-sha256（s6_delivered_zip_lifecycle 与 w_zip_lifecycle_check 同口径）：缺哈希记录/哈希不匹配/输入包缺失必须 FAIL，不按 mtime 选包；校验器自检用临时副本验证三种坏输入均被拒；本轮以 1efc0d2 包显式指定跑通完整链（含活动/排队/卸载与工具独立运行） | PASS（双版） |
| N23 | 故障注入作用于真实路径：tests/y_fault_observer.py 在真实 worker 进程的真实 wait_for 等待边界注入（S6 活动/排队/卸载、W 活动/排队；RuntimeError/TimeoutError/无关 KeyError），输出经真实 _run_*_worker 解析、真实 assert_*_fields 判定，正常对照必须过、逐个故障必须 FAIL；worker 入口 rc=19/缺 RESULT 行/损坏 JSON/缺字段全部检出；卸载任务结果显式归类（returned/cancelled/failed:*），异常保留完整栈 | PASS（双版） |
| N08 | 模式事实登记扩展到命令入口（Y2）：y2_command_first_use_check（新套件，真实 PluginManager+CommandService，双版 64 断言/版）——v1 旧退出升级登记不推进；新身份仅 off/on（零对话）登记当前模式，直接切另一模式两方向恰好 +1；重复命令幂等；登记边界失败→受控文案+退出不失+重载对账补登记+重试不多推进；已生效身份切换恰好一次、同模式重载不动 | PASS（双版） |

---

# 0.7.0 X1–X6 返工后矩阵（N01–N24 v3，2026-09-19；历史记录）

> Y 轮更正：本节"每版 745 项断言全 PASS（298+113+170+64）"与实际日志
> 不符——X 轮实际每版 708（348+113+183+64），且 N04/N10/N22 的
> "真实链/完整生命周期"表述超出当时
> 证据（见 v4 节更正明细）。表中其余行为 X 轮真实通过项。X 轮实际
> 分套：旧 10 套 348 + n 系 113 + w 系 183 + x 系 64 = 708/版。

**对 v2 矩阵的更正**：W 轮的 N01/N07/N08/N22 证据随后被独立复验否定——首次直接
切换漏登记/漏推进（X1）、旧 persona_on 越过新 user off（X2）、迁移按前缀猜编
码（X3）、N22 实为 reload 子集而非完整生命周期、N04/N10 仍非真实链、部分任务
异常未进判定（X6）。本轮逐项修复并以下表为准；v2 表保留作历史。

全量回归（实现 SHA 见提交链；两版各一遍，日志 local_evidence/x_logs/）：
18 套 × 4.26.0/4.28.0；~~每版 745 项断言全 PASS~~（**Y 轮更正：745 不
成立，实际每版 708** = 348 + 113 + 183 + 64；w_lifecycle/w_zip/s6 父
套件内部各自跨双版 spawn worker，不重复计入总数）。

| 编号 | 场景 | 本轮证据 | 状态 |
| --- | --- | --- | --- |
| N01 | 默认升级 | 真实 859f18e 旧库迁移对账（w3/x3：键/source_persona/epoch/幂等/WAL 备份/不覆盖）；v1 升级登记不改代次（x1） | PASS（双版） |
| N02 | user 跨人格接续 | n2 真实调度链 + t1 N04 真实 PersonaManager/ConversationManager 双窗共享 | PASS（双版；实机待 N24） |
| N03 | persona 隔离 | n0 + T1 真实 PersonaManager（p:persona_a/b 键隔离） | PASS（双版） |
| N04 | user 当前人格规则（真实链） | t1 N04：真实 PersonaManager/ConversationManager/宿主 `_ensure_persona_and_skills` 装配；每轮 system=当前窗口人格、开场白单次、工具/动态注入保留、u: 单键、source_persona=persona_a/persona_b、跨人格链 | PASS（双版） |
| N05 | 身份与路由 | p1/p3 + 特殊人格名键不碰撞（真实 PersonaManager 接受 `__mode_user__`/`u:`/`p:maid` 等） | PASS（双版） |
| N06 | 来源与退出 | p1/r5/w2 | PASS（双版） |
| N07 | 退出转换 | n1 真实转换函数 + w_lifecycle 退出矩阵（persona off→user 保持；user off→persona 保护现有+未来人格；单人格 on 仅解除该人格） | PASS（双版） |
| N08 | 模式往返（真实宿主） | w_lifecycle 无预热直切：首轮登记 persona→直切 user 恰好 +1→persona +1→user +1；旧模式/更早同模式内容均不回灌；归档行可查 | PASS（双版） |
| N09 | 跨人格并发 | n2 屏障（B 未进模型、最终请求含 A 一次完整问答、gather 异常入结论） | PASS（双版） |
| N10 | reset/new 双模式真实分发 | t5 双模式真实 Context/命令分发矩阵：user 模式 reset 联动、new 无 provider 仍联动、改名/过滤拒绝不清空；persona 模式继承保护下 new 不联动不误提示（X5） | PASS（双版） |
| N11 | 切换与取消 | w_lifecycle Phase 7（挂起 interrupted/排队受控让出）+ S6 生命周期（turn_off/turn_on/uninstall，含交付 ZIP 版） | PASS（双版） |
| N12 | 迁移与恢复 | n1 触发器故障→逐项回滚核验→重试完整；w3 WAL/备份/不覆盖 | PASS（双版） |
| N13 | 状态诊断 | w_lifecycle：fresh/接管后/继承退出的 status 文本与实际一致；identity_stats 代次 bug 已修 | PASS（双版） |
| N14 | 只读入口 | n3 mode=ro 一致快照 | PASS（双版） |
| N15 | 过滤与消歧 | n3 真实 CLI：唯一基础身份/歧义候选拒绝/时间边界/状态白名单 | PASS（双版） |
| N16 | 归档与异常 | n3 archives/状态标注；X1 后归档行可查（archived_rows_queryable） | PASS（双版） |
| N17 | 一致性快照 | mode=ro 读事务 + w3 WAL 共存备份 | PASS（双版） |
| N18 | HTML | 本轮 W6/W7 变更后已重渲染（w-render-*.png）；X 轮 HTML/筛选未再变化，视觉结论继承该版本与范围 | PASS（继承+上轮通过；Codex 可复核） |
| N19 | JSON | n3 信封/记录字段/截断/resolved_identity | PASS（双版） |
| N20 | 文件与限额 | n3 guards + backups 整树守卫（大小写/相对/等价路径，字节不变） | PASS（双版） |
| N21 | 数据边界 | 工具不读配置/不连网；Git/ZIP 干净（p6） | PASS（双版） |
| N22 | ZIP 独立使用（真实交付包） | s_rework `s6_delivered_zip_lifecycle`：定位**实际交付 ZIP**、哈希对 .sha256 核对、解包安装跑完整生命周期（load/reload/turn_off/turn_on/uninstall+活动/排队受控停止+注册表/目录清理断言）双版 70 断言；w_zip 另证工具独立运行 | PASS（双版） |
| N23 | 测试可信度 | w_lifecycle 故障注入（12 字段翻假+任务 TimeoutError/RuntimeError+缺字段全检出）、_run_worker rc=19 判失败、s6 断言注入（任务异常/生命周期布尔假/卸载未清理全检出）；KeyError 归属查清=宿主 call_event_hook 对已卸载 handler 的日志路径（夹具分类+受控不变量照断言） | PASS（双版） |
| N24 | 朋友演练 | 同 QQ 黑→白→私聊双向接续、本地查看/JSON、GUI | **待实机（MIS-145）** |

