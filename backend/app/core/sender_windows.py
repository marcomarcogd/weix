"""Windows 平台 WeChat 消息发送器。

通过 pyautogui 模拟鼠标点击和微信右键粘贴菜单操作 GUI，与 macOS AppleScript 方案对应。
支持私聊/群聊、免搜索缓存、全局锁串行化、发送后停靠。
"""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.core.windows_sender_calibration import (
    ClientGeometry,
    calibration_is_compatible,
    enable_per_monitor_dpi_awareness,
    get_client_geometry,
    get_process_file_version,
    load_calibration,
    resolve_point,
)

enable_per_monitor_dpi_awareness()

import pyautogui
import pyperclip

from app.core.base import BaseMessageSender
from app.config import get_config

logger = logging.getLogger(__name__)

# 微信窗口标题（中文/英文）
WECHAT_WINDOW_TITLES = ["微信", "WeChat"]
WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}

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
            raise RuntimeError("微信窗口缺少有效句柄")
        try:
            import win32con
            import win32gui

            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)

            # 最小化窗口的旧坐标通常位于 (-32000, -32000)，恢复后必须
            # 重新读取真实尺寸，避免后续可见性修正使用旧坐标缩小或误点窗口。
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            if right > left and bottom > top:
                self.left = left
                self.top = top
                self.width = right - left
                self.height = bottom - top
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception as exc:
            raise RuntimeError("微信窗口激活失败") from exc


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
        self._send_method = str(win_cfg.get("method", "uia") or "uia").strip().lower()
        self._type_delay = win_cfg.get("type_delay", 0.3)
        self._window_activate_delay = win_cfg.get("window_activate_delay", 0.5)
        self._search_result_delay = win_cfg.get("search_result_delay", 2.0)
        self._skip_search_ttl = win_cfg.get("skip_search_ttl", 60)
        self._context_menu_delay = float(win_cfg.get("context_menu_delay", 0.25))
        self._paste_method = str(win_cfg.get("paste_method", "context_menu"))
        self._verify_after_send = win_cfg.get("verify_after_send", True)
        self._verify_timeout = float(win_cfg.get("verify_timeout", 30.0))
        self._park_after_send = bool(win_cfg.get("park_after_send", False))
        self._parking_receiver = str(
            win_cfg.get("parking_receiver", "文件传输助手") or ""
        )
        self._calibration = load_calibration()
        self._confirmation_source = None
        self._uia_sender = None
        self._last_send_result = None

        self._last_receiver = ""
        self._last_send_time: float = 0.0
        self._active_wechat_hwnd: int | None = None

    # --- 公共接口 ---

    async def send_text(
        self,
        msg: str,
        receiver: str,
        force_skip: bool = False,
        is_group: bool = False,
        target_id: str = "",
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
        if not msg or not receiver:
            logger.error("消息内容或接收者为空")
            return False

        # 自动回复默认只走安全 UIA。旧坐标实现仅保留给显式配置的手工兼容
        # 场景，UIA 失败时绝不会自动回退到鼠标或固定坐标。
        if self._send_method != "legacy_coordinates":
            return await self._send_text_uia(
                msg,
                receiver,
                is_group=is_group,
                target_id=target_id,
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            self._send_text_sync,
            msg,
            receiver,
            force_skip,
            is_group,
            target_id,
        )

    async def send_image(self, path: str, receiver: str) -> bool:
        logger.warning("Windows 平台暂不支持 send_image")
        return False

    async def is_wechat_running(self) -> bool:
        """检查微信进程是否在运行。"""
        return self._find_wechat_window() is not None

    async def open_chat(self, receiver: str) -> bool:
        """打开指定聊天（用于发送后停靠）。"""
        if not receiver:
            return False
        if self._send_method != "legacy_coordinates":
            return await self._get_uia_sender().open_chat(receiver, is_group=False)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, self._open_chat_sync, receiver)

    async def prewarm_uia(self) -> dict:
        """只读预热 UIA 树，不激活、不点击微信。"""
        if self._send_method == "legacy_coordinates":
            return {
                "available": False,
                "reason": "当前显式启用了旧坐标发送模式",
            }
        return await self._get_uia_sender().prewarm()

    async def diagnose_uia(self) -> dict:
        """返回账号/PID 绑定与关键 UIA 控件状态。"""
        return await self._get_uia_sender().diagnose()

    def refresh_policy(self) -> None:
        """热更新 UIA 发送策略；发送次数限制始终硬编码为一次。"""
        cfg = get_config().windows_sender if hasattr(get_config(), "windows_sender") else {}
        self._send_method = str(cfg.get("method", "uia") or "uia").strip().lower()
        if self._uia_sender is not None:
            self._uia_sender.refresh_policy()

    @property
    def last_send_result(self):
        return self._last_send_result

    def _get_uia_sender(self):
        if self._uia_sender is None:
            from app.core.sender_windows_uia import WindowsUIASender

            self._uia_sender = WindowsUIASender()
        return self._uia_sender

    async def _send_text_uia(
        self,
        msg: str,
        receiver: str,
        *,
        is_group: bool,
        target_id: str,
    ) -> bool:
        """执行一次 UIA 动作，再由现有监听器做最终数据库确认。"""
        if not self._confirmation_is_available(target_id):
            return False

        confirmation = self._begin_send_confirmation(
            msg,
            int(time.time()) - 2,
            target_id,
        )
        try:
            result = await self._get_uia_sender().send_text_result(
                msg,
                receiver,
                is_group,
                target_id,
            )
            self._last_send_result = result
            if not result.action_performed:
                logger.error(
                    "UIA 在发送动作前停止 | receiver=%s | target_id=%s | stage=%s | code=%s | error=%s",
                    receiver,
                    target_id,
                    result.stage,
                    result.error_code,
                    result.error_message,
                )
                return False

            confirmed = await asyncio.to_thread(self._verify_sent_text, confirmation)
            if not confirmed:
                result.pending(
                    "db_verify",
                    "db_confirmation_timeout",
                    "发送动作已执行但数据库未确认；为避免重复发送已停止",
                )
                logger.error(
                    "UIA 发送动作已执行但数据库回读未确认，已禁止补发 "
                    "| receiver=%s | target_id=%s | method=%s",
                    receiver,
                    target_id,
                    result.method,
                )
                self.reset_search_state()
                return False

            result.sent(method=result.method)
            self._remember_current_chat(receiver)
            if self._park_after_send and self._parking_receiver and receiver != self._parking_receiver:
                parked = await self._get_uia_sender().open_chat(
                    self._parking_receiver,
                    is_group=False,
                )
                if not parked:
                    logger.warning("UIA 发送后停靠失败，已清空会话状态")
                    self.reset_search_state()
            logger.info(
                "UIA 消息发送并回读确认成功 | receiver=%s | target_id=%s | method=%s",
                receiver,
                target_id,
                result.method,
            )
            return True
        finally:
            if confirmation is not None:
                confirmation.cancel()

    def reset_search_state(self) -> None:
        """清空免搜索状态。"""
        self._last_receiver = ""
        self._last_send_time = 0.0

    def set_confirmation_source(self, source) -> None:
        """注入已打开数据库的消息监听器，发送器不得自行解密数据库。"""
        self._confirmation_source = source

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
            skip_search = self._should_skip_search(receiver, force_skip, is_group)

            if not self._confirmation_is_available(target_id):
                return False

            while True:
                send_action_attempted = False
                confirmation = None
                try:
                    # 检查微信是否在运行
                    if self._find_wechat_window() is None:
                        logger.error("未找到微信窗口")
                        return False

                    if not skip_search:
                        self._full_search(receiver, is_group=is_group)
                    else:
                        logger.info("免搜索发送 | receiver=%s", receiver)

                    send_started_at = int(time.time()) - 2
                    confirmation = self._begin_send_confirmation(
                        msg,
                        send_started_at,
                        target_id,
                    )

                    # 聚焦输入框 + 粘贴消息 + 点击发送；不使用键盘快捷键。
                    self._activate_wechat()
                    input_x, input_y = self._focus_message_input()
                    self._paste_text(msg, input_x, input_y)

                    # 点击本身可能已经生效后才抛异常，所以必须在调用前标记。
                    # 从这一刻开始禁止任何自动补发，避免数据库写入延迟导致重复消息。
                    send_action_attempted = True
                    self._click_send_button()

                    if not self._verify_sent_text(confirmation):
                        logger.error(
                            "发送动作已执行但数据库回读未确认；为避免重复发送已停止补发 "
                            "| receiver=%s | target_id=%s",
                            receiver,
                            target_id,
                        )
                        self.reset_search_state()
                        return False

                    self._remember_current_chat(receiver)
                    self._park_if_needed(receiver)

                    logger.info("消息发送成功 | receiver=%s", receiver)
                    return True

                except Exception as exc:
                    self.reset_search_state()
                    if send_action_attempted:
                        logger.error(
                            "发送动作可能已经执行，已禁止自动重试 "
                            "| receiver=%s | target_id=%s | error=%s",
                            receiver,
                            target_id,
                            exc,
                        )
                        return False

                    # 仅在尚未执行发送动作时，允许免搜索路径切换为一次完整搜索。
                    if skip_search and not force_skip:
                        logger.warning(
                            "免搜索在发送前失败，改用一次完整搜索 "
                            "| receiver=%s | error=%s",
                            receiver,
                            exc,
                        )
                        skip_search = False
                        continue

                    logger.error("消息发送前失败 | receiver=%s | error=%s", receiver, exc)
                    return False
                finally:
                    if confirmation is not None:
                        confirmation.cancel()

    def _open_chat_sync(self, receiver: str) -> bool:
        """同步打开聊天。"""
        with self._gui_lock:
            try:
                self._full_search(receiver, is_group=False)
                self.reset_search_state()
                return True
            except Exception as exc:
                logger.error("打开聊天失败: %s", exc)
                return False

    # --- GUI 操作原语 ---

    @staticmethod
    def _find_wechat_window():
        """查找微信主窗口。"""
        win = WindowsSender._find_wechat_window_win32()
        if win is not None:
            return win

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
    def _find_wechat_window_win32():
        """按进程名精确查找微信主窗口，避免误选 Weix/浏览器窗口。"""
        try:
            import psutil
            import win32gui
            import win32process
        except Exception:
            return None

        matches: list[_WindowRef] = []

        def enum_handler(hwnd, _):
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if title not in WECHAT_WINDOW_TITLES:
                return True
            try:
                _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc_name = psutil.Process(pid).name().lower()
            except Exception:
                return True
            if proc_name not in WECHAT_PROCESS_NAMES:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top
            # Windows 会把最小化窗口放到 (-32000, -32000)，此时尺寸通常
            # 只有约 160x28；关闭到托盘时主窗口则不可见但仍保留正常尺寸。
            # 两者都是有效微信主窗口，真正发送前会自动恢复。
            is_minimized = bool(win32gui.IsIconic(hwnd))
            if not is_minimized and (width < 400 or height < 300):
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
            win32gui.EnumWindows(enum_handler, None)
        except Exception:
            return None
        return matches[0] if matches else None

    def _activate_wechat(self) -> None:
        """激活微信窗口并确认前台归属；失败时禁止继续鼠标操作。"""
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")

        hwnd = self._window_hwnd(win)
        if not hwnd:
            raise RuntimeError("无法取得微信主窗口句柄，已中止本次 GUI 操作")

        self._active_wechat_hwnd = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                win.activate()
            except Exception as exc:
                last_error = exc
                logger.debug("微信窗口常规激活失败（第 %d 次）: %s", attempt, exc)

            win = self._ensure_window_visible(win)
            if self._is_wechat_foreground(hwnd):
                time.sleep(max(float(self._window_activate_delay), 0.0))
                if self._is_wechat_foreground(hwnd):
                    self._active_wechat_hwnd = hwnd
                    self._assert_calibration_ready(hwnd)
                    return

            try:
                self._request_wechat_foreground(hwnd)
            except Exception as exc:
                last_error = exc
                logger.debug("微信窗口前台请求失败（第 %d 次）: %s", attempt, exc)

            time.sleep(max(float(self._window_activate_delay), 0.05))
            if self._is_wechat_foreground(hwnd):
                self._active_wechat_hwnd = hwnd
                self._assert_calibration_ready(hwnd)
                return

        logger.error(
            "微信窗口未能切换到前台，已中止本次 GUI 操作 | hwnd=%s | error=%s",
            hwnd,
            last_error or "foreground verification failed",
        )
        raise RuntimeError("微信窗口未成功切换到前台，已中止本次发送")

    def _assert_calibration_ready(self, hwnd: int) -> None:
        """真实点击前必须确认本机坐标和当前微信版本完全匹配。"""
        current_version = get_process_file_version(hwnd)
        compatible, reason = calibration_is_compatible(
            self._calibration,
            current_version,
        )
        if compatible:
            return
        self._active_wechat_hwnd = None
        logger.error("微信点击校准不可用，已停止 GUI 操作: %s", reason)
        raise RuntimeError(f"{reason}；请先在 Weix 管理器中完成微信点击校准")

    @staticmethod
    def _window_hwnd(win) -> int | None:
        """兼容 Win32 与 pygetwindow 窗口对象的句柄字段。"""
        hwnd = getattr(win, "hwnd", None) or getattr(win, "_hWnd", None)
        try:
            return int(hwnd) if hwnd else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_wechat_foreground(hwnd: int) -> bool:
        """仅接受微信主窗口或其所属弹出窗口处于前台。"""
        try:
            import win32con
            import win32gui
            import win32process

            foreground = int(win32gui.GetForegroundWindow() or 0)
            if not foreground:
                return False
            if foreground == int(hwnd):
                return True
            root_owner = int(
                win32gui.GetAncestor(foreground, win32con.GA_ROOTOWNER) or 0
            )
            if root_owner == int(hwnd):
                return True
            _main_thread, main_pid = win32process.GetWindowThreadProcessId(hwnd)
            _foreground_thread, foreground_pid = (
                win32process.GetWindowThreadProcessId(foreground)
            )
            return bool(main_pid and foreground_pid == main_pid)
        except Exception:
            return False

    @staticmethod
    def _request_wechat_foreground(hwnd: int) -> None:
        """通过 Win32 线程输入关联请求前台，不发送任何键盘快捷键。"""
        import win32api
        import win32con
        import win32gui
        import win32process

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        current_thread = win32api.GetCurrentThreadId()
        target_thread, _target_pid = win32process.GetWindowThreadProcessId(hwnd)
        foreground = win32gui.GetForegroundWindow()
        foreground_thread = 0
        if foreground:
            foreground_thread, _foreground_pid = (
                win32process.GetWindowThreadProcessId(foreground)
            )

        attached_threads: list[int] = []
        try:
            for thread_id in (foreground_thread, target_thread):
                if (
                    thread_id
                    and thread_id != current_thread
                    and thread_id not in attached_threads
                ):
                    win32process.AttachThreadInput(current_thread, thread_id, True)
                    attached_threads.append(thread_id)

            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            try:
                win32gui.SetActiveWindow(hwnd)
            except Exception:
                # SetForegroundWindow 的结果会在调用方再次读取并严格校验。
                pass
        finally:
            for thread_id in reversed(attached_threads):
                try:
                    win32process.AttachThreadInput(current_thread, thread_id, False)
                except Exception:
                    pass

    def _assert_wechat_foreground(self, action: str) -> None:
        """鼠标操作前后校验微信前台状态，焦点变化时立即失败关闭。"""
        hwnd = self._active_wechat_hwnd
        if hwnd and self._is_wechat_foreground(hwnd):
            return
        self._active_wechat_hwnd = None
        logger.error("检测到前台焦点已离开微信，已中止 GUI 操作 | action=%s", action)
        raise RuntimeError(f"执行{action}前微信已失去前台焦点")

    def _get_active_wechat_window(self):
        """返回已验证的同一个微信主窗口，拒绝窗口句柄漂移。"""
        self._assert_wechat_foreground("读取微信窗口位置")
        win = self._find_wechat_window()
        if win is None:
            raise RuntimeError("未找到微信窗口")
        hwnd = self._window_hwnd(win)
        if not hwnd or hwnd != self._active_wechat_hwnd:
            self._active_wechat_hwnd = None
            raise RuntimeError("微信主窗口发生变化，已中止本次 GUI 操作")
        return win

    def _get_active_client_geometry(self) -> ClientGeometry:
        """每次点击前重新读取同一微信窗口的客户区，避免使用恢复前旧坐标。"""
        win = self._get_active_wechat_window()
        hwnd = self._window_hwnd(win)
        if not hwnd:
            raise RuntimeError("无法读取微信客户区")
        return get_client_geometry(hwnd)

    def _guarded_click(self, x: int, y: int, action: str) -> None:
        """只允许在微信保持前台时执行一次左键点击。"""
        self._assert_wechat_foreground(action)
        pyautogui.click(x, y)
        time.sleep(0.03)
        self._assert_wechat_foreground(action)

    def _guarded_right_click(self, x: int, y: int, action: str) -> None:
        """只允许在微信保持前台时执行一次右键点击。"""
        self._assert_wechat_foreground(action)
        pyautogui.rightClick(x, y)
        time.sleep(0.03)
        self._assert_wechat_foreground(action)

    def _ensure_window_visible(self, win):
        """把非最大化微信窗口挪回屏幕内，避免发送按钮位于屏幕外。"""
        hwnd = getattr(win, "hwnd", None)
        if not hwnd:
            return win
        try:
            import win32con
            import win32gui

            screen_w, screen_h = pyautogui.size()
            _flags, show_cmd, *_rest = win32gui.GetWindowPlacement(hwnd)
            if show_cmd == win32con.SW_SHOWMAXIMIZED:
                return win
            if win.width >= screen_w or win.height >= screen_h:
                return win

            margin = 8
            new_left = min(max(win.left, margin), max(screen_w - win.width - margin, margin))
            new_top = min(max(win.top, margin), max(screen_h - win.height - margin, margin))
            if new_left == win.left and new_top == win.top:
                return win

            win32gui.SetWindowPos(
                hwnd,
                None,
                int(new_left),
                int(new_top),
                int(win.width),
                int(win.height),
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
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
        x, y = self._calibrated_point("search_input")
        self._guarded_click(x, y, "聚焦微信搜索框")
        time.sleep(0.15)
        return x, y

    def _clear_search_input(self) -> None:
        """点击搜索框右侧清空按钮；搜索为空时该点击无副作用。"""
        x, y = self._calibrated_point("search_clear")
        self._guarded_click(x, y, "清空微信搜索框")
        time.sleep(0.15)

    def _click_first_search_result(self) -> None:
        """点击搜索结果第一项。"""
        x, y = self._calibrated_point("private_search_result")
        self._guarded_click(x, y, "选择微信搜索结果")
        time.sleep(0.15)

    def _click_group_search_result(self) -> None:
        """点击“群聊”分区里的搜索结果，避开顶部“搜索网络结果”。"""
        x, y = self._calibrated_point("group_search_result")
        self._guarded_click(x, y, "选择微信群聊搜索结果")
        time.sleep(0.15)

    def _focus_message_input(self) -> tuple[int, int]:
        """点击消息输入区域，确保光标在输入框内。"""
        x, y = self._calibrated_point("message_input")

        self._guarded_click(x, y, "聚焦微信消息输入框")
        time.sleep(0.15)
        self._guarded_click(x, y, "再次聚焦微信消息输入框")
        time.sleep(0.1)
        return x, y

    def _paste_text(self, text: str, x: int, y: int) -> None:
        """粘贴文本，不使用 Ctrl+V。"""
        self._assert_wechat_foreground("粘贴微信文本")
        pyperclip.copy(text)
        time.sleep(0.05)
        if self._paste_method == "context_menu":
            self._paste_text_via_context_menu(x, y)
            return
        raise RuntimeError("Windows 发送器只允许使用经校验的微信右键粘贴菜单")

    def _paste_text_via_context_menu(self, x: int, y: int) -> None:
        """使用微信输入框右键菜单的“粘贴”，避开快捷键和 Qt 控件 WM_PASTE 限制。"""
        before = self._wechat_popup_windows()
        self._guarded_right_click(x, y, "打开微信粘贴菜单")
        time.sleep(self._context_menu_delay)
        popup = self._wait_for_new_wechat_popup(before, (x, y))
        if popup is None:
            raise RuntimeError("未识别到属于微信的粘贴菜单，已停止发送")
        popup_geometry = ClientGeometry(
            left=popup[0],
            top=popup[1],
            width=popup[2],
            height=popup[3],
            dpi=self._get_active_client_geometry().dpi,
        )
        paste_x, paste_y = resolve_point(
            self._calibration,
            "paste_menu",
            popup_geometry,
        )
        self._guarded_click(paste_x, paste_y, "点击微信粘贴菜单")
        time.sleep(0.3)

    def _click_send_button(self) -> None:
        """点击微信输入区右下角发送按钮。"""
        x, y = self._calibrated_point("send_button")
        time.sleep(0.15)
        self._guarded_click(x, y, "点击微信发送按钮")
        time.sleep(0.3)

    def _calibrated_point(self, name: str) -> tuple[int, int]:
        """从已确认配置解析一个客户区点击点。"""
        if self._calibration is None:
            raise RuntimeError("未完成微信点击校准")
        return resolve_point(
            self._calibration,
            name,
            self._get_active_client_geometry(),
        )

    def _wechat_popup_windows(self) -> dict[int, tuple[int, int, int, int]]:
        """枚举与当前微信主窗口同进程、同根所有者的可见弹窗。"""
        hwnd = self._active_wechat_hwnd
        if not hwnd:
            return {}
        try:
            import win32gui
            import win32process

            _thread_id, main_pid = win32process.GetWindowThreadProcessId(hwnd)
            matches: dict[int, tuple[int, int, int, int]] = {}

            def enum_handler(candidate, _extra):
                if candidate == hwnd or not win32gui.IsWindowVisible(candidate):
                    return
                try:
                    _candidate_thread, candidate_pid = (
                        win32process.GetWindowThreadProcessId(candidate)
                    )
                    left, top, right, bottom = win32gui.GetWindowRect(candidate)
                except Exception:
                    return
                width = right - left
                height = bottom - top
                if (
                    candidate_pid == main_pid
                    and 30 <= width <= 600
                    and 20 <= height <= 800
                ):
                    matches[int(candidate)] = (
                        int(left),
                        int(top),
                        int(width),
                        int(height),
                    )

            win32gui.EnumWindows(enum_handler, None)
            return matches
        except Exception as exc:
            logger.debug("枚举微信粘贴菜单失败: %s", exc)
            return {}

    def _wait_for_new_wechat_popup(
        self,
        before: dict[int, tuple[int, int, int, int]],
        origin: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        deadline = time.monotonic() + max(self._context_menu_delay, 0.25) + 0.5
        while time.monotonic() <= deadline:
            self._assert_wechat_foreground("识别微信粘贴菜单")
            current = self._wechat_popup_windows()
            candidates = [
                rect
                for hwnd, rect in current.items()
                if hwnd not in before and self._popup_is_near_origin(rect, origin)
            ]
            if candidates:
                return min(candidates, key=lambda rect: rect[2] * rect[3])
            time.sleep(0.05)
        return None

    @staticmethod
    def _popup_is_near_origin(
        rect: tuple[int, int, int, int],
        origin: tuple[int, int],
    ) -> bool:
        left, top, width, height = rect
        right = left + width
        bottom = top + height
        x, y = origin
        horizontal_distance = max(left - x, x - right, 0)
        vertical_distance = max(top - y, y - bottom, 0)
        return horizontal_distance <= 80 and vertical_distance <= 80

    # --- 发送后校验 ---

    def _confirmation_is_available(self, target_id: str) -> bool:
        """校验开启时，发送前必须已有同一流水线的消息监听器。"""
        if not self._verify_after_send:
            return True
        if not target_id:
            logger.error("发送回读校验缺少目标会话 ID，已在点击前停止")
            return False
        source = self._confirmation_source
        if source is None or not getattr(source, "is_running", False):
            logger.error("消息监听器未运行，已在点击前停止发送确认")
            return False
        if not callable(getattr(source, "register_send_confirmation", None)):
            logger.error("消息监听器不支持发送确认，已在点击前停止")
            return False
        return True

    def _begin_send_confirmation(
        self,
        msg: str,
        since_ts: int,
        target_id: str,
    ):
        if not self._verify_after_send:
            return None
        return self._confirmation_source.register_send_confirmation(
            target_id,
            msg,
            since_ts,
            self._verify_timeout,
        )

    def _verify_sent_text(self, confirmation) -> bool:
        """等待现有消息监听器确认；不扫描、不打开、更不解密第二份数据库。"""
        if not self._verify_after_send:
            return True
        if confirmation is None:
            return False
        return bool(confirmation.wait(self._verify_timeout))

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
