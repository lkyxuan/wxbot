# -*- coding: utf-8 -*-
"""
视频号分享卡片：点开卡片，从 wx_channel 拿到视频信息
======================================================

wxautox4 收到的视频号卡片是 other 类型，content 只有「视频号<作者名>」，没有标题和链接。
本模块调用消息对象的 click() 在微信里打开视频（视频号会以侧边栏形式在主窗口右侧播放），
本机运行的 wx_channel（https://github.com/nobiyou/wx_channel ，以管理员身份运行）
拦截到播放请求后，把视频 ID、标题、地址、解密 key 记进它的浏览记录。
这里从浏览记录里找到刚播放的那条，立即关掉侧边栏（它会自动连播下一条），
再把消息内容改写成带「视频ID」的文字，交给 AI 接口；读视频内容、转文字、总结由网关完成。

wx_channel 接口地址见配置 wx_channel_api（默认 http://127.0.0.1:2026），
接口令牌（它 config.yaml 里的 web_console_token）放在环境变量 WX_CHANNEL_TOKEN。
"""

import os
import time
from datetime import datetime

import requests

from logger import log

CARD_PREFIX = "视频号"
# 点开后等 wx_channel 记下视频的最长时间（实测 4～6 秒）
CAPTURE_TIMEOUT = 20
POLL_INTERVAL = 1.0


def is_channels_card(msg):
    """是不是视频号分享卡片（wxautox4 识别为 other，内容以「视频号」开头）。"""
    content = getattr(msg, "content", "") or ""
    return getattr(msg, "type", "") == "other" and content.startswith(CARD_PREFIX) and len(content) > len(CARD_PREFIX)


def _author_of(msg):
    return (msg.content or "")[len(CARD_PREFIX):].strip()


def _browse_records(api):
    headers = {}
    token = os.environ.get("WX_CHANNEL_TOKEN", "").strip()
    if token:
        headers["X-Local-Auth"] = token
    resp = requests.get(f"{api.rstrip('/')}/api/browse", params={"page": 1, "pageSize": 10},
                        headers=headers, timeout=5)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(body.get("error") or "wx_channel 返回失败")
    return (body.get("data") or {}).get("items") or []


def _record_time(record):
    """浏览记录的播放时间（本机时间戳）。wx_channel 返回带时区的 ISO 时间。"""
    for key in ("browseTime", "updatedAt", "createdAt"):
        text = record.get(key) or ""
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return 0.0


def snapshot(records):
    """点开前已有的浏览记录：{视频ID: 播放时间}。"""
    return {r.get("id"): _record_time(r) for r in records}


def pick_record(records, author, before):
    """在浏览记录里找点开后才新增或更新的、作者对得上的那条。

    before 是点开前的快照：点开之前就有、时间也没变的记录（比如刚看过同一作者的另一条）不算。
    同一条视频以前看过的话，记录会被更新（播放时间变新），同样能找到。
    侧边栏会自动连播下一条，所以可能有多条新记录：取作者匹配的、时间最早的一条。
    """
    fresh = [r for r in records
             if r.get("id") not in before or _record_time(r) > before[r.get("id")]]
    matched = [r for r in fresh if author and author == (r.get("author") or "").strip()]
    if not matched:
        matched = [r for r in fresh if author and author in (r.get("author") or "")]
    if not matched:
        return None
    return min(matched, key=_record_time)


def _find_close_button(window):
    """视频号侧边栏（WeChatAppEx 的 Chrome 窗口）右上角的「关闭」按钮。

    侧边栏里还有个同名的「关闭」（标签栏里，不可见），所以按层级找：
    Chrome_WidgetWin_0 → GlassBrowserCaptionButtonContainer → ImageButton「关闭」。
    """
    try:
        pane = window.PaneControl(ClassName="Chrome_WidgetWin_0", searchDepth=1)
        if not pane.Exists(0.5, 0.1):
            return None
        container = pane.PaneControl(ClassName="GlassBrowserCaptionButtonContainer", searchDepth=4)
        if not container.Exists(0.5, 0.1):
            return None
        button = container.ButtonControl(Name="关闭", ClassName="ImageButton", searchDepth=1)
        return button if button.Exists(0.5, 0.1) else None
    except Exception:
        return None


def _main_window():
    try:
        import uiautomation as auto
    except ImportError:
        return None
    win = auto.WindowControl(ClassName="mmui::MainWindow", searchDepth=1)
    return win if win.Exists(0.5, 0.1) else None


def close_sidebar(msg):
    """关掉视频号侧边栏。用 UIA Invoke，不动鼠标，也不要求微信在前台；按 Esc 只会暂停，关不掉。"""
    windows = []
    root = getattr(msg, "root", None)
    if root is not None:
        windows.append(root)
    main = _main_window()
    if main is not None:
        windows.append(main)
    for window in windows:
        button = _find_close_button(window)
        if button is None:
            continue
        try:
            button.GetInvokePattern().Invoke()
            return True
        except Exception as e:
            log("WARNING", f"关闭视频号侧边栏失败：{e}")
    return False


def _fmt_duration(ms):
    s = round(int(ms or 0) / 1000)
    return f"{s // 60}分{s % 60:02d}秒" if s >= 60 else f"{s}秒"


def describe(record):
    """把浏览记录写成给 AI 看的一段话。视频ID 原样保留，网关的工具靠它取视频。"""
    title = (record.get("title") or "").strip() or "（无标题）"
    return (f"[分享了一条视频号视频] 作者：{record.get('author') or '未知'}｜标题：{title}"
            f"｜时长：{_fmt_duration(record.get('duration'))}｜视频ID：{record.get('id')}")


def capture(msg, api):
    """点开视频号卡片，返回改写后的消息内容；失败时返回说明文字（仍交给 AI，由它告诉用户）。"""
    author = _author_of(msg)
    try:
        before = snapshot(_browse_records(api))
    except Exception as e:
        log("WARNING", f"读取 wx_channel 浏览记录失败（它可能没在运行）：{e}")
        return f"[分享了一条视频号视频] 作者：{author}（没能读取这条视频，wx_channel 没在运行）"
    try:
        msg.click()
    except Exception as e:
        log("WARNING", f"点开视频号卡片失败：{e}")
        return f"[分享了一条视频号视频] 作者：{author}（没能打开这条视频，读不到内容）"

    record = None
    error = None
    deadline = time.time() + CAPTURE_TIMEOUT
    try:
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                record = pick_record(_browse_records(api), author, before)
            except Exception as e:
                error = e
                continue
            if record:
                break
    finally:
        if not close_sidebar(msg):
            log("WARNING", "没找到视频号侧边栏的关闭按钮，侧边栏可能还开着")

    if record:
        log("INFO", f"视频号视频已获取：{record.get('author')} - {record.get('title')} ({record.get('id')})")
        return describe(record)
    if error is not None:
        log("WARNING", f"读取 wx_channel 浏览记录失败（它可能没在运行）：{error}")
    else:
        log("WARNING", f"{CAPTURE_TIMEOUT} 秒内 wx_channel 没记下作者为「{author}」的视频")
    return f"[分享了一条视频号视频] 作者：{author}（没能读取这条视频，读不到内容）"
