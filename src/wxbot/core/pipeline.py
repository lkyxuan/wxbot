"""消息处理链：每条消息依次交给各个 Handler，第一个返回 True 的 Handler 消费掉它。

新增功能 = 在 features/ 写一个 Handler 并在 bot.py 的 build_pipeline 里注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from wxbot.drivers.base import IncomingMessage

if TYPE_CHECKING:
    from wxbot.core.bot import Bot


@dataclass
class Context:
    bot: Bot
    msg: IncomingMessage

    def reply(self, text: str) -> bool:
        return self.bot.reply(self.msg, text)


class Handler(Protocol):
    name: str

    def handle(self, ctx: Context) -> bool:
        """处理了就返回 True（后续 Handler 不再执行）。"""
        ...


class Pipeline:
    def __init__(self, handlers: list[Handler]) -> None:
        self.handlers = handlers

    def run(self, ctx: Context) -> str | None:
        for h in self.handlers:
            if h.handle(ctx):
                return h.name
        return None
