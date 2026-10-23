"""按会话保存聊天记录，用作 AI 上下文。

目录命名规则沿用 SiverWXbot_plus memory_store.py（Apache-2.0）的思路：
可读名字 + 原始会话名的完整 sha256，避免 Windows 非法字符被删掉后不同群共用同一份记忆。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class MemoryItem:
    time: str
    sender: str
    content: str
    is_self: bool


def storage_key(chat: str) -> str:
    digest = hashlib.sha256(chat.encode("utf-8")).hexdigest()
    readable = _ILLEGAL.sub("", chat).strip(" .")[:40]
    return f"{readable}~{digest}" if readable else f"chat~{digest}"


class MemoryStore:
    def __init__(self, base_dir: str | Path, max_count: int = 3000) -> None:
        self.base = Path(base_dir)
        self.max_count = max_count
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _file(self, key: str) -> Path:
        return self.base / key / "memory.json"

    def _read(self, key: str) -> list[dict]:
        path = self._file(key)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, key: str, chat: str, items: list[dict]) -> None:
        path = self._file(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "name.json").write_text(json.dumps({"chat": chat}, ensure_ascii=False), encoding="utf-8")
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def add(self, chat: str, item: MemoryItem) -> None:
        key = storage_key(chat)
        with self._lock(key):
            items = self._read(key)
            items.append(asdict(item))
            self._write(key, chat, items[-self.max_count:])

    def recent(self, chat: str, n: int) -> list[MemoryItem]:
        key = storage_key(chat)
        with self._lock(key):
            return [MemoryItem(**d) for d in self._read(key)[-n:]] if n > 0 else []

    def clear(self, chat: str) -> None:
        key = storage_key(chat)
        with self._lock(key):
            self._file(key).unlink(missing_ok=True)
