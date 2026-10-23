from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from wxbot.config import ProviderConfig

log = logging.getLogger(__name__)


@dataclass
class ChatTurn:
    role: str       # "user" / "assistant"
    content: str


class AIClient(ABC):
    @abstractmethod
    def chat(self, message: str, prompt: str = "", history: list[ChatTurn] | None = None,
             image_path: str = "") -> str: ...


class FallbackClient(AIClient):
    """主接口失败时自动改用备用接口（对应 SiverWXbot_plus 的 fallback_api_index）。"""

    def __init__(self, primary: AIClient, fallback: AIClient) -> None:
        self.primary, self.fallback = primary, fallback

    def chat(self, message, prompt="", history=None, image_path=""):
        try:
            return self.primary.chat(message, prompt, history, image_path)
        except Exception as e:  # noqa: BLE001
            log.warning("主接口失败，改用备用接口：%s", e)
            return self.fallback.chat(message, prompt, history, image_path)


def _make(cfg: ProviderConfig) -> AIClient:
    if cfg.type == "openai_compat":
        from wxbot.ai.openai_compat import OpenAICompatClient

        return OpenAICompatClient(cfg)
    raise ValueError(f"不支持的 AI 接口类型：{cfg.type}")


def build_clients(providers: dict[str, ProviderConfig]) -> dict[str, AIClient]:
    base = {name: _make(cfg) for name, cfg in providers.items()}
    clients: dict[str, AIClient] = {}
    for name, cfg in providers.items():
        if cfg.fallback and cfg.fallback in base and cfg.fallback != name:
            clients[name] = FallbackClient(base[name], base[cfg.fallback])
        else:
            clients[name] = base[name]
    return clients
