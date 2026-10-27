# -*- coding: utf-8 -*-
"""
GitHub 项目 MCP 服务
====================

视频里经常介绍 GitHub 上的开源项目，但只说名字或功能。本服务用 GitHub 公开搜索接口
找到准确的仓库，返回链接、星数、简介，供智能体在总结里附上；
再把项目记进用户的 Notion 数据库「视频里的 GitHub 项目」，方便以后回看。

环境变量：
- GITHUB_TOKEN（可选）：带上后搜索额度更高（未登录每分钟 10 次）。只读公开数据，
  不需要任何权限范围。
- NOTION_TOKEN：Notion 内部集成的 Key（notion.so/my-integrations 创建），
  数据库页面右上角「⋯ → 连接」里要把这个集成加进去，否则写不进去。
- NOTION_PROJECTS_DB：数据库 ID（数据库链接里那串 32 位十六进制）。

单独运行（调试用）：python github_server.py
"""

import os

import requests
from mcp.server.mcpserver import MCPServer

API = "https://api.github.com"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TIMEOUT = 15

server = MCPServer("github")


def _get(path, params=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(f"{API}{path}", params=params, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError("GitHub 搜索太频繁，请稍后再试")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _fmt_stars(n):
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _line(repo):
    desc = (repo.get("description") or "").strip() or "（无简介）"
    return (f"{repo['full_name']} ⭐{_fmt_stars(repo.get('stargazers_count'))}"
            f"｜{desc}｜{repo['html_url']}")


@server.tool()
def find_github_repo(query: str, limit: int = 3) -> str:
    """在 GitHub 上找开源项目，返回最匹配的几个仓库（全名、星数、简介、链接）。

    query: 项目名（如 "lobe-chat"、"owner/repo"），或者视频里描述的功能关键词（英文更准）。
    视频转文字时项目名常被听错（例如把 "Dify" 听成 "地 fy"），可以换几种拼写多搜几次，
    结合简介判断哪个才是视频里说的；拿不准时如实说明。
    """
    q = query.strip()
    if not q:
        return "请提供项目名或关键词"
    if q.count("/") == 1 and " " not in q:
        repo = _get(f"/repos/{q}")
        if repo:
            return _line(repo)
    data = _get("/search/repositories", {"q": q, "per_page": max(1, min(int(limit), 5))}) or {}
    items = data.get("items") or []
    if not items:
        return f"GitHub 上没搜到和「{q}」相关的仓库"
    return "\n".join(_line(r) for r in items)


def _notion(method, path, body=None):
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        raise RuntimeError("没有设置 Notion 的 Key（环境变量 NOTION_TOKEN）")
    resp = requests.request(method, f"{NOTION_API}{path}", json=body, timeout=TIMEOUT, headers={
        "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json"})
    if resp.status_code == 404:
        raise RuntimeError("找不到 Notion 数据库：检查 NOTION_PROJECTS_DB，并确认数据库已「连接」到这个集成")
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion 返回错误（HTTP {resp.status_code}）：{resp.text[:300]}")
    return resp.json()


def _db_id():
    db = os.environ.get("NOTION_PROJECTS_DB", "").strip()
    if not db:
        raise RuntimeError("没有设置 Notion 数据库 ID（环境变量 NOTION_PROJECTS_DB）")
    return db


def _text(value, limit=1900):
    # Notion 单段富文本最多 2000 字
    return [{"type": "text", "text": {"content": (value or "")[:limit]}}] if value else []


def _plain(prop):
    items = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items)


@server.tool()
def save_github_project(repo: str, video_title: str = "", video_author: str = "", notes: str = "") -> str:
    """把一个 GitHub 项目记进 Notion「视频里的 GitHub 项目」。同一个仓库只记一次。

    repo: 仓库全名 owner/name（先用 find_github_repo 确认）
    video_title / video_author: 来源视频的标题和作者
    notes: 视频里关于这个项目讲了什么（一两句话，用中文）
    """
    full = repo.strip().removeprefix("https://github.com/").strip("/")
    info = _get(f"/repos/{full}")
    if not info:
        return f"GitHub 上没有 {full} 这个仓库，先用 find_github_repo 查准全名"
    url = info["html_url"]
    db = _db_id()
    found = _notion("POST", f"/databases/{db}/query",
                    {"filter": {"property": "GitHub", "url": {"equals": url}}, "page_size": 1})
    if found.get("results"):
        return f"{info['full_name']} 之前已经记过了：{found['results'][0].get('url', '')}"
    page = _notion("POST", "/pages", {
        "parent": {"database_id": db},
        "properties": {
            "项目": {"title": _text(info["full_name"])},
            "GitHub": {"url": url},
            "星数": {"number": int(info.get("stargazers_count") or 0)},
            "简介": {"rich_text": _text(info.get("description") or "")},
            "视频要点": {"rich_text": _text(notes)},
            "来源视频": {"rich_text": _text(video_title)},
            "视频作者": {"rich_text": _text(video_author)},
            "状态": {"select": {"name": "待看"}},
        },
    })
    return f"已记到 Notion：{info['full_name']} ⭐{_fmt_stars(info.get('stargazers_count'))}　{page.get('url', '')}"


@server.tool()
def list_saved_projects(limit: int = 10) -> str:
    """列出最近记进 Notion 的 GitHub 项目（主人问「最近记了哪些项目」时用）。"""
    data = _notion("POST", f"/databases/{_db_id()}/query", {
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": max(1, min(int(limit), 50))})
    rows = []
    for r in data.get("results") or []:
        p = r.get("properties") or {}
        name = _plain(p.get("项目"))
        url = (p.get("GitHub") or {}).get("url") or ""
        status = ((p.get("状态") or {}).get("select") or {}).get("name") or ""
        day = (r.get("created_time") or "")[:10]
        rows.append(f"{day} {name}（{status}）{url}")
    return "\n".join(rows) if rows else "还没有记过项目"


if __name__ == "__main__":
    server.run()
