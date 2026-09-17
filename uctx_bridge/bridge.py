"""轮次生命周期协调器（ADR-002/003）：读侧接管 + 写侧三轨。

读侧（on_llm_request，即宿主 OnLLMRequestEvent 钩子内调用）：
    范围判定 → 解析人格 → 读取共享历史快照 → 登记轮次（去重）→
    ``req.contexts = 人格开场白 + 共享历史`` → ``req.conversation = None``
    （短路宿主对原窗口的整段写回）。人格、系统提示、工具集、其他插件
    动态内容原样保留。

写侧（三轨）：
    轨 1 OnAgentDoneEvent：缓存终态响应与消息轨迹快照（不提交）；
    轨 2 OnDecoratingResultEvent：**唯一提交点**——此时 agent_user_aborted
        旗标已可用，按终态判定 commit_turn；
    轨 3 恢复：插件 terminate 时未决轮次 → interrupted；重启由账本
        recover_running 兜底。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api.event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest

from .identity import SharedIdentity, resolve_persona_scope
from .ledger import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    EpochStaleError,
    TurnLedger,
    sanitize_message,
    sanitize_messages,
)
from .scope import ScopeResolver

_AGENT_USER_ABORTED_EXTRA = "agent_user_aborted"


def _extract_text(content: Any) -> str:
    """从 str / part 列表 / Message / dict 中提取纯文本（与测试同构）。"""

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
    """进程内活动轮次（读侧登记 → 写侧提交）。"""

    event_key: str
    identity: SharedIdentity
    epoch: int
    user_prompt_text: str
    user_fingerprint: str
    agent_done_response: Any | None = None
    agent_done_messages: list[Any] | None = None
    done_seen: bool = False
    committed: bool = False

    def fallback_user(self) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "text", "text": ""}]}


@dataclass
class BridgeStats:
    captured: int = 0
    skipped: int = 0
    committed_completed: int = 0
    committed_failed: int = 0
    committed_aborted: int = 0
    epoch_stale_rejected: int = 0


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
    ) -> None:
        self._ledger = ledger
        self._scope = scope_resolver
        self._get_persona_manager = persona_manager_getter
        self._max_history_turns = max_history_turns
        self._log = logger
        self._pending: dict[str, PendingTurn] = {}
        self._identity_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self.stats = BridgeStats()

    # -- 内部 -------------------------------------------------------------
    def _logger(self) -> Any:
        return self._log

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

    async def _identity_lock(self, identity_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._identity_locks.get(identity_key)
            if lock is None:
                lock = asyncio.Lock()
                self._identity_locks[identity_key] = lock
            return lock

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
        """OnLLMRequestEvent 钩子处理。返回是否接管了本轮（True=已纳入共享）。

        未纳入时对 req 不做任何修改，宿主保持原生行为。
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
        prompt_text = (req.prompt or "").strip()
        fingerprint = hashlib.sha256(
            f"{identity.key}|{prompt_text}|{len(req.image_urls)}|{len(req.audio_urls)}".encode(
                "utf-8"
            )
        ).hexdigest()

        # 读侧身份锁：快照 + 登记 原子化（同身份串行；不同身份并行）
        async with await self._identity_lock(identity.key):
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

        begin_dialogs = await self._persona_begin_dialogs(event, req)
        req.contexts = [*begin_dialogs, *history]
        # 短路宿主写回：宿主 _save_to_history 以 req.conversation None 直接返回
        req.conversation = None

        self._pending[event_key] = PendingTurn(
            event_key=event_key,
            identity=identity,
            epoch=turn.epoch,
            user_prompt_text=prompt_text,
            user_fingerprint=fingerprint,
        )
        self.stats.captured += 1
        self._info(
            f"uctx 接管轮次 {event_key}（身份已脱敏，epoch={turn.epoch}，"
            f"历史 {len(history)} 条）"
        )
        return True

    def _build_user_message(self, req: ProviderRequest) -> dict[str, Any]:
        """本轮 user 消息的入库形态：文本 + 多模态占位。"""

        parts: list[dict[str, Any]] = []
        if (req.prompt or "").strip():
            parts.append({"type": "text", "text": req.prompt})
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
        """与宿主同参解析人格，回补开场白（保持人格行为一致）。

        开场白每轮注入、不入库（宿主在 conversation.history 中重复累积的行为
        不复制到共享账本）。
        """

        try:
            manager = self._get_persona_manager()
            if manager is None:
                return []
            conversation_persona_id = getattr(
                getattr(req, "conversation", None), "persona_id", None
            )
            # 注意：此调用发生在我们置 None 之前（handle_llm_request 顺序保证）
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
        except Exception:  # noqa: BLE001 - 开场白回补失败不阻塞本轮
            return []

    # -- 写侧 -------------------------------------------------------------
    async def handle_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        llm_response: Any,
    ) -> None:
        """轨 1：缓存轨迹快照（此时刻 aborted 旗标尚未写入，不做终态判定）。"""

        event_key = self.event_key_for(event)
        pending = self._pending.get(event_key)
        if pending is None or pending.committed:
            return
        messages = getattr(run_context, "messages", None) or []
        pending.agent_done_response = llm_response
        pending.agent_done_messages = list(messages)
        pending.done_seen = True

    async def handle_decorating_result(self, event: AstrMessageEvent) -> None:
        """轨 2：唯一提交点。此时旗标可用，按终态判定提交账本。"""

        event_key = self.event_key_for(event)
        pending = self._pending.get(event_key)
        if pending is None or pending.committed:
            return
        aborted = event.get_extra(_AGENT_USER_ABORTED_EXTRA) is True

        trajectory: list[dict[str, Any]] = []
        reply_text: str | None = None
        if pending.agent_done_messages is not None:
            user_msg, trajectory = self._split_trajectory(pending)
            resp = pending.agent_done_response
            resp_text = _extract_text(getattr(resp, "result_chain", None)) or (
                getattr(resp, "completion_text", "") or ""
            )
            reply_text = resp_text or None

        if aborted:
            status = STATUS_ABORTED
        elif pending.done_seen and pending.agent_done_response is not None:
            resp = pending.agent_done_response
            role = getattr(resp, "role", "")
            has_tool_calls = bool(getattr(resp, "tools_call_name", None))
            text = (getattr(resp, "completion_text", "") or "").strip()
            if role == "err":
                status = STATUS_FAILED
            elif not text and not has_tool_calls:
                # 与宿主一致：空回复不产生成功记录
                status = STATUS_FAILED
            else:
                status = STATUS_COMPLETED
        else:
            # 未经过完成钩子（模型 err / 中断等）→ 失败
            status = STATUS_FAILED

        self._commit(pending, status, trajectory, reply_text)

    def _split_trajectory(
        self, pending: PendingTurn
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """从消息快照中定位本轮 user（指纹），切出 (user 消息, 其后轨迹)。"""

        messages = pending.agent_done_messages or []
        prompt = pending.user_prompt_text
        idx = -1
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if getattr(m, "role", None) != "user":
                continue
            content = getattr(m, "content", None)
            text = _extract_text(content)
            if prompt and text == prompt:
                idx = i
                break
            if not prompt:
                idx = i
                break
        if idx < 0:
            # 指纹定位失败（极端压缩/重写）：退化为空 user、全量轨迹由调用方清理
            return None, []
        user_dict = _message_to_dict(messages[idx]) or pending.fallback_user()
        rest = [_message_to_dict(m) for m in messages[idx + 1 :]]
        return user_dict, [r for r in rest if r is not None]

    def _commit(
        self,
        pending: PendingTurn,
        status: str,
        trajectory: list[dict[str, Any]],
        reply_text: str | None,
    ) -> None:
        try:
            self._ledger.commit_turn(
                event_key=pending.event_key,
                status=status,
                trajectory=trajectory,
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
            pending.committed = True  # 已被清空吞没，不复活
            self.stats.epoch_stale_rejected += 1
            self._info("uctx 轮次因 epoch 清空被丢弃（慢请求不复活旧历史）")
        finally:
            self._pending.pop(pending.event_key, None)

    async def handle_after_message_sent(self, event: AstrMessageEvent) -> None:
        """发送成功标记（发送失败/未确认时该钩子不触发，send_state 保持 NULL）。"""

        event_key = self.event_key_for(event)
        try:
            self._ledger.mark_turn_sent(event_key)
        except Exception:  # noqa: BLE001
            self._warn("uctx 发送状态标记失败")

    # -- 兜底 -------------------------------------------------------------
    def finalize_pending_as_interrupted(self) -> int:
        """插件卸载/重载时：未决轮次 → interrupted（可恢复，不伪成功）。"""

        count = 0
        for pending in list(self._pending.values()):
            if pending.committed:
                continue
            self._commit(pending, "interrupted", [], None)
            count += 1
        return count

    @property
    def pending_count(self) -> int:
        return sum(1 for p in self._pending.values() if not p.committed)
