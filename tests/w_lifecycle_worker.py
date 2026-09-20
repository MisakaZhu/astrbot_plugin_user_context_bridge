"""W 生命周期 worker（W1/W2/W5）：真实 PluginManager reload 识别模式变化。

由 tests/w_lifecycle_check.py 以两版宿主解释器分别 spawn。覆盖：

  persona 加载 → 同模式 reload 保留历史 → 显式切 user（代次+1、旧历史
  不回灌）→ 切回 persona（代次+1、不复活）→ 再切 user（代次+1、最初
  persona 问答不复活）→ reset 后切换不复活 → 退出继承矩阵（persona off→
  user 保持；user off→persona 保护现有人格与未来人格；单人格 on 只解除
  该人格）→ status 文本与真实接管/有效数一致 → 配置变化时挂起 A+排队 B
  受控停止且新配置下一轮真实完成。

真实宿主组件：PluginManager load/reload、注册钩子、Runner/调度/装饰链、
AstrBotConfig；模型/QQ 为合成替身。输出单行 JSON。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR_NAME = "astrbot_plugin_user_context_bridge"
SOURCE_FILES = [
    "main.py",
    "metadata.yaml",
    "requirements.txt",
    "_conf_schema.json",
    "uctx_bridge/__init__.py",
    "uctx_bridge/identity.py",
    "uctx_bridge/scope.py",
    "uctx_bridge/ledger.py",
    "uctx_bridge/bridge.py",
    "uctx_bridge/commands.py",
]


def setup_instance_root(root: Path, zip_path: str | None = None) -> None:
    """插件落位：默认从源码复制；给定 zip_path 时从**真实 ZIP 包**解包
    （N22：安装链以候选包内容为准，不得用源码复制夹具冒充）。"""

    plug = root / "data" / "plugins" / PLUGIN_DIR_NAME
    plug.mkdir(parents=True, exist_ok=True)
    (root / "data" / "config").mkdir(parents=True, exist_ok=True)
    if zip_path:
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "tools/uctx_records.py" in names, "候选包应含只读工具"
            for name in names:
                target = (plug / name).resolve()
                if not str(target).startswith(str(plug.resolve())):
                    raise ValueError(f"ZIP 条目路径异常：{name}")
            zf.extractall(plug)
    else:
        for rel in SOURCE_FILES:
            src = REPO / rel
            dst = plug / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def write_config(root: Path, scope: str) -> None:
    cfg_path = root / "data" / "config" / f"{PLUGIN_DIR_NAME}_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "shared_groups": ["700000001", "700000002", "700000003"],
                "include_private": True,
                "history_scope": scope,
                "max_history_turns": 40,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def main() -> int:
    instance_root = Path(sys.argv[1])
    zip_arg = sys.argv[2] if len(sys.argv) > 2 else None
    setup_instance_root(instance_root, zip_path=zip_arg)
    os.environ["ASTRBOT_ROOT"] = str(instance_root)
    sys.path.insert(0, str(instance_root))
    sys.path.insert(0, str(REPO))

    from astrbot.core import AstrBotConfig
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    from tests.fakes import FakeConversation, FakeEvent, FakeProvider
    from tests.p3_bridge_flow_check import FakePersonaManager

    out: dict = {}
    generations: list = []

    class PerWindowPersona(FakePersonaManager):
        """按窗口返回不同人格：A→black，B→white，其余→third。"""

        async def resolve_selected_persona(self, **kw):
            umo = str(kw.get("umo") or "")
            if "700000001" in umo:
                scope = "black"
            elif "700000002" in umo:
                scope = "white"
            else:
                scope = "third"
            return (scope, {"name": scope, "_begin_dialogs_processed": []},
                    None, False)

    cfg_dict = {"platform_settings": {"unique_session": False}}
    conv_mgr = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        get_conversation=AsyncMock(return_value=None),
        update_conversation=AsyncMock(),
    )
    context = SimpleNamespace(
        persona_manager=PerWindowPersona(),
        conversation_manager=conv_mgr,
        get_config=lambda umo=None, **kw: cfg_dict,
        get_using_provider_async=AsyncMock(return_value=object()),
        get_using_provider=lambda umo: object(),
        get_registered_star=lambda name: star_map.get(
            f"data.plugins.{name}.main"
        ),
        get_using_tts_provider_async=AsyncMock(return_value=None),
        get_using_tts_provider=lambda umo: None,
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda t: None, get_full_tool_set=lambda: None
        ),
    )
    astrbot_cfg = AstrBotConfig(
        config_path=str(instance_root / "data" / "cmd_config.json")
    )
    pm = PluginManager(context, astrbot_cfg)
    base_key = "aiocqhttp\x1fbot_001\x1f10001"

    def meta_of(name: str):
        db_path = instance_root / "data" / "plugin_data" / PLUGIN_DIR_NAME / "uctx_ledger.db"
        if not db_path.exists():
            return None
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT value FROM meta WHERE identity_key=? AND name=?",
                (base_key, name),
            ).fetchone()
        return row[0] if row else None

    def current_gen() -> int:
        v = meta_of("mode_generation")
        return int(v) if v is not None else 0

    def snapshot_gen() -> None:
        generations.append(
            {
                "scope_mode": meta_of("scope_mode"),
                "generation": current_gen(),
            }
        )

    async def drive(ev, provider, prompt: str) -> bool:
        """真实调度链一轮；返回是否真实完成（未接管/停止=False）。"""
        from copy import deepcopy

        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
        from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
        from astrbot.core.astr_agent_run_util import run_agent
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType

        req = ProviderRequest()
        req.prompt = prompt
        req.contexts = []
        req.system_prompt = "w system"
        req.conversation = FakeConversation(user_id=ev.unified_msg_origin)
        stopped = await call_event_hook(
            ev, EventType.OnLLMRequestEvent, req
        )
        if stopped or ev.is_stopped():
            return False
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=provider,
            request=req,
            run_context=ContextWrapper(context=SimpleNamespace(event=ev)),
            tool_executor=FunctionToolExecutor(),
            agent_hooks=MAIN_AGENT_HOOKS,
            streaming=False,
        )

        class AgentStage:
            async def process(self, ev_inner):
                async for _ in run_agent(runner, 30, True, False, False):
                    yield

        class Transport:
            async def process(self, ev_inner):
                result = ev_inner.get_result()
                if result is not None and getattr(result, "chain", None):
                    await ev_inner.send(result)
                    await call_event_hook(
                        ev_inner, EventType.OnAfterMessageSentEvent
                    )

        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["t2i"] = False
        cfg["content_safety"]["also_use_in_response"] = False
        cfg["provider_tts_settings"]["enable"] = False
        cfg["platform_settings"]["reply_with_mention"] = False
        cfg["platform_settings"]["reply_with_quote"] = False
        cfg["platform_settings"]["segmented_reply"]["enable"] = False
        stage_ctx = SimpleNamespace(
            astrbot_config=cfg,
            plugin_manager=SimpleNamespace(
                context=SimpleNamespace(
                    get_using_tts_provider_async=AsyncMock(return_value=None),
                    get_using_tts_provider=lambda umo: None,
                )
            ),
        )
        decorator = ResultDecorateStage()
        await decorator.initialize(stage_ctx)
        scheduler = PipelineScheduler.__new__(PipelineScheduler)
        scheduler.ctx = stage_ctx
        scheduler.stages = [AgentStage(), decorator, Transport()]
        await scheduler._process_stages(ev)
        resp = runner.get_final_llm_resp()
        return resp is not None and (resp.completion_text or "") != ""

    def active_plugin():
        meta = star_map.get(f"data.plugins.{PLUGIN_DIR_NAME}.main")
        return meta.star_cls if meta else None

    async def reload_with(scope: str | None) -> object:
        if scope is not None:
            write_config(instance_root, scope)
        await pm.reload(PLUGIN_DIR_NAME)
        obj = active_plugin()
        snapshot_gen()
        return obj

    async def ask(obj, group, question, answer):
        """返回 (是否产出回复, 上下文, 事件, 本轮采集增量)。

        采集增量以 bridge.stats.captured 为准：退出/范围外轮次走原生
        路径模型仍会作答（completed=True），但插件不得采集（captured=0）。
        """

        ev = FakeEvent(sender_id="10001", group_id=group, message_str=question)
        provider = FakeProvider([answer])
        cap0 = obj._bridge.stats.captured
        completed = await drive(ev, provider, question)
        captured = obj._bridge.stats.captured - cap0
        ctx = (
            json.dumps(provider.call_log[0].get("contexts") or [],
                       ensure_ascii=False, default=str)
            if captured and provider.call_log
            else ""
        )
        return completed, ctx, ev, captured

    async def command(obj, ev, name):
        return await getattr(obj._commands, name)(ev)

    # -- Phase 0：persona 加载；status 无接管证据不得宣称共享 ----------------
    write_config(instance_root, "persona")
    ok, _err = await pm.load(specified_dir_name=PLUGIN_DIR_NAME)
    out["load_ok"] = bool(ok)
    plugin = active_plugin()
    out["bound_handlers"] = len(star_handlers_registry.get_handlers_by_module_name(
        f"data.plugins.{PLUGIN_DIR_NAME}.main"))
    ev_st = FakeEvent(sender_id="10001", group_id="700000001",
                      message_str="/uctx status")
    st = await command(plugin, ev_st, "status")
    out["status_fresh_no_evidence"] = (
        "尚无记录" in st and "实际接管" in st and "共享中" not in st
    )
    done, _ctx, _ev, _cap = await ask(plugin, "700000001", "P-问", "P-答")
    out["persona_turn_done"] = done
    snapshot_gen()
    st = await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000001",
        message_str="/uctx status"), "status")
    out["status_after_turn"] = (
        "实际接管：最近" in st and "1 轮已完成" in st and "p:black" not in st
    )

    # -- Phase 1（X1）：无同模式预热，首轮后直接切 user ----------------------
    # 首轮完成后 scope_mode 必须已持久化（begin_turn 同事务登记）
    out["gen0_persona_recorded"] = (
        meta_of("scope_mode") == "persona" and current_gen() == 0
    )
    plugin = await reload_with("user")
    out["first_direct_switch_bumps"] = (
        meta_of("scope_mode") == "user" and current_gen() == 1
    )
    # 旧 persona 历史被归档：当前代次读不到（X1 验收）
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "U-问", "U-答")
    out["first_switch_archives_old"] = (
        "P-问" not in ctx and "P-答" not in ctx
    )
    with sqlite3.connect(str(instance_root / "data" / "plugin_data"
        / PLUGIN_DIR_NAME / "uctx_ledger.db")) as db:
        archived = db.execute(
            "SELECT COUNT(*) FROM turns WHERE mode_generation=0"
            " AND identity_key LIKE '%p:black%'").fetchone()[0]
    out["archived_rows_queryable"] = archived >= 1

    # -- Phase 2：同模式 reload 保留历史（user） ------------------------------
    plugin = await reload_with("user")
    out["same_mode_reload_preserves"] = done and "U-问" in ctx
    out["same_mode_gen_unchanged"] = current_gen() == 1
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "S-问", "S-答")
    out["user_history_chains"] = (
        done and "U-问" in ctx and "U-答" in ctx and "S-问" in ctx
    )

    # -- Phase 3：切回 persona：代次 +1，user 记录不回灌 ----------------------
    plugin = await reload_with("persona")
    out["switch_back_bumps"] = (
        meta_of("scope_mode") == "persona" and current_gen() == 2
    )
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "RPB-问", "RPB-答")
    out["debug_bp_ctx"] = ctx[:300]
    out["debug_bp_done_cap"] = [done, _cap]
    out["back_to_persona_fresh"] = (
        done and "U-问" not in ctx and "U-答" not in ctx
        and "S-问" not in ctx and "S-答" not in ctx
    )

    # -- Phase 4：再切 user：最初 persona 问答也不复活 ------------------------
    plugin = await reload_with("user")
    out["second_user_bumps"] = (
        meta_of("scope_mode") == "user" and current_gen() == 3
    )
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "U2-问", "U2-答")
    out["second_user_no_resurrect"] = (
        done and "RPB-问" not in ctx and "RPB-答" not in ctx
        and "P-问" not in ctx and "U-问" not in ctx
    )
    out["generation_sequence"] = generations

    # -- Phase 5：reset 后切换不复活 -----------------------------------------
    ev_reset = FakeEvent(sender_id="10001", group_id="700000001",
                         message_str="/uctx reset")
    reset_text = await command(plugin, ev_reset, "reset")
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "U3-问", "U3-答")
    out["reset_text_scope"] = "跨人格" in reset_text
    plugin = await reload_with("persona")
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "RPB2-问", "RPB2-答")
    plugin = await reload_with("user")
    out["post_reset_switch_gen"] = current_gen()
    done, ctx, _ev, _cap = await ask(plugin, "700000001", "U4-问", "U4-答")
    out["post_reset_no_resurrect"] = (
        done and "U3-问" not in ctx and "U3-答" not in ctx
    )

    # -- Phase 6：退出继承矩阵（真实命令 + reload） ---------------------------
    # persona off（black）
    plugin = await reload_with("persona")
    off_ev = FakeEvent(sender_id="10001", group_id="700000001",
                       message_str="/uctx off")
    await command(plugin, off_ev, "off")
    done, _ctx, _ev, cap_off = await ask(plugin, "700000001", "OFF-问", "OFF-答")
    out["persona_off_blocks"] = cap_off == 0
    # 切 user：退出继承（status 与运行时一致）
    plugin = await reload_with("user")
    st = await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000001",
        message_str="/uctx status"), "status")
    out["status_shows_inherited_off"] = (
        "已退出" in st and "共享中" not in st
    )
    done, _ctx, _ev, cap_iu = await ask(plugin, "700000001", "IU-问", "IU-答")
    out["user_inherits_off_runtime"] = cap_iu == 0
    # user on：解除
    on_ev = FakeEvent(sender_id="10001", group_id="700000001",
                      message_str="/uctx on")
    await command(plugin, on_ev, "on")
    done, _ctx, _ev, cap_on = await ask(plugin, "700000001", "ON-问", "ON-答")
    out["user_on_unblocks"] = done is True and cap_on == 1
    # user off → 切 persona：基础保护覆盖现有人格与未来人格
    await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000001",
        message_str="/uctx off"), "off")
    plugin = await reload_with("persona")
    done, _ctx, _ev, cap_pb = await ask(plugin, "700000001", "PB-问", "PB-答")
    done3, _ctx, _ev, cap_pf = await ask(plugin, "700000003", "PF-问", "PF-答")
    out["persona_off_after_user_off"] = cap_pb == 0
    out["future_persona_protected"] = cap_pf == 0
    st = await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000003",
        message_str="/uctx status"), "status")
    # black 本人确有显式 off（原因=本人格退出）；未来人格窗口的退出
    # 原因才是继承——继承语义必须落实到 status 文本
    out["status_persona_inherited"] = "已退出" in st and "继承" in st
    # 单人格 on 只解除该人格
    on_text = await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000001",
        message_str="/uctx on"), "on")
    out["single_on_cmd_head"] = on_text[:80]

    async def ask_detail(obj, group, question, answer) -> dict:
        """AA1：RA/RF 分项诊断——命令返回、done/captured/model_calls/
        stopped、身份/事件键、同 event_key 此前是否已有终态。"""
        ev = FakeEvent(sender_id="10001", group_id=group,
                       message_str=question)
        provider = FakeProvider([answer])
        key = obj._bridge.event_key_for(ev)
        db_path = (
            instance_root / "data" / "plugin_data" / PLUGIN_DIR_NAME
            / "uctx_ledger.db"
        )
        prior: list = []
        with sqlite3.connect(db_path) as c:
            prior = [r[0] for r in c.execute(
                "SELECT status FROM turns WHERE event_key=?",
                (key,)).fetchall()]
        cap0 = obj._bridge.stats.captured
        completed = await drive(ev, provider, question)
        captured = obj._bridge.stats.captured - cap0
        return {
            "done": completed,
            "captured": captured,
            "model_calls": len(provider.call_log),
            "stopped": ev.is_stopped(),
            "event_key": key,
            "prior_terminal_same_key": prior,
        }

    ra = await ask_detail(plugin, "700000001", "RA-问", "RA-答")
    rf = await ask_detail(plugin, "700000003", "RF-问", "RF-答")
    out["single_on_RA"] = ra
    out["single_on_RF"] = rf
    out["single_on_releases_only_that"] = (
        ra["done"] is True and ra["captured"] == 1 and rf["captured"] == 0
    )

    # -- Phase 7：配置变化时挂起 A + 排队 B（同基础身份，B 真实排队） --------
    from astrbot.core.utils.active_event_registry import active_event_registry

    # 先清退出状态：user on 解除 user 键退出与基础保护
    plugin = await reload_with("user")
    await command(plugin, FakeEvent(
        sender_id="10001", group_id="700000001",
        message_str="/uctx on"), "on")
    plugin = await reload_with("persona")

    entered_a, release_a = asyncio.Event(), asyncio.Event()

    class Hanging(FakeProvider):
        async def text_chat(self, **kwargs):
            self.call_log.append({"contexts": []})
            entered_a.set()
            await release_a.wait()
            return await super().text_chat(**kwargs)

    ev_a = FakeEvent(sender_id="10001", group_id="700000001", message_str="切换期挂起")
    active_event_registry.register(ev_a)
    t_a = asyncio.create_task(drive(ev_a, Hanging(["LATE-SWITCH"]), "切换期挂起"))
    await asyncio.wait_for(entered_a.wait(), 3)
    # B 与 A 同基础身份同人格（同窗口 700000001）→ 真实在身份锁上排队
    ev_b = FakeEvent(sender_id="10001", group_id="700000001", message_str="切换期排队")
    prov_b = FakeProvider(["QUEUED-SWITCH"])
    t_b = asyncio.create_task(drive(ev_b, prov_b, "切换期排队"))
    await asyncio.sleep(0.3)
    # Z1b：A/B 排队在**切换前旧实例**的身份锁上——先抓住旧 bridge 引用，
    # 等待者/挂起清理必须同时覆盖旧实例与新实例，不得只看新 bridge。
    pre_switch_bridge = plugin._bridge
    plugin = await reload_with("user")
    release_a.set()
    import traceback

    # Y1：任务结果显式归类（returned / host_boundary_keyerror / failed:*），
    # 异常始终保留完整调用栈、异常类型、键与最深帧来源；只有精确命中
    # "宿主 call_event_hook 在事件停止后为已卸载插件打日志索引 star_map"
    # 这一个边界才可受控让出，其余 KeyError/RuntimeError/TimeoutError 均失败。
    try:
        await asyncio.wait_for(t_a, 3)
        out["switch_active_outcome"] = "returned"
    except BaseException as exc:  # noqa: BLE001
        out["switch_active_outcome"] = f"failed:{type(exc).__name__}"
        out["switch_active_traceback"] = traceback.format_exc()
    out["switch_active_returned"] = out["switch_active_outcome"] == "returned"
    try:
        await asyncio.wait_for(t_b, 3)
        out["switch_queued_outcome"] = "returned"
    except BaseException as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        out["switch_queued_full_traceback"] = tb
        out["switch_queued_exc_type"] = type(exc).__name__
        out["switch_queued_exc_key"] = (
            exc.args[0] if isinstance(exc, KeyError) and exc.args else None
        )
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            deepest = frames[-1]
            rel = Path(deepest.filename).as_posix().split("site-packages/")[-1]
            out["switch_queued_exc_source"] = f"{rel}:{deepest.name}"
        else:
            out["switch_queued_exc_source"] = None
        classified = (
            isinstance(exc, KeyError)
            and out["switch_queued_exc_key"]
            == f"data.plugins.{PLUGIN_DIR_NAME}.main"
            and out["switch_queued_exc_source"]
            == "astrbot/core/pipeline/context_utils.py:call_event_hook"
            and (
                ev_b.get_extra("agent_stop_requested") is True
                or ev_b.is_stopped()
            )
        )
        out["switch_queued_outcome"] = (
            "host_boundary_keyerror"
            if classified
            else f"failed:{type(exc).__name__}"
        )
    out["switch_queued_returned"] = out["switch_queued_outcome"] == "returned"
    out["switch_queued_model_calls"] = len(prov_b.call_log)
    out["switch_active_no_late"] = not any(
        "LATE-SWITCH" in (c.get_plain_text() or "") for c in ev_a.sent_chains
    )
    out["switch_active_stopped"] = (
        ev_a.get_extra("agent_stop_requested") is True or ev_a.is_stopped()
    )
    with sqlite3.connect(
        instance_root / "data" / "plugin_data" / PLUGIN_DIR_NAME / "uctx_ledger.db"
    ) as db:
        row = db.execute(
            "SELECT status FROM turns WHERE user_message LIKE '%切换期挂起%'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    out["switch_active_interrupted"] = bool(row and row[0] == "interrupted")
    out["switch_queued_no_output"] = not any(
        (c.get_plain_text() or "").strip() for c in ev_b.sent_chains
    )
    # X6：受控让出的事件停止证据（宿主边界 KeyError 路径下事件应已停止）
    out["switch_queued_stopped"] = ev_b.is_stopped()
    # Y1：受控让出还需 0 模型调用、无共享回写、锁无残留等待者
    with sqlite3.connect(
        instance_root / "data" / "plugin_data" / PLUGIN_DIR_NAME / "uctx_ledger.db"
    ) as db:
        queued_rows = db.execute(
            "SELECT status FROM turns WHERE user_message LIKE '%切换期排队%'"
        ).fetchall()
    out["switch_queued_ledger_no_completed"] = not any(
        r[0] == "completed" for r in queued_rows
    )

    def _bridge_lock_waiters(bridge) -> int:
        return sum(
            len(lock._waiters or []) for lock in bridge._identity_locks.values()
        )

    # Z1b：旧实例（A/B 真正排队处）与新实例的锁等待者、未终态挂起任务
    # 全部落 JSON，父断言逐项 == 0
    out["switch_lock_waiters_pre_bridge"] = _bridge_lock_waiters(
        pre_switch_bridge)
    out["switch_queued_lock_waiters_after"] = _bridge_lock_waiters(
        plugin._bridge)
    out["switch_pending_uncommitted"] = (
        pre_switch_bridge.pending_count + plugin._bridge.pending_count
    )
    done, ctx, _ev, cap_new = await ask(plugin, "700000001", "NEW-问", "NEW-答")
    out["switch_new_config_turn_done"] = done
    out["switch_new_config_fresh"] = (
        "切换期挂起" not in ctx and "切换期排队" not in ctx
        and "LATE-SWITCH" not in ctx and "QUEUED-SWITCH" not in ctx
    )
    out["final_gen_isolated"] = current_gen() >= 4

    print("@@RESULT@@" + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
