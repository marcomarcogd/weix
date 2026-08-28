import os
import sys
import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.base import WeChatMessage
from app.core.message_monitor import MessageMonitor, MonitorConfig


class DummyDBReader:
    def query_messages_since(self, timestamp: int):
        return []


def _monitor() -> MessageMonitor:
    return MessageMonitor(DummyDBReader(), MonitorConfig())


def _msg(sender: str, *, room_id: str = "", is_group: bool = False) -> WeChatMessage:
    return WeChatMessage(
        msg_id=f"{sender}:{room_id or 'private'}",
        msg_type=1,
        content="测试",
        sender=sender,
        room_id=room_id,
        create_time=datetime.fromtimestamp(1778673000),
        is_group=is_group,
    )


def test_should_process_keeps_self_message_for_memory(monkeypatch):
    monkeypatch.setattr(
        "app.core.message_monitor.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "private_chat_mode": "all",
            }
        ),
    )
    monitor = _monitor()
    monitor.remember_sent_message("wxid_allowed", "测试")

    msg = _msg("wxid_allowed")
    msg.is_self = True
    msg.content = "测试"

    assert monitor._should_process(msg) is True
    assert monitor._stats.self_skipped == 1


def test_should_process_keeps_private_for_local_statistics(monkeypatch):
    monkeypatch.setattr(
        "app.core.message_monitor.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "private_chat_mode": "whitelist",
                "private_whitelist": ["wxid_allowed"],
            }
        ),
    )

    assert _monitor()._should_process(_msg("wxid_blocked")) is True


def test_should_process_keeps_private_in_whitelist(monkeypatch):
    monkeypatch.setattr(
        "app.core.message_monitor.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "private_chat_mode": "whitelist",
                "private_whitelist": ["wxid_allowed"],
            }
        ),
    )

    assert _monitor()._should_process(_msg("wxid_allowed")) is True


def test_should_process_keeps_group_for_local_statistics(monkeypatch):
    monkeypatch.setattr(
        "app.core.message_monitor.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "group_chat_mode": "whitelist",
                "group_whitelist": ["allowed@chatroom"],
            }
        ),
    )

    msg = _msg("member_wxid", room_id="blocked@chatroom", is_group=True)

    assert _monitor()._should_process(msg) is True


def test_should_process_skips_recent_bot_sent_message(monkeypatch):
    monkeypatch.setattr(
        "app.core.message_monitor.get_config",
        lambda: SimpleNamespace(
            auto_reply={
                "enabled": True,
                "private_chat_mode": "all",
            }
        ),
    )
    monitor = _monitor()
    monitor.remember_sent_message("wxid_receiver", "AI 回复")

    msg = _msg("wxid_receiver")
    msg.content = "AI 回复"

    assert monitor._should_process(msg) is False


@pytest.mark.asyncio
async def test_stop_waits_for_active_poll_executor_to_finish():
    """停止监听时应等待正在执行的线程池查询结束，避免 loop 关闭后线程回调。"""
    started = threading.Event()
    release = threading.Event()

    class BlockingDBReader:
        def query_messages_since(self, timestamp: int):
            started.set()
            release.wait(timeout=1.0)
            return []

    monitor = MessageMonitor(
        BlockingDBReader(),
        MonitorConfig(poll_interval=60.0),
    )
    await monitor.start(lookback_seconds=0)
    assert await asyncio.to_thread(started.wait, 1.0)

    stop_task = asyncio.create_task(monitor.stop())
    await asyncio.sleep(0.05)

    assert not stop_task.done()

    release.set()
    await asyncio.wait_for(stop_task, timeout=1.0)


def test_send_confirmation_matches_self_target_content_and_time():
    monitor = _monitor()
    monitor._running = True
    now = int(time.time())
    handle = monitor.register_send_confirmation(
        "room@chatroom",
        "收到",
        now - 2,
        5.0,
    )
    msg = WeChatMessage(
        msg_id="self:group:1",
        msg_type=1,
        content="wxid_friend:\n收到\u200b",
        sender="wxid_self",
        room_id="room@chatroom",
        create_time=datetime.fromtimestamp(now),
        is_group=True,
        is_self=True,
    )

    monitor._notify_send_confirmations(msg)

    assert handle.wait(0) is True


def test_send_confirmation_rejects_peer_wrong_target_and_stale_messages():
    monitor = _monitor()
    monitor._running = True
    now = int(time.time())
    handle = monitor.register_send_confirmation(
        "room@chatroom",
        "只允许精确匹配",
        now - 2,
        5.0,
    )

    wrong_messages = [
        WeChatMessage(
            msg_id="peer",
            msg_type=1,
            content="只允许精确匹配",
            sender="wxid_peer",
            room_id="room@chatroom",
            create_time=datetime.fromtimestamp(now),
            is_group=True,
            is_self=False,
        ),
        WeChatMessage(
            msg_id="wrong-room",
            msg_type=1,
            content="只允许精确匹配",
            sender="wxid_self",
            room_id="other@chatroom",
            create_time=datetime.fromtimestamp(now),
            is_group=True,
            is_self=True,
        ),
        WeChatMessage(
            msg_id="stale",
            msg_type=1,
            content="只允许精确匹配",
            sender="wxid_self",
            room_id="room@chatroom",
            create_time=datetime.fromtimestamp(now - 60),
            is_group=True,
            is_self=True,
        ),
    ]
    for msg in wrong_messages:
        monitor._notify_send_confirmations(msg)

    assert handle.wait(0) is False
    handle.cancel()
