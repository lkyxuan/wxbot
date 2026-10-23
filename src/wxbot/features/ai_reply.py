from __future__ import annotations

import logging

from wxbot.ai.base import ChatTurn
from wxbot.core.pipeline import Context
from wxbot.drivers.base import ChatType, MessageAttr

log = logging.getLogger(__name__)


class AIReply:
    name = "ai"

    def handle(self, ctx: Context) -> bool:
        bot, msg = ctx.bot, ctx.msg
        if msg.attr != MessageAttr.FRIEND or msg.type not in ("text", "quote", "voice"):
            return False
        if msg.chat_type == ChatType.GROUP and bot.settings.group.reply_only_when_at and not msg.is_at_me:
            return False
        client = bot.ai_clients.get(bot.settings.ai.default)
        if client is None:
            return False
        history: list[ChatTurn] = []
        if bot.memory:
            n = bot.settings.memory.context_count
            # 当前这条已在 Bot.on_message 里写入记忆，取历史时排除掉最后一条
            for item in bot.memory.recent(msg.chat, n + 1)[:-1]:
                if item.is_self:
                    history.append(ChatTurn("assistant", item.content))
                else:
                    prefix = f"{item.sender}: " if msg.chat_type == ChatType.GROUP else ""
                    history.append(ChatTurn("user", prefix + item.content))
        text = msg.content
        if msg.chat_type == ChatType.GROUP:
            text = f"{msg.sender}: {text}"
        try:
            answer = client.chat(text, prompt=bot.settings.prompt_for(msg.chat), history=history)
        except Exception as e:  # noqa: BLE001
            log.error("AI 接口调用失败：%s", e)
            bot.alert("AI 接口调用失败", f"{msg.chat}: {e}")
            answer = bot.settings.ai.error_reply
        if answer:
            ctx.reply(answer)
        return True
