"""内存中的假驱动：不需要微信和 Windows，用于单元测试和本地调试。"""

from __future__ import annotations

from wxbot.drivers.base import IncomingMessage, MessageCallback, SendResult, WeChatDriver


class FakeDriver(WeChatDriver):
    def __init__(self, nickname: str = "测试机器人") -> None:
        self.nickname = nickname
        self.callbacks: dict[str, MessageCallback] = {}
        self.sent: list[tuple[str, str, object]] = []   # (chat, 内容, at)

    def connect(self) -> str:
        return self.nickname

    def is_online(self) -> bool:
        return True

    def listen(self, chat: str, callback: MessageCallback) -> SendResult:
        self.callbacks[chat] = callback
        return SendResult(True)

    def unlisten(self, chat: str) -> None:
        self.callbacks.pop(chat, None)

    def send_text(self, chat, text, at=None) -> SendResult:
        self.sent.append((chat, text, at))
        return SendResult(True)

    def send_file(self, chat, path) -> SendResult:
        self.sent.append((chat, f"[文件]{path}", None))
        return SendResult(True)

    def stop(self) -> None:
        self.callbacks.clear()

    # 测试辅助：模拟收到一条消息
    def push(self, msg: IncomingMessage) -> None:
        self.callbacks[msg.chat](msg)
