# 更新日志

## 0.2.0（返工候选，待 Codex 复验）

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
