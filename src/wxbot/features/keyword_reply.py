from __future__ import annotations

from wxbot.core.pipeline import Context
from wxbot.drivers.base import MessageAttr


class KeywordReply:
    name = "keyword"

    def handle(self, ctx: Context) -> bool:
        cfg = ctx.bot.settings.keyword_reply
        if not cfg.enabled or ctx.msg.attr != MessageAttr.FRIEND:
            return False
        for keyword, answer in cfg.rules.items():
            if keyword in ctx.msg.content:
                ctx.reply(answer)
                return True
        return False
