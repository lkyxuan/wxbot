# -*- coding: utf-8 -*-
"""网关「自己判断接不接话」的单元测试：大模型和 MCP 都用假的，不联网。

运行：在 gateway 目录下 python -m pytest tests
"""
import json
import os
import sys
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gateway  # noqa: E402


def make_resp(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeClient:
    """按调用顺序返回预设内容；judge 调用（system 里有「要不要主动接话」）走 judge 队列。"""

    def __init__(self, judge=None, agent=None):
        self.judge_out = list(judge or [])
        self.agent_out = list(agent or [])
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs["messages"][0]["content"]
        if "要不要主动接话" in system:
            out = self.judge_out.pop(0)
            if isinstance(out, Exception):
                raise out
            return make_resp(out if isinstance(out, str) else json.dumps(out))
        return make_resp(self.agent_out.pop(0))

    def agent_calls(self):
        return [c for c in self.calls if "要不要主动接话" not in c["messages"][0]["content"]]


class FakeMCP:
    def openai_tools(self, names):
        return [], {}


AGENT = {
    "prompt": "你是美食助手",
    "mcp": [],
    "auto_reply": {
        "reply_when": "有人问吃什么",
        "labels": {"review": "在评价某家店"},
        "silent_on_labels": ["review"],
        "cooldown_seconds": 600,
        "context_messages": 3,
    },
}


def setup(judge=None, agent=None, agent_cfg=None):
    cfg = {"llm": {"base_url": "http://x", "model": "m"}, "agents": {"food": agent_cfg or json.loads(json.dumps(AGENT))}}
    runner = gateway.AgentRunner(cfg, FakeMCP())
    client = FakeClient(judge, agent)
    runner._llm = lambda agent: (client, "m", None)
    return gateway.AutoReply(runner), client


MSGS = [
    {"role": "system", "content": "面板 prompt"},
    {"role": "user", "content": "小明: 今天好热"},
    {"role": "assistant", "content": "是啊"},
    {"role": "user", "content": "小红: 中午吃啥"},
]
META = {"chat": "美食群", "sender": "小红", "mentioned": False}


def test_no_header_answers_without_judging():
    ar, client = setup(agent=["好的"])
    assert ar.respond("food", MSGS, {"chat": "", "sender": "", "mentioned": None}) == "好的"
    assert len(client.calls) == 1


def test_agent_without_auto_reply_answers():
    cfg = {"prompt": "p", "mcp": []}
    ar, client = setup(agent=["好的"], agent_cfg=cfg)
    assert ar.respond("food", MSGS, META) == "好的"


def test_mentioned_always_answers():
    ar, client = setup(agent=["推荐你去吃面"])
    assert ar.respond("food", MSGS, dict(META, mentioned=True)) == "推荐你去吃面"
    assert len(client.calls) == 1


def test_judge_says_reply():
    ar, client = setup(judge=[{"reply": True, "labels": {"review": False}}], agent=["吃火锅吧"])
    assert ar.respond("food", MSGS, META) == "吃火锅吧"
    judge_user = client.calls[0]["messages"][1]["content"]
    # 只带最近 3 条，机器人的话标成「机器人」，面板 prompt 不进判断
    assert "机器人: 是啊" in judge_user and "小红: 中午吃啥" in judge_user
    assert "面板 prompt" not in judge_user
    assert gateway.NO_REPLY in client.agent_calls()[0]["messages"][0]["content"]


def test_judge_says_no():
    ar, client = setup(judge=['```json\n{"reply": false, "labels": {"review": false}}\n```'])
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert client.agent_calls() == []


def test_label_triggers_silent_run():
    ar, client = setup(judge=[{"reply": False, "labels": {"review": True}}], agent=["已记录"])
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    system = client.agent_calls()[0]["messages"][0]["content"]
    assert "【旁听】" in system and "群名：美食群" in system and "发言人：小红" in system
    assert "消息标识" in system and "review" in system


def test_star_listens_to_everything():
    cfg = json.loads(json.dumps(AGENT))
    cfg["auto_reply"]["silent_on_labels"] = ["*"]
    ar, client = setup(judge=[{"reply": False, "labels": {}}], agent=["x"], agent_cfg=cfg)
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert len(client.agent_calls()) == 1


def test_cooldown_limits_proactive_replies():
    ar, client = setup(judge=[{"reply": True}, {"reply": True}], agent=["第一次"])
    assert ar.respond("food", MSGS, META) == "第一次"
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert len(client.agent_calls()) == 1


def test_cooldown_is_per_chat_and_not_for_mentions():
    ar, client = setup(judge=[{"reply": True}, {"reply": True}], agent=["a", "b", "c"])
    assert ar.respond("food", MSGS, META) == "a"
    assert ar.respond("food", MSGS, dict(META, chat="别的群")) == "b"
    assert ar.respond("food", MSGS, dict(META, mentioned=True)) == "c"


def test_agent_no_reply_releases_cooldown():
    ar, client = setup(judge=[{"reply": True}, {"reply": True}], agent=[gateway.NO_REPLY, "第二次"])
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert ar.respond("food", MSGS, META) == "第二次"


def test_agent_error_releases_cooldown():
    ar, client = setup(judge=[{"reply": True}, {"reply": True}], agent=["第二次"])
    ar.runner.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        ar.respond("food", MSGS, META)
    except RuntimeError:
        pass
    else:
        raise AssertionError("应抛出异常")
    del ar.runner.run
    assert ar.respond("food", MSGS, META) == "第二次"


def test_multiline_message_is_one_line_and_marked_latest():
    msgs = MSGS[:-1] + [{"role": "user", "content": "小红: 那家面馆真不错\n下次再去"}]
    ar, client = setup(judge=[{"reply": False}])
    ar.respond("food", msgs, META)
    judge_user = client.calls[0]["messages"][1]["content"]
    assert "【最新】小红: 那家面馆真不错 / 下次再去" in judge_user


def test_judge_error_means_no_reply():
    ar, client = setup(judge=[RuntimeError("down"), "不是 JSON"])
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert client.agent_calls() == []


def test_request_meta_headers():
    app = gateway.Flask(__name__)
    with app.test_request_context(headers={
        "X-WX-Chat": quote("美食群", safe=""), "X-WX-Sender": quote("小红", safe=""), "X-WX-Mentioned": "0"}):
        assert gateway.request_meta() == {"chat": "美食群", "sender": "小红", "mentioned": False}
    with app.test_request_context():
        assert gateway.request_meta()["mentioned"] is None


def chat_cfg(**over):
    cfg = json.loads(json.dumps(AGENT))
    cfg["auto_reply"].update({"cooldown_seconds": 0, "chat_when": "大家在闲聊、你能自然接一句", "chat_cooldown_seconds": 60}, **over)
    return cfg


def test_chat_off_without_chat_when():
    ar, client = setup(judge=[{"reply": False, "chat": True, "labels": {}}])
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert '"chat"' not in client.calls[0]["messages"][0]["content"]
    assert client.agent_calls() == []


def test_chat_chimes_in_with_its_own_cooldown():
    ar, client = setup(judge=[{"reply": False, "chat": True}, {"reply": False, "chat": True}],
                       agent=["哈哈是挺热"], agent_cfg=chat_cfg())
    assert ar.respond("food", MSGS, META) == "哈哈是挺热"
    assert "闲聊" in client.agent_calls()[0]["messages"][0]["content"]
    # 一分钟内第二句闲聊不插
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert len(client.agent_calls()) == 1
    assert '"chat": false' in client.calls[0]["messages"][0]["content"]


def test_questions_not_blocked_by_chat_cooldown():
    ar, client = setup(judge=[{"reply": False, "chat": True}, {"reply": True}, {"reply": True}],
                       agent=["插一句", "吃面", "吃饭"], agent_cfg=chat_cfg())
    assert ar.respond("food", MSGS, META) == "插一句"
    # cooldown_seconds=0：问吃的每句都回，不受闲聊冷却影响
    assert ar.respond("food", MSGS, META) == "吃面"
    assert ar.respond("food", MSGS, META) == "吃饭"


def test_chat_no_reply_releases_chat_cooldown():
    ar, client = setup(judge=[{"reply": False, "chat": True}, {"reply": False, "chat": True}],
                       agent=[gateway.NO_REPLY, "第二次"], agent_cfg=chat_cfg())
    assert ar.respond("food", MSGS, META) == gateway.NO_REPLY
    assert ar.respond("food", MSGS, META) == "第二次"
