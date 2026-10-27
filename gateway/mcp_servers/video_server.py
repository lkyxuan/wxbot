# -*- coding: utf-8 -*-
"""
视频号视频总结 MCP 服务
========================

机器人收到视频号分享卡片后会点开它，视频在微信里播放时，本机运行的 wx_channel
（https://github.com/nobiyou/wx_channel ，MIT）把这条视频的地址和解密 key 记进
它的「浏览记录」。本服务从那里取视频 → 下载 → 解密 → 抽音频 → 第三方语音转文字，
把标题、作者和完整文字稿交给智能体总结。

环境变量：
- WX_CHANNEL_API：wx_channel 的 HTTP 接口，默认 http://127.0.0.1:2026
- WX_CHANNEL_TOKEN：wx_channel config.yaml 里的 web_console_token
- ASR_BASE_URL：OpenAI 兼容的语音转文字接口，默认硅基流动 https://api.siliconflow.cn/v1
- ASR_MODEL：默认 FunAudioLLM/SenseVoiceSmall
- ASR_API_KEY：语音转文字接口的 Key

抽音频用 imageio-ffmpeg 自带的 ffmpeg，不用另装。
文字稿缓存在 gateway/data/videos/<视频ID>.json，同一条视频不会重复转写。

单独运行（调试用）：python video_server.py
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from mcp.server.mcpserver import MCPServer

WX_CHANNEL_API = os.environ.get("WX_CHANNEL_API", "http://127.0.0.1:2026").rstrip("/")
ASR_BASE_URL = os.environ.get("ASR_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
ASR_MODEL = os.environ.get("ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "videos")

# 视频号只加密文件开头这一段（128KB），后面是明文
ENCRYPTED_PREFIX = 131072
# 只认这么久以内的浏览记录，避免把很久以前看过的视频当成刚分享的
RECENT_MINUTES = 30
# 文字稿太长时截断，免得撑爆上下文
MAX_TRANSCRIPT_CHARS = 12000

server = MCPServer("video")

_MASK = (1 << 64) - 1


class _Isaac64:
    """ISAAC64 伪随机数发生器，视频号用它从 decodeKey 生成异或密钥流。

    移植自 wx_channel 的 pkg/util/isaac64.go（MIT），逐位对照过它的输出。
    """

    def __init__(self, seed):
        self.randrsl = [0] * 256
        self.randrsl[0] = seed & _MASK
        self.mm = [0] * 256
        self.aa = self.bb = self.cc = 0
        self._randinit()

    @staticmethod
    def _mix(a, b, c, d, e, f, g, h):
        a = (a - e) & _MASK; f ^= h >> 9; h = (h + a) & _MASK
        b = (b - f) & _MASK; g ^= (a << 9) & _MASK; a = (a + b) & _MASK
        c = (c - g) & _MASK; h ^= b >> 23; b = (b + c) & _MASK
        d = (d - h) & _MASK; a ^= (c << 15) & _MASK; c = (c + d) & _MASK
        e = (e - a) & _MASK; b ^= d >> 14; d = (d + e) & _MASK
        f = (f - b) & _MASK; c ^= (e << 20) & _MASK; e = (e + f) & _MASK
        g = (g - c) & _MASK; d ^= f >> 17; f = (f + g) & _MASK
        h = (h - d) & _MASK; e ^= (g << 14) & _MASK; g = (g + h) & _MASK
        return a, b, c, d, e, f, g, h

    def _randinit(self):
        v = [0x9E3779B97F4A7C13] * 8
        for _ in range(4):
            v = list(self._mix(*v))
        for src in (self.randrsl, self.mm):
            for j in range(0, 256, 8):
                v = [(v[k] + src[j + k]) & _MASK for k in range(8)]
                v = list(self._mix(*v))
                self.mm[j:j + 8] = v
        self._isaac64()
        self.randcnt = 256

    def _isaac64(self):
        mm = self.mm
        self.cc = (self.cc + 1) & _MASK
        self.bb = (self.bb + self.cc) & _MASK
        aa, bb = self.aa, self.bb
        for j in range(256):
            x = mm[j]
            r = j % 4
            if r == 0:
                aa = (~(aa ^ (aa << 21))) & _MASK
            elif r == 1:
                aa ^= aa >> 5
            elif r == 2:
                aa ^= (aa << 12) & _MASK
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(j + 128) % 256]) & _MASK
            y = (mm[(x >> 3) % 256] + aa + bb) & _MASK
            mm[j] = y
            bb = (mm[(y >> 11) % 256] + x) & _MASK
            self.randrsl[j] = bb
        self.aa, self.bb = aa, bb

    def keystream(self, length):
        out = bytearray()
        while len(out) < length:
            if self.randcnt == 0:
                self._isaac64()
                self.randcnt = 256
            self.randcnt -= 1
            out += self.randrsl[self.randcnt].to_bytes(8, "big")
        return bytes(out[:length])


def decrypt_prefix(data, key):
    """把视频开头被加密的一段异或回明文。data 是 bytes/bytearray，返回解密后的 bytes。"""
    n = min(len(data), ENCRYPTED_PREFIX)
    ks = _Isaac64(int(key)).keystream(n)
    head = bytes(a ^ b for a, b in zip(data[:n], ks))
    return head + bytes(data[n:])


def _looks_like_mp4(head):
    return any(box in head[4:32] for box in (b"ftyp", b"styp", b"moov", b"mdat"))


def _wx_get(path, params=None):
    headers = {}
    token = os.environ.get("WX_CHANNEL_TOKEN", "").strip()
    if token:
        headers["X-Local-Auth"] = token
    try:
        resp = requests.get(f"{WX_CHANNEL_API}{path}", params=params, headers=headers, timeout=10)
    except requests.RequestException as e:
        raise RuntimeError(f"连不上 wx_channel（{WX_CHANNEL_API}），它可能没在运行：{e}")
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"wx_channel 返回错误：{body.get('error') or body}")
    return body.get("data")


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def find_record(video_id="", author=""):
    """按视频 ID 取浏览记录；不给 ID 就取最近播放的一条（可按作者名过滤）。"""
    if video_id:
        return _wx_get(f"/api/browse/{video_id}")
    data = _wx_get("/api/browse", {"page": 1, "pageSize": 20}) or {}
    since = datetime.now(timezone.utc) - timedelta(minutes=RECENT_MINUTES)
    for item in data.get("items") or []:
        t = _parse_time(item.get("browseTime") or "")
        if t and t.astimezone(timezone.utc) < since:
            continue
        if author and author not in (item.get("author") or ""):
            continue
        return item
    return None


def _download(url, key, path):
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
    if key:
        with open(path, "r+b") as f:
            head = f.read(ENCRYPTED_PREFIX)
            plain = decrypt_prefix(head, key)
            if not _looks_like_mp4(plain):
                raise RuntimeError("视频解密后文件头不对，key 可能和视频对不上")
            f.seek(0)
            f.write(plain)


def _extract_audio(video_path, audio_path):
    import imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", video_path,
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", audio_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0 or not os.path.exists(audio_path):
        err = proc.stderr.decode("utf-8", "ignore").strip()
        if "does not contain any stream" in err or "Output file is empty" in err:
            return False   # 没有音轨
        raise RuntimeError(f"抽音频失败：{err[-300:]}")
    return os.path.getsize(audio_path) > 0


def _transcribe(audio_path):
    key = os.environ.get("ASR_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置语音转文字的 Key（环境变量 ASR_API_KEY）")
    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"{ASR_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": ASR_MODEL},
            files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
            timeout=300,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"语音转文字失败（HTTP {resp.status_code}）：{resp.text[:300]}")
    return (resp.json().get("text") or "").strip()


def transcribe_record(record):
    """下载、解密、转写一条浏览记录，返回文字稿（带缓存）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = os.path.join(DATA_DIR, f"{record['id']}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f).get("transcript", "")
    if not record.get("videoUrl"):
        raise RuntimeError("浏览记录里没有视频地址")
    with tempfile.TemporaryDirectory() as tmp:
        video = os.path.join(tmp, "video.mp4")
        audio = os.path.join(tmp, "audio.mp3")
        _download(record["videoUrl"], record.get("decryptKey") or "", video)
        transcript = _transcribe(audio) if _extract_audio(video, audio) else ""
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"id": record["id"], "title": record.get("title"), "author": record.get("author"),
                   "transcript": transcript, "at": datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False)
    return transcript


def _fmt_duration(ms):
    # wx_channel 浏览记录里的 duration 单位是毫秒
    s = round(int(ms or 0) / 1000)
    return f"{s // 60}分{s % 60:02d}秒" if s >= 60 else f"{s}秒"


@server.tool()
def get_channels_video_transcript(video_id: str = "", author: str = "") -> str:
    """取一条视频号视频的标题、作者和完整语音文字稿，用来总结视频内容。

    用户分享视频号视频时，消息里会带「视频ID」，把它原样传进 video_id。
    没有视频ID时留空，会取刚刚播放过的那条（可用 author 传作者名过滤）。
    """
    record = find_record(video_id.strip(), author.strip())
    if not record:
        return "没找到这条视频的播放记录（可能还没点开或已超过半小时），没法读取内容。"
    lines = [
        f"标题/文案：{record.get('title') or '（无）'}",
        f"作者：{record.get('author') or '未知'}",
        f"时长：{_fmt_duration(record.get('duration'))}",
        f"点赞 {record.get('likeCount', 0)} · 评论 {record.get('commentCount', 0)} · 转发 {record.get('forwardCount', 0)}",
    ]
    transcript = transcribe_record(record)
    if not transcript:
        lines.append("文字稿：（这条视频没有可识别的人声，只能根据标题和文案判断内容）")
    else:
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "……（后面太长已截断）"
        lines.append("文字稿：\n" + transcript)
    return "\n".join(lines)


if __name__ == "__main__":
    server.run()
