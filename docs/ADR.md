# 架构决策记录（ADR）

状态标记：Proposed / Accepted（P0 验证后）/ Superseded。

## ADR-001：宿主调用链事实（4.26.0 与 4.28.0 实测源码核对）

日期：2026-09-17。结论基于对本地 AstrBot 4.26.0 / 4.28.0 包的只读源码核对与 P0 脱网运行验证（见 docs/evidence/）。

内置 Agent 每轮处理链（两版一致）：

1. `InternalAgentSubStage.process`（pipeline/process_stage/method/agent_sub_stages/internal.py）
   - 按 `event.unified_msg_origin` 获取宿主会话锁（同 UMO 串行；不同 UMO 并发）。
   - `build_main_agent(..., apply_reset=False)`：
     - `req.contexts = json.loads(conversation.history)`（原窗口历史）；
     - 人格解析 `_ensure_persona_and_skills`：persona prompt 进 `req.system_prompt`；人格开场白 `begin_dialogs` 以 `req.contexts[:0] = begin_dialogs` 插入头部；
     - 返回 `agent_runner`、`req`、`provider`、未执行的 `reset_coro`。
   - `call_event_hook(OnLLMRequestEvent, req)`：**插件钩子窗口**。返回 truthy 会中止本轮（不 reset、不运行）。
   - `await reset_coro`：Runner.reset 消费 `req.contexts` 构建 `run_context.messages = [system] + contexts + [本轮 user]`。
   - `run_agent(...)` 流式/非流式执行模型与工具循环。
   - 模型完成（含异常路径手动触发）时执行 `MainAgentHooks.on_agent_done` → 依次触发 `OnLLMResponseEvent(llm_response)` 与 `OnAgentDoneEvent(run_context, llm_response)`。**该时机早于宿主持久化**。
   - 生成器耗尽后 `final_resp = agent_runner.get_final_llm_resp()`；`if not event.is_stopped() or agent_runner.was_aborted(): await _save_to_history(...)`。
   - `_save_to_history`：**开头 `if not req or not req.conversation: return`**；否则将 `run_context.messages`（去 initial system、去 `_no_save`）整段 `conv_manager.update_conversation(umo, req.conversation.cid, history=...)` **覆盖写回原窗口会话**。

关键差异路径（已由 tests/p0_lifecycle_check.py 在 4.26.0/4.28.0 双版本实测证实，17/17 PASS）：

- 模型异常：Provider 异常被 Runner fallback 层捕获并转为 `role="err"` 响应（`All chat models failed: ...`），step 以 ERROR 态结束——**不触发 on_agent_done**；`final_llm_resp.role == "err"`，错误消息进入事件结果链。
- run_agent 级异常（step 生成器外逃逸的异常）：run_agent 捕获后手动 `on_agent_done(run_context, LLMResponse(role="err"))`。
- 用户中止（aborted）：step 产出 aborted 响应，run_agent aborted 分支 return 并设置 `event.extra["agent_user_aborted"]=True`。**on_agent_done 仍会触发但形态因版本而异**：4.28 注入打断标记对（user 请求 + assistant `USER_INTERRUPTION_MESSAGE`）并以 marker 响应回调；4.26 fallback 层无停止检查（中止依赖 provider 协作 `abort_signal`），回调可能是空 assistant 或**完整回复文本**。两版中 `agent_user_aborted` 旗标都在 on_agent_done **之后**写入。
- `req.conversation` 在 Runner.reset / step 中的全部引用仅为 `token_usage` 读写，均有 `if req.conversation` / `else 0` 防护——置 None 不影响模型运行。

## ADR-002：唯一权威历史来源 = 插件自身 SQLite 轮次账本，宿主写回以 `req.conversation = None` 短路

**决策**：范围内轮次由插件仓储接管读写。

- 读：`on_llm_request` 中将 `req.contexts` 替换为 `人格 begin_dialogs + 当前 epoch 共享历史`；`req.system_prompt`、`req.func_tool`、`extra_user_content_parts` 原样保留（人格/系统规则/工具/其他插件动态内容不受影响）。
- 写：同一钩子中置 `req.conversation = None`。后果（按 ADR-001 源码事实）：
  - 宿主 `_save_to_history` 直接 return——合并历史**不会**覆盖写回原窗口，无双写、无停用后回灌；
  - Runner 对 None 已有防护，模型运行与统计不受影响；
  - 人格已在 build 阶段（钩子之前）注入，本轮人格不丢失。
- 插件写侧时机：`OnAgentDoneEvent`（成功与异常路径都触发，携带完整 `run_context.messages` 轨迹与终态 `llm_response`）为主终态钩子；`OnDecoratingResultEvent` 兜底处理 aborted（该路径不触发 on_agent_done，以 `event.get_extra("agent_user_aborted")` 识别）；`OnAfterMessageSentEvent` 标记发送状态。**不以钩子触发当作宿主写回完成**——因为宿主写回已被短路，插件自身的原子事务提交才是本轮持久化完成点。
- 未采纳方案：复用宿主独立共享 conversation——多窗口并发时宿主整段覆盖写会互相覆盖（丢历史），且 `_save_to_history` 时机不受插件控制；仅改 `req.session_id` 不解决写回目标。

**回补人格开场白**：宿主在 build 阶段把 `begin_dialogs` 插入 `req.contexts[:0]`；插件替换 contexts 时需自行调用 `persona_manager.resolve_selected_persona(...)`（与宿主同参）取 persona 并回补 begin_dialogs，保持人格行为一致。begin_dialogs 与临时提示词不持久化进共享账本（写入时剔除），避免反复累积。

## ADR-003：轮次终态机与去重（R2 返工后 v2）

每轮在插件库建立轮次记录（turn）：`event_key` 唯一约束（事件 ID 派生），状态机：

```
running --on_agent_done(真实 assistant 回复)--> completed
running --on_agent_done(err / 打断形态)-------> failed / aborted（见下）
running --on_decorating_result 兜底终态化-----> failed / aborted / interrupted
running --插件重启扫描---------------> interrupted
终态幂等：重复完成通知以 event_key 吸收，一轮只写一次
```

**写侧终态机 v2（独立验收 R2 返工：宿主在 run_agent 中间 yield 时即执行装饰阶段，
工具轮的中间输出会触发 decorating；aborted 无输出路径不触发任何下游钩子——
on_decorating_result 不能作为整轮提交点）**：

- 主提交点 `OnAgentDoneEvent`（成功 / aborted / run_agent 级异常均触发且轨迹完整）：
  - `role == "err"` → failed；
  - `event.is_stopped()` → aborted（用户停止是中止的决定性信号，覆盖 4.28 marker
    与 4.26「aborted 回调保留完整/空文本」两种形态）；
  - 文本空且无工具调用 → failed（与宿主一致：空回复不成功）；
  - 其他 → completed。
- 辅助轨 `OnDecoratingResultEvent`：仅做模型 err 加速检测（done_seen 为假且事件
  结果文本为宿主错误文案「LLM 响应错误…」时立即 failed）。
- 辅助轨 `OnAfterMessageSentEvent`：send_state 标记 + 停止旗标兜底。
- fail-watchdog（默认 180s 可配）：登记时启动，仍 running（模型 err 不触发完成钩子/
  模型挂起/钩子缺失）→ 受控 failed 并释放身份锁。
- 恢复轨：插件 terminate 未决 → interrupted；重启由账本 recover_running 兜底。

- 失败（err）不生成成功回复记录；重试产生新 event_key 新轮次，不重复入库。
- aborted 保留已产出内容但带 aborted 状态，不作为完整成功轮次。
- 轨迹提取：on_llm_request 时记录本轮 user 消息内容指纹；on_agent_done 从 `run_context.messages` 尾部定位该指纹的 user 消息，其后全部消息（工具调用/结果/最终 assistant）即本轮轨迹。指纹定位而非位置切片，规避 Runner 上下文压缩对头部的裁剪；4.28 aborted 注入的打断标记对（USER_INTERRUPTION_REQUEST 文本）不入共享账本。
- 多模态：入库时 image_url/audio_url 的 base64/本地路径降级为文本占位（`[图片]`/`[音频]`），防库膨胀与失效临时文件引用（A14）。不保存 reasoning_content（隐藏思考）。

## ADR-004：并发与顺序（R3 返工后 v2）

- 宿主会话锁按 UMO 串行同窗口；跨窗口同身份并发由插件层协调：
  - 进程内：每共享身份一把 `asyncio.Lock`，**自读取历史快照前获取，持有至本轮终态化
    （completed / failed / aborted / interrupted）释放**（R3 返工：锁只在读侧短临界区
    持有会导致后继轮读取不到前一未完成轮次）；fail-watchdog 保证模型挂起时受控
    释放，不同身份锁独立不互相阻塞；
  - 跨连接/进程：SQLite `BEGIN IMMEDIATE` 事务 + WAL；写入以 `(identity, epoch, seq)` 排序，seq 在身份内自增；
  - 同库双 AstrBot 实例：插件启动时在库内登记实例租约（含 PID 与时间戳），检测到活动租约冲突时拒绝启用共享并以只读告警——明确不支持同库双实例同时写入，不假装跨进程安全。
- 慢请求与新轮次：新轮次读取的是提交事务里的已提交轮次快照；进行中的旧轮次完成后以更大 seq 追加，不覆盖已有数据。

## ADR-005：清空（epoch）

- 每共享身份维护 `current_epoch`（插件库 meta 表）。
- `/uctx reset`（及 P5 对齐的原生 /reset、/new 语义）执行：epoch+1 并把旧 epoch 数据标记归档；新轮次只读写新 epoch。
- 清空前的慢请求：登记轮次时记录 epoch；完成写入时校验 epoch 仍为当前值，否则整轮丢弃（不把旧历史复活）。
- reset 只影响本人身份；其他身份不受影响。

## ADR-006：范围与身份

- 共享身份键：`(platform_id, self_id, persona_scope, sender_id)`。`persona_scope` 取宿主 `persona_manager.resolve_selected_persona` 解析结果的人格 ID，无人格时用稳定常量 `__default__`（默认人格也有稳定身份值）。
- 来源范围（受控）：配置显式列出允许的群号列表与"是否含该用户私聊"；插件默认关闭，无授权来源不采集不注入。管理员配置范围；个人 on/off 只能退出/加入，不能扩大范围。
- 归属判定以**本轮事件的实际 sender** 为准（`event.get_sender_id()`），不从昵称或全群历史推断；未唤醒闲聊（`is_wake`/`is_at_or_wake_command` 为假且未进入 LLM 流程）、机器人自身消息、管理命令不进入共享轮次。
- `event.unified_msg_origin` 永不修改；回复仍发回触发窗口（插件不触碰消息路由）。

## ADR-007：支持边界

- v1 支持声明：QQ OneBot v11 / aiocqhttp 适配器 + AstrBot 内置 Agent 路径，宿主 4.26.0 与 4.28.0（以真实宿主集成测试证据为准；未实测版本不宣称）。
- 明确不支持（不自动宣称兼容）：QQ 官方适配器、Dify/Coze 等第三方会话执行器（third_party 路径）、WebChat 专用行为。
- 插件数据位于 AstrBot 插件数据目录（`StarTools.get_data_dir`），与 Git 源码分离；日志默认不输出对话原文。

## ADR-008：原生 /reset、/new 的语义与边界

- 宿主原生 `/reset` 仅清空当前 UMO 的宿主会话（conversation history 置空），群聊场景下该 UMO 代表全群窗口且权限场景（unique_session / alter_cmd 配置）随版本与配置漂移。
- **v1 决策：不注册同名 /reset、/new 命令做 epoch 联动**。理由：镜像宿主权限判定会复制随版本漂移的配置逻辑；若权限判定与宿主不一致，可能出现「宿主拒绝但共享历史被清」或反之的越权/失效场景，比不联动更糟。
- 实际语义（README 明示）：原生 /reset、/new 只重置当前窗口的宿主会话；插件的共享历史清空使用 `/uctx reset`（本人维度、epoch 切换）。/uctx reset 的回复中同时提示两者区别。
- 未支持项：原生 /reset 联动切换共享 epoch。若宿主后续提供稳定接口（如会话重置事件钩子），在后续版本实现。

## ADR-009：实例租约的运行时身份与归属校验（R6 返工）

- 租约 ID 改为**每次插件加载运行时生成**（uuid），不再持久化到数据目录——两个实例
  从同一文件读到同一 ID 会绕过冲突检查并互相覆盖。
- `instance_leases` 增加 `owner_token`：`acquire` 返回本次调用的 token，`heartbeat/
  release` 必须携带正确 token——活跃实例不能被另一实例心跳保活、释放或覆盖。
- 崩溃后重启：旧租约（不同运行时 ID）在心跳新鲜窗口内仍被视为活跃，新实例禁用
  共享并告警，直至租约过期（有意保守：无法确定旧进程已死）。热重载（同进程
  terminate→initialize）先 release 再 acquire，不受影响。
- 多进程排他已由真实双子进程验证（第一实例 acquire 成功、第二实例被拒）。

## ADR-010：原生 /reset、/new 联动（R7 返工，取代 ADR-008 的 v1 决策）

- 插件注册同名单 `/reset`、`/new` 命令 handler，与宿主 builtin 命令共存（不拦截、
  不覆盖宿主回复，独立 send 提示）。
- 联动条件：共享启用 + 发送者共享身份在管理员范围内且未退出（**用窗口范围判定，
  不用对话轮次的 evaluate**——命令消息本身被其排除）。
- `/reset` 权限镜像宿主 builtin（两版逻辑一致）：unique_session + scene +
  alter_cmd 配置 + role 检查 + 当前会话存在；宿主会拒绝的场景不联动（避免越权
  清空共享历史）。`/new` 无宿主权限门槛，范围内即联动。
- 联动动作：切换该身份 epoch（原生 reset/new 语义 = 开新上下文，同步对共享历史
  生效）+ 提示「仅影响本人」。未共享用户：不动作不加提示，宿主原行为。

## ADR-011：二次验收 S1~S6 修复（终态机 v3）

- **S1 重复投递**：已终态事件的重复接管在返回前必须释放刚获取的身份锁，并对事件调用宿主公开的 `event.stop_event()` 终止传播——宿主钩子协议在 `is_stopped` 后由 internal 阶段直接返回，防止对重复消息二次执行模型。
- **S2 真实 /stop**：宿主 `/stop`（ActiveEventRegistry.request_agent_stop_all）只置 `agent_stop_requested` extra、不置 `is_stopped`；停止判定取宿主 `_should_stop_agent` 同源并集（is_stopped / agent_stop_requested / agent_user_aborted）。
- **S3 watchdog 受控失败覆盖执行**：watchdog 触发时先对轮次事件设置 `agent_stop_requested`（宿主 /stop 同款信号），run_agent 的 stop watcher 请求停止——4.28 形态为模型调用被取消/aborted 收尾；4.26 形态为 step 后置检查使迟到的 resp 被 run_agent 停止分支吞掉（不发送、不产出）。两种形态下旧轮均不再影响用户与存储，之后才落账 failed 并释放身份锁。仅作用于本轮事件，不波及同 UMO 其他用户的活跃轮次。
- **S4 正文前缀非程序终态**：删除「事件结果文本以宿主错误文案开头即判失败」的启发式——模型可以生成任意开头的正文。模型 err（不触发完成钩子）统一由 fail-watchdog 受控收尾；装饰钩子仅保留停止旗标兜底。
- **S5 原生 reset 镜像补全**：`_host_reset_would_run` 补齐宿主拒绝分支——第三方执行器（4.26/4.28 字段位置差异兼容）与可用模型提供方检查；宿主拒绝（无 provider 等）时不得清空共享历史。
- **S6 真实 PluginManager 生命周期（A17 本地完成）**：tests/s6_plugin_lifecycle_worker.py 以子进程隔离实例（ASTRBOT_ROOT 临时根、data/plugins 安装、data/config 预建——4.26 的 AstrBotConfig 不自动建目录）驱动真实 PluginManager.load/reload/uninstall_plugin 与 turn_off/turn_on：加载绑定 12 个 handler、默认关闭不采集、启用配置重载生效、真实钩子下群A→群B 接续、活动轮挂起时卸载将 pending 终态化 interrupted 且注册表与插件目录清理。

## ADR-012：三次验收 T1~T6 修复

- **T1 人格同源解析**：`resolve_persona_scope` 增加 `provider_settings` 参数并与宿主 `_ensure_persona_and_skills` 同参（4.26 在 conversation.persona_id=None 时只从该参数读默认人格，漏传把不同人格折叠为同一身份）；bridge/commands 经 `provider_settings_getter`（宿主 `get_config(umo)["provider_settings"]`）注入。解析失败或结果为空抛 `PersonaResolutionError`：对话轮受控跳过（不接管、不折叠 __default__）、命令返回明确错误文案——不静默合并身份。开场白回补走同一 resolver 同一参数（身份与开场白同源）。
- **T2 锁后取消窗口**：pending 与 watchdog 的登记提前到人格开场白解析（锁后唯一可等待点）之前；`except asyncio.CancelledError` 分支：轮次终态化 interrupted + 全套停止信号（见 T4）+ 释放身份锁 + `event.stop_event()`（宿主 call_event_hook 吞掉取消继续流程，is_stopped 阻止其后的模型执行）后 re-raise 保持取消语义。
- **T3 关闭协议**：`shutdown()`——置 `_closing`（新请求与排队获锁者干净让出并终止事件传播，不触碰将关账本、不以异常回退成继续执行）→ 每个活动轮全套停止信号 + interrupted + 释放锁（唤醒排队者）。main.terminate 顺序：shutdown → 释放租约 → 关账本。真实 PluginManager 的 turn_off/turn_on/reload/uninstall 与「挂起活动轮 + 同身份跨窗排队轮」经子进程 worker 验证。
- **T4 缓冲正文抑制**：停止信号升级为全套（`agent_stop_requested` + `stop_event`）——宿主 scheduler 在 yield 暂停点检测 is_stopped 后不再执行后续阶段（respond），run_agent aborted 分支交出的缓冲旧正文（buffer_intermediate_messages=True）到不了下游；另在 decorating 钩子对本轮已 failed/interrupted 的残留 result `clear_result()` 双保险。
- **T5 版本兼容**：provider 检查按真实 Context 实例的接口存在性选择（`get_using_provider_async` 或同步 `get_using_provider`），4.26 真实 Context 无 async 接口（探针证实）走同步。
- **T6 原生命令成功关联（ADR-010 v2）**：删除同名单 `/reset`、`/new` 处理器（与宿主实际执行脱节：禁用误清/旧名误清/新名漏清）。改为 **decorating 后置事实关联**：宿主 builtin `reset`/`new_conv` 处理器在本事件 `activated_handlers` 中（WakingCheckStage 结构化激活证据，天然涵盖权限/禁用/改名/自定义过滤）**且** 事件结果为宿主 builtin 固定成功文案（"✅ Conversation reset successfully"/"✅ Switched to new conversation"，程序生成字面量、两版一致、非模型正文）时，经轻量防御（provider+会话存在）后切换发送者共享身份 epoch。时机在 StarRequestSubStage 的 yield 窗口（result 仍存在、respond 未执行），即真实管线中插件 on_decorating_result 钩子的触发点。
