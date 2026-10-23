"""配置加载：YAML + 环境变量（${VAR} 占位符）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


class ListenConfig(BaseModel):
    users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    type: str = "openai_compat"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    fallback: str | None = None     # 失败时改用的另一个 provider 名


class AIConfig(BaseModel):
    default: str = ""
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_prompt: str = "默认"
    prompt_by_chat: dict[str, str] = Field(default_factory=dict)
    error_reply: str = "在忙，我稍后回复您"


class GroupConfig(BaseModel):
    reply_only_when_at: bool = True
    at_sender: bool = True


class KeywordConfig(BaseModel):
    enabled: bool = True
    rules: dict[str, str] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    enabled: bool = True
    dir: str = "data/memory"
    max_count: int = 3000
    context_count: int = 20


class DelayConfig(BaseModel):
    enabled: bool = True
    min_seconds: float = 1
    max_seconds: float = 5


class NotifyConfig(BaseModel):
    webhook_url: str = ""


class Settings(BaseModel):
    admin_chat: str = "文件传输助手"
    listen: ListenConfig = Field(default_factory=ListenConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    group: GroupConfig = Field(default_factory=GroupConfig)
    keyword_reply: KeywordConfig = Field(default_factory=KeywordConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    reply_delay: DelayConfig = Field(default_factory=DelayConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    prompts_dir: str = "config/prompts"

    def prompt_for(self, chat: str) -> str:
        name = self.ai.prompt_by_chat.get(chat, self.ai.default_prompt)
        path = Path(self.prompts_dir) / f"{name}.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_settings(path: str | Path = "config/config.yaml") -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Settings.model_validate(_expand(data))
