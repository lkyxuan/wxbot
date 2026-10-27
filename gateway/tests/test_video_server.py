# -*- coding: utf-8 -*-
"""视频号视频总结服务的单元测试：wx_channel 和语音转文字都用假的，不联网。

运行：在 gateway 目录下 python -m pytest tests
"""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_servers"))

import video_server  # noqa: E402

# 期望值来自 wx_channel 的 Go 实现 GenerateDecryptorArray(seed, 131072)
VECTORS = {
    0: ("9d39247e33776d412af7398005aaa5c7",
        "e1662af3b7e59867c919ad19055fc8cecea2b154d37e1459b2f96d1da14cef1f"),
    12345: ("e8721821768522e9899fb4a5e5539b95",
            "932e7fed86b329540fd72561f3401d0f11412b1fe976217efa016e2a9a17fcbc"),
    18446744073709551615: ("7c38fd3a2e7cd8ad64081892c82430f3",
                           "5afbffd76305e81467f97c6370fa07916e9b197611bf5c357a854eb92a6354a1"),
}


def test_keystream_matches_go_implementation():
    for seed, (head, digest) in VECTORS.items():
        ks = video_server._Isaac64(seed).keystream(131072)
        assert ks[:16].hex() == head
        assert hashlib.sha256(ks).hexdigest() == digest


def test_decrypt_prefix_roundtrip_only_touches_first_128k():
    plain = b"\x00\x00\x00\x20ftypisom" + os.urandom(200000)
    enc = video_server.decrypt_prefix(plain, "12345")   # 异或两次还原
    assert enc[131072:] == plain[131072:]
    assert enc[:131072] != plain[:131072]
    back = video_server.decrypt_prefix(enc, "12345")
    assert back == plain
    assert video_server._looks_like_mp4(back[:32])


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_find_record_picks_recent_and_matches_author(monkeypatch):
    items = [
        {"id": "a", "author": "别人", "browseTime": _iso(1)},
        {"id": "b", "author": "阿湍2059", "browseTime": _iso(2)},
        {"id": "c", "author": "阿湍2059", "browseTime": _iso(120)},
    ]
    monkeypatch.setattr(video_server, "_wx_get", lambda path, params=None: {"items": items})
    assert video_server.find_record()["id"] == "a"
    assert video_server.find_record(author="阿湍2059")["id"] == "b"
    assert video_server.find_record(author="没有这个人") is None


def test_tool_uses_cache_and_formats(monkeypatch, tmp_path):
    record = {"id": "v1", "title": "怎么做红烧肉", "author": "厨师", "duration": 95000,
              "likeCount": 3, "commentCount": 1, "forwardCount": 0, "videoUrl": "http://x"}
    monkeypatch.setattr(video_server, "_wx_get", lambda path, params=None: record)
    monkeypatch.setattr(video_server, "DATA_DIR", str(tmp_path))
    calls = []

    def fake_download(url, key, path):
        calls.append(url)
        open(path, "wb").close()

    monkeypatch.setattr(video_server, "_download", fake_download)
    monkeypatch.setattr(video_server, "_extract_audio", lambda v, a: True)
    monkeypatch.setattr(video_server, "_transcribe", lambda a: "先焯水再炒糖色")

    out = video_server.get_channels_video_transcript(video_id="v1")
    assert "怎么做红烧肉" in out and "1分35秒" in out and "先焯水再炒糖色" in out
    video_server.get_channels_video_transcript(video_id="v1")
    assert len(calls) == 1   # 第二次走缓存
