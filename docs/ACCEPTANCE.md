# 验收矩阵映射（A01-A18，返工后 v2）

对应 0.2.0 返工候选（独立验收 R1~R7 返工后）。每行给出：场景 → 落点断言（最终模型请求 /
实际持久化 / 回复目标）→ 测试文件与断言名 → 状态。R 系列映射来自 Codex 独立验收报告。

测试运行（两版宿主各跑一遍，合成数据，假模型/假传输；r_rework_check 使用真实
PipelineScheduler + ResultDecorateStage 调度链）：

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
| A04 | 不同机器人/平台实例/人格 | 同 QQ 号在 self_id/platform_id/persona_scope 任一维度变化时身份键不同 | p1: A04.same-qq-different-{bot,persona,platform}-isolated | PASS（双版本） |
| A05 | 未启用来源、关闭共享、退出 | 判定层不采集；范围外轮次宿主原生写回；命令 off/on 生效且作用于正确身份（R5）且个人不能扩大范围 | p1: A05.*；p3: OOS.*；p5: A05.cmd-* / personal-on-cannot-expand；r: R5.off-stops-selected-persona | PASS（双版本） |
| A06 | 非隔离群 UMO、同名用户、普通闲聊 | 归属按真实 sender（昵称无关）；未唤醒/机器人自言/管理命令/空 sender 排除 | p1: A06.*（nickname-irrelevant / group-umo-attributed-by-sender / bot-self / command / not-wake / empty-sender） | PASS（双版本） |
| A07 | 重复事件、重试、重复完成通知 | event_key 唯一约束：一轮一记录，终态不翻转；闭环层每轮一对 user/assistant | p2: A07.dup-event-same-turn / dup-commit-idempotent / terminal-not-flipped / single-pair；p3: A01.turn2-no-duplicate | PASS（双版本） |
| A08 | 跨窗口同时发消息 | 同身份锁覆盖至终态（R3）：后继轮等待前轮完成且请求含前轮问答（屏障验证）；不同身份并行（计时）；全部落账无覆盖 | r: R3.second-waits-for-first / second-sees-first-question-and-answer / different-identity-parallel；p4: A08.*（22 项栈） | PASS（双版本） |
| A09 | 多连接并发与同库双实例 | 4 连接并发 24 轮无丢失无覆盖；双实例防护（R6）：运行时租约身份 + owner_token 归属校验（伪造 token 的跨实例释放被拒）+ 真实双子进程第二实例被拒 | p2: A09.*；r: R6.runtime-ids-distinct / no-cross-release / lease-still-active / owner-release-ok / multiprocess-exclusive | PASS（双版本） |
| A10 | 模型报错、超时、取消、发送失败 | 真实调度链（R2 v2）：工具中间输出不提前提交、err 文本快速 failed、无钩子 err 由 watchdog 受控失败、模型前取消终态 aborted 无遗留、空回复 failed、发送状态可区分、卸载 interrupted | r: R2.tool-round-* / intermediate-decorate-not-committed-early / abort-finalized-aborted / err-fast-failed / watchdog-controlled-failure / empty-reply-failed / terminate-interrupted；p4: A10.*；p2: A10.failed-aborted-not-in-history | PASS（双版本） |
| A11 | reset/new 期间慢请求（含原生命令联动） | 存储层 epoch 防复活 + bridge 慢请求吸收 + 命令 reset 仅本人；**原生 /reset、/new 联动（R7）**：admin /reset 与 /new 切换 epoch、权限镜像（非 admin 群聊不联动）、范围外不动作 | p2: A11.*；p4: A11.*；p5: A11.cmd-*；r: R7.permission-mirrored-no-bump / admin-reset-bumps-epoch / new-bumps-epoch / out-of-scope-untouched | PASS（双版本；原生联动为组件级，实机补充） |
| A12 | 重启、重载、异常中断恢复 | 已提交历史保留；遗留 running → interrupted 不入历史；恢复幂等 | p2: A12.*；p4: A12.*（4 项） | PASS（双版本） |
| A13 | 长上下文裁剪 | max_turns 尾部保留最近 N 轮（不破坏轮次完整性） | p2: A13.trim-keeps-recent / full-untrimmed | PASS（双版本） |
| A14 | 图像/语音/引用与工具结果 | base64/本地路径不入库不入请求（[图片]/[音频] 占位）；真实两步工具轮的调用/结果配对入库且下一轮可续（R2/R4）；临时 extra 内容不入库（R4） | p2: A18.tool-structure-kept；p3: A14.*；r: R2.tool-trajectory-pairs / R4.transient-excluded / R4.next-request-contains-pair | PASS（双版本；语音/引用为宿主装配路径的间接验证，实机补充） |
| A15 | 多插件共存 | 其他插件 system_prompt 动态注入在接管后保留；人格/系统提示进入最终模型请求；开场白单次注入；宿主 mark_as_temp 临时注入不永久污染共享历史 | p5: A15.other-plugin-prompt-kept；p3: A15.*；r: R4.transient-excluded | PASS（双版本） |
| A16 | 停用/卸载与重新启用 | 停用后不采集不注入、原生行为恢复；重启用后停用期间消息不回灌；卸载/重载未决轮次 interrupted（真实调度链验证） | p5: A16.*；r: R2.terminate-interrupted | PASS（双版本；GUI 级停用/卸载待实机） |
| A17 | 插件打包安装 | R1：宿主 `data.plugins.<name>.main` 真实包路径导入（实例根入 path，不注入插件根）；宿主真实校验器通过 metadata/_conf_schema；ZIP 清单=白名单 | p6: A17.module-importable（宿主式）/ metadata-valid / config-schema-valid / zip-manifest；r: import_plugin_module（r_rework 全程经宿主式导入驱动 R6/R7） | PASS（双版本；GUI 安装待实机） |
| A18 | 发布准备与隐私 | Git 跟踪清单无 db/log/zip/venv/缓存/真实配置；ZIP 同样干净 | p6: A18.git-manifest-clean / zip-manifest-clean | PASS（双版本） |

## 未覆盖 / 待实机项

- **A14 语音、引用消息**：宿主装配路径（Record/Reply → 附件占位）由 P0 源码核对与请求装配共用路径间接覆盖；真实 QQ 语音/引用端到端待 MIS-145 实机验证。
- **A11 原生联动的实机确认**：与宿主 builtin /reset 在真实命令分发中的共存顺序（多 handler 依次执行）已按源码核对设计，实机演练确认最终用户体验。
- **A16 GUI 级停用/卸载**（WebUI 开关、插件管理页卸载）与 **A17 独立实例 GUI 安装**：本地已验证宿主式包路径导入、元数据/配置经宿主真实校验器、包清单干净；完整 GUI 流程待 MIS-145 用户实机（独立离线实例）执行。
- **真实 QQ 演练**（跨群/私聊接续、重启、reset、停用恢复）：按主计划单列「待用户执行」，合成测试不冒充实机通过。
- **fail-watchdog 默认 180s**：无钩子的模型 err 轮最长 180s 后落 failed（不影响历史读取，其只认 completed）；可经构造参数调整，实机可观察是否需缩短。
