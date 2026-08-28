"""Windows 微信发送器的客户区坐标与本机校准配置。"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.utils.paths import get_data_dir

logger = logging.getLogger(__name__)

CALIBRATION_VERSION = 1
CALIBRATION_FILENAME = "windows_sender_calibration.json"
MIN_CLIENT_WIDTH = 720
MIN_CLIENT_HEIGHT = 520
WECHAT_WINDOW_TITLES = {"微信", "WeChat"}
WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}


@dataclass(frozen=True)
class ClientGeometry:
    """微信客户区在虚拟桌面中的物理像素位置。"""

    left: int
    top: int
    width: int
    height: int
    dpi: int = 96

    @property
    def scale(self) -> float:
        return max(float(self.dpi), 1.0) / 96.0


POINT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "search_input": {
        "label": "搜索框",
        "anchor": "top_left",
        "x": 173.0,
        "y": 49.0,
        "region": "search",
    },
    "search_clear": {
        "label": "清空搜索",
        "anchor": "top_left",
        "x": 247.0,
        "y": 49.0,
        "region": "search",
    },
    "private_search_result": {
        "label": "私聊结果",
        "anchor": "top_left",
        "x": 153.0,
        "y": 130.0,
        "region": "result",
    },
    "group_search_result": {
        "label": "群聊结果",
        "anchor": "top_left",
        "x": 153.0,
        "y": 169.0,
        "region": "result",
    },
    "message_input": {
        "label": "消息输入区",
        "anchor": "ratio",
        "x": 0.5,
        "y": 0.88,
        "region": "input",
    },
    "send_button": {
        "label": "发送按钮",
        "anchor": "bottom_right",
        "x": 72.0,
        "y": 40.0,
        "region": "send",
    },
    "paste_menu": {
        "label": "粘贴菜单项",
        "anchor": "top_left",
        "x": 24.0,
        "y": 15.0,
        "region": "popup",
    },
}


def calibration_path() -> Path:
    return get_data_dir() / CALIBRATION_FILENAME


def default_calibration(
    wechat_version: str = "",
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """返回由旧配置值迁移出的客户区预测点。"""
    return {
        "version": CALIBRATION_VERSION,
        "confirmed": bool(confirmed),
        "wechat_version": str(wechat_version or ""),
        "reference_dpi": 96,
        "points": {
            name: {
                "anchor": definition["anchor"],
                "x": definition["x"],
                "y": definition["y"],
            }
            for name, definition in POINT_DEFINITIONS.items()
        },
    }


def load_calibration(path: Path | None = None) -> dict[str, Any] | None:
    target = path or calibration_path()
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取微信点击校准失败: %s", exc)
        return None
    return value if isinstance(value, dict) else None


def save_calibration(profile: dict[str, Any], path: Path | None = None) -> Path:
    """原子保存本机坐标；文件只包含数值和微信版本。"""
    target = path or calibration_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def calibration_is_compatible(
    profile: dict[str, Any] | None,
    wechat_version: str,
) -> tuple[bool, str]:
    if not isinstance(profile, dict):
        return False, "未完成微信点击校准"
    if profile.get("version") != CALIBRATION_VERSION:
        return False, "微信点击校准格式已过期"
    if profile.get("confirmed") is not True:
        return False, "微信点击校准尚未确认"
    points = profile.get("points")
    if not isinstance(points, dict) or any(
        name not in points for name in POINT_DEFINITIONS
    ):
        return False, "微信点击校准点不完整"
    for name, definition in POINT_DEFINITIONS.items():
        point = points.get(name)
        if not isinstance(point, dict):
            return False, f"微信点击校准点格式错误: {definition['label']}"
        anchor = str(point.get("anchor") or "")
        if anchor != definition["anchor"]:
            return False, f"微信点击校准锚点错误: {definition['label']}"
        try:
            x_value = float(point.get("x"))
            y_value = float(point.get("y"))
        except (TypeError, ValueError):
            return False, f"微信点击校准坐标错误: {definition['label']}"
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            return False, f"微信点击校准坐标错误: {definition['label']}"
    saved_version = str(profile.get("wechat_version") or "").strip()
    current_version = str(wechat_version or "").strip()
    if not saved_version or not current_version:
        return False, "无法确认微信版本，已停止坐标点击"
    if saved_version != current_version:
        return False, f"微信版本已从 {saved_version} 变为 {current_version}，请重新校准"
    return True, ""


def get_client_geometry(hwnd: int) -> ClientGeometry:
    """读取真实客户区并转换为物理屏幕坐标。"""
    import win32gui

    client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (client_left, client_top))
    width = int(client_right - client_left)
    height = int(client_bottom - client_top)
    if width < MIN_CLIENT_WIDTH or height < MIN_CLIENT_HEIGHT:
        raise RuntimeError(
            f"微信客户区过小（{width}x{height}），请恢复正常窗口后重新尝试"
        )

    dpi = 96
    try:
        import ctypes

        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        if get_dpi:
            dpi = int(get_dpi(int(hwnd)) or 96)
    except Exception:
        dpi = 96
    return ClientGeometry(
        left=int(screen_left),
        top=int(screen_top),
        width=width,
        height=height,
        dpi=max(dpi, 96),
    )


def resolve_point(
    profile: dict[str, Any],
    name: str,
    geometry: ClientGeometry,
) -> tuple[int, int]:
    """按客户区锚点解析物理屏幕坐标，并执行区域检查。"""
    if name not in POINT_DEFINITIONS:
        raise KeyError(name)
    raw = profile.get("points", {}).get(name)
    if not isinstance(raw, dict):
        raise RuntimeError(f"缺少校准点: {POINT_DEFINITIONS[name]['label']}")

    anchor = str(raw.get("anchor") or POINT_DEFINITIONS[name]["anchor"])
    value_x = float(raw.get("x"))
    value_y = float(raw.get("y"))
    scale = geometry.scale
    if anchor == "top_left":
        x = geometry.left + round(value_x * scale)
        y = geometry.top + round(value_y * scale)
    elif anchor == "bottom_right":
        x = geometry.left + geometry.width - round(value_x * scale)
        y = geometry.top + geometry.height - round(value_y * scale)
    elif anchor == "ratio":
        if not (0.0 < value_x < 1.0 and 0.0 < value_y < 1.0):
            raise RuntimeError(f"校准比例无效: {POINT_DEFINITIONS[name]['label']}")
        x = geometry.left + round(geometry.width * value_x)
        y = geometry.top + round(geometry.height * value_y)
    else:
        raise RuntimeError(f"不支持的校准锚点: {anchor}")

    validate_point(name, x, y, geometry)
    return int(x), int(y)


def validate_point(
    name: str,
    x: int,
    y: int,
    geometry: ClientGeometry,
) -> None:
    """限制点击点只能落在对应的微信客户区功能区域。"""
    relative_x = int(x) - geometry.left
    relative_y = int(y) - geometry.top
    if not (0 < relative_x < geometry.width and 0 < relative_y < geometry.height):
        raise RuntimeError(f"{POINT_DEFINITIONS[name]['label']}超出微信客户区")

    scale = geometry.scale
    region = POINT_DEFINITIONS[name]["region"]
    left_panel_limit = min(round(380 * scale), round(geometry.width * 0.48))
    if region == "search":
        valid = relative_x < left_panel_limit and relative_y < round(120 * scale)
    elif region == "result":
        valid = (
            relative_x < left_panel_limit
            and round(70 * scale) < relative_y < round(360 * scale)
        )
    elif region == "input":
        valid = (
            relative_x > max(round(300 * scale), round(geometry.width * 0.3))
            and relative_y > round(geometry.height * 0.64)
            and relative_y < geometry.height - round(24 * scale)
        )
    elif region == "send":
        valid = (
            relative_x > round(geometry.width * 0.62)
            and relative_y > round(geometry.height * 0.72)
        )
    elif region == "popup":
        valid = True
    else:
        valid = False
    if not valid:
        raise RuntimeError(
            f"{POINT_DEFINITIONS[name]['label']}不在预期区域，已停止点击，请重新校准"
        )


def point_to_profile_value(
    name: str,
    relative_x: float,
    relative_y: float,
    geometry: ClientGeometry,
) -> dict[str, float | str]:
    """把预览图中的客户区坐标转换为可跨 DPI 使用的锚点值。"""
    anchor = POINT_DEFINITIONS[name]["anchor"]
    scale = geometry.scale
    if anchor == "top_left":
        x_value = float(relative_x) / scale
        y_value = float(relative_y) / scale
    elif anchor == "bottom_right":
        x_value = float(geometry.width - relative_x) / scale
        y_value = float(geometry.height - relative_y) / scale
    elif anchor == "ratio":
        x_value = float(relative_x) / float(geometry.width)
        y_value = float(relative_y) / float(geometry.height)
    else:
        raise RuntimeError(f"不支持的校准锚点: {anchor}")
    return {
        "anchor": anchor,
        "x": round(x_value, 6),
        "y": round(y_value, 6),
    }


def get_process_file_version(hwnd: int) -> str:
    try:
        import psutil
        import win32api
        import win32process

        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        executable = psutil.Process(pid).exe()
        info = win32api.GetFileVersionInfo(executable, "\\")
        ms = int(info["FileVersionMS"])
        ls = int(info["FileVersionLS"])
        return ".".join(
            str(value)
            for value in (
                ms >> 16,
                ms & 0xFFFF,
                ls >> 16,
                ls & 0xFFFF,
            )
        )
    except Exception as exc:
        logger.debug("读取微信版本失败: %s", exc)
        return ""


def find_wechat_main_window(*, visible_only: bool = False) -> int | None:
    """只读查找微信主窗口，供管理器校准使用。"""
    try:
        import psutil
        import win32gui
        import win32process
    except Exception:
        return None

    matches: list[int] = []

    def enum_handler(hwnd, _extra):
        title = str(win32gui.GetWindowText(hwnd) or "").strip()
        if title not in WECHAT_WINDOW_TITLES:
            return
        if visible_only and (
            not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)
        ):
            return
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in WECHAT_PROCESS_NAMES:
                return
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        if right - left < 400 or bottom - top < 300:
            return
        matches.append(int(hwnd))

    try:
        win32gui.EnumWindows(enum_handler, None)
    except Exception:
        return None
    return matches[0] if matches else None


def enable_per_monitor_dpi_awareness() -> None:
    """尽早启用 Per-Monitor V2；已由宿主设置时保持其现状。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
