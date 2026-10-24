# -*- coding: utf-8 -*-
"""
自建 AI 网关
============

微信机器人只负责收发消息；每个群在面板里映射到一个「OpenAI API 格式兼容接口」，
接口地址填本网关，模型名填智能体名（例如 crypto、food）。

    微信群 ──> SiverWXbot ──> 本网关 /v1/chat/completions (model=智能体名)
                                   │
                                   ├─ 智能体 = 提示词 + 一组 MCP 工具
                                   ├─ 调大模型（DeepSeek 等，支持工具调用即可）
                                   └─ 大模型需要时调用 MCP 工具（你的各个产品）

同一个网关可以服务多个群，每个群用的智能体、能调用的工具各不相同。
配置见 config.example.json，使用说明见 README.md。

启动：python gateway.py            （默认读取同目录 config.json）
     python gateway.py my.json    （指定配置文件）
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import AsyncExitStack

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gateway")


# ============================================================
# 配置
# ============================================================
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("listen", {})
    cfg.setdefault("api_keys", [])
    cfg.setdefault("mcp_servers", {})
    cfg.setdefault("agents", {})
    if not cfg["agents"]:
        raise ValueError("配置里至少要有一个智能体（agents）")
    for name, agent in cfg["agents"].items():
        prompt_file = agent.get("prompt_file")
        if prompt_file:
            with open(os.path.join(BASE_DIR, prompt_file), "r", encoding="utf-8") as f:
                agent["prompt"] = f.read().strip()
        for server_name in agent.get("mcp", []):
            if server_name not in cfg["mcp_servers"]:
                raise ValueError(f"智能体 {name} 引用了不存在的 MCP 服务：{server_name}")
    return cfg


def resolve_secret(block, key):
    """优先读环境变量（xxx_env），其次读明文字段，避免把 Key 写进配置文件。"""
    env_name = block.get(f"{key}_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return block.get(key, "")


# ============================================================
# MCP 连接管理
# ------------------------------------------------------------
# MCP 客户端是异步的，Flask 是同步的：单独开一个后台事件循环线程，
# 启动时把所有 MCP 服务连上并保持长连接，请求线程通过
# run_coroutine_threadsafe 把工具调用投递过去。
# ============================================================
class MCPManager:
    def __init__(self, servers_cfg):
        self.servers_cfg = servers_cfg
        self.sessions = {}      # 服务名 -> ClientSession
        self.tools = {}         # 服务名 -> [Tool]
        self.loop = asyncio.new_event_loop()
        self._stack = None
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp-loop")

    def start(self):
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect_all(), self.loop).result(timeout=120)

    def _run(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    async def _connect_all(self):
        self._stack = AsyncExitStack()
        for name, cfg in self.servers_cfg.items():
            command = cfg["command"]
            # 写 python 时用网关自己的解释器，保证装的依赖一致（Windows 上尤其省事）
            if command in ("python", "python3"):
                command = sys.executable
            env = dict(os.environ)
            env.update(cfg.get("env", {}))
            params = StdioServerParameters(
                command=command,
                args=cfg.get("args", []),
                env=env,
                cwd=os.path.join(BASE_DIR, cfg.get("cwd", ".")),
            )
            try:
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools = (await session.list_tools()).tools
            except Exception as e:
                # 一个产品挂了不影响其它智能体，用到它的智能体只是少了这些工具
                log.error("MCP 服务 %s 连接失败：%s", name, e)
                continue
            self.sessions[name] = session
            self.tools[name] = tools
            log.info("MCP 服务 %s 已连接，工具：%s", name, ", ".join(t.name for t in tools))

    def openai_tools(self, server_names):
        """把若干 MCP 服务的工具转成 OpenAI function calling 格式。
        工具名加上「服务名__」前缀，避免不同产品里的同名工具冲突；超过 64 字符时
        截断并加哈希保证唯一。返回 (工具列表, {对外名称: (服务名, 原工具名)})，
        调用时只认这张表，智能体调不到没分配给它的工具。"""
        result, name_map = [], {}
        for server in server_names:
            for t in self.tools.get(server, []):
                name = f"{server}__{t.name}"
                if len(name) > 64 or name in name_map:
                    digest = hashlib.sha1(f"{server}/{t.name}".encode("utf-8")).hexdigest()[:8]
                    name = f"{name[:55]}_{digest}"
                name_map[name] = (server, t.name)
                result.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.description or "",
                        "parameters": t.input_schema or {"type": "object", "properties": {}},
                    },
                })
        return result, name_map

    def call(self, server, tool, arguments, timeout=60):
        session = self.sessions.get(server)
        if session is None:
            return f"工具不可用：{server}/{tool}"
        try:
            res = self._run(session.call_tool(tool, arguments), timeout)
        except Exception as e:
            return f"工具调用失败：{e}"
        texts = [c.text for c in (res.content or []) if getattr(c, "type", "") == "text"]
        text = "\n".join(texts) if texts else json.dumps(res.structured_content, ensure_ascii=False)
        return ("工具报错：" + text) if res.is_error else text


# ============================================================
# 智能体：提示词 + 工具 + 多轮工具调用循环
# ============================================================
class AgentRunner:
    def __init__(self, cfg, mcp):
        self.cfg = cfg
        self.mcp = mcp
        self.clients = {}       # (base_url, api_key) -> OpenAI，按需复用

    def _llm(self, agent):
        llm = dict(self.cfg.get("llm", {}))
        llm.update(agent.get("llm", {}))  # 智能体可以单独指定模型
        base_url = llm.get("base_url")
        api_key = resolve_secret(llm, "api_key")
        key = (base_url, api_key)
        if key not in self.clients:
            self.clients[key] = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        return self.clients[key], llm.get("model"), llm.get("extra_body")

    def build_messages(self, agent, incoming):
        """智能体提示词放最前；机器人面板里给群配的 prompt（system）默认接在后面，
        这样人设 / 语气仍可在面板里调，能力边界由网关决定。"""
        system_parts = [agent.get("prompt", "")]
        rest = []
        for m in incoming:
            if m.get("role") == "system":
                if agent.get("use_client_prompt", True) and m.get("content"):
                    system_parts.append(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False))
            else:
                rest.append(m)
        system = "\n\n".join(p for p in system_parts if p)
        return ([{"role": "system", "content": system}] if system else []) + rest

    def run(self, agent_name, incoming_messages):
        agent = self.cfg["agents"][agent_name]
        client, model, extra_body = self._llm(agent)
        messages = self.build_messages(agent, incoming_messages)
        tools, tool_map = self.mcp.openai_tools(agent.get("mcp", []))
        max_rounds = int(agent.get("max_tool_rounds", 5))

        for round_no in range(max_rounds + 1):
            kwargs = {"model": model, "messages": messages}
            if extra_body:
                kwargs["extra_body"] = extra_body
            # 最后一轮不再给工具，逼模型直接作答，防止无限调用
            if tools and round_no < max_rounds:
                kwargs["tools"] = tools
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                return (msg.content or "").strip()

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("[%s] 调用工具 %s %s", agent_name, c.function.name, args)
                target = tool_map.get(c.function.name)
                if target is None:
                    output = f"没有这个工具：{c.function.name}"
                else:
                    output = self.mcp.call(*target, args)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": output[:8000]})
        return "查询步骤太多了，换个问法试试"


# ============================================================
# HTTP 接口（OpenAI 兼容）
# ============================================================
def create_app(cfg, runner):
    app = Flask(__name__)
    api_keys = [k for k in cfg.get("api_keys", []) if k]
    # 公开的静态页面（例如美食网页地图）：{URL 前缀: [目录, ...]}，按顺序找到第一个存在的文件。
    # 这些路径不需要 Key，只放可以公开的内容。
    public = {
        "/" + prefix.strip("/"): [os.path.join(BASE_DIR, d) for d in (dirs if isinstance(dirs, list) else [dirs])]
        for prefix, dirs in cfg.get("public", {}).items()
    }

    def public_match(path):
        for prefix in sorted(public, key=len, reverse=True):
            if path == prefix or path.startswith(prefix + "/"):
                return prefix
        return None

    @app.get("/<path:path>")
    def serve_public(path):
        path = "/" + path
        prefix = public_match(path)
        if prefix is None:
            abort(404)
        if path == prefix:
            # 页面里用相对路径引用 leaflet/、data.json，必须带结尾斜杠
            return redirect(request.full_path.replace(prefix, prefix + "/", 1).rstrip("?"))
        rel = path[len(prefix):].lstrip("/") or "index.html"
        for d in public[prefix]:
            if os.path.isfile(os.path.join(d, rel)):
                resp = send_from_directory(d, rel)  # send_from_directory 会拒绝 ../ 越界
                resp.headers["Cache-Control"] = "no-cache"
                return resp
        abort(404)

    @app.before_request
    def check_auth():
        if public_match(request.path) is not None and request.method == "GET":
            return None
        if not api_keys:
            return None
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token not in api_keys:
            return jsonify({"error": {"message": "invalid api key", "type": "auth_error"}}), 401
        return None

    @app.get("/v1/models")
    def list_models():
        return jsonify({"object": "list", "data": [
            {"id": name, "object": "model", "owned_by": "gateway"} for name in cfg["agents"]
        ]})

    @app.post("/v1/chat/completions")
    def chat_completions():
        body = request.get_json(force=True, silent=True) or {}
        agent_name = body.get("model") or cfg.get("default_agent")
        if agent_name not in cfg["agents"]:
            return jsonify({"error": {
                "message": f"未知智能体 {agent_name}，可用：{', '.join(cfg['agents'])}",
                "type": "invalid_request_error"}}), 404
        started = time.time()
        try:
            content = runner.run(agent_name, body.get("messages", []))
        except Exception as e:
            log.exception("智能体 %s 执行失败", agent_name)
            return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500
        log.info("[%s] 回复（%.1fs）：%s", agent_name, time.time() - started, content[:100])

        cid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        if body.get("stream"):
            # 机器人默认非流式；为兼容其它客户端，流式时把完整回复作为一个块返回
            def sse():
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": agent_name,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            return Response(sse(), mimetype="text/event-stream")
        return jsonify({
            "id": cid, "object": "chat.completion", "created": created, "model": agent_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    return app


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "config.json")
    cfg = load_config(config_path)
    mcp = MCPManager(cfg["mcp_servers"])
    mcp.start()
    app = create_app(cfg, AgentRunner(cfg, mcp))
    host = cfg["listen"].get("host", "127.0.0.1")
    port = int(cfg["listen"].get("port", 8100))
    log.info("网关已启动 http://%s:%s/v1  智能体：%s", host, port, ", ".join(cfg["agents"]))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
