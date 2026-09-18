# 更新日志

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
