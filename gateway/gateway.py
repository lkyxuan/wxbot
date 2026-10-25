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
import re
import sys
import threading
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from urllib.parse import unquote

from flask import Flask, Response, jsonify, request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gateway")

# 智能体不想说话时回这个标记，机器人收到后什么都不发
NO_REPLY = "<NO_REPLY>"


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

    def build_messages(self, agent, incoming, notes=()):
        """智能体提示词放最前；机器人面板里给群配的 prompt（system）默认接在后面，
        这样人设 / 语气仍可在面板里调，能力边界由网关决定。notes 是网关按本次情况追加的说明。"""
        system_parts = [agent.get("prompt", "")]
        rest = []
        for m in incoming:
            if m.get("role") == "system":
                if agent.get("use_client_prompt", True) and m.get("content"):
                    system_parts.append(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False))
            else:
                rest.append(m)
        system_parts.extend(notes)
        system = "\n\n".join(p for p in system_parts if p)
        return ([{"role": "system", "content": system}] if system else []) + rest

    def run(self, agent_name, incoming_messages, notes=()):
        agent = self.cfg["agents"][agent_name]
        client, model, extra_body = self._llm(agent)
        messages = self.build_messages(agent, incoming_messages, notes)
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
# 自己判断接不接话（智能体配置里的 auto_reply）
# ------------------------------------------------------------
# 机器人在开了「AI 判断接话」的群里每句话都发过来，并用 X-WX-Mentioned
# 告诉网关有没有 @ 机器人。没带这个请求头的请求一律照常回答。
#   被 @         → 完整智能体回答
#   没被 @       → 先用便宜模型 + 最近几条聊天判断，输出「接不接」和若干标签
#     要接且不在冷却期       → 完整智能体回答
#     命中 silent_on_labels → 完整智能体静默跑一遍（可以调工具记录），回复丢弃
#     其它                  → 返回 NO_REPLY
# ============================================================
def content_text(content):
    """消息内容转纯文本：多模态列表里的图片记成 [图片]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict):
                parts.append("[图片]")
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def parse_judge_json(text):
    """从判断模型的输出里取 JSON 对象；模型偶尔会包一层 ```json，取第一个 {...}。"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError(f"判断结果不是 JSON：{text!r}")
    return json.loads(match.group(0))


class AutoReply:
    def __init__(self, runner):
        self.runner = runner
        self._last_spoke = {}           # 冷却键 -> 上次主动发言时间（time.monotonic）
        self._lock = threading.Lock()

    @staticmethod
    def _transcript(messages, limit):
        lines = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            text = content_text(m.get("content")).strip()
            if not text:
                continue
            # 机器人发过的话标成「机器人」；群友的消息本来就是「昵称: 内容」。
            # 一条消息可能有多行（合并回复会用换行拼起来），压成一行，保证一行就是一条消息
            text = " / ".join(part.strip() for part in text.splitlines() if part.strip())
            lines.append(f"机器人: {text}" if role == "assistant" else text)
        return lines[-limit:] if limit > 0 else lines

    def judge(self, agent_name, agent, messages):
        """返回 {"reply": bool, "chat": bool, "labels": {标签: bool}}；出错时按不接话处理。
        chat 只在配置了 chat_when 时判断：闲聊里值得插一句（冷却另算，见 respond）。"""
        ar = agent["auto_reply"]
        labels = ar.get("labels", {})
        lines = self._transcript(messages, int(ar.get("context_messages", 8)))
        if not lines:
            return {"reply": False, "chat": False, "labels": {}}
        label_rules = "\n".join(f"- {name}：{desc}" for name, desc in labels.items()) or "（无）"
        label_example = ", ".join(f'"{name}": false' for name in labels)
        chat_when = ar.get("chat_when") or ""
        chat_rule = (f"如果不需要接话，再判断 chat：这是不是一段值得插一句的闲聊（{chat_when}）。"
                     "chat 只表示「可以插一句」，不是每句闲聊都要接。\n\n") if chat_when else ""
        chat_example = '"chat": false, ' if chat_when else ""
        system = (
            "你在帮一个微信群里的 AI 助手判断：群里最新这条消息（标了【最新】的那条），助手要不要主动接话。"
            "这条消息没有 @ 助手，大多数闲聊都不该接，拿不准时不接。\n\n"
            f"需要接话的情况（reply）：\n{ar.get('reply_when') or '有人明显在向助手提问或求助'}\n\n"
            f"{chat_rule}"
            f"另外判断这些标签（只看【最新】那条）：\n{label_rules}\n\n"
            "只输出一个 JSON 对象，不要别的文字，格式：\n"
            f'{{"reply": false, {chat_example}"labels": {{{label_example}}}}}'
        )
        lines[-1] = "【最新】" + lines[-1]
        user = ("最近的群聊（每行一条消息，格式「昵称: 内容」，一条消息里的换行写成「 / 」）：\n"
                + "\n".join(lines))
        llm = dict(self.runner.cfg.get("llm", {}))
        llm.update(agent.get("llm", {}))
        llm.update(ar.get("judge_llm", {}))
        try:
            client, model, extra_body = self.runner._llm({"llm": llm})
            kwargs = {"model": model, "temperature": 0,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if extra_body:
                kwargs["extra_body"] = extra_body
            resp = client.chat.completions.create(**kwargs)
            data = parse_judge_json(resp.choices[0].message.content)
        except Exception as e:
            log.warning("[%s] 判断接话失败，按不接话处理：%s", agent_name, e)
            return {"reply": False, "chat": False, "labels": {}}
        raw_labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
        return {
            "reply": data.get("reply") is True,
            "chat": bool(chat_when) and data.get("chat") is True,
            "labels": {name: raw_labels.get(name) is True for name in labels},
        }

    def _claim_cooldown(self, key, seconds):
        """不在冷却期就占住（记下现在的时间）并返回 True。先占后跑，防止几条消息同时触发。"""
        now = time.monotonic()
        with self._lock:
            last = self._last_spoke.get(key)
            if last is not None and now - last < seconds:
                return False
            self._last_spoke[key] = now
            return True

    def _release_cooldown(self, key):
        # 占了冷却但最后没说话（智能体回了 NO_REPLY），不算一次主动发言
        with self._lock:
            self._last_spoke.pop(key, None)

    def respond(self, agent_name, messages, meta):
        agent = self.runner.cfg["agents"][agent_name]
        ar = agent.get("auto_reply")
        if not ar or meta.get("mentioned") is None:
            return self.runner.run(agent_name, messages)

        if meta["mentioned"]:
            log.info("[%s] 被 @，直接回答", agent_name)
            return self.runner.run(agent_name, messages)

        base_note = f"如果其实没什么需要你说的，只回复 {NO_REPLY}，不要回复其它内容。"

        verdict = self.judge(agent_name, agent, messages)
        hit = [name for name, on in verdict["labels"].items() if on]
        silent_on = ar.get("silent_on_labels", [])
        cooldown_key = meta.get("chat") or agent_name
        if verdict["reply"] and self._claim_cooldown(cooldown_key, float(ar.get("cooldown_seconds", 600))):
            log.info("[%s] 判断：接话，标签 %s", agent_name, hit)
            note = "这条消息没有 @ 你，是你判断后主动接话。"
            if hit:
                note += f"判断阶段命中的标签：{'、'.join(hit)}。"
            try:
                content = self.runner.run(agent_name, messages, [base_note, note])
            except Exception:
                self._release_cooldown(cooldown_key)
                raise
            if not content or content.strip() == NO_REPLY:
                self._release_cooldown(cooldown_key)
            return content

        # 闲聊插一句：单独的冷却（chat_cooldown_seconds，默认 60 秒），和上面的接话冷却互不影响
        chat_key = f"{cooldown_key}#chat"
        if not verdict["reply"] and verdict.get("chat") \
                and self._claim_cooldown(chat_key, float(ar.get("chat_cooldown_seconds", 60))):
            log.info("[%s] 判断：闲聊插一句，标签 %s", agent_name, hit)
            note = ("这条消息没有 @ 你，是群友在闲聊，你主动插一句：像群友一样简短自然，一两句就够，"
                    "不要推销功能，也不要把话题硬拉回你的专长。")
            if hit:
                note += f"判断阶段命中的标签：{'、'.join(hit)}。"
            try:
                content = self.runner.run(agent_name, messages, [base_note, note])
            except Exception:
                self._release_cooldown(chat_key)
                raise
            if not content or content.strip() == NO_REPLY:
                self._release_cooldown(chat_key)
            return content

        if "*" in silent_on or any(name in silent_on for name in hit):
            log.info("[%s] 判断：%s，静默旁听，标签 %s", agent_name,
                     "要接但在冷却期" if verdict["reply"] else "不接话", hit)
            last = next((content_text(m.get("content")) for m in reversed(messages)
                         if m.get("role") == "user"), "")
            msg_key = hashlib.sha1(
                f"{meta.get('chat', '')}\n{meta.get('sender', '')}\n{last}".encode("utf-8")).hexdigest()[:16]
            note = (
                "【旁听】这条消息不需要回复，你的回复不会发到群里。"
                "需要记录的话用工具记录，然后只回复 " + NO_REPLY + "。\n"
                f"命中的标签：{'、'.join(hit) or '无'}\n"
                f"群名：{meta.get('chat', '')}\n发言人：{meta.get('sender', '')}\n"
                f"收到时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"消息标识（可用于去重）：{msg_key}"
            )
            try:
                self.runner.run(agent_name, messages, [note])
            except Exception:
                log.exception("[%s] 静默旁听执行失败", agent_name)
            return NO_REPLY

        log.info("[%s] 判断：%s", agent_name, "要接但在冷却期" if verdict["reply"] else "不接话")
        return NO_REPLY


# ============================================================
# HTTP 接口（OpenAI 兼容）
# ============================================================
def request_meta():
    """机器人带来的会话信息（X-WX-* 请求头，中文是 UTF-8 百分号编码）。
    mentioned：None 表示没带这个头（照常回答），True/False 表示有没有 @ 机器人。"""
    def header(name):
        return unquote(request.headers.get(name, ""))
    mentioned = request.headers.get("X-WX-Mentioned")
    return {
        "chat": header("X-WX-Chat"),
        "sender": header("X-WX-Sender"),
        "mentioned": None if mentioned is None else mentioned.strip() == "1",
    }


def create_app(cfg, runner):
    app = Flask(__name__)
    auto_reply = AutoReply(runner)
    api_keys = [k for k in cfg.get("api_keys", []) if k]
    @app.before_request
    def check_auth():
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
            content = auto_reply.respond(agent_name, body.get("messages", []), request_meta())
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
