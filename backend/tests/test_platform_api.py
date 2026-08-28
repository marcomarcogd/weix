import asyncio
import os
import sys
from types import SimpleNamespace

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import platform_api
from app.api import config as config_api
from app.core import db_reader_macos


class SharedReader:
    def find_database_files(self):
        return ["/wx/db_storage/contact/contact.db"]

    def open_db(self, *_args, **_kwargs):
        raise AssertionError("contacts API must not reuse platform.db_reader")


class ContactReader:
    opened = []

    def find_database_files(self):
        return ["/wx/db_storage/contact/contact.db"]

    def open_db(self, path, key):
        self.opened.append((path, key))
        return True

    def get_contacts(self):
        return [{"wxid": "wxid_a", "nickname": "A"}]

    def get_chatrooms(self):
        return [{"room_id": "room@chatroom", "name": "测试群"}]


class FakeExtractor:
    def load_keys(self):
        return {"contact/contact.db": "00" * 32}


def test_contacts_api_uses_isolated_reader(monkeypatch):
    shared_reader = SharedReader()
    platform = SimpleNamespace(
        key_extractor=FakeExtractor(),
        db_reader=shared_reader,
        is_macos=True,
    )

    monkeypatch.setattr(platform_api.Platform, "get", lambda: platform)
    monkeypatch.setattr(db_reader_macos, "MacOSDBReader", ContactReader)

    result = asyncio.run(platform_api.list_contacts(type="all", search=""))

    assert result["ready"] is True
    assert result["total_contacts"] == 1
    assert result["total_chatrooms"] == 1
    assert ContactReader.opened == [
        ("/wx/db_storage/contact/contact.db", bytes.fromhex("00" * 32))
    ]


class AccountExtractor:
    def __init__(self):
        self.cleared = False

    def get_available_accounts(self):
        return [
            {"wxid": "account_a", "data_dir": "D:/private/a"},
            {"wxid": "account_b", "data_dir": "D:/private/b"},
        ]

    def selected_account(self):
        return "account_a"

    @property
    def bound_account(self):
        return "account_a"

    @property
    def bound_pid(self):
        return 4321

    def clear_process_binding(self):
        self.cleared = True


def test_accounts_api_does_not_expose_local_data_paths(monkeypatch):
    extractor = AccountExtractor()
    platform = SimpleNamespace(is_windows=True, key_extractor=extractor)
    monkeypatch.setattr(platform_api.Platform, "get", lambda: platform)

    result = asyncio.run(platform_api.list_accounts())

    assert result["selected"] == "account_a"
    assert result["bound_pid"] == 4321
    assert result["accounts"][0]["active"] is True
    assert all("data_dir" not in item for item in result["accounts"])


def test_select_account_persists_and_requires_manager_restart(monkeypatch, tmp_path):
    extractor = AccountExtractor()
    sender = SimpleNamespace(reset_search_state=lambda: None)
    platform = SimpleNamespace(
        is_windows=True,
        key_extractor=extractor,
        _sender=sender,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wechat:\n  windows: {}\n", encoding="utf-8")
    cfg = SimpleNamespace(wechat={"windows": {}})
    monkeypatch.setattr(platform_api.Platform, "get", lambda: platform)
    monkeypatch.setattr(platform_api, "_config_path", lambda: str(config_path))
    monkeypatch.setattr(platform_api, "get_config", lambda: cfg)

    result = asyncio.run(
        platform_api.select_account(platform_api.AccountSelection(wxid="ACCOUNT_B"))
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["wechat"]["windows"]["account"] == "account_b"
    assert result == {
        "success": True,
        "selected": "account_b",
        "requires_restart": True,
    }
    assert extractor.cleared is True


def test_windows_account_discovery_accepts_non_wxid_directory(monkeypatch, tmp_path):
    from app.core.key_extractor_windows import WindowsKeyExtractor

    account_dir = tmp_path / "xbowei_d60e"
    (account_dir / "db_storage").mkdir(parents=True)
    extractor = object.__new__(WindowsKeyExtractor)
    monkeypatch.setattr(extractor, "_find_wechat_data_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(extractor, "selected_account", lambda: "xbowei_d60e")

    accounts = extractor.get_available_accounts()

    assert [item["wxid"] for item in accounts] == ["xbowei_d60e"]
    assert accounts[0]["selected"] is True


def test_chat_config_only_persists_allowlisted_uia_policy_fields(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "auto_reply:\n  enabled: false\nwindows_sender:\n  verify_timeout: 30\n",
        encoding="utf-8",
    )
    cfg = SimpleNamespace(
        auto_reply={"enabled": False},
        windows_sender={"verify_timeout": 30},
    )
    monkeypatch.setattr(config_api, "_get_config_path", lambda: str(config_path))
    monkeypatch.setattr(config_api, "get_config", lambda: cfg)
    monkeypatch.setattr(
        "app.core.platform.Platform.get",
        lambda: SimpleNamespace(_sender=None),
    )

    result = asyncio.run(
        config_api.update_chat_config(
            {
                "enabled": True,
                "windows_sender": {
                    "send_mode": "background",
                    "background_post_message": True,
                    "allow_foreground_activation": False,
                    "hot_activate_accessibility": True,
                    "send_retries": 99,
                    "mouse_fallback": True,
                },
            }
        )
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result == {"success": True}
    assert saved["windows_sender"] == {
        "verify_timeout": 30,
        "method": "uia",
        "send_mode": "background",
        "background_post_message": True,
        "allow_foreground_activation": False,
        "hot_activate_accessibility": True,
    }
    assert cfg.auto_reply["enabled"] is True


def test_activate_uia_api_uses_explicit_sender_action(monkeypatch):
    calls = []

    async def activate_uia():
        calls.append("activate")
        return {
            "ok": True,
            "status": "activated",
            "wrote_memory": True,
        }

    platform = SimpleNamespace(
        is_windows=True,
        sender=SimpleNamespace(activate_uia=activate_uia),
    )
    monkeypatch.setattr(platform_api.Platform, "get", lambda: platform)

    result = asyncio.run(platform_api.activate_uia())

    assert result["ok"] is True
    assert result["wrote_memory"] is True
    assert calls == ["activate"]
