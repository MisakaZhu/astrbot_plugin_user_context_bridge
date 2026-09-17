"""轮次生命周期协调器（ADR-002/003，返工后 v2）。

读侧（on_llm_request）：
    范围判定 → 解析人格 → **获取身份锁（覆盖整轮生命周期，R3）** →
    读取共享历史快照 → 登记轮次（去重）→
    ``req.contexts = 人格开场白 + 共享历史`` → ``req.conversation = None``
    （短路宿主对原窗口的整段写回）。人格、系统提示、工具集、其他插件
    动态内容原样保留。

写侧终态机 v2（R2 返工：以真实宿主调度为准）：
    主提交点 = OnAgentDoneEvent（成功 / aborted / run_agent 级异常均触发，
    且 run_context.messages 轨迹完整）：
        role == "err"                     -> failed
        event.is_stopped()                -> aborted（用户停止是中止的决定性
                                             信号，覆盖 4.26「aborted 回调保留
                                             完整文本」与 4.28 marker 两种形态）
        文本空且无工具调用                  -> failed（与宿主一致：空回复不成功）
        其他                               -> completed
    辅助轨：
        OnDecoratingResultEvent：仅做模型 err 加速检测（done_seen 为假且
            事件结果文本为宿主错误文案时立即 failed）——宿主在 run_agent
            中间 yield 时也会执行装饰阶段，工具轮的中间输出不允许在此提交。
        OnAfterMessageSentEvent：标记 send_state；若轮次仍 running 且停止
            旗标已置 → aborted 兜底。
        fail-watchdog（T1，默认 180s）：仍 running（模型 err 不触发完成钩子、
            模型挂起、钩子缺失等）→ 受控 failed 并释放身份锁。
        插件 terminate：未决轮次 → interrupted。
    身份锁在轮次终态化（commit / watchdog / interrupted）时释放；后继同
    身份轮次因此必然读取到前一完整（或已受控失败）的轮次。

轨迹边界识别（R4 返工）：以 prompt 为本轮 user 文本前缀（宿主把
extra_user_content_parts 追加在 prompt 之后），从消息尾部定位；临时内容
（_no_save）在入库时剔除；定位失败时用登记的 user 消息与终态响应构造
保底配对，保证 completed 轮次必有 assistant。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api.event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest

from .identity import SharedIdentity, resolve_persona_scope
from .ledger import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    EpochStaleError,
    TurnLedger,
    sanitize_message,
    sanitize_messages,
)
from .scope import ScopeResolver

_AGENT_USER_ABORTED_EXTRA = "agent_user_aborted"
_HOST_ERR_TEXT_PREFIX = "LLM 响应错误"
DEFAULT_FAIL_WATCHDOG_SECONDS = 180.0


def _extract_text(content: Any) -> str:
    """从 str / part 列表 / Message / dict 中提取纯文本。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_extract_text(item) for item in content)
    if isinstance(content, dict):
        if content.get("type") in ("text",) and isinstance(content.get("text"), str):
            return content["text"]
        if "text" in content and isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return _extract_text(content.get("content"))
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    inner = getattr(content, "content", None)
    if inner is not None and inner is not content:
        return _extract_text(inner)
    return ""


def _message_to_dict(message: Any) -> dict[str, Any] | None:
    """把宿主 Message（pydantic）或 dict 规整为可存 dict；不可存返回 None。"""

    if isinstance(message, dict):
        return sanitize_message(message)
    if hasattr(message, "model_dump"):
        try:
            return sanitize_message(message.model_dump())
        except Exception:  # noqa: BLE001
            return None
    return None


@dataclass
class PendingTurn:
    """进程内活动轮次（读侧登记 → 终态化释放身份锁）。"""

    event_key: str
    identity: SharedIdentity
    epoch: int
    user_prompt_text: str
    registered_user_message: dict
    event: AstrMessageEvent
    release_lock: Callable[[], None]
    agent_done_response: Any | None = None
    agent_done_messages: list[Any] | None = None
    done_seen: bool = False
    committed: bool = False
    watchdog_task: asyncio.Task | None = field(default=None, repr=False)

    def fallback_user(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.registered_user_message))


class BridgeStats:
    def __init__(self) -> None:
        self.captured = 0
        self.skipped = 0
        self.committed_completed = 0
        self.committed_failed = 0
        self.committed_aborted = 0
        self.epoch_stale_rejected = 0
        self.watchdog_failures = 0


class ContextBridge:
    """协调一轮消息在共享账本中的完整生命周期。"""

    def __init__(
        self,
        *,
        ledger: TurnLedger,
        scope_resolver: ScopeResolver,
        persona_manager_getter: Callable[[], Any],
        max_history_turns: int | None = None,
        logger: Any = None,
        fail_watchdog_seconds: float = DEFAULT_FAIL_WATCHDOG_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._scope = scope_resolver
        self._get_persona_manager = persona_manager_getter
        self._max_history_turns = max_history_turns
        self._fail_watchdog_seconds = fail_watchdog_seconds
        self._log = logger
        self._pending: dict[str, PendingTurn] = {}
        self._identity_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self.stats = BridgeStats()

    # -- 内部 -------------------------------------------------------------
    def _info(self, msg: str) -> None:
        if self._log is not None:
            try:
                self._log.info(msg)
            except Exception:
                pass

    def _warn(self, msg: str) -> None:
        if self._log is not None:
            try:
                self._log.warning(msg)
            except Exception:
                pass

    async def _acquire_identity_lock(self, identity_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._identity_locks.get(identity_key)
            if lock is None:
                lock = asyncio.Lock()
                self._identity_locks[identity_key] = lock
            return lock

    def _release_identity_lock(self, identity_key: str) -> None:
        lock = self._identity_locks.get(identity_key)
        if lock is not None and lock.locked():
            lock.release()

    @staticmethod
    def event_key_for(event: AstrMessageEvent) -> str:
        """事件唯一键：UMO + 消息 ID（重复投递/重试同键）。"""

        message_id = getattr(event.message_obj, "message_id", "") or ""
        return f"{event.unified_msg_origin}#{message_id}"

    # -- 读侧 -------------------------------------------------------------
    async def handle_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> bool:
        """OnLLMRequestEvent 钩子处理。返回是否接管了本轮。

        未纳入时对 req 不做任何修改。接管时持有身份锁直至本轮终态化
        （completed / failed / aborted / interrupted），保证后继同身份轮次
        读取到本轮完整结果（R3）。
        """

        persona_scope = await resolve_persona_scope(
            self._get_persona_manager(), event, getattr(req, "conversation", None)
        )
        decision = self._scope.evaluate(event, persona_scope)
        if not decision.in_scope or decision.identity is None:
            self.stats.skipped += 1
            return False
        identity = decision.identity

        event_key = self.event_key_for(event)
        lock = await self._acquire_identity_lock(identity.key)
        await lock.acquire()
        released = False

        def _release() -> None:
            nonlocal released
            if not released:
                released = True
                self._release_identity_lock(identity.key)

        try:
            history = self._ledger.load_history(
                identity.key, max_turns=self._max_history_turns
            )
            user_message = self._build_user_message(req)
            turn = self._ledger.begin_turn(
                identity_key=identity.key,
                event_key=event_key,
                source_type="group" if event.get_group_id() else "private",
                source_id=event.get_group_id() or identity.sender_id,
                umo=event.unified_msg_origin,
                user_message=user_message,
            )
            if turn.status != STATUS_RUNNING:
                # 重复投递且原轮已终态：不重复接管，保持宿主原生行为
                self.stats.skipped += 1
                return False

            begin_dialogs = await self._persona_begin_dialogs(event, req)
            req.contexts = [*begin_dialogs, *history]
            req.conversation = None

            pending = PendingTurn(
                event_key=event_key,
                identity=identity,
                epoch=turn.epoch,
                user_prompt_text=(req.prompt or "").strip(),
                registered_user_message=user_message,
                event=event,
                release_lock=_release,
            )
            pending.watchdog_task = asyncio.create_task(self._fail_watchdog(pending))
            self._pending[event_key] = pending
            self.stats.captured += 1
            self._info(f"uctx 接管轮次（epoch={turn.epoch}，历史 {len(history)} 条）")
            return True
        except Exception:
            _release()
            raise

    def _build_user_message(self, req: ProviderRequest) -> dict[str, Any]:
        """本轮 user 消息的入库形态：文本 + 非临时附加内容 + 多模态占位。

        临时内容（TextPart.mark_as_temp / _no_save）在入库时剔除，不永久
        污染共享历史（R4）。
        """

        parts: list[dict[str, Any]] = []
        if (req.prompt or "").strip():
            parts.append({"type": "text", "text": req.prompt})
        for part in req.extra_user_content_parts or []:
            dumped = _message_to_dict({"role": "user", "content": [part]})
            if dumped and dumped.get("content"):
                parts.extend(dumped["content"])
        for _ in req.image_urls or []:
            parts.append({"type": "text", "text": "[图片]"})
        for _ in req.audio_urls or []:
            parts.append({"type": "text", "text": "[音频]"})
        if not parts:
            parts.append({"type": "text", "text": ""})
        return {"role": "user", "content": parts}

    async def _persona_begin_dialogs(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> list[dict[str, Any]]:
        """与宿主同参解析人格，回补开场白（每轮注入、不入库）。"""

        try:
            manager = self._get_persona_manager()
            if manager is None:
                return []
            conversation_persona_id = getattr(
                getattr(req, "conversation", None), "persona_id", None
            )
            result = await manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
            )
            persona = result[1] if result else None
            dialogs = (persona or {}).get("_begin_dialogs_processed") or []
            out: list[dict[str, Any]] = []
            for d in dialogs:
                cleaned = sanitize_message(dict(d))
                if cleaned is not None:
                    out.append(cleaned)
            return out
        except Exception:  # noqa: BLE001
            return []

    # -- 写侧：主提交点 -----------------------------------------------------
    async def handle_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        llm_response: Any,
    ) -> None:
        """主提交点（R2 v2）：成功 / aborted / run_agent 级异常在此终态化。"""

        event_key = self.event_key_for(event)
        pending = self._pending.get(event_key)
        if pending is None or pending.committed:
            return
        messages = getattr(run_context, "messages", None) or []
        pending.agent_done_response = llm_response
        pending.agent_done_messages = list(messages)
        pending.done_seen = True

        role = getattr(llm_response, "role", "")
        has_tool_calls = bool(getattr(llm_response, "tools_call_name", None))
        text = (getattr(llm_response, "completion_text", "") or "").strip()

        if role == "err":
            status = STATUS_FAILED
        elif event.is_stopped():
            # 用户停止是中止的决定性信号：覆盖 4.28 marker 与 4.26
            # 「aborted 回调保留完整/空文本」两种形态（R2）
            status = STATUS_ABORTED
        elif not text and not has_tool_calls:
            status = STATUS_FAILED
        else:
            status = STATUS_COMPLETED

        user_msg, trajectory = self._split_trajectory(pending)
        reply_text = text or None
        if status == STATUS_COMPLETED and not trajectory:
            trajectory = self._fallback_assistant(llm_response)
        self._finalize(pending, status, trajectory, reply_text)

    # -- 写侧：辅助轨 -------------------------------------------------------
    async def handle_decorating_result(self, event: AstrMessageEvent) -> None:
        """辅助轨：模型 err 加速检测（不做常规提交）。

        宿主在 run_agent 中间 yield 时也会执行装饰阶段（工具轮的中间
        输出），因此这里只在「尚未见过完成钩子 + 事件结果为宿主错误文案」
        （模型 err 终局，不会再有 on_agent_done）时立即 failed；其余情形
        由 on_agent_done 或 fail-watchdog 收尾。
        """

        event_key = self.event_key_for(event)
        pending = self._pending.get(event_key)
        if pending is None or pending.committed or pending.done_seen:
            return
        result = event.get_result()
        text = ""
        if result is not None:
            try:
                text = (result.get_plain_text() or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
        if text.startswith(_HOST_ERR_TEXT_PREFIX):
            self._finalize(pending, STATUS_FAILED, [], None)

    async def handle_after_message_sent(self, event: AstrMessageEvent) -> None:
        """发送标记 + 停止旗标兜底（aborted 无输出路径可能缺其他钩子）。"""

        event_key = self.event_key_for(event)
        try:
            self._ledger.mark_turn_sent(event_key)
        except Exception:  # noqa: BLE001
            self._warn("uctx 发送状态标记失败")
        pending = self._pending.get(event_key)
        if pending is None or pending.committed:
            return
        if event.is_stopped() or event.get_extra(_AGENT_USER_ABORTED_EXTRA) is True:
            reply = getattr(pending.agent_done_response, "completion_text", "") or None
            self._finalize(pending, STATUS_ABORTED, [], reply)

    async def _fail_watchdog(self, pending: PendingTurn) -> None:
        """受控失败兜底：超时仍 running（模型 err / 挂起 / 钩子缺失）→ failed。"""

        try:
            await asyncio.sleep(self._fail_watchdog_seconds)
        except asyncio.CancelledError:
            return
        if pending.committed:
            return
        self.stats.watchdog_failures += 1
        self._warn("uctx 轮次超时未终态化，按受控失败收尾")
        self._finalize(pending, STATUS_FAILED, [], None)

    # -- 轨迹提取（R4） -----------------------------------------------------
    def _split_trajectory(
        self, pending: PendingTurn
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """定位本轮 user（prompt 为前缀；extra parts 追加在 prompt 之后）。"""

        messages = pending.agent_done_messages or []
        prompt = pending.user_prompt_text
        idx = -1
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if getattr(m, "role", None) != "user":
                continue
            text = _extract_text(getattr(m, "content", None))
            if prompt:
                if text.startswith(prompt):
                    idx = i
                    break
            else:
                idx = i
                break
        if idx < 0:
            return None, []
        user_dict = _message_to_dict(messages[idx]) or pending.fallback_user()
        rest = [_message_to_dict(m) for m in messages[idx + 1 :]]
        return user_dict, [r for r in rest if r is not None]

    @staticmethod
    def _fallback_assistant(llm_response: Any) -> list[dict[str, Any]]:
        """轨迹缺失时用终态响应构造保底 assistant，保证 completed 配对。"""

        text = (getattr(llm_response, "completion_text", "") or "").strip()
        tool_calls = getattr(llm_response, "tools_call_name", None)
        if text:
            return [{"role": "assistant", "content": [{"type": "text", "text": text}]}]
        if tool_calls:
            try:
                dumped = [
                    tc.model_dump()
                    for tc in llm_response.to_openai_tool_calls_model()
                ]
                return [{"role": "assistant", "content": None, "tool_calls": dumped}]
            except Exception:  # noqa: BLE001
                return []
        return []

    # -- 终态化 -------------------------------------------------------------
    def _finalize(
        self,
        pending: PendingTurn,
        status: str,
        trajectory: list[dict[str, Any]],
        reply_text: str | None,
    ) -> None:
        if pending.committed:
            pending.release_lock()
            return
        if pending.watchdog_task is not None and not pending.watchdog_task.done():
            pending.watchdog_task.cancel()
        try:
            self._ledger.commit_turn(
                event_key=pending.event_key,
                status=status,
                trajectory=sanitize_messages(trajectory),
                reply_text=reply_text,
            )
            pending.committed = True
            if status == STATUS_COMPLETED:
                self.stats.committed_completed += 1
            elif status == STATUS_FAILED:
                self.stats.committed_failed += 1
            else:
                self.stats.committed_aborted += 1
        except EpochStaleError:
            pending.committed = True  # 已被清空吞没，不复活旧历史
            self.stats.epoch_stale_rejected += 1
            self._info("uctx 轮次因 epoch 清空被丢弃（慢请求不复活旧历史）")
        finally:
            self._pending.pop(pending.event_key, None)
            pending.release_lock()

    # -- 兜底 -------------------------------------------------------------
    def finalize_pending_as_interrupted(self) -> int:
        """插件卸载/重载时：未决轮次 → interrupted（可恢复，不伪成功）。"""

        count = 0
        for pending in list(self._pending.values()):
            if pending.committed:
                continue
            self._finalize(pending, STATUS_INTERRUPTED, [], None)
            count += 1
        return count

    @property
    def pending_count(self) -> int:
        return sum(1 for p in self._pending.values() if not p.committed)
