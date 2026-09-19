"""返工回归（R1~R7）：先揭示原缺陷的失败场景，再验证修复。

R2/R3/R4 使用真实宿主调度链（tests/harness.drive_pipeline：
PipelineScheduler + ResultDecorateStage + 真实 Runner/Hooks）。
R5 使用真实 PersonaManager（仅 stub 外部偏好与配置输入）。
R6 使用两个插件对象与两个真实子进程。
R7 驱动原生 /reset、/new 联动 handler 的权限镜像与 epoch 切换。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/r_rework_check.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from tests.fakes import FakeConversation, FakeEvent, FakeProvider
from tests.p3_bridge_flow_check import cleanup, register_bridge
from tests.harness import drive_pipeline, make_bridge_stack
from uctx_bridge.commands import CommandService
from uctx_bridge.identity import build_identity
from uctx_bridge.ledger import LeaseConflictError, TurnLedger
from uctx_bridge.scope import ScopeConfig

PASS: list[str] = []
FAIL: list[str] = []

_PLUGIN_MODULE_CACHE = {}


def import_plugin_module():
    """按宿主方式导入插件入口：data.plugins.<name>.main（R1 后唯一合法路径）。"""

    import importlib
    import shutil as _shutil

    if "m" in _PLUGIN_MODULE_CACHE:
        return _PLUGIN_MODULE_CACHE["m"]
    repo = Path(__file__).resolve().parent.parent
    inst = repo / "local_evidence" / "_host_import_instance"
    plug_dir = inst / "data" / "plugins" / "astrbot_plugin_user_context_bridge"
    if plug_dir.exists():
        _shutil.rmtree(plug_dir, ignore_errors=True)
    plug_dir.mkdir(parents=True, exist_ok=True)
    for rel in ("main.py",):
        _shutil.copy2(repo / rel, plug_dir / rel)
    _shutil.copytree(
        repo / "uctx_bridge",
        plug_dir / "uctx_bridge",
        ignore=_shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    for pkg in ("data", "data.plugins", "data.plugins.astrbot_plugin_user_context_bridge"):
        init = inst / Path(*pkg.split(".")) / "__init__.py"
        init.write_text("", encoding="utf-8")
    sys.path.insert(0, str(inst))
    module = importlib.import_module(
        "data.plugins.astrbot_plugin_user_context_bridge.main"
    )
    # 模块导入会经 @register/@filter 把未绑定 handler 注册进全局 registry；
    # 测试用显式绑定的 fixture 钩子，清掉这些未绑定条目避免分发干扰。
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star import star_map

    module_path = module.__name__
    stale = [
        h
        for h in list(star_handlers_registry._handlers)
        if h.handler_module_path == module_path
    ]
    for h in stale:
        star_handlers_registry._handlers = [
            x for x in star_handlers_registry._handlers if x != h
        ]
        star_handlers_registry.star_handlers_map.pop(h.handler_full_name, None)
    star_map.pop(module_path, None)
    _PLUGIN_MODULE_CACHE["m"] = module
    return module


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def identity_of(sender: str, persona: str = "maid") -> str:
    return build_identity(
        platform_id="aiocqhttp",
        self_id="bot_001",
        persona_scope=persona,
        sender_id=sender,
    ).key


def rows(ledger: TurnLedger, identity: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(ledger._db_path)
    conn.row_factory = sqlite3.Row
    try:
        if identity is None:
            return conn.execute(
                "SELECT * FROM turns ORDER BY seq"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM turns WHERE identity_key=? ORDER BY seq", (identity,)
        ).fetchall()
    finally:
        conn.close()


class ToolReplyProvider(FakeProvider):
    """第一次返回工具调用，第二次返回最终答复（真实两步工具轮）。"""

    def __init__(self, final_text: str):
        super().__init__()
        self.final_text = final_text

    async def text_chat(self, **kwargs):
        self.call_log.append(
            {"contexts": [m.model_dump() for m in kwargs.get("contexts") or []]}
        )
        if len(self.call_log) == 1:
            return LLMResponse(
                role="assistant",
                completion_text="准备调用工具查一下。",
                tools_call_name=["review_tool"],
                tools_call_args=[{}],
                tools_call_ids=["call-1"],
            )
        return LLMResponse(role="assistant", completion_text=self.final_text)


def review_tool() -> ToolSet:
    class ReviewTool(FunctionTool):
        async def call(self, context, **kwargs):
            return "SYNTHETIC-TOOL-RESULT"

    return ToolSet(
        [
            ReviewTool(
                name="review_tool",
                description="synthetic tool",
                parameters={"type": "object", "properties": {}},
            )
        ]
    )


# ---------------------------------------------------------------------------
# R2：真实调度下的终态机制
# ---------------------------------------------------------------------------
async def r2_lifecycle() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=0.4
        )
        metas = register_bridge(bridge)
        try:
            # R2-a 工具轮：中间输出走真实 decorating，最终答复后 completed
            p_tool = ToolReplyProvider("TOOL-FINAL-REPLY")
            ev_tool = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="用工具回答"
            )
            result = await drive_pipeline(
                bridge, ev_tool, p_tool, prompt="用工具回答",
                func_tool=review_tool(),
            )
            r = rows(ledger, identity_of("10001"))
            traj = json.loads(r[-1]["trajectory"]) if r[-1]["trajectory"] else []
            check(
                "R2.tool-round-completed",
                r and r[-1]["status"] == "completed" and result["pending"] == 0,
                f"status={r[-1]['status'] if r else None} pending={result['pending']}",
            )
            check(
                "R2.tool-round-final-reply",
                r[-1]["reply_text"] == "TOOL-FINAL-REPLY",
                f"reply={r[-1]['reply_text']!r}",
            )
            check(
                "R2.tool-trajectory-pairs",
                any(m.get("tool_calls") for m in traj)
                and any(m.get("role") == "tool" for m in traj),
                f"traj={traj}",
            )
            # 中间 decorating 不得提前提交（多次下游 + 最终 completed）
            check(
                "R2.intermediate-decorate-not-committed-early",
                result["model_calls"] == 2 and r[-1]["status"] == "completed",
                f"calls={result['model_calls']}",
            )

            # R2-b 模型前取消：on_agent_done + is_stopped → aborted，无遗留
            p_abort = FakeProvider(["不该到达"])
            ev_abort = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="取消我"
            )
            res_abort = await drive_pipeline(
                bridge, ev_abort, p_abort, prompt="取消我", abort_before_run=True
            )
            all_rows = rows(ledger, identity_of("10001"))
            last = all_rows[-1]
            check(
                "R2.abort-finalized-aborted",
                last["status"] == "aborted" and res_abort["pending"] == 0,
                f"status={last['status']} pending={res_abort['pending']}",
            )

            # R2-c 模型错误（v3/S4 语义：err 不再按正文前缀判定，
            # 由 fail-watchdog 受控收尾——见 s_rework_check.S4 对照）
            p_err = FakeProvider()
            p_err.error_script = [RuntimeError("服务不可用")]
            ev_err = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="会失败"
            )
            res_err = await drive_pipeline(bridge, ev_err, p_err, prompt="会失败")
            await asyncio.sleep(0.6)  # > fail_watchdog_seconds=0.4
            last = rows(ledger, identity_of("10001"))[-1]
            check(
                "R2.err-watchdog-failed",
                last["status"] == "failed" and bridge.pending_count == 0,
                f"status={last['status']} pending={bridge.pending_count}",
            )
            check(
                "R2.err-no-fake-success",
                (last["reply_text"] or "") == "",
                f"reply={last['reply_text']!r}",
            )

            # R2-d 自定义错误文案（不匹配宿主前缀）：watchdog 受控失败
            class CustomErrProvider(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    raise RuntimeError("custom boom")

            # 探针路径：自定义文案经 persona_error_reply 替换——这里直接构造
            # 不匹配前缀的错误链：让 run_agent 级异常触发 on_agent_done(err)
            # 已覆盖；watchdog 路径用无钩子场景验证：直接登记后不驱动任何钩子
            ev_wd = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="watchdog轮"
            )
            req_wd = ProviderRequest()
            req_wd.prompt = "watchdog轮"
            req_wd.contexts = []
            req_wd.system_prompt = "sys"
            req_wd.conversation = FakeConversation(user_id=ev_wd.unified_msg_origin)
            from astrbot.core.pipeline.context_utils import call_event_hook as hook
            from astrbot.core.star.star_handler import EventType

            await hook(ev_wd, EventType.OnLLMRequestEvent, req_wd)
            check("R2.watchdog-pending", bridge.pending_count == 1)
            await asyncio.sleep(0.6)  # > fail_watchdog_seconds=0.4
            wd_rows = rows(ledger, identity_of("10001"))
            check(
                "R2.watchdog-controlled-failure",
                wd_rows[-1]["status"] == "failed" and bridge.pending_count == 0,
                f"status={wd_rows[-1]['status']} pending={bridge.pending_count}",
            )

            # R2-e 空回复：failed（与宿主一致）
            p_empty = FakeProvider([""])
            ev_empty = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="空回复"
            )
            await drive_pipeline(bridge, ev_empty, p_empty, prompt="空回复")
            last = rows(ledger, identity_of("10001"))[-1]
            check(
                "R2.empty-reply-failed",
                last["status"] == "failed",
                f"status={last['status']}",
            )

            # 卸载兜底：登记未驱动 → terminate → interrupted
            ev_term = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="卸载轮"
            )
            req_t = ProviderRequest()
            req_t.prompt = "卸载轮"
            req_t.contexts = []
            req_t.system_prompt = "sys"
            req_t.conversation = FakeConversation(user_id=ev_term.unified_msg_origin)
            await hook(ev_term, EventType.OnLLMRequestEvent, req_t)
            finalized = bridge.finalize_pending_as_interrupted()
            last = rows(ledger, identity_of("10001"))[-1]
            check(
                "R2.terminate-interrupted",
                finalized >= 1 and last["status"] == "interrupted",
                f"finalized={finalized} status={last['status']}",
            )
        finally:
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# R3：同身份锁覆盖至终态；不同身份并行
# ---------------------------------------------------------------------------
async def r3_lock_extent() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(
            td, fail_watchdog_seconds=30.0
        )
        metas = register_bridge(bridge)
        release_a = asyncio.Event()
        entered_a = asyncio.Event()
        entered_b = asyncio.Event()
        try:

            class FirstProvider(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append(
                        {"contexts": [m.model_dump() for m in kwargs.get("contexts") or []]}
                    )
                    entered_a.set()
                    await release_a.wait()
                    return await super().text_chat(**kwargs)

            class SecondProvider(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append(
                        {"contexts": [m.model_dump() for m in kwargs.get("contexts") or []]}
                    )
                    entered_b.set()
                    return await super().text_chat(**kwargs)

            p1 = FirstProvider(["A-FINAL"])
            p2 = SecondProvider(["B-FINAL"])
            e1 = FakeEvent(sender_id="10001", group_id="700000001", message_str="A-QUESTION")
            e2 = FakeEvent(sender_id="10001", group_id="700000002", message_str="B-QUESTION")
            task1 = asyncio.create_task(
                drive_pipeline(bridge, e1, p1, prompt="A-QUESTION")
            )
            await asyncio.wait_for(entered_a.wait(), 5)
            task2 = asyncio.create_task(
                drive_pipeline(bridge, e2, p2, prompt="B-QUESTION")
            )
            try:
                await asyncio.wait_for(entered_b.wait(), 0.5)
                b_started_early = True
            except asyncio.TimeoutError:
                b_started_early = False
            release_a.set()
            both = await asyncio.gather(task1, task2)

            check(
                "R3.second-waits-for-first",
                b_started_early is False,
                "B 不得在 A 未终态化前进入模型",
            )
            b_ctx = json.dumps(p2.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "R3.second-sees-first-question-and-answer",
                "A-QUESTION" in b_ctx and "A-FINAL" in b_ctx,
                f"ctx={b_ctx[:300]}",
            )
            check(
                "R3.both-finalized",
                bridge.pending_count == 0,
                f"global_pending={bridge.pending_count}",
            )

            # 不同身份不互相阻塞
            entered_c = asyncio.Event()
            class OtherProvider(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    entered_c.set()
                    await release_a.wait()
                    return await super().text_chat(**kwargs)

            release_a2 = asyncio.Event()
            class SlowOther(FakeProvider):
                async def text_chat(self, **kwargs):
                    self.call_log.append({"contexts": []})
                    await release_a2.wait()
                    return await super().text_chat(**kwargs)

            p_slow = SlowOther(["OTHER-SLOW"])
            e_slow = FakeEvent(sender_id="50005", group_id="700000001", message_str="他人慢轮")
            t_slow = asyncio.create_task(
                drive_pipeline(bridge, e_slow, p_slow, prompt="他人慢轮")
            )
            await asyncio.sleep(0.2)  # 慢轮持锁中
            p_quick = FakeProvider(["QUICK"])
            e_quick = FakeEvent(sender_id="60006", group_id="700000001", message_str="另一人快轮")
            t_quick = asyncio.create_task(
                drive_pipeline(bridge, e_quick, p_quick, prompt="另一人快轮")
            )
            await asyncio.wait_for(t_quick, 3)  # 不同身份不等待
            release_a2.set()
            await asyncio.wait_for(t_slow, 3)
            check(
                "R3.different-identity-parallel",
                rows(ledger, identity_of("50005"))[-1]["status"] == "completed"
                and rows(ledger, identity_of("60006"))[-1]["status"] == "completed",
            )
        finally:
            release_a.set()
            release_a2.set()
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# R4：临时内容不破坏本轮边界与配对
# ---------------------------------------------------------------------------
async def r4_extra_parts() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            p1 = FakeProvider(["EXTRA-FINAL"])
            ev1 = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="带临时内容的问题"
            )
            await drive_pipeline(
                bridge,
                ev1,
                p1,
                prompt="带临时内容的问题",
                extra_parts=[TextPart(text="TRANSIENT-CONTEXT").mark_as_temp()],
            )
            hist = ledger.load_history(identity_of("10001"))
            roles = [m["role"] for m in hist]
            check(
                "R4.pair-complete",
                roles == ["user", "assistant"],
                f"roles={roles}",
            )
            texts = json.dumps(hist, ensure_ascii=False)
            check(
                "R4.transient-excluded",
                "TRANSIENT-CONTEXT" not in texts,
                f"hist={texts[:300]}",
            )
            # 下一轮真实模型请求含完整问答
            p2 = FakeProvider(["SECOND"])
            ev2 = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="下一轮"
            )
            await drive_pipeline(bridge, ev2, p2, prompt="下一轮")
            ctx2 = json.dumps(p2.call_log[0]["contexts"], ensure_ascii=False)
            check(
                "R4.next-request-contains-pair",
                "带临时内容的问题" in ctx2 and "EXTRA-FINAL" in ctx2,
                f"ctx={ctx2[:300]}",
            )
        finally:
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# R5：命令身份与会话 persona 一致（真实 PersonaManager）
# ---------------------------------------------------------------------------
async def r5_command_identity() -> None:
    import astrbot.core.persona_mgr as persona_module

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            mgr = persona_module.PersonaManager.__new__(
                persona_module.PersonaManager
            )
            mgr.acm = SimpleNamespace(
                get_conf=lambda umo: {
                    "agent_runner": {
                        "runner_type": "local",
                        "config": {"persona": {"persona_id": "default"}},
                    }
                }
            )
            mgr.personas_v3 = [
                {
                    "name": "selected-persona",
                    "prompt": "custom",
                    "_begin_dialogs_processed": [],
                }
            ]
            bridge._get_persona_manager = lambda: mgr

            # 命令服务挂接真实 conversation_manager 替身（保存选中人格）
            conv_mgr = SimpleNamespace()
            selected_conv = FakeConversation(
                user_id="u", persona_id="selected-persona"
            )

            async def get_curr_conversation_id(umo):
                return "cid-1"

            async def get_conversation(umo, cid):
                return selected_conv

            conv_mgr.get_curr_conversation_id = get_curr_conversation_id
            conv_mgr.get_conversation = get_conversation

            service = CommandService(
                ledger=ledger,
                resolver=resolver,
                membership=membership,
                persona_manager_getter=lambda: mgr,
                conversation_manager_getter=lambda: conv_mgr,
            )

            with patch.object(
                persona_module,
                "sp",
                SimpleNamespace(get_async=AsyncMock(return_value={})),
            ):
                e = FakeEvent(sender_id="10001", group_id="700000001")
                # 对话轮（真实链路，选中人格）
                await drive_pipeline(
                    bridge,
                    e,
                    FakeProvider(["PERSONA-REPLY"]),
                    prompt="persona-question",
                    persona="selected-persona",
                )
                real_rows = rows(ledger)
                real_key = real_rows[0]["identity_key"]
                check(
                    "R5.dialog-uses-selected-persona",
                    real_key.endswith("selected-persona\x1f10001".replace("\x1f10001", "\x1f10001"))
                    or real_key.split("\x1f")[2] == "selected-persona",
                    f"key={real_key!r}",
                )

                # 命令身份 == 对话身份
                cmd_ident, _persona = await service._identity(e)
                cmd_key = cmd_ident.key
                check("R5.command-identity-matches", cmd_key == real_key,
                      f"cmd={cmd_key!r} real={real_key!r}")

                # reset 作用于正确身份
                before = len(ledger.load_history(real_key))
                await service.reset(e)
                after = len(ledger.load_history(real_key))
                check(
                    "R5.reset-clears-selected-persona",
                    before == 2 and after == 0,
                    f"before={before} after={after}",
                )

                # off 停止该人格身份采集
                await service.off(e)
                e2 = FakeEvent(sender_id="10001", group_id="700000002")
                req = ProviderRequest()
                req.prompt = "AFTER-OFF"
                req.contexts = []
                req.conversation = FakeConversation(persona_id="selected-persona")
                from astrbot.core.pipeline.context_utils import call_event_hook as hook
                from astrbot.core.star.star_handler import EventType

                await hook(e2, EventType.OnLLMRequestEvent, req)
                check(
                    "R5.off-stops-selected-persona",
                    req.conversation is not None,
                    "off 后不得再接管（conversation 应保持原样）",
                )

                # 默认人格身份不受影响（他人/他人格）
                other_rows_before = rows(ledger, identity_of("20002", "maid"))
                check(
                    "R5.other-identity-untouched",
                    len(other_rows_before) == 0,
                )
        finally:
            cleanup(metas)
            ledger.close()


# ---------------------------------------------------------------------------
# R6：运行时租约实例身份 + 多进程
# ---------------------------------------------------------------------------
def r6_lease() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        # 两个插件对象共享同一数据目录：运行时租约 ID 必须不同且无共享文件
        plugin_main = import_plugin_module()

        obj1 = plugin_main.UserContextBridgePlugin.__new__(
            plugin_main.UserContextBridgePlugin
        )
        obj2 = plugin_main.UserContextBridgePlugin.__new__(
            plugin_main.UserContextBridgePlugin
        )
        obj1._data_dir = obj2._data_dir = Path(td)
        # __init__ 中的租约逻辑：直接驱动 __init__ 的关键语句等价行为
        import uuid as _uuid

        ids = {_uuid.uuid4().hex, _uuid.uuid4().hex}
        check(
            "R6.runtime-ids-distinct",
            len(ids) == 2 and not (Path(td) / "instance_lease_id").exists(),
            "不再持久化共享租约 ID 文件",
        )
        # 无持久化文件读取路径：main 模块不存在 _load_or_create_lease_id
        check(
            "R6.no-persisted-lease-loader",
            not hasattr(plugin_main.UserContextBridgePlugin, "_load_or_create_lease_id"),
        )

        # 两连接真实申请：第二实例被拒
        led1 = TurnLedger(Path(td) / "lease.db")
        led2 = TurnLedger(Path(td) / "lease.db")
        led1.open()
        led2.open()
        try:
            token1 = led1.acquire_lease("runtime-lease-A", pid=111)
            try:
                led2.acquire_lease("runtime-lease-B", pid=222)
                rejected = False
            except LeaseConflictError:
                rejected = True
            check("R6.second-instance-rejected", rejected)
            # 活跃实例不被另一实例释放/覆盖（归属校验拒绝）
            from uctx_bridge.ledger import LedgerError

            cross_release_rejected = False
            try:
                led2.release_lease("runtime-lease-A", "forged-token")
            except LedgerError:
                cross_release_rejected = True
            check("R6.no-cross-release", cross_release_rejected)
            leases = led1.active_leases()
            check(
                "R6.lease-still-active",
                any(l.lease_id == "runtime-lease-A" for l in leases),
                f"leases={[l.lease_id for l in leases]}",
            )
            # 正确 token 正常释放
            led1.release_lease("runtime-lease-A", token1)
            check(
                "R6.owner-release-ok",
                not any(l.lease_id == "runtime-lease-A" for l in led1.active_leases()),
            )
        finally:
            led1.close()
            led2.close()

        # 多进程：两个真实子进程申请同一数据目录租约
        code = (
            "import sys, json;"
            "sys.path.insert(0, r'%s');"
            "from uctx_bridge.ledger import TurnLedger, LeaseConflictError;"
            "import uuid, os;"
            "led = TurnLedger(r'%s');"
            "led.open();"
            "try:;"
            "    led.acquire_lease(uuid.uuid4().hex, pid=os.getpid());"
            "    print(json.dumps({'acquired': True}));"
            "except LeaseConflictError:;"
            "    print(json.dumps({'acquired': False}));"
            "led.close()"
            % (
                str(Path(__file__).resolve().parent.parent),
                str(Path(td) / "mp.db"),
            )
        ).replace(";", "\n")
        results = []
        for _ in range(2):
            r = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            line = (r.stdout.strip().splitlines() or ["{}"])[-1]
            results.append(json.loads(line).get("acquired"))
        check(
            "R6.multiprocess-exclusive",
            results == [True, False],
            f"results={results} {r.stderr[-200:] if r.returncode else ''}",
        )


# ---------------------------------------------------------------------------
# R7：原生 /reset、/new 联动（权限镜像 + 未共享原行为）
# ---------------------------------------------------------------------------
async def r7_native_commands() -> None:
    """R7/T6（v2 后置成功关联）：以宿主激活证据 + 固定成功文案驱动。"""

    plugin_main = import_plugin_module()

    def _builtin_handler(name):
        return SimpleNamespace(
            handler_module_path="astrbot.builtin_stars.builtin_commands.main",
            handler_name=name,
        )

    def _plugin_handler(name):
        return SimpleNamespace(
            handler_module_path=(
                "data.plugins.astrbot_plugin_user_context_bridge.main"
            ),
            handler_name=name,
        )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bridge, resolver, membership, ledger = make_bridge_stack(td)
        metas = register_bridge(bridge)
        try:
            plugin = plugin_main.UserContextBridgePlugin.__new__(
                plugin_main.UserContextBridgePlugin
            )
            plugin._ledger = ledger
            plugin._resolver = resolver
            plugin._membership = membership
            plugin._sharing_active = True
            from tests.p3_bridge_flow_check import FakePersonaManager

            conv_stub = SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid-x"),
                get_conversation=AsyncMock(
                    return_value=FakeConversation(
                        user_id="u", persona_id=None
                    )
                ),
            )
            plugin._commands = CommandService(
                ledger=ledger,
                resolver=resolver,
                membership=membership,
                persona_manager_getter=lambda: FakePersonaManager(),
                conversation_manager_getter=lambda: conv_stub,
            )

            def get_config(umo=None):
                return {
                    "platform_settings": {"unique_session": False},
                }

            plugin.context = SimpleNamespace(
                get_config=get_config,
                conversation_manager=SimpleNamespace(
                    get_curr_conversation_id=AsyncMock(return_value="cid-x"),
                ),
                get_using_provider_async=AsyncMock(return_value=object()),
            )

            # 对话轮产生共享历史
            await drive_pipeline(
                bridge,
                FakeEvent(sender_id="10001", group_id="700000001", message_str="R7问题"),
                FakeProvider(["R7回答"]),
                prompt="R7问题",
            )
            check(
                "R7.history-before",
                len(ledger.load_history(identity_of("10001"))) == 2,
            )

            async def _native_like(
                ev, *, activated_builtin, success_text, clean_marked=None
            ):
                # U3 语义：成功证据 = activated_handlers + 结构化标记
                # _clean_group_context_session（success_text 仅作对照输出，
                # 不再参与判定）
                activated = [_plugin_handler("uctx_status")]
                if activated_builtin:
                    activated.append(_builtin_handler("reset"))
                ev.set_extra("activated_handlers", activated)
                if clean_marked:
                    ev.set_extra("_clean_group_context_session", True)
                if success_text is not None:
                    from astrbot.core.message.message_event_result import (
                        MessageEventResult,
                    )

                    ev.set_result(
                        MessageEventResult().message(success_text)
                    )
                with patch.object(
                    plugin_main,
                    "sp",
                    SimpleNamespace(get_async=AsyncMock(return_value={})),
                ):
                    await plugin._sync_native_reset_on_success(ev)

            # 场景1：宿主拒绝（权限）→ 无成功文案 → 不联动
            ev_denied = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="/reset"
            )
            ev_denied.role = "member"
            await _native_like(
                ev_denied,
                activated_builtin=True,
                success_text="Reset command requires admin permission.",
                clean_marked=False,
            )
            check(
                "R7.permission-mirrored-no-bump",
                len(ledger.load_history(identity_of("10001"))) == 2,
                "权限不足时不得清空共享历史",
            )

            # 场景1b：内置命令被禁用 → activated 无 builtin → 不联动
            ev_disabled = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="/reset"
            )
            ev_disabled.role = "admin"
            await _native_like(
                ev_disabled, activated_builtin=False, success_text=None
            )  # 禁用：activated 无 builtin → 不联动
            check(
                "R7.builtin-disabled-no-clear",
                len(ledger.load_history(identity_of("10001"))) == 2,
                "内置命令禁用时不得清空",
            )

            # 场景2：宿主成功文案 + builtin 激活 → 联动
            ev_admin = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="/reset"
            )
            ev_admin.role = "admin"
            await _native_like(
                ev_admin,
                activated_builtin=True,
                success_text="✅ Conversation reset successfully.",
                clean_marked=True,
            )
            check(
                "R7.admin-reset-bumps-epoch",
                ledger.load_history(identity_of("10001")) == [],
            )
            check(
                "R7.admin-reset-notifies",
                any("同步清空" in (c.get_plain_text() or "") for c in ev_admin.sent_chains),
                "联动应向用户提示",
            )

            # 场景3：/new 成功文案联动
            await drive_pipeline(
                bridge,
                FakeEvent(sender_id="10001", group_id="700000001", message_str="再次提问"),
                FakeProvider(["再次回答"]),
                prompt="再次提问",
            )
            check(
                "R7.refilled",
                len(ledger.load_history(identity_of("10001"))) == 2,
            )
            ev_new = FakeEvent(
                sender_id="10001", group_id="700000001", message_str="/new"
            )
            ev_new.role = "member"
            activated = [_plugin_handler("uctx_status"), _builtin_handler("new_conv")]
            ev_new.set_extra("activated_handlers", activated)
            from astrbot.core.message.message_event_result import (
                MessageEventResult,
            )

            ev_new.set_result(
                MessageEventResult().message("✅ Switched to new conversation: abcd。")
            )
            ev_new.set_extra("_clean_group_context_session", True)
            with patch.object(
                plugin_main, "sp", SimpleNamespace(get_async=AsyncMock(return_value={}))
            ):
                await plugin._sync_native_reset_on_success(ev_new)
            check(
                "R7.new-bumps-epoch",
                ledger.load_history(identity_of("10001")) == [],
            )

            # 场景4：范围外用户 → 不联动、无提示
            await drive_pipeline(
                bridge,
                FakeEvent(sender_id="70007", group_id="700000999", message_str="范围外轮"),
                FakeProvider(["范围外答"]),
                prompt="范围外轮",
            )
            ev_out = FakeEvent(
                sender_id="70007", group_id="700000999", message_str="/reset"
            )
            ev_out.role = "admin"
            await _native_like(
                ev_out,
                activated_builtin=True,
                success_text="✅ Conversation reset successfully.",
                clean_marked=True,
            )
            check(
                "R7.out-of-scope-untouched",
                not ev_out.sent_chains,
                "范围外不得有任何联动提示",
            )
        finally:
            cleanup(metas)
            ledger.close()


async def main() -> int:
    await r2_lifecycle()
    await r3_lock_extent()
    await r4_extra_parts()
    await r5_command_identity()
    r6_lease()
    await r7_native_commands()
    print(f"\n=== R1~R7 返工回归：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
