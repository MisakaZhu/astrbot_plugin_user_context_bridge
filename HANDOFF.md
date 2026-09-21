# HANDOFF（交接说明）

## 0.7.0 X1–X6 返工交付（2026-09-19，待 Codex 复验）

**分支 `feat/0.7.0-persona-records`；W 返工基线 4f542fe。X 轮修复全部阻断项；未 push/未发布/未部署。**

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| X1 首次切换漏推进 | 全新安装无身份可枚举，首轮只写 turns 不登记 scope_mode；recorded=None 被当初次升级 | begin_turn 同事务登记 scope_mode（首次参与）；apply_scope_mode 语义不变（None=登记不改代次，仅对迁移遗留/仅退出身份） | w_lifecycle Phase 1 无预热直切：登记 persona→直切 user +1→归档可查（双版）；x1 单元 |
| X2 旧 persona_on 越过新 user off | persona_on 无先后关系 | user 维度 opt_out 撤销该基础身份既有 persona_on；此后单人格 on 才解除 | w_lifecycle Phase 6 + x2 单元（多轮 off/on/往返/磁盘回读） |
| X3 迁移按前缀猜编码 | 旧数据 scope 是原始人格 ID，`p:maid`/`u:`/`q:foo` 被误当已编码 | _migrate_identity_key/encode_membership_key 按输入版本解码：v1 一律 p:+字面值；v2 仅 __mode_user__ 歧义隔离；两遍改写消除 UNIQUE 中间态碰撞；source_persona 保留字面值；meta 记录来源版本，membership 同源 | x3（真实 859f18e 旧库六个特殊人格名+成对碰撞） |
| X4 持久化失败内存分裂 | 内存先发布、写盘后行 | MembershipStore 先持久化候选状态成功后发布内存（off/两种 on/protect_base/migrate）；命令层受控失败文案（不泄漏路径） | x4 故障注入（命令→采集→磁盘→重试） |
| X5 原生联动绕过有效退出 | main.py 仍用 is_opted_out | 改 effective_optout；t5 双模式真实分发矩阵含继承保护场景（不联动/不误提示） | t5 X5 行 + t_rework 父断言 |
| X6 测试放过失败 | worker 异常转字符串、父未断言；_run_worker 不查 rc/JSON | worker 保留完整栈并入 JSON；父断言任务异常/非零 rc/坏 JSON 全判 FAIL；s6 断言函数化+故障注入（任务异常/生命周期布尔/卸载未清理）；KeyError 归属=宿主 call_event_hook 对已卸载插件 handler 的日志路径（宿主侧边界，夹具分类+受控不变量照断言） | w_lifecycle/s_rework 故障注入段 |
| N04 | user 模式无真实链 | t1 N04：真实 PersonaManager/ConversationManager/宿主 `_ensure_persona_and_skills`；u: 单键/当前人格 system/开场白/工具/动态注入/source_persona/跨人格链 | t1 双版 + t_rework 父断言 |
| N22 | 非交付包且无卸载 | s6 worker 支持从交付 ZIP 解包安装；s_rework 定位实际交付包、核对 .sha256、跑完整生命周期（含 turn_off/turn_on/uninstall+活动/排队受控停止+注册表/目录清理） | s_rework 双版 70 断言 |

### 版本与包

- **AF 轮（2026-09-21）**：AF1 完整对账——正式 writer
  `persist_observation` 从本次 CompletedProcess 登记原始 AUDIT 行/
  traceback 段数/stdout+stderr 段 sha256 与采集绑定
  （tag/mode/capture_id/启动命令/injected）入 `_run_binding`；快照
  改为完整核心 payload（含目标布尔值）；provider-raise 要求 raw
  traceback>=3 段且与捕获一致；rc 行继续核对。只改文件反例（删
  AUDIT/AUDIT 错值/只删 raw 栈/磁盘 JSON+RESULT 反转 stop
  n04_all_model_called 与 provider n04_all_completed）均按证据维度
  拒绝、note 指认具体键，改后恢复原字节。AF1.4 schema 合成观测经
  正式 writer 生成新一致证据（injected=True、独立 capture_id），
  不再借用真实负例路径。AF2.A 期望 tag/mode/capture_key 由调用侧
  显式消费；全对象互换（两版均拒）、单版错 tag、stop 写侧
  mode=normal 绑定维度拒绝，未改版本保持合格。AF2.B
  `manifest_closing_checks` 实际打开并解析 manifest 文件，三方核对
  元信息/双引用/payload sha 并沿引用正式读回；写入边界注入
  {}/错误元信息/串路径收尾 FAIL、撤破坏对照干净通过。仅改
  `tests/t_rework_check.py`。全量 20 套 × 双版各 **915** PASS /
  0 FAIL（t 185 + w 92 + s 84 + w_zip 78 + y2 74 + x 64 + n3 59 +
  r 38 + w_rework 37 + p1 36 + n1 30 + p2 27 + p4 22 + p3 19 +
  p0 17 + p5 16 + n2 14 + n0 10 + p6 8 + aa1 5）。实现/包仍为
  acabbf7（SHA 复核一致）。HEAD 见 git log；
  **对 AE 的更正**：provider 真实栈并非 final_text_head（摘要≠栈）；
  AE 的 AUDIT 未与 raw 行对账；schema 合成观测当时借用不一致旧文件；
  manifest 当时仅存在检查、字段取内存——均由 AF 修复；AE2 交换/
  撤交换事实保留。
- **AE 轮（2026-09-21）**：AE1 证据关联——`verify_run_evidence`
  正式共用读回（引用==写侧绑定关联校验、rc 行、log↔JSON 自洽、
  诊断/调用/AUDIT==本次调用侧快照、AUDIT mode 绑定、provider 真实
  RuntimeError 栈）；`run_aa2_negative` 唯一 run ID/专属目录
  `local_evidence/aa2_runs/<run_id>/` + 唯一命名 manifest（run/
  scenario/tag/mode 与 JSON/log 双引用），删 glob/mtime/目录计数
  兜底与固定 verdict 文件名覆盖。AE2.A 错引用改为从本次捕获映射
  显式替换（跨版/跨模式/正常T1 三例），无注入对照 accepted、注入
  后 rejected、撤注入自检 FAIL 三方留存（`ae2a_wrong_reference_
  three_way.json`）；AE2.B AD1 循环按场景显式传 mode（provider
  场景不再错传 stop），仅 schema 反例 specific_target_hit=false/
  diag_ok/evidence_ok 且 rejected。仅改 `tests/t_rework_check.py`。
  正式全量 20 套 × 双版各 **1003** PASS / 0 FAIL（t 273 + w 92 +
  s 84 + w_zip 78 + y2 74 + x 64 + n3 59 + r 38 + w_rework 37 +
  p1 36 + n1 30 + p2 27 + p4 22 + p3 19 + p0 17 + p5 16 + n2 14 +
  n0 10 + p6 8 + aa1 5）。运行实现/包仍为 acabbf7（SHA 复核一致）。
  HEAD 见 git log；
- **AD 轮（2026-09-20）**：AD1 指定目标判定（stop→all-model-called、
  provider-raise→all-completed-zero-watchdog-zero-pending；仅翻假
  schema 反例 stop/provider 两模式均 rejected）+ AD2 证据重开验证
  （实际读回文件核对内容；三种证据损坏/缺失/错引用反向检验均 rejected）。
  正式全量 20 套 × 双版各 **900** PASS / 0 FAIL（t 170 + w 92 + aa1 5）。
  **AE 轮更正**：provider 仓库自检当时错传 stop mode；"错引用
  rejected"当时实因引用为空；"读回核对内容"当时仅结构/子串检查。
  HEAD 见 git log；
- **AC 轮（2026-09-20）**：AC1 T 自检改用 `_assert_t1_version` 正式
  断言（W 断言误用已更正）+ 正常对照/撤故障反向对照 + 正式 T 失败
  detail 附留证路径；AC2 `run_aa2_negative` 逐版本独立 verdict
  （target_hit/diag_ok/no_collateral 均按 .venv/.venv426 独立判定）+
  `_aa2_audit` 消费 + 逐窗/汇总一致性 + 三种新坏观测反向检验均
  rejected；AC3 observer_runner persist_run 落盘 + verdict manifest
  + 重开验证。正式全量 20 套 × 双版各 **893** PASS / 0 FAIL。
  HEAD 见 git log；
- **AB 轮（2026-09-20）**：AB1 功能失败留证（persist_run 每次运行
  都保存 + assert 失败 detail 附留证路径 + AB1 验证覆盖 语义/缺失/
  rc19/正常对照）+ AB2 负例判定收紧（run_aa2_negative 要求 target_hit
  + no_collateral + diag_ok；四种坏观测反向检验均 rejected）。HEAD 见
  git log；
- **AA 轮（2026-09-20）**：测试稳定性与留证收尾。HEAD 见 git log；
  **运行实现/包仍为 acabbf7**（ZIP 原字节保持，SHA-256
  `c21956febfda41e3baeb0b611734c9b907ea060fc8583aeb1ad3491e18aad728`
  复核一致；tests/ 不在打包白名单内，故测试/文档提交不换包）。
  N22 以显式路径+SHA 指定 acabbf7 包复跑：s 84/0、w_zip 78/0。
- AA1：修复 `tests/fakes.py` 消息 ID 用 `id(abm)` 的地址复用缺陷——
  新消息改用进程内单调序号，`message_id=` 显式覆盖保留给真正重复投递
  用例（S1 同对象复用语义未变）。基线双版固化：同窗 199/200、跨窗
  197/200 重复（`local_evidence/aa_logs/aa1_baseline_*_oldfakes.log`）；
  修复后 `tests/aa1_event_key_check.py` 双版 5/5。**因果边界**：上一轮
  426 W 套件 `single-on-releases-only-that` 失败的原始分项未留存，其
  确切原因未证实；本轮证实并消除的是夹具缺陷本身。该场景现带 RA/RF
  分项诊断（命令返回/done/captured/model_calls/stopped/事件键/同键
  先前终态），若再失败可据实归因。
- AA2：t1 `_model_view` 空调用不再二次 KeyError；新增
  `n04_window_diagnostics`（model_calls/hook_stopped/event_stopped/
  outcome/pipeline_error/final_text_head）与 `hook_error`（完整栈）；
  受控停止如实记 hook-stopped 不虚构 pipeline_error；无调用仍判 FAIL。
  负例经正式父入口（真实请求钩子停止 + provider 真实抛错）双版验证
  检出且诊断字段完整。五入口失败自动留证 stdout/stderr/JSON 至
  `local_evidence/worker_failures/`。
- 稳定性（事先固定次数，全结果保留
  `local_evidence/aa_logs/stability/`）：T1 worker 与 W 套件各双版 3 次
  独立目录，12/12 正常（T1 n04_model_calls 均 [1,1,1]；W 均 78/0）。
- 正式全量：20 套 × 双版各 **882** PASS / 0 FAIL（40 次 rc=0；AB 后含 t 152 + w 92 + aa1 5；
  aa1_event_key_check 5 项/版），日志 local_evidence/y_logs/。

- **本轮（Z1–Z3）交付提交 acabbf7**；候选包
  `astrbot_plugin_user_context_bridge-acabbf7.zip`（13 文件），
  SHA-256 `c21956febfda41e3baeb0b611734c9b907ea060fc8583aeb1ad3491e18aad728`；
  全量回归 19 套 × 双版各 **854** 项全 PASS（0 FAIL，38 次 rc=0；
  日志 local_evidence/y_logs/ + regression_summary.json）。N22 复现命令：
  `PYTHONPATH=. <venv>/Scripts/python.exe -X utf8 tests/s_rework_check.py --delivered-zip release/astrbot_plugin_user_context_bridge-acabbf7.zip --delivered-sha256 c21956febfda41e3baeb0b611734c9b907ea060fc8583aeb1ad3491e18aad728`
  及同参 `tests/w_zip_lifecycle_check.py`。
- 修复范围：Z1a 统一 worker 入口判定（tests/worker_result.py：safe_run
  捕获超时/启动失败，read_worker_result 判定 rc/缺行/坏 JSON/非对象；
  w_zip 与 T1/T5 入口收敛；正式父函数+真实 subprocess 路径注入 6 类
  故障）；Z1b 旧实例+新实例锁等待者/挂起清理入父断言（==0 硬不变量）；
  Z2 命令登记先行协议（真实 SQLite TEMP TRIGGER 语句失败与第二连接
  持锁双版基线 → 登记失败=整条未生效、失败 on 不解除退出、直接切模式
  无 ghost，正式回归在 y2 套件 Z2.*）；Z3a N04 真实 FunctionTool/
  ToolSet+人格工具选择链到终模型（类型/名称/schema/无串入断言+丢弃
  负例 z3_tools_fault_observer）、动态注入挂真实请求钩子；Z3b user
  follower 双人格（命令=maid 经 resolve_persona_scope 实链、新轮=
  second 账本 source_persona、同 u: 键）；y4_rollback_verify 改含旧
  记录副本+verified_silent_key_split 断言（双版 true）。
- 断点恢复：工作区干净（文档收尾提交后）；旧包
  ac51bde/27aadae/ce8b75c/4cee8cf/1efc0d2 均保留原字节。
- 已知限制：426 t1 worker 曾两次在套件内偶发 N04 三窗模型未调用
  （直跑与复跑不可复现）；已加 n04_pipeline_errors 诊断字段，最终
  全量回归未复现。若复验再遇，请依据该字段定位。

- **本轮（Y1–Y4）交付提交 1efc0d2**；候选包
  `astrbot_plugin_user_context_bridge-1efc0d2.zip`（13 文件），
  SHA-256 `9f6299eab0bab7b6fb6547418880d71ede1597b166c64a7f4a03c2d64842ec57`；
  全量回归 19 套 × 双版各 **814** 项全 PASS（日志 local_evidence/y_logs/，
  run_one/run_regression 输出）；N22 以显式路径+SHA 指定该包跑通。
  N22 复现命令：
  `PYTHONPATH=. <venv>/Scripts/python.exe -X utf8 tests/s_rework_check.py --delivered-zip release/astrbot_plugin_user_context_bridge-1efc0d2.zip --delivered-sha256 9f6299eab0bab7b6fb6547418880d71ede1597b166c64a7f4a03c2d64842ec57`
  及同参 `tests/w_zip_lifecycle_check.py`。
- 修复范围：Y1 任务/子进程失败全入验收（S6 卸载等待归类+完整栈、
  worker 入口 rc/缺行/坏 JSON、真实路径故障注入 y_fault_observer、
  W KeyError 精确边界严格分类、N22 显式 ZIP+SHA）；Y2 首次 off/on
  命令登记模式事实（新套件 y2_command_first_use_check）；Y3 N04
  真实链（t1 重写：模型调用/真实终态/0 watchdog/0 pending）与 N10
  双模式矩阵+user follower 屏障（t5 扩展）；Y4 文档按真实 0.6.0
  （859f18e）代码验证更正（回滚步骤、pre-migrate-v3-*/backup API、
  静默键分裂、ADR-017、X 轮 745→708）。
- 断点恢复：工作区应干净；实现提交 1efc0d2，纯文档收尾为后续提交；
  旧包 ac51bde/27aadae/ce8b75c/4cee8cf 未触碰。

- 交付提交 760f647（X 轮主体）+ 4cee8cf（w_rework 适配）；候选包
  `astrbot_plugin_user_context_bridge-4cee8cf.zip`（13 文件），
  SHA-256 `6581762dfd2e112a53ccbdb52e7e3925e66d212f439ed6cd9fe384b405941e4d`；
  双版导入探针 + ZIP 工具独立运行 OK；旧 ac51bde/27aadae/ce8b75c 包未触碰。
- 全量回归：18 套 × 双版；~~745~~（**Y 轮更正：实际每版 708**，日志 local_evidence/x_logs/）。
- 边界：未 push/未发布/未部署；真实 QQ/模型未用；N24 实机待 MIS-145。

---

## 历史交接

<details>
<summary>W 返工/0.7.0 首候选/0.6.0（历史）</summary>


## 0.7.0 W1–W8 返工交付（2026-09-19，待 Codex 复验）

**分支 `feat/0.7.0-persona-records`；返工基线 219cef2（首个候选，Codex 判未通过）。本次修复 W1–W8 全部阻断项；未 push、未发布、实机未验证。**

### 修复与证据（对应独立验收报告 23 号 W1–W8 编号）

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| W1 模式变化不被 reload 识别 | 构造器把新配置当"上一生效模式"，无持久化 | 生效模式持久化 meta.scope_mode（基础身份维度），apply_scope_mode 单事务判定（未记录=登记不改代次；不同=代次+1）；真实 reload/停机改配置/同配置不清空/黑白轮换不误清全覆盖 | w_lifecycle_worker 双版：四阶段代次 [0,0,1,2,3]、不回灌不复活、reset 后切换不复活 |
| W2 退出继承未接入 | protect_base/release_base 无生产调用 | 真实转换函数 _convert_exit_for_mode_change（加法、幂等）接入 initialize 对账；off/on 分维度命令；effective_optout 唯一语义；损坏/写失败受控 | w_lifecycle Phase 6 退出矩阵 + n1 + w2 场景（双版） |
| W3 迁移非原子/备份丢行 | DDL/回填分事务提交；checkpoint busy 忽略+仅拷主库 | migrate_ledger：单 IMMEDIATE 事务（失败真回滚、重试完整）+ backup API 一致性快照（WAL 已提交不丢）+ 备份唯一名不覆盖 + 迁移移入租约序列 | w3（真实 859f18e 旧库：对账/WAL/故障重试/备份共存）+ n1 N12 收紧 |
| W4 user 键与真实人格碰撞 | __mode_user__ 保留字可被真实人格占用 | 结构化编码 p:/u:/q:；0.6.0 裸键迁移改写 p:；不可辨认旧候选键 q: 隔离不注入；membership scheme3 同步（不可辨认证安全方向转基础保护） | w4 编码断言 + 探针证据（persona_probe）；ADR-016 |
| W5 status 旧实现 | 仅查精确 optout、忽略接管证据与代次 | status 重写：模式/当前真实人格/范围/有效退出（同源）/实际接管证据/有效数（当前 epoch+代次）/reset 范围 | w_lifecycle status 断言 + n2；修复 identity_stats 代次读错键 |
| W6 导出身份不消歧 | 无参/部分参导出多基础身份 | 导出先解析唯一基础身份；缺失/歧义→候选列表（不含正文）+rc2 不写文件；唯一推断输出明示；文档示例修正 | n3 真实 CLI 矩阵 |
| W7 backups 不受保护 | 仅判断"DB 父目录名为 backups" | 规范化路径保护 <库目录>/backups/ 整树（大小写/相对/等价），真实 CLI 校验备份字节不变 | n3 N20 场景 |
| W8 测试/矩阵缺口 | 关键断言空泛/未覆盖真实路径 | 新增 w 三套（150 断言）；N12 逐项核验回滚+重试；N07/N08/N09/N13/N22 收紧；故障注入实调父断言（12 字段翻假全检出）；N18 新产物重渲染 | local_evidence/w_logs/、n3_html/w-render-*.png |

### 版本与包

- 全量回归：17 套 × 4.26.0/4.28.0 各 **561 项全 PASS**（298+113+150）；日志 local_evidence/w_logs/（34 份）。
- 交付提交 ce8b75c（实现+文档）；候选包 release/astrbot_plugin_user_context_bridge-ce8b75c.zip（13 文件白名单含 tools/uctx_records.py），SHA-256 40493ca1623d3589ae0572cd6a6dac0cfa4f1cfb51e9453559c6e8d33da887bd；双版导入探针 OK + ZIP 解包后工具独立运行 OK；旧 0.6.0 包（ac51bde，78e64560…9af）逐字节未触碰。
- 边界：未 push、未建 Release、未部署；真实 QQ/模型未用；迁移仅合成/真实实现构造旧库；N24 实机待 MIS-145。

### 历史

<details>
<summary>0.7.0 首个候选（219cef2，未通过）与 0.6.0 交接（历史）</summary>


## ⚠️ 暂停断点（2026-09-19，W1–W8 返工中途，用户升级 ZCode）

**基线 219cef2（W 返工起点，工作区干净）。返工依据：`上下文共享-规划交接-20260917\24_ZCode_GLM53_0.7.0_W1-W8返工提示词.md` + `23_Codex独立验收_0.7.0_219cef2.md` + 证据目录 `上下文共享-独立验收-219cef2-20260919`。Linear MIS-165~169 已置 In Progress。**

### 本次已改（未提交 → 立即以 WIP 提交保存）

- **W4 identity.py**：scope 段改结构化编码 `p:<persona>` / `u:`（user）/ `q:`（迁移隔离），SharedIdentity 增加 mode 字段，key/from_key/build_identity/identity_from_event 全部按编码；`MODE_USER_SCOPE="__mode_user__"` 已删除（结构性无碰撞方案，不再靠保留字）。
- **W2 scope.py**：MembershipStore 重写——`effective_optout`（唯一有效退出语义，persona=直接退出或(基础保护且未 persona_on)；user=直接退出或基础保护；**不**按 persona 退出阻断 user，否则 user on 后永远无法恢复）、`opt_in_persona`/`opt_in_user`（user on 解除 user 键+基础保护，保留人格显式退出）、损坏文件抛 MembershipError（不再按空集合继续）、原子写、`migrate_legacy_keys()`（scheme3，`__mode_user__` 不可辨认键→p: 退出+base_protected 安全方向，备份 .pre-scheme3.bak）；resolver evaluate 用 mode 构造身份 + effective_optout 单点判定。
- **W1/W3 ledger.py**：SCHEMA_VERSION=3；`inspect_schema_version()`；`migrate_ledger()`（只读校验→**backup API 一致性备份**含 WAL、临时文件原子改名、唯一名不覆盖→**单 IMMEDIATE 事务**加列+全表键改写（`_migrate_identity_key`：裸 `__mode_user__`→q:，裸其他→p:，3 段 base 键不动）+source_persona 回填+版本标记，失败 ROLLBACK 原库真不变）；`migrate_from_v1` 留兼容别名；`open(auto_migrate=False)` 不写 schema 置 `_legacy_pending`（防 executescript 误标 v1 库）+`ensure_migrated()`；新方法 `get_scope_mode`/`apply_scope_mode`（未记录→登记不改代次 changed=False；已记录不同→代次+1）/`enumerate_base_keys`/`identity_stats`（有效 completed 按 epoch+代次，两条固定字面量 SQL）。
- **W1/W2 main.py**：构造器只存 `_config_scope`（期望模式，不再当"上一生效模式"）；membership 损坏→None+`_membership_error`；`initialize` 顺序改**连接(不迁移)→取租约→membership 迁移→ensure_migrated→recover→模式对账**；对账：枚举 bases（ledger+membership 侧，含仅有退出的身份）→ recorded!=desired 时 `_convert_exit_for_mode_change`（加法转换：→user 有任何退出则写 user 键退出；→persona user 键退出则 protect_base）→`apply_scope_mode`；`_disable_sharing` 统一受控禁用（租约冲突/membership 损坏/迁移失败三分支）；`_collect_base_identities` 删除；register 版本 0.7.0。
- **W2/W5 commands.py**：全量重写——`unavailable_reason` 支路；`_identity` 返回(身份,当前真实人格)；off/on 按模式（persona on 仅解除本人格 opt_in_persona；user on 解除 user 键+基础保护）；status 显示模式/当前真实人格/窗口范围/**有效退出（同源语义+来源）**/实际接管证据（last_turn_at 无记录时明确"尚无记录（开关开启不等于已实际接管）"）/有效 completed（当前纪元·代次）/reset 范围说明；reset 文案按模式区分范围。

### 未完成（恢复后从此继续）

1. **tools/uctx_records.py（W4 解码/W6/W7）**：还没改。需：decode_identity 支持 p:/u:/q:/bare(legacy 标注)；删 MODE_USER_SCOPE 常量（47 行、169 行）；版本检查接受 v3（现 `<2` 拒绝逻辑保留即可）；**W6**：export 先解析唯一基础身份（无参/部分参→候选列表+rc=2 不写文件；唯一推断在 stdout+JSON filters+HTML 明示），list 不动；**W7**：guard_output_path 保护 `<db_parent>/backups/`（normcase+resolve，目录级拒绝）；README 示例同步。
2. **测试**：全部未动——旧 n0/n1/n2/n3 仍用 `__mode_user__` 会红；需按 W8 改写：`tests/user_key()` 改 `build_identity(mode="user")`/`u:` 键；n1 迁移断言收紧（W3 语义变了：失败回滚后重试完整完成）；n3 期望值按新键编码/消歧更新；新增 W 系列（真实 PluginManager 四阶段 reload、退出矩阵、迁移对账/故障/WAL 备份、键碰撞真实 PersonaManager、status 文本对照、CLI 消歧/守卫矩阵、N09 屏障、N04 真实 PersonaManager、N10 真实命令分发、N11/N17、N22 真实 ZIP 生命周期 worker、故障注入实调父测试）。**先跑基线 FAIL 证据再修**的顺序已不适用（源码已修），改为：新测试若在已修复源码上应 PASS，同时用 `git stash`/旧提交快速抽验关键反例确实曾被复现（或在 WIP 提交前的工作树上验证）——如实记录方式。
3. bridge.py 若引用旧键/mode 需检查（grep 无残留，应该不用动）；`_run_s6_worker` SOURCE_FILES 无 tools/（N22 需新 ZIP 生命周期 worker）。
4. 全量回归（旧 10+新套×2 版）→ 文档同步（ACCEPTANCE 更正上轮 PASS 口径、ADR-015 修订编码、README/CHANGELOG/COMPATIBILITY/HANDOFF/STATUS）→ 新候选 ZIP（13 文件，含 tools）→ Linear In Review + 证据评论。MIS-170 保持 In Progress 等 Codex。

### 恢复步骤

1. `git log --oneline -2` 应见 WIP 提交（"W1-W8 返工 WIP：源码阶段"）；`git status` 干净。
2. 按上面"未完成"顺序继续；先改 tools，再测试，跑 `PYTHONPATH=. <venv>/Scripts/python.exe -X utf8 tests/p2_ledger_check.py`（应过）与 n1（需改后）。
3. 语义要点速查：apply_scope_mode 首次记录返回 (gen, False)；effective_optout user 分支不做 has_any；迁移备份名 `pre-migrate-v3-*`；membership scheme=3。

---

## 0.7.0 候选交付（2026-09-19，已被 W1-W8 返工取代，历史保留）

<details>
<summary>0.7.0 首个候选（219cef2，Codex 验收未通过，见 23 号验收报告）</summary>



**分支 `feat/0.7.0-persona-records`（基于 main/859f18e）。N0-N4 本地开发与自测闭环；未 push、未发布、实机未验证。**

### 版本与证据对应

| 项 | 值 |
| --- | --- |
| 交付提交（实现+文档） | `27aadae`（N4 提交，含 main.py 回归修复/文档/版本 0.7.0） |
| 候选包 | `release/astrbot_plugin_user_context_bridge-27aadae.zip`（13 文件白名单，含 `tools/uctx_records.py`） |
| 包 SHA-256 | `fb53a72d692b589b22b5b8402f7f07d72f5d0c485b3444b4121c957643f46497` |
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
