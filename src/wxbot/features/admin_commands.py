"""管理员命令：在 admin_chat 会话里发送 / 开头的指令。"""

from __future__ import annotations

from wxbot.core.pipeline import Context
from wxbot.drivers.base import MessageAttr

HELP = "可用命令：\n/状态\n/用户列表\n/群列表\n/清除记忆 会话名"


class AdminCommands:
    name = "admin"

    def handle(self, ctx: Context) -> bool:
        msg, bot = ctx.msg, ctx.bot
        if msg.chat != bot.settings.admin_chat or not msg.content.startswith("/"):
            return False
        if msg.attr not in (MessageAttr.SELF, MessageAttr.FRIEND):
            return False
        cmd, _, arg = msg.content.partition(" ")
        if cmd in ("/帮助", "/指令"):
            ctx.reply(HELP)
        elif cmd == "/状态":
            ctx.reply(bot.status_text())
        elif cmd == "/用户列表":
            ctx.reply("当前用户：\n" + ", ".join(bot.settings.listen.users))
        elif cmd == "/群列表":
            ctx.reply("当前群：\n" + ", ".join(bot.settings.listen.groups))
        elif cmd == "/清除记忆" and arg.strip():
            if bot.memory:
                bot.memory.clear(arg.strip())
            ctx.reply(f"已清除 {arg.strip()} 的记忆")
        else:
            ctx.reply("未知命令。\n" + HELP)
        return True
