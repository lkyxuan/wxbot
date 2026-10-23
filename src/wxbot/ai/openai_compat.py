"""OpenAI 兼容接口（DeepSeek、通义、OpenAI 等）。

改编自 SiverWXbot_plus wxbot_core.py 中的 OpenAIAPI（Apache-2.0），已精简并修改。
"""

from __future__ import annotations

import base64
import mimetypes

from openai import OpenAI

from wxbot.ai.base import AIClient, ChatTurn
from wxbot.config import ProviderConfig


def _image_to_data_url(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime = "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.standard_b64encode(f.read()).decode()}"


class OpenAICompatClient(AIClient):
    def __init__(self, cfg: ProviderConfig) -> None:
        self.model = cfg.model
        self.client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=180.0, max_retries=2)

    def chat(self, message, prompt="", history=None, image_path=""):
        messages: list[dict] = []
        if prompt:
            messages.append({"role": "system", "content": prompt})
        for turn in history or []:
            messages.append({"role": turn.role, "content": turn.content})
        if image_path:
            user_content = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": _image_to_data_url(image_path)}},
            ]
        else:
            user_content = message
        messages.append({"role": "user", "content": user_content})
        resp = self.client.chat.completions.create(model=self.model, messages=messages)
        return (resp.choices[0].message.content or "").strip()


__all__ = ["ChatTurn", "OpenAICompatClient"]
