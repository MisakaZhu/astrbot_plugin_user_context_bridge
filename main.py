"""astrbot_plugin_user_context_bridge

同一 QQ 用户在同一机器人、同一人格及明确启用的群聊与私聊之间，
共享连续真实对话历史。

技术路线见 docs/ADR.md（ADR-001 ~ ADR-007）。当前为 P0 骨架：
插件类与生命周期已就位；共享身份、范围判定、轮次账本与钩子接线
在 P1 ~ P4 逐步落地。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_user_context_bridge",
    "Ewnscat-ya",
    "同一用户跨会话上下文共享（群聊/私聊连续真实对话历史）",
    "0.1.0",
)
class UserContextBridgePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # P1+: 身份/范围解析器、轮次仓储、生命周期协调器在此装配

    async def initialize(self) -> None:
        # P2+: 打开插件数据目录中的 SQLite 轮次账本，恢复 interrupted 轮次
        await super().initialize()

    async def terminate(self) -> None:
        # P2+: 释放进行中轮次的处理权（可恢复检查点），关闭数据库连接
        await super().terminate()
