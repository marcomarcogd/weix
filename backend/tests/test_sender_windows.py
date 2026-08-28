"""Windows 平台消息发送器测试。

验证 WindowsSender 通过鼠标点击和微信右键粘贴菜单操作 GUI。
所有测试 mock pyautogui 调用，验证操作序列而非实际 GUI 行为。
"""

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyautogui
import pyperclip
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def mock_pyautogui():
    """全局 mock pyautogui，防止测试中误操作真实 GUI。"""
    with (
        patch("pyautogui.press", MagicMock()),
        patch("pyautogui.hotkey", MagicMock()),
        patch("pyautogui.click", MagicMock()),
        patch("pyautogui.rightClick", MagicMock()),
        patch("pyautogui.size", MagicMock(return_value=(1920, 1080))),
        patch("pyperclip.copy", MagicMock()),
    ):
        yield


class FakeWindow:
    """模拟 pygetwindow 窗口对象。"""
    def __init__(self, left=0, top=0, width=1200, height=800, hwnd=123):
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.hwnd = hwnd

    def activate(self):
        pass


def _make_sender():
    """创建带 mock 窗口的 WindowsSender。"""
    from app.core.sender_windows import WindowsSender
    from app.core.windows_sender_calibration import ClientGeometry, default_calibration

    sender = WindowsSender()
    sender._send_method = "legacy_coordinates"
    sender._window_activate_delay = 0
    sender._search_result_delay = 0
    sender._type_delay = 0
    sender._skip_search_ttl = 60
    sender._verify_after_send = False
    sender._park_after_send = False
    sender._calibration = default_calibration("test", confirmed=True)
    sender._assert_calibration_ready = MagicMock()
    sender._get_active_client_geometry = MagicMock(
        return_value=ClientGeometry(0, 0, 1200, 800, 96)
    )
    sender._wechat_popup_windows = MagicMock(return_value={})
    sender._wait_for_new_wechat_popup = MagicMock(
        return_value=(100, 200, 160, 120)
    )
    return sender


def _mock_window(sender, window=None):
    """注入假窗口。"""
    if window is None:
        window = FakeWindow()
    sender._find_wechat_window = MagicMock(return_value=window)
    sender._is_wechat_foreground = MagicMock(return_value=True)
    return window


@pytest.mark.asyncio
async def test_send_text_full_search_flow():
    """完整搜索发送：点击搜索框 → 粘贴名称 → 点击结果 → 粘贴消息 → 点击发送。"""
    sender = _make_sender()
    _mock_window(sender)

    ok = await sender.send_text("你好", "文件传输助手")

    assert ok is True
    # 不使用键盘快捷键或按键，避免焦点错误导致退出/误操作。
    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()
    # 验证消息已复制到剪贴板
    pyperclip.copy.assert_any_call("你好")
    pyperclip.copy.assert_any_call("文件传输助手")


@pytest.mark.asyncio
async def test_send_text_skip_search_same_receiver():
    """同接收者在 TTL 内应跳过搜索。"""
    sender = _make_sender()
    _mock_window(sender)

    # 第一次：完整搜索
    ok = await sender.send_text("第一条", "文件传输助手")
    assert ok is True
    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()

    # 重置 mock 调用记录
    pyautogui.hotkey.reset_mock()

    # 第二次：同接收者，应跳过搜索
    await sender.send_text("第二条", "文件传输助手")

    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()


@pytest.mark.asyncio
async def test_send_text_group_chat_always_full_search():
    """群聊始终使用完整搜索，不跳过。"""
    sender = _make_sender()
    _mock_window(sender)

    # 两次群聊，每次都应完整搜索；点击“群聊”分区结果，避开顶部搜一搜。
    await sender.send_text("消息1", "测试群", is_group=True)
    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()
    assert (153, 169) in [call.args for call in pyautogui.click.call_args_list]

    pyautogui.hotkey.reset_mock()
    pyautogui.press.reset_mock()
    pyautogui.click.reset_mock()

    await sender.send_text("消息2", "测试群", is_group=True)
    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()
    assert (153, 169) in [call.args for call in pyautogui.click.call_args_list]


@pytest.mark.asyncio
async def test_send_text_empty_msg_returns_false():
    """空消息直接返回 False。"""
    sender = _make_sender()

    ok = await sender.send_text("", "wxid_test")
    assert ok is False

    ok = await sender.send_text("hello", "")
    assert ok is False


@pytest.mark.asyncio
async def test_send_text_no_wechat_window():
    """微信未运行时应返回 False。"""
    sender = _make_sender()
    sender._find_wechat_window = MagicMock(return_value=None)

    ok = await sender.send_text("你好", "测试")
    assert ok is False


@pytest.mark.asyncio
async def test_open_chat_searches_without_sending():
    """open_chat 应执行搜索但不发送消息。"""
    sender = _make_sender()
    _mock_window(sender)

    ok = await sender.open_chat("小号")
    assert ok is True
    pyautogui.hotkey.assert_not_called()
    pyautogui.press.assert_not_called()
    # 不应复制消息文本
    for call_args in pyperclip.copy.call_args_list:
        if call_args[0]:
            assert call_args[0][0] == "小号"


def test_park_after_send_searches_file_transfer_assistant_once():
    """Windows 发送器独占停靠动作，每次成功发送后只搜索一次。"""
    sender = _make_sender()
    sender._park_after_send = True
    sender._parking_receiver = "文件传输助手"
    sender._full_search = MagicMock()

    sender._park_if_needed("测试群")

    sender._full_search.assert_called_once_with("文件传输助手", is_group=False)
    assert sender._last_receiver == "文件传输助手"


def test_reset_search_state():
    """reset_search_state 清空免搜索状态。"""
    sender = _make_sender()
    sender._last_receiver = "someone"
    sender._last_send_time = time.monotonic()

    sender.reset_search_state()
    assert sender._last_receiver == ""
    assert sender._last_send_time == 0.0


def test_unconfirmed_send_is_never_retried():
    """数据库延迟时发送按钮只能点击一次，不能因未确认而补发。"""
    sender = _make_sender()
    _mock_window(sender)
    sender._verify_after_send = True
    sender._last_receiver = "测试联系人"
    sender._last_send_time = time.monotonic()
    sender._full_search = MagicMock()
    sender._activate_wechat = MagicMock()
    sender._focus_message_input = MagicMock(return_value=(10, 20))
    sender._paste_text = MagicMock()
    sender._click_send_button = MagicMock()
    sender._verify_sent_text = MagicMock(return_value=False)
    sender._confirmation_is_available = MagicMock(return_value=True)
    confirmation = MagicMock()
    sender._begin_send_confirmation = MagicMock(return_value=confirmation)

    ok = sender._send_text_sync(
        "只发一次",
        "测试联系人",
        force_skip=False,
        is_group=False,
        target_id="wxid_target",
    )

    assert ok is False
    sender._click_send_button.assert_called_once_with()
    sender._full_search.assert_not_called()
    assert sender._last_receiver == ""
    confirmation.cancel.assert_called_once_with()


def test_activate_wechat_aborts_when_foreground_cannot_be_obtained():
    """激活重试后仍不是微信前台时，必须在任何鼠标点击前中止。"""
    sender = _make_sender()
    window = FakeWindow(hwnd=123)
    sender._find_wechat_window = MagicMock(return_value=window)
    sender._ensure_window_visible = MagicMock(return_value=window)
    sender._is_wechat_foreground = MagicMock(return_value=False)
    sender._request_wechat_foreground = MagicMock()

    with pytest.raises(RuntimeError, match="未成功切换到前台"):
        sender._activate_wechat()

    assert sender._request_wechat_foreground.call_count == 3
    assert sender._active_wechat_hwnd is None
    pyautogui.click.assert_not_called()
    pyautogui.rightClick.assert_not_called()


def test_context_menu_paste_aborts_when_chrome_has_focus():
    """Chrome 抢焦点后不得右击，以免误触 Google 识图菜单。"""
    sender = _make_sender()
    sender._active_wechat_hwnd = 123
    sender._is_wechat_foreground = MagicMock(return_value=False)

    with pytest.raises(RuntimeError, match="失去前台焦点"):
        sender._paste_text_via_context_menu(100, 200)

    pyautogui.rightClick.assert_not_called()
    pyautogui.click.assert_not_called()


def test_guarded_click_detects_focus_stolen_after_click():
    """点击期间焦点被抢走时必须立即报错，不再执行后续动作。"""
    sender = _make_sender()
    sender._active_wechat_hwnd = 123
    sender._is_wechat_foreground = MagicMock(side_effect=[True, False])

    with pytest.raises(RuntimeError, match="失去前台焦点"):
        sender._guarded_click(100, 200, "测试点击")

    pyautogui.click.assert_called_once_with(100, 200)


def test_pre_send_skip_failure_falls_back_to_one_full_search():
    """免搜索在点击发送前失败时，可以安全地完整搜索一次。"""
    sender = _make_sender()
    _mock_window(sender)
    sender._last_receiver = "测试联系人"
    sender._last_send_time = time.monotonic()
    sender._full_search = MagicMock()
    sender._activate_wechat = MagicMock()
    sender._focus_message_input = MagicMock(
        side_effect=[RuntimeError("输入框尚未就绪"), (10, 20)]
    )
    sender._paste_text = MagicMock()
    sender._click_send_button = MagicMock()

    ok = sender._send_text_sync(
        "安全恢复",
        "测试联系人",
        force_skip=False,
        is_group=False,
        target_id="wxid_target",
    )

    assert ok is True
    sender._full_search.assert_called_once_with("测试联系人", is_group=False)
    sender._click_send_button.assert_called_once_with()


def test_send_action_exception_is_never_retried():
    """点击调用即使抛异常也视为可能已发送，禁止再次执行。"""
    sender = _make_sender()
    _mock_window(sender)
    sender._last_receiver = "测试联系人"
    sender._last_send_time = time.monotonic()
    sender._full_search = MagicMock()
    sender._activate_wechat = MagicMock()
    sender._focus_message_input = MagicMock(return_value=(10, 20))
    sender._paste_text = MagicMock()
    sender._click_send_button = MagicMock(side_effect=RuntimeError("点击结果未知"))
    sender._verify_sent_text = MagicMock()
    sender._confirmation_is_available = MagicMock(return_value=True)
    confirmation = MagicMock()
    sender._begin_send_confirmation = MagicMock(return_value=confirmation)

    ok = sender._send_text_sync(
        "不能补发",
        "测试联系人",
        force_skip=False,
        is_group=False,
        target_id="wxid_target",
    )

    assert ok is False
    sender._click_send_button.assert_called_once_with()
    sender._verify_sent_text.assert_not_called()
    sender._full_search.assert_not_called()
    assert sender._last_receiver == ""
    confirmation.cancel.assert_called_once_with()


@pytest.mark.asyncio
async def test_is_wechat_running_true():
    """有微信窗口时返回 True。"""
    sender = _make_sender()
    _mock_window(sender)

    running = await sender.is_wechat_running()
    assert running is True


@pytest.mark.asyncio
async def test_is_wechat_running_false():
    """无微信窗口时返回 False。"""
    sender = _make_sender()
    sender._find_wechat_window = MagicMock(return_value=None)

    running = await sender.is_wechat_running()
    assert running is False


def test_find_wechat_window_accepts_minimized_main_window():
    """最小化微信仍应识别为在线，发送前再恢复窗口。"""
    from app.core.sender_windows import WindowsSender

    process = MagicMock()
    process.name.return_value = "Weixin.exe"

    def enum_windows(callback, extra):
        callback(123, extra)

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.GetWindowRect", return_value=(-32000, -32000, -31840, -31972)),
        patch("win32gui.IsIconic", return_value=True),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 456)),
        patch("psutil.Process", return_value=process),
    ):
        window = WindowsSender._find_wechat_window_win32()

    assert window is not None
    assert window.hwnd == 123
    assert window.title == "微信"


class FakeUIAAdapter:
    """只模拟 UIA 语义，不接触真实窗口、键盘或鼠标。"""

    def __init__(self):
        self.window = object()
        self.input = object()
        self.button = object()
        self.activations = []
        self.open_visible_calls = 0
        self.search_calls = 0
        self.post_calls = 0
        self.invoke_calls = 0
        self.visible_error = None
        self.post_result = True
        self.states = [(10, 20, 30, 40)] * 10
        self.text = ""

    def input_state(self):
        return self.states.pop(0)

    def main_window(self, pid, *, activate=False):
        assert pid == 4321
        self.activations.append(activate)
        return self.window

    def open_visible_session(self, window, receiver):
        assert window is self.window
        self.open_visible_calls += 1
        if self.visible_error:
            raise RuntimeError(self.visible_error)

    def search_and_open(self, window, receiver, is_group):
        assert window is self.window
        self.search_calls += 1

    def chat_input(self, window, receiver):
        return self.input

    def set_text(self, control, text, *, background):
        self.text = text
        return True

    def read_text(self, control):
        return self.text

    def send_button(self, window, input_control):
        return self.button

    def post_send(self, window, button):
        self.post_calls += 1
        if self.post_result:
            self.text = ""
        return self.post_result

    def invoke_send(self, button):
        self.invoke_calls += 1
        self.text = ""
        return True


def _uia_binding():
    return {
        "selected_account": "wxid_me",
        "bound_account": "wxid_me",
        "bound_pid": 4321,
    }


def test_uia_auto_prefers_background_and_posts_once():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = FakeUIAAdapter()
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._send_mode = "auto"
    sender._hot_activate_accessibility = False

    result = sender._send_text_sync_result(
        "1", "测试群", True, "room@chatroom"
    )

    assert result.action_performed is True
    assert result.status == "pending_verify"
    assert result.method == "background"
    assert adapter.open_visible_calls == 1
    assert adapter.search_calls == 0
    assert adapter.post_calls == 1
    assert adapter.invoke_calls == 0


def test_uia_auto_falls_back_foreground_only_before_send_action():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = FakeUIAAdapter()
    adapter.visible_error = "可见会话不存在"
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._send_mode = "auto"
    sender._hot_activate_accessibility = False

    result = sender._send_text_sync_result(
        "回复", "测试联系人", False, "wxid_friend"
    )

    assert result.action_performed is True
    assert result.method == "foreground"
    assert adapter.open_visible_calls == 1
    assert adapter.search_calls == 1
    assert adapter.post_calls == 0
    assert adapter.invoke_calls == 1


def test_uia_post_message_uncertain_never_switches_or_retries():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = FakeUIAAdapter()
    adapter.post_result = False
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._send_mode = "auto"
    sender._hot_activate_accessibility = False

    result = sender._send_text_sync_result(
        "只发一次", "测试群", True, "room@chatroom"
    )

    assert result.action_performed is True
    assert result.status == "pending_verify"
    assert adapter.post_calls == 1
    assert adapter.search_calls == 0
    assert adapter.invoke_calls == 0


def test_uia_background_user_state_change_stops_without_foreground_fallback():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = FakeUIAAdapter()
    adapter.states = [(10, 20, 30, 40), (11, 20, 30, 40)]
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._send_mode = "auto"
    sender._hot_activate_accessibility = False

    result = sender._send_text_sync_result(
        "不应发送", "测试群", True, "room@chatroom"
    )

    assert result.error_code == "background_state_changed"
    assert result.action_performed is False
    assert adapter.post_calls == 0
    assert adapter.search_calls == 0


def test_uia_missing_or_mismatched_account_binding_fails_closed():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = FakeUIAAdapter()
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=lambda: {
            "selected_account": "wxid_a",
            "bound_account": "wxid_b",
            "bound_pid": 4321,
        },
    )
    sender._hot_activate_accessibility = False

    result = sender._send_text_sync_result(
        "不应发送", "测试联系人", False, "wxid_friend"
    )

    assert result.error_code == "account_binding_unavailable"
    assert result.action_performed is False
    assert adapter.open_visible_calls == 0
    assert adapter.search_calls == 0


def test_direct_uia_main_window_anchors_from_bound_pid_handle():
    """不能依赖 UIA Root；应从 Win32 唯一句柄直接锚定 UIA。"""
    from app.core.sender_windows_uia import DirectUIAAdapter

    window = SimpleNamespace(
        ClassName="mmui::MainWindow",
        Name="当前聊天标题",
        ProcessId=4321,
        NativeWindowHandle=123,
    )
    auto = SimpleNamespace(ControlFromHandle=MagicMock(return_value=window))
    adapter = object.__new__(DirectUIAAdapter)
    adapter.auto = auto

    def enum_windows(callback, extra):
        assert callback(123, extra) is True
        assert callback(456, extra) is True

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch(
            "win32process.GetWindowThreadProcessId",
            side_effect=lambda hwnd: (1, 4321 if hwnd == 123 else 9999),
        ),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32gui.GetWindowRect", return_value=(0, 0, 1200, 800)),
    ):
        result = adapter.main_window(4321, activate=False)

    assert result is window
    auto.ControlFromHandle.assert_called_once_with(123)


def test_direct_uia_main_window_rejects_wrong_pid_before_uia_access():
    from app.core.sender_windows_uia import DirectUIAAdapter, UIAWindowError

    auto = SimpleNamespace(ControlFromHandle=MagicMock())
    adapter = object.__new__(DirectUIAAdapter)
    adapter.auto = auto

    def enum_windows(callback, extra):
        callback(123, extra)

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 9999)),
    ):
        with pytest.raises(UIAWindowError) as captured:
            adapter.main_window(4321)

    assert captured.value.code == "window_not_found"
    auto.ControlFromHandle.assert_not_called()


def test_direct_uia_main_window_accepts_verified_native_qt_shell():
    from app.core.sender_windows_uia import DirectUIAAdapter

    shell = SimpleNamespace(
        ClassName="Qt51514QWindowIcon",
        Name="微信",
        ProcessId=4321,
        NativeWindowHandle=123,
    )
    adapter = object.__new__(DirectUIAAdapter)
    adapter.auto = SimpleNamespace(ControlFromHandle=MagicMock(return_value=shell))

    with (
        patch("win32gui.EnumWindows", side_effect=lambda callback, extra: callback(123, extra)),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 4321)),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32gui.GetWindowRect", return_value=(0, 0, 1200, 800)),
    ):
        result = adapter.main_window(4321)

    assert result is shell


def test_direct_uia_main_window_rejects_unrelated_uia_class():
    from app.core.sender_windows_uia import DirectUIAAdapter, UIAWindowError

    unrelated = SimpleNamespace(
        ClassName="Chrome_WidgetWin_1",
        Name="微信",
        ProcessId=4321,
        NativeWindowHandle=123,
    )
    adapter = object.__new__(DirectUIAAdapter)
    adapter.auto = SimpleNamespace(ControlFromHandle=MagicMock(return_value=unrelated))

    with (
        patch("win32gui.EnumWindows", side_effect=lambda callback, extra: callback(123, extra)),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 4321)),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32gui.GetWindowRect", return_value=(0, 0, 1200, 800)),
    ):
        with pytest.raises(UIAWindowError) as captured:
            adapter.main_window(4321)

    assert captured.value.code == "uia_identity_mismatch"


def test_uia_hot_activator_writes_only_verified_zero_byte():
    from app.core.sender_windows_uia import AccessibilityHotActivator

    driver = SimpleNamespace(
        _weixin_dll_module=MagicMock(
            return_value=(0x10000000, 0x200000, r"C:\Weixin\4.1.13.12\Weixin.dll")
        ),
        _qaccessible_active_rva=MagicMock(return_value=0x1234),
        _read_process_byte=MagicMock(side_effect=[0, 1]),
        _write_process_byte=MagicMock(return_value=True),
    )
    kernel32 = SimpleNamespace(
        OpenProcess=MagicMock(return_value=987),
        CloseHandle=MagicMock(return_value=True),
    )
    activator = AccessibilityHotActivator(driver_cls=driver, kernel32=kernel32)

    with patch("win32process.GetWindowThreadProcessId", return_value=(1, 4321)):
        result = activator.activate(123, 4321)

    assert result["ok"] is True
    assert result["status"] == "activated"
    assert result["wrote_memory"] is True
    driver._write_process_byte.assert_called_once_with(987, 0x10001234, 1)
    assert driver._read_process_byte.call_count == 2
    kernel32.CloseHandle.assert_called_once_with(987)


def test_uia_hot_activator_rejects_unexpected_gate_value_without_write():
    from app.core.sender_windows_uia import AccessibilityHotActivator

    driver = SimpleNamespace(
        _weixin_dll_module=MagicMock(
            return_value=(0x10000000, 0x200000, r"C:\Weixin\4.1.13.12\Weixin.dll")
        ),
        _qaccessible_active_rva=MagicMock(return_value=0x1234),
        _read_process_byte=MagicMock(return_value=7),
        _write_process_byte=MagicMock(),
    )
    kernel32 = SimpleNamespace(
        OpenProcess=MagicMock(return_value=987),
        CloseHandle=MagicMock(return_value=True),
    )
    activator = AccessibilityHotActivator(driver_cls=driver, kernel32=kernel32)

    with patch("win32process.GetWindowThreadProcessId", return_value=(1, 4321)):
        result = activator.activate(123, 4321)

    assert result["ok"] is False
    assert result["status"] == "unexpected_value"
    assert result["wrote_memory"] is False
    driver._write_process_byte.assert_not_called()


def test_uia_hot_activation_requires_explicit_opt_in():
    from app.core.sender_windows_uia import WindowsUIASender

    adapter = MagicMock()
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._hot_activate_accessibility = False

    result = sender._activate_accessibility_sync()

    assert result["ok"] is False
    assert result["status"] == "disabled"
    assert result["attempted"] is False
    adapter.hot_activate_accessibility.assert_not_called()


def test_uia_hot_activation_rechecks_controls_after_single_write():
    from app.core.sender_windows_uia import WindowsUIASender

    shell = SimpleNamespace(ClassName="Qt51514QWindowIcon")
    adapter = MagicMock()
    adapter.main_window.return_value = shell
    adapter.search_box.side_effect = [RuntimeError("missing"), object()]
    adapter.session_list.return_value = object()
    adapter.hot_activate_accessibility.return_value = {
        "ok": True,
        "attempted": True,
        "status": "activated",
        "reason": "已完成 UIA gate 单字节热激活",
        "wrote_memory": True,
        "dll_version": "4.1.13.12",
    }
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )
    sender._hot_activate_accessibility = True

    with patch("app.core.sender_windows_uia.time.sleep", return_value=None):
        result = sender._activate_accessibility_sync()

    assert result["ok"] is True
    assert result["status"] == "activated"
    adapter.hot_activate_accessibility.assert_called_once_with(shell, 4321)


def test_uia_diagnosis_distinguishes_missing_visible_window():
    from app.core.sender_windows_uia import UIAWindowError, WindowsUIASender

    adapter = MagicMock()
    adapter.main_window.side_effect = UIAWindowError(
        "window_not_found",
        "请打开微信聊天主窗口",
    )
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )

    diagnosis = sender._diagnose_sync()

    assert diagnosis["reason_code"] == "window_not_found"
    assert diagnosis["narrator_hint"] is False
    assert "打开" in diagnosis["help"]


def test_uia_diagnosis_checks_controls_inside_verified_native_shell():
    from app.core.sender_windows_uia import WindowsUIASender

    shell = SimpleNamespace(ClassName="Qt51514QWindowIcon")
    adapter = MagicMock()
    adapter.main_window.return_value = shell
    adapter.search_box.side_effect = RuntimeError("missing")
    adapter.session_list.side_effect = RuntimeError("missing")
    adapter.descendants.return_value = []
    sender = WindowsUIASender(
        adapter_factory=lambda: adapter,
        binding_provider=_uia_binding,
    )

    diagnosis = sender._diagnose_sync()

    assert diagnosis["main_window"] is True
    assert diagnosis["window_class"] == "Qt51514QWindowIcon"
    assert diagnosis["descendant_count"] == 0
    assert diagnosis["reason_code"] == "uia_controls_missing"
    assert diagnosis["narrator_hint"] is False
    assert "搜索框" in diagnosis["reason"]
    assert "保持静默" in diagnosis["help"]


class FakeControl:
    def __init__(self, *, name="", aid="", children=None):
        self.Name = name
        self.AutomationId = aid
        self.ClassName = ""
        self.ControlTypeName = "ListItemControl"
        self.BoundingRectangle = SimpleNamespace(left=1, top=1, right=10, bottom=10)
        self._children = list(children or [])

    def GetChildren(self):
        return self._children


def _direct_adapter_without_import():
    from app.core.sender_windows_uia import DirectUIAAdapter

    adapter = object.__new__(DirectUIAAdapter)
    adapter.search_box = MagicMock(return_value=FakeControl())
    adapter.set_text = MagicMock(return_value=True)
    adapter.invoke = MagicMock(return_value=True)
    return adapter


def test_direct_uia_search_ignores_result_order_and_wrong_sections():
    """广告、聊天记录和结果顺序变化不能影响群聊精确选择。"""
    adapter = _direct_adapter_without_import()
    search_list = FakeControl(
        aid="search_list",
        children=[
            FakeControl(name="聊天记录"),
            FakeControl(name="测试群", aid="search_item_history"),
            FakeControl(name="其他信息", aid="search_item_ad"),
            FakeControl(name="群聊"),
            FakeControl(name="测试群", aid="search_item_room"),
        ],
    )
    window = FakeControl(children=[search_list])

    adapter.search_and_open(window, "测试群", is_group=True)

    adapter.invoke.assert_called_once_with(search_list.GetChildren()[-1])


def test_direct_uia_search_rejects_same_type_duplicate_name():
    adapter = _direct_adapter_without_import()
    search_list = FakeControl(
        aid="search_list",
        children=[
            FakeControl(name="群聊"),
            FakeControl(name="重名群", aid="search_item_room_1"),
            FakeControl(name="重名群", aid="search_item_room_2"),
        ],
    )
    window = FakeControl(children=[search_list])

    with pytest.raises(RuntimeError, match="重名"):
        adapter.search_and_open(window, "重名群", is_group=True)

    adapter.invoke.assert_not_called()


def test_uia_sender_source_keeps_hot_activation_opt_in_without_ocr_or_mouse_fallback():
    source = Path(
        os.path.join(os.path.dirname(__file__), "..", "app", "core", "sender_windows_uia.py")
    ).read_text(encoding="utf-8")

    assert 'cfg.get("hot_activate_accessibility", False)' in source
    assert "from wechatauto.uia_driver import WeChatUIA" in source
    assert "pyautogui" not in source
    assert "import ocr" not in source.casefold()
    assert "ocr_helper" not in source.casefold()


def test_find_wechat_window_accepts_hidden_tray_main_window():
    """隐藏到托盘的微信主窗口仍应识别为在线。"""
    from app.core.sender_windows import WindowsSender

    process = MagicMock()
    process.name.return_value = "Weixin.exe"

    callback_results = []

    def enum_windows(callback, extra):
        callback_results.append(callback(123, extra))

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch("win32gui.IsWindowVisible", return_value=False),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.GetWindowRect", return_value=(271, 170, 1104, 983)),
        patch("win32gui.IsIconic", return_value=False),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 456)),
        patch("psutil.Process", return_value=process),
    ):
        window = WindowsSender._find_wechat_window_win32()

    assert window is not None
    assert window.hwnd == 123
    assert window.title == "微信"
    assert callback_results == [True]


def test_find_wechat_window_rejects_small_non_minimized_window():
    """普通小工具窗口不能被误认成微信主窗口。"""
    from app.core.sender_windows import WindowsSender

    process = MagicMock()
    process.name.return_value = "Weixin.exe"

    def enum_windows(callback, extra):
        callback(123, extra)

    with (
        patch("win32gui.EnumWindows", side_effect=enum_windows),
        patch("win32gui.IsWindowVisible", return_value=True),
        patch("win32gui.GetWindowText", return_value="微信"),
        patch("win32gui.GetWindowRect", return_value=(100, 100, 260, 128)),
        patch("win32gui.IsIconic", return_value=False),
        patch("win32process.GetWindowThreadProcessId", return_value=(1, 456)),
        patch("psutil.Process", return_value=process),
    ):
        window = WindowsSender._find_wechat_window_win32()

    assert window is None


def test_minimized_window_refreshes_geometry_after_restore():
    """恢复最小化窗口后应使用新的屏幕坐标和尺寸。"""
    from app.core.sender_windows import _WindowRef

    window = _WindowRef(
        left=-32000,
        top=-32000,
        width=160,
        height=28,
        title="微信",
        hwnd=123,
    )

    with (
        patch("win32gui.IsIconic", return_value=True),
        patch("win32gui.ShowWindow", MagicMock()),
        patch("win32gui.SetForegroundWindow", MagicMock()),
        patch("win32gui.GetWindowRect", return_value=(100, 50, 1300, 850)),
    ):
        window.activate()

    assert (window.left, window.top, window.width, window.height) == (100, 50, 1200, 800)


def test_global_lock_serialization():
    """验证全局锁确保 GUI 操作串行。"""
    from app.core.sender_windows import WindowsSender

    lock = WindowsSender._gui_lock
    assert isinstance(lock, type(threading.Lock()))
    assert lock.acquire(blocking=False)  # 锁未被持有
    lock.release()


def test_ensure_window_visible_moves_offscreen_window():
    """非最大化微信窗口跑出屏幕时，应挪回可见区域再点击发送。"""
    sender = _make_sender()
    offscreen = FakeWindow(left=1173, top=47, width=922, height=802, hwnd=123)
    refreshed = FakeWindow(left=990, top=47, width=922, height=802, hwnd=123)
    sender._find_wechat_window = MagicMock(return_value=refreshed)

    with (
        patch("win32gui.GetWindowPlacement", MagicMock(return_value=(0, 1, None, None, None))),
        patch("win32gui.SetWindowPos", MagicMock()) as set_window_pos,
    ):
        result = sender._ensure_window_visible(offscreen)

    assert result is refreshed
    set_window_pos.assert_called_once()
    args = set_window_pos.call_args[0]
    assert args[2] == 990
    assert args[3] == 47


def test_client_coordinate_resolution_uses_client_origin_and_anchors():
    from app.core.windows_sender_calibration import (
        ClientGeometry,
        default_calibration,
        resolve_point,
    )

    profile = default_calibration("4.1.13.12", confirmed=True)
    geometry = ClientGeometry(left=110, top=80, width=1200, height=800, dpi=96)

    assert resolve_point(profile, "search_input", geometry) == (283, 129)
    assert resolve_point(profile, "message_input", geometry) == (710, 784)
    assert resolve_point(profile, "send_button", geometry) == (1238, 840)


def test_fixed_client_offsets_scale_with_window_dpi():
    from app.core.windows_sender_calibration import (
        ClientGeometry,
        default_calibration,
        resolve_point,
    )

    profile = default_calibration("4.1.13.12", confirmed=True)
    geometry = ClientGeometry(left=20, top=30, width=1500, height=900, dpi=144)

    assert resolve_point(profile, "search_input", geometry) == (280, 104)
    assert resolve_point(profile, "send_button", geometry) == (1412, 870)


def test_calibration_rejects_point_outside_expected_region():
    from app.core.windows_sender_calibration import (
        ClientGeometry,
        default_calibration,
        resolve_point,
    )

    profile = default_calibration("4.1.13.12", confirmed=True)
    profile["points"]["group_search_result"]["x"] = 700

    with pytest.raises(RuntimeError, match="不在预期区域"):
        resolve_point(
            profile,
            "group_search_result",
            ClientGeometry(0, 0, 1200, 800, 96),
        )


def test_calibration_requires_same_wechat_version():
    from app.core.windows_sender_calibration import (
        calibration_is_compatible,
        default_calibration,
    )

    profile = default_calibration("4.1.13.12", confirmed=True)

    compatible, reason = calibration_is_compatible(profile, "4.2.0.0")
    assert compatible is False
    assert "重新校准" in reason


def test_verification_source_missing_aborts_before_any_click():
    sender = _make_sender()
    sender._verify_after_send = True
    sender._confirmation_source = None
    sender._find_wechat_window = MagicMock()

    ok = sender._send_text_sync(
        "不能发送",
        "测试联系人",
        force_skip=False,
        is_group=False,
        target_id="wxid_target",
    )

    assert ok is False
    sender._find_wechat_window.assert_not_called()
    pyautogui.rightClick.assert_not_called()
    pyautogui.click.assert_not_called()


def test_unowned_or_missing_paste_popup_aborts_before_left_click():
    sender = _make_sender()
    sender._active_wechat_hwnd = 123
    sender._is_wechat_foreground = MagicMock(return_value=True)
    sender._wait_for_new_wechat_popup = MagicMock(return_value=None)

    with pytest.raises(RuntimeError, match="未识别到属于微信"):
        sender._paste_text_via_context_menu(600, 700)

    pyautogui.rightClick.assert_called_once_with(600, 700)
    pyautogui.click.assert_not_called()
