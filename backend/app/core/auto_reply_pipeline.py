"""自动回复流水线：消息监控 → 规则匹配 → 回复发送。

串联 MessageMonitor、RuleEngine、WorkflowEngine 和 MacOSSender，
实现微信消息的实时自动回复。
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, time as datetime_time
from typing import Optional

from app.config import get_config
from app.core.message_monitor import MessageMonitor
from app.core.platform import Platform
from app.utils.paths import get_data_dir

logger = logging.getLogger(__name__)


def _normalize_db_key_path(path: str) -> str:
    return path.replace("\\", "/").lower()


def _key_matches_db_path(key_path: str, full_path: str) -> bool:
    normalized_key = _normalize_db_key_path(key_path)
    normalized_full = _normalize_db_key_path(full_path)
    basename = os.path.basename(full_path)
    if "/" in normalized_key:
        return normalized_full.endswith(normalized_key)
    return os.path.normcase(key_path) == os.path.normcase(basename)


class AutoReplyPipeline:
    """自动回复流水线。

    启动后后台轮询微信消息数据库，对符合条件的新消息执行规则匹配
    并自动发送回复。

    使用方式:
        pipeline = AutoReplyPipeline(session_factory)
        await pipeline.start()
        # ... 服务运行中 ...
        await pipeline.stop()
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._monitor: Optional[MessageMonitor] = None
        self._sender = None
        self._rule_engine = None
        self._workflow_engine = None
        self._ai_agent = None  # WeixAgent 实例（延迟初始化）
        self._name_map: dict[str, str] = {}  # wxid -> 显示名
        self._ambiguous_display_names: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 消息防抖缓冲: sender_key -> [messages]
        self._buffer: dict[str, list] = {}
        self._buffer_timers: dict[str, asyncio.Task] = {}
        self._debounce_seconds = 20
        # 专属群规则首条命中立即回复；同一规则在短窗口内只触发一次，
        # 避免微信数据库重复读到同一批消息时造成连发。
        self._group_rule_dedupe_seconds = 20
        self._group_rule_last_trigger_at: dict[tuple[str, str, str], float] = {}
        self._recent_chat_context: dict[str, list[str]] = {}
        self._recent_context_limit = 12
        platform = Platform.get()
        if platform.is_macos:
            sender_cfg = get_config().macos_sender if hasattr(get_config(), "macos_sender") else {}
            self._park_after_send = sender_cfg.get("park_after_send", True)
            self._parking_receiver = sender_cfg.get("parking_receiver", "小号")
        else:
            # WindowsSender 在发送成功并通过数据库回读校验后自行完成停靠。
            # 流水线不能再次 open_chat，否则每次回复会重复搜索停靠联系人。
            self._park_after_send = False
            self._parking_receiver = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动自动回复流水线。"""
        if self._running:
            logger.warning("流水线已在运行")
            return

        platform = Platform.get()
        self._sender = platform.sender
        # 私有发送器用于停靠操作（macOS 是 PrivateChatSender，Windows 兼容处理）
        if platform.is_macos:
            from app.core.sender_macos import PrivateChatSender
            self._private_sender = PrivateChatSender()
        else:
            self._private_sender = platform.sender

        # 1. 加载密钥
        keys = self._load_keys(platform)
        if not keys:
            logger.warning("未找到数据库密钥，跳过流水线启动")
            return False

        # 2. 打开消息数据库 (用于监控)
        msg_reader = self._open_message_db(platform, keys)
        if msg_reader is None:
            logger.warning("无法打开消息数据库，跳过流水线启动")
            return False

        # 3. 构建名称映射 (wxid -> 显示名，用于 AppleScript 搜索)
        self._name_map = self._build_name_map(platform, keys)
        self._ambiguous_display_names = self._find_ambiguous_display_names(
            self._name_map
        )
        if self._ambiguous_display_names:
            logger.warning(
                "检测到 %d 个联系人/群聊重名；相关会话将拒绝自动发送",
                len(self._ambiguous_display_names),
            )

        # 4. 仅回填今天的本地统计，不进入自动回复队列。
        await self._backfill_today_messages(msg_reader)

        # 5. 启动消息监控。自动发送默认不回放启动前的旧消息，避免重启后
        # 对同一条消息重复回复；如只做观察，可在配置中显式增加回看时间。
        self._monitor = MessageMonitor(msg_reader)
        lookback_seconds = self._monitor_lookback_seconds()
        await self._monitor.start(lookback_seconds=lookback_seconds)
        logger.info("消息监控启动回看窗口: %.1fs", lookback_seconds)
        if hasattr(self._sender, "set_confirmation_source"):
            self._sender.set_confirmation_source(self._monitor)
        if platform.is_windows and hasattr(self._sender, "prewarm_uia"):
            diagnosis = await self._sender.prewarm_uia()
            if diagnosis.get("available"):
                logger.info("Windows UIA 已预热完成")
            else:
                logger.warning(
                    "Windows UIA 尚不可用，自动发送将保持静默: %s",
                    diagnosis.get("reason", "未知原因"),
                )

        # 6. 加载规则引擎 (先从 YAML 同步到 DB)
        from app.workflow.rule_engine import RuleEngine

        await self._seed_rules_from_yaml()
        self._rule_engine = RuleEngine(session_factory=self._session_factory)
        await self._rule_engine.load_rules()

        # 7. 加载工作流引擎 (支持 legacy / langgraph 切换)
        wf_engine_type = get_config().workflow_engine

        if wf_engine_type == "langgraph":
            from app.workflow.langgraph_engine import LangGraphWorkflowEngine
            self._workflow_engine = LangGraphWorkflowEngine(
                session_factory=self._session_factory
            )
            logger.info("使用 LangGraph 工作流引擎")
        else:
            from app.workflow.engine import WorkflowEngine
            self._workflow_engine = WorkflowEngine(
                session_factory=self._session_factory
            )
            logger.info("使用 Legacy 工作流引擎")

        await self._workflow_engine.load_workflows()

        # 8. 启动后台处理循环
        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info("自动回复流水线已启动")
        return True

    async def stop(self) -> None:
        """停止自动回复流水线。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 清理防抖缓冲
        for timer in self._buffer_timers.values():
            timer.cancel()
        self._buffer_timers.clear()
        self._buffer.clear()
        self._group_rule_last_trigger_at.clear()
        if self._monitor:
            await self._monitor.stop()
        if self._sender and hasattr(self._sender, "set_confirmation_source"):
            self._sender.set_confirmation_source(None)
        logger.info("自动回复流水线已停止")

    # ------------------------------------------------------------------
    # Internal: 初始化
    # ------------------------------------------------------------------

    @staticmethod
    def _monitor_lookback_seconds() -> float:
        """Read a non-negative startup lookback; fail closed to no replay."""
        monitor_cfg = getattr(get_config(), "monitor", {}) or {}
        try:
            return max(0.0, float(monitor_cfg.get("lookback_seconds", 0.0)))
        except (TypeError, ValueError):
            logger.warning("monitor.lookback_seconds 配置无效，已按 0 秒处理")
            return 0.0

    @staticmethod
    def _load_keys(platform) -> dict[str, str]:
        """加载数据库密钥。"""
        extractor = platform.key_extractor
        if hasattr(extractor, "load_keys"):
            keys = extractor.load_keys()
        else:
            keys = getattr(extractor, "_keys", {})

        if not keys:
            import json

            cache = get_data_dir() / "all_keys.json"
            if cache.exists():
                with open(cache) as f:
                    keys = json.load(f)
        return keys

    @staticmethod
    def _open_message_db(platform, keys: dict[str, str]):
        """打开消息数据库并返回 reader。

        按优先级收集候选文件（message_0.db > MSG.db），逐个尝试打开。
        Windows 不允许回退到 biz_message_0.db 等同样含 Msg_% 表的业务库。
        """
        reader = platform.db_reader

        all_dbs: list[str] = []
        if hasattr(reader, "find_database_files"):
            all_dbs = reader.find_database_files()

        # 诊断：列出所有文件中的 message 相关 DB
        msg_related = [f for f in all_dbs if "message" in os.path.basename(f).lower()]
        logger.info(
            f"找到 {len(all_dbs)} 个 DB 文件，"
            f"其中 message 相关: {[os.path.basename(f) for f in msg_related]}"
        )
        logger.info(f"可用密钥: {list(keys.keys())}")

        # 按优先级构建候选列表: message_0.db > MSG.db。
        # macOS 保留旧版其他消息库兜底；Windows 4.x 必须精确选库。
        candidates: list[tuple[str, str]] = []  # (path, hex_key)
        legacy_candidates: list[tuple[str, str]] = []
        other_fallback: list[tuple[str, str]] = []

        for full_path in all_dbs:
            basename = os.path.basename(full_path)
            basename_lower = basename.lower()
            for key_path, hex_key in keys.items():
                key_name = os.path.basename(key_path)
                key_name_lower = key_name.lower()
                if not _key_matches_db_path(key_path, full_path):
                    continue
                pair = (full_path, hex_key)
                if (
                    key_name_lower == "message_0.db"
                    and basename_lower == "message_0.db"
                ):
                    candidates.append((full_path, hex_key))
                elif key_name_lower == "msg.db" and basename_lower == "msg.db":
                    legacy_candidates.append(pair)
                elif not getattr(platform, "is_windows", False):
                    # macOS 旧版存在不同消息库命名，保留平台限定兜底。
                    if pair not in other_fallback:
                        other_fallback.append(pair)

        all_candidates = candidates + legacy_candidates + other_fallback

        logger.info(
            f"候选数据库: message_0={len(candidates)} 个, "
            f"MSG={len(legacy_candidates)} 个, "
            f"其他={len(other_fallback)} 个"
        )

        if not all_candidates:
            # 诊断：检查是否 message_0.db 存在但缺少密钥
            msg0_files = [f for f in all_dbs if os.path.basename(f) == "message_0.db"]
            if msg0_files:
                logger.warning(
                    f"message_0.db 存在 ({msg0_files[0]}) 但无匹配密钥，"
                    f"已提取的密钥路径: {list(keys.keys())}"
                )
                # 兜底：用所有已知密钥直接尝试 message_0.db
                all_candidates = [(f, k) for f in msg0_files for k in keys.values()]
                if not all_candidates:
                    return None
            else:
                logger.warning("未找到消息数据库（无匹配密钥的 DB 文件）")
                return None

        # 逐个尝试，验证是否为真正的消息数据库
        for db_path, hex_key in all_candidates:
            basename = os.path.basename(db_path)
            try:
                key_bytes = bytes.fromhex(hex_key)
                if not reader.open_db(db_path, key_bytes):
                    continue
                if reader.is_message_db():
                    logger.info(f"消息数据库已打开: {db_path}")
                    return reader
                reader.close()
            except Exception as exc:
                logger.warning(f"打开候选数据库失败 ({basename}): {exc}")
                continue

        # 最终兜底：用所有密钥直接尝试 message_0.db（密钥可能未关联路径）
        msg0_files = [f for f in all_dbs if os.path.basename(f) == "message_0.db"]
        if msg0_files:
            msg0_path = msg0_files[0]
            logger.info(
                f"候选数据库均非消息表，尝试用 {len(keys)} 个密钥直接解密 "
                f"{os.path.basename(msg0_path)}"
            )
            for hex_key in keys.values():
                try:
                    key_bytes = bytes.fromhex(hex_key)
                    if reader.open_db(msg0_path, key_bytes) and reader.is_message_db():
                        logger.info(f"兜底成功: 消息数据库已打开: {msg0_path}")
                        return reader
                    reader.close()
                except Exception:
                    continue

        logger.warning("所有候选数据库均不包含消息表，消息监控无法启动")
        return None

    @staticmethod
    def _build_name_map(platform, keys: dict[str, str]) -> dict[str, str]:
        """构建 wxid -> 显示名 映射 (用于联系人搜索)。"""
        db_reader = platform.db_reader

        name_map: dict[str, str] = {}

        all_dbs: list[str] = []
        if hasattr(db_reader, "find_database_files"):
            all_dbs = db_reader.find_database_files()
        elif hasattr(db_reader, "__class__") and hasattr(db_reader.__class__, "find_database_files"):
            all_dbs = db_reader.__class__.find_database_files()

        # 查找 contact.db
        contact_db_path = None
        contact_key = None
        for full_path in all_dbs:
            for key_path, hex_key in keys.items():
                if _key_matches_db_path(key_path, full_path):
                    if "contact.db" in key_path or "contact.db" in os.path.basename(
                        full_path
                    ):
                        contact_db_path = full_path
                        contact_key = hex_key
                        break
            if contact_db_path:
                break

        if not contact_db_path or not contact_key:
            logger.warning("未找到联系人数据库，名称映射为空")
            return name_map

        try:
            # 使用 platform.db_reader 获取同类 reader
            contact_reader = platform.db_reader.__class__()
            key_bytes = bytes.fromhex(contact_key)
            if contact_reader.open_db(contact_db_path, key_bytes):
                # 联系人
                for c in contact_reader.get_contacts():
                    wxid = c.get("wxid", "")
                    if wxid:
                        # 优先备注：备注由用户自己设置，比昵称更唯一可靠
                        name_map[wxid] = (
                            c.get("remark") or c.get("nickname") or c.get("alias") or wxid
                        )
                # 群聊
                for r in contact_reader.get_chatrooms():
                    room_id = r.get("room_id", "")
                    if room_id:
                        AutoReplyPipeline._merge_chatroom_name(
                            name_map,
                            room_id,
                            r.get("name", ""),
                        )
                logger.info(f"名称映射已构建: {len(name_map)} 条")
                contact_reader.close()
        except Exception as exc:
            logger.error(f"构建名称映射失败: {exc}")

        return name_map

    @staticmethod
    def _merge_chatroom_name(name_map: dict[str, str], room_id: str, name: str) -> None:
        """合并群聊显示名，不用空值覆盖已有可搜索名称。"""
        if not room_id:
            return
        current = name_map.get(room_id, "")
        if current and not current.endswith("@chatroom"):
            return
        name_map[room_id] = name or current or room_id

    @staticmethod
    def _normalize_display_name(name: str) -> str:
        return str(name or "").strip().casefold()

    @classmethod
    def _find_ambiguous_display_names(cls, name_map: dict[str, str]) -> set[str]:
        """建立跨联系人/群聊的显示名反向索引。"""
        targets: dict[str, set[str]] = {}
        for target_id, display_name in name_map.items():
            normalized = cls._normalize_display_name(display_name)
            if not normalized:
                continue
            targets.setdefault(normalized, set()).add(str(target_id))
        return {
            display_name
            for display_name, target_ids in targets.items()
            if len(target_ids) > 1
        }

    # ------------------------------------------------------------------
    # Internal: 规则初始化
    # ------------------------------------------------------------------

    async def _seed_rules_from_yaml(self) -> None:
        """将 YAML 配置中的自动回复规则同步到数据库（如不存在）。"""
        config = get_config().auto_reply
        yaml_rules = config.get("rules", [])
        if not yaml_rules:
            logger.info("YAML 中未配置自动回复规则")
            return

        if self._session_factory is None:
            return

        from sqlalchemy import select
        from app.models.database import AutoReplyRule

        try:
            async with self._session_factory() as session:
                # 查询已有规则
                result = await session.execute(select(AutoReplyRule.name))
                existing_names = {r for r in result.scalars().all()}

                new_count = 0
                for rule in yaml_rules:
                    name = rule.get("name", "")
                    if not name or name in existing_names:
                        continue

                    record = AutoReplyRule(
                        name=name,
                        type=rule.get("type", "keyword"),
                        patterns=rule.get("patterns", []),
                        reply=rule.get("reply", ""),
                        priority=rule.get("priority", 0),
                        enabled=rule.get("enabled", True),
                        workflow=rule.get("workflow", ""),
                    )
                    session.add(record)
                    existing_names.add(name)
                    new_count += 1

                if new_count > 0:
                    await session.commit()
                    logger.info(f"从 YAML 同步了 {new_count} 条自动回复规则到数据库")

        except Exception as exc:
            logger.error(f"同步规则失败: {exc}")

    # ------------------------------------------------------------------
    # Internal: 消息处理循环
    # ------------------------------------------------------------------

    async def _process_loop(self) -> None:
        """后台消息处理循环。"""
        logger.info("消息处理循环已启动")
        while self._running:
            try:
                msg = await asyncio.wait_for(self._monitor.get_message(), timeout=1.0)
                await self._handle_message(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"处理消息异常: {exc}", exc_info=True)

    async def _handle_message(self, msg) -> None:
        """先持久化用于统计，再按开关和白名单决定是否自动回复。"""
        logger.info(
            f">>> 收到消息 | sender={msg.sender} | is_group={msg.is_group} | "
            f"is_self={getattr(msg, 'is_self', False)} | "
            f"content={msg.content[:80]}"
        )
        config = get_config().auto_reply

        if msg.is_group:
            receiver = msg.room_id or msg.sender
        else:
            receiver = msg.sender
        buffer_key = receiver

        # 首页统计独立于自动回复权限。禁用或不在白名单的消息也只读记录，
        # 但不会进入 AI、规则或发送链路。
        await self._persist_message(msg)

        if not config.get("enabled", False):
            logger.debug("自动回复未启用，仅记录消息 | receiver=%s", receiver)
            return

        if not msg.is_group:
            mode = config.get("private_chat_mode", "whitelist")
            if mode == "none":
                logger.warning(f"私聊已禁用，跳过: {msg.sender}")
                return
            if mode == "whitelist":
                whitelist = config.get("private_whitelist", [])
                if not whitelist:
                    logger.warning(f"私聊白名单为空，跳过: {msg.sender}")
                    return
                if msg.sender not in whitelist and str(msg.sender) not in whitelist:
                    logger.warning(f"私聊不在白名单，跳过: {msg.sender}")
                    return
            # mode == "all": 放行所有私聊
            logger.info(
                f"私聊放行 | sender={msg.sender} | mode={mode} | "
                f"in_whitelist={msg.sender in config.get('private_whitelist', [])}"
            )

        if msg.is_group:
            mode = config.get("group_chat_mode", "whitelist")
            if mode == "none":
                logger.warning(f"群聊已禁用，跳过: {msg.room_id}")
                return
            if mode == "whitelist":
                whitelist = config.get("group_whitelist", [])
                if not whitelist:
                    logger.warning(f"群聊白名单为空，跳过: {msg.room_id}")
                    return
                if msg.room_id not in whitelist and str(msg.room_id) not in whitelist:
                    logger.warning(f"群聊不在白名单，跳过: {msg.room_id}")
                    return
            logger.info(
                f"群聊放行 | room={msg.room_id} | mode={mode} | "
                f"in_whitelist={msg.room_id in config.get('group_whitelist', [])}"
            )

        # 只有通过自动回复权限的会话才进入短期上下文和 AI 记忆。
        self._remember_message_context(msg)

        if getattr(msg, "is_self", False):
            await self._remember_self_message(msg)
            logger.info(
                "当前账号自发消息已记录，跳过自动回复 | receiver=%s | content=%s",
                receiver,
                msg.content[:50],
            )
            return

        if not getattr(msg, "is_text", False) or not str(msg.content or "").strip():
            logger.info(
                "非文本或空内容消息已记录，跳过自动回复 | sender=%s | msg_type=%s",
                msg.sender,
                msg.msg_type,
            )
            return

        # 专属群规则走即时通道，不进入普通消息的 20 秒合并等待。
        # 首条命中会立刻占用去重窗口；即使发送结果暂未确认，也不会补发，
        # 以降低重复点击和重复回复风险。
        group_rule = (
            self._find_group_reply_rule(config, receiver)
            if msg.is_group
            else None
        )
        if group_rule and group_rule.get("immediate", True):
            group_rule_only = bool(group_rule.get("rule_only", True))
            if self._group_reply_rule_matches(group_rule, msg.content):
                reply_text = str(group_rule.get("reply", "")).strip()
                if not self._reserve_group_rule_trigger(receiver, group_rule):
                    logger.info(
                        "群聊专属规则短窗口内已触发，跳过重复回复 | room=%s",
                        receiver,
                    )
                    return
                logger.info(
                    "群聊专属规则即时命中 | room=%s | keyword=%s",
                    receiver,
                    group_rule.get("keyword", ""),
                )
                await self._send_reply(reply_text, receiver, msg)
                return
            if group_rule_only:
                logger.debug(
                    "群聊专属规则未命中且禁止兜底，保持静默 | room=%s",
                    receiver,
                )
                return

        # 防抖：取消旧定时器，入队，启动新 20s 定时器
        if buffer_key in self._buffer_timers:
            self._buffer_timers[buffer_key].cancel()

        if buffer_key not in self._buffer:
            self._buffer[buffer_key] = []
        self._buffer[buffer_key].append(msg)

        self._buffer_timers[buffer_key] = asyncio.create_task(
            self._flush_buffer(buffer_key)
        )
        logger.debug(
            f"消息入缓冲 | key={buffer_key} | 缓冲数={len(self._buffer[buffer_key])}"
        )

    async def _flush_buffer(self, buffer_key: str) -> None:
        """防抖到期：合并缓冲消息，执行规则匹配 + AI 回复。"""
        await asyncio.sleep(self._debounce_seconds)

        messages = self._buffer.pop(buffer_key, [])
        self._buffer_timers.pop(buffer_key, None)

        if not messages:
            return

        reply_messages = [
            m for m in messages
            if not getattr(m, "is_self", False)
            and getattr(m, "is_text", False)
            and str(m.content or "").strip()
        ]
        if not reply_messages:
            logger.info("缓冲内无可回复文本消息，跳过 | key=%s", buffer_key)
            return

        msg = reply_messages[0]
        if msg.is_group:
            receiver = msg.room_id or msg.sender
        else:
            receiver = msg.sender

        parts = [m.content for m in reply_messages]
        combined = "\n".join(parts)
        if len(combined) > 2000:
            combined = combined[:2000] + "..."

        logger.info(
            f"缓冲刷新 | key={buffer_key} | 合并 {len(messages)} 条 | "
            f"content={combined[:80]}"
        )

        config = get_config().auto_reply
        reply_mode = config.get("reply_mode", "all")
        reply_text = ""
        group_rule = (
            self._find_group_reply_rule(config, receiver)
            if msg.is_group
            else None
        )
        group_rule_only = bool(group_rule and group_rule.get("rule_only", True))

        # 群聊专属规则优先于全局规则和 AI。包含匹配直接针对原始消息文本，
        # 因此不受 Windows 群消息中的发送者前缀或关键词位置影响。
        if group_rule:
            matched = any(
                self._group_reply_rule_matches(group_rule, item.content)
                for item in reply_messages
            )
            if matched:
                reply_text = str(group_rule.get("reply", "")).strip()
                logger.info(
                    "群聊专属规则命中 | room=%s | keyword=%s",
                    receiver,
                    group_rule.get("keyword", ""),
                )
            elif group_rule_only:
                logger.debug(
                    "群聊专属规则未命中且禁止兜底，保持静默 | room=%s",
                    receiver,
                )
                return

        # 1. 规则匹配（逐条匹配，取第一条命中）
        if (
            not reply_text
            and not group_rule_only
            and reply_mode in ("keyword", "all")
            and self._rule_engine
        ):
            for m in reply_messages:
                result = await self._rule_engine.match(m.content)
                if result.get("matched"):
                    reply_text = result.get("reply", "")
                    workflow_name = result.get("workflow", "")
                    if workflow_name and self._workflow_engine:
                        await self._workflow_engine.start_workflow(workflow_name, m.sender)
                    break

        # 2. AI 兜底（用合并内容调用）
        if (
            not reply_text
            and not group_rule_only
            and reply_mode in ("ai", "all")
        ):
            ai_msg = reply_messages[0]
            ai_msg.content = combined
            reply_text = await self._ai_chat(ai_msg)

        # 3. 发送回复
        if reply_text:
            await self._send_reply(reply_text, receiver, msg)

    @staticmethod
    def _group_reply_rule_matches(rule: dict, content: str) -> bool:
        """使用原始消息文本进行大小写无关的 Unicode 包含匹配。"""
        if str(rule.get("match_type", "contains")).lower() != "contains":
            return False
        keyword = str(rule.get("keyword", "")).casefold()
        return bool(keyword) and keyword in str(content or "").casefold()

    def _reserve_group_rule_trigger(self, room_id: str, rule: dict) -> bool:
        """为专属群规则占用短去重窗口；首条立即放行，后续直接丢弃。"""
        now = time.monotonic()
        key = (
            str(room_id),
            str(rule.get("keyword", "")).casefold(),
            str(rule.get("reply", "")),
        )
        last_trigger_at = self._group_rule_last_trigger_at.get(key)
        if (
            last_trigger_at is not None
            and now - last_trigger_at < self._group_rule_dedupe_seconds
        ):
            return False

        self._group_rule_last_trigger_at[key] = now
        cutoff = now - self._group_rule_dedupe_seconds
        self._group_rule_last_trigger_at = {
            item_key: triggered_at
            for item_key, triggered_at in self._group_rule_last_trigger_at.items()
            if triggered_at >= cutoff
        }
        return True

    async def _send_reply(self, reply_text: str, receiver: str, msg) -> bool:
        """清洗并发送回复；发送动作发生后绝不由流水线自动补发。"""
        reply_text = self._clean_reply_for_wechat(reply_text)
        if not reply_text:
            return False

        display_name = self._name_map.get(receiver, receiver)
        normalized_display_name = self._normalize_display_name(display_name)
        if normalized_display_name in self._ambiguous_display_names:
            logger.error(
                "接收者显示名与其他联系人或群聊重名，拒绝自动发送 "
                "| receiver=%s | display_name=%s | is_group=%s",
                receiver,
                display_name,
                msg.is_group,
            )
            return False
        if self._is_unsearchable_name(display_name):
            logger.error(
                "接收者名称无法搜索，拒绝自动发送 | receiver=%s | display_name=%s | is_group=%s",
                receiver,
                display_name,
                msg.is_group,
            )
            return False

        success = await self._sender.send_text(
            reply_text,
            display_name,
            force_skip=False,
            is_group=msg.is_group,
            target_id=receiver,
        )
        if success:
            if self._monitor:
                self._monitor.remember_sent_message(receiver, reply_text)
            logger.info(
                "自动回复已发送 | receiver=%s | reply=%s",
                display_name,
                reply_text[:50],
            )
            await self._park_after_reply()
            return True

        last_result = getattr(self._sender, "last_send_result", None)
        logger.error(
            "自动回复发送失败 | receiver=%s | display_name=%s | is_group=%s "
            "| stage=%s | code=%s | action_performed=%s | draft_cleared=%s",
            receiver,
            display_name,
            msg.is_group,
            getattr(last_result, "stage", ""),
            getattr(last_result, "error_code", ""),
            bool(getattr(last_result, "action_performed", False)),
            bool(getattr(last_result, "draft_cleared", False)),
        )
        return False

    @staticmethod
    def _find_group_reply_rule(config: dict, room_id: str) -> Optional[dict]:
        """返回指定群当前启用的专属规则；未配置时返回 None。"""
        rules = config.get("group_reply_rules", [])
        if not isinstance(rules, list):
            return None
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            if str(rule.get("room_id", "")) != str(room_id):
                continue
            if str(rule.get("match_type", "contains")).lower() != "contains":
                logger.warning(
                    "群聊专属规则匹配类型不受支持，已保持静默 | room=%s",
                    room_id,
                )
            return rule
        return None

    @staticmethod
    def _is_unsearchable_name(name: str) -> bool:
        """判断名称是否无法在微信搜索框中精准搜索。"""
        if not name:
            return True
        # wxid_xxx 原始 ID（微信搜索框搜不到）
        if name.startswith("wxid_"):
            return True
        # 群聊原始 ID：数字@chatroom（微信搜索框搜不到）
        # 使用 endswith 而非 in：合法群聊显示名不会以 @chatroom 结尾
        if name.endswith("@chatroom"):
            return True
        return False

    async def _park_after_reply(self) -> None:
        """自动回复完成后停靠到固定私聊，下一条消息始终重新搜索目标。"""
        if not self._park_after_send or not self._parking_receiver:
            return
        try:
            success = await self._private_sender.open_chat(self._parking_receiver)
            self._private_sender.reset_search_state()
            if hasattr(self._sender, "reset_search_state"):
                self._sender.reset_search_state()
            if success:
                logger.info("自动回复后已停靠到聊天 | receiver=%s", self._parking_receiver)
            else:
                logger.warning("自动回复后停靠聊天失败 | receiver=%s", self._parking_receiver)
        except Exception as exc:
            logger.warning("自动回复后停靠聊天异常 | receiver=%s | error=%s", self._parking_receiver, exc)

    async def _persist_message(self, msg) -> None:
        """持久化消息到数据库。"""
        if self._session_factory is None:
            return
        try:
            from app.services.message_service import MessageService
            async with self._session_factory() as session:
                service = MessageService(session)
                await service.save_message({
                    "msg_id": msg.msg_id,
                    "msg_type": msg.msg_type,
                    "content": msg.content or "",
                    "sender": msg.sender,
                    "sender_name": self._name_map.get(msg.sender, ""),
                    "room_id": msg.room_id or "",
                    "room_name": self._name_map.get(msg.room_id, "") if msg.room_id else "",
                    "is_group": msg.is_group,
                    "create_time": msg.create_time,
                })
        except Exception as exc:
            logger.error(f"持久化消息失败: {exc}")

    async def _backfill_today_messages(self, reader) -> None:
        """将今天的微信文本消息回填到本地统计，不触发任何自动回复。"""
        if self._session_factory is None:
            return

        midnight = datetime.combine(datetime.now().date(), datetime_time.min)
        try:
            messages = await asyncio.to_thread(
                reader.query_messages_since,
                int(midnight.timestamp()),
            )
            if not messages:
                return

            from app.services.message_service import MessageService

            payloads = [
                {
                    "msg_id": msg.msg_id,
                    "msg_type": msg.msg_type,
                    "content": msg.content or "",
                    "sender": msg.sender,
                    "sender_name": self._name_map.get(msg.sender, ""),
                    "room_id": msg.room_id or "",
                    "room_name": (
                        self._name_map.get(msg.room_id, "") if msg.room_id else ""
                    ),
                    "is_group": msg.is_group,
                    "create_time": msg.create_time,
                }
                for msg in messages
            ]
            async with self._session_factory() as session:
                inserted = await MessageService(session).save_messages(payloads)
            logger.info(
                "今日消息统计已回填: 扫描=%d, 新增=%d",
                len(messages),
                inserted,
            )
        except Exception as exc:
            logger.warning("回填今日消息统计失败: %s", exc)


    def _remember_message_context(self, msg) -> None:
        """记录最近聊天上下文，供下一次 AI 回复理解当前账号刚说过什么。"""
        session_id = self._chat_session_id(msg)
        content = str(msg.content or "").strip()
        if not session_id or not content:
            return

        if getattr(msg, "is_self", False):
            label = "我"
        elif msg.is_group:
            label = self._name_map.get(msg.sender, msg.sender)
        else:
            label = self._name_map.get(msg.sender, "对方")

        line = f"{label}: {content}"
        bucket = self._recent_chat_context.setdefault(session_id, [])
        bucket.append(line)
        if len(bucket) > self._recent_context_limit:
            del bucket[:-self._recent_context_limit]

    def _format_recent_context(self, session_id: str) -> str:
        lines = self._recent_chat_context.get(session_id, [])
        return "\n".join(lines[-self._recent_context_limit:]) if lines else "无历史对话"

    async def _remember_self_message(self, msg) -> None:
        """自发消息只写入记忆，不触发规则和 AI 回复。"""
        content = str(msg.content or "").strip()
        if not content:
            return
        try:
            if self._ai_agent is None:
                from app.ai.agent import WeixAgent
                self._ai_agent = WeixAgent()
                logger.info("AI 助手已初始化用于记忆自发消息")
            session_id = self._chat_session_id(msg)
            await self._ai_agent.remember_observation(
                message=content,
                session_id=session_id,
                context={
                    "is_group": msg.is_group,
                    "user_name": self._name_map.get(msg.sender, msg.sender),
                    "user_wxid": msg.sender,
                    "room_id": msg.room_id or "",
                    "room_name": self._name_map.get(msg.room_id, "") if msg.room_id else "",
                    "speaker": "self",
                },
            )
        except Exception as exc:
            logger.warning("记录自发消息到 AI 记忆失败: %s", exc)

    @staticmethod
    def _chat_session_id(msg) -> str:
        return (
            f"group:{msg.room_id}" if msg.is_group and msg.room_id
            else f"private:{msg.sender}"
        )

    @staticmethod
    def _clean_reply_for_wechat(text: str) -> str:
        """把规则/AI 回复压成单条微信口语文本。"""
        text = str(text or "").strip()
        if not text:
            return ""

        # 去掉常见 emoji 和符号表情，避免和当前账号风格冲突。
        text = re.sub(
            "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002700-\U000027BF]+",
            "",
            text,
        )
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        text = re.sub(r"\s+([，。！？、；：,.!?])", r"\1", text)
        text = re.sub(r"([（(])\s+", r"\1", text)
        text = re.sub(r"\s+([）)])", r"\1", text)
        return text

    async def _ai_chat(self, msg) -> str:
        """调用 AI 生成聊天回复。"""
        try:
            if self._ai_agent is None:
                from app.ai.agent import WeixAgent
                self._ai_agent = WeixAgent()
                logger.info("AI 助手已初始化")

            session_id = (
                f"group:{msg.room_id}" if msg.is_group
                else f"private:{msg.sender}"
            )

            # 查找显示名
            sender_name = self._name_map.get(msg.sender, msg.sender)
            room_name = ""
            if msg.is_group and msg.room_id:
                room_name = self._name_map.get(msg.room_id, msg.room_id)

            context = {
                "is_group": msg.is_group,
                "user_name": sender_name,
                "user_wxid": msg.sender,
                "room_id": msg.room_id or "",
                "room_name": room_name,
                "chat_context": self._format_recent_context(session_id),
            }

            reply = await self._ai_agent.chat(
                message=msg.content,
                session_id=session_id,
                context=context,
            )
            return reply
        except Exception as exc:
            logger.error(f"AI 回复失败: {exc}")
            return ""
