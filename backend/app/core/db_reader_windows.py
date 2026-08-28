"""Windows 平台 WeChat 数据库读取器。

使用 pycryptodome 的 AES-256-CBC 解密 SQLCipher4 加密的 SQLite 数据库页面，
以只读模式读取微信消息和联系人数据。
"""

import hashlib
import hmac as hmac_mod
import logging
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime
from typing import ClassVar, Optional

from Crypto.Cipher import AES
from Crypto.Hash import SHA512
from Crypto.Protocol.KDF import PBKDF2

from app.core.base import BaseDBReader, WeChatMessage
from app.core.wechat_paths_windows import find_wechat_data_dirs

logger = logging.getLogger(__name__)

# SQLCipher4 页面大小
PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
# SQLCipher4 HMAC-SHA512: IV(16) + HMAC(64)
RESERVED_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_FRAME_SIZE = WAL_FRAME_HEADER_SIZE + PAGE_SIZE
# 消息类型常量
MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_VOICE = 34
MSG_TYPE_CARD = 49
MSG_TYPE_SYSTEM = 10000


class WindowsDBReader(BaseDBReader):
    """Windows 平台 WeChat 数据库读取器。

    使用 pycryptodome 的 AES-256-CBC 手动解密 SQLCipher4 加密的数据库页面。
    在每个查询中按需解密所需页面，避免全量解密。
    """

    # Windows 微信数据目录（常见路径，运行时会动态补充注册表和进程路径）
    WINDOWS_DATA_DIRS: ClassVar[list[str]] = [
        os.path.expandvars(r"%USERPROFILE%\Documents\WeChat Files"),
        os.path.expandvars(r"%USERPROFILE%\Documents\xwechat_files"),
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat"),
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat\WeChat Files"),
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat\xwechat_files"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\WeChat"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\WeChat\WeChat Files"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\WeChat\xwechat_files"),
        r"D:\WeChat Files",
        r"D:\xwechat_files",
        r"E:\WeChat Files",
        r"E:\xwechat_files",
    ]

    # 消息监听器默认每 0.5 秒轮询。这里只限制磁盘签名检查频率；
    # 实际刷新优先合并几 MB 的 WAL，不再每轮解密整个消息库。
    REFRESH_INTERVAL = 0.25

    def __init__(self):
        self._key: Optional[bytes] = None
        self._aes_key: Optional[bytes] = None
        self._hmac_key: Optional[bytes] = None
        self._db_path: str = ""
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._decrypted_path: str = ""
        self._iterations: int = 256000
        self._reserve_size: int = RESERVED_SIZE
        self._hmac_hash: str = "sha512"
        self._key_mode: str = ""
        self._lock = threading.Lock()
        self._msg_table_cache: Optional[list[tuple[str, str]]] = None
        self._name2id_cache: Optional[dict[int, str]] = None
        self._last_refresh: float = 0
        self._source_signature: Optional[tuple[int, int]] = None
        self._wal_signature: Optional[tuple[int, int]] = None
        self._last_empty_query_key: Optional[
            tuple[
                int,
                Optional[tuple[int, int]],
                Optional[tuple[int, int]],
            ]
        ] = None
        self._current_sender_id: Optional[int] = None
        self._direction_warning_logged: bool = False
        self._full_refresh_retry_after: float = 0.0
        self._full_refresh_backoff_seconds: float = 5.0

    # --- 公共接口 ---

    def open_db(self, db_path: str, key: bytes) -> bool:
        """打开并解密数据库。

        Args:
            db_path: 加密数据库文件路径。
            key: 原始密钥字节 (32 bytes)。

        Returns:
            True 表示成功打开。
        """
        logger.info(f"打开数据库: {db_path}")

        if not os.path.exists(db_path):
            logger.error(f"数据库文件不存在: {db_path}")
            return False

        # 同一个 reader 可能会依次尝试多个候选库。每次打开前必须清空
        # 上一个数据库的表映射和身份推断，避免把另一台机器/另一账号的
        # sender_id 缓存带到当前库。
        if self._sqlite_conn is not None or self._decrypted_path:
            self.close()
        self._msg_table_cache = None
        self._name2id_cache = None
        self._current_sender_id = None
        self._direction_warning_logged = False

        self._db_path = db_path
        self._key = key

        # 派生加密密钥
        if not self._derive_key():
            return False

        # 首次打开仍需生成完整解密快照；后续消息通过 WAL 增量合并。
        try:
            (
                self._decrypted_path,
                self._sqlite_conn,
                self._source_signature,
                self._wal_signature,
            ) = self._build_full_snapshot()
            self._last_refresh = time.monotonic()
            self._full_refresh_retry_after = 0.0
            logger.info("数据库打开成功")
            return True
        except Exception as exc:
            logger.error(f"打开数据库失败: {exc}")
            return False

    def refresh(self) -> bool:
        """刷新解密快照以获取最新消息。

        主库未变化时只合并 WAL 中已提交的新页面；仅当微信 checkpoint
        改写主库时才重新生成完整快照。WAL 合并失败会恢复原页面并保留
        旧连接，不会让查询读到半写入状态。

        Returns:
            True 表示刷新成功。
        """
        if not self._db_path or not self._key:
            return False
        started = time.perf_counter()
        refresh_reason = "unchanged"
        try:
            with self._lock:
                source_signature = self._file_signature(self._db_path)
                wal_signature = self._file_signature(self._wal_path())

                if (
                    source_signature == self._source_signature
                    and wal_signature == self._wal_signature
                ):
                    self._last_refresh = time.monotonic()
                    return True

                if source_signature != self._source_signature:
                    refresh_reason = "checkpoint"
                    now = time.monotonic()
                    if now < self._full_refresh_retry_after:
                        logger.debug(
                            "微信主库 checkpoint 重建处于退避期，保留上一份可读快照"
                        )
                        return False
                    try:
                        self._replace_with_full_snapshot_locked(reason=refresh_reason)
                    except Exception:
                        self._full_refresh_retry_after = (
                            time.monotonic() + self._full_refresh_backoff_seconds
                        )
                        raise
                else:
                    refresh_reason = "wal"
                    self._refresh_from_wal_locked()
                self._full_refresh_retry_after = 0.0
                self._last_refresh = time.monotonic()
            elapsed = time.perf_counter() - started
            if elapsed >= 1.0:
                logger.info(
                    "数据库刷新耗时较长: reason=%s, elapsed=%.3fs",
                    refresh_reason,
                    elapsed,
                )
            return True
        except Exception as exc:
            self._full_refresh_retry_after = max(
                self._full_refresh_retry_after,
                time.monotonic() + self._full_refresh_backoff_seconds,
            )
            logger.error(f"刷新数据库副本失败: {exc}")
            return False

    def query_messages_since(self, timestamp: int) -> list[WeChatMessage]:
        """查询指定时间戳之后的消息。

        Args:
            timestamp: Unix 时间戳。

        Returns:
            WeChatMessage 列表。
        """
        if self._sqlite_conn is None:
            logger.error("数据库未打开")
            return []

        # 定期刷新解密副本以获取微信新写入的消息
        if time.monotonic() - self._last_refresh > self.REFRESH_INTERVAL:
            self.refresh()

        if self._has_msg_shard_tables():
            query_key = (
                int(timestamp),
                self._source_signature,
                self._wal_signature,
            )
            if query_key == self._last_empty_query_key:
                return []
            messages = self._query_v4_messages_since(timestamp)
            self._last_empty_query_key = query_key if not messages else None
            return messages

        try:
            cursor = self._sqlite_conn.execute(
                """
                SELECT msg_id, msg_content, msg_type, msg_talker,
                       msg_create_time, chatroom_id, at_list
                FROM MSG
                WHERE msg_create_time > ?
                ORDER BY msg_create_time ASC
                """,
                (timestamp,),
            )

            messages: list[WeChatMessage] = []
            for row in cursor:
                msg_type = row["msg_type"] or 0
                is_group = False
                room_id = ""
                sender = row["msg_talker"] or ""

                # 判断群聊: talker 以 @chatroom 结尾
                if sender and "@chatroom" in str(sender):
                    is_group = True
                    room_id = str(sender)

                # 解析 @ 列表
                at_list: list[str] = []
                at_raw = row["at_list"] or ""
                if at_raw:
                    try:
                        at_list = str(at_raw).split(",")
                    except Exception:
                        pass

                content = row["msg_content"] or ""
                # 处理二进制内容
                if isinstance(content, bytes):
                    try:
                        content = content.decode("utf-8", errors="replace")
                    except Exception:
                        content = str(content)

                if is_group:
                    sender_from_content = self._parse_group_sender_from_content(content)
                    sender = sender_from_content or sender
                    content = self._clean_group_content(content, sender)

                msg = WeChatMessage(
                    msg_id=str(row["msg_id"] or ""),
                    msg_type=msg_type,
                    content=str(content),
                    sender=str(sender),
                    room_id=room_id,
                    create_time=datetime.fromtimestamp(
                        (row["msg_create_time"] or 0) / 1000.0
                    ),
                    is_group=is_group,
                    at_list=at_list,
                )
                messages.append(msg)

            logger.debug(
                f"查询到 {len(messages)} 条消息 (timestamp > {timestamp})"
            )
            return messages

        except Exception as exc:
            logger.error(f"查询消息失败: {exc}")
            return []

    def _query_v4_messages_since(self, timestamp: int) -> list[WeChatMessage]:
        """查询 Windows Weixin 4.x Msg_<md5> 分表消息。"""
        if self._sqlite_conn is None:
            return []

        ts_sec = timestamp // 1000 if timestamp > 10000000000 else timestamp
        try:
            msg_tables = self._get_v4_msg_tables()
            messages: list[WeChatMessage] = []
            scanned = 0

            for table, username in msg_tables:
                try:
                    cursor = self._sqlite_conn.execute(
                        f'SELECT local_id, create_time, real_sender_id, '
                        f'message_content, local_type, source, '
                        f'status, origin_source, server_seq '
                        f'FROM "{table}" '
                        f'WHERE create_time > ? '
                        f'ORDER BY create_time ASC',
                        (ts_sec,),
                    )
                except Exception:
                    continue

                for row in cursor:
                    scanned += 1
                    is_self = self._is_self_sent_v4_row(row)
                    local_type = row["local_type"] or 0
                    if local_type != MSG_TYPE_TEXT:
                        continue

                    content = self._decode_message_content(row["message_content"])
                    if not content.strip() or self._is_garbled(content):
                        continue

                    is_group = "@chatroom" in username
                    sender = username
                    if is_group:
                        sender = self._resolve_v4_group_sender(row, username)
                        content = self._clean_group_content(content, sender)

                    messages.append(
                        WeChatMessage(
                            msg_id=f"{table}:{row['local_id']}",
                            msg_type=local_type,
                            content=content,
                            sender=sender,
                            room_id=username if is_group else "",
                            create_time=datetime.fromtimestamp(row["create_time"] or 0),
                            is_group=is_group,
                            is_self=is_self,
                            at_list=[],
                        )
                    )

            messages.sort(key=lambda msg: msg.create_time or datetime.min)
            if messages:
                logger.info(
                    f"检测到 {len(messages)} 条 Windows 4.x 新文本消息 "
                    f"(扫描 {len(msg_tables)} 个表, 命中 {scanned} 行)"
                )
            return messages
        except Exception as exc:
            logger.error(f"查询 Windows 4.x 消息失败: {exc}")
            return []

    def get_contacts(self) -> list[dict]:
        """获取联系人列表。

        Returns:
            联系人字典列表，包含 wxid, name, remark, type 等字段。
        """
        if self._sqlite_conn is None:
            logger.error("数据库未打开")
            return []

        try:
            if self._is_v4_contact_schema():
                return self._get_contacts_v4()

            cursor = self._sqlite_conn.execute(
                """
                SELECT UserName, Alias, NickName, Remark, Type,
                       HeadImgUrl, ChatRoomType
                FROM Contact
                WHERE UserName != ''
                ORDER BY NickName
                """
            )

            contacts: list[dict] = []
            for row in cursor:
                contact = {
                    "wxid": row["UserName"] or "",
                    "alias": row["Alias"] or "",
                    "nickname": row["NickName"] or "",
                    "remark": row["Remark"] or "",
                    "type": row["Type"] or 0,
                    "head_img_url": row["HeadImgUrl"] or "",
                    "chatroom_type": row["ChatRoomType"] or 0,
                }
                contacts.append(contact)

            logger.debug(f"获取到 {len(contacts)} 个联系人")
            return contacts

        except Exception as exc:
            logger.error(f"获取联系人失败: {exc}")
            return []

    def _get_contacts_v4(self) -> list[dict]:
        """微信 4.x schema: contact 表，小写列名。"""
        cursor = self._sqlite_conn.execute(
            """
            SELECT username, alias, nick_name, remark, local_type,
                   big_head_url, small_head_url, chat_room_type
            FROM contact
            WHERE username != '' AND delete_flag = 0
            ORDER BY nick_name
            """
        )
        contacts: list[dict] = []
        for row in cursor:
            contact = {
                "wxid": row["username"] or "",
                "alias": row["alias"] or "",
                "nickname": row["nick_name"] or "",
                "remark": row["remark"] or "",
                "type": row["local_type"] or 0,
                "head_img_url": row["big_head_url"] or row["small_head_url"] or "",
                "chatroom_type": row["chat_room_type"] or 0,
            }
            contacts.append(contact)
        logger.debug(f"获取到 {len(contacts)} 个联系人 (V4 schema)")
        return contacts

    def get_chatrooms(self) -> list[dict]:
        """获取群聊列表。

        Returns:
            群聊字典列表。
        """
        if self._sqlite_conn is None:
            logger.error("数据库未打开")
            return []

        try:
            if self._is_v4_contact_schema():
                return self._get_chatrooms_v4()

            cursor = self._sqlite_conn.execute(
                """
                SELECT ChatRoomName, UserNameList, DisplayNameList,
                       ChatRoomOwner, MemberCount
                FROM ChatRoom
                WHERE ChatRoomName != ''
                """
            )

            rooms: list[dict] = []
            for row in cursor:
                room = {
                    "room_id": row["ChatRoomName"] or "",
                    "members": (row["UserNameList"] or "").split(";"),
                    "display_names": (row["DisplayNameList"] or "").split(";"),
                    "owner": row["ChatRoomOwner"] or "",
                    "member_count": row["MemberCount"] or 0,
                }
                rooms.append(room)

            logger.debug(f"获取到 {len(rooms)} 个群聊")
            return rooms

        except Exception as exc:
            logger.error(f"获取群聊列表失败: {exc}")
            return []

    def _get_chatrooms_v4(self) -> list[dict]:
        """微信 4.x schema: chat_room + chatroom_member + contact。"""
        rooms: list[dict] = []
        room_rows = self._sqlite_conn.execute(
            "SELECT id, username, owner FROM chat_room WHERE username != ''"
        ).fetchall()

        for rr in room_rows:
            room_id = rr["username"] or ""
            room_pk = rr["id"]
            owner = rr["owner"] or ""
            if not room_id:
                continue

            member_count = self._sqlite_conn.execute(
                "SELECT COUNT(*) FROM chatroom_member WHERE room_id = ?",
                (room_pk,),
            ).fetchone()[0]

            # 群名来源：contact 表的 nick_name（群主设置的真实群名）
            name = ""
            contact_row = self._sqlite_conn.execute(
                "SELECT nick_name, remark FROM contact WHERE username = ?",
                (room_id,),
            ).fetchone()
            if contact_row:
                name = contact_row["nick_name"] or contact_row["remark"] or ""

            # 没有群名时用前几个成员昵称拼凑
            if not name:
                member_names = self._sqlite_conn.execute(
                    """
                    SELECT c.nick_name
                    FROM chatroom_member cm
                    JOIN contact c ON c.id = cm.member_id
                    WHERE cm.room_id = ?
                    LIMIT 5
                    """,
                    (room_pk,),
                ).fetchall()
                names = [m["nick_name"] for m in member_names if m["nick_name"]]
                if names:
                    name = "、".join(names[:5])
                    if member_count > 5:
                        name += f"...({member_count}人)"

            rooms.append({
                "room_id": room_id,
                "members": [],
                "display_names": [],
                "owner": owner,
                "member_count": member_count,
                "name": name,
            })

        logger.debug(f"获取到 {len(rooms)} 个群聊 (V4 schema)")
        return rooms

    def close(self) -> None:
        """关闭数据库连接并清理临时文件。"""
        with self._lock:
            if self._sqlite_conn:
                try:
                    self._sqlite_conn.close()
                except Exception as exc:
                    logger.debug(f"关闭数据库连接异常: {exc}")
                self._sqlite_conn = None

            if self._decrypted_path and os.path.exists(self._decrypted_path):
                try:
                    os.unlink(self._decrypted_path)
                    logger.debug("临时解密文件已清理")
                except Exception as exc:
                    logger.debug(f"清理临时文件异常: {exc}")
                # 清理关联的 WAL/SHM/journal 文件
                for suffix in ("-wal", "-shm", "-journal"):
                    aux = self._decrypted_path + suffix
                    if os.path.exists(aux):
                        try:
                            os.unlink(aux)
                        except Exception:
                            pass
                self._decrypted_path = ""
            self._source_signature = None
            self._wal_signature = None
            self._last_empty_query_key = None

    def __del__(self) -> None:
        self.close()

    # --- 平台通用方法 ---

    def is_message_db(self) -> bool:
        """验证当前打开的数据库是否包含消息表（MSG 或 Msg_% 表）。"""
        if self._sqlite_conn is None:
            return False
        try:
            cursor = self._sqlite_conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND (name='MSG' OR name LIKE 'Msg_%')"
            )
            count = cursor.fetchone()[0]
            return count > 0
        except Exception:
            return False

    def _is_v4_contact_schema(self) -> bool:
        """检测是否为微信 4.x 联系人 schema（小写表名）。"""
        if self._sqlite_conn is None:
            return False
        try:
            cursor = self._sqlite_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contact'"
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    def is_contact_db(self) -> bool:
        """验证当前打开的数据库是否包含联系人表。"""
        if self._sqlite_conn is None:
            return False
        try:
            cursor = self._sqlite_conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name IN ('Contact', 'ChatRoom', 'contact', 'chat_room')"
            )
            count = cursor.fetchone()[0]
            return count >= 1
        except Exception:
            return False

    @classmethod
    def get_current_wxid(cls) -> str:
        """获取当前登录用户的 wxid。

        通过扫描微信数据目录中 wxid_ 开头的文件夹获取。
        """
        for expanded in cls._get_data_dirs():
            try:
                for entry in os.scandir(expanded):
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith("wxid_"):
                        return entry.name
                    if os.path.isdir(os.path.join(entry.path, "db_storage")):
                        return entry.name
                    if os.path.isdir(os.path.join(entry.path, "Msg")):
                        return entry.name
            except OSError:
                continue
        return ""

    @classmethod
    def find_database_files(cls, wxid: str = "") -> list[str]:
        """查找 Windows 上指定 wxid 的所有 .db 数据库文件。

        支持两种目录结构:
        - WeChat Files/<wxid>/Msg/...
        - xwechat_files/<wxid>/db_storage/<category>/<db>.db
        """
        db_files: list[str] = []

        for expanded in cls._get_data_dirs():
            try:
                for wxid_entry in os.scandir(expanded):
                    if not wxid_entry.is_dir():
                        continue
                    if wxid and wxid_entry.name != wxid:
                        continue

                    storage = os.path.join(wxid_entry.path, "db_storage")
                    if os.path.isdir(storage):
                        for root, _dirs, files in os.walk(storage):
                            for fname in files:
                                if fname.endswith(".db"):
                                    db_files.append(os.path.join(root, fname))
                        continue

                    for db_dir_name in ("Msg", "Contact"):
                        db_dir = os.path.join(wxid_entry.path, db_dir_name)
                        if not os.path.isdir(db_dir):
                            continue
                        for root, _dirs, files in os.walk(db_dir):
                            for fname in files:
                                if fname.endswith(".db"):
                                    db_files.append(os.path.join(root, fname))
            except OSError:
                continue

        return db_files

    @classmethod
    def _get_data_dirs(cls) -> list[str]:
        """Return discovered Windows WeChat data roots plus legacy fallbacks."""
        discovered = [item.path for item in find_wechat_data_dirs()]
        candidates = discovered + [
            os.path.expandvars(path) for path in cls.WINDOWS_DATA_DIRS
        ]

        data_dirs: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            if not path:
                continue
            norm = os.path.normpath(path)
            key = os.path.normcase(norm)
            if key in seen or not os.path.isdir(norm):
                continue
            seen.add(key)
            data_dirs.append(norm)
        return data_dirs

    @classmethod
    def cleanup_temp_files(cls, stale_seconds: int = 600) -> int:
        """清理过期的解密临时文件及其 WAL/SHM/journal 辅助文件。

        Returns:
            已删除的文件数量。
        """
        tmp_dir = tempfile.gettempdir()
        now = time.time()
        removed = 0
        try:
            for name in os.listdir(tmp_dir):
                if not name.startswith("weix_decrypted_"):
                    continue
                if not (
                    name.endswith(".db")
                    or name.endswith(".db-wal")
                    or name.endswith(".db-shm")
                    or name.endswith(".db-journal")
                ):
                    continue
                fpath = os.path.join(tmp_dir, name)
                try:
                    if now - os.path.getmtime(fpath) > stale_seconds:
                        os.unlink(fpath)
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        if removed:
            logger.info(f"已清理 {removed} 个临时解密文件")
        return removed

    def get_my_messages(
        self,
        limit: int = 5000,
        since_days: int = 90,
    ) -> list[dict]:
        """提取当前用户发出的所有文本消息（用于风格分析）。

        Windows 版 MSG 表中需要根据 talker 和 is_sender 字段判断。
        """
        if self._sqlite_conn is None:
            logger.error("数据库未打开")
            return []

        if self._has_msg_shard_tables():
            return self._get_my_v4_messages(limit=limit, since_days=since_days)

        since_ts = int(time.time()) - since_days * 86400

        try:
            cursor = self._sqlite_conn.execute(
                """
                SELECT msg_content, msg_create_time, msg_talker
                FROM MSG
                WHERE msg_create_time > ?
                AND msg_type = ?
                AND is_sender = 1
                ORDER BY msg_create_time DESC
                LIMIT ?
                """,
                (since_ts, MSG_TYPE_TEXT, limit),
            )

            messages: list[dict] = []
            for row in cursor:
                talker = row["msg_talker"] or ""
                is_group = "@chatroom" in str(talker)

                content = row["msg_content"] or ""
                if isinstance(content, bytes):
                    try:
                        content = content.decode("utf-8", errors="replace")
                    except Exception:
                        content = str(content)
                content = str(content).strip()
                if not content or len(content) < 2:
                    continue

                messages.append({
                    "content": content,
                    "create_time": row["msg_create_time"],
                    "room_id": str(talker) if is_group else "",
                    "is_group": is_group,
                })

            logger.info(f"提取当前用户消息: {len(messages)} 条")
            return messages

        except Exception as exc:
            logger.error(f"提取当前用户消息失败: {exc}")
            return []

    def _get_my_v4_messages(
        self,
        limit: int = 5000,
        since_days: int = 90,
    ) -> list[dict]:
        """提取 Windows Weixin 4.x 当前用户发出的文本消息。"""
        if self._sqlite_conn is None:
            return []

        if self._get_current_sender_id() is None:
            logger.warning("无法可靠识别当前账号，跳过本人消息风格提取")
            return []

        since_ts = int(time.time()) - since_days * 86400
        messages: list[dict] = []

        try:
            for table, username in self._get_v4_msg_tables():
                if len(messages) >= limit:
                    break
                try:
                    cursor = self._sqlite_conn.execute(
                        f'SELECT message_content, create_time, real_sender_id, '
                        f'status, origin_source, server_seq '
                        f'FROM "{table}" '
                        f'WHERE create_time > ? '
                        f'AND local_type = 1 '
                        f'ORDER BY create_time DESC '
                        f'LIMIT ?',
                        (since_ts, limit - len(messages)),
                    )
                except Exception:
                    continue

                is_group = "@chatroom" in username
                for row in cursor:
                    if not self._is_self_sent_v4_row(row):
                        continue
                    content = self._decode_message_content(row["message_content"]).strip()
                    if not content or len(content) < 2 or self._is_garbled(content):
                        continue
                    messages.append({
                        "content": content,
                        "create_time": row["create_time"],
                        "room_id": username if is_group else "",
                        "is_group": is_group,
                    })

            messages.sort(key=lambda item: item["create_time"])
            logger.info(f"提取 Windows 4.x 当前用户消息: {len(messages)} 条")
            return messages
        except Exception as exc:
            logger.error(f"提取 Windows 4.x 当前用户消息失败: {exc}")
            return []

    # --- 内部方法 ---

    def _has_msg_shard_tables(self) -> bool:
        if self._sqlite_conn is None:
            return False
        try:
            row = self._sqlite_conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'Msg_%' LIMIT 1"
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _get_v4_msg_tables(self) -> list[tuple[str, str]]:
        if self._sqlite_conn is None:
            return []
        if self._msg_table_cache is not None:
            return self._msg_table_cache

        hash_to_user: dict[str, str] = {}
        try:
            cursor = self._sqlite_conn.execute("SELECT user_name FROM Name2Id")
            for row in cursor:
                username = row["user_name"] or ""
                if username:
                    hash_to_user[hashlib.md5(username.encode()).hexdigest()] = username
        except Exception:
            pass

        self._msg_table_cache = []
        cursor = self._sqlite_conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'Msg_%'"
        )
        for row in cursor:
            table = row[0]
            username = hash_to_user.get(table[4:], "")
            self._msg_table_cache.append((table, username))
        logger.info(f"Windows 4.x 消息表缓存已构建: {len(self._msg_table_cache)} 个会话表")
        return self._msg_table_cache

    def _get_name2id_map(self) -> dict[int, str]:
        """获取 Windows 4.x Name2Id.rowid 到 user_name 的映射。"""
        if self._name2id_cache is not None:
            return self._name2id_cache

        self._name2id_cache = {}
        if self._sqlite_conn is None:
            return self._name2id_cache

        try:
            cursor = self._sqlite_conn.execute(
                "SELECT rowid, user_name FROM Name2Id WHERE user_name IS NOT NULL"
            )
            for row in cursor:
                user_name = str(row["user_name"] or "").strip()
                if user_name:
                    self._name2id_cache[int(row["rowid"])] = user_name
        except Exception as exc:
            logger.debug("读取 Name2Id 映射失败: %s", exc)

        return self._name2id_cache

    def _resolve_v4_group_sender(self, row, fallback: str) -> str:
        """优先按 real_sender_id 解析群成员，失败时再检查 source。"""
        try:
            real_sender_id = int(row["real_sender_id"] or 0)
        except (TypeError, ValueError, IndexError):
            real_sender_id = 0

        if real_sender_id:
            sender = self._get_name2id_map().get(real_sender_id, "")
            if sender:
                return sender

        return self._parse_group_sender(row["source"], fallback)

    def _is_self_sent_v4_row(self, row) -> bool:
        """识别当前账号自己发出的 Windows 4.x 消息。"""
        real_sender_id = row["real_sender_id"] or 0
        current_sender_id = self._get_current_sender_id()
        status = row["status"] or 0
        origin_source = row["origin_source"] or 0
        server_seq = row["server_seq"] or 0
        if status == 2 and origin_source == 1 and server_seq == 0:
            return True

        if current_sender_id is not None and real_sender_id:
            return real_sender_id == current_sender_id

        # real_sender_id 缺失或当前账号无法可靠推断时采用安全失败：
        # 宁可不回复，也不能把自己的已同步消息误判成对方消息。
        if not self._direction_warning_logged:
            logger.warning("消息方向证据不足，已安全阻止自动回复")
            self._direction_warning_logged = True
        return True

    def _get_current_sender_id(self) -> Optional[int]:
        """从 Name2Id 定位当前登录账号的 rowid。"""
        if self._current_sender_id is not None:
            return self._current_sender_id
        if self._sqlite_conn is None:
            return None

        current_wxid = self._normalize_current_wxid(self.get_current_wxid())
        if current_wxid:
            try:
                row = self._sqlite_conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 1",
                    (current_wxid,),
                ).fetchone()
                if row:
                    self._current_sender_id = int(row["rowid"])
                    logger.info(
                        "Windows 当前账号 sender_id 已通过账号名识别: %s",
                        self._current_sender_id,
                    )
                    return self._current_sender_id
            except Exception as exc:
                logger.debug("通过账号名识别 Windows sender_id 失败: %s", exc)

        inferred = self._infer_current_sender_id()
        if inferred is not None:
            self._current_sender_id = inferred
            logger.info(
                "Windows 当前账号 sender_id 已通过本机发送记录识别: %s",
                inferred,
            )
        return self._current_sender_id

    def _infer_current_sender_id(self) -> Optional[int]:
        """从跨会话本机发送特征与覆盖率推断当前账号 sender_id。"""
        if self._sqlite_conn is None:
            return None

        local_markers: Counter[int] = Counter()
        private_coverage: Counter[int] = Counter()
        private_table_count = 0

        for table, username in self._get_v4_msg_tables():
            try:
                marker_rows = self._sqlite_conn.execute(
                    f'SELECT real_sender_id, COUNT(*) AS count FROM "{table}" '
                    "WHERE real_sender_id > 0 AND status = 2 "
                    "AND origin_source = 1 AND server_seq = 0 "
                    "GROUP BY real_sender_id"
                ).fetchall()
                for marker in marker_rows:
                    local_markers[int(marker["real_sender_id"])] += int(
                        marker["count"]
                    )

                if username and "@chatroom" not in username:
                    private_table_count += 1
                    sender_rows = self._sqlite_conn.execute(
                        f'SELECT DISTINCT real_sender_id FROM "{table}" '
                        "WHERE real_sender_id > 0"
                    ).fetchall()
                    private_coverage.update(
                        int(sender["real_sender_id"]) for sender in sender_rows
                    )
            except Exception:
                continue

        marker_candidate = self._dominant_sender_candidate(
            local_markers,
            minimum_count=3,
            dominance_ratio=5,
        )
        # 仅靠“跨私聊会话覆盖率”时要求候选至少出现在一半已解析私聊表，
        # 且显著领先第二名。数据较少或证据接近时宁可不自动回复。
        coverage_candidate = self._dominant_sender_candidate(
            private_coverage,
            minimum_count=max(3, (private_table_count + 1) // 2),
            dominance_ratio=3,
        )

        if (
            marker_candidate is not None
            and coverage_candidate is not None
            and marker_candidate != coverage_candidate
        ):
            logger.warning(
                "Windows 当前账号 sender_id 证据冲突，已禁用方向判定: "
                "local_marker=%s, private_coverage=%s",
                marker_candidate,
                coverage_candidate,
            )
            return None

        return marker_candidate or coverage_candidate

    @staticmethod
    def _dominant_sender_candidate(
        counts: Counter[int],
        *,
        minimum_count: int,
        dominance_ratio: int,
    ) -> Optional[int]:
        ranked = counts.most_common(2)
        if not ranked or ranked[0][1] < minimum_count:
            return None
        top_id, top_count = ranked[0]
        second_count = ranked[1][1] if len(ranked) > 1 else 0
        if second_count and top_count < second_count * dominance_ratio:
            return None
        return int(top_id)

    @staticmethod
    def _normalize_current_wxid(wxid: str) -> str:
        wxid = str(wxid or "")
        if "_6c" in wxid:
            wxid = wxid.split("_6c", 1)[0]
        return wxid

    @staticmethod
    def _decode_message_content(content) -> str:
        if content is None:
            return ""
        if isinstance(content, bytes):
            try:
                return content.decode("utf-8", errors="replace")
            except Exception:
                return str(content)
        return str(content)

    @staticmethod
    def _is_garbled(text: str) -> bool:
        if not text:
            return True
        bad = 0
        for ch in text:
            code = ord(ch)
            if code == 0xFFFD or (code < 0x20 and code not in (0x09, 0x0A, 0x0D)):
                bad += 1
        return bad / len(text) > 0.3

    @staticmethod
    def _parse_group_sender(source_blob, fallback: str) -> str:
        if not source_blob:
            return fallback
        try:
            if isinstance(source_blob, bytes):
                text = source_blob.decode("utf-8", errors="replace")
            else:
                text = str(source_blob)
            match = re.search(r"(?:wxid|gh)_[a-z0-9_-]+", text, re.IGNORECASE)
            if match:
                return match.group(0)
            match = re.search(r"\d+@openim", text)
            if match:
                return match.group(0)
        except Exception:
            pass
        return fallback

    @staticmethod
    def _parse_group_sender_from_content(content: str) -> str:
        """从旧版群消息正文开头解析发送者 ID。"""
        text = str(content or "")
        match = re.match(
            r"^\s*((?:wxid|gh)_[a-z0-9_-]+|\d+@openim)\s*:\s*(?:\r?\n|$)",
            text,
            re.IGNORECASE,
        )
        return match.group(1) if match else ""

    @classmethod
    def _clean_group_content(cls, content: str, sender: str = "") -> str:
        """仅移除确认为发送者 ID 的群消息正文前缀。"""
        text = str(content or "")
        candidates = [str(sender or "").strip()]
        parsed = cls._parse_group_sender_from_content(text)
        if parsed:
            candidates.append(parsed)

        for candidate in candidates:
            if not candidate:
                continue
            prefix = re.compile(
                rf"^\s*{re.escape(candidate)}\s*:\s*(?:\r?\n|$)",
                re.IGNORECASE,
            )
            cleaned, count = prefix.subn("", text, count=1)
            if count:
                return cleaned.strip()

        return text.strip()

    @staticmethod
    def _derive_mac_key(enc_key: bytes, salt: bytes, hash_name: str = "sha512") -> bytes:
        """派生 SQLCipher 4 HMAC 校验密钥。"""
        mac_salt = bytes(b ^ 0x3A for b in salt)
        return hashlib.pbkdf2_hmac(hash_name, enc_key, mac_salt, 2, dklen=KEY_SIZE)

    @staticmethod
    def _looks_like_decrypted_page1(decrypted: bytes) -> bool:
        """识别 SQLCipher page 1 解密后的常见明文形态。"""
        return (
            decrypted[:16] == SQLITE_HEADER
            or decrypted[:2] == b"\x10\x00"
        )

    def _rebuild_page1(self, decrypted: bytes) -> bytes:
        """把 page 1 解密片段还原成普通 SQLite page。"""
        if decrypted[:16] == SQLITE_HEADER:
            padding = b"\x00" * (PAGE_SIZE - len(decrypted))
            return decrypted + padding
        return SQLITE_HEADER + decrypted + b"\x00" * self._reserve_size

    @staticmethod
    def _file_signature(path: str) -> Optional[tuple[int, int]]:
        """返回文件大小和纳秒修改时间；文件不存在时返回 None。"""
        if not path:
            return None
        try:
            stat = os.stat(path)
            return stat.st_size, stat.st_mtime_ns
        except FileNotFoundError:
            return None

    def _wal_path(self) -> str:
        return f"{self._db_path}-wal" if self._db_path else ""

    @staticmethod
    def _open_decrypted_connection(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            conn.close()
            raise
        return conn

    def _invalidate_runtime_caches(self) -> None:
        self._msg_table_cache = None
        self._name2id_cache = None
        self._current_sender_id = None
        self._direction_warning_logged = False
        self._last_empty_query_key = None

    def _build_full_snapshot(
        self,
    ) -> tuple[str, sqlite3.Connection, tuple[int, int], Optional[tuple[int, int]]]:
        """生成稳定的完整快照，并尽量合并当前 WAL 已提交页面。"""
        decrypted_path = ""
        source_signature: Optional[tuple[int, int]] = None

        for attempt in range(2):
            before = self._file_signature(self._db_path)
            if before is None:
                raise FileNotFoundError(self._db_path)
            candidate = self._decrypt_to_temp()
            after = self._file_signature(self._db_path)
            if before == after:
                decrypted_path = candidate
                source_signature = after
                break
            try:
                os.unlink(candidate)
            except OSError:
                pass
            if attempt == 0:
                logger.warning(
                    "完整解密期间微信主库发生 checkpoint，准备重新读取一次"
                )

        if not decrypted_path or source_signature is None:
            raise RuntimeError("微信主库持续变化，暂时无法生成一致快照")

        wal_signature: Optional[tuple[int, int]] = None
        try:
            wal_signature, wal_data = self._read_wal_snapshot()
            pages, frame_count, db_size = self._collect_committed_wal_pages(wal_data)
            if pages:
                self._write_decrypted_pages(decrypted_path, pages, db_size)
                logger.info(
                    "完整快照已合并 WAL: frames=%d, pages=%d",
                    frame_count,
                    len(pages),
                )
        except Exception as exc:
            # 完整主库本身仍可安全读取。保留 wal_signature=None，下一轮会重试。
            wal_signature = None
            logger.warning("首次 WAL 合并未完成，将在下一轮重试: %s", exc)

        try:
            conn = self._open_decrypted_connection(decrypted_path)
        except Exception:
            try:
                os.unlink(decrypted_path)
            except OSError:
                pass
            raise
        return decrypted_path, conn, source_signature, wal_signature

    def _replace_with_full_snapshot_locked(self, reason: str = "checkpoint") -> None:
        started = time.perf_counter()
        old_path = self._decrypted_path
        old_conn = self._sqlite_conn
        new_path, new_conn, source_signature, wal_signature = (
            self._build_full_snapshot()
        )

        self._decrypted_path = new_path
        self._sqlite_conn = new_conn
        self._source_signature = source_signature
        self._wal_signature = wal_signature
        self._invalidate_runtime_caches()

        if old_conn:
            try:
                old_conn.close()
            except Exception:
                pass
        if old_path:
            try:
                os.unlink(old_path)
            except OSError:
                pass
        logger.info(
            "完整快照重建完成: reason=%s, elapsed=%.3fs",
            reason,
            time.perf_counter() - started,
        )

    def _read_wal_snapshot(self) -> tuple[Optional[tuple[int, int]], bytes]:
        """读取一次稳定的 WAL 内容；微信写入期间发生变化则留到下一轮。"""
        wal_path = self._wal_path()
        before = self._file_signature(wal_path)
        if before is None:
            return None, b""
        if before[0] <= WAL_HEADER_SIZE:
            return before, b""

        with open(wal_path, "rb") as wal:
            data = wal.read(before[0])
        after = self._file_signature(wal_path)
        if before != after or len(data) != before[0]:
            raise RuntimeError("WAL 正在写入，本轮跳过以避免读取半帧")
        return after, data

    def _collect_committed_wal_pages(
        self,
        wal_data: bytes,
    ) -> tuple[dict[int, bytes], int, int]:
        """解析同一 WAL 世代中最后一次提交前的加密页面。"""
        if len(wal_data) <= WAL_HEADER_SIZE:
            return {}, 0, 0
        if not self._aes_key or not self._hmac_key:
            raise RuntimeError("WAL 解密密钥尚未派生")

        magic, _version, wal_page_size = struct.unpack(">III", wal_data[:12])
        if magic not in (0x377F0682, 0x377F0683):
            raise RuntimeError("无法识别 WAL 文件头")
        if wal_page_size == 1:
            wal_page_size = 65536
        if wal_page_size != PAGE_SIZE:
            raise RuntimeError(f"WAL 页面大小不受支持: {wal_page_size}")

        wal_salt = wal_data[16:24]
        total_frames = (len(wal_data) - WAL_HEADER_SIZE) // WAL_FRAME_SIZE
        frames: list[tuple[int, int, int, bytes]] = []
        last_commit_index = -1
        committed_db_size = 0

        for index in range(total_frames):
            offset = WAL_HEADER_SIZE + index * WAL_FRAME_SIZE
            header = wal_data[offset:offset + WAL_FRAME_HEADER_SIZE]
            page = wal_data[
                offset + WAL_FRAME_HEADER_SIZE:offset + WAL_FRAME_SIZE
            ]
            if len(header) != WAL_FRAME_HEADER_SIZE or len(page) != PAGE_SIZE:
                break
            page_number, commit_size = struct.unpack(">II", header[:8])
            if header[8:16] != wal_salt or page_number <= 0:
                continue
            frames.append((index, page_number, commit_size, page))
            if commit_size:
                last_commit_index = index
                committed_db_size = commit_size

        if last_commit_index < 0:
            return {}, 0, 0

        pages: dict[int, bytes] = {}
        applied_frames = 0
        for index, page_number, _commit_size, page in frames:
            if index > last_commit_index:
                continue
            if committed_db_size and page_number > committed_db_size:
                continue
            if not self._verify_page_hmac(
                page,
                self._hmac_key,
                self._reserve_size,
                self._hmac_hash,
                page_number=page_number,
            ):
                raise RuntimeError(
                    f"WAL 第 {index + 1} 帧 HMAC 校验失败，本轮不合并"
                )
            pages[page_number] = self._decrypt_encrypted_page(page, page_number)
            applied_frames += 1

        return pages, applied_frames, committed_db_size

    def _decrypt_encrypted_page(self, page: bytes, page_number: int) -> bytes:
        if not self._aes_key or len(page) != PAGE_SIZE:
            raise RuntimeError("无法解密不完整页面")
        reserved = page[PAGE_SIZE - self._reserve_size:PAGE_SIZE]
        iv = reserved[:IV_SIZE]
        if page_number == 1:
            encrypted = page[SALT_SIZE:PAGE_SIZE - self._reserve_size]
        else:
            encrypted = page[:PAGE_SIZE - self._reserve_size]
        decrypted = AES.new(self._aes_key, AES.MODE_CBC, iv=iv).decrypt(encrypted)
        if page_number == 1:
            return self._rebuild_page1(decrypted)
        return decrypted + b"\x00" * self._reserve_size

    @staticmethod
    def _restore_decrypted_pages(
        path: str,
        original_size: int,
        backups: dict[int, bytes],
    ) -> None:
        with open(path, "r+b") as output:
            for offset, original in sorted(backups.items()):
                output.seek(offset)
                output.write(original)
            output.truncate(original_size)

    def _write_decrypted_pages(
        self,
        path: str,
        pages: dict[int, bytes],
        committed_db_size: int,
    ) -> tuple[int, dict[int, bytes]]:
        """覆盖解密页面并返回回滚信息。"""
        original_size = os.path.getsize(path)
        backups: dict[int, bytes] = {}

        try:
            with open(path, "r+b") as output:
                for page_number, page in sorted(pages.items()):
                    offset = (page_number - 1) * PAGE_SIZE
                    if offset not in backups:
                        output.seek(offset)
                        backups[offset] = output.read(PAGE_SIZE)
                    output.seek(offset)
                    output.write(page)

                # WAL 可能新增页面但未携带 page 1，补齐 SQLite 头中的页数。
                output.seek(0)
                page1 = output.read(PAGE_SIZE)
                if len(page1) != PAGE_SIZE or page1[:16] != SQLITE_HEADER:
                    raise RuntimeError("WAL 合并后 page 1 无效")
                current_pages = struct.unpack(">I", page1[28:32])[0]
                current_size = max(
                    original_size,
                    output.seek(0, os.SEEK_END),
                )
                file_pages = (current_size + PAGE_SIZE - 1) // PAGE_SIZE
                max_written = max(pages, default=0)
                new_pages = max(
                    current_pages,
                    committed_db_size,
                    max_written,
                    file_pages,
                )
                if new_pages != current_pages:
                    if 0 not in backups:
                        output.seek(0)
                        backups[0] = output.read(PAGE_SIZE)
                    page1 = page1[:28] + struct.pack(">I", new_pages) + page1[32:]
                    output.seek(0)
                    output.write(page1)
                output.flush()
            return original_size, backups
        except Exception:
            if backups:
                self._restore_decrypted_pages(path, original_size, backups)
            raise

    def _refresh_from_wal_locked(self) -> None:
        started = time.perf_counter()
        wal_signature, wal_data = self._read_wal_snapshot()
        if self._file_signature(self._db_path) != self._source_signature:
            self._replace_with_full_snapshot_locked()
            return
        pages, frame_count, db_size = self._collect_committed_wal_pages(wal_data)
        if not pages:
            self._wal_signature = wal_signature
            return

        if self._file_signature(self._db_path) != self._source_signature:
            self._replace_with_full_snapshot_locked()
            return

        old_conn = self._sqlite_conn
        if old_conn:
            old_conn.close()
        self._sqlite_conn = None

        original_size = 0
        backups: dict[int, bytes] = {}
        try:
            original_size, backups = self._write_decrypted_pages(
                self._decrypted_path,
                pages,
                db_size,
            )
            self._sqlite_conn = self._open_decrypted_connection(
                self._decrypted_path
            )
        except Exception:
            if backups:
                self._restore_decrypted_pages(
                    self._decrypted_path,
                    original_size,
                    backups,
                )
            self._sqlite_conn = self._open_decrypted_connection(
                self._decrypted_path
            )
            raise

        if self._file_signature(self._db_path) != self._source_signature:
            if self._sqlite_conn:
                self._sqlite_conn.close()
            self._restore_decrypted_pages(
                self._decrypted_path,
                original_size,
                backups,
            )
            self._sqlite_conn = self._open_decrypted_connection(
                self._decrypted_path
            )
            self._replace_with_full_snapshot_locked()
            return
        self._wal_signature = wal_signature
        self._invalidate_runtime_caches()
        logger.info(
            "WAL 增量刷新完成: frames=%d, pages=%d, elapsed=%.3fs",
            frame_count,
            len(pages),
            time.perf_counter() - started,
        )

    def _derive_key(self) -> bool:
        """从原始密钥派生 AES 和 HMAC 密钥。

        新版 WeChat 4.x 内存中的 key 通常已是 AES-256 页面密钥；
        旧格式则可能还需要 PBKDF2 派生。这里按 direct AES -> PBKDF2
        的顺序尝试，确保和提取器验证逻辑一致。
        """
        if not self._key:
            logger.error("缺少原始密钥")
            return False

        try:
            with open(self._db_path, "rb") as f:
                page1 = f.read(PAGE_SIZE)

            if len(page1) < PAGE_SIZE:
                logger.error("无法读取完整的 page 1")
                return False

            salt = page1[:16]  # SQLCipher salt
            direct_modes = [
                (80, "sha512"),
                (48, "sha1"),
            ]
            for reserve_size, hash_name in direct_modes:
                try:
                    reserved = page1[PAGE_SIZE - reserve_size:PAGE_SIZE]
                    iv = reserved[:IV_SIZE]
                    encrypted = page1[SALT_SIZE:PAGE_SIZE - reserve_size]
                    cipher = AES.new(self._key, AES.MODE_CBC, iv=iv)
                    decrypted = cipher.decrypt(encrypted)
                    hmac_ok = self._verify_page_hmac(
                        page1,
                        self._derive_mac_key(self._key, salt, hash_name),
                        reserve_size,
                        hash_name,
                    )
                    if self._looks_like_decrypted_page1(decrypted) and hmac_ok:
                        self._aes_key = self._key
                        self._hmac_key = self._derive_mac_key(self._key, salt, hash_name)
                        self._iterations = 0
                        self._reserve_size = reserve_size
                        self._hmac_hash = hash_name
                        self._key_mode = "direct"
                        logger.info(
                            f"密钥验证成功 (direct AES, reserve={reserve_size}, hmac={hash_name})"
                        )
                        return True
                except Exception as exc:
                    logger.debug(f"direct AES 验证失败: {exc}")

            # 尝试多种迭代次数
            for iterations, hash_name, reserve_size in [
                (256000, "sha512", 80),
                (64000, "sha1", 48),
                (4000, "sha1", 48),
            ]:
                try:
                    hmac_module = SHA512 if hash_name == "sha512" else __import__(
                        "Crypto.Hash.SHA1", fromlist=["SHA1"]
                    )
                    derived = PBKDF2(
                        self._key,
                        salt,
                        dkLen=KEY_SIZE,
                        count=iterations,
                        hmac_hash_module=hmac_module,
                    )
                    mac_key = PBKDF2(
                        derived,
                        bytes(b ^ 0x3A for b in salt),
                        dkLen=KEY_SIZE,
                        count=2,
                        hmac_hash_module=hmac_module,
                    )
                    # 验证: 使用派生密钥解密 page 1
                    reserved = page1[PAGE_SIZE - reserve_size:PAGE_SIZE]
                    iv = reserved[:IV_SIZE]
                    encrypted = page1[SALT_SIZE:PAGE_SIZE - reserve_size]
                    aes_key = derived
                    cipher = AES.new(aes_key, AES.MODE_CBC, iv=iv)
                    decrypted = cipher.decrypt(encrypted)

                    if self._looks_like_decrypted_page1(decrypted) and self._verify_page_hmac(
                        page1, mac_key, reserve_size, hash_name
                    ):
                        self._aes_key = aes_key
                        self._hmac_key = mac_key
                        self._iterations = iterations
                        self._reserve_size = reserve_size
                        self._hmac_hash = hash_name
                        self._key_mode = "pbkdf2"
                        logger.info(
                            f"密钥派生成功 (iterations={iterations}, hmac={hash_name}, reserve={reserve_size})"
                        )
                        return True

                except Exception as exc:
                    logger.debug(
                        f"迭代 {iterations} 密钥派生失败: {exc}"
                    )
                    continue

            logger.error("所有迭代次数的密钥派生均失败")
            return False

        except Exception as exc:
            logger.error(f"密钥派生失败: {exc}")
            return False

    @staticmethod
    def _verify_page_hmac(
        page: bytes,
        mac_key: bytes,
        reserve_size: int,
        hash_name: str,
        page_number: int = 1,
    ) -> bool:
        if len(page) != PAGE_SIZE or page_number <= 0:
            return False
        data_start = SALT_SIZE if page_number == 1 else 0
        if hash_name == "sha512":
            digestmod = hashlib.sha512
            stored = page[PAGE_SIZE - reserve_size + IV_SIZE:PAGE_SIZE]
            data = page[data_start:PAGE_SIZE - reserve_size + IV_SIZE]
        else:
            digestmod = hashlib.sha1
            first = page[data_start:PAGE_SIZE]
            stored = first[-32:-12]
            data = first[:-32]

        calculated = hmac_mod.new(mac_key, data, digestmod)
        calculated.update(struct.pack("<I", page_number))
        return hmac_mod.compare_digest(calculated.digest(), stored)

    def _decrypt_to_temp(self) -> str:
        """将整个加密数据库解密到临时文件。

        Returns:
            临时文件路径。
        """
        if not self._aes_key:
            raise RuntimeError("AES 密钥未派生")

        file_size = os.path.getsize(self._db_path)
        total_pages = (file_size + PAGE_SIZE - 1) // PAGE_SIZE

        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False, prefix="weix_decrypted_"
        )
        tmp_path = tmp.name

        try:
            with open(self._db_path, "rb") as src:
                for page_num in range(total_pages):
                    page_data = src.read(PAGE_SIZE)
                    if len(page_data) < PAGE_SIZE:
                        # 最后一页可能不足 4096 字节，直接写入
                        tmp.write(page_data)
                        continue
                    tmp.write(
                        self._decrypt_encrypted_page(page_data, page_num + 1)
                    )

                    if page_num % 1000 == 0 and page_num > 0:
                        logger.debug(
                            f"解密进度: {page_num}/{total_pages} 页"
                        )

            logger.info(
                f"数据库解密完成 ({total_pages} 页) -> {tmp_path}"
            )
            tmp.close()
            return tmp_path

        except Exception:
            # 出错时清理临时文件
            tmp.close()
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    # --- 辅助查询方法 ---

    def get_table_info(self, table_name: str) -> list[dict]:
        """获取表结构信息 (PRAGMA table_info)。"""
        if self._sqlite_conn is None:
            return []

        try:
            cursor = self._sqlite_conn.execute(
                f"PRAGMA table_info({table_name})"
            )
            return [dict(row) for row in cursor]
        except Exception as exc:
            logger.error(f"获取表 {table_name} 结构失败: {exc}")
            return []

    def get_messages_by_type(
        self, msg_type: int, limit: int = 100
    ) -> list[WeChatMessage]:
        """按类型查询消息。

        Args:
            msg_type: 消息类型。
            limit: 最大条数。

        Returns:
            WeChatMessage 列表。
        """
        if self._sqlite_conn is None:
            return []

        if self._has_msg_shard_tables():
            return self._get_v4_messages_by_type(msg_type, limit)

        try:
            cursor = self._sqlite_conn.execute(
                """
                SELECT msg_id, msg_content, msg_type, msg_talker,
                       msg_create_time, chatroom_id
                FROM MSG
                WHERE msg_type = ?
                ORDER BY msg_create_time DESC
                LIMIT ?
                """,
                (msg_type, limit),
            )

            messages: list[WeChatMessage] = []
            for row in cursor:
                messages.append(
                    WeChatMessage(
                        msg_id=str(row["msg_id"] or ""),
                        msg_type=row["msg_type"] or 0,
                        content=str(row["msg_content"] or ""),
                        sender=str(row["msg_talker"] or ""),
                        room_id=str(row["chatroom_id"] or ""),
                        create_time=datetime.fromtimestamp(
                            (row["msg_create_time"] or 0) / 1000.0
                        ),
                    )
                )
            return messages

        except Exception as exc:
            logger.error(f"按类型查询消息失败: {exc}")
            return []

    def get_messages_by_talker(
        self, talker: str, limit: int = 100
    ) -> list[WeChatMessage]:
        """查询指定会话的消息。

        Args:
            talker: 会话 wxid。
            limit: 最大条数。

        Returns:
            WeChatMessage 列表。
        """
        if self._sqlite_conn is None:
            return []

        if self._has_msg_shard_tables():
            return self._get_v4_messages_by_talker(talker, limit)

        try:
            cursor = self._sqlite_conn.execute(
                """
                SELECT msg_id, msg_content, msg_type, msg_talker,
                       msg_create_time, chatroom_id
                FROM MSG
                WHERE msg_talker = ?
                ORDER BY msg_create_time DESC
                LIMIT ?
                """,
                (talker, limit),
            )

            messages: list[WeChatMessage] = []
            for row in cursor:
                messages.append(
                    WeChatMessage(
                        msg_id=str(row["msg_id"] or ""),
                        msg_type=row["msg_type"] or 0,
                        content=str(row["msg_content"] or ""),
                        sender=str(row["msg_talker"] or ""),
                        create_time=datetime.fromtimestamp(
                            (row["msg_create_time"] or 0) / 1000.0
                        ),
                    )
                )
            return messages

        except Exception as exc:
            logger.error(f"查询会话 {talker} 消息失败: {exc}")
            return []

    def _get_v4_messages_by_type(
        self,
        msg_type: int,
        limit: int = 100,
    ) -> list[WeChatMessage]:
        messages: list[WeChatMessage] = []
        try:
            for table, username in self._get_v4_msg_tables():
                if len(messages) >= limit:
                    break
                cursor = self._sqlite_conn.execute(  # type: ignore[union-attr]
                    f'SELECT local_id, create_time, real_sender_id, '
                    f'message_content, local_type, source, '
                    f'status, origin_source, server_seq '
                    f'FROM "{table}" '
                    f'WHERE local_type = ? '
                    f'ORDER BY local_id DESC '
                    f'LIMIT ?',
                    (msg_type, limit - len(messages)),
                )
                is_group = "@chatroom" in username
                for row in cursor:
                    content = self._decode_message_content(row["message_content"])
                    is_self = self._is_self_sent_v4_row(row)
                    sender = username
                    if is_group:
                        sender = self._resolve_v4_group_sender(row, username)
                        content = self._clean_group_content(content, sender)
                    messages.append(
                        WeChatMessage(
                            msg_id=f"{table}:{row['local_id']}",
                            msg_type=row["local_type"] or 0,
                            content=content,
                            sender=sender,
                            room_id=username if is_group else "",
                            create_time=datetime.fromtimestamp(row["create_time"] or 0),
                            is_group=is_group,
                            is_self=is_self,
                            at_list=[],
                        )
                    )
            return messages
        except Exception as exc:
            logger.error(f"按类型查询 Windows 4.x 消息失败: {exc}")
            return []

    def _get_v4_messages_by_talker(
        self,
        talker: str,
        limit: int = 100,
    ) -> list[WeChatMessage]:
        table = f"Msg_{hashlib.md5(talker.encode()).hexdigest()}"
        messages: list[WeChatMessage] = []
        try:
            exists = self._sqlite_conn.execute(  # type: ignore[union-attr]
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                return []
            cursor = self._sqlite_conn.execute(  # type: ignore[union-attr]
                f'SELECT local_id, create_time, real_sender_id, '
                f'message_content, local_type, source, '
                f'status, origin_source, server_seq '
                f'FROM "{table}" '
                f'ORDER BY local_id DESC '
                f'LIMIT ?',
                (limit,),
            )
            is_group = "@chatroom" in talker
            for row in cursor:
                content = self._decode_message_content(row["message_content"])
                is_self = self._is_self_sent_v4_row(row)
                sender = talker
                if is_group:
                    sender = self._resolve_v4_group_sender(row, talker)
                    content = self._clean_group_content(content, sender)
                messages.append(
                    WeChatMessage(
                        msg_id=f"{table}:{row['local_id']}",
                        msg_type=row["local_type"] or 0,
                        content=content,
                        sender=sender,
                        room_id=talker if is_group else "",
                        create_time=datetime.fromtimestamp(row["create_time"] or 0),
                        is_group=is_group,
                        is_self=is_self,
                        at_list=[],
                    )
                )
            return messages
        except Exception as exc:
            logger.error(f"查询 Windows 4.x 会话 {talker} 消息失败: {exc}")
            return []
