import hashlib
import hmac
import os
import sqlite3
import struct
import sys

import pytest
from Crypto.Cipher import AES

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.db_reader_windows import (
    PAGE_SIZE,
    WAL_FRAME_SIZE,
    WAL_HEADER_SIZE,
    WindowsDBReader,
)
from app.core.wechat_paths_windows import WeChatDataDir, find_wechat_data_dirs
from diagnose_weixin_windows import _find_message_key, _is_message_db_rel_path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属测试")
def test_windows_db_reader_find_database_files_scans_wechat_documents(tmp_path, monkeypatch):
    """Windows reader 应能在 Documents/WeChat Files 下发现微信 DB 文件。"""
    base = tmp_path / "Documents" / "WeChat Files" / "wxid_user"
    msg_dir = base / "Msg"
    contact_dir = base / "Contact"
    msg_dir.mkdir(parents=True)
    contact_dir.mkdir(parents=True)
    msg_db = msg_dir / "MSG.db"
    micro_msg_db = contact_dir / "MicroMsg.db"
    ignored = msg_dir / "ignored.txt"
    msg_db.write_text("x")
    micro_msg_db.write_text("x")
    ignored.write_text("x")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    files = WindowsDBReader().find_database_files()

    assert str(msg_db) in files
    assert str(micro_msg_db) in files
    assert str(ignored) not in files


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属测试")
def test_windows_db_reader_find_database_files_scans_xwechat_storage(tmp_path, monkeypatch):
    """Windows reader 应能发现新版 xwechat_files/db_storage 下的 DB 文件。"""
    base = tmp_path / "xwechat_files" / "wxid_user" / "db_storage"
    msg_dir = base / "message"
    contact_dir = base / "contact"
    msg_dir.mkdir(parents=True)
    contact_dir.mkdir(parents=True)
    msg_db = msg_dir / "message_0.db"
    contact_db = contact_dir / "contact.db"
    ignored = msg_dir / "ignored.txt"
    msg_db.write_text("x")
    contact_db.write_text("x")
    ignored.write_text("x")

    monkeypatch.setattr(
        "app.core.db_reader_windows.find_wechat_data_dirs",
        lambda: [WeChatDataDir(str(tmp_path / "xwechat_files"), "test")],
    )

    files = WindowsDBReader().find_database_files()

    assert str(msg_db) in files
    assert str(contact_db) in files
    assert str(ignored) not in files


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属测试")
def test_wechat_data_dir_discovery_scans_one_level_drive_dirs(tmp_path, monkeypatch):
    """应能发现 D:\\wxjilu\\xwechat_files 这类自定义保存目录。"""
    data_root = tmp_path / "wxjilu" / "xwechat_files"
    data_root.mkdir(parents=True)

    monkeypatch.setattr(
        "app.core.wechat_paths_windows.get_available_drives",
        lambda: [str(tmp_path) + os.sep],
    )
    monkeypatch.setattr(
        "app.core.wechat_paths_windows.read_wechat_install_path_from_registry",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.core.wechat_paths_windows.get_wechat_exe_path",
        lambda: None,
    )

    data_dirs = find_wechat_data_dirs()

    assert WeChatDataDir(str(data_root), "drive shallow scan") in data_dirs


def test_windows_v4_self_sent_uses_current_account_rowid(monkeypatch):
    """当前账号 rowid 不是 1 时，也应识别为自己发出的消息。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_friend')")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (2, 'wxid_me')")

    reader = WindowsDBReader()
    reader._sqlite_conn = conn
    monkeypatch.setattr(WindowsDBReader, "get_current_wxid", classmethod(lambda cls: "wxid_me"))

    self_row = conn.execute(
        "SELECT 2 AS real_sender_id, 3 AS status, 0 AS origin_source, 123 AS server_seq"
    ).fetchone()
    friend_row = conn.execute(
        "SELECT 1 AS real_sender_id, 3 AS status, 0 AS origin_source, 123 AS server_seq"
    ).fetchone()

    assert reader._is_self_sent_v4_row(self_row) is True
    assert reader._is_self_sent_v4_row(friend_row) is False
    assert reader._get_current_sender_id() == 2


def test_windows_v4_self_sent_fallback_keeps_local_send_signature():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    reader = WindowsDBReader()
    reader._sqlite_conn = conn

    local_send_row = conn.execute(
        "SELECT 0 AS real_sender_id, 2 AS status, 1 AS origin_source, 0 AS server_seq"
    ).fetchone()

    assert reader._is_self_sent_v4_row(local_send_row) is True


@pytest.mark.parametrize(
    "path",
    [
        "message/message_0.db",
        "account_a/message/message_0.db",
        r"account_b\message\message_0.db",
    ],
)
def test_diagnose_recognizes_nested_windows_v4_message_db(path):
    assert _is_message_db_rel_path(path) is True


def test_diagnose_finds_nested_windows_v4_message_key():
    keys = {
        "account_a/contact/contact.db": "CONTACT_KEY",
        "account_a/message/message_0.db": "MESSAGE_KEY",
    }

    assert _find_message_key(keys) == "MESSAGE_KEY"


def test_windows_v4_group_sender_uses_name2id_rowid_and_cleans_prefix():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_me')")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (7, 'wxid_friend')")

    reader = WindowsDBReader()
    reader._sqlite_conn = conn
    row = conn.execute(
        "SELECT 7 AS real_sender_id, ? AS source",
        (b"unrelated protobuf bytes",),
    ).fetchone()

    sender = reader._resolve_v4_group_sender(row, "room@chatroom")
    content = reader._clean_group_content("wxid_friend:\n收到", sender)

    assert sender == "wxid_friend"
    assert content == "收到"

    reader.close()


def test_windows_group_content_prefix_fallback_does_not_use_room_id_as_sender():
    reader = WindowsDBReader()

    sender = reader._parse_group_sender_from_content("wxid_member:\n收到")
    content = reader._clean_group_content("wxid_member:\n收到", sender)

    assert sender == "wxid_member"
    assert content == "收到"
    assert reader._clean_group_content("时间: 10:00", "room@chatroom") == "时间: 10:00"


def test_windows_v4_missing_group_sender_keeps_safe_room_fallback():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    reader = WindowsDBReader()
    reader._sqlite_conn = conn
    row = conn.execute(
        "SELECT 0 AS real_sender_id, ? AS source",
        (b"unknown sender bytes",),
    ).fetchone()

    assert reader._resolve_v4_group_sender(row, "room@chatroom") == "room@chatroom"
    assert reader._clean_group_content("普通消息", "room@chatroom") == "普通消息"

    reader.close()


def test_windows_v4_missing_sender_id_fails_closed(monkeypatch):
    """即使已知本人 rowid，消息缺少发送者 ID 时也不能猜成对方。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (7, 'wxid_me')")
    reader = WindowsDBReader()
    reader._sqlite_conn = conn
    monkeypatch.setattr(reader, "get_current_wxid", lambda: "wxid_me")
    row = conn.execute(
        "SELECT 0 AS real_sender_id, 3 AS status, "
        "2 AS origin_source, 100 AS server_seq"
    ).fetchone()

    assert reader._is_self_sent_v4_row(row) is True

    reader.close()


@pytest.mark.parametrize("self_sender_id", [2, 987654])
def test_windows_v4_infers_self_sender_from_dominant_local_markers(
    monkeypatch,
    self_sender_id,
):
    """sender_id 在不同机器上变化时应动态识别，不能写死本机值。"""
    reader = WindowsDBReader()
    reader._sqlite_conn = sqlite3.connect(":memory:")
    reader._sqlite_conn.row_factory = sqlite3.Row
    reader._msg_table_cache = [
        ("Msg_a", "friend_a"),
        ("Msg_b", "friend_b"),
    ]
    for table in ("Msg_a", "Msg_b"):
        reader._sqlite_conn.execute(
            f'CREATE TABLE "{table}" ('
            "real_sender_id INTEGER, status INTEGER, "
            "origin_source INTEGER, server_seq INTEGER)"
        )
    reader._sqlite_conn.executemany(
        "INSERT INTO Msg_a VALUES (?, ?, ?, ?)",
        [(self_sender_id, 2, 1, 0)] * 5 + [(31, 3, 2, 100)] * 3,
    )
    reader._sqlite_conn.executemany(
        "INSERT INTO Msg_b VALUES (?, ?, ?, ?)",
        [(self_sender_id, 2, 1, 0)] * 4 + [(40, 3, 2, 101)] * 2,
    )
    monkeypatch.setattr(reader, "get_current_wxid", lambda: "alias_folder")

    assert reader._get_current_sender_id() == self_sender_id
    assert reader._is_self_sent_v4_row({
        "real_sender_id": self_sender_id,
        "status": 3,
        "origin_source": 2,
        "server_seq": 999,
    }) is True
    assert reader._is_self_sent_v4_row({
        "real_sender_id": 31,
        "status": 3,
        "origin_source": 2,
        "server_seq": 998,
    }) is False

    reader.close()


def test_windows_v4_ambiguous_direction_fails_closed(monkeypatch):
    """两个候选证据接近时不能猜测；所有不确定消息均按自己处理以阻止回复。"""
    reader = WindowsDBReader()
    reader._sqlite_conn = sqlite3.connect(":memory:")
    reader._sqlite_conn.row_factory = sqlite3.Row
    reader._msg_table_cache = [
        ("Msg_a", "friend_a"),
        ("Msg_b", "friend_b"),
    ]
    for table in ("Msg_a", "Msg_b"):
        reader._sqlite_conn.execute(
            f'CREATE TABLE "{table}" ('
            "real_sender_id INTEGER, status INTEGER, "
            "origin_source INTEGER, server_seq INTEGER)"
        )
    reader._sqlite_conn.executemany(
        "INSERT INTO Msg_a VALUES (?, ?, ?, ?)",
        [(7, 2, 1, 0)] * 6,
    )
    reader._sqlite_conn.executemany(
        "INSERT INTO Msg_b VALUES (?, ?, ?, ?)",
        [(9, 2, 1, 0)] * 5,
    )
    monkeypatch.setattr(reader, "get_current_wxid", lambda: "unknown_account")

    assert reader._get_current_sender_id() is None
    assert reader._is_self_sent_v4_row({
        "real_sender_id": 31,
        "status": 3,
        "origin_source": 2,
        "server_seq": 998,
    }) is True

    reader.close()


def test_windows_v4_conflicting_evidence_fails_closed(monkeypatch):
    """本机发送标记与跨私聊覆盖率指向不同 ID 时必须拒绝判定。"""
    reader = WindowsDBReader()
    reader._sqlite_conn = sqlite3.connect(":memory:")
    reader._sqlite_conn.row_factory = sqlite3.Row
    reader._msg_table_cache = [("Msg_group", "room@chatroom")]
    reader._sqlite_conn.execute(
        "CREATE TABLE Msg_group (real_sender_id INTEGER, status INTEGER, "
        "origin_source INTEGER, server_seq INTEGER)"
    )
    reader._sqlite_conn.executemany(
        "INSERT INTO Msg_group VALUES (?, ?, ?, ?)",
        [(99, 2, 1, 0)] * 20,
    )

    for index in range(6):
        table = f"Msg_private_{index}"
        reader._msg_table_cache.append((table, f"friend_{index}"))
        reader._sqlite_conn.execute(
            f'CREATE TABLE "{table}" ('
            "real_sender_id INTEGER, status INTEGER, "
            "origin_source INTEGER, server_seq INTEGER)"
        )
        reader._sqlite_conn.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?)',
            [(77, 3, 2, 100 + index), (1000 + index, 3, 2, 200 + index)],
        )
    monkeypatch.setattr(reader, "get_current_wxid", lambda: "unknown_account")

    assert reader._get_current_sender_id() is None

    reader.close()


def _build_encrypted_wal_page(
    reader: WindowsDBReader,
    page_number: int,
    marker: int,
) -> tuple[bytes, bytes]:
    plaintext = bytes([marker]) + b"\x00" * (
        PAGE_SIZE - reader._reserve_size - 1
    )
    iv = bytes(range(16))
    encrypted = AES.new(reader._aes_key, AES.MODE_CBC, iv=iv).encrypt(
        plaintext
    )
    authenticated = encrypted + iv
    digest = hmac.new(reader._hmac_key, authenticated, hashlib.sha512)
    digest.update(struct.pack("<I", page_number))
    return authenticated + digest.digest(), plaintext + b"\x00" * 80


def test_windows_wal_merge_uses_current_salt_and_last_committed_frame():
    """预分配 WAL 中旧世代和未提交尾帧都不能覆盖最新已提交消息。"""
    reader = WindowsDBReader()
    reader._aes_key = bytes(range(32))
    reader._hmac_key = bytes(range(32, 64))
    reader._reserve_size = 80
    reader._hmac_hash = "sha512"

    old_salt = b"old-salt"
    current_salt = b"new-salt"
    old_page, _ = _build_encrypted_wal_page(reader, 2, 5)
    committed_page, committed_plain = _build_encrypted_wal_page(reader, 2, 13)
    uncommitted_page, _ = _build_encrypted_wal_page(reader, 2, 10)

    header = struct.pack(">III", 0x377F0682, 3007000, PAGE_SIZE)
    header += b"\x00" * 4 + current_salt + b"\x00" * 8
    frames = [
        struct.pack(">II", 2, 2) + old_salt + b"\x00" * 8 + old_page,
        struct.pack(">II", 2, 2) + current_salt + b"\x00" * 8 + committed_page,
        struct.pack(">II", 2, 0) + current_salt + b"\x00" * 8 + uncommitted_page,
    ]
    wal_data = header + b"".join(frames)

    pages, frame_count, db_size = reader._collect_committed_wal_pages(wal_data)

    assert len(wal_data) == WAL_HEADER_SIZE + 3 * WAL_FRAME_SIZE
    assert pages == {2: committed_plain}
    assert frame_count == 1
    assert db_size == 2


def test_windows_wal_hmac_is_bound_to_actual_page_number():
    reader = WindowsDBReader()
    reader._aes_key = bytes(range(32))
    reader._hmac_key = bytes(range(32, 64))
    page, _ = _build_encrypted_wal_page(reader, 7, 13)

    assert reader._verify_page_hmac(
        page,
        reader._hmac_key,
        80,
        "sha512",
        page_number=7,
    ) is True
    assert reader._verify_page_hmac(
        page,
        reader._hmac_key,
        80,
        "sha512",
        page_number=1,
    ) is False


def test_windows_reuses_empty_query_until_snapshot_changes(monkeypatch):
    """同一数据库快照不应每 0.5 秒重复扫描全部会话表。"""
    reader = WindowsDBReader()
    reader._sqlite_conn = sqlite3.connect(":memory:")
    reader._sqlite_conn.row_factory = sqlite3.Row
    reader._sqlite_conn.execute("CREATE TABLE Msg_test (local_id INTEGER)")
    reader._last_refresh = float("inf")
    reader._source_signature = (4096, 1)
    reader._wal_signature = (4120, 2)
    calls = []

    def fake_query(timestamp):
        calls.append(timestamp)
        return []

    monkeypatch.setattr(reader, "_query_v4_messages_since", fake_query)

    assert reader.query_messages_since(123) == []
    assert reader.query_messages_since(123) == []
    assert calls == [123]

    reader._wal_signature = (8240, 3)
    assert reader.query_messages_since(123) == []
    assert calls == [123, 123]

    reader.close()
