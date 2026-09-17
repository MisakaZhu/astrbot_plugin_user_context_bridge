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

## ADR-003：轮次终态机与去重

每轮在插件库建立轮次记录（turn）：`event_key` 唯一约束（事件 ID 派生），状态机：

```
running --on_agent_done(真实 assistant 回复)--> completed
running --on_agent_done(err / 打断形态)-------> failed / aborted（见下）
running --on_decorating_result 兜底终态化-----> failed / aborted / interrupted
running --插件重启扫描---------------> interrupted
终态幂等：重复完成通知以 event_key 吸收，一轮只写一次
```

**写侧三轨设计（P0 实证驱动）**：

- 轨 1（主）`OnAgentDoneEvent`：成功路径在此终态化（携带完整 `run_context.messages` 轨迹）。对回调内容做形态判定：`role=="err"` → failed；`completion_text` 为空或等于该版本 `USER_INTERRUPTION_MESSAGE` 常量 → aborted 候选。
- 轨 2（兜底，必须）`OnDecoratingResultEvent`：on_agent_done **不覆盖**的路径——模型 err（两版均不触发完成钩子）、on_agent_done 内无法确认的 aborted（4.26 可能回调完整回复文本，且 `agent_user_aborted` 旗标在钩子后才写入）。此阶段旗标已可用：`agent_user_aborted` 为真 → aborted；事件结果为错误文案/无 on_agent_done 记录 → failed。
- 轨 3（恢复）插件启动扫描：仍处 running 的轮次 → interrupted（不产生伪成功、不重复回复）。

- 失败（err）不生成成功回复记录；重试产生新 event_key 新轮次，不重复入库。
- aborted 保留已产出内容但带 aborted 状态，不作为完整成功轮次。
- 轨迹提取：on_llm_request 时记录本轮 user 消息内容指纹；on_agent_done 从 `run_context.messages` 尾部定位该指纹的 user 消息，其后全部消息（工具调用/结果/最终 assistant）即本轮轨迹。指纹定位而非位置切片，规避 Runner 上下文压缩对头部的裁剪；4.28 aborted 注入的打断标记对（USER_INTERRUPTION_REQUEST 文本）不入共享账本。
- 多模态：入库时 image_url/audio_url 的 base64/本地路径降级为文本占位（`[图片]`/`[音频]`），防库膨胀与失效临时文件引用（A14）。不保存 reasoning_content（隐藏思考）。

## ADR-004：并发与顺序

- 宿主会话锁按 UMO 串行同窗口；跨窗口同身份并发由插件层协调：
  - 进程内：每共享身份一把 `asyncio.Lock`，仅用于"读取快照 + 登记轮次"的短临界区，不跨模型执行期持有（不阻塞不同身份，也不死等模型）；
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
