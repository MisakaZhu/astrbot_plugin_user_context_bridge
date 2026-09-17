# HANDOFF（交接说明）

面向 Codex 独立验收（MIS-144）与用户实机验证（MIS-145）。对应最终提交见下（证据均对应该提交）。

## 交付摘要（P7 更新）

- 本地候选版本：0.1.0，默认分支 main，无远端、未 push（GitHub 上传由用户另行决定）。
- 最终提交：见「阶段历史」末行（docs/STATUS.md）；工作区干净。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（本地忽略目录，不入 Git）；SHA-256 见交付报告（Linear MIS-143 评论）与 release/ 内校验文件。
- 支持声明：QQ OneBot v11（aiocqhttp）+ AstrBot 内置 Agent，宿主 4.26.0 / 4.28.0（本地实测）。未实测版本不宣称。

## 复现

```bash
# 七套测试（4.26.0 与 4.28.0 各一遍）
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check \
         p3_bridge_flow_check p4_concurrency_check p5_commands_check \
         p6_packaging_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 期望：每套 PASS=x FAIL=0；两版宿主各 151 项断言通过
```

## 关键决策入口

- 宿主调用链事实与实证：docs/ADR.md ADR-001；tests/p0_lifecycle_check.py。
- 唯一权威历史源与写回短路（req.conversation=None）：ADR-002；tests/p3（SHORT.host-writeback-short-circuited + OOS.host-writes-back 对照）。
- 轮次终态机三轨（on_agent_done 缓存 → on_decorating_result 唯一提交 → 恢复轨）：ADR-003；P0 的 V4/V5 实证是设计依据。
- 并发/epoch/身份/范围：ADR-004~006；原生 /reset、/new 边界：ADR-008。
- A01-A18 逐项映射：docs/ACCEPTANCE.md。

## 验收建议（Codex，MIS-144）

1. 复跑七套测试（两版宿主）核对 151×2。
2. 重点核查：
   - ADR-002 短路是否有未覆盖宿主路径（webchat 分支、third_party 执行器均不在 v1 支持范围）；
   - req.conversation=None 的全部宿主引用面（P0 源码核对 + 运行时验证双证据）；
   - bridge 提交点时序（on_decorating_result 在 _save_to_history 之后由 pipeline 顺序保证——插件提交不依赖宿主写回，因后者已短路）；
   - 并发与 epoch 防复活的落库证据（p2/p4 的 SQLite 级断言）。
3. 交付包：核对 ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

1. 测试环境 AstrBot（4.26.0 或 4.28.0）+ aiocqhttp + 测试 QQ。
2. 安装 ZIP（或复制源码目录）→ WebUI 启用插件 → 配置 shared_groups（两个测试群）+ include_private。
3. 同一测试 QQ：群A 提问（含可回忆事实）→ 群B 追问 → 私聊追问 → 回群A 总结；验证接续与回复窗口。
4. 另一 QQ 同群发言，验证互不串扰。
5. 重启 AstrBot → 继续追问，验证历史保留。
6. /uctx reset → 追问验证清空；/uctx off → 新消息不共享 → /uctx on 恢复接续（停用期间消息不在）。
7. WebUI 停用插件 → 群/私聊恢复原生会话行为；重新启用不回灌停用期间消息。
8. 记录：宿主版本、插件提交 SHA、各步结果；原始聊天留本地，对外只报结论。

## 待用户决定

- 新代码开源许可（发布前确认；当前标注待选择）。
- GitHub 仓库名称、远端与上传（P7 后另行决定，本仓库无 remote）。

## 已知限制

- v1 不自动迁移既有 QQ 历史（从启用后的有效对话开始积累）。
- 原生 /reset、/new 不联动共享 epoch（ADR-008；用 /uctx reset）。
- 同库双 AstrBot 实例并发写不支持（租约检测 + 禁用降级）。
- 语音/引用消息的端到端链路、GUI 级停用/卸载、独立实例 GUI 安装：待实机验证（ACCEPTANCE.md 未覆盖项）。
- 其他插件若直接改写 req.contexts（而非 system_prompt/extra parts），与本插件的上下文替换语义存在覆盖竞争——共存放管路径已在 ADR-002 声明，实测关系/风格类插件（system_prompt 注入）共存无冲突（p5 A15）。
