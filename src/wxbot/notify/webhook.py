"""告警通知：通用 Webhook（飞书/钉钉/企业微信机器人等）。异步发送，不阻塞微信回调线程。"""

from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger(__name__)


def notify(url: str, title: str, content: str) -> None:
    if not url:
        return

    def _send() -> None:
        try:
            # 飞书/钉钉/企业微信都接受这种纯文本格式的变体；需要定制时在这里改
            requests.post(url, json={"msg_type": "text", "msgtype": "text",
                                     "content": {"text": f"{title}\n{content}"},
                                     "text": {"content": f"{title}\n{content}"}}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("Webhook 发送失败：%s", e)

    threading.Thread(target=_send, daemon=True).start()
