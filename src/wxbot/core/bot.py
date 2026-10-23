from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime

from wxbot.ai.base import AIClient, build_clients
from wxbot.config import Settings
from wxbot.core.pipeline import Context, Pipeline
from wxbot.drivers.base import ChatType, IncomingMessage, MessageAttr, WeChatDriver
from wxbot.memory.store import MemoryItem, MemoryStore
from wxbot.notify.webhook import notify

log = logging.getLogger(__name__)

MAX_SEGMENT = 2000   # 微信单条消息过长时分段发送


def build_pipeline() -> Pipeline:
    from wxbot.features.admin_commands import AdminCommands
    from wxbot.features.ai_reply import AIReply
    from wxbot.features.keyword_reply import KeywordReply

    return Pipeline([AdminCommands(), KeywordReply(), AIReply()])


class Bot:
    def __init__(self, settings: Settings, driver: WeChatDriver,
                 ai_clients: dict[str, AIClient] | None = None, sleep=time.sleep) -> None:
        self.settings = settings
        self.driver = driver
        self.ai_clients = ai_clients if ai_clients is not None else build_clients(settings.ai.providers)
        self.memory = (MemoryStore(settings.memory.dir, settings.memory.max_count)
                       if settings.memory.enabled else None)
        self.pipeline = build_pipeline()
        self.nickname = ""
        self.started_at: datetime | None = None
        self.received = 0
        self._sleep = sleep
        # wxautox4 的监听回调在线程池里并发执行；同一会话的消息必须串行处理，
        # 否则“写入记忆 → 取历史 → 回复”会与同会话的下一条消息交错，AI 上下文错乱
        self._chat_locks: dict[str, threading.Lock] = {}
        self._chat_locks_guard = threading.Lock()

    # ---- 生命周期 ----
    def start(self) -> None:
        self.nickname = self.driver.connect()
        self.started_at = datetime.now()
        chats = [self.settings.admin_chat, *self.settings.listen.users, *self.settings.listen.groups]
        for chat in dict.fromkeys(chats):
            result = self.driver.listen(chat, self.on_message)
            log.info("监听 %s：%s", chat, "成功" if result else f"失败 {result.message}")
        log.info("机器人已启动，账号：%s", self.nickname)

    def stop(self) -> None:
        self.driver.stop()

    def run_forever(self, check_interval: float = 30) -> None:
        self.start()
        try:
            while True:
                self._sleep(check_interval)
                if not self.driver.is_online():
                    self.alert("微信已离线", f"账号 {self.nickname} 掉线了")
        except KeyboardInterrupt:
            log.info("收到退出信号")
        finally:
            self.stop()

    # ---- 消息 ----
    def _chat_lock(self, chat: str) -> threading.Lock:
        with self._chat_locks_guard:
            return self._chat_locks.setdefault(chat, threading.Lock())

    def on_message(self, msg: IncomingMessage) -> None:
        with self._chat_lock(msg.chat):
            self._handle(msg)

    def _handle(self, msg: IncomingMessage) -> None:
        log.info("[%s] %s(%s): %s", msg.chat, msg.sender, msg.attr.value, msg.content)
        if msg.attr == MessageAttr.FRIEND:
            self.received += 1
            self._remember(msg.chat, msg.sender, msg.content, is_self=False)
        try:
            self.pipeline.run(Context(self, msg))
        except Exception as e:
            log.exception("处理消息出错")
            self.alert("处理消息出错", f"{msg.chat}: {e}")

    def reply(self, msg: IncomingMessage, text: str) -> bool:
        cfg = self.settings.reply_delay
        if cfg.enabled and msg.attr == MessageAttr.FRIEND:
            self._sleep(random.uniform(cfg.min_seconds, cfg.max_seconds))
        at = msg.sender if (msg.chat_type == ChatType.GROUP and self.settings.group.at_sender) else None
        ok = True
        for i in range(0, max(len(text), 1), MAX_SEGMENT):
            result = self.driver.send_text(msg.chat, text[i:i + MAX_SEGMENT], at=at if i == 0 else None)
            ok = ok and bool(result)
        if ok:
            self._remember(msg.chat, self.nickname, text, is_self=True)
        else:
            self.alert("发送失败", f"{msg.chat}: {text[:100]}")
        return ok

    # ---- 工具 ----
    def _remember(self, chat: str, sender: str, content: str, is_self: bool) -> None:
        if self.memory:
            self.memory.add(chat, MemoryItem(datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                             sender, content, is_self))

    def alert(self, title: str, content: str) -> None:
        log.warning("%s：%s", title, content)
        notify(self.settings.notify.webhook_url, f"[wxbot] {title}", content)

    def status_text(self) -> str:
        started = self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else "未启动"
        return (f"账号：{self.nickname}\n启动时间：{started}\n已收消息：{self.received}\n"
                f"在线：{'是' if self.driver.is_online() else '否'}")
