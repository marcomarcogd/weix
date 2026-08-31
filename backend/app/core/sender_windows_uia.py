"""Windows 微信 UI Automation 消息发送器。

优先通过 wechatauto-replica 提供的 UIA 驱动操作微信控件，避免移动真实鼠标。
该驱动针对微信 4.1.12+ 的自绘界面，会在必要时热激活 Qt accessibility gate，
但不会自动降级到坐标/OCR；鼠标兜底由上层配置显式决定。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from typing import Any

from app.config import get_config
from app.core.send_result import SendResult

logger = logging.getLogger(__name__)

_UIA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wx-uia")
_PYWIN32_DLL_HANDLES: list[object] = []


def _window_pid_from_control(driver: Any, control: Any) -> int | None:
    """Resolve a UIA control's owning process without activating it."""
    for attribute in ("ProcessId", "process_id"):
        try:
            value = getattr(control, attribute, None)
            if callable(value):
                value = value()
            pid = int(value or 0)
            if pid > 0:
                return pid
        except (TypeError, ValueError, AttributeError):
            pass

    try:
        hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if not hwnd:
        return None

    resolver = getattr(driver, "_pid_from_hwnd", None)
    if callable(resolver):
        try:
            pid = int(resolver(hwnd) or 0)
            if pid > 0:
                return pid
        except (TypeError, ValueError, OSError):
            pass

    if os.name != "nt":
        return None
    try:
        pid_value = wintypes.DWORD()
        if not ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid_value)
        ):
            return None
        return int(pid_value.value) or None
    except (AttributeError, OSError):
        return None


def _prepare_windows_imports() -> None:
    """Make pywin32 importable from the bundled/embedded Python runtime."""
    if os.name != "nt":
        return

    site_dirs: list[Path] = []
    for raw_path in tuple(sys.path):
        if raw_path:
            candidate = Path(raw_path)
            if candidate.name.casefold() == "site-packages":
                site_dirs.append(candidate)

    for site_dir in site_dirs:
        dll_dir = site_dir / "pywin32_system32"
        if dll_dir.is_dir():
            try:
                handle = os.add_dll_directory(str(dll_dir))
                _PYWIN32_DLL_HANDLES.append(handle)
            except (AttributeError, OSError):
                pass

        for relative in ("win32", "win32/lib", "pythonwin"):
            module_dir = site_dir / relative
            if module_dir.is_dir() and str(module_dir) not in sys.path:
                sys.path.insert(0, str(module_dir))


class _SelectedWeChatUIA:
    """Bind the third-party UIA driver to the account-selected Weixin process."""

    def __init__(self, target_pid: int | None):
        _prepare_windows_imports()
        try:
            from wechatauto.uia_driver import WeChatUIA
        except ImportError as exc:  # pragma: no cover - dependency install issue
            raise RuntimeError(
                "UIA 发送依赖未安装，请执行: pip install wechatauto-replica"
            ) from exc

        class BoundDriver(WeChatUIA):
            def _wechat_hwnds(self):
                handles = super()._wechat_hwnds()
                if target_pid is None:
                    return []
                return [
                    handle
                    for handle in handles
                    if self._pid_from_hwnd(handle) == target_pid
                ]

        self.driver = BoundDriver()


class WindowsUIASender:
    """Send Windows WeChat text without pyautogui or physical mouse movement."""

    def __init__(self):
        _prepare_windows_imports()
        self._driver: Any = None
        self._driver_pid: int | None = None
        self._driver_lock = threading.Lock()
        win_cfg = get_config().windows_sender if hasattr(get_config(), "windows_sender") else {}
        configured_mode = str(win_cfg.get("send_mode", "") or "").strip().lower()
        configured_mode = {
            "background": "background_uia",
            "foreground": "foreground_uia",
        }.get(configured_mode, configured_mode)
        if configured_mode not in {"foreground_uia", "background_uia", "auto"}:
            configured_mode = (
                "background_uia"
                if bool(win_cfg.get("background_mode", False))
                else "foreground_uia"
            )
        self._send_mode = configured_mode
        self._background_mode = configured_mode == "background_uia"
        self._allow_foreground_activation = bool(
            win_cfg.get("allow_foreground_activation", False)
        )
        self._send_key_fallback = str(
            win_cfg.get("send_key_fallback", "none") or "none"
        ).strip().lower()
        if self._send_key_fallback not in {"none", "enter", "ctrl_enter"}:
            self._send_key_fallback = "none"
        # Some Weixin 4.x builds expose Invoke/Legacy actions that report
        # success without activating the custom Qt button.  This explicit
        # fallback focuses the UIA-discovered button and invokes its default
        # keyboard action; it is foreground-only and never uses coordinates.
        self._send_button_key_fallback = str(
            win_cfg.get("send_button_key_fallback", "none") or "none"
        ).strip().lower()
        if self._send_button_key_fallback not in {"none", "enter", "ctrl_enter"}:
            self._send_button_key_fallback = "none"
        self._input_verify_timeout = float(win_cfg.get("input_verify_timeout", 3.0))
        self._ui_verify_timeout = float(win_cfg.get("ui_verify_timeout", 4.0))
        self._require_ui_verify = bool(win_cfg.get("require_ui_verify", True))
        try:
            self._uia_search_retries = max(1, int(win_cfg.get("uia_search_retries", 2)))
        except (TypeError, ValueError):
            self._uia_search_retries = 2
        try:
            self._uia_search_settle = max(
                0.5, float(win_cfg.get("uia_search_settle", 1.0))
            )
        except (TypeError, ValueError):
            self._uia_search_settle = 1.0
        self._background_post_message = bool(
            win_cfg.get("background_post_message", False)
        )
        self._hot_activate_accessibility = bool(
            win_cfg.get("hot_activate_accessibility", False)
        )
        self._ensure_full_layout = bool(win_cfg.get("ensure_full_layout", True))
        self._last_result: SendResult | None = None
        self._driver_account = ""
        self._last_binding_error: dict[str, Any] | None = None
        self._last_navigation_error: dict[str, Any] | None = None
        self._last_background_capability: dict[str, Any] = {}
        self._background_attempts = 1
        self._foreground_attempts = 1
        self._refresh_send_policy()

    @staticmethod
    def _attempt_limit(value: Any, default: int = 1) -> int:
        """Keep retry counts bounded so a bad config cannot loop indefinitely."""
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return default

    def _refresh_send_policy(self) -> None:
        """Reload the live send policy without rebuilding the UIA driver."""
        win_cfg = get_config().windows_sender if hasattr(get_config(), "windows_sender") else {}
        configured_mode = str(win_cfg.get("send_mode", "") or "").strip().lower()
        configured_mode = {
            "background": "background_uia",
            "foreground": "foreground_uia",
        }.get(configured_mode, configured_mode)
        if configured_mode not in {"foreground_uia", "background_uia", "auto"}:
            configured_mode = (
                "background_uia"
                if bool(win_cfg.get("background_mode", False))
                else "foreground_uia"
            )
        self._send_mode = configured_mode
        self._background_mode = configured_mode == "background_uia"
        self._allow_foreground_activation = bool(
            win_cfg.get("allow_foreground_activation", False)
        )
        self._background_post_message = bool(
            win_cfg.get("background_post_message", False)
        )
        self._hot_activate_accessibility = bool(
            win_cfg.get("hot_activate_accessibility", False)
        )
        self._send_key_fallback = str(
            win_cfg.get("send_key_fallback", "none") or "none"
        ).strip().lower()
        if self._send_key_fallback not in {"none", "enter", "ctrl_enter"}:
            self._send_key_fallback = "none"
        self._send_button_key_fallback = str(
            win_cfg.get("send_button_key_fallback", "none") or "none"
        ).strip().lower()
        if self._send_button_key_fallback not in {"none", "enter", "ctrl_enter"}:
            self._send_button_key_fallback = "none"
        self._background_attempts = self._attempt_limit(
            win_cfg.get("background_attempts", 1)
        )
        self._foreground_attempts = self._attempt_limit(
            win_cfg.get("foreground_attempts", 1)
        )

    def refresh_send_policy(self) -> None:
        """Public hook used by the management API after saving sender settings."""
        self._refresh_send_policy()

    async def send_text(
        self,
        msg: str,
        receiver: str,
        is_group: bool = False,
        target_id: str = "",
        attempt_id: str = "",
    ) -> bool:
        result = await self.send_text_result(msg, receiver, is_group, target_id, attempt_id)
        return result.success

    async def send_text_result(
        self,
        msg: str,
        receiver: str,
        is_group: bool = False,
        target_id: str = "",
        attempt_id: str = "",
    ) -> SendResult:
        """Send text and retain stage-level UIA diagnostics."""
        method = self._send_mode
        result = SendResult.for_message(msg, target_id or receiver, method, attempt_id)
        if not msg or not receiver:
            result.fail("draft", "invalid_request", "消息内容或接收者为空")
            self._last_result = result
            return result
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _UIA_EXECUTOR,
            self._send_text_sync_result,
            msg,
            receiver,
            is_group,
            target_id,
            attempt_id,
        )
        self._last_result = result
        return result

    @property
    def last_result(self) -> SendResult | None:
        return self._last_result

    async def diagnose(self) -> dict[str, Any]:
        """Probe the selected account and UIA controls without sending."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_UIA_EXECUTOR, self._diagnose_sync)

    async def activate_accessibility(self) -> dict[str, Any]:
        """Materialize the configured UIA tree without activating the window."""
        payload = await self.diagnose()
        available = bool(payload.get("uia_available"))
        return {
            "ok": available,
            "status": "already_ready" if available else "unavailable",
            "reason": payload.get("error") or (
                "微信 UIA 关键控件已就绪" if available else "微信 UIA 不可用"
            ),
        }

    async def is_wechat_running(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_UIA_EXECUTOR, self._is_running_sync)

    async def open_chat(self, receiver: str, is_group: bool = False) -> bool:
        if not receiver:
            return False
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _UIA_EXECUTOR,
            self._open_chat_sync,
            receiver,
            is_group,
        )

    def _get_bound_pid(self) -> int | None:
        try:
            from app.core.platform import Platform

            pid = Platform.get().key_extractor.bound_pid
            return int(pid) if pid else None
        except Exception:
            return None

    def _binding_info(self) -> dict[str, Any]:
        """Return the selected-account/PID binding state used by UIA."""
        info: dict[str, Any] = {
            "selected_account": "",
            "bound_account": "",
            "bound_pid": None,
            "status": "ambiguous_process",
            "error_code": "ambiguous_process",
            "error_message": "没有确认所选微信账号对应的主进程 PID，拒绝选择任意窗口",
        }
        try:
            from app.core.platform import Platform

            extractor = Platform.get().key_extractor
            selected = (
                extractor.selected_account()
                if hasattr(extractor, "selected_account")
                else ""
            )
            bound_account = str(getattr(extractor, "bound_account", "") or "")
            bound_pid = getattr(extractor, "bound_pid", None)
            info["selected_account"] = str(selected or "")
            info["bound_account"] = bound_account
            info["bound_pid"] = int(bound_pid) if bound_pid else None
        except Exception as exc:
            info.update(
                status="binding_unavailable",
                error_code="binding_unavailable",
                error_message=f"无法读取微信账号绑定状态: {exc}",
            )
            return info

        selected = str(info["selected_account"] or "").casefold()
        bound_account = str(info["bound_account"] or "").casefold()
        if not info["bound_pid"]:
            return info
        if selected and bound_account and selected != bound_account:
            return {
                **info,
                "status": "account_binding_mismatch",
                "error_code": "account_binding_mismatch",
                "error_message": (
                    f"所选账号 {info['selected_account']} 与已绑定账号 "
                    f"{info['bound_account']} 不一致"
                ),
            }
        if selected and not bound_account:
            return {
                **info,
                "status": "account_binding_unverified",
                "error_code": "account_binding_unverified",
                "error_message": "已找到微信进程 PID，但无法确认它属于所选账号",
            }
        return {
            **info,
            "status": "bound",
            "error_code": "",
            "error_message": "",
        }

    def _get_driver(self):
        binding = self._binding_info()
        target_pid = binding.get("bound_pid") or self._get_bound_pid()
        target_account = str(
            binding.get("bound_account") or binding.get("selected_account") or ""
        ).casefold()
        with self._driver_lock:
            if (
                self._driver is None
                or self._driver_pid != target_pid
                or self._driver_account != target_account
            ):
                self._driver = _SelectedWeChatUIA(target_pid).driver
                self._driver_pid = target_pid
                self._driver_account = target_account
            return self._driver

    def _is_running_sync(self) -> bool:
        _prepare_windows_imports()
        try:
            if self._binding_info()["status"] != "bound":
                return False
            driver = self._get_driver()
            return driver._find_main() is not None
        except Exception as exc:
            logger.debug("UIA 检查微信进程失败: %s", exc)
            return False

    def _ensure_driver_window(self, method: str | None = None):
        binding = self._binding_info()
        if binding["status"] != "bound":
            self._last_binding_error = binding
            logger.error("UIA 账号绑定不可用 | code=%s | message=%s", binding["error_code"], binding["error_message"])
            return None

        self._last_binding_error = None
        driver = self._get_driver()
        background = method == "background_uia" if method else self._background_mode
        if background:
            # wechatauto.ensure_window() deliberately activates the window. In
            # background mode, never call it. If explicitly enabled, use the
            # driver's process-memory gate wake, which does not show or focus
            # the window and does not inject keyboard or mouse input.
            window = driver._find_main()
            if window is None and self._hot_activate_accessibility:
                before = self._foreground_input_state()
                wake = getattr(driver, "_wake_accessibility", None)
                woke = bool(wake()) if callable(wake) else False
                after = self._foreground_input_state()
                if before != after:
                    self._last_binding_error = {
                        "status": "background_input_state_changed",
                        "error_code": "background_input_state_changed",
                        "error_message": "后台 UIA 热激活期间前台、焦点或鼠标状态发生变化",
                    }
                    return None
                if woke:
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        window = driver._find_main()
                        if window is not None:
                            logger.info("后台 UIA 无障碍树已完成热激活")
                            break
                        time.sleep(0.1)
            if window is None:
                logger.error("后台 UIA 未找到可访问的微信主窗口，不切换前台")
                return None
            driver._win = window
            if not self._window_matches_bound_pid(driver, window, binding["bound_pid"]):
                return None
            return driver
        if not driver.ensure_window():
            logger.error("UIA 未找到可访问的微信主窗口")
            return None
        if not self._window_matches_bound_pid(driver, driver._win, binding["bound_pid"]):
            return None
        return driver

    def _window_matches_bound_pid(self, driver: Any, window: Any, target_pid: int) -> bool:
        actual_pid = _window_pid_from_control(driver, window)
        if actual_pid != int(target_pid):
            self._last_binding_error = {
                "status": "window_pid_mismatch",
                "error_code": "window_pid_mismatch",
                "error_message": (
                    f"UIA 窗口 PID {actual_pid or '-'} 与绑定 PID {target_pid} 不一致"
                ),
                "bound_pid": target_pid,
                "window_pid": actual_pid,
            }
            logger.error(self._last_binding_error["error_message"])
            return False
        return True

    @staticmethod
    def _supports_legacy_value(control: Any) -> bool:
        try:
            pattern = control.GetLegacyIAccessiblePattern()
            return pattern is not None and callable(getattr(pattern, "SetValue", None))
        except Exception:
            return False

    @staticmethod
    def _supports_invoke(control: Any) -> bool:
        try:
            legacy = control.GetLegacyIAccessiblePattern()
            if legacy is not None and callable(getattr(legacy, "DoDefaultAction", None)):
                return True
        except Exception:
            pass
        try:
            import uiautomation as auto

            for pattern_id in (
                auto.PatternId.InvokePattern,
                auto.PatternId.SelectionItemPattern,
            ):
                if control.GetPattern(pattern_id) is not None:
                    return True
        except Exception:
            pass
        return False

    def _probe_background_capability(self) -> dict[str, Any]:
        """Check background-only patterns before attempting any send action."""
        capability: dict[str, Any] = {
            "available": False,
            "method": "background_uia",
            "reason_code": "",
            "reason": "",
            "window": None,
            "session_list": False,
            "search_box": None,
            "chat_input": None,
            "send_button": None,
        }
        binding = self._binding_info()
        if binding["status"] != "bound":
            capability.update(
                reason_code=binding["error_code"],
                reason=binding["error_message"],
            )
            self._last_background_capability = capability
            return capability

        driver = self._ensure_driver_window("background_uia")
        if driver is None:
            binding_error = self._last_binding_error or {}
            capability.update(
                reason_code=binding_error.get("error_code", "background_window_unavailable"),
                reason=binding_error.get("error_message", "后台 UIA 未找到绑定窗口"),
            )
            self._last_background_capability = capability
            return capability

        window = driver._win
        hwnd = int(getattr(window, "NativeWindowHandle", 0) or 0)
        capability["window"] = {
            "hwnd": hwnd,
            "pid": _window_pid_from_control(driver, window),
            "class_name": str(getattr(window, "ClassName", "") or ""),
        }
        missing: list[str] = []
        try:
            session_list = window.ListControl(AutomationId="session_list")
            capability["session_list"] = bool(session_list.Exists(0.2, 0.1))
        except Exception:
            capability["session_list"] = False
        if not capability["session_list"]:
            missing.append("session_list")

        try:
            search_box = driver._search_box(window)
        except Exception:
            search_box = None
        capability["search_box"] = (
            {
                "patterns": self._pattern_summary(search_box),
                "legacy_value": self._supports_legacy_value(search_box),
            }
            if search_box
            else None
        )
        if search_box is None or not self._supports_legacy_value(search_box):
            missing.append("search_box_legacy_value")

        try:
            input_control = driver._chat_input(window)
        except Exception:
            input_control = None
        capability["chat_input"] = (
            {
                "patterns": self._pattern_summary(input_control),
                "legacy_value": self._supports_legacy_value(input_control),
            }
            if input_control
            else None
        )
        if input_control is None or not self._supports_legacy_value(input_control):
            missing.append("chat_input_legacy_value")

        button = self._find_send_button(driver, input_control) if input_control else None
        capability["send_button"] = (
            {
                "name": str(getattr(button, "Name", "") or ""),
                "patterns": self._pattern_summary(button),
                "invokable": self._supports_invoke(button),
            }
            if button
            else None
        )
        if button is None or not self._supports_invoke(button):
            missing.append("send_button_invoke")

        if self._require_ui_verify:
            try:
                if driver._message_list() is None:
                    missing.append("message_list")
            except Exception:
                missing.append("message_list")

        if missing:
            capability.update(
                reason_code="background_patterns_incomplete",
                reason="后台 UIA 缺少必要控件或 Pattern: " + ", ".join(missing),
            )
        else:
            capability["available"] = True
        self._last_background_capability = capability
        return capability

    @staticmethod
    def _invoke_without_mouse(control: Any) -> bool:
        """Invoke/select a UIA control without uiautomation.Control.Click()."""
        try:
            import uiautomation as auto

            legacy_pattern = control.GetLegacyIAccessiblePattern()
            if legacy_pattern is not None and legacy_pattern.DoDefaultAction(waitTime=0.1):
                return True
            invoke_pattern = control.GetPattern(auto.PatternId.InvokePattern)
            if invoke_pattern is not None and invoke_pattern.Invoke(waitTime=0.1):
                return True
            selection_pattern = control.GetPattern(auto.PatternId.SelectionItemPattern)
            if selection_pattern is not None and selection_pattern.Select(waitTime=0.1):
                return True
        except Exception as exc:
            logger.debug("UIA 无鼠标调用控件失败: %s", exc)
        return False

    @staticmethod
    def _select_session_without_mouse(control: Any) -> bool:
        """Select a visible session item without invoking its default action."""
        try:
            import uiautomation as auto

            selection_pattern = control.GetPattern(auto.PatternId.SelectionItemPattern)
            if selection_pattern is None:
                return False
            return bool(selection_pattern.Select(waitTime=0.1))
        except Exception as exc:
            logger.debug("UIA 后台选择可见会话失败: %s", exc)
            return False

    @staticmethod
    def _invoke_session_without_mouse(control: Any) -> bool:
        """Invoke a visible session item without Legacy actions or mouse input."""
        try:
            import uiautomation as auto

            invoke_pattern = control.GetPattern(auto.PatternId.InvokePattern)
            if invoke_pattern is None:
                return False
            return bool(invoke_pattern.Invoke(waitTime=0.1))
        except Exception as exc:
            logger.debug("UIA 后台激活可见会话失败: %s", exc)
            return False

    @staticmethod
    def _set_text_without_mouse(
        driver: Any,
        control: Any,
        text: str,
        allow_focus_fallback: bool = True,
        prefer_focus_fallback: bool = False,
    ) -> bool:
        """Set an edit control through ValuePattern, optionally using focus."""
        if not allow_focus_fallback:
            try:
                legacy_pattern = control.GetLegacyIAccessiblePattern()
                if legacy_pattern is not None and legacy_pattern.SetValue(text, waitTime=0.1):
                    return True
            except Exception:
                pass
            logger.debug("UIA LegacyIAccessiblePattern 写入失败，后台模式拒绝其他回退")
            return False

        if not prefer_focus_fallback:
            try:
                value_pattern = control.GetValuePattern()
                if value_pattern is not None and not value_pattern.IsReadOnly:
                    if value_pattern.SetValue(text, waitTime=0.1):
                        return True
            except Exception:
                pass

        try:
            import uiautomation as auto

            if not control.SetFocus():
                return False
            auto.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            driver._clip_set(text)
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            return True
        except Exception as exc:
            logger.debug("UIA 无鼠标写入文本失败: %s", exc)
        return False

    @staticmethod
    def _is_searchable_identifier(value: Any) -> bool:
        """Return whether a value is suitable for Weixin's visible search box."""
        text = str(value or "").strip().casefold()
        if not text:
            return False
        if text.startswith("wxid_"):
            return False
        if text.endswith("@chatroom"):
            return False
        return True

    def _collect_search_results_with_retry(
        self,
        driver: Any,
        search_box: Any,
        keyword: str,
        is_group: bool,
        background: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Retry transient empty/invalid UIA search snapshots without clicking."""
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self._uia_search_retries + 1):
            written = self._set_text_without_mouse(
                driver,
                search_box,
                keyword,
                allow_focus_fallback=not background,
            )
            if not written:
                attempts.append({"attempt": attempt, "written": False, "count": 0})
                continue

            time.sleep(0.8 if attempt == 1 else 0.4)
            try:
                results = driver._collect_results(
                    keyword,
                    settle=self._uia_search_settle,
                )
            except Exception as exc:
                logger.debug(
                    "UIA 搜索结果快照读取失败 | keyword=%s | attempt=%s | error=%s",
                    keyword,
                    attempt,
                    exc,
                )
                results = []

            if is_group:
                group_results = [
                    item for item in results
                    if (item.get("section") or "") == "群聊"
                ]
                if group_results:
                    results = group_results
                else:
                    results = [
                        item for item in results
                        if item.get("name") == keyword
                        and (item.get("section") or "") in {"最常使用", "最近使用"}
                    ]

            attempts.append({
                "attempt": attempt,
                "written": True,
                "count": len(results),
            })
            if results:
                return results, attempts

        return [], attempts

    def _open_chat_without_mouse(
        self,
        driver: Any,
        keyword: str,
        is_group: bool,
        background_mode: bool | None = None,
    ) -> bool:
        background = self._background_mode if background_mode is None else background_mode
        self._last_navigation_error = None
        if background:
            return self._open_visible_session_without_mouse(driver, keyword)

        driver = self._ensure_foreground_navigation(driver)
        if driver is None:
            return False
        search_box = driver._search_box(driver._win)
        if search_box is None:
            return False

        try:
            keywords = driver._resolve_search_keyword(keyword)
        except Exception:
            keywords = [keyword]
        results = []
        used_keyword = keyword
        search_attempts: list[dict[str, Any]] = []
        for candidate in keywords:
            candidate_results, candidate_attempts = (
                self._collect_search_results_with_retry(
                    driver,
                    search_box,
                    candidate,
                    is_group,
                    background=background,
                )
            )
            search_attempts.extend(
                {"keyword": candidate, **attempt}
                for attempt in candidate_attempts
            )
            results = candidate_results
            if results:
                used_keyword = candidate
                break

        if not results:
            self._last_navigation_error = {
                "error_code": "search_result_not_found",
                "error_message": f"搜索未找到目标会话: {used_keyword}",
                "searched_keywords": list(keywords),
                "search_attempts": search_attempts,
            }
            return False

        exact = [item for item in results if item.get("name") == used_keyword]
        if len(exact) > 1 or (len(results) > 1 and not exact):
            logger.error(
                "UIA 搜索结果不唯一 | keyword=%s | count=%s",
                used_keyword,
                len(results),
            )
            self._last_navigation_error = {
                "error_code": "ambiguous_search_result",
                "error_message": f"搜索结果不唯一: {used_keyword}",
                "count": len(results),
            }
            return False
        chosen = (exact or results)[0]
        if not self._invoke_without_mouse(chosen["cell"]):
            self._last_navigation_error = {
                "error_code": "search_result_invoke_failed",
                "error_message": f"无法打开搜索结果: {used_keyword}",
            }
            return False
        expected_names = {chosen.get("name"), used_keyword}
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            current_name = driver.current_chat()
            if current_name and current_name in expected_names and driver._chat_input() is not None:
                return True
            time.sleep(0.2)
        self._last_navigation_error = {
            "error_code": "chat_open_verification_failed",
            "error_message": f"目标会话已调用打开动作，但标题或输入框校验失败: {used_keyword}",
        }
        return False

    def _open_visible_session_without_mouse(self, driver: Any, keyword: str) -> bool:
        """Open a visible recent session through UIA without search or focus."""
        try:
            session_list = driver._win.ListControl(AutomationId="session_list")
            if not session_list.Exists(0.5, 0.1):
                return False
        except Exception:
            return False

        # The pipeline already resolved target_id to the exact display name.
        # Calling the upstream DB resolver here reloads every WeChat database
        # and adds several seconds before a visible-session click, while not
        # improving the exact-name/AutomationId safety check below.
        candidates = {keyword.strip()}

        try:
            sessions = session_list.GetChildren()
        except Exception:
            return False

        matches = []
        for session in sessions:
            try:
                name = (session.Name or "").split("\n", 1)[0].strip()
                aid = session.AutomationId or ""
            except Exception:
                continue
            if name not in candidates and aid not in {
                f"session_item_{candidate}" for candidate in candidates
            }:
                continue
            matches.append(session)
        if len(matches) != 1:
            self._last_navigation_error = {
                "error_code": "ambiguous_search_result" if matches else "search_result_not_found",
                "error_message": (
                    f"可见会话结果不唯一: {keyword}"
                    if matches
                    else f"可见会话列表未找到目标: {keyword}"
                ),
                "count": len(matches),
            }
            return False
        session = matches[0]
        # Weixin 4.1.13.12 exposes both SelectionItem and Invoke on recent
        # sessions. SelectionItem can acknowledge without switching the chat;
        # try it first, then use InvokePattern only. Never call the ambiguous
        # Legacy default action, which has been observed to collapse the pane.
        try:
            session_name = str(getattr(session, "Name", "") or "").split("\n", 1)[0].strip()
            session_automation_id = str(getattr(session, "AutomationId", "") or "")
        except Exception:
            session_name = ""
            session_automation_id = ""
        session_patterns = self._pattern_summary(session)
        def target_is_open() -> bool:
            try:
                current_name = driver.current_chat()
                return bool(
                    current_name
                    and current_name in candidates
                    and driver._chat_input() is not None
                )
            except Exception:
                return False

        selected = False
        invoked = False
        posted = False
        post_method = ""
        # The tested Weixin 4.1.13.12 build acknowledges UIA Select/Invoke
        # without switching. Prefer the explicitly enabled posted-window
        # message path; it does not inject physical mouse input.
        if self._background_post_message:
            posted, post_method = self._post_button_message_without_mouse(
                driver,
                session,
            )
            post_deadline = time.monotonic() + 1.2
            while time.monotonic() < post_deadline:
                if target_is_open():
                    return True
                time.sleep(0.1)

        selected = self._select_session_without_mouse(session)
        selection_deadline = time.monotonic() + 0.6
        while time.monotonic() < selection_deadline:
            if target_is_open():
                return True
            time.sleep(0.1)

        invoked = self._invoke_session_without_mouse(session)
        invoke_deadline = time.monotonic() + 1.2
        while time.monotonic() < invoke_deadline:
            if target_is_open():
                return True
            time.sleep(0.2)

        current_name = driver.current_chat() or ""
        try:
            input_present = driver._chat_input() is not None
        except Exception:
            input_present = False
        navigation_details = {
            "selection_acknowledged": selected,
            "invoke_acknowledged": invoked,
            "post_message_acknowledged": posted,
            "post_message_method": post_method,
            "session_name": session_name,
            "session_automation_id": session_automation_id,
            "session_patterns": session_patterns,
            "current_chat": current_name,
            "input_present": input_present,
        }
        logger.warning(
            "后台 UIA 可见会话选择未生效 | receiver=%s | selected=%s "
            "| automation_id=%s | selection_acknowledged=%s "
            "| invoke_acknowledged=%s | post_message_acknowledged=%s "
            "| patterns=%s "
            "| current_chat=%s | input_present=%s",
            keyword,
            session_name,
            session_automation_id,
            selected,
            invoked,
            posted,
            session_patterns,
            current_name,
            input_present,
        )
        if not selected and not invoked and not posted:
            self._last_navigation_error = {
                "error_code": "search_result_invoke_failed",
                "error_message": f"可见会话调用未确认，且实际聊天未切换: {keyword}",
                **navigation_details,
            }
            return False
        self._last_navigation_error = {
            "error_code": "chat_open_verification_failed",
            "error_message": f"可见会话打开后标题或输入框校验失败: {keyword}",
            **navigation_details,
        }
        return False

    @staticmethod
    def _navigation_controls_ready(driver: Any) -> bool:
        """Check whether the visible UIA tree exposes search and sessions."""
        try:
            session_list = driver._win.ListControl(AutomationId="session_list")
            if not session_list.Exists(0.3, 0.1):
                return False
        except Exception:
            return False
        try:
            return driver._search_box(driver._win) is not None
        except Exception:
            return False

    def _ensure_foreground_navigation(self, driver: Any):
        """Materialize the foreground navigation layout without mouse input."""
        if self._navigation_controls_ready(driver):
            return driver
        if not self._ensure_full_layout:
            self._last_binding_error = {
                "status": "uia_layout_unavailable",
                "error_code": "navigation_controls_missing",
                "error_message": "前台 UIA 未发现搜索框或会话列表，且已关闭完整布局恢复",
            }
            return None

        try:
            hwnd = int(getattr(driver._win, "NativeWindowHandle", 0) or 0)
            if not hwnd or os.name != "nt":
                raise RuntimeError("绑定窗口没有可用 HWND")
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            # SW_MAXIMIZE materializes the left navigation tree in Weixin 4.x.
            if not user32.ShowWindow(wintypes.HWND(hwnd), 3):
                logger.debug("UIA 完整布局恢复未报告窗口状态变化 | hwnd=%s", hwnd)
        except Exception as exc:
            self._last_binding_error = {
                "status": "uia_layout_unavailable",
                "error_code": "navigation_layout_restore_failed",
                "error_message": f"无法恢复前台 UIA 完整布局: {exc}",
            }
            return None

        binding = self._binding_info()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                window = driver._find_main()
                if window is not None:
                    if not self._window_matches_bound_pid(driver, window, binding["bound_pid"]):
                        return None
                    driver._win = window
                    if self._navigation_controls_ready(driver):
                        logger.info("已恢复前台 UIA 完整导航布局 | hwnd=%s", hwnd)
                        return driver
            except Exception:
                pass
            time.sleep(0.2)

        self._last_binding_error = {
            "status": "uia_layout_unavailable",
            "error_code": "navigation_controls_missing",
            "error_message": "恢复窗口布局后仍未发现搜索框或会话列表",
        }
        return None

    @staticmethod
    def _find_send_button(driver: Any, input_control: Any) -> Any:
        """Find the current chat's UIA send button without using coordinates."""
        container = input_control
        for _ in range(4):
            try:
                container = container.GetParentControl()
            except Exception:
                break
            if container is None:
                break
            try:
                for name in ("发送", "发送(S)", "Send"):
                    button = container.ButtonControl(Name=name)
                    if button.Exists(0.2, 0.1):
                        return button
            except Exception:
                continue

        try:
            for name in ("发送", "发送(S)", "Send"):
                button = driver._win.ButtonControl(Name=name)
                if button.Exists(0.2, 0.1):
                    return button
        except Exception:
            pass
        return None

    @staticmethod
    def _invoke_control(control: Any) -> tuple[bool, str]:
        """Invoke a control through UIA patterns without moving the mouse."""
        return WindowsUIASender._invoke_control_variant(control, background=False)

    @staticmethod
    def _invoke_control_variant(
        control: Any,
        excluded: set[str] | None = None,
        background: bool = False,
    ) -> tuple[bool, str]:
        """Try another exposed action after a reported-but-ineffective call.

        Some Weixin 4.x controls report ``InvokePattern.Invoke() == True``
        without consuming the draft. The caller verifies the postcondition and
        uses this helper to try a different pattern without replaying the
        whole message send.
        """
        excluded = set(excluded or ())
        ordered = (
            ("InvokePattern", "SelectionItemPattern", "LegacyIAccessible.DoDefaultAction")
            if background
            else ("LegacyIAccessible.DoDefaultAction", "SelectionItemPattern", "InvokePattern")
        )
        for method in ordered:
            if method in excluded:
                continue
            try:
                if method == "LegacyIAccessible.DoDefaultAction":
                    pattern = control.GetLegacyIAccessiblePattern()
                    if pattern is not None and pattern.DoDefaultAction(waitTime=0.2):
                        return True, method
                    continue

                import uiautomation as auto

                pattern = control.GetPattern(getattr(auto.PatternId, method))
                if pattern is None:
                    continue
                if method == "InvokePattern" and pattern.Invoke(waitTime=0.2):
                    return True, method
                if method == "SelectionItemPattern" and pattern.Select(waitTime=0.2):
                    return True, method
            except Exception as exc:
                logger.debug("UIA 备用发送 Pattern 失败 | method=%s | error=%s", method, exc)
        return False, ""

    @staticmethod
    def _invoke_background_control(control: Any) -> tuple[bool, str]:
        """Prefer the legacy action for background UIA before InvokePattern."""
        try:
            legacy_pattern = control.GetLegacyIAccessiblePattern()
            if legacy_pattern is not None and legacy_pattern.DoDefaultAction(waitTime=0.2):
                return True, "LegacyIAccessible.DoDefaultAction"
        except Exception:
            pass
        try:
            import uiautomation as auto

            invoke_pattern = control.GetPattern(auto.PatternId.InvokePattern)
            if invoke_pattern is not None and invoke_pattern.Invoke(waitTime=0.2):
                return True, "InvokePattern"
        except Exception:
            pass
        return False, ""

    @staticmethod
    def _invoke_focused_button_key(
        control: Any,
        fallback: str,
    ) -> tuple[bool, str]:
        """Invoke a UIA-discovered send button through its focused default action.

        This is intentionally limited to foreground UIA.  It does not click a
        rectangle, and background UIA must not inject keyboard input or change
        the user's global focus state.
        """
        if fallback not in {"enter", "ctrl_enter"}:
            return False, ""
        try:
            if hasattr(control, "IsEnabled") and not bool(control.IsEnabled):
                return False, ""
            if not control.SetFocus():
                return False, ""

            import uiautomation as auto

            keys = "{Ctrl}{Enter}" if fallback == "ctrl_enter" else "{Enter}"
            auto.SendKeys(keys, waitTime=0.1)
            return True, f"button_key:{fallback}"
        except Exception as exc:
            logger.debug(
                "UIA 发送按钮键盘兜底失败 | fallback=%s | error=%s",
                fallback,
                exc,
            )
            return False, ""

    @staticmethod
    def _post_button_message_without_mouse(
        driver: Any,
        control: Any,
    ) -> tuple[bool, str]:
        """Ask the UIA-discovered button to handle a click without input injection.

        Weixin's custom ``mmui::XOutlineButton`` exposes a usable UIA
        bounding rectangle but its UIA invoke action either does nothing or
        activates the window on the tested 4.1.12.26 build. Posting the
        button's mouse messages to the already materialized main window keeps
        the physical cursor, keyboard focus, and foreground window untouched.
        """
        if os.name != "nt":
            return False, ""
        try:
            hwnd = int(getattr(driver._win, "NativeWindowHandle", 0) or 0)
            bounds = getattr(control, "BoundingRectangle", None)
            if not hwnd or bounds is None:
                return False, ""

            user32 = ctypes.windll.user32
            point = wintypes.POINT(
                int((float(bounds.left) + float(bounds.right)) / 2),
                int((float(bounds.top) + float(bounds.bottom)) / 2),
            )
            if user32.IsIconic(wintypes.HWND(hwnd)):
                class WindowPlacement(ctypes.Structure):
                    _fields_ = [
                        ("length", wintypes.UINT),
                        ("flags", wintypes.UINT),
                        ("show_cmd", wintypes.UINT),
                        ("min_position", wintypes.POINT),
                        ("max_position", wintypes.POINT),
                        ("normal_position", wintypes.RECT),
                    ]

                placement = WindowPlacement()
                placement.length = ctypes.sizeof(placement)
                if not user32.GetWindowPlacement(
                    wintypes.HWND(hwnd),
                    ctypes.byref(placement),
                ):
                    return False, ""
                point.x -= int(placement.normal_position.left)
                point.y -= int(placement.normal_position.top)
                max_x = int(
                    placement.normal_position.right
                    - placement.normal_position.left
                )
                max_y = int(
                    placement.normal_position.bottom
                    - placement.normal_position.top
                )
            else:
                user32.ScreenToClient.argtypes = [
                    wintypes.HWND,
                    ctypes.POINTER(wintypes.POINT),
                ]
                user32.ScreenToClient.restype = wintypes.BOOL
                if not user32.ScreenToClient(
                    wintypes.HWND(hwnd),
                    ctypes.byref(point),
                ):
                    return False, ""
                client_rect = wintypes.RECT()
                if not user32.GetClientRect(
                    wintypes.HWND(hwnd),
                    ctypes.byref(client_rect),
                ):
                    return False, ""
                max_x = int(client_rect.right - client_rect.left)
                max_y = int(client_rect.bottom - client_rect.top)
            if not (0 <= point.x < max_x and 0 <= point.y < max_y):
                return False, ""

            before = WindowsUIASender._foreground_input_state()

            user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostMessageW.restype = wintypes.BOOL
            lparam = (int(point.y) << 16) | (int(point.x) & 0xFFFF)
            if not user32.PostMessageW(
                wintypes.HWND(hwnd),
                0x0201,  # WM_LBUTTONDOWN
                0x0001,  # MK_LBUTTON
                lparam,
            ):
                return False, ""
            time.sleep(0.03)
            if not user32.PostMessageW(
                wintypes.HWND(hwnd),
                0x0202,  # WM_LBUTTONUP
                0,
                lparam,
            ):
                return False, ""
            # Qt may request activation even for a posted client message.
            # Give its event loop a brief chance to consume the message, then
            # restore the exact prior foreground/focus only when Weixin stole it.
            time.sleep(0.08)
            WindowsUIASender._restore_foreground_after_post(
                hwnd,
                int(before.get("foreground_hwnd", 0)),
                int(before.get("focus_hwnd", 0)),
            )
            return True, "PostMessage:WM_LBUTTON"
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            logger.debug("后台发送按钮消息投递失败: %s", exc)
            return False, ""

    @staticmethod
    def _restore_foreground_after_post(
        weixin_hwnd: int,
        previous_hwnd: int,
        previous_focus_hwnd: int = 0,
    ) -> bool:
        """Restore foreground/focus only if the posted Weixin click stole it."""
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            current_hwnd = int(user32.GetForegroundWindow() or 0)
            if current_hwnd != int(weixin_hwnd):
                return current_hwnd == int(previous_hwnd)
            if (
                not previous_hwnd
                or previous_hwnd == weixin_hwnd
                or not user32.IsWindow(wintypes.HWND(previous_hwnd))
            ):
                return previous_hwnd == weixin_hwnd

            kernel32 = ctypes.windll.kernel32
            current_thread = int(kernel32.GetCurrentThreadId())
            weixin_thread = int(
                user32.GetWindowThreadProcessId(
                    wintypes.HWND(weixin_hwnd),
                    None,
                )
            )
            previous_thread = int(
                user32.GetWindowThreadProcessId(
                    wintypes.HWND(previous_hwnd),
                    None,
                )
            )
            attached_threads: list[int] = []
            try:
                for thread_id in (weixin_thread, previous_thread):
                    if (
                        thread_id
                        and thread_id != current_thread
                        and thread_id not in attached_threads
                        and user32.AttachThreadInput(
                            current_thread,
                            thread_id,
                            True,
                        )
                    ):
                        attached_threads.append(thread_id)
                user32.BringWindowToTop(wintypes.HWND(previous_hwnd))
                user32.SetForegroundWindow(wintypes.HWND(previous_hwnd))
                if (
                    previous_focus_hwnd
                    and user32.IsWindow(wintypes.HWND(previous_focus_hwnd))
                ):
                    user32.SetFocus(wintypes.HWND(previous_focus_hwnd))
                return int(user32.GetForegroundWindow() or 0) == previous_hwnd
            finally:
                for thread_id in reversed(attached_threads):
                    user32.AttachThreadInput(current_thread, thread_id, False)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            logger.debug("后台窗口消息后恢复前台失败: %s", exc)
            return False

    @staticmethod
    def _post_enter_without_focus(driver: Any) -> bool:
        """Post an Enter key sequence to the WeChat window without activating it."""
        try:
            import ctypes

            hwnd = int(getattr(driver._win, "NativeWindowHandle", 0) or 0)
            if not hwnd:
                return False

            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostMessageW.restype = wintypes.BOOL

            key_messages = (
                (0x0100, 0x000D, 0x001C0001),
                (0x0102, 0x000D, 0x001C0001),
                (0x0101, 0x000D, 0xC01C0001),
            )
            for message, wparam, lparam in key_messages:
                if not user32.PostMessageW(hwnd, message, wparam, lparam):
                    return False
                time.sleep(0.05)
            return True
        except Exception as exc:
            logger.debug("后台 UIA 投递回车失败: %s", exc)
            return False

    @staticmethod
    def _read_control_value(control: Any) -> str:
        try:
            pattern = control.GetValuePattern()
            if pattern is not None:
                return str(pattern.Value or "")
        except Exception:
            pass
        try:
            pattern = control.GetLegacyIAccessiblePattern()
            if pattern is not None:
                return str(getattr(pattern, "Value", "") or "")
        except Exception:
            pass
        return ""

    def _wait_input_empty(self, input_control: Any) -> bool:
        deadline = time.monotonic() + self._input_verify_timeout
        while time.monotonic() < deadline:
            if not self._read_control_value(input_control).strip():
                return True
            time.sleep(0.1)
        return not self._read_control_value(input_control).strip()

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").replace("\r\n", "\n").split())

    def _ui_contains_sent_text(self, driver: Any, text: str) -> bool:
        """Look for the exact text in the visible UIA message list."""
        try:
            message_list = driver._message_list()
        except Exception:
            message_list = None
        if message_list is None:
            return False
        expected = self._normalize_text(text)

        def walk(control: Any, depth: int = 0) -> bool:
            if depth > 12:
                return False
            try:
                name = self._normalize_text(getattr(control, "Name", ""))
                if name == expected:
                    return True
                children = control.GetChildren()
            except Exception:
                return False
            for child in children:
                if walk(child, depth + 1):
                    return True
            return False

        return walk(message_list)

    def _wait_ui_message(self, driver: Any, text: str) -> bool:
        deadline = time.monotonic() + self._ui_verify_timeout
        while time.monotonic() < deadline:
            if self._ui_contains_sent_text(driver, text):
                return True
            time.sleep(0.2)
        return self._ui_contains_sent_text(driver, text)

    @staticmethod
    def _pattern_summary(control: Any) -> dict[str, bool]:
        summary: dict[str, bool] = {}
        try:
            import uiautomation as auto

            for name, pattern_id in (
                ("ValuePattern", auto.PatternId.ValuePattern),
                ("InvokePattern", auto.PatternId.InvokePattern),
                ("SelectionItemPattern", auto.PatternId.SelectionItemPattern),
            ):
                try:
                    summary[name] = control.GetPattern(pattern_id) is not None
                except Exception:
                    summary[name] = False
        except Exception:
            pass
        try:
            summary["LegacyIAccessible"] = control.GetLegacyIAccessiblePattern() is not None
        except Exception:
            summary["LegacyIAccessible"] = False
        return summary

    @staticmethod
    def _control_diagnostics(control: Any) -> dict[str, Any] | None:
        """Return read-only UIA metadata useful for diagnosing custom Qt controls."""
        if control is None:
            return None

        def read(name: str, default: Any = "") -> Any:
            try:
                value = getattr(control, name)
                return value() if callable(value) else value
            except Exception:
                return default

        rect = read("BoundingRectangle", None)
        rectangle = None
        if rect is not None:
            rectangle = {
                key: read_rect
                for key, read_rect in (
                    ("left", getattr(rect, "left", None)),
                    ("top", getattr(rect, "top", None)),
                    ("right", getattr(rect, "right", None)),
                    ("bottom", getattr(rect, "bottom", None)),
                )
                if read_rect is not None
            }

        clickable_point = None
        try:
            point_data = control.GetClickablePoint()
            if len(point_data) == 3:
                point_x, point_y, available = point_data
            else:
                point, available = point_data
                point_x = getattr(point, "x", 0)
                point_y = getattr(point, "y", 0)
            clickable_point = {
                "available": bool(available),
                "x": int(point_x),
                "y": int(point_y),
            }
        except Exception:
            pass

        legacy: dict[str, Any] | None = None
        try:
            pattern = control.GetLegacyIAccessiblePattern()
            if pattern is not None:
                legacy = {}
                for name in (
                    "Name",
                    "DefaultAction",
                    "Role",
                    "State",
                    "ChildId",
                    "Value",
                    "Description",
                    "Help",
                    "KeyboardShortcut",
                ):
                    try:
                        legacy[name[0].lower() + name[1:]] = getattr(pattern, name)
                    except Exception:
                        pass
        except Exception:
            pass

        runtime_id = None
        try:
            runtime_id = list(control.GetRuntimeId())
        except Exception:
            pass

        return {
            "name": str(read("Name", "") or ""),
            "automation_id": str(read("AutomationId", "") or ""),
            "control_type": str(read("ControlTypeName", "") or ""),
            "localized_control_type": str(read("LocalizedControlType", "") or ""),
            "class_name": str(read("ClassName", "") or ""),
            "framework_id": str(read("FrameworkId", "") or ""),
            "provider_description": str(read("ProviderDescription", "") or ""),
            "native_window_handle": int(read("NativeWindowHandle", 0) or 0),
            "process_id": int(read("ProcessId", 0) or 0),
            "is_enabled": bool(read("IsEnabled", False)),
            "is_offscreen": bool(read("IsOffscreen", False)),
            "is_keyboard_focusable": bool(read("IsKeyboardFocusable", False)),
            "has_keyboard_focus": bool(read("HasKeyboardFocus", False)),
            "bounding_rectangle": rectangle,
            "clickable_point": clickable_point,
            "runtime_id": runtime_id,
            "patterns": WindowsUIASender._pattern_summary(control),
            "legacy": legacy,
        }

    def _open_chat_sync(self, receiver: str, is_group: bool) -> bool:
        try:
            for method in self._candidate_methods():
                driver = self._ensure_driver_window(method)
                if driver is None:
                    continue
                if self._open_chat_without_mouse(
                    driver,
                    receiver,
                    is_group,
                    background_mode=method == "background_uia",
                ):
                    return True
            logger.error("UIA 无法打开目标会话 | receiver=%s", receiver)
            return False
        except Exception as exc:
            logger.exception("UIA 打开会话失败 | receiver=%s | error=%s", receiver, exc)
            return False

    def _candidate_methods(self) -> list[str]:
        if self._send_mode == "auto":
            capability = self._probe_background_capability()
            if capability.get("available"):
                methods = ["background_uia"]
                if self._allow_foreground_activation:
                    methods.append("foreground_uia")
                return methods
            if self._allow_foreground_activation:
                logger.info(
                    "后台 UIA 能力不足，按显式配置选择前台 UIA | code=%s | reason=%s",
                    capability.get("reason_code", ""),
                    capability.get("reason", ""),
                )
                return ["foreground_uia"]
            logger.info(
                "后台 UIA 能力不足且禁止前台激活，停止发送 | code=%s | reason=%s",
                capability.get("reason_code", ""),
                capability.get("reason", ""),
            )
            return []
        return [self._send_mode]

    def _attempt_limit_for_method(self, method: str) -> int:
        if method == "background_uia":
            return self._background_attempts
        return self._foreground_attempts

    @staticmethod
    def _can_retry_result(result: SendResult) -> bool:
        """Retry only failures proven to have happened before delivery."""
        if result.status == "pending_verify":
            return False
        if result.action_performed or result.draft_cleared:
            return False
        if result.ui_verified or result.db_verified:
            return False
        # A global input-state change is a hard safety stop, never a fallback.
        if result.error_code == "background_input_state_changed":
            return False
        return True

    @staticmethod
    def _foreground_input_state() -> dict[str, int]:
        """Read foreground/focus/cursor state without changing Windows input state."""
        state = {"foreground_hwnd": 0, "focus_hwnd": 0, "cursor_x": 0, "cursor_y": 0}
        if os.name != "nt":
            return state
        try:
            user32 = ctypes.windll.user32
            state["foreground_hwnd"] = int(user32.GetForegroundWindow() or 0)
            point = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(point)):
                state["cursor_x"] = int(point.x)
                state["cursor_y"] = int(point.y)
            foreground_thread = user32.GetWindowThreadProcessId(
                wintypes.HWND(state["foreground_hwnd"]), None
            )
            if foreground_thread:
                class GuiThreadInfo(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND),
                        ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND),
                        ("hwndCaret", wintypes.HWND),
                    ]

                info = GuiThreadInfo()
                info.cbSize = ctypes.sizeof(info)
                if user32.GetGUIThreadInfo(foreground_thread, ctypes.byref(info)):
                    state["focus_hwnd"] = int(info.hwndFocus or 0)
        except (AttributeError, OSError, TypeError):
            pass
        return state

    @staticmethod
    def _hwnd_process_id(hwnd: int) -> int:
        """Resolve an HWND owner without activating or focusing it."""
        if os.name != "nt" or not hwnd:
            return 0
        try:
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                wintypes.HWND(hwnd),
                ctypes.byref(pid),
            )
            return int(pid.value or 0)
        except (AttributeError, OSError, TypeError, ValueError):
            return 0

    def _verify_background_state(
        self,
        result: SendResult,
        before: dict[str, int],
        phase: str,
    ) -> bool:
        """Reject only activation/focus stolen by Weixin itself.

        Cursor movement or switching between non-Weixin windows belongs to the
        user and must not make an otherwise background operation fail.
        """
        after = self._foreground_input_state()
        unchanged = before == after
        target_pid = int(self._driver_pid or 0)
        before_foreground_pid = self._hwnd_process_id(
            int(before.get("foreground_hwnd", 0))
        )
        before_focus_pid = self._hwnd_process_id(int(before.get("focus_hwnd", 0)))
        after_foreground_pid = self._hwnd_process_id(
            int(after.get("foreground_hwnd", 0))
        )
        after_focus_pid = self._hwnd_process_id(int(after.get("focus_hwnd", 0)))
        weixin_activated = bool(
            target_pid
            and (
                (
                    after_foreground_pid == target_pid
                    and before_foreground_pid != target_pid
                )
                or (
                    after_focus_pid == target_pid
                    and before_focus_pid != target_pid
                )
            )
        )
        result.details.setdefault("background_guard", {})[phase] = {
            "before": before,
            "after": after,
            "unchanged": unchanged,
            "target_pid": target_pid,
            "weixin_activated": weixin_activated,
            "user_state_changed": bool(not unchanged and not weixin_activated),
        }
        if not weixin_activated:
            return True

        # The send button can legitimately move focus/activation after it has
        # accepted the draft. The action is now ambiguous, so verify the
        # target database instead of retrying or reporting an immediate loss.
        if result.action_performed and phase in {"after_invoke", "after_post_message"}:
            result.pending(
                "db_verify",
                error_code="db_verification_deferred",
                error_message="后台 UIA 已执行发送动作，前台状态发生变化，等待数据库确认",
                db_verified=False,
                ui_verified=result.ui_verified,
                background_phase=phase,
                background_before=before,
                background_after=after,
            )
            logger.info(
                "后台 UIA 发送动作后前台状态变化，转入数据库验证 | phase=%s",
                phase,
            )
            return False

        result.fail(
            "invoke" if phase == "after_invoke" else "window",
            "background_input_state_changed",
            "后台 UIA 操作使微信获得前台或键盘焦点，已停止发送",
            background_phase=phase,
            background_before=before,
            background_after=after,
        )
        return False

    def _post_key_without_focus(self, driver: Any, ctrl: bool = False) -> bool:
        """Post an explicitly configured Enter/Ctrl+Enter sequence."""
        try:
            import ctypes

            hwnd = int(getattr(driver._win, "NativeWindowHandle", 0) or 0)
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostMessageW.restype = wintypes.BOOL
            if ctrl and not user32.PostMessageW(hwnd, 0x0100, 0x11, 0x001D0001):
                return False
            for message, wparam, lparam in (
                (0x0100, 0x000D, 0x001C0001),
                (0x0102, 0x000D, 0x001C0001),
                (0x0101, 0x000D, 0xC01C0001),
            ):
                if not user32.PostMessageW(hwnd, message, wparam, lparam):
                    return False
                time.sleep(0.05)
            if ctrl and not user32.PostMessageW(hwnd, 0x0101, 0x11, 0xC01D0001):
                return False
            return True
        except Exception as exc:
            logger.debug("后台 UIA 按键投递失败: %s", exc)
            return False

    def _send_text_once(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        method: str,
        attempt_id: str = "",
    ) -> SendResult:
        result = SendResult.for_message(msg, target_id or receiver, method, attempt_id)
        background = method == "background_uia"
        background_state = self._foreground_input_state() if background else None
        try:
            driver = self._ensure_driver_window(method)
            if driver is None:
                binding_error = self._last_binding_error or {}
                return result.fail(
                    "window",
                    binding_error.get("error_code", "window_not_found"),
                    binding_error.get("error_message", "未找到绑定账号的 UIA 主窗口"),
                    binding=binding_error,
                )
            if background and not self._verify_background_state(
                result, background_state or {}, "after_window"
            ):
                return result

            current_name = driver.current_chat()
            input_control = driver._chat_input()
            if current_name != receiver or input_control is None:
                opened = self._open_chat_without_mouse(
                    driver,
                    receiver,
                    is_group,
                    background_mode=background,
                )
                if (
                    not opened
                    and target_id
                    and target_id != receiver
                    and self._is_searchable_identifier(target_id)
                ):
                    opened = self._open_chat_without_mouse(
                        driver,
                        target_id,
                        is_group,
                        background_mode=background,
                    )
                if not opened:
                    navigation_error = self._last_navigation_error or {}
                    return result.fail(
                        "search",
                        navigation_error.get("error_code", "chat_open_failed"),
                        navigation_error.get("error_message", "无法唯一打开目标会话"),
                        **{
                            key: value
                            for key, value in navigation_error.items()
                            if key not in {"error_code", "error_message"}
                        },
                    )
                input_control = driver._chat_input()

            if background and not self._verify_background_state(
                result, background_state or {}, "after_navigation"
            ):
                return result

            if input_control is None:
                return result.fail("draft", "input_not_found", "未找到聊天输入框")
            if not self._set_text_without_mouse(
                driver,
                input_control,
                msg,
                allow_focus_fallback=not background,
            ):
                return result.fail("draft", "draft_write_failed", "UIA 无法写入消息正文")

            written = self._read_control_value(input_control)
            if self._normalize_text(written) != self._normalize_text(msg):
                retry_written = self._set_text_without_mouse(
                    driver,
                    input_control,
                    msg,
                    allow_focus_fallback=not background,
                    prefer_focus_fallback=True,
                )
                if retry_written:
                    written = self._read_control_value(input_control)
                if self._normalize_text(written) != self._normalize_text(msg):
                    return result.fail(
                        "draft",
                        "draft_readback_mismatch",
                        "输入框回读内容与待发送正文不一致",
                        written=written,
                        retry_attempted=not background,
                    )

            if background and not self._verify_background_state(
                result, background_state or {}, "after_draft"
            ):
                return result

            if not background:
                try:
                    has_focus = bool(getattr(input_control, "HasKeyboardFocus", False))
                except Exception:
                    has_focus = False
                if not has_focus:
                    try:
                        if not input_control.SetFocus():
                            return result.fail(
                                "draft",
                                "input_focus_failed",
                                "正文已写入，但无法把聊天输入框设为键盘焦点",
                            )
                    except Exception as exc:
                        return result.fail(
                            "draft",
                            "input_focus_failed",
                            f"正文已写入，但无法把聊天输入框设为键盘焦点: {exc}",
                        )
                result.details["input_focused"] = True

            button = self._find_send_button(driver, input_control)
            invoke_method = ""
            invoke_attempts: list[str] = []
            if button is not None:
                if background and self._background_post_message:
                    invoked, invoke_method = self._post_button_message_without_mouse(
                        driver,
                        button,
                    )
                else:
                    invoke = (
                        self._invoke_background_control
                        if background
                        else self._invoke_control
                    )
                    invoked, invoke_method = invoke(button)
                if not invoked:
                    return result.fail(
                        "invoke",
                        "background_button_message_failed"
                        if background and self._background_post_message
                        else "send_button_invoke_failed",
                        "后台发送按钮消息投递失败"
                        if background and self._background_post_message
                        else "发送按钮存在但 Invoke/Legacy Pattern 调用失败",
                        patterns=self._pattern_summary(button),
                    )
                if invoke_method:
                    invoke_attempts.append(invoke_method)
                result.action_performed = True
            else:
                if self._send_key_fallback == "none":
                    return result.fail(
                        "invoke",
                        "send_button_not_found",
                        "未找到真实发送按钮，且未启用按键兜底",
                    )
                if background:
                    return result.fail(
                        "invoke",
                        "background_send_button_required",
                        "后台 UIA 禁止键盘投递，必须找到真实发送按钮",
                    )
                else:
                    if not input_control.SetFocus():
                        return result.fail("invoke", "input_focus_failed", "无法聚焦聊天输入框")
                    import uiautomation as auto

                    keys = "{Ctrl}{Enter}" if self._send_key_fallback == "ctrl_enter" else "{Enter}"
                    auto.SendKeys(keys, waitTime=0.1)
                result.action_performed = True
                invoke_method = f"key:{self._send_key_fallback}"
                invoke_attempts.append(invoke_method)

            result.details["invoke_method"] = invoke_method
            result.details["invoke_attempts"] = list(invoke_attempts)
            if background and not self._verify_background_state(
                result, background_state or {}, "after_invoke"
            ):
                return result

            # Pattern invocation is only an acknowledgement. Verify that the
            # draft was consumed before considering the action successful. If
            # a pattern returned True but did nothing, try the remaining
            # exposed patterns without replaying the whole send operation.
            draft_cleared = self._wait_input_empty(input_control)
            attempted_patterns = set(invoke_attempts)
            while (
                not draft_cleared
                and button is not None
                and invoke_attempts
                and not invoke_method.startswith("key:")
                and invoke_method != "PostMessage:WM_LBUTTON"
            ):
                invoked, alternate_method = self._invoke_control_variant(
                    button,
                    attempted_patterns,
                    background=background,
                )
                if not invoked:
                    break
                attempted_patterns.add(alternate_method)
                invoke_attempts.append(alternate_method)
                result.action_performed = True
                result.details["invoke_attempts"] = list(invoke_attempts)
                result.details["invoke_method"] = " -> ".join(invoke_attempts)
                if background and not self._verify_background_state(
                    result, background_state or {}, "after_invoke"
                ):
                    return result
                draft_cleared = self._wait_input_empty(input_control)

            # Some Weixin 4.x builds acknowledge every exposed Pattern but do
            # not consume the draft. Before trying another delivery action,
            # check whether the message already appeared in the UI; clicking
            # again in that ambiguous state could create a duplicate.
            if (
                not draft_cleared
                and button is not None
                and self._ui_contains_sent_text(driver, msg)
            ):
                result.ui_verified = True
                return result.fail(
                    "ui_verify",
                    "send_state_ambiguous",
                    "消息列表已出现正文，但输入框未清空，已禁止再次发送",
                    invoke_attempts=list(invoke_attempts),
                )

            # The experimental background mode may use the button's own
            # window messages when explicitly enabled. The production
            # foreground UIA path never derives a click from a rectangle.
            if (
                background
                and not draft_cleared
                and button is not None
                and self._background_post_message
                and not invoke_method.startswith("key:")
                and invoke_method != "PostMessage:WM_LBUTTON"
            ):
                if background and not self._verify_background_state(
                    result, background_state or {}, "before_post_message"
                ):
                    return result
                posted, posted_method = self._post_button_message_without_mouse(
                    driver,
                    button,
                )
                if posted:
                    invoke_attempts.append(posted_method)
                    result.action_performed = True
                    result.details["invoke_attempts"] = list(invoke_attempts)
                    result.details["invoke_method"] = " -> ".join(invoke_attempts)
                    if background and not self._verify_background_state(
                        result, background_state or {}, "after_post_message"
                    ):
                        return result
                    draft_cleared = self._wait_input_empty(input_control)

            # Weixin 4.1.12.26 can expose a real, enabled send button whose
            # Invoke/Legacy patterns return success but do not consume the
            # draft.  Focusing that same UIA element and pressing its explicit
            # default key invokes the button itself, independent of the chat
            # input's Enter/Ctrl+Enter preference.  Never do this in background
            # mode because it would inject keyboard input into the desktop.
            if (
                not draft_cleared
                and not background
                and button is not None
                and self._send_button_key_fallback != "none"
            ):
                invoked, fallback_method = self._invoke_focused_button_key(
                    button,
                    self._send_button_key_fallback,
                )
                if invoked:
                    invoke_attempts.append(fallback_method)
                    result.action_performed = True
                    result.details["invoke_attempts"] = list(invoke_attempts)
                    result.details["invoke_method"] = " -> ".join(invoke_attempts)
                    draft_cleared = self._wait_input_empty(input_control)

            if not draft_cleared:
                return result.fail(
                    "ui_verify",
                    "send_not_accepted",
                    "发送动作完成但输入框未清空，微信可能未接受发送",
                    invoke_attempts=list(invoke_attempts),
                )
            result.draft_cleared = True

            ui_verified = self._wait_ui_message(driver, msg)
            result.ui_verified = ui_verified
            if not ui_verified and self._require_ui_verify:
                return result.fail(
                    "ui_verify",
                    "ui_message_not_found",
                    "输入框已清空，但消息列表未找到本人发送正文",
                )
            return result.sent(
                "ui_verify",
                action_performed=True,
                draft_cleared=True,
                ui_verified=ui_verified,
            )
        except Exception as exc:
            logger.exception("UIA 消息发送失败 | receiver=%s | error=%s", receiver, exc)
            return result.fail("invoke", "uia_exception", str(exc))

    def _diagnose_sync(self) -> dict[str, Any]:
        binding = self._binding_info()
        target_pid = binding.get("bound_pid")
        probe_method = self._diagnostic_method()
        payload: dict[str, Any] = {
            "method": self._send_mode,
            "probe_method": probe_method,
            "selected_account": binding.get("selected_account", ""),
            "bound_account": binding.get("bound_account", ""),
            "binding_status": binding.get("status", ""),
            "bound_pid": target_pid,
            "driver_pid": self._driver_pid,
            "window": None,
            "uia_available": False,
            "current_chat": "",
            "session_list": None,
            "visible_sessions": [],
            "search_box": None,
            "chat_input": None,
            "send_button": None,
            "error_code": binding.get("error_code", ""),
            "error": "",
        }
        if binding["status"] != "bound":
            payload["error"] = binding.get("error_message", "账号绑定不可用")
            return payload
        try:
            driver = self._ensure_driver_window(probe_method)
            if driver is None:
                binding_error = self._last_binding_error or {}
                payload["error_code"] = binding_error.get(
                    "error_code", "uia_window_unavailable"
                )
                payload["error"] = binding_error.get(
                    "error_message", "未找到可访问的绑定账号 UIA 主窗口"
                )
                return payload
            if probe_method == "foreground_uia":
                driver = self._ensure_foreground_navigation(driver)
                if driver is None:
                    binding_error = self._last_binding_error or {}
                    payload["error_code"] = binding_error.get(
                        "error_code", "navigation_controls_missing"
                    )
                    payload["error"] = binding_error.get(
                        "error_message", "前台 UIA 导航控件不可用"
                    )
                    return payload
            window = getattr(driver, "_win", None)
            if window is None:
                payload["error"] = "未找到可访问的 mmui::MainWindow"
                return payload
            driver._win = window
            if not self._window_matches_bound_pid(driver, window, target_pid):
                binding_error = self._last_binding_error or {}
                payload["error_code"] = binding_error.get("error_code", "window_pid_mismatch")
                payload["error"] = binding_error.get("error_message", "UIA 窗口 PID 不匹配")
                payload["window"] = {
                    "hwnd": int(getattr(window, "NativeWindowHandle", 0) or 0),
                    "pid": _window_pid_from_control(driver, window),
                    "class_name": str(getattr(window, "ClassName", "") or ""),
                    "name": str(getattr(window, "Name", "") or ""),
                }
                return payload
            payload["uia_available"] = True
            payload["driver_pid"] = self._driver_pid
            hwnd = int(getattr(window, "NativeWindowHandle", 0) or 0)
            payload["window"] = {
                "hwnd": hwnd,
                "pid": _window_pid_from_control(driver, window),
                "class_name": str(getattr(window, "ClassName", "") or ""),
                "name": str(getattr(window, "Name", "") or ""),
            }
            session_list = None
            try:
                session_list = window.ListControl(AutomationId="session_list")
                if not session_list.Exists(0.2, 0.1):
                    session_list = None
            except Exception:
                session_list = None
            search_box = driver._search_box(window)
            input_control = driver._chat_input(window)
            button = self._find_send_button(driver, input_control) if input_control else None
            payload["current_chat"] = driver.current_chat() or ""
            payload["session_list"] = (
                {
                    "automation_id": getattr(session_list, "AutomationId", ""),
                    "name": getattr(session_list, "Name", ""),
                }
                if session_list
                else None
            )
            if session_list is not None:
                try:
                    sessions = list(session_list.GetChildren())[:50]
                except Exception:
                    sessions = []
                for session in sessions:
                    try:
                        raw_name = str(getattr(session, "Name", "") or "")
                        payload["visible_sessions"].append(
                            {
                                "name": raw_name.split("\n", 1)[0].strip(),
                                "automation_id": str(
                                    getattr(session, "AutomationId", "") or ""
                                ),
                                "patterns": self._pattern_summary(session),
                            }
                        )
                    except Exception:
                        continue
            payload["search_box"] = (
                {
                    "name": getattr(search_box, "Name", ""),
                    "patterns": self._pattern_summary(search_box),
                }
                if search_box
                else None
            )
            payload["chat_input"] = (
                {
                    "automation_id": getattr(input_control, "AutomationId", ""),
                    "patterns": self._pattern_summary(input_control),
                }
                if input_control
                else None
            )
            payload["send_button"] = self._control_diagnostics(button)
        except Exception as exc:
            payload["error_code"] = "uia_diagnose_exception"
            payload["error"] = str(exc)
        return payload

    def _diagnostic_method(self) -> str:
        """Choose a read-only UIA probe mode without sending or mouse fallback."""
        if self._send_mode == "auto":
            capability = self._probe_background_capability()
            if capability.get("available"):
                return "background_uia"
            return "foreground_uia"
        return self._send_mode

    def _send_text_sync(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        attempt_id: str = "",
    ) -> bool:
        return self._send_text_sync_result(msg, receiver, is_group, target_id, attempt_id).success

    def _send_text_sync_result(
        self,
        msg: str,
        receiver: str,
        is_group: bool,
        target_id: str,
        attempt_id: str = "",
    ) -> SendResult:
        """Run the configured UIA mode(s) serially inside the UIA executor."""
        last_result: SendResult | None = None
        attempt_history: list[dict[str, Any]] = []
        methods = self._candidate_methods()
        policy = {
            "send_mode": self._send_mode,
            "background_attempts": self._background_attempts,
            "foreground_attempts": self._foreground_attempts,
            "allow_foreground_activation": self._allow_foreground_activation,
        }

        for method in methods:
            max_attempts = self._attempt_limit_for_method(method)
            for attempt_number in range(1, max_attempts + 1):
                result = self._send_text_once(
                    msg,
                    receiver,
                    is_group,
                    target_id,
                    method,
                    attempt_id,
                )
                attempt_history.append(
                    {
                        "method": method,
                        "attempt": attempt_number,
                        "max_attempts": max_attempts,
                        "status": result.status,
                        "stage": result.stage,
                        "error_code": result.error_code,
                        "action_performed": result.action_performed,
                        "draft_cleared": result.draft_cleared,
                    }
                )
                result.details["delivery_attempts"] = list(attempt_history)
                result.details["delivery_policy"] = dict(policy)
                last_result = result

                if result.success:
                    return result
                if not self._can_retry_result(result):
                    if result.action_performed or result.draft_cleared:
                        logger.error(
                            "UIA 已经执行过发送动作，禁止重试或切换模式 | method=%s | stage=%s | code=%s",
                            method,
                            result.stage,
                            result.error_code,
                        )
                    elif result.error_code == "background_input_state_changed":
                        logger.error(
                            "后台 UIA 已改变全局输入状态，禁止重试或切换模式 | stage=%s",
                            result.stage,
                        )
                    return result
                if attempt_number < max_attempts:
                    logger.warning(
                        "UIA 发送失败，按配置重试当前模式 (%s/%s) | method=%s | stage=%s | code=%s",
                        attempt_number,
                        max_attempts,
                        method,
                        result.stage,
                        result.error_code,
                    )
                    continue

            logger.warning(
                "UIA 模式已耗尽配置次数，准备切换下一模式 | method=%s | attempts=%s",
                method,
                max_attempts,
            )

        if last_result is not None:
            last_result.details["attempts_exhausted"] = True
            last_result.details["delivery_attempts"] = list(attempt_history)
            last_result.details["delivery_policy"] = dict(policy)
            return last_result
        return SendResult.for_message(
            msg,
            target_id or receiver,
            self._send_mode,
            attempt_id,
        ).fail(
            "window",
            "uia_not_attempted",
            "没有可用的 UIA 发送模式",
            attempts_exhausted=True,
            delivery_policy=policy,
            delivery_attempts=attempt_history,
        )
