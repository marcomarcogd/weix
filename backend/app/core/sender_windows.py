"""Windows 平台 WeChat 消息发送器。

通过 pyautogui 模拟鼠标点击和微信右键粘贴菜单操作 GUI，与 macOS AppleScript 方案对应。
支持私聊/群聊、免搜索缓存、全局锁串行化、发送后停靠。
"""

import asyncio
import ctypes
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ctypes import wintypes

import pyautogui
import pyperclip

from app.core.base import BaseMessageSender
from app.core.send_result import SendResult
from app.config import get_config
from app.utils.paths import get_data_dir

logger = logging.getLogger(__name__)

# 微信窗口标题（中文/英文）
WECHAT_WINDOW_TITLES = ["微信", "WeChat"]
WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}


if os.name == "nt":
    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _WND_ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    _USER32.EnumWindows.argtypes = [_WND_ENUM_PROC, wintypes.LPARAM]
    _USER32.EnumWindows.restype = wintypes.BOOL
    _USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    _USER32.IsWindowVisible.restype = wintypes.BOOL
    _USER32.IsIconic.argtypes = [wintypes.HWND]
    _USER32.IsIconic.restype = wintypes.BOOL
    _USER32.IsZoomed.argtypes = [wintypes.HWND]
    _USER32.IsZoomed.restype = wintypes.BOOL
    _USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _USER32.GetWindowTextLengthW.restype = ctypes.c_int
    _USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _USER32.GetWindowTextW.restype = ctypes.c_int
    _USER32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _USER32.GetWindowRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    _USER32.GetWindowRect.restype = wintypes.BOOL
    _USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _USER32.ShowWindow.restype = wintypes.BOOL
    _USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _USER32.SetForegroundWindow.restype = wintypes.BOOL
    _USER32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _USER32.SetWindowPos.restype = wintypes.BOOL
else:  # pragma: no cover - this module is only selected on Windows
    _USER32 = None
    _WND_ENUM_PROC = None


def _window_title(hwnd: int) -> str:
    if _USER32 is None:
        return ""
    length = int(_USER32.GetWindowTextLengthW(hwnd))
    buffer = ctypes.create_unicode_buffer(max(length + 1, 256))
    _USER32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _window_pid(hwnd: int) -> int | None:
    if _USER32 is None:
        return None
    pid = wintypes.DWORD()
    if not _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    return int(pid.value)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if _USER32 is None:
        return None
    rect = wintypes.RECT()
    if not _USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _activate_window(hwnd: int) -> None:
    """Activate a window without importing pywin32."""
    if _USER32 is None:
        return
    show_command = 9 if _USER32.IsIconic(hwnd) else 5  # SW_RESTORE / SW_SHOW
    _USER32.ShowWindow(hwnd, show_command)
    _USER32.SetForegroundWindow(hwnd)


def _move_window(hwnd: int, left: int, top: int, width: int, height: int) -> bool:
    if _USER32 is None:
        return False
    # SWP_NOZORDER | SWP_NOACTIVATE
    return bool(_USER32.SetWindowPos(hwnd, None, left, top, width, height, 0x0004 | 0x0010))


def _prepare_mouse_for_gui() -> None:
    """Move the cursor away from PyAutoGUI's emergency corners before automation."""
    try:
        screen_width, screen_height = pyautogui.size()
        safe_x = max(1, min(int(screen_width) - 2, int(screen_width) // 2))
        safe_y = max(1, min(int(screen_height) - 2, int(screen_height) // 2))
        if _USER32 is not None:
            _USER32.SetCursorPos(safe_x, safe_y)
    except Exception as exc:
        logger.debug("准备微信 GUI 鼠标位置失败: %s", exc)

# 单线程 executor，保证 GUI 操作严格串行
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wx-gui")


@dataclass
class _WindowRef:
    """Minimal window reference shared by pygetwindow/win32 fallbacks."""

    left: int
    top: int
    width: int
    height: int
    title: str = ""
    hwnd: int | None = None

    def activate(self) -> None:
        if not self.hwnd:
            return
        try:
            import win32con
            import win32gui

            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(self.hwnd)
            return
        except Exception:
            # pywin32 is not compatible with every bundled Python build.
            # Use user32 directly so the selected account still gets focused.
            try:
                _activate_window(self.hwnd)
            except Exception:
                # The caller has a click-based fallback after activation.
                pass


class WindowsSender(BaseMessageSender):
    """Windows 平台消息发送器。

    通过 pyautogui 模拟鼠标操作微信 GUI：
      - 完整搜索：激活微信 → 点击搜索框 → 粘贴名称 → 点击首个结果 → 粘贴消息 → 点击发送
      - 免搜索：同接收者 + 在 TTL 内 → 直接粘贴消息发送
      - 发送后停靠：切换到固定私聊，避免下一条消息搜索串扰
    """

    # 全局串行锁，确保单线程操作微信 GUI
    _gui_lock = threading.Lock()
    _global_last_activity: float = 0.0

    def __init__(self):
        config = get_config()
        win_cfg = config.windows_sender if hasattr(config, "windows_sender") else {}
        self._method = str(win_cfg.get("method", "uia")).strip().lower()
        self._allow_mouse_fallback = bool(win_cfg.get("allow_mouse_fallback", False))
        self._uia_sender = None
        self._type_delay = win_cfg.get("type_delay", 0.3)
        self._window_activate_delay = win_cfg.get("window_activate_delay", 0.5)
        self._search_result_delay = win_cfg.get("search_result_delay", 2.0)
        self._skip_search_ttl = win_cfg.get("skip_search_ttl", 60)
        self._search_x_offset = int(win_cfg.get("search_x_offset", 173))
        self._search_y_offset = int(win_cfg.get("search_y_offset", 49))
        self._search_clear_x_offset = int(win_cfg.get("search_clear_x_offset", 247))
        self._search_clear_y_offset = int(win_cfg.get("search_clear_y_offset", 49))
        self._search_result_x_offset = int(win_cfg.get("search_result_x_offset", 153))
        self._search_result_y_offset = int(win_cfg.get("search_result_y_offset", 130))
        self._group_search_result_x_offset = int(
            win_cfg.get("group_search_result_x_offset", self._search_result_x_offset)
        )
        self._group_search_result_y_offset = int(
            win_cfg.get("group_search_result_y_offset", self._search_y_offset + 120)
        )
        self._click_x_ratio = win_cfg.get("click_x_ratio", 0.5)
        self._click_y_ratio = win_cfg.get("click_y_ratio", 0.88)
        self._send_button_x_from_right = int(win_cfg.get("send_button_x_from_right", 72))
        self._send_button_y_from_bottom = int(win_cfg.get("send_button_y_from_bottom", 40))
        self._paste_menu_x_offset = int(win_cfg.get("paste_menu_x_offset", 24))
        self._paste_menu_y_offset = int(win_cfg.get("paste_menu_y_offset", 15))
        self._context_menu_delay = float(win_cfg.get("context_menu_delay", 0.25))
        self._paste_method = str(win_cfg.get("paste_method", "context_menu"))
        self._verify_after_send = win_cfg.get("verify_after_send", True)
        self._verify_timeout = float(win_cfg.get("verify_timeout", 30.0))
        self._verify_interval = float(win_cfg.get("verify_interval", 1.0))
        self._park_after_send = bool(win_cfg.get("park_after_send", False))
        self._parking_receiver = str(win_cfg.get("parking_receiver", "") or "")

        self._last_receiver = ""
        self._last_send_time: float = 0.0
        self._last_result: SendResult | None = None

    # --- 公共接口 ---

    async def send_text(
        self,
        msg: str,
        receiver: str,
        force_skip: bool = False,
        is_group: bool = False,
        target_id: str = "",
        attempt_id: str = "",
        wait_for_db_verify: bool = True,
    ) -> bool:
        """发送文本消息。

        Args:
            msg: 消息内容。
            receiver: 接收者名称（用于搜索）。
            force_skip: 强制跳过搜索（macOS 兼容参数）。
            is_group: 是否为群聊（群聊始终完整搜索）。
            target_id: 数据库会话 ID，用于发送后校验实际落点。

        Returns:
            True 表示发送成功。
        """
        result = await self.send_text_result(
            msg,
            receiver,
            force_skip=force_skip,
            is_group=is_group,
            target_id=target_id,
            attempt_id=attempt_id,
            wait_for_db_verify=wait_for_db_verify,
        )
        return result.success

    async def send_text_result(
        self,
        msg: str,
        receiver: str,
        force_skip: bool = False,
        is_group: bool = False,
        target_id: str = "",
        attempt_id: str = "",
        wait_for_db_verify: bool = True,
    ) -> SendResult:
        """Send text and retain structured delivery diagnostics."""
        method = self._method if self._method in {"uia", "uia_only", "uia-first"} else "mouse"
        result = SendResult.for_message(msg, target_id or receiver, method, attempt_id)
        if not msg or not receiver:
            result.fail("draft", "invalid_request", "消息内容或接收者为空")
            self._last_result = result
            return result

        if self._method in {"uia", "uia_only", "uia-first"}:
            result = await self._send_text_uia_result(
                msg,
                receiver,
                is_group,
                target_id,
                attempt_id,
                wait_for_db_verify,
            )
            uia_mode = str(getattr(self._uia_sender, "_send_mode", "") or "")
            if (
                result.success
                or result.status == "pending_verify"
                or result.action_performed
                or result.draft_cleared
                or not self._allow_mouse_fallback
                or uia_mode == "auto"
            ):
                self._last_result = result
                return result
            logger.warning("UIA 发送失败，按配置回退鼠标发送 | receiver=%s", receiver)

            result = await self._send_text_mouse_result(
                msg,
                receiver,
                force_skip,
                is_group,
                target_id,
                attempt_id,
            )
            self._last_result = result
            return result

        result = await self._send_text_mouse_result(
            msg,
            receiver,
            force_skip,
            is_group,
            target_id,
            attempt_id,
        )
        self._last_result = result
        return result

    async def verify_pending_result(
        self,
        result: SendResult,
        msg: str,
        target_id: str = "",
        timeout: float | None = None,
    ) -> SendResult:
        """Continue database-only verification for a prior send attempt.

        This method never touches the UI.  It exists so callers can keep a
        ``pending_verify`` log row alive while WeChat finishes writing the
        message database, without re-entering the send path.
        """
        if result.status != "pending_verify":
            return result
        target_id = str(target_id or result.details.get("verification_target_id", ""))
        since_ts = int(result.details.get("db_verify_since_ts") or result.started_at)
        if not target_id:
            return result.pending(
                "db_verify",
                error_code="target_id_required",
                error_message="数据库验证必须提供目标会话 ID，已禁止跨会话匹配",
                db_verified=False,
            )

        loop = asyncio.get_running_loop()
        verified = await loop.run_in_executor(
            _executor,
            self._verify_sent_text,
            msg,
            since_ts,
            target_id,
            timeout,
        )
        result.db_verified = verified
        if verified:
            return result.sent("db_verify", db_verified=True, ui_verified=result.ui_verified)
        return result.pending(
            "db_verify",
            error_code="db_not_confirmed",
            error_message="发送动作已完成，但目标会话数据库仍未确认",
            db_verified=False,
            ui_verified=result.ui_verified,
        )

    @property
    def last_result(self) -> SendResult | None:
        return self._last_result

    async def diagnose_uia(self) -> dict:
        """Probe the selected account's UIA tree without sending."""
        if self._uia_sender is None:
            from app.core.sender_windows_uia import WindowsUIASender

            self._uia_sender = WindowsUIASender()
        payload = await self._uia_sender.diagnose()
        window = payload.get("window") or {}
        payload.update(
            available=bool(payload.get("uia_available")),
            main_window=bool(window),
            reason=str(payload.get("error") or "微信 UIA 关键控件已就绪"),
            reason_code=str(payload.get("error_code") or ""),
            window_class=str(window.get("class_name") or ""),
        )
        return payload

    async def prewarm_uia(self) -> dict:
        """Compatibility hook used by the current startup pipeline."""
        return await self.diagnose_uia()

    async def activate_uia(self) -> dict:
        """Run the configured non-foreground UIA accessibility activation."""
        if self._uia_sender is None:
            from app.core.sender_windows_uia import WindowsUIASender

            self._uia_sender = WindowsUIASender()
        return await self._uia_sender.activate_accessibility()

    def refresh_policy(self) -> None:
        """Reload settings saved by the current management API."""
        cfg = get_config().windows_sender if hasattr(get_config(), "windows_sender") else {}
        self._method = str(cfg.get("method", "uia") or "uia").strip().lower()
        self._allow_mouse_fallback = bool(cfg.get("allow_mouse_fallback", False))
        self._verify_after_send = bool(cfg.get("verify_after_send", True))
        self._park_after_send = bool(cfg.get("park_after_send", False))
        self._parking_receiver = str(cfg.get("parking_receiver", "") or "")
        if self._uia_sender is not None:
            self._uia_sender.refresh_send_policy()

    @property
    def last_send_result(self) -> SendResult | None:
        """Compatibility alias for the current diagnostics and tests."""
        return self._last_result

    async def _send_text_mouse_result(
        self,
        msg: str,
        receiver: str,
        force_skip: bool,
        is_group: bool,
        target_id: str,
        attempt_id: str = "",
    ) -> SendResult:
        result = SendResult.for_message(msg, target_id or receiver, "mouse", attempt_id)
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            _executor,
            self._send_text_sync,
            msg,
            receiver,
            force_skip,
            is_group,
            target_id,
        )
        if not success:
            return result.fail(
                "db_verify" if self._verify_after_send else "invoke",
                "legacy_send_failed",
                "传统 Windows 发送流程失败",
            )
        result.action_performed = True
        result.db_verified = bool(self._verify_after_send)
        return result.sent(
            "db_verify" if self._verify_after_send else "invoke",
            action_performed=True,
            db_verified=result.db_verified,
        )

    async def _send_text_uia(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        attempt_id: str = "",
        wait_for_db_verify: bool = True,
    ) -> bool:
        result = await self._send_text_uia_result(
            msg,
            receiver,
            is_group,
            target_id,
            attempt_id,
            wait_for_db_verify,
        )
        return result.success

    async def _send_text_uia_result(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        attempt_id: str = "",
        wait_for_db_verify: bool = True,
    ) -> SendResult:
        if self._uia_sender is None:
            from app.core.sender_windows_uia import WindowsUIASender

            self._uia_sender = WindowsUIASender()
        send_started_at = int(time.time())
        result = await self._uia_sender.send_text_result(
            msg,
            receiver,
            is_group=is_group,
            target_id=target_id,
            attempt_id=attempt_id,
        )
        result.details.setdefault("db_verify_since_ts", send_started_at)
        result.details.setdefault("verification_target_id", str(target_id or ""))
        if not result.success or not self._verify_after_send:
            return result

        if not wait_for_db_verify:
            return result.pending(
                "db_verify",
                error_code="db_verification_deferred",
                error_message="UIA 已完成发送，数据库验证已转入后台，不阻塞发送请求",
                db_verified=False,
                ui_verified=result.ui_verified,
            )

        if not target_id:
            return result.pending(
                "db_verify",
                error_code="target_id_required",
                error_message="数据库验证必须提供目标会话 ID，已禁止跨会话匹配",
                db_verified=False,
                ui_verified=result.ui_verified,
            )

        loop = asyncio.get_running_loop()
        verified = await loop.run_in_executor(
            _executor,
            self._verify_sent_text,
            msg,
            send_started_at,
            target_id,
        )
        result.db_verified = verified
        if verified:
            return result.sent(
                "db_verify",
                db_verified=True,
                ui_verified=result.ui_verified,
            )

        result.pending(
            "db_verify",
            error_code="db_not_confirmed",
            error_message="UIA 已完成发送，但目标会话数据库暂未确认",
            db_verified=False,
            ui_verified=result.ui_verified,
        )
        if not verified:
            logger.error(
                "UIA 已执行发送，但数据库回读未确认 | receiver=%s | target_id=%s",
                receiver,
                target_id,
            )
        return result

    async def send_image(self, path: str, receiver: str) -> bool:
        logger.warning("Windows 平台暂不支持 send_image")
        return False

    async def is_wechat_running(self) -> bool:
        """检查微信进程是否在运行。"""
        if self._method in {"uia", "uia_only", "uia-first"}:
            if self._uia_sender is None:
                from app.core.sender_windows_uia import WindowsUIASender

                self._uia_sender = WindowsUIASender()
            if await self._uia_sender.is_wechat_running():
                return True
            if not self._allow_mouse_fallback or getattr(self._uia_sender, "_send_mode", "") == "auto":
                return False
        return self._find_wechat_window() is not None

    async def open_chat(self, receiver: str) -> bool:
        """打开指定聊天（用于发送后停靠）。"""
        if not receiver:
            return False
        if self._method in {"uia", "uia_only", "uia-first"}:
            if self._uia_sender is None:
                from app.core.sender_windows_uia import WindowsUIASender

                self._uia_sender = WindowsUIASender()
            opened = await self._uia_sender.open_chat(receiver)
            if opened or not self._allow_mouse_fallback or getattr(self._uia_sender, "_send_mode", "") == "auto":
                return opened
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, self._open_chat_sync, receiver)

    def reset_search_state(self) -> None:
        """清空免搜索状态。"""
        self._last_receiver = ""
        self._last_send_time = 0.0

    def _remember_current_chat(self, receiver: str) -> None:
        """记录当前停留的聊天，用于免搜索判断。"""
        self._last_receiver = receiver
        self._last_send_time = time.monotonic()
        WindowsSender._global_last_activity = self._last_send_time

    def _park_if_needed(self, receiver: str) -> None:
        """发送后停靠到固定聊天，避免后续免搜索发送落错会话。"""
        if not self._park_after_send or not self._parking_receiver:
            return
        if receiver == self._parking_receiver:
            return
        try:
            self._full_search(self._parking_receiver, is_group=False)
            self._remember_current_chat(self._parking_receiver)
        except Exception as exc:
            logger.warning("发送后停靠失败，已清空免搜索状态: %s", exc)
            self.reset_search_state()

    # --- 同步核心逻辑 ---

    def _send_text_sync(
        self,
        msg: str,
        receiver: str,
        force_skip: bool,
        is_group: bool,
        target_id: str = "",
    ) -> bool:
        """同步消息发送，在全局锁内执行。"""
        with self._gui_lock:
            try:
                _prepare_mouse_for_gui()

                # 检查微信是否在运行
                if self._find_wechat_window() is None:
                    logger.error("未找到微信窗口")
                    return False

                # 判断是否跳过搜索
                skip_search = self._should_skip_search(receiver, force_skip, is_group)

                if not skip_search:
                    self._full_search(receiver, is_group=is_group)
                else:
                    logger.info("免搜索发送 | receiver=%s", receiver)

                send_started_at = int(time.time()) - 2

                # 聚焦输入框 + 粘贴消息 + 点击发送；不使用键盘快捷键。
                self._activate_wechat()
                input_x, input_y = self._focus_message_input()
                self._paste_text(msg, input_x, input_y)
                self._click_send_button()

                if not self._verify_sent_text(msg, send_started_at, target_id):
                    logger.error(
                        "消息发送后未在目标会话数据库中确认 | receiver=%s | target_id=%s",
                        receiver,
                        target_id,
                    )
                    return False

                self._remember_current_chat(receiver)
                self._park_if_needed(receiver)

                logger.info("消息发送成功 | receiver=%s", receiver)
                return True

            except Exception as exc:
                logger.error("消息发送失败: %s", exc)

                # 免搜索失败时重试完整搜索
                if skip_search and not force_skip:
                    logger.info("免搜索失败，重试完整搜索")
                    self.reset_search_state()
                    return self._send_text_sync(
                        msg,
                        receiver,
                        force_skip=False,
                        is_group=is_group,
                        target_id=target_id,
                    )

                return False

    def _open_chat_sync(self, receiver: str) -> bool:
        """同步打开聊天。"""
        with self._gui_lock:
            try:
                _prepare_mouse_for_gui()
                self._full_search(receiver, is_group=False)
                self.reset_search_state()
                return True
            except Exception as exc:
                logger.error("打开聊天失败: %s", exc)
                return False

    # --- GUI 操作原语 ---

    def _get_bound_pid(self) -> int | None:
        """Return the PID selected by the Windows key extractor."""
        try:
            from app.core.platform import Platform

            pid = Platform.get().key_extractor.bound_pid
            return int(pid) if pid else None
        except Exception:
            return None

    def _find_wechat_window(self):
        """Find the selected Weixin main window."""
        target_pid = self._get_bound_pid()
        win = WindowsSender._find_wechat_window_win32(target_pid)
        if win is not None:
            return win

        # When an account is explicitly selected, never fall back to an
        # arbitrary same-titled window from another logged-in account.
        if target_pid:
            return None

        try:
            import pygetwindow as gw
            for title in WECHAT_WINDOW_TITLES:
                windows = gw.getWindowsWithTitle(title)
                for candidate in windows:
                    candidate_title = (candidate.title or "").strip()
                    if candidate_title in WECHAT_WINDOW_TITLES:
                        return candidate
            return None
        except ImportError:
            # 回退：用 pyautogui 的窗口列表
            for title in WECHAT_WINDOW_TITLES:
                wins = pyautogui.getWindowsWithTitle(title)
                for candidate in wins:
                    candidate_title = (candidate.title or "").strip()
                    if candidate_title in WECHAT_WINDOW_TITLES:
                        return candidate
            return None

    @staticmethod
    def _find_wechat_window_win32(target_pid: int | None = None):
        """按进程名精确查找微信主窗口，避免误选 Weix/浏览器窗口。"""
        if _USER32 is None or _WND_ENUM_PROC is None:
            return None

        import psutil

        matches: list[_WindowRef] = []

        def enum_handler(hwnd, _):
            if not _USER32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if title not in WECHAT_WINDOW_TITLES:
                return True
            try:
                pid = _window_pid(hwnd)
                if not pid:
                    return True
                proc_name = psutil.Process(pid).name().lower()
            except Exception:
                return True
            if proc_name not in WECHAT_PROCESS_NAMES:
                return True
            if target_pid and pid != target_pid:
                return True
            rect = _window_rect(hwnd)
            if rect is None:
                return True
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top
            if width < 400 or height < 300:
                return True
            matches.append(
                _WindowRef(
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                    title=title,
                    hwnd=hwnd,
                )
            )
            return True

        try:
            callback = _WND_ENUM_PROC(enum_handler)
            _USER32.EnumWindows(callback, 0)
        except Exception:
            return None
        matches.sort(key=lambda item: (item.width * item.height, -int(item.hwnd or 0)), reverse=True)
        return matches[0] if matches else None

    def _activate_wechat(self) -> None:
        """激活微信窗口。"""
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")

        try:
            win.activate()
        except Exception:
            # pygetwindow.activate 某些版本不可靠，用点击任务栏兜底
            pass

        time.sleep(self._window_activate_delay)
        win = self._ensure_window_visible(win)
        try:
            pyautogui.click(win.left + min(40, max(10, win.width // 20)), win.top + 20)
        except Exception:
            pass
        time.sleep(0.1)

    def _ensure_window_visible(self, win):
        """把非最大化微信窗口挪回屏幕内，避免发送按钮位于屏幕外。"""
        hwnd = getattr(win, "hwnd", None)
        if not hwnd:
            return win
        try:
            screen_w, screen_h = pyautogui.size()
            if _USER32 is not None and _USER32.IsZoomed(hwnd):
                return win
            if win.width >= screen_w or win.height >= screen_h:
                return win

            margin = 8
            new_left = min(max(win.left, margin), max(screen_w - win.width - margin, margin))
            new_top = min(max(win.top, margin), max(screen_h - win.height - margin, margin))
            if new_left == win.left and new_top == win.top:
                return win

            moved = False
            try:
                import win32con
                import win32gui

                win32gui.SetWindowPos(
                    hwnd,
                    None,
                    int(new_left),
                    int(new_top),
                    int(win.width),
                    int(win.height),
                    win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
                )
                moved = True
            except Exception:
                moved = _move_window(
                    hwnd,
                    int(new_left),
                    int(new_top),
                    int(win.width),
                    int(win.height),
                )
            if not moved:
                return win
            time.sleep(0.2)
            refreshed = self._find_wechat_window()
            return refreshed or win
        except Exception as exc:
            logger.debug("微信窗口可见性修正失败: %s", exc)
            return win

    def _full_search(self, receiver: str, is_group: bool = False) -> None:
        """完整搜索流程：激活 → 点击搜索框 → 粘贴 → 点击首个结果。"""
        self._activate_wechat()

        self._focus_search_input()
        self._clear_search_input()
        search_x, search_y = self._focus_search_input()
        self._paste_text(receiver, search_x, search_y)
        time.sleep(self._search_result_delay)

        if is_group:
            self._click_group_search_result()
        else:
            self._click_first_search_result()
        time.sleep(self._search_result_delay)

        logger.debug("完整搜索完成 | receiver=%s", receiver)

    def _focus_search_input(self) -> tuple[int, int]:
        """点击微信左侧搜索框。"""
        x, y = self._window_offset_point(self._search_x_offset, self._search_y_offset)
        pyautogui.click(x, y)
        time.sleep(0.15)
        return x, y

    def _clear_search_input(self) -> None:
        """点击搜索框右侧清空按钮；搜索为空时该点击无副作用。"""
        x, y = self._window_offset_point(
            self._search_clear_x_offset,
            self._search_clear_y_offset,
        )
        pyautogui.click(x, y)
        time.sleep(0.15)

    def _click_first_search_result(self) -> None:
        """点击搜索结果第一项。"""
        x, y = self._window_offset_point(
            self._search_result_x_offset,
            self._search_result_y_offset,
        )
        pyautogui.click(x, y)
        time.sleep(0.15)

    def _click_group_search_result(self) -> None:
        """点击“群聊”分区里的搜索结果，避开顶部“搜索网络结果”。"""
        x, y = self._window_offset_point(
            self._group_search_result_x_offset,
            self._group_search_result_y_offset,
        )
        pyautogui.click(x, y)
        time.sleep(0.15)

    def _focus_message_input(self) -> tuple[int, int]:
        """点击消息输入区域，确保光标在输入框内。"""
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")

        try:
            x = win.left + int(win.width * self._click_x_ratio)
            y = win.top + int(win.height * self._click_y_ratio)
        except AttributeError:
            # pyautogui 回退
            x, y = pyautogui.size()
            x = int(x * self._click_x_ratio)
            y = int(y * self._click_y_ratio)

        pyautogui.click(x, y)
        time.sleep(0.15)
        pyautogui.click(x, y)  # 双击确保焦点
        time.sleep(0.1)
        return x, y

    def _paste_text(self, text: str, x: int, y: int) -> None:
        """粘贴文本，不使用 Ctrl+V。"""
        pyperclip.copy(text)
        time.sleep(0.05)
        if self._paste_method == "context_menu":
            self._paste_text_via_context_menu(x, y)
            return

        win = self._find_wechat_window()
        hwnd = getattr(win, "hwnd", None) if win else None
        if hwnd:
            self._post_paste_to_focused_control(hwnd)
        time.sleep(0.3)

    def _paste_text_via_context_menu(self, x: int, y: int) -> None:
        """使用微信输入框右键菜单的“粘贴”，避开快捷键和 Qt 控件 WM_PASTE 限制。"""
        pyautogui.rightClick(x, y)
        time.sleep(self._context_menu_delay)
        pyautogui.click(x + self._paste_menu_x_offset, y + self._paste_menu_y_offset)
        time.sleep(0.3)

    @staticmethod
    def _post_paste_to_focused_control(hwnd: int) -> None:
        try:
            import win32api
            import win32con
            import win32gui
            import win32process

            fg_hwnd = win32gui.GetForegroundWindow() or hwnd
            target_thread, _pid = win32process.GetWindowThreadProcessId(fg_hwnd)
            current_thread = win32api.GetCurrentThreadId()
            attached = False
            try:
                if target_thread != current_thread:
                    attached = bool(
                        win32process.AttachThreadInput(
                            current_thread,
                            target_thread,
                            True,
                        )
                    )
                focus_hwnd = win32gui.GetFocus() or hwnd
            finally:
                if attached:
                    win32process.AttachThreadInput(
                        current_thread,
                        target_thread,
                        False,
                    )

            win32gui.PostMessage(focus_hwnd, win32con.WM_PASTE, 0, 0)
        except Exception as exc:
            logger.debug("WM_PASTE 粘贴失败: %s", exc)

    def _click_send_button(self) -> None:
        """点击微信输入区右下角发送按钮。"""
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")
        x = win.left + win.width - self._send_button_x_from_right
        y = win.top + win.height - self._send_button_y_from_bottom
        time.sleep(0.15)
        pyautogui.click(x, y)
        time.sleep(0.3)

    def _window_offset_point(self, x_offset: int, y_offset: int) -> tuple[int, int]:
        """按微信窗口左上角固定偏移取点，适配默认窗口和最大化窗口。"""
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")
        x = win.left + min(max(int(x_offset), 1), max(win.width - 1, 1))
        y = win.top + min(max(int(y_offset), 1), max(win.height - 1, 1))
        return x, y

    # --- 发送后校验 ---

    def _verify_sent_text(
        self,
        msg: str,
        since_ts: int,
        target_id: str = "",
        timeout: float | None = None,
    ) -> bool:
        """发送后从本地消息库回读确认，避免 GUI 假阳性。"""
        if not self._verify_after_send:
            return True

        deadline = time.monotonic() + (
            self._verify_timeout if timeout is None else max(0.0, float(timeout))
        )
        while time.monotonic() <= deadline:
            try:
                if self._find_recent_self_text(msg, since_ts, target_id):
                    return True
            except Exception as exc:
                logger.debug("发送回读校验异常: %s", exc)
            time.sleep(self._verify_interval)
        return False

    @staticmethod
    def _find_recent_self_text(msg: str, since_ts: int, target_id: str = "") -> bool:
        from app.core.db_reader_windows import WindowsDBReader

        db_path, hex_key = WindowsSender._find_message_db_key()
        if not db_path or not hex_key:
            logger.warning("发送回读校验跳过: 未找到 message_0.db 密钥")
            return False

        reader = WindowsDBReader()
        try:
            if not reader.open_db(db_path, bytes.fromhex(hex_key)):
                return False
            return WindowsSender._reader_has_recent_self_text(
                reader,
                msg,
                since_ts,
                target_id,
            )
        finally:
            reader.close()

    @staticmethod
    def _find_message_db_key() -> tuple[str, str]:
        from app.core.db_reader_windows import WindowsDBReader

        keys_path = get_data_dir() / "all_keys.json"
        if not keys_path.exists():
            return "", ""
        try:
            keys = json.loads(keys_path.read_text(encoding="utf-8"))
        except Exception:
            return "", ""

        for db_path in WindowsDBReader.find_database_files():
            if os.path.basename(db_path) != "message_0.db":
                continue
            for key_path, hex_key in keys.items():
                if WindowsSender._key_matches_db_path(str(key_path), db_path):
                    return db_path, str(hex_key)
        return "", ""

    @staticmethod
    def _key_matches_db_path(key_path: str, full_path: str) -> bool:
        normalized_key = key_path.replace("\\", "/").lower()
        normalized_full = full_path.replace("\\", "/").lower()
        basename = os.path.basename(full_path)
        if "/" in normalized_key:
            return normalized_full.endswith(normalized_key)
        return os.path.normcase(key_path) == os.path.normcase(basename)

    @staticmethod
    def _reader_has_recent_self_text(
        reader,
        msg: str,
        since_ts: int,
        target_id: str = "",
    ) -> bool:
        # 微信有时先把消息写入库，再返回 UIA 调用结果；允许少量落盘顺序偏差，
        # 但仍然保留 target_id、本人发送标记和正文匹配，避免跨会话误判。
        query_since_ts = max(0, int(since_ts) - 3)
        normalized_msg = WindowsSender._normalize_text(msg)
        if not normalized_msg or reader._sqlite_conn is None:
            return False
        if not target_id:
            logger.warning("拒绝无 target_id 的发送回读校验")
            return False

        if reader._has_msg_shard_tables():
            tables = [
                (table, username)
                for table, username in reader._get_v4_msg_tables()
                if not target_id or username == target_id
            ]
            if target_id and not tables:
                logger.warning("发送回读校验未找到目标会话表 | target_id=%s", target_id)
                return False
            for table, _username in tables:
                try:
                    cursor = reader._sqlite_conn.execute(
                        f'SELECT message_content, real_sender_id, status, '
                        f'origin_source, server_seq '
                        f'FROM "{table}" '
                        f'WHERE create_time >= ? AND local_type = 1 '
                        f'ORDER BY local_id DESC LIMIT 20',
                        (query_since_ts,),
                    )
                except Exception:
                    continue
                for row in cursor:
                    if not reader._is_self_sent_v4_row(row):
                        continue
                    content = reader._decode_message_content(row["message_content"])
                    if WindowsSender._normalize_text(content) == normalized_msg:
                        return True
            return False

        try:
            params: list[object] = [query_since_ts * 1000]
            talker_filter = ""
            if target_id:
                talker_filter = "AND msg_talker = ?"
                params.append(target_id)
            cursor = reader._sqlite_conn.execute(
                f"""
                SELECT msg_content
                FROM MSG
                WHERE msg_create_time >= ?
                  AND msg_type = 1
                  AND is_sender = 1
                  {talker_filter}
                ORDER BY msg_create_time DESC
                LIMIT 50
                """,
                tuple(params),
            )
            for row in cursor:
                if WindowsSender._normalize_text(row["msg_content"]) == normalized_msg:
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(
            r"^\s*(?:(?:wxid|gh)_[a-z0-9_-]+|\d+@openim)\s*:\s*(?:\n|$)",
            "",
            normalized,
            count=1,
            flags=re.IGNORECASE,
        )
        normalized = "".join(
            character for character in normalized
            if character not in "\u200b\u200c\u200d\ufeff"
        )
        return " ".join(normalized.split())

    # --- 内部判断 ---

    def _should_skip_search(
        self,
        receiver: str,
        force_skip: bool,
        is_group: bool,
    ) -> bool:
        """判断是否可以跳过搜索直接发送。"""
        # 群聊始终完整搜索
        if is_group:
            return False

        # 强制跳过
        if force_skip:
            return True

        # 同接收者且在 TTL 内
        if receiver != self._last_receiver:
            return False

        other_activity = self._global_last_activity > self._last_send_time
        if other_activity:
            return False

        elapsed = time.monotonic() - self._last_send_time
        return elapsed <= self._skip_search_ttl
