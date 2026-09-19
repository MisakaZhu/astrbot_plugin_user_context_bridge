# 更新日志

## 0.7.0（Z1–Z3 返工候选；Y 轮候选 ebf0144 尚未通过独立验收，本版修复剩余项）

依据独立复验（报告 29 号，基线 ebf0144）修复：

- **Z1a 全部实际入口的失败判定**：新增 tests/worker_result.py 作为唯一
  共用判定（safe_run 捕获超时/启动失败；read_worker_result 判定非零
  退出码、缺 RESULT 行、JSON 损坏、JSON 非对象，错误保留可定位信息）；
  w_zip_lifecycle_check 的 ZIP 安装链与 t_rework_check 的 T1/T5 worker
  入口此前只看 RESULT 行（rc=19 仍 69/98 PASS）——已全部收敛到该判定；
  故障注入分别经正式 t1_t5_t6_workers / 正式 N22 安装链入口及其真实
  subprocess 处理路径（正常对照过，rc19/缺行/坏 JSON/JSON 数组/必要
  字段缺失逐个 FAIL）。
- **Z1b 锁与任务清理进入父断言**：W worker 现同时记录切换前**旧实例**
  （排队实际发生处）与新实例的身份锁等待者及未终态挂起任务数，
  assert_worker_fields 逐项 ==0（缺字段/非零均 FAIL）；本轮无真实锁
  残留，系把既有证据缺口补成硬不变量而非修复产品泄漏。
- **Z2 命令保存/登记可恢复协议**：`/uctx off|on` 改为**模式登记先行**
  ——登记失败（真实 SQLite 语句失败或锁冲突，含 sqlite3.Error）即整条
  命令受控失败，退出/加入尚未写入，不再出现"退出已保存、模式事实
  缺失"的不可恢复分裂（该分裂曾使登记失败后直接切另一模式 0→0 漏
  推进）；失败 on 不解除既有退出（磁盘/内存/status/后继 captured 一致）；
  后一写失败只留下"已登记未退出"的可重试状态。真实故障基线（TEMP
  TRIGGER 语句失败 / 第二连接 BEGIN IMMEDIATE 持锁）双版留存于
  local_evidence/y_logs/z2_baseline_426|428.log；正式回归入 y2 套件。
- **Z3a 工具到最终模型**：N04 三窗改用真实 FunctionTool/ToolSet 与
  宿主人格工具选择链（每人格配专属工具，经 _ensure_persona_and_skills
  装配），FakeProvider 记录终模型 func_tool 的类型/工具名/可序列化
  OpenAI schema；断言当前人格工具存在、旧/他人格工具不串入；动态注入
  改挂真实 OnLLMRequestEvent 钩子（不再预拼 system_prompt）；新增工具
  丢弃负例（真实 Runner._func_tool_for_provider 边界置 None）——正式
  父断言必须 FAIL。
- **Z3b N10 user follower 双人格**：命令窗口解析 maid、follower 新轮
  解析 second（真实 resolve_selected_persona/provider_settings 链），
  父断言检查两者确实不同、同平台/机器人/发送者、新轮写入同一 u: 键、
  once-only 屏障与新问答保留不变。
- **回滚探针更正**：y4_rollback_verify 的"直接换旧代码"段改为在**含
  旧记录的迁移库副本**上验证（裸键 0 条、编码键 1 条、写入后两套键
  并存），关键结果加 verified_silent_key_split 失败断言；COMPATIBILITY
  明确恢复的是升级前快照、升级后新增数据不在备份内。

## 0.7.0（Y1–Y4 返工候选；X 轮候选 4d73c7a 整体验收未通过，本版修复剩余项；本节为历史记录）

依据独立复验（报告 27 号，基线 4d73c7a）修复：

- **Y1 假通过路径**：S6 卸载等待不再吞异常（任务结果显式归类为
  正常返回/取消=明确预期停止/失败，异常保留完整调用栈）；全部实际
  worker 子进程入口（S6 与 W）统一检查非零退出码、缺失 RESULT 行与
  损坏 JSON，rc=19+合法 JSON 不再视为成功；故障注入作用于真实路径
  ——观测器在真实 worker 进程的真实 wait_for 等待边界注入（S6
  活动/排队/卸载任务、W 活动/排队任务），输出交由真实父断言判定，
  正常对照必须过、逐个故障必须判 FAIL；W 排队任务对宿主
  call_event_hook 已卸载插件日志 KeyError 的受控分类收紧为精确边界
  （完整调用栈+异常类型/键/最深帧来源+事件停止+0 模型调用+无共享
  回写+锁无残留），任意其他 KeyError/RuntimeError/TimeoutError 一律
  失败；N22 正式入口改为显式接收候选 ZIP 路径与预期 SHA-256（缺
  .sha256 记录、哈希不匹配、输入包缺失必须失败，不再按 mtime 选包）。
- **Y2 首次命令身份登记**：`/uctx off`/`/uctx on` 等产生持久化身份
  状态的命令入口现在登记当前生效模式（此前仅首次轮次登记）。新用户
  仅执行命令后直接切换模式，两种方向都恰好推进一次代次；区分 v1
  升级登记、新身份首次命令参与与已生效身份配置切换；登记边界失败
  返回受控文案不假报成功，重载对账补登记，重试不多推进不失退出。
- **Y3 N04 真实链**：user 模式场景改走完整宿主链——真实
  PersonaManager/ConversationManager → 宿主请求装配 → 注册请求钩子 →
  ToolLoopAgentRunner/假模型实际调用 → 真实终态钩子提交 → 下一窗口
  请求；群A→群B→私聊三窗全部真实 completed、恰好一次模型调用、
  0 watchdog、0 pending；后继窗口终模型实参含当前窗口人格系统规则、
  当前开场白恰好一次、工具与动态注入，不含旧人格系统/开场白，含前一
  窗口完整问答；动态临时内容不落共享账本。不再手写账本提交或依赖
  watchdog 释放正常轮次。
- **Y3 N10 双模式矩阵**：真实 Context/命令分发/装饰链矩阵补齐两模式
  原定分支（正常 reset/new、权限拒绝=真实宿主群聊 admin 默认、禁用、
  过滤、改名旧名拒绝/新名成功、无 provider reset 拒绝与 new 成功、
  范围外、继承保护）；follower 查询键随命令事件身份走（user 模式查
  u: 键，不再固定查 persona 键）；新增 user 模式 once-only follower
  屏障（一次成功只 bump 一次/提示一次，命令挂起期间同账号另一人格
  新问答不被二次装饰清掉）。
- **Y4 文档与统计更正**：X 轮"745 项"统计不成立（实际每版 708，
  18 套外层运行日志行），已更正并改按最终日志重计；备份描述更正为
  `pre-migrate-v3-*` + SQLite backup API（非 WAL checkpoint+复制）；
  回滚步骤改按真实 0.6.0（859f18e）代码验证——直接换旧代码的真实
  行为是静默键分裂而非"列冲突报错"；v1 人格名一律按字面值保留编码，
  仅来源确定为 v2 的 `__mode_user__` 才隔离（ADR-017）；X 轮超出
  证据的声称（完整栈/完整 N04/N10/N22）同步更正。

## 0.7.0（W1–W8 返工候选；首个候选 219cef2 未通过独立验收，本版修复；本节为历史记录，其中 W8 统计口径与部分"完整链"表述已被 X/Y 轮复验收紧）

依据独立验收（报告 23 号）修复的阻断项：
- **W1 真实重载不推进模式代次**：生效模式现持久化于账本 meta（基础身份
  维度 scope_mode），真实 PluginManager.reload 与停机改配置后启动均可
  识别显式切换（代次 +1、新历史开始、旧记录归档不回灌不复活）；同配置
  reload/重启/人格黑白轮换不清空；reset 后切换不复活已清空历史；配置
  变化时挂起/排队轮受控停止。
- **W2 退出继承未接入**：persona off→user 转换写入 user 键退出；user
  off→persona 形成基础身份保护（覆盖现有与未来人格）；单人格 on 仅解除
  该人格；退出判定收敛为 MembershipStore.effective_optout 单一语义，
  status/命令/运行时一致；membership 损坏/写失败受控禁用共享，绝不按
  空退出集合继续采集。
- **W3 迁移非原子/备份丢行**：migrate_ledger 单事务完成加列+键改写+
  回填+版本标记（失败 ROLLBACK 原库真不变、重试完整执行）；备份改用
  SQLite backup API 一致性快照（持读事务期间已提交的 WAL 数据不丢），
  临时文件原子改名、绝不覆盖既有备份；迁移移入租约持有序列。
- **W4 共享键碰撞**：user 键改用结构性编码 `u:`（persona=`p:<id>`，
  迁移隔离=`q:`），与任意真实人格 ID 不可能相同；0.6.0 裸键与首个候选
  键由迁移统一改写，不可辨认的旧候选键隔离保留不注入。
- **W5 status 误报**：显示模式/当前真实人格/来源范围/有效退出（同源
  语义）/实际接管证据（无记录明确提示）/有效 completed 数（按当前
  epoch+代次）；reset/new 文案按模式说明清空范围。
- **W6 导出身份消歧**：export 先解析唯一基础身份，缺失/歧义报不含正文
  的候选错误且不写文件；唯一推断在输出中明示；文档示例修正。
- **W7 备份覆写**：输出守卫保护源库关联的 backups/ 目录整树（大小写/
  相对/等价路径），真实 CLI 校验备份字节不变。
- **W8 测试与矩阵**：新增 w_lifecycle（真实 PluginManager 四阶段
  reload/退出矩阵/status，双版 57 断言）、w_rework（真实 0.6.0 旧库
  迁移/WAL 一致性备份/故障重试、键编码，34 断言）、w_zip_lifecycle
  （真实候选 ZIP 解包安装跑完整生命周期+工具独立运行，59 断言）三套；
  N12 收紧为逐项校验回滚后结构与行内容并可重试完成；N07 补齐 user
  off→persona 与未来人格及单人格 on；N09 屏障证明 B 未进入模型且最终
  请求含 A 一次完整问答；故障注入实调父断言（12 关键字段翻假全检出）。

## 0.7.0（跨人格共享与本地记录候选，待 Codex 独立验收）

新增（ADR-015）：
- **可选跨人格共享**：新配置 `history_scope`（`persona` 默认=0.6.0
  行为；`user`=同一 platform+bot+QQ 用户跨人格共享，每个窗口仍使用
  该窗口当前人格、系统规则与工具，实际人格仅作记录元数据
  `source_persona`）。非法配置保守回退 persona 并告警，不静默扩大
  共享范围。user 模式身份键第三段使用保留字面量 `__mode_user__`。
- **账本 schema v1→v2 迁移**：turns 增加 `source_persona` /
  `mode_generation` 列与 `schema_version` 表；升级（保持 persona）
  不清空、不改语义，迁移前自动备份 `backups/pre-migrate-v2-*`，幂
  等可重复，损坏输入报错不写（旧库 0.6.0 首次由新版插件启动自动迁
  移）。
- **模式代次**：显式切换 persona↔user 时该基础身份
  `mode_generation +1`（一个事务），新模式从空历史开始，旧记录归档
  可查不回灌；reload 同配置/重启/正常人格轮换不触发。
- **退出状态继承**：membership v2（`base_protected`/`persona_on`），
  persona off → user 仍退出；user 退出 → 保护该用户全部现有人格与
  未来新人格，单人格 on 只解除该人格；管理员范围仍是上限。
- **本地只读记录工具** `tools/uctx_records.py`（随安装包交付，纯标
  准库）：`list` 身份分区元数据 / `export-json` 程序导出 /
  `export-html` 自包含离线浏览页。`mode=ro` 只读 + 单读事务一致快
  照；默认仅当前代次 completed，归档/失败/中止/中断需显式选择并标
  注；时间起点包含、终点不含；上限截断明示；HTML 注入转义、无外部
  资源、站内搜索；输出路径守卫（拒覆盖源库/WAL/SHM、原子写、失败无
  半文件）；v1 旧库拒绝并提示先迁移。真实浏览器渲染与搜索交互已在
  本地 Playwright 验证（截图留证，Codex 视觉复核待 MIS-170）。
- `/uctx status` 显示当前共享模式；user 模式 reset/new 清空该用户
  跨人格整份有效历史（persona 模式仅当前人格），原生命令联动语义
  不变。

测试：新增 n0/n1/n2/n3 四套（9/14/11/50 断言），与旧 10 套合计
14 套 × 4.26.0/4.28.0 双版全过（逐项计数见 docs/ACCEPTANCE.md）。

## 0.6.0（五次返工候选，待 Codex 复验）

五次验收（06d5598）V1/V2 返工（ADR-014）：
- V1 一次性成功关联：插件私有 applied 状态（同步原子认领）去重——同
  一原生命令成功只切一次 epoch、提示一次；屏障验证新问答在后续装饰
  后仍可读（基线缺陷双版复现 notify=2 → 修复后 1）；不清除宿主标记。
- V2a 真实恢复链：worker new-without-provider-then-recovery 场景
  （同事件同账本，无直接 bump_epoch）。
- V2b 故障注入实调父测试 s6_plugin_lifecycle（正常 16 PASS、逐字段
  false 各触发、全 false 对照）。
- V2c U1 排队取消断言 ev→b 笔误修正。
- 文档口径统一（结构化标记描述、T2=9、A17 handler=10）。

## 0.5.0（已交付四次返工候选 06d5598；五次验收未通过，见 0.6.0）

四次验收（0a915d3）U1~U4 返工（ADR-013）：
- U1 全等待点取消语义：初始人格解析/锁等待/锁后全部覆盖——取消后事件
  停止传播（宿主吞取消后在下一个 yield 检查点截断）、0 模型 0 输出、
  等待中取消不释放他人锁、A 不受 B 取消影响、后继同身份跨窗请求完成。
- U2 new/reset 分命令成功语义：new 不要求 provider（宿主真实语义），
  无 provider 的 new 成功也联动清空；reset 保留 provider 防御；恢复
  provider 后不回灌。
- U3 结构化成功事实：`_clean_group_context_session` 布尔 extra 替代
  成功文案 startswith（前置文本装饰不再影响判定）；activated_handlers
  结构化激活证据保留。
- U4 测试有效性：harness trace 初始化修复（UnboundLocalError 不再被
  吞）；gather 结果纳入断言；T2 登记前取消收紧；S6 生命周期关键字段
  进入父测试（故障注入验证 FAIL）；断言数校准（290/版本）。
- r7/s5 场景适配 U3 结构化标记语义；t5 worker 经真实 PipelineScheduler
  + ResultDecorateStage + 真实 bound 处理器驱动（不再直接调 helper）。
- 全量：10 套 × 4.26.0/4.28.0 各 290 项断言全 PASS（日志加总）。

## 0.4.0（已交付三次返工候选；四次验收未通过，见 0.5.0）

三次验收（169a88a）T1~T6 返工（ADR-012）：
- T1 人格同源解析（provider_settings 参数；4.26 默认人格隔离修复；解析失败受控不折叠）；真实 PersonaManager/ConversationManager/宿主新会话路径两版验证。
- T2 锁后取消窗口：pending/watchdog 提前登记 + CancelledError 清理（interrupted + 停止传播 + 释放锁）；取消轮无模型调用无输出。
- T3 关闭协议 shutdown()：关闭入口/排队干净让出/活动轮停止信号/terminate 顺序；真实 PluginManager turn_off/turn_on/reload/uninstall 与挂起+排队轮验证（无迟到输出、无异常回退、恢复不回灌）。
- T4 缓冲正文抑制：全套停止信号（scheduler yield 断链）+ decorating 清尾双保险；buffer=True 场景 watchdog 后无新增正文。
- T5 provider 检查版本兼容（4.26 真实 Context 同步接口）。
- T6 原生命令后置成功关联：删同名单处理器；activated_handlers 结构化证据 + 宿主固定成功文案 + 轻量防御；禁用/改名/过滤/无 provider/权限矩阵经真实 Context 命令分发两版验证。
- commands.py 过时文案修正；测试新增 t_rework_check（35 项/版）与 t1/t5 worker；p1 适配受控失败语义。
- 全量：10 套 × 4.26.0/4.28.0 各 258 项断言全 PASS（日志加总）。

## 0.3.0（已交付二次返工候选 169a88a；三次验收未通过，见 0.4.0）

二次验收（d8a7147）S1~S6 返工（ADR-011）：
- S1 重复投递释放身份锁 + stop_event 终止传播（防二次回复）。
- S2 真实 /stop（agent_stop_requested 信号）终态 aborted；真实
  ConversationCommands.stop + 活动事件注册表验证。
- S3 watchdog 受控失败覆盖真实执行：先发宿主 /stop 同款停止信号再收尾，
  旧轮迟到输出在两版宿主均被抑制（屏障验证）。
- S4 删除正文前缀判失败的启发式；err 由 watchdog 收尾（诊断文本反例回归）。
- S5 原生 reset 镜像补 provider/third-party 检查；真实 reset 拒绝/成功/
  权限/停用场景验证（宿主拒绝时不清空共享历史）。
- S6 真实 PluginManager 隔离实例生命周期（安装/加载/默认关闭/配置重载/
  运行接续/挂起轮卸载释放/注册表与目录清理），两版宿主通过。
- 测试：9 套 × 4.26.0/4.28.0 各 221 项断言全 PASS（日志加总）。

## 0.2.0（已交付返工候选 d8a7147；二次验收未通过，见 0.3.0）

独立验收（8477eba）R1~R7 返工：
- R1 发布包按宿主 data.plugins 路径可导入（包内相对导入；A17 改宿主式验证）。
- R2 终态机 v2：on_agent_done 主提交 + is_stopped 判 aborted + err 文本加速 +
  fail-watchdog；新增真实调度链 harness（PipelineScheduler + ResultDecorateStage）。
- R3 身份锁覆盖至轮次终态；屏障验证跨窗口顺序与真实请求内容。
- R4 prompt 前缀匹配识别本轮边界；临时内容不入库；completed 必有 assistant。
- R5 命令身份接入会话 persona（修复 getter 未调用缺陷）。
- R6 租约运行时身份 + owner_token 归属校验；多进程排他验证。
- R7 原生 /reset、/new 联动 epoch（ADR-010）。
- 测试：8 套 × 两版宿主各 181 项断言全 PASS。

## 0.1.0（已交付本地候选 8477eba；独立验收未通过，见 0.2.0）

- P0（MIS-136）`1c080d1`：独立 Git 仓库基线；4.26.0/4.28.0 宿主调用链 ADR（ADR-001~007）；最小脱网生命周期验证 17/17（实证：模型 err 不触发完成钩子、aborted 旗标滞后——写侧兜底轨为必须项）。
- P1（MIS-137）`ffeec5d`：四维共享身份（平台实例/self_id/人格范围/真实 sender）；显式范围（默认关闭、群白名单+私聊开关、个人退出不扩大范围）；归属判定排除项。35/35。
- P2（MIS-138）`bcec73d`：SQLite 轮次账本——event_key 去重、事务内 seq、epoch 清空与防复活、重启恢复、同库双实例租约防护、多模态/思考脱敏。27/27。
- P3（MIS-139）`dd650e7`：完整闭环——on_llm_request 替换上下文并短路宿主写回（req.conversation=None），on_decorating_result 唯一提交点；群A→群B→私聊→群A 接续实证。19/19。
- P4（MIS-140）`414b7a3`：跨窗口并发顺序、异常/中止终态、发送状态可识别、reset 慢请求竞态、崩溃恢复。21/21。
- P5（MIS-141）`a389f3a`：/uctx 命令组（status/reset/off/on/scope）；停用/重启用不回灌；其他插件动态注入共存；ADR-008 原生 /reset、/new 边界。16/16。
- P6（MIS-142）`d9d55e9`：A01-A18 验收映射（docs/ACCEPTANCE.md）；宿主真实校验器 + 打包/隐私审计。8/8。
- P7（MIS-143）：交付文档、可安装 ZIP 与校验值（见 HANDOFF）。

测试统计：七套 × 4.26.0/4.28.0 各 151 项断言全部通过（合成数据、假模型/假传输）。
