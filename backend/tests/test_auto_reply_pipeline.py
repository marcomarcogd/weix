import asyncio
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.auto_reply_pipeline import AutoReplyPipeline
from app.core.base import WeChatMessage


class FakeRuleEngine:
    def __init__(self):
        self.calls = []

    async def match(self, content):
        self.calls.append(content)
        return {"matched": True, "reply": "自动回复"}


class FakeSender:
    def __init__(self):
        self.sent = []
        self.opened = []

    async def send_text(self, msg, receiver, **kwargs):
        self.sent.append((msg, receiver, kwargs))
        return True

    async def open_chat(self, receiver, **kwargs):
        self.opened.append((receiver, kwargs))
        return True

    def reset_search_state(self):
        pass


class FakeMonitor:
    def __init__(self):
        self.remembered = []

    def remember_sent_message(self, receiver, reply):
        self.remembered.append((receiver, reply))


class FakeAgent:
    def __init__(self):
        self.remembered = []
        self.chats = []

    async def remember_observation(self, message, session_id, context=None):
        self.remembered.append((message, session_id, context or {}))

    async def chat(self, message, session_id, context=None):
        self.chats.append((message, session_id, context or {}))
        return "好嘞\n\n我知道了 😄"


def _group_msg(room_id="room@chatroom", content="你好", msg_id="1"):
    return WeChatMessage(
        msg_id=msg_id,
        msg_type=1,
        content=content,
        sender=room_id,
        room_id=room_id,
        create_time=datetime.fromtimestamp(1778673000),
        is_group=True,
    )


def _private_msg(*, is_self=False, content="你好"):
    return WeChatMessage(
        msg_id="private:1",
        msg_type=1,
        content=content,
        sender="wxid_friend",
        room_id="",
        create_time=datetime.fromtimestamp(1778673000),
        is_group=False,
        is_self=is_self,
    )


@pytest.mark.asyncio
async def test_flush_buffer_uses_platform_sender_with_is_group(monkeypatch):
    """自动回复发送应走 Platform.sender facade，不应硬编码 macOS sender。"""
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply={"reply_mode": "keyword"}),
    )

    sender = FakeSender()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._rule_engine = FakeRuleEngine()
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 0
    pipeline._name_map = {"room@chatroom": "测试群"}
    pipeline._buffer["room@chatroom"] = [_group_msg()]

    await pipeline._flush_buffer("room@chatroom")

    assert sender.sent == [
        (
            "自动回复",
            "测试群",
            {"is_group": True, "force_skip": False, "target_id": "room@chatroom"},
        )
    ]
    assert pipeline._monitor.remembered == [("room@chatroom", "自动回复")]


@pytest.mark.asyncio
async def test_flush_buffer_refuses_unsearchable_group_without_display_name(monkeypatch):
    """群聊没有可搜索显示名时应拒绝发送，不能盲发到当前窗口。"""
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply={"reply_mode": "keyword"}),
    )

    sender = FakeSender()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._rule_engine = FakeRuleEngine()
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 0
    pipeline._name_map = {}
    pipeline._buffer["room@chatroom"] = [_group_msg()]

    await pipeline._flush_buffer("room@chatroom")

    assert sender.sent == []
    assert pipeline._monitor.remembered == []


def test_merge_chatroom_name_does_not_overwrite_existing_display_name():
    name_map = {"room@chatroom": "联系人表群名"}

    AutoReplyPipeline._merge_chatroom_name(name_map, "room@chatroom", "")

    assert name_map["room@chatroom"] == "联系人表群名"


def test_open_message_db_uses_platform_specific_reader():
    class FakeReader:
        def __init__(self):
            self.opened = []
            self.closed = False

        def find_database_files(self):
            return ["C:/Users/me/MSG.db"]

        def open_db(self, path, key):
            self.opened.append((path, key))
            return True

        def is_message_db(self):
            return True

        def is_contact_db(self):
            return False

        def close(self):
            self.closed = True

    reader = FakeReader()
    platform = SimpleNamespace(db_reader=reader)

    result = AutoReplyPipeline._open_message_db(platform, {"MSG.db": "00" * 32})

    assert result is reader
    assert reader.opened == [("C:/Users/me/MSG.db", bytes.fromhex("00" * 32))]


def test_open_message_db_never_selects_windows_biz_message_database():
    """公众号库即使含 Msg_% 表，也不能成为 Windows 自动回复的数据源。"""
    class FakeReader:
        def __init__(self):
            self.opened = []

        def find_database_files(self):
            return [
                "D:/xwechat/db_storage/message/biz_message_0.db",
                "D:/xwechat/db_storage/message/message_0.db",
            ]

        def open_db(self, path, key):
            self.opened.append((path, key))
            return True

        def is_message_db(self):
            return True

        def close(self):
            pass

    reader = FakeReader()
    platform = SimpleNamespace(db_reader=reader, is_windows=True)
    keys = {
        "message/biz_message_0.db": "bb" * 32,
        "message/message_0.db": "aa" * 32,
    }

    result = AutoReplyPipeline._open_message_db(platform, keys)

    assert result is reader
    assert reader.opened == [
        (
            "D:/xwechat/db_storage/message/message_0.db",
            bytes.fromhex("aa" * 32),
        )
    ]


@pytest.mark.asyncio
async def test_handle_self_message_only_records_memory(monkeypatch):
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "private_chat_mode": "all",
            }
        ),
    )

    sender = FakeSender()
    agent = FakeAgent()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._ai_agent = agent
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 0
    pipeline._name_map = {"wxid_friend": "朋友"}

    await pipeline._handle_message(_private_msg(is_self=True, content="我刚说的"))

    assert pipeline._buffer == {}
    assert sender.sent == []
    assert agent.chats == []
    assert agent.remembered == [
        (
            "我刚说的",
            "private:wxid_friend",
            {
                "is_group": False,
                "user_name": "朋友",
                "user_wxid": "wxid_friend",
                "room_id": "",
                "room_name": "",
                "speaker": "self",
            },
        )
    ]
    assert pipeline._format_recent_context("private:wxid_friend") == "我: 我刚说的"


@pytest.mark.asyncio
async def test_flush_buffer_cleans_reply_before_sending(monkeypatch):
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply={"reply_mode": "ai"}),
    )

    sender = FakeSender()
    agent = FakeAgent()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._ai_agent = agent
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 0
    pipeline._name_map = {"wxid_friend": "朋友"}
    pipeline._buffer["wxid_friend"] = [_private_msg(content="在吗")]

    await pipeline._flush_buffer("wxid_friend")

    assert sender.sent == [
        (
            "好嘞我知道了",
            "朋友",
            {"is_group": False, "force_skip": False, "target_id": "wxid_friend"},
        )
    ]
    assert "\n" not in sender.sent[0][0]
    assert "😄" not in sender.sent[0][0]


def test_clean_reply_for_wechat_removes_extra_spaces_newlines_and_emoji():
    text = AutoReplyPipeline._clean_reply_for_wechat("好 的\n\n我 知道 了  😄  ！")

    assert text == "好的我知道了！"


@pytest.mark.asyncio
async def test_group_reply_rule_immediate_mode_sends_without_buffering(monkeypatch):
    config = {
        "enabled": True,
        "reply_mode": "all",
        "group_chat_mode": "whitelist",
        "group_whitelist": ["room@chatroom"],
        "group_reply_rules": [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "1",
                "rule_only": True,
                "immediate": True,
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply=config),
    )

    sender = FakeSender()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 3600
    pipeline._name_map = {"room@chatroom": "测试群"}

    await pipeline._handle_message(_group_msg(content="通知 @所有人 请马上回复", msg_id="1"))
    await pipeline._handle_message(_group_msg(content="@所有人 重复通知", msg_id="2"))

    assert [item[0] for item in sender.sent] == ["1"]
    assert pipeline._buffer == {}
    assert pipeline._buffer_timers == {}
    assert pipeline._monitor.remembered == [("room@chatroom", "1")]


@pytest.mark.asyncio
async def test_group_reply_rule_can_disable_immediate_mode(monkeypatch):
    config = {
        "enabled": True,
        "reply_mode": "all",
        "group_chat_mode": "whitelist",
        "group_whitelist": ["room@chatroom"],
        "group_reply_rules": [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "自定义回复",
                "rule_only": True,
                "immediate": False,
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply=config),
    )

    sender = FakeSender()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._monitor = FakeMonitor()
    pipeline._park_after_send = False
    pipeline._debounce_seconds = 3600
    pipeline._name_map = {"room@chatroom": "测试群"}

    await pipeline._handle_message(_group_msg(content="@所有人 稍后合并", msg_id="1"))

    assert sender.sent == []
    assert len(pipeline._buffer["room@chatroom"]) == 1
    timer = pipeline._buffer_timers.pop("room@chatroom")
    timer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await timer

    pipeline._debounce_seconds = 0
    await pipeline._flush_buffer("room@chatroom")

    assert [item[0] for item in sender.sent] == ["自定义回复"]


@pytest.mark.parametrize(
    "content",
    [
        "@所有人 请注意",
        "请大家 @所有人 看一下",
        "通知结束 @所有人",
        "wxid_member:\n@所有人\u2005请注意",
    ],
)
@pytest.mark.asyncio
async def test_group_reply_rule_matches_keyword_anywhere_with_custom_reply(
    monkeypatch,
    content,
):
    config = {
        "reply_mode": "ai",
        "group_reply_rules": [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "收到，自定义回复",
                "rule_only": True,
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply=config),
    )

    sender = FakeSender()
    agent = FakeAgent()
    rule_engine = FakeRuleEngine()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._ai_agent = agent
    pipeline._rule_engine = rule_engine
    pipeline._monitor = FakeMonitor()
    pipeline._debounce_seconds = 0
    pipeline._name_map = {"room@chatroom": "测试群"}
    pipeline._buffer["room@chatroom"] = [
        _group_msg(content=content),
    ]

    await pipeline._flush_buffer("room@chatroom")

    assert sender.sent == [
        (
            "收到，自定义回复",
            "测试群",
            {
                "is_group": True,
                "force_skip": False,
                "target_id": "room@chatroom",
            },
        )
    ]
    assert agent.chats == []
    assert rule_engine.calls == []


@pytest.mark.asyncio
async def test_group_rule_only_miss_stays_silent_without_global_rule_or_ai(monkeypatch):
    config = {
        "reply_mode": "all",
        "group_reply_rules": [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "1",
                "rule_only": True,
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply=config),
    )

    sender = FakeSender()
    agent = FakeAgent()
    rule_engine = FakeRuleEngine()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._ai_agent = agent
    pipeline._rule_engine = rule_engine
    pipeline._monitor = FakeMonitor()
    pipeline._debounce_seconds = 0
    pipeline._name_map = {"room@chatroom": "测试群"}
    pipeline._buffer["room@chatroom"] = [
        _group_msg(content="普通群消息"),
    ]

    await pipeline._flush_buffer("room@chatroom")

    assert sender.sent == []
    assert agent.chats == []
    assert rule_engine.calls == []


@pytest.mark.asyncio
async def test_group_rule_batch_replies_only_once_and_other_group_keeps_ai(monkeypatch):
    config = {
        "reply_mode": "ai",
        "group_reply_rules": [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "1",
                "rule_only": True,
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        "app.core.auto_reply_pipeline.get_config",
        lambda: SimpleNamespace(auto_reply=config),
    )

    sender = FakeSender()
    agent = FakeAgent()
    pipeline = AutoReplyPipeline()
    pipeline._sender = sender
    pipeline._ai_agent = agent
    pipeline._monitor = FakeMonitor()
    pipeline._debounce_seconds = 0
    pipeline._name_map = {
        "room@chatroom": "专属群",
        "other@chatroom": "其他群",
    }
    pipeline._buffer["room@chatroom"] = [
        _group_msg(content="@所有人 第一条", msg_id="1"),
        _group_msg(content="第二条也有 @所有人", msg_id="2"),
    ]

    await pipeline._flush_buffer("room@chatroom")

    assert [item[0] for item in sender.sent] == ["1"]
    assert agent.chats == []

    pipeline._buffer["other@chatroom"] = [
        _group_msg(room_id="other@chatroom", content="普通消息", msg_id="3"),
    ]
    await pipeline._flush_buffer("other@chatroom")

    assert [item[0] for item in sender.sent] == ["1", "好嘞我知道了"]
    assert len(agent.chats) == 1


@pytest.mark.asyncio
async def test_windows_pipeline_does_not_repeat_sender_parking(monkeypatch):
    from app.core.auto_reply_pipeline import Platform

    monkeypatch.setattr(
        Platform,
        "get",
        classmethod(lambda cls: SimpleNamespace(is_macos=False)),
    )
    pipeline = AutoReplyPipeline()
    private_sender = FakeSender()
    pipeline._private_sender = private_sender

    await pipeline._park_after_reply()

    assert pipeline._park_after_send is False
    assert private_sender.opened == []


def test_group_reply_rule_config_keeps_custom_reply_and_requires_whitelist():
    from fastapi import HTTPException
    from app.api.config import _normalize_group_reply_rules

    rules = _normalize_group_reply_rules(
        [
            {
                "room_id": "room@chatroom",
                "match_type": "contains",
                "keyword": "@所有人",
                "reply": "自定义内容",
                "rule_only": True,
                "immediate": False,
                "enabled": True,
            }
        ],
        ["room@chatroom"],
    )

    assert rules[0]["reply"] == "自定义内容"
    assert rules[0]["immediate"] is False
    with pytest.raises(HTTPException):
        _normalize_group_reply_rules(rules, [])


def test_group_reply_rule_config_rejects_duplicate_enabled_room():
    from fastapi import HTTPException
    from app.api.config import _normalize_group_reply_rules

    duplicate_rules = [
        {
            "room_id": "room@chatroom",
            "match_type": "contains",
            "keyword": "@所有人",
            "reply": "1",
            "enabled": True,
        },
        {
            "room_id": "room@chatroom",
            "match_type": "contains",
            "keyword": "通知",
            "reply": "收到",
            "enabled": True,
        },
    ]

    with pytest.raises(HTTPException):
        _normalize_group_reply_rules(duplicate_rules, ["room@chatroom"])
