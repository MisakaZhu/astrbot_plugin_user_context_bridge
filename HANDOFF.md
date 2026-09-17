# HANDOFF（交接说明）

面向 Codex 复验（MIS-144）与用户实机验证（MIS-145）。对应最终提交见 docs/STATUS.md；证据均对应该提交。

## 交付摘要（S1~S6 二次返工后，0.3.0）

- 版本 0.3.0（二次返工候选），main 分支，无远端、未 push。
- 基线链：8477eba（一轮）→ d8a7147（R1~R7）→ 本次（S1~S6，ADR-011）。
- 测试：9 套 × 4.26.0/4.28.0 各 **221 项断言全部 PASS**（从日志加总：P0=17 P1=35 P2=27 P3=19 P4=22 P5=16 P6=8 R=37 S=40）；新增 tests/s_rework_check.py（40 项/版，含真实 PluginManager 生命周期子进程 S6）。
- 可安装包：`release/astrbot_plugin_user_context_bridge-<final-sha>.zip`（12 文件白名单）；SHA-256 见交付报告与同目录 .sha256 文件。

## 二次返工要点（对应二次验收报告 S1~S6）

| 项 | 根因 | 修复 | 证据 |
| --- | --- | --- | --- |
| S1 重复投递锁泄漏 | 已终态事件重复接管直接 return，锁未释放且宿主可能二次执行 | 释放刚获取的身份锁 + `event.stop_event()` 终止传播 | s: S1.*（重复不重接、传播终止、锁已释放、后继轮实际完成且含前轮问答） |
| S2 真实 /stop 记 completed | /stop 只置 agent_stop_requested，不置 is_stopped | 停止判定取 `_should_stop_agent` 同源并集（三信号） | s: S2.*（真实 ConversationCommands.stop + 活动注册表：aborted 终态、历史/输出干净、锁释放） |
| S3 watchdog 不停旧执行 | 只改账本放锁，旧 Runner 迟到输出仍发送 | watchdog 先设 agent_stop_requested（宿主 /stop 同款信号）再收尾；两版形态（取消/吞输出）均无迟到输出 | s: S3.*（信号、旧执行停止、无迟到输出、failed 落账、后继干净且完成） |
| S4 正文前缀误判失败 | 模型可生成「LLM 响应错误」开头的正常正文 | 删除文本前缀启发式；err 统一 watchdog 收尾 | s: S4.*（诊断文本工具轮 completed + 真实/自定义 err 均受控 failed） |
| S5 宿主拒绝仍清空 | 镜像漏 provider 与第三方执行器检查 | 补全宿主拒绝分支（字段两版兼容） | s: S5.*（真实 reset：无 provider 拒绝不清空、成功联动、权限、停用） |
| S6 A17 本地缺口 | 无真实 PluginManager 生命周期证据 | 子进程隔离实例：安装→load→默认关闭→配置 reload→真实钩子接续→挂起轮卸载→清理 | s: S6.<venv/venv426>.*（各 5 项，两版通过） |

## 复现

```bash
for t in p0_lifecycle_check p1_identity_scope_check p2_ledger_check          p3_bridge_flow_check p4_concurrency_check p5_commands_check          p6_packaging_check r_rework_check s_rework_check; do
  PYTHONPATH=. <venv>/Scripts/python.exe tests/$t.py
done
# 期望：每套 FAIL=0；两版宿主各 221 项断言通过（s_rework 含子进程 S6，
# 需能创建临时目录；4.26 首载需 data/config 预建——worker 已内置）
```

## 复验建议（Codex，MIS-144）

1. 复跑 9 套测试（两版宿主）核对 221×2（从日志逐项加总）。
2. 重点核查：
   - S2/S3 的停止信号协议与宿主 `_should_stop_agent`/`request_agent_stop_all` 的一致性及两版形态差异（4.28 取消 vs 4.26 吞输出）；
   - S1 的全部退出路径（重复/登记失败/取消/卸载）无无主锁——bridge 中锁释放在 `_release` 闭包唯一出口；
   - S5 镜像与宿主 builtin reset 逐分支对齐（conversation.py 两版）；
   - S6 worker 的隔离性（ASTRBOT_ROOT 临时根、子进程退出即销毁、无全局污染）。
3. 交付包：ZIP 清单与 SHA-256。

## 实机演练清单（用户，MIS-145，脱敏）

同前版（HANDOFF 历史）+：真实 `/stop` 中止长回复后跨窗口继续；宿主无可用模型时 `/reset`（应只见宿主拒绝提示，共享历史不清空）。

## 待用户决定

- 新代码开源许可（发布前确认）。
- GitHub 仓库名称、远端与上传（本仓库无 remote）。

## 已知限制

- v1 不自动迁移既有 QQ 历史。
- 同库双实例：第二实例禁用；崩溃后重启需等旧租约心跳过期（默认 300s，有意保守）。
- fail-watchdog 默认 180s：无钩子的模型 err 轮最迟此时落 failed（不影响读取；S3 修复后旧执行被停止信号切断）。
- 原生命令联动：命令改名/过滤组合（alter_cmd 别名机制作用于插件命令的最终行为）以源码核对 + 组件级真实 reset 验证覆盖，实机确认列入 MIS-145。
- 语音/引用端到端、GUI 级停用/卸载、真实 QQ 演练：待 MIS-145。
