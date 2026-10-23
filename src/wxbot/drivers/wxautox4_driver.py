"""wxautox4 适配器（Windows + 已激活的 wxautox4）。

调用方式参考了 SiverWXbot_plus 的 wxbot_core.py（Apache-2.0），已重写为驱动接口。
wxautox4 通过 UI 自动化操作微信窗口，所有调用必须串行，这里统一加锁。
"""

from __future__ import annotations

import logging
import threading

from wxbot.drivers.base import (
    ChatType,
    IncomingMessage,
    MessageAttr,
    MessageCallback,
    SendResult,
    WeChatDriver,
)

log = logging.getLogger(__name__)


def _to_result(resp) -> SendResult:
    # wxautox4 返回 WxResponse（dict 形态，含 status / message）；SendFiles 成功时有时返回 None
    if resp is None:
        return SendResult(True)
    try:
        ok = bool(resp)
        msg = resp.get("message", "") if hasattr(resp, "get") else ""
    except Exception:  # noqa: BLE001
        ok, msg = True, ""
    return SendResult(ok, str(msg or ""))


class WxAutoX4Driver(WeChatDriver):
    def __init__(self, groups: set[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._wx = None
        self._groups = groups or set()
        self.nickname = ""

    def connect(self) -> str:
        from wxautox4 import WeChat, WxParam
        from wxautox4.utils.useful import check_license

        if not check_license():
            raise RuntimeError("wxautox4 未激活，请先运行：python -m wxbot activate <激活码>")
        # 与 SiverWXbot_plus 相同的经验参数：开启消息哈希去重、启动时重新校准偏移
        WxParam.MESSAGE_HASH = True
        WxParam.FORCE_MESSAGE_XBIAS = True
        with self._lock:
            self._wx = WeChat()
            self.nickname = self._wx.nickname
            self._wx.StartListening()
        return self.nickname

    def is_online(self) -> bool:
        with self._lock:
            return bool(self._wx and self._wx.IsOnline())

    def _wrap(self, callback: MessageCallback):
        def handler(msg, chat):
            try:
                attr = getattr(msg, "attr", "other")
                is_group = chat.who in self._groups or getattr(chat, "chat_type", "") == "group"
                content = str(getattr(msg, "content", ""))
                callback(
                    IncomingMessage(
                        chat=chat.who,
                        chat_type=ChatType.GROUP if is_group else ChatType.PRIVATE,
                        attr=MessageAttr(attr) if attr in MessageAttr._value2member_map_ else MessageAttr.OTHER,
                        type=str(getattr(msg, "type", "text")),
                        sender=str(getattr(msg, "sender", "")),
                        content=content,
                        is_at_me=bool(self.nickname) and f"@{self.nickname}" in content,
                        raw=msg,
                    )
                )
            except Exception:
                log.exception("处理 wxautox4 回调出错")

        return handler

    def listen(self, chat: str, callback: MessageCallback) -> SendResult:
        with self._lock:
            return _to_result(self._wx.AddListenChat(nickname=chat, callback=self._wrap(callback)))

    def unlisten(self, chat: str) -> None:
        with self._lock:
            self._wx.RemoveListenChat(nickname=chat)

    def send_text(self, chat, text, at=None) -> SendResult:
        with self._lock:
            return _to_result(self._wx.SendMsg(msg=text, who=chat, at=at))

    def send_file(self, chat, path) -> SendResult:
        with self._lock:
            return _to_result(self._wx.SendFiles(filepath=path, who=chat))

    def stop(self) -> None:
        with self._lock:
            if self._wx is not None:
                self._wx.StopListening()


def activate(code: str) -> bool:
    from wxautox4.utils.useful import authenticate

    return bool(authenticate(code))
