# -*- coding: utf-8 -*-
"""GitHub 项目服务的单元测试：GitHub 和 Notion 接口都用假的，不联网。

运行：在 gateway 目录下 python -m pytest tests
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_servers"))

import github_server  # noqa: E402

REPO = {"full_name": "langgenius/dify", "html_url": "https://github.com/langgenius/dify",
        "stargazers_count": 123456, "description": "LLM app platform"}


def test_find_by_full_name_and_search(monkeypatch):
    def fake_get(path, params=None):
        if path == "/repos/langgenius/dify":
            return REPO
        assert path == "/search/repositories" and params["q"] == "dify"
        return {"items": [REPO]}
    monkeypatch.setattr(github_server, "_get", fake_get)
    assert "⭐123.5k" in github_server.find_github_repo("langgenius/dify")
    assert "https://github.com/langgenius/dify" in github_server.find_github_repo("dify")


def test_save_skips_duplicates_and_writes_page(monkeypatch):
    monkeypatch.setenv("NOTION_PROJECTS_DB", "db1")
    monkeypatch.setattr(github_server, "_get", lambda path, params=None: REPO)
    calls = []
    existing = []

    def fake_notion(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/query"):
            return {"results": existing}
        return {"url": "https://notion.so/p1"}

    monkeypatch.setattr(github_server, "_notion", fake_notion)
    out = github_server.save_github_project("https://github.com/langgenius/dify/", "标杆项目", "阿湍2059", "可视化编排")
    assert "已记到 Notion" in out
    props = calls[-1][2]["properties"]
    assert calls[-1][2]["parent"] == {"database_id": "db1"}
    assert props["GitHub"]["url"] == REPO["html_url"] and props["星数"]["number"] == 123456
    assert props["视频作者"]["rich_text"][0]["text"]["content"] == "阿湍2059"

    existing.append({"url": "https://notion.so/old"})
    assert "之前已经记过了" in github_server.save_github_project("langgenius/dify")
