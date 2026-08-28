"""Windows 微信 4.x 的直接 UI Automation 发送实现。

本模块只读取系统已公开的 UIA 树。它不会实例化第三方库中会写入
Weixin.dll 的 accessibility gate，也不使用 OCR、固定坐标或物理鼠标兜底。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from typing import Any, Callable

from app.config import get_config
from app.core.send_result import SendResult

logger = logging.getLogger(__name__)

MAIN_CLASS = "mmui::MainWindow"
MAIN_NAMES = {"微信", "Weixin"}
SEARCH_EDIT_CLASS = "mmui::XValidatorTextEdit"
SEARCH_EDIT_NAME = "搜索"
SESSION_LIST_AID = "session_list"
SEARCH_LIST_AID = "search_list"
RESULT_AID_PREFIX = "search_item_"
CHAT_INPUT_AID = "chat_input_field"
SECTION_HEADERS = {"联系人", "群聊", "最常使用", "最近使用", "聊天记录"}
SEND_NAMES = {"发送", "发送(S)", "Send"}

_UIA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wx-uia")


class UIAWindowError(RuntimeError):
    """带可诊断原因码的微信窗口/UIA 锚定错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _safe_attr(control: Any, name: str, default: Any = "") -> Any:
    try:
        value = getattr(control, name, default)
        return value() if callable(value) else value
    except Exception:
        return default


def _is_visible(control: Any) -> bool:
    rect = _safe_attr(control, "BoundingRectangle", None)
    if rect is None:
        return False
    try:
        return float(rect.right) > float(rect.left) and float(rect.bottom) > float(rect.top)
    except Exception:
        return False


class DirectUIAAdapter:
    """Direct uiautomation adapter with no accessibility hot activation."""

    def __init__(self) -> None:
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise RuntimeError(
                "UIA 依赖未安装，请重新安装 backend/requirements.txt"
            ) from exc
        self.auto = auto

    def main_window(self, pid: int, *, activate: bool = False) -> Any:
        """通过已绑定 PID 的 Win32 句柄锚定 UIA，不扫描 UIA Root。"""
        try:
            import win32gui
            import win32process
        except ImportError as exc:
            raise UIAWindowError("win32_unavailable", "Windows 窗口组件不可用") from exc

        candidates: list[tuple[int, int]] = []

        def enum_handler(hwnd, _extra):
            try:
                _thread_id, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
                if int(owner_pid or 0) != int(pid):
                    return True
                title = str(win32gui.GetWindowText(hwnd) or "").strip()
                if title not in MAIN_NAMES or not win32gui.IsWindowVisible(hwnd):
                    return True
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = max(0, int(right) - int(left))
                height = max(0, int(bottom) - int(top))
                if width < 400 or height < 300:
                    return True
                candidates.append((width * height, int(hwnd)))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_handler, None)
        except Exception as exc:
            raise UIAWindowError(
                "window_enumeration_failed",
                f"枚举微信窗口失败: {exc}",
            ) from exc

        candidates.sort(reverse=True)
        handles = [hwnd for _area, hwnd in candidates]
        if not handles:
            raise UIAWindowError(
                "window_not_found",
                "未找到绑定 PID 对应的可见微信主窗口；请打开微信聊天主窗口",
            )
        if len(handles) != 1:
            raise UIAWindowError(
                "ambiguous_window",
                "绑定 PID 对应多个可见微信主窗口，已拒绝选择任意窗口",
            )

        hwnd = handles[0]
        try:
            window = self.auto.ControlFromHandle(hwnd)
        except Exception as exc:
            raise UIAWindowError(
                "uia_tree_unavailable",
                "微信窗口已找到，但无法通过窗口句柄读取 UIA 树",
            ) from exc
        class_name = str(_safe_attr(window, "ClassName", "") or "")
        process_id = int(_safe_attr(window, "ProcessId", 0) or 0)
        native_hwnd = int(_safe_attr(window, "NativeWindowHandle", 0) or 0)
        if process_id != int(pid) or native_hwnd != int(hwnd):
            raise UIAWindowError(
                "uia_identity_mismatch",
                "UIA 控件所属 PID 或窗口句柄与已绑定微信不一致",
            )
        if class_name != MAIN_CLASS:
            raise UIAWindowError(
                "uia_tree_unavailable",
                f"微信窗口已找到，但 UIA 树尚未物化（当前类名: {class_name or '-'}）",
            )
        if activate:
            self.activate(window, pid)
        return window

    @staticmethod
    def activate(window: Any, pid: int) -> None:
        hwnd = int(_safe_attr(window, "NativeWindowHandle", 0) or 0)
        if not hwnd or os.name != "nt":
            raise RuntimeError("微信 UIA 主窗口没有可用句柄")
        import win32con
        import win32gui
        import win32process

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            foreground = win32gui.GetForegroundWindow()
            _thread, foreground_pid = win32process.GetWindowThreadProcessId(foreground)
            if int(foreground_pid or 0) == int(pid):
                return
            time.sleep(0.1)
        raise RuntimeError("微信没有成功切换到前台")

    @staticmethod
    def descendants(root: Any, *, limit: int = 10000) -> list[Any]:
        found: list[Any] = []
        queue = list(root.GetChildren())
        while queue and len(found) < limit:
            control = queue.pop(0)
            found.append(control)
            try:
                queue.extend(control.GetChildren())
            except Exception:
                continue
        return found

    def _unique(self, root: Any, predicate: Callable[[Any], bool], label: str) -> Any:
        matches = [item for item in self.descendants(root) if predicate(item)]
        if len(matches) != 1:
            raise RuntimeError(
                f"未找到唯一的{label}" if not matches else f"发现多个{label}"
            )
        return matches[0]

    def search_box(self, window: Any) -> Any:
        return self._unique(
            window,
            lambda item: (
                str(_safe_attr(item, "ClassName", "") or "") == SEARCH_EDIT_CLASS
                and str(_safe_attr(item, "Name", "") or "").strip() == SEARCH_EDIT_NAME
                and _is_visible(item)
            ),
            "微信搜索框",
        )

    def session_list(self, window: Any) -> Any:
        return self._unique(
            window,
            lambda item: (
                str(_safe_attr(item, "AutomationId", "") or "") == SESSION_LIST_AID
                and _is_visible(item)
            ),
            "可见会话列表",
        )

    def chat_input(self, window: Any, receiver: str) -> Any:
        return self._unique(
            window,
            lambda item: (
                str(_safe_attr(item, "AutomationId", "") or "") == CHAT_INPUT_AID
                and str(_safe_attr(item, "Name", "") or "").strip() == receiver
                and _is_visible(item)
            ),
            f"会话“{receiver}”的输入框",
        )

    @staticmethod
    def _first_line(control: Any) -> str:
        return str(_safe_attr(control, "Name", "") or "").split("\n", 1)[0].strip()

    def open_visible_session(self, window: Any, receiver: str) -> None:
        session_list = self.session_list(window)
        matches = [
            item
            for item in session_list.GetChildren()
            if self._first_line(item) == receiver and _is_visible(item)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"可见会话列表未找到目标“{receiver}”"
                if not matches
                else f"可见会话列表中目标“{receiver}”不唯一"
            )
        if not self.invoke(matches[0]):
            raise RuntimeError(f"UIA 无法打开可见会话“{receiver}”")

    def search_and_open(self, window: Any, receiver: str, is_group: bool) -> None:
        search_box = self.search_box(window)
        if not self.set_text(search_box, receiver, background=False):
            raise RuntimeError("UIA 无法写入微信搜索框")

        deadline = time.monotonic() + 2.5
        search_list = None
        while time.monotonic() < deadline:
            candidates = [
                item
                for item in self.descendants(window)
                if str(_safe_attr(item, "AutomationId", "") or "") == SEARCH_LIST_AID
                and _is_visible(item)
            ]
            if len(candidates) == 1:
                search_list = candidates[0]
                break
            if len(candidates) > 1:
                raise RuntimeError("微信搜索结果列表不唯一")
            time.sleep(0.1)
        if search_list is None:
            raise RuntimeError("微信搜索未返回结果列表")

        section = ""
        results: list[tuple[Any, str]] = []
        for item in search_list.GetChildren():
            name = self._first_line(item)
            aid = str(_safe_attr(item, "AutomationId", "") or "")
            if not aid and name in SECTION_HEADERS:
                section = name
                continue
            if aid.startswith(RESULT_AID_PREFIX) and name == receiver:
                results.append((item, section))

        expected_section = "群聊" if is_group else "联系人"
        typed = [item for item, item_section in results if item_section == expected_section]
        if len(typed) != 1:
            if not typed:
                raise RuntimeError(
                    f"搜索结果中没有名称完全一致的{expected_section}“{receiver}”"
                )
            raise RuntimeError(f"搜索结果中存在重名{expected_section}“{receiver}”")
        if not self.invoke(typed[0]):
            raise RuntimeError(f"UIA 无法打开搜索结果“{receiver}”")

    @staticmethod
    def invoke(control: Any) -> bool:
        """Invoke a non-send control once using its first supported pattern."""
        try:
            legacy = control.GetLegacyIAccessiblePattern()
            if legacy is not None and callable(getattr(legacy, "DoDefaultAction", None)):
                return bool(legacy.DoDefaultAction(waitTime=0.1))
        except Exception:
            pass
        try:
            pattern = control.GetPattern(10000)  # InvokePatternId
            if pattern is not None and callable(getattr(pattern, "Invoke", None)):
                return bool(pattern.Invoke(waitTime=0.1))
        except Exception:
            pass
        try:
            pattern = control.GetPattern(10010)  # SelectionItemPatternId
            if pattern is not None and callable(getattr(pattern, "Select", None)):
                return bool(pattern.Select(waitTime=0.1))
        except Exception:
            pass
        return False

    @staticmethod
    def set_text(control: Any, text: str, *, background: bool) -> bool:
        """Use UIA value patterns only; never use clipboard or keyboard shortcuts."""
        if not background:
            try:
                value = control.GetValuePattern()
                if value is not None and not bool(_safe_attr(value, "IsReadOnly", True)):
                    if value.SetValue(text, waitTime=0.1):
                        return True
            except Exception:
                pass
        try:
            legacy = control.GetLegacyIAccessiblePattern()
            if legacy is not None and callable(getattr(legacy, "SetValue", None)):
                return bool(legacy.SetValue(text, waitTime=0.1))
        except Exception:
            pass
        return False

    @staticmethod
    def read_text(control: Any) -> str:
        try:
            value = control.GetValuePattern()
            if value is not None:
                return str(_safe_attr(value, "Value", "") or "")
        except Exception:
            pass
        try:
            legacy = control.GetLegacyIAccessiblePattern()
            if legacy is not None:
                return str(_safe_attr(legacy, "Value", "") or "")
        except Exception:
            pass
        return ""

    def send_button(self, window: Any, input_control: Any) -> Any:
        container = input_control
        for _ in range(4):
            container = _safe_attr(container, "GetParentControl", None)
            if container is None:
                break
            matches = [
                item
                for item in self.descendants(container)
                if str(_safe_attr(item, "Name", "") or "").strip() in SEND_NAMES
                and str(_safe_attr(item, "ControlTypeName", "") or "") == "ButtonControl"
                and _is_visible(item)
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError("当前会话存在多个发送按钮")
        return self._unique(
            window,
            lambda item: (
                str(_safe_attr(item, "Name", "") or "").strip() in SEND_NAMES
                and str(_safe_attr(item, "ControlTypeName", "") or "") == "ButtonControl"
                and _is_visible(item)
            ),
            "当前会话发送按钮",
        )

    @staticmethod
    def invoke_send(button: Any) -> bool:
        """Perform exactly one foreground send default action."""
        try:
            legacy = button.GetLegacyIAccessiblePattern()
            if legacy is not None and callable(getattr(legacy, "DoDefaultAction", None)):
                return bool(legacy.DoDefaultAction(waitTime=0.2))
        except Exception:
            # 调用一旦开始就不能再尝试另一种 Pattern。
            return False
        try:
            pattern = button.GetPattern(10000)
            if pattern is not None and callable(getattr(pattern, "Invoke", None)):
                return bool(pattern.Invoke(waitTime=0.2))
        except Exception:
            pass
        return False

    @staticmethod
    def post_send(window: Any, button: Any) -> bool:
        """Post one click pair to the exact UIA-confirmed button."""
        if os.name != "nt":
            return False
        hwnd = int(_safe_attr(window, "NativeWindowHandle", 0) or 0)
        rect = _safe_attr(button, "BoundingRectangle", None)
        if not hwnd or rect is None:
            return False
        point = wintypes.POINT(
            int((float(rect.left) + float(rect.right)) / 2),
            int((float(rect.top) + float(rect.bottom)) / 2),
        )
        user32 = ctypes.windll.user32
        if not user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point)):
            return False
        if not (0 <= point.x <= 0x7FFF and 0 <= point.y <= 0x7FFF):
            return False
        lparam = (int(point.y) << 16) | (int(point.x) & 0xFFFF)
        down = user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
        time.sleep(0.03)
        up = user32.PostMessageW(hwnd, 0x0202, 0, lparam)
        return bool(down and up)

    @staticmethod
    def input_state() -> tuple[int, int, int, int]:
        if os.name != "nt":
            return (0, 0, 0, 0)
        user32 = ctypes.windll.user32
        foreground = int(user32.GetForegroundWindow() or 0)
        thread_id = int(user32.GetWindowThreadProcessId(foreground, None) or 0)
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        focus = int(info.hwndFocus or 0) if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) else 0
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return foreground, focus, int(point.x), int(point.y)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class WindowsUIASender:
    """Background-first UIA sender with strict pre-action fallback rules."""

    def __init__(
        self,
        adapter_factory: Callable[[], Any] = DirectUIAAdapter,
        binding_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._adapter: Any = None
        self._binding_provider = binding_provider
        self._last_result: SendResult | None = None
        self.refresh_policy()

    def refresh_policy(self) -> None:
        cfg = get_config().windows_sender if hasattr(get_config(), "windows_sender") else {}
        mode = str(cfg.get("send_mode", "auto") or "auto").strip().lower()
        self._send_mode = mode if mode in {"auto", "background", "foreground"} else "auto"
        self._background_post_message = bool(cfg.get("background_post_message", True))
        self._allow_foreground_activation = bool(cfg.get("allow_foreground_activation", True))

    def _binding(self) -> dict[str, Any]:
        if self._binding_provider is not None:
            return dict(self._binding_provider())
        try:
            from app.core.platform import Platform

            extractor = Platform.get().key_extractor
            selected = extractor.selected_account() if hasattr(extractor, "selected_account") else ""
            return {
                "selected_account": str(selected or ""),
                "bound_account": str(getattr(extractor, "bound_account", "") or ""),
                "bound_pid": getattr(extractor, "bound_pid", None),
            }
        except Exception as exc:
            return {"error": str(exc), "bound_pid": None, "bound_account": ""}

    @staticmethod
    def _validate_binding(binding: dict[str, Any]) -> tuple[bool, str]:
        selected = str(binding.get("selected_account", "") or "").casefold()
        account = str(binding.get("bound_account", "") or "").casefold()
        if not binding.get("bound_pid"):
            return False, "未将微信账号唯一绑定到 Weixin.exe 主进程"
        if not account:
            return False, "无法确认已绑定微信进程所属账号"
        if selected and selected != account:
            return False, "所选微信账号与已绑定进程账号不一致"
        return True, ""

    def _get_adapter(self) -> Any:
        if self._adapter is None:
            self._adapter = self._adapter_factory()
        return self._adapter

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_UIA_EXECUTOR, self._com_call, func, args)

    @staticmethod
    def _com_call(func: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        initialized = False
        try:
            import pythoncom

            pythoncom.CoInitialize()
            initialized = True
        except ImportError:
            pythoncom = None
        try:
            return func(*args)
        finally:
            if initialized and pythoncom is not None:
                pythoncom.CoUninitialize()

    async def prewarm(self) -> dict[str, Any]:
        return await self.diagnose()

    async def diagnose(self) -> dict[str, Any]:
        return await self._run(self._diagnose_sync)

    def _diagnose_sync(self) -> dict[str, Any]:
        binding = self._binding()
        valid, reason = self._validate_binding(binding)
        result: dict[str, Any] = {
            **binding,
            "available": False,
            "main_window": False,
            "search_box": False,
            "session_list": False,
            "chat_input": False,
            "send_button": False,
            "reason": reason,
            "reason_code": "account_binding_unavailable" if reason else "",
            "narrator_hint": False,
        }
        if not valid:
            return result
        try:
            adapter = self._get_adapter()
            window = adapter.main_window(int(binding["bound_pid"]), activate=False)
            result["main_window"] = True
            try:
                adapter.search_box(window)
                result["search_box"] = True
            except Exception:
                pass
            try:
                adapter.session_list(window)
                result["session_list"] = True
            except Exception:
                pass
            inputs = [
                item
                for item in adapter.descendants(window)
                if str(_safe_attr(item, "AutomationId", "") or "") == CHAT_INPUT_AID
                and _is_visible(item)
            ]
            result["chat_input"] = len(inputs) == 1
            if len(inputs) == 1:
                try:
                    adapter.send_button(window, inputs[0])
                    result["send_button"] = True
                except Exception:
                    pass
            result["available"] = all(
                result[key]
                for key in ("main_window", "search_box", "session_list", "chat_input", "send_button")
            )
            if result["available"]:
                result["reason"] = "UIA 关键控件已就绪"
                result["reason_code"] = ""
            else:
                result["reason"] = "微信 UIA 树缺少关键控件"
                result["reason_code"] = "uia_controls_missing"
                result["narrator_hint"] = True
        except UIAWindowError as exc:
            result["reason"] = str(exc)
            result["reason_code"] = exc.code
            result["narrator_hint"] = exc.code == "uia_tree_unavailable"
            if exc.code == "window_not_found":
                result["help"] = "请打开完整的微信聊天主窗口并保持可见，然后重新检测。"
        except Exception as exc:
            result["reason"] = str(exc)
            result["reason_code"] = "uia_diagnose_failed"
        return result

    async def send_text_result(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
    ) -> SendResult:
        result = await self._run(
            self._send_text_sync_result,
            msg,
            receiver,
            is_group,
            target_id,
        )
        self._last_result = result
        return result

    def _send_text_sync_result(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
    ) -> SendResult:
        if not msg or not receiver or not target_id:
            return SendResult.for_message(msg, target_id, self._send_mode).fail(
                "draft", "invalid_request", "消息、接收者或目标会话 ID 为空"
            )
        modes = [self._send_mode]
        if self._send_mode == "auto":
            modes = ["background", "foreground"]
        last: SendResult | None = None
        for mode in modes:
            result = self._attempt(msg, receiver, is_group, target_id, mode)
            last = result
            if result.action_performed or result.status == "pending_verify":
                return result
            if mode == "background" and result.error_code == "background_state_changed":
                return result
        return last or SendResult.for_message(msg, target_id, self._send_mode).fail(
            "window", "uia_unavailable", "UIA 发送不可用"
        )

    def _attempt(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        mode: str,
    ) -> SendResult:
        result = SendResult.for_message(msg, target_id, mode)
        binding = self._binding()
        valid, reason = self._validate_binding(binding)
        if not valid:
            return result.fail("window", "account_binding_unavailable", reason)
        if mode == "background" and not self._background_post_message:
            return result.fail("invoke", "background_disabled", "后台发送按钮消息已关闭")
        if mode == "foreground" and not self._allow_foreground_activation:
            return result.fail("window", "foreground_disabled", "前台 UIA 回退已关闭")

        try:
            adapter = self._get_adapter()
            background = mode == "background"
            state = adapter.input_state() if background else None
            window = adapter.main_window(int(binding["bound_pid"]), activate=not background)
            if background:
                adapter.open_visible_session(window, receiver)
            else:
                adapter.search_and_open(window, receiver, is_group)

            deadline = time.monotonic() + 2.0
            input_control = None
            while time.monotonic() < deadline:
                try:
                    input_control = adapter.chat_input(window, receiver)
                    break
                except Exception:
                    time.sleep(0.1)
            if input_control is None:
                return result.fail("draft", "chat_verification_failed", "会话标题或输入框核对失败")
            if background and adapter.input_state() != state:
                return result.fail(
                    "window", "background_state_changed", "后台操作期间前台、焦点或鼠标发生变化"
                )
            if not adapter.set_text(input_control, msg, background=background):
                return result.fail("draft", "draft_write_failed", "UIA 无法写入消息正文")
            if adapter.read_text(input_control) != msg:
                return result.fail("draft", "draft_verify_failed", "UIA 输入正文核对失败")
            button = adapter.send_button(window, input_control)
            if background and adapter.input_state() != state:
                return result.fail(
                    "window", "background_state_changed", "后台操作期间前台、焦点或鼠标发生变化"
                )

            # 从这里开始，即使系统调用返回失败也视为动作可能已执行，禁止回退或补发。
            result.action_performed = True
            performed = (
                adapter.post_send(window, button)
                if background
                else adapter.invoke_send(button)
            )
            if not performed:
                return result.pending(
                    "invoke", "send_action_uncertain", "发送动作结果不确定，已禁止重试"
                )
            if background and adapter.input_state() != state:
                return result.pending(
                    "invoke", "background_state_changed", "发送动作后用户输入状态发生变化"
                )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if not adapter.read_text(input_control):
                    result.ui_verified = True
                    break
                time.sleep(0.1)
            return result.pending("db_verify", "awaiting_db_confirmation", "等待消息数据库确认")
        except UIAWindowError as exc:
            return result.fail("window", exc.code, str(exc))
        except Exception as exc:
            return result.fail("window", "uia_pre_send_failed", str(exc))

    async def open_chat(self, receiver: str, is_group: bool = False) -> bool:
        return bool(await self._run(self._open_chat_sync, receiver, is_group))

    def _open_chat_sync(self, receiver: str, is_group: bool) -> bool:
        binding = self._binding()
        valid, _reason = self._validate_binding(binding)
        if not valid:
            return False
        adapter = self._get_adapter()
        modes = [self._send_mode]
        if self._send_mode == "auto":
            modes = ["background", "foreground"]
        for mode in modes:
            try:
                background = mode == "background"
                if not background and not self._allow_foreground_activation:
                    continue
                window = adapter.main_window(int(binding["bound_pid"]), activate=not background)
                if background:
                    adapter.open_visible_session(window, receiver)
                else:
                    adapter.search_and_open(window, receiver, is_group)
                adapter.chat_input(window, receiver)
                return True
            except Exception:
                continue
        return False

    @property
    def last_result(self) -> SendResult | None:
        return self._last_result
