from wxbot.ai.base import AIClient
from wxbot.config import Settings
from wxbot.core.bot import Bot
from wxbot.drivers.base import ChatType, IncomingMessage, MessageAttr
from wxbot.drivers.fake_driver import FakeDriver
from wxbot.memory.store import storage_key


class EchoAI(AIClient):
    def __init__(self):
        self.calls = []

    def chat(self, message, prompt="", history=None, image_path=""):
        self.calls.append((message, history))
        return f"AI:{message}"


def make_bot(tmp_path, **overrides):
    data = {
        "listen": {"users": ["张三"], "groups": ["测试群"]},
        "ai": {"default": "echo"},
        "keyword_reply": {"rules": {"价格": "请看公告"}},
        "memory": {"dir": str(tmp_path / "mem"), "context_count": 10},
        "reply_delay": {"enabled": False},
        "prompts_dir": str(tmp_path),
    }
    data.update(overrides)
    driver = FakeDriver()
    ai = EchoAI()
    bot = Bot(Settings.model_validate(data), driver, ai_clients={"echo": ai})
    bot.start()
    return bot, driver, ai


def msg(chat, content, sender=None, chat_type=ChatType.PRIVATE, attr=MessageAttr.FRIEND, at=False):
    return IncomingMessage(chat=chat, chat_type=chat_type, attr=attr, type="text",
                           sender=sender or chat, content=content, is_at_me=at)


def test_listens_admin_users_and_groups(tmp_path):
    _, driver, _ = make_bot(tmp_path)
    assert set(driver.callbacks) == {"文件传输助手", "张三", "测试群"}


def test_keyword_reply_wins_over_ai(tmp_path):
    _, driver, ai = make_bot(tmp_path)
    driver.push(msg("张三", "现在价格多少"))
    assert driver.sent == [("张三", "请看公告", None)]
    assert ai.calls == []


def test_ai_reply_with_memory(tmp_path):
    _, driver, ai = make_bot(tmp_path)
    driver.push(msg("张三", "你好"))
    driver.push(msg("张三", "再见"))
    assert driver.sent[-1] == ("张三", "AI:再见", None)
    history = ai.calls[-1][1]
    assert [t.content for t in history] == ["你好", "AI:你好"]


def test_group_requires_at_and_ats_sender(tmp_path):
    _, driver, _ = make_bot(tmp_path)
    driver.push(msg("测试群", "随便聊聊", sender="李四", chat_type=ChatType.GROUP))
    assert driver.sent == []
    driver.push(msg("测试群", "@测试机器人 你好", sender="李四", chat_type=ChatType.GROUP, at=True))
    assert driver.sent == [("测试群", "AI:李四: @测试机器人 你好", "李四")]


def test_admin_command(tmp_path):
    _, driver, _ = make_bot(tmp_path)
    driver.push(msg("文件传输助手", "/群列表", attr=MessageAttr.SELF))
    assert driver.sent == [("文件传输助手", "当前群：\n测试群", None)]


def test_long_reply_is_segmented(tmp_path):
    bot, driver, _ = make_bot(tmp_path)
    bot.reply(msg("张三", "x"), "a" * 4500)
    assert [len(s[1]) for s in driver.sent] == [2000, 2000, 500]


def test_storage_key_distinguishes_illegal_chars():
    assert storage_key("群1|群2") != storage_key("群1群2")
    assert storage_key("A:B").startswith("AB~")


def test_same_chat_messages_are_serialized(tmp_path):
    import threading
    import time

    _, driver, ai = make_bot(tmp_path)
    original = ai.chat

    def slow_chat(message, prompt="", history=None, image_path=""):
        time.sleep(0.05)
        return original(message, prompt, history, image_path)

    ai.chat = slow_chat
    threads = [threading.Thread(target=driver.push, args=(msg("张三", f"消息{i}"),)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    first, second = ai.calls
    # 第二条处理时，历史里应完整包含第一条及其回复，且不含自身
    assert [t.content for t in second[1]] == [first[0], f"AI:{first[0]}"]
    assert first[1] == []
