"""微信驱动抽象层。

整个项目只有 drivers/ 目录下的代码可以接触具体的微信自动化库（wxautox4 等）。
业务代码只依赖这里定义的接口，换驱动或写测试时用 FakeDriver 即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MessageAttr(str, Enum):
    FRIEND = "friend"   # 别人发的
    SELF = "self"       # 自己发的（含手机端同步过来的）
    SYSTEM = "system"   # 系统消息，如“xx 加入了群聊”
    OTHER = "other"


class ChatType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


@dataclass
class IncomingMessage:
    chat: str                       # 会话名（私聊对方昵称/备注，或群名）
    chat_type: ChatType
    attr: MessageAttr
    type: str                       # text / image / voice / quote / file ...
    sender: str
    content: str
    time: datetime = field(default_factory=datetime.now)
    is_at_me: bool = False
    raw: Any = None                 # 驱动原始对象，仅驱动内部使用


@dataclass
class SendResult:
    ok: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


MessageCallback = Callable[[IncomingMessage], None]


class WeChatDriver(ABC):
    @abstractmethod
    def connect(self) -> str:
        """连接微信客户端，返回当前登录账号昵称。"""

    @abstractmethod
    def is_online(self) -> bool: ...

    @abstractmethod
    def listen(self, chat: str, callback: MessageCallback) -> SendResult:
        """开始监听一个会话，新消息到达时调用 callback。"""

    @abstractmethod
    def unlisten(self, chat: str) -> None: ...

    @abstractmethod
    def send_text(self, chat: str, text: str, at: str | list[str] | None = None) -> SendResult: ...

    @abstractmethod
    def send_file(self, chat: str, path: str) -> SendResult: ...

    @abstractmethod
    def stop(self) -> None: ...
