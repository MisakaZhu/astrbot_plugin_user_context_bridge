"""测试替身：最小假事件 / 假 Provider / 假会话管理器。

设计原则：只替换网络与平台边界（模型、QQ 传输），宿主的 Runner、钩子分发、
消息装配、写回逻辑全部使用 astrbot 包内真实代码。

本文件为合成测试夹具，不包含任何真实账号、群号或配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.provider import Provider, ProviderMeta


def make_platform_meta(platform_id: str = "aiocqhttp") -> PlatformMetadata:
    return PlatformMetadata(
        name="aiocqhttp",
        description="fake platform for tests",
        id=platform_id,
    )


def make_message_obj(
    *,
    sender_id: str,
    group_id: str = "",
    message_str: str = "你好",
    self_id: str = "bot_001",
) -> AstrBotMessage:
    abm = AstrBotMessage()
    abm.type = (
        MessageType.GROUP_MESSAGE if group_id else MessageType.FRIEND_MESSAGE
    )
    abm.self_id = self_id
    abm.sender = MessageMember(user_id=sender_id, nickname=f"测试用户{sender_id[-2:]}")
    abm.group = Group(group_id=group_id) if group_id else None
    abm.message_str = message_str
    abm.message_id = f"mock-msg-{sender_id}-{id(abm)}"
    abm.session_id = group_id or sender_id
    abm.raw_message = None
    abm.message = []
    return abm


class FakeEvent(AstrMessageEvent):
    """最小事件实现：记录 send 的内容，供断言最终回复目标与内容。"""

    def __init__(
        self,
        *,
        sender_id: str,
        group_id: str = "",
        message_str: str = "你好",
        platform_id: str = "aiocqhttp",
        self_id: str = "bot_001",
        role: str = "member",
    ) -> None:
        self.message_str = message_str
        self.message_obj = make_message_obj(
            sender_id=sender_id,
            group_id=group_id,
            message_str=message_str,
            self_id=self_id,
        )
        self.platform_meta = make_platform_meta(platform_id)
        message_type = (
            MessageType.GROUP_MESSAGE if group_id else MessageType.FRIEND_MESSAGE
        )
        self.session = MessageSession(
            platform_name=platform_id,
            message_type=message_type,
            session_id=group_id or sender_id,
        )
        self.role = role
        self.is_wake = True
        self.is_at_or_wake_command = bool(group_id) is False
        self._extras: dict[str, Any] = {}
        self._force_stopped = False
        self._result = None
        self.sent_chains: list[MessageChain] = []
        self._has_send_oper = False
        self.created_at = 0.0
        # trace 属性在宿主某些路径会被调用，这里给一个最小替身
        self.trace = _NullTrace()
        self.plugins_name = None
        self.platform = self.platform_meta

    @property
    def unified_msg_origin(self) -> str:
        return str(self.session)

    async def send(self, message_chain: MessageChain):
        self.sent_chains.append(message_chain)
        self._has_send_oper = True


class _NullTrace:
    def record(self, *args, **kwargs):
        return None


class FakeProvider(Provider):
    """可控假模型：记录每次请求的完整入参，返回预设回复。

    reply_script: 依次弹出的回复列表（支持多轮工具循环场景）；耗尽后重复最后一个。
    """

    def __init__(self, reply_script: list[str] | None = None) -> None:
        super().__init__(
            provider_config={
                "id": "fake_provider",
                "type": "openai",
                "name": "fake-provider",
                "model": "fake-model",
                "key": ["test-key"],
                "api_base": "http://127.0.0.1:0/v1",
                "max_context_tokens": 128000,
            },
            provider_settings={},
        )
        self.model_name = "fake-model"
        self.reply_script = list(reply_script or ["这是假模型的固定回复。"])
        self.call_log: list[dict[str, Any]] = []
        self.error_script: list[Exception | None] = []

    def meta(self) -> ProviderMeta:  # noqa: D102 - 绕过全局注册表
        return ProviderMeta(
            id="fake_provider",
            model=self.get_model(),
            type="openai",
        )

    def get_current_key(self) -> str:
        return "test-key"

    def set_key(self, key: str) -> None:
        pass

    async def get_models(self) -> list[str]:
        return ["fake-model"]

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool=None,
        contexts=None,
        system_prompt: str | None = None,
        tool_calls_result=None,
        model: str | None = None,
        extra_user_content_parts=None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_log.append(
            {
                "prompt": prompt,
                "contexts": [
                    m if isinstance(m, dict) else m.model_dump() for m in (contexts or [])
                ],
                "system_prompt": system_prompt,
            }
        )
        if self.error_script:
            err = self.error_script.pop(0)
            if err is not None:
                raise err
        if len(self.reply_script) > 1:
            reply = self.reply_script.pop(0)
        else:
            reply = self.reply_script[0]
        return LLMResponse(role="assistant", completion_text=reply)

    async def text_chat_stream(self, **kwargs):
        resp = await self.text_chat(**kwargs)
        yield resp


@dataclass
class FakeConversation:
    """宿主 Conversation PO 的最小替身（v1 兼容形态）。"""

    platform_id: str = "aiocqhttp"
    user_id: str = ""
    cid: str = "cid-original-window"
    history: str = "[]"
    title: str | None = None
    persona_id: str | None = None
    created_at: int = 0
    updated_at: int = 0
    token_usage: int = 0


class FakeConversationManager:
    """记录 update_conversation 调用，用于证明宿主是否发生写回。"""

    def __init__(self) -> None:
        self.update_calls: list[dict[str, Any]] = []

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: int | None = None,
    ) -> None:
        self.update_calls.append(
            {
                "umo": unified_msg_origin,
                "cid": conversation_id,
                "history": history,
                "token_usage": token_usage,
            }
        )


@dataclass
class HookSpy:
    """记录钩子观察到的数据，供断言。"""

    llm_request_seen: list[ProviderRequest] = field(default_factory=list)
    llm_response_seen: list[LLMResponse] = field(default_factory=list)
    agent_done_seen: list[dict[str, Any]] = field(default_factory=list)
