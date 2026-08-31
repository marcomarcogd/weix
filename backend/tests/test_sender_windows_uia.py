from types import SimpleNamespace

import pytest

from app.core.send_result import SendResult
from app.core.sender_windows_uia import WindowsUIASender


class FakeValuePattern:
    IsReadOnly = False

    def __init__(self):
        self.value = ""

    @property
    def Value(self):
        return self.value

    def SetValue(self, value, waitTime=0.1):
        self.value = value
        return True


class FakeLegacyValuePattern:
    def __init__(self, value_pattern):
        self.value_pattern = value_pattern

    def SetValue(self, value, waitTime=0.1):
        self.value_pattern.value = value
        return True


class FakeInput:
    def __init__(self):
        self.value_pattern = FakeValuePattern()
        self.legacy_value_pattern = FakeLegacyValuePattern(self.value_pattern)
        self.focused = False

    def GetLegacyIAccessiblePattern(self):
        return self.legacy_value_pattern

    def GetValuePattern(self):
        return self.value_pattern

    def SetFocus(self):
        self.focused = True
        return True


class FakeLegacyPattern:
    def __init__(self):
        self.invoked = False

    def DoDefaultAction(self, waitTime=0.1):
        self.invoked = True
        return True


class FakeSendButton:
    def __init__(self):
        self.legacy_pattern = FakeLegacyPattern()

    def GetLegacyIAccessiblePattern(self):
        return self.legacy_pattern


class FakeButtonKeySendButton(FakeSendButton):
    IsEnabled = True

    def __init__(self, input_control):
        super().__init__()
        self.input_control = input_control
        self.focused = False

    def SetFocus(self):
        self.focused = True
        return True


class FakeFallbackSendButton:
    def __init__(self, input_control):
        self.input_control = input_control

    def GetLegacyIAccessiblePattern(self):
        return self

    def DoDefaultAction(self, waitTime=0.1):
        self.input_control.value_pattern.value = ""
        return True

    def GetPattern(self, _pattern_id):
        return None


class FakeDriver:
    def __init__(self):
        self.input = FakeInput()
        self.sent = []
        self._win = self

    def _find_main(self):
        return self

    def current_chat(self):
        return "测试联系人"

    def _chat_input(self):
        return self.input


class FakeDiagnosticSessionList:
    AutomationId = "session_list"
    Name = "会话列表"

    def Exists(self, *_args):
        return False


class FakeDiagnosticWindow:
    NativeWindowHandle = 1234
    ClassName = "mmui::MainWindow"
    Name = "微信"

    def ListControl(self, **_kwargs):
        return FakeDiagnosticSessionList()


class FakeDiagnosticDriver:
    def __init__(self):
        self._win = FakeDiagnosticWindow()

    def _find_main(self):
        return self._win

    def _pid_from_hwnd(self, hwnd):
        assert hwnd == 1234
        return 5678

    def _search_box(self, _window):
        return None

    def _chat_input(self, _window):
        return None

    def current_chat(self):
        return "测试联系人"


class FakeSession:
    def __init__(self, name, aid=""):
        self.Name = name
        self.AutomationId = aid

    def GetPattern(self, _pattern_id):
        return None

    def GetLegacyIAccessiblePattern(self):
        return None


class FakeSessionList:
    def __init__(self, sessions):
        self._sessions = sessions

    def Exists(self, *_args):
        return True

    def GetChildren(self):
        return list(self._sessions)


class FakeVisibleDriver(FakeDriver):
    def __init__(self, sessions):
        super().__init__()
        self._session_list = FakeSessionList(sessions)

    def ListControl(self, **_kwargs):
        return self._session_list


def test_uia_defaults_fail_closed_for_non_uia_fallbacks(monkeypatch):
    monkeypatch.setattr(
        "app.core.sender_windows_uia.get_config",
        lambda: SimpleNamespace(windows_sender={}),
    )

    sender = WindowsUIASender()

    assert sender._send_mode == "foreground_uia"
    assert sender._background_post_message is False
    assert sender._send_key_fallback == "none"
    assert sender._send_button_key_fallback == "none"
    assert sender._require_ui_verify is True


def test_current_management_background_alias_maps_to_upstream_mode(monkeypatch):
    monkeypatch.setattr(
        "app.core.sender_windows_uia.get_config",
        lambda: SimpleNamespace(
            windows_sender={
                "send_mode": "background",
                "allow_foreground_activation": False,
                "allow_mouse_fallback": False,
            }
        ),
    )

    sender = WindowsUIASender()

    assert sender._send_mode == "background_uia"
    assert sender._background_mode is True
    assert sender._allow_foreground_activation is False


@pytest.mark.asyncio
async def test_uia_sender_writes_text_without_click(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "background_uia"
    sender._background_mode = True
    sender._background_post_message = False
    sender._send_key_fallback = "enter"
    sender._require_ui_verify = False
    driver = FakeDriver()
    monkeypatch.setattr(sender, "_get_driver", lambda: driver)
    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: FakeSendButton())
    monkeypatch.setattr(
        sender,
        "_invoke_background_control",
        lambda _control: driver.input.value_pattern.__setattr__("value", "")
        or (True, "InvokePattern"),
    )
    monkeypatch.setattr(
        sender,
        "_post_key_without_focus",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("background UIA must not inject keyboard input")
        ),
    )
    monkeypatch.setattr(
        "uiautomation.SendKeys",
        lambda *args, **kwargs: driver.sent.append(args[0]),
    )

    assert await sender.send_text("你好", "测试联系人") is True
    assert driver.input.value_pattern.value == ""
    assert driver.input.focused is False
    assert driver.sent == []


@pytest.mark.asyncio
async def test_background_uia_uses_posted_button_message_without_input_injection(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "background_uia"
    sender._background_mode = True
    sender._background_post_message = True
    sender._require_ui_verify = False
    driver = FakeDriver()
    monkeypatch.setattr(sender, "_get_driver", lambda: driver)
    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: FakeSendButton())

    def post_button(_driver, _button):
        driver.input.value_pattern.value = ""
        return True, "PostMessage:WM_LBUTTON"

    monkeypatch.setattr(sender, "_post_button_message_without_mouse", post_button)
    monkeypatch.setattr(
        "uiautomation.SendKeys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("background post-message path must not inject keys")
        ),
    )

    result = await sender.send_text_result("后台发送", "测试联系人")

    assert result.success is True
    assert result.method == "background_uia"
    assert result.details["invoke_method"] == "PostMessage:WM_LBUTTON"
    assert driver.input.focused is False


def test_foreground_uia_never_uses_posted_button_message(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._background_mode = False
    sender._background_post_message = True
    sender._require_ui_verify = False
    sender._input_verify_timeout = 0.01
    driver = FakeDriver()
    monkeypatch.setattr(sender, "_get_driver", lambda: driver)
    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: FakeSendButton())

    monkeypatch.setattr(
        sender,
        "_post_button_message_without_mouse",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("foreground UIA must not use coordinate-derived window messages")
        ),
    )

    result = sender._send_text_sync_result(
        "前台无坐标发送",
        "测试联系人",
        False,
        "wxid_target",
    )

    assert result.success is False
    assert result.method == "foreground_uia"
    assert result.error_code == "send_not_accepted"
    assert driver.input.focused is True


def test_foreground_uia_uses_explicit_focused_button_key_fallback(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._send_button_key_fallback = "enter"
    sender._require_ui_verify = False
    sender._input_verify_timeout = 0.01
    driver = FakeDriver()
    button = FakeButtonKeySendButton(driver.input)

    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: button)
    monkeypatch.setattr(sender, "_invoke_control", lambda _control: (True, "InvokePattern"))
    monkeypatch.setattr(
        "uiautomation.SendKeys",
        lambda keys, **_kwargs: (
            driver.input.value_pattern.__setattr__("value", "")
            if keys == "{Enter}"
            else None
        ),
    )

    result = sender._send_text_once(
        "按钮键盘兜底",
        "测试联系人",
        False,
        "wxid_target",
        "foreground_uia",
    )

    assert result.success is True
    assert button.focused is True
    assert result.details["invoke_attempts"][-1] == "button_key:enter"
    assert result.draft_cleared is True


@pytest.mark.asyncio
async def test_background_uia_does_not_call_ensure_window(monkeypatch):
    sender = WindowsUIASender()

    class Driver(FakeDriver):
        def ensure_window(self):
            raise AssertionError("background mode must not call ensure_window")

    driver = Driver()
    monkeypatch.setattr(sender, "_get_driver", lambda: driver)
    sender._send_mode = "background_uia"
    sender._background_mode = True
    sender._background_post_message = False
    sender._send_key_fallback = "enter"
    sender._require_ui_verify = False
    monkeypatch.setattr(
        sender,
        "_binding_info",
        lambda: {
            "selected_account": "wxid_selected_1",
            "bound_account": "wxid_selected_1",
            "bound_pid": 5678,
            "status": "bound",
            "error_code": "",
            "error_message": "",
        },
    )
    monkeypatch.setattr(sender, "_window_matches_bound_pid", lambda *_args: True)
    monkeypatch.setattr(
        sender,
        "_find_send_button",
        lambda *_args: FakeSendButton(),
    )
    monkeypatch.setattr(
        sender,
        "_invoke_background_control",
        lambda _control: driver.input.value_pattern.__setattr__("value", "")
        or (True, "InvokePattern"),
    )

    assert await sender.send_text("后台发送", "测试联系人") is True


def test_background_uia_can_hot_activate_without_ensure_window(monkeypatch):
    sender = WindowsUIASender()
    sender._hot_activate_accessibility = True

    class Driver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.ready = False
            self.wake_calls = 0

        def _find_main(self):
            return self if self.ready else None

        def _wake_accessibility(self):
            self.wake_calls += 1
            self.ready = True
            return True

        def ensure_window(self):
            raise AssertionError("background hot activation must not call ensure_window")

    driver = Driver()
    monkeypatch.setattr(sender, "_get_driver", lambda: driver)
    monkeypatch.setattr(
        sender,
        "_binding_info",
        lambda: {
            "selected_account": "wxid_selected_1",
            "bound_account": "wxid_selected_1",
            "bound_pid": 5678,
            "status": "bound",
            "error_code": "",
            "error_message": "",
        },
    )
    monkeypatch.setattr(sender, "_window_matches_bound_pid", lambda *_args: True)
    monkeypatch.setattr(
        sender,
        "_foreground_input_state",
        lambda: {"foreground_hwnd": 1, "focus_hwnd": 2, "cursor_x": 3, "cursor_y": 4},
    )

    assert sender._ensure_driver_window("background_uia") is driver
    assert driver.wake_calls == 1


def test_background_text_write_prefers_legacy_value_pattern():
    sender = WindowsUIASender()
    driver = FakeDriver()

    class LegacyOnlyInput(FakeInput):
        def GetValuePattern(self):
            raise AssertionError("background mode must not use ValuePattern")

    driver.input = LegacyOnlyInput()
    assert sender._set_text_without_mouse(
        driver,
        driver.input,
        "只走 Legacy",
        allow_focus_fallback=False,
    ) is True
    assert driver.input.value_pattern.value == "只走 Legacy"


def test_uia_diagnose_resolves_window_pid_without_sending(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._background_mode = False
    driver = FakeDiagnosticDriver()
    probe_methods = []
    monkeypatch.setattr(
        sender,
        "_ensure_driver_window",
        lambda method=None: probe_methods.append(method) or driver,
    )
    monkeypatch.setattr(sender, "_ensure_foreground_navigation", lambda value: value)
    monkeypatch.setattr(
        sender,
        "_binding_info",
        lambda: {
            "selected_account": "wxid_selected_1",
            "bound_account": "wxid_selected_1",
            "bound_pid": 5678,
            "status": "bound",
            "error_code": "",
            "error_message": "",
        },
    )

    result = sender._diagnose_sync()

    assert result["uia_available"] is True
    assert result["bound_pid"] == 5678
    assert result["window"]["hwnd"] == 1234
    assert result["window"]["pid"] == 5678
    assert result["current_chat"] == "测试联系人"
    assert result["probe_method"] == "foreground_uia"
    assert probe_methods == ["foreground_uia"]
    assert result["error"] == ""


def test_uia_diagnose_reports_window_initialization_error(monkeypatch):
    sender = WindowsUIASender()
    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: None)
    monkeypatch.setattr(
        sender,
        "_binding_info",
        lambda: {
            "selected_account": "wxid_selected_1",
            "bound_account": "wxid_selected_1",
            "bound_pid": 5678,
            "status": "bound",
            "error_code": "",
            "error_message": "",
        },
    )

    result = sender._diagnose_sync()

    assert result["uia_available"] is False
    assert result["error_code"] == "uia_window_unavailable"


def test_foreground_navigation_can_be_disabled_without_window_resize(monkeypatch):
    sender = WindowsUIASender()
    sender._ensure_full_layout = False
    driver = FakeDiagnosticDriver()
    monkeypatch.setattr(sender, "_navigation_controls_ready", lambda _driver: False)

    assert sender._ensure_foreground_navigation(driver) is None
    assert sender._last_binding_error["error_code"] == "navigation_controls_missing"


def test_visible_session_refuses_duplicate_names(monkeypatch):
    sender = WindowsUIASender()
    driver = FakeVisibleDriver([
        FakeSession("文件传输助手", "session_item_1"),
        FakeSession("文件传输助手", "session_item_2"),
    ])
    opened = sender._open_visible_session_without_mouse(driver, "文件传输助手")

    assert opened is False
    assert sender._last_navigation_error["error_code"] == "ambiguous_search_result"


def test_visible_session_does_not_reload_contact_database(monkeypatch):
    sender = WindowsUIASender()
    sender._background_post_message = True
    driver = FakeVisibleDriver([FakeSession("邢月小号群", "session_item_邢月小号群")])
    monkeypatch.setattr(
        driver,
        "_resolve_search_keyword",
        lambda _keyword: (_ for _ in ()).throw(
            AssertionError("visible exact-name navigation must not reload contact DB")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        sender,
        "_post_button_message_without_mouse",
        lambda *_args: (True, "PostMessage:WM_LBUTTON"),
    )
    monkeypatch.setattr(driver, "current_chat", lambda: "邢月小号群")

    assert sender._open_visible_session_without_mouse(driver, "邢月小号群") is True


def test_visible_session_accepts_verified_switch_when_pattern_returns_false(monkeypatch):
    sender = WindowsUIASender()
    sender._background_post_message = False
    driver = FakeVisibleDriver([FakeSession("邢月小号群", "session_item_target")])
    state = {"current_chat": "华旅"}

    def switch_without_acknowledgement(_control):
        state["current_chat"] = "邢月小号群"
        return False

    monkeypatch.setattr(sender, "_select_session_without_mouse", switch_without_acknowledgement)
    monkeypatch.setattr(driver, "current_chat", lambda: state["current_chat"])

    opened = sender._open_visible_session_without_mouse(driver, "邢月小号群")

    assert opened is True
    assert sender._last_navigation_error is None


def test_visible_session_falls_back_to_invoke_pattern_without_legacy(monkeypatch):
    sender = WindowsUIASender()
    sender._background_post_message = False
    driver = FakeVisibleDriver([FakeSession("邢月小号群", "session_item_target")])
    state = {"current_chat": "JL"}

    monkeypatch.setattr(sender, "_select_session_without_mouse", lambda _control: True)

    def invoke_session(_control):
        state["current_chat"] = "邢月小号群"
        return True

    monkeypatch.setattr(sender, "_invoke_session_without_mouse", invoke_session)
    monkeypatch.setattr(driver, "current_chat", lambda: state["current_chat"])

    opened = sender._open_visible_session_without_mouse(driver, "邢月小号群")

    assert opened is True
    assert sender._last_navigation_error is None


def test_visible_session_falls_back_to_post_message_without_mouse(monkeypatch):
    sender = WindowsUIASender()
    sender._background_post_message = True
    driver = FakeVisibleDriver([FakeSession("邢月小号群", "session_item_target")])
    state = {"current_chat": "JL"}

    monkeypatch.setattr(sender, "_select_session_without_mouse", lambda _control: True)
    monkeypatch.setattr(sender, "_invoke_session_without_mouse", lambda _control: True)

    def post_session(_driver, _control):
        state["current_chat"] = "邢月小号群"
        return True, "PostMessage:WM_LBUTTON"

    monkeypatch.setattr(sender, "_post_button_message_without_mouse", post_session)
    monkeypatch.setattr(driver, "current_chat", lambda: state["current_chat"])

    opened = sender._open_visible_session_without_mouse(driver, "邢月小号群")

    assert opened is True
    assert sender._last_navigation_error is None


def test_visible_session_failure_records_non_invasive_diagnostics(monkeypatch):
    sender = WindowsUIASender()
    sender._background_post_message = False
    driver = FakeVisibleDriver([FakeSession("邢月小号群", "session_item_target")])
    monkeypatch.setattr(sender, "_select_session_without_mouse", lambda _control: True)
    monkeypatch.setattr(driver, "current_chat", lambda: "JL")
    sender._ui_verify_timeout = 0.01

    opened = sender._open_visible_session_without_mouse(driver, "邢月小号群")

    assert opened is False
    assert sender._last_navigation_error["error_code"] == "chat_open_verification_failed"
    assert sender._last_navigation_error["selection_acknowledged"] is True
    assert sender._last_navigation_error["invoke_acknowledged"] is False
    assert sender._last_navigation_error["post_message_acknowledged"] is False
    assert sender._last_navigation_error["session_name"] == "邢月小号群"
    assert sender._last_navigation_error["current_chat"] == "JL"
    assert sender._last_navigation_error["input_present"] is True


def test_visible_session_selection_never_uses_legacy_default_action():
    sender = WindowsUIASender()

    class SelectionPattern:
        def Select(self, waitTime=0.1):
            return True

    class SessionControl:
        def GetPattern(self, _pattern_id):
            return SelectionPattern()

        def GetLegacyIAccessiblePattern(self):
            raise AssertionError("visible session must not use Legacy default action")

    assert sender._select_session_without_mouse(SessionControl()) is True


def test_visible_session_invoke_never_uses_legacy_default_action():
    sender = WindowsUIASender()

    class InvokePattern:
        def Invoke(self, waitTime=0.1):
            return True

    class SessionControl:
        def GetPattern(self, _pattern_id):
            return InvokePattern()

        def GetLegacyIAccessiblePattern(self):
            raise AssertionError("visible session must not use Legacy default action")

    assert sender._invoke_session_without_mouse(SessionControl()) is True


def test_search_open_requires_chat_input_after_title_match(monkeypatch):
    sender = WindowsUIASender()
    driver = FakeDriver()
    cell = FakeSession("文件传输助手", "search_item_1")
    monkeypatch.setattr(sender, "_set_text_without_mouse", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(driver, "_chat_input", lambda: None)
    monkeypatch.setattr(driver, "current_chat", lambda: "文件传输助手")
    monkeypatch.setattr(
        driver,
        "_search_box",
        lambda *_args, **_kwargs: driver.input,
        raising=False,
    )
    monkeypatch.setattr(
        driver,
        "_collect_results",
        lambda _keyword, **_kwargs: [{"cell": cell, "name": "文件传输助手", "section": "联系人"}],
        raising=False,
    )
    monkeypatch.setattr(sender, "_ensure_foreground_navigation", lambda value: value)
    monkeypatch.setattr(sender, "_invoke_without_mouse", lambda _control: True)

    opened = sender._open_chat_without_mouse(driver, "文件传输助手", False, background_mode=False)

    assert opened is False
    assert sender._last_navigation_error["error_code"] == "chat_open_verification_failed"


def test_search_retries_transient_empty_snapshot(monkeypatch):
    sender = WindowsUIASender()
    sender._uia_search_retries = 2
    sender._uia_search_settle = 0.5
    driver = FakeDriver()
    cell = FakeSession("测试联系人", "search_item_1")
    calls = []

    monkeypatch.setattr(sender, "_set_text_without_mouse", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(sender, "_ensure_foreground_navigation", lambda value: value)
    monkeypatch.setattr(sender, "_invoke_without_mouse", lambda _control: True)
    monkeypatch.setattr(
        driver,
        "_search_box",
        lambda *_args, **_kwargs: driver.input,
        raising=False,
    )
    monkeypatch.setattr(
        driver,
        "_collect_results",
        lambda _keyword, **_kwargs: calls.append(True)
        or ([] if len(calls) == 1 else [{"cell": cell, "name": "测试联系人", "section": "联系人"}]),
        raising=False,
    )

    opened = sender._open_chat_without_mouse(
        driver,
        "测试联系人",
        False,
        background_mode=False,
    )

    assert opened is True
    assert len(calls) == 2


def test_search_does_not_fallback_to_unsearchable_target_id(monkeypatch):
    sender = WindowsUIASender()
    driver = FakeDriver()
    attempted = []

    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(
        sender,
        "_open_chat_without_mouse",
        lambda _driver, keyword, *_args, **_kwargs: attempted.append(keyword) or False,
    )

    result = sender._send_text_once(
        "正文",
        "芙莉叶",
        False,
        "wxid_ybtkerfcizd422",
        "foreground_uia",
    )

    assert result.success is False
    assert result.stage == "search"
    assert attempted == ["芙莉叶"]


def test_uia_binding_refuses_to_guess_when_pid_is_unknown(monkeypatch):
    sender = WindowsUIASender()
    platform = SimpleNamespace(
        key_extractor=SimpleNamespace(
            selected_account=lambda: "wxid_selected_1",
            bound_account="",
            bound_pid=None,
        )
    )
    monkeypatch.setattr("app.core.platform.Platform.get", lambda: platform)

    result = sender._binding_info()

    assert result["status"] == "ambiguous_process"
    assert result["error_code"] == "ambiguous_process"


def test_uia_binding_rejects_account_mismatch(monkeypatch):
    sender = WindowsUIASender()
    platform = SimpleNamespace(
        key_extractor=SimpleNamespace(
            selected_account=lambda: "wxid_selected_1",
            bound_account="wxid_other_2",
            bound_pid=5678,
        )
    )
    monkeypatch.setattr("app.core.platform.Platform.get", lambda: platform)

    result = sender._binding_info()

    assert result["status"] == "account_binding_mismatch"
    assert result["error_code"] == "account_binding_mismatch"


def test_auto_selects_foreground_when_background_probe_is_incomplete(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    sender._allow_foreground_activation = True
    monkeypatch.setattr(
        sender,
        "_probe_background_capability",
        lambda: {
            "available": False,
            "reason_code": "background_patterns_incomplete",
            "reason": "send_button_invoke",
        },
    )

    assert sender._candidate_methods() == ["foreground_uia"]


def test_auto_keeps_foreground_fallback_when_background_probe_is_available(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    sender._allow_foreground_activation = True
    monkeypatch.setattr(sender, "_probe_background_capability", lambda: {"available": True})

    assert sender._candidate_methods() == ["background_uia", "foreground_uia"]


def test_auto_stops_when_background_probe_is_incomplete_and_foreground_is_disabled(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    sender._allow_foreground_activation = False
    monkeypatch.setattr(
        sender,
        "_probe_background_capability",
        lambda: {"available": False, "reason_code": "incomplete", "reason": "test"},
    )

    assert sender._candidate_methods() == []


def test_auto_does_not_retry_after_send_action(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    calls = []

    attempted = SendResult.for_message("你好", "wxid_target", "background_uia")
    attempted.action_performed = True
    attempted.fail("ui_verify", "ui_message_not_found", "未找到消息")

    def send_once(*_args):
        calls.append(True)
        return attempted

    monkeypatch.setattr(sender, "_candidate_methods", lambda: ["background_uia", "foreground_uia"])
    monkeypatch.setattr(sender, "_send_text_once", send_once)

    result = sender._send_text_sync_result("你好", "目标", False, "wxid_target")

    assert result is attempted
    assert calls == [True]


def test_auto_does_not_fallback_after_background_input_state_changed(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    calls = []

    attempted = SendResult.for_message("你好", "wxid_target", "background_uia")
    attempted.fail(
        "invoke",
        "background_input_state_changed",
        "前台状态变化",
    )

    def send_once(*_args):
        calls.append(True)
        return attempted

    monkeypatch.setattr(
        sender,
        "_candidate_methods",
        lambda: ["background_uia", "foreground_uia"],
    )
    monkeypatch.setattr(sender, "_send_text_once", send_once)

    result = sender._send_text_sync_result("你好", "目标", False, "wxid_target")

    assert result is attempted
    assert calls == [True]


def test_background_state_change_after_send_action_waits_for_database_verify(monkeypatch):
    sender = WindowsUIASender()
    sender._driver_pid = 22
    attempted = SendResult.for_message("你好", "wxid_target", "background_uia")
    attempted.action_performed = True
    monkeypatch.setattr(
        sender,
        "_hwnd_process_id",
        lambda hwnd: {1: 11, 2: 22, 3: 22}.get(hwnd, 0),
    )

    monkeypatch.setattr(
        sender,
        "_foreground_input_state",
        lambda: {
            "foreground_hwnd": 2,
            "focus_hwnd": 3,
            "cursor_x": 4,
            "cursor_y": 5,
        },
    )

    unchanged = sender._verify_background_state(
        attempted,
        {
            "foreground_hwnd": 1,
            "focus_hwnd": 1,
            "cursor_x": 4,
            "cursor_y": 5,
        },
        "after_invoke",
    )

    assert unchanged is False
    assert attempted.status == "pending_verify"
    assert attempted.stage == "db_verify"
    assert attempted.error_code == "db_verification_deferred"


def test_background_guard_allows_user_cursor_and_window_changes(monkeypatch):
    sender = WindowsUIASender()
    sender._driver_pid = 99
    result = SendResult.for_message("你好", "wxid_target", "background_uia")
    monkeypatch.setattr(
        sender,
        "_foreground_input_state",
        lambda: {
            "foreground_hwnd": 2,
            "focus_hwnd": 0,
            "cursor_x": 800,
            "cursor_y": 600,
        },
    )
    monkeypatch.setattr(
        sender,
        "_hwnd_process_id",
        lambda hwnd: {1: 10, 2: 20}.get(hwnd, 0),
    )

    safe = sender._verify_background_state(
        result,
        {
            "foreground_hwnd": 1,
            "focus_hwnd": 0,
            "cursor_x": 100,
            "cursor_y": 100,
        },
        "after_window",
    )

    assert safe is True
    guard = result.details["background_guard"]["after_window"]
    assert guard["unchanged"] is False
    assert guard["user_state_changed"] is True
    assert guard["weixin_activated"] is False


def test_background_guard_rejects_weixin_stealing_foreground(monkeypatch):
    sender = WindowsUIASender()
    sender._driver_pid = 99
    result = SendResult.for_message("你好", "wxid_target", "background_uia")
    monkeypatch.setattr(
        sender,
        "_foreground_input_state",
        lambda: {
            "foreground_hwnd": 9,
            "focus_hwnd": 0,
            "cursor_x": 100,
            "cursor_y": 100,
        },
    )
    monkeypatch.setattr(
        sender,
        "_hwnd_process_id",
        lambda hwnd: {1: 10, 9: 99}.get(hwnd, 0),
    )

    safe = sender._verify_background_state(
        result,
        {
            "foreground_hwnd": 1,
            "focus_hwnd": 0,
            "cursor_x": 100,
            "cursor_y": 100,
        },
        "after_window",
    )

    assert safe is False
    assert result.error_code == "background_input_state_changed"


def test_background_retries_then_switches_to_foreground(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "auto"
    sender._background_attempts = 2
    sender._foreground_attempts = 1
    calls = []

    def send_once(_msg, _receiver, _is_group, _target_id, method, _attempt_id=""):
        calls.append(method)
        result = SendResult.for_message("你好", "wxid_target", method)
        if method == "background_uia":
            return result.fail("search", "background_transient", "后台暂时找不到会话")
        return result.sent("ui_verify", action_performed=True, ui_verified=True)

    monkeypatch.setattr(
        sender,
        "_candidate_methods",
        lambda: ["background_uia", "foreground_uia"],
    )
    monkeypatch.setattr(sender, "_send_text_once", send_once)

    result = sender._send_text_sync_result("你好", "目标", False, "wxid_target")

    assert result.success is True
    assert calls == ["background_uia", "background_uia", "foreground_uia"]
    assert [item["method"] for item in result.details["delivery_attempts"]] == calls
    assert result.details["delivery_policy"]["background_attempts"] == 2


def test_foreground_retries_until_configured_limit(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._foreground_attempts = 2
    calls = []

    def send_once(*_args):
        calls.append(True)
        result = SendResult.for_message("你好", "wxid_target", "foreground_uia")
        if len(calls) == 1:
            return result.fail("draft", "draft_transient", "正文暂时写入失败")
        return result.sent("ui_verify", action_performed=True, ui_verified=True)

    monkeypatch.setattr(sender, "_candidate_methods", lambda: ["foreground_uia"])
    monkeypatch.setattr(sender, "_send_text_once", send_once)

    result = sender._send_text_sync_result("你好", "目标", False, "wxid_target")

    assert result.success is True
    assert len(calls) == 2
    assert result.details["delivery_attempts"][0]["error_code"] == "draft_transient"


def test_attempt_limit_exhaustion_returns_explicit_failure(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "background_uia"
    sender._background_attempts = 2
    calls = []

    def send_once(*_args):
        calls.append(True)
        return SendResult.for_message("你好", "wxid_target", "background_uia").fail(
            "window", "background_unavailable", "后台窗口暂不可用"
        )

    monkeypatch.setattr(sender, "_candidate_methods", lambda: ["background_uia"])
    monkeypatch.setattr(sender, "_send_text_once", send_once)

    result = sender._send_text_sync_result("你好", "目标", False, "wxid_target")

    assert result.success is False
    assert result.status == "failed"
    assert result.details["attempts_exhausted"] is True
    assert len(result.details["delivery_attempts"]) == 2
    assert len(calls) == 2


def test_uia_retries_text_after_readback_mismatch(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._send_key_fallback = "none"
    sender._require_ui_verify = False
    driver = FakeDriver()
    calls = []

    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: FakeSendButton())

    def write_text(_driver, control, text, allow_focus_fallback=True, prefer_focus_fallback=False):
        calls.append(prefer_focus_fallback)
        control.value_pattern.value = text if prefer_focus_fallback else "错误正文"
        return True

    monkeypatch.setattr(sender, "_set_text_without_mouse", write_text)

    def invoke(_control):
        driver.input.value_pattern.value = ""
        return True, "InvokePattern"

    monkeypatch.setattr(sender, "_invoke_control", invoke)

    result = sender._send_text_once(
        "正确正文",
        "测试联系人",
        False,
        "wxid_target",
        "foreground_uia",
    )

    assert result.success is True
    assert calls == [False, True]
    assert result.draft_cleared is True


def test_uia_retries_remaining_button_pattern_after_false_positive_invoke(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._send_key_fallback = "none"
    sender._require_ui_verify = False
    sender._input_verify_timeout = 0.01
    driver = FakeDriver()
    button = FakeFallbackSendButton(driver.input)

    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: button)
    monkeypatch.setattr(
        sender,
        "_invoke_control",
        lambda _control: (True, "InvokePattern"),
    )

    result = sender._send_text_once(
        "第一次调用无效",
        "测试联系人",
        False,
        "wxid_target",
        "foreground_uia",
    )

    assert result.success is True
    assert result.draft_cleared is True
    assert result.details["invoke_attempts"] == [
        "InvokePattern",
        "LegacyIAccessible.DoDefaultAction",
    ]
    assert result.details["invoke_method"] == (
        "InvokePattern -> LegacyIAccessible.DoDefaultAction"
    )


def test_foreground_uia_focuses_input_before_invoke(monkeypatch):
    sender = WindowsUIASender()
    sender._send_mode = "foreground_uia"
    sender._send_key_fallback = "none"
    sender._require_ui_verify = False
    driver = FakeDriver()

    monkeypatch.setattr(sender, "_ensure_driver_window", lambda _method=None: driver)
    monkeypatch.setattr(
        sender,
        "_set_text_without_mouse",
        lambda _driver, control, text, **_kwargs: control.value_pattern.__setattr__("value", text) or True,
    )
    monkeypatch.setattr(sender, "_find_send_button", lambda *_args: FakeSendButton())

    def invoke(_control):
        assert driver.input.focused is True
        driver.input.value_pattern.value = ""
        return True, "LegacyIAccessible.DoDefaultAction"

    monkeypatch.setattr(sender, "_invoke_control", invoke)

    result = sender._send_text_once(
        "需要焦点",
        "测试联系人",
        False,
        "wxid_target",
        "foreground_uia",
    )

    assert result.success is True
    assert result.details["input_focused"] is True


def test_uia_rebuilds_driver_when_bound_account_changes(monkeypatch):
    sender = WindowsUIASender()
    binding = {
        "selected_account": "wxid_account_a",
        "bound_account": "wxid_account_a",
        "bound_pid": 5678,
        "status": "bound",
        "error_code": "",
        "error_message": "",
    }
    created = []

    class FakeBoundDriver:
        def __init__(self, pid):
            self.pid = pid

    class FakeDriverFactory:
        def __init__(self, pid):
            created.append(pid)
            self.driver = FakeBoundDriver(pid)

    monkeypatch.setattr(
        "app.core.sender_windows_uia._SelectedWeChatUIA",
        FakeDriverFactory,
    )
    monkeypatch.setattr(sender, "_binding_info", lambda: binding)

    first = sender._get_driver()
    assert sender._get_driver() is first

    binding["selected_account"] = "wxid_account_b"
    binding["bound_account"] = "wxid_account_b"
    second = sender._get_driver()

    assert second is not first
    assert created == [5678, 5678]
