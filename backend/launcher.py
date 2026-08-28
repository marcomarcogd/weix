"""Weix Windows 图形管理器。

管理当前项目目录中的 FastAPI 后端与 Vite 前端。管理器本身不包含配置、
数据或密钥；PyInstaller --windowed 打包后也不会创建命令行窗口。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

import psutil
import yaml
from PyQt6.QtCore import QObject, QRectF, QLockFile, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.windows_sender_calibration import (
    CALIBRATION_FILENAME,
    POINT_DEFINITIONS,
    ClientGeometry,
    calibration_is_compatible,
    default_calibration,
    find_wechat_main_window,
    get_client_geometry,
    get_process_file_version,
    load_calibration,
    point_to_profile_value,
    resolve_point,
    save_calibration,
)


APP_TITLE = "Weix 服务管理器"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = 5173
BACKEND_HEALTH_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}/api/health"
FRONTEND_URL = f"http://{FRONTEND_HOST}:{FRONTEND_PORT}/"
STARTUP_TIMEOUT_SECONDS = 150.0
GRACEFUL_STOP_TIMEOUT_SECONDS = 20.0
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
SERVICE_STATE_VERSION = 1
SERVICE_STATE_FILENAME = ".weix_services.json"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ServiceState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    PARTIAL = auto()
    STOPPING = auto()
    RESTARTING = auto()
    ERROR = auto()


@dataclass
class ServiceProcessRef:
    """可跨管理器实例校验的本项目服务进程引用。"""

    kind: str
    pid: int
    create_time: float
    popen: Optional[subprocess.Popen[str]] = None
    adopted: bool = False


class LauncherSignals(QObject):
    log_received = pyqtSignal(str, str)
    start_finished = pyqtSignal(bool, str)
    stop_finished = pyqtSignal(bool, str)


class RotatingLogSink:
    """小型 UTF-8 轮转日志写入器，允许在运行中安全清理。"""

    def __init__(
        self,
        path: Path,
        max_bytes: int = LOG_MAX_BYTES,
        backup_count: int = LOG_BACKUP_COUNT,
    ):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str) -> None:
        clean = str(text).rstrip("\r\n")
        if not clean:
            return
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {clean}\n"
        encoded_size = len(line.encode("utf-8"))
        with self._lock:
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + encoded_size > self.max_bytes:
                self._rotate_locked()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)

    def clear(self) -> None:
        with self._lock:
            for candidate in self.managed_paths():
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def managed_paths(self) -> list[Path]:
        return [self.path] + [
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(1, self.backup_count + 1)
        ]

    def _rotate_locked(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))


MAIN_CALIBRATION_POINTS = (
    "search_input",
    "search_clear",
    "private_search_result",
    "group_search_result",
    "message_input",
    "send_button",
)


class CalibrationMarker(QGraphicsEllipseItem):
    """可在只读截图上拖动的校准标记，不产生系统鼠标事件。"""

    def __init__(self, label: str, color: QColor):
        super().__init__(-8, -8, 16, 16)
        self.setBrush(QBrush(color))
        self.setPen(QPen(QColor("#ffffff"), 2))
        self.setZValue(10)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        )
        text = QGraphicsSimpleTextItem(label, self)
        text.setBrush(QBrush(QColor("#ffeb3b")))
        text.setPos(11, -12)
        text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


class CalibrationCanvas(QGraphicsView):
    def __init__(self, pixmap: QPixmap, points: dict[str, tuple[float, float]]):
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._scene.addItem(QGraphicsPixmapItem(pixmap))
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._markers: dict[str, CalibrationMarker] = {}
        colors = (
            QColor("#e53935"),
            QColor("#fb8c00"),
            QColor("#8e24aa"),
            QColor("#3949ab"),
            QColor("#00897b"),
            QColor("#43a047"),
            QColor("#d81b60"),
        )
        for index, (name, position) in enumerate(points.items()):
            marker = CalibrationMarker(
                str(POINT_DEFINITIONS[name]["label"]),
                colors[index % len(colors)],
            )
            marker.setPos(float(position[0]), float(position[1]))
            self._scene.addItem(marker)
            self._markers[name] = marker
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMinimumHeight(360)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def point(self, name: str) -> tuple[float, float]:
        position = self._markers[name].scenePos()
        return float(position.x()), float(position.y())


def _seed_calibration_from_config(
    project_root: Path,
    wechat_version: str,
) -> dict[str, Any]:
    existing = load_calibration(project_root / "data" / CALIBRATION_FILENAME)
    compatible, _reason = calibration_is_compatible(existing, wechat_version)
    if compatible and existing is not None:
        return existing

    profile = default_calibration(wechat_version, confirmed=False)
    config_path = project_root / "config" / "config.yaml"
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        win_cfg = raw.get("windows_sender", {})
        if not isinstance(win_cfg, dict):
            return profile
        mappings = {
            "search_input": ("search_x_offset", "search_y_offset"),
            "search_clear": ("search_clear_x_offset", "search_clear_y_offset"),
            "private_search_result": (
                "search_result_x_offset",
                "search_result_y_offset",
            ),
            "group_search_result": (
                "group_search_result_x_offset",
                "group_search_result_y_offset",
            ),
            "send_button": (
                "send_button_x_from_right",
                "send_button_y_from_bottom",
            ),
            "paste_menu": ("paste_menu_x_offset", "paste_menu_y_offset"),
        }
        for point_name, keys in mappings.items():
            point = profile["points"][point_name]
            if keys[0] in win_cfg:
                point["x"] = float(win_cfg[keys[0]])
            if keys[1] in win_cfg:
                point["y"] = float(win_cfg[keys[1]])
        message_point = profile["points"]["message_input"]
        if "click_x_ratio" in win_cfg:
            message_point["x"] = float(win_cfg["click_x_ratio"])
        if "click_y_ratio" in win_cfg:
            message_point["y"] = float(win_cfg["click_y_ratio"])
    except Exception:
        pass
    return profile


def _capture_client_pixmap(geometry: ClientGeometry) -> QPixmap:
    """仅把当前屏幕中的微信客户区截到内存，不写入任何图片文件。"""
    from PIL import ImageGrab

    image = ImageGrab.grab(
        bbox=(
            geometry.left,
            geometry.top,
            geometry.left + geometry.width,
            geometry.top + geometry.height,
        ),
        all_screens=True,
    ).convert("RGBA")
    if image.size != (geometry.width, geometry.height):
        image = image.resize((geometry.width, geometry.height))
    raw = image.tobytes("raw", "RGBA")
    qimage = QImage(
        raw,
        geometry.width,
        geometry.height,
        geometry.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
    return QPixmap.fromImage(qimage)


def _menu_preview_pixmap() -> QPixmap:
    pixmap = QPixmap(240, 150)
    pixmap.fill(QColor("#f5f5f5"))
    painter = QPainter(pixmap)
    painter.setPen(QPen(QColor("#777777"), 1))
    painter.drawRect(1, 1, 237, 147)
    painter.setPen(QColor("#333333"))
    painter.setFont(QFont("Microsoft YaHei", 10))
    painter.drawText(14, 28, "粘贴")
    painter.drawLine(8, 38, 230, 38)
    painter.drawText(14, 66, "其它菜单项（示意）")
    painter.end()
    return pixmap


class CalibrationDialog(QDialog):
    def __init__(
        self,
        project_root: Path,
        screenshot: QPixmap,
        geometry: ClientGeometry,
        wechat_version: str,
        parent=None,
    ):
        super().__init__(parent)
        self._project_root = project_root
        self._geometry = geometry
        self._wechat_version = wechat_version
        self.saved_path: Optional[Path] = None
        self.setWindowTitle("微信点击校准（只读预览）")
        self.resize(1080, 820)

        seed = _seed_calibration_from_config(project_root, wechat_version)
        fallback_seed = default_calibration(wechat_version, confirmed=False)
        positions: dict[str, tuple[float, float]] = {}
        for name in MAIN_CALIBRATION_POINTS:
            try:
                screen_x, screen_y = resolve_point(seed, name, geometry)
            except Exception:
                screen_x, screen_y = resolve_point(fallback_seed, name, geometry)
            positions[name] = (
                float(screen_x - geometry.left),
                float(screen_y - geometry.top),
            )
        paste_seed = seed["points"]["paste_menu"]

        layout = QVBoxLayout(self)
        instruction = QLabel(
            "请确认预览确实是微信，并把圆点拖到对应控件中央。"
            "截图只保存在内存中，不会写入磁盘；保存的文件只有坐标和微信版本。"
        )
        instruction.setWordWrap(True)
        layout.addWidget(instruction)

        self._main_canvas = CalibrationCanvas(screenshot, positions)
        layout.addWidget(self._main_canvas, stretch=1)

        menu_group = QGroupBox(
            "右键菜单相对位置（运行时还会验证菜单属于同一个 Weixin 进程）"
        )
        menu_layout = QVBoxLayout(menu_group)
        self._menu_canvas = CalibrationCanvas(
            _menu_preview_pixmap(),
            {
                "paste_menu": (
                    float(paste_seed.get("x", 24.0)),
                    float(paste_seed.get("y", 15.0)),
                )
            },
        )
        self._menu_canvas.setMaximumHeight(190)
        menu_layout.addWidget(self._menu_canvas)
        layout.addWidget(menu_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("确认并保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        try:
            profile = default_calibration(self._wechat_version, confirmed=True)
            for name in MAIN_CALIBRATION_POINTS:
                relative_x, relative_y = self._main_canvas.point(name)
                if not (
                    0 < relative_x < self._geometry.width
                    and 0 < relative_y < self._geometry.height
                ):
                    raise RuntimeError(f"{POINT_DEFINITIONS[name]['label']}超出预览范围")
                profile["points"][name] = point_to_profile_value(
                    name,
                    relative_x,
                    relative_y,
                    self._geometry,
                )
                resolve_point(profile, name, self._geometry)

            paste_x, paste_y = self._menu_canvas.point("paste_menu")
            if not (0 < paste_x < 240 and 0 < paste_y < 150):
                raise RuntimeError("粘贴菜单项超出菜单预览范围")
            profile["points"]["paste_menu"] = {
                "anchor": "top_left",
                "x": round(paste_x, 3),
                "y": round(paste_y, 3),
            }
            self.saved_path = save_calibration(
                profile,
                self._project_root / "data" / CALIBRATION_FILENAME,
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "校准未保存", str(exc))


def discover_project_root() -> Path:
    """定位含现有 venv、后端与前端依赖的项目目录。"""
    candidates: list[Path] = []
    configured = os.getenv("WEIX_PROJECT_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend([executable_dir, executable_dir.parent])
    else:
        candidates.append(Path(__file__).resolve().parent.parent)
    candidates.append(Path.cwd())

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        normalized = os.path.normcase(str(resolved))
        if normalized in seen:
            continue
        seen.add(normalized)
        if (
            (resolved / "venv" / "Scripts" / "python.exe").is_file()
            and (resolved / "backend" / "app" / "main.py").is_file()
            and (
                resolved
                / "frontend"
                / "node_modules"
                / "vite"
                / "bin"
                / "vite.js"
            ).is_file()
        ):
            return resolved
    raise FileNotFoundError(
        "未找到完整的 Weix 项目目录。请把 WeixManager.exe 放回项目的 dist 目录。"
    )


def find_node_executable() -> Optional[Path]:
    located = shutil.which("node.exe") or shutil.which("node")
    if located:
        return Path(located)
    common = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "nodejs"
        / "node.exe"
    )
    return common if common.is_file() else None


def port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def listening_pid(port: int) -> Optional[int]:
    try:
        for connection in psutil.net_connections(kind="inet"):
            if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                continue
            if int(connection.laddr.port) == port:
                return connection.pid
    except (psutil.AccessDenied, OSError):
        pass
    return None


def url_is_ready(url: str, timeout: float = 0.6) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


class ServiceController:
    """管理并安全接管可确认为当前项目的前后端进程。"""

    SOURCE_LABELS = {
        "manager": "管理器",
        "backend": "后端",
        "frontend": "前端",
    }

    def __init__(self, project_root: Path, signals: LauncherSignals):
        self.project_root = project_root
        self.signals = signals
        self.logs_dir = project_root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.logs_dir / ".weix_backend.stop"
        self.service_state_file = self.logs_dir / SERVICE_STATE_FILENAME
        self._sinks = {
            source: RotatingLogSink(self.logs_dir / f"{source}.log")
            for source in self.SOURCE_LABELS
        }
        self._backend_process: Optional[ServiceProcessRef] = None
        self._frontend_process: Optional[ServiceProcessRef] = None
        self._operation_lock = threading.RLock()
        self._process_lock = threading.RLock()

    def log(self, source: str, text: str) -> None:
        clean = ANSI_ESCAPE_RE.sub("", str(text)).rstrip("\r\n")
        if not clean:
            return
        sink = self._sinks.get(source, self._sinks["manager"])
        sink.write(clean)
        self.signals.log_received.emit(source, clean)

    def clear_logs(self) -> None:
        for sink in self._sinks.values():
            sink.clear()

    @staticmethod
    def _normalized_path(value: str | Path) -> str:
        raw = str(value).strip().strip('"')
        try:
            resolved = Path(raw).resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = Path(os.path.abspath(raw))
        return os.path.normcase(os.path.normpath(str(resolved)))

    def _service_definition(self, kind: str) -> tuple[Path, Path, int]:
        if kind == "backend":
            return (
                self.project_root / "backend" / "managed_server.py",
                self.project_root / "backend",
                BACKEND_PORT,
            )
        if kind == "frontend":
            return (
                self.project_root
                / "frontend"
                / "node_modules"
                / "vite"
                / "bin"
                / "vite.js",
                self.project_root / "frontend",
                FRONTEND_PORT,
            )
        raise ValueError(f"未知服务类型：{kind}")

    @staticmethod
    def _option_matches(arguments: list[str], option: str, value: str) -> bool:
        try:
            index = arguments.index(option)
        except ValueError:
            return False
        return index + 1 < len(arguments) and arguments[index + 1] == value

    def _path_option_matches(
        self,
        arguments: list[str],
        option: str,
        value: Path,
    ) -> bool:
        try:
            index = arguments.index(option)
        except ValueError:
            return False
        return index + 1 < len(arguments) and (
            self._normalized_path(arguments[index + 1])
            == self._normalized_path(value)
        )

    def _process_matches_service(self, process: psutil.Process, kind: str) -> bool:
        """按精确脚本、工作目录和端口匹配，无法读取时拒绝接管。"""
        target, expected_cwd, port = self._service_definition(kind)
        try:
            name = process.name().lower()
            arguments = [str(value) for value in process.cmdline()]
            cwd = process.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

        expected_names = {"python", "python.exe"} if kind == "backend" else {
            "node",
            "node.exe",
        }
        if name not in expected_names:
            return False
        normalized_arguments = {
            self._normalized_path(value)
            for value in arguments
            if value and not value.startswith("-")
        }
        if self._normalized_path(target) not in normalized_arguments:
            return False
        if self._normalized_path(cwd) != self._normalized_path(expected_cwd):
            return False
        if not self._option_matches(arguments, "--port", str(port)):
            return False
        if kind == "backend":
            if not self._path_option_matches(
                arguments,
                "--stop-file",
                self.stop_file,
            ):
                return False
        elif "--strictPort" not in arguments:
            return False
        return True

    @staticmethod
    def _process_identity_matches(
        process: psutil.Process,
        reference: ServiceProcessRef,
    ) -> bool:
        try:
            return (
                process.pid == reference.pid
                and abs(process.create_time() - reference.create_time) < 1.0
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def _validated_process(
        self,
        reference: Optional[ServiceProcessRef],
    ) -> Optional[psutil.Process]:
        if reference is None:
            return None
        try:
            process = psutil.Process(reference.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        if not self._process_identity_matches(process, reference):
            return None
        if not self._process_matches_service(process, reference.kind):
            return None
        return process

    @staticmethod
    def _is_descendant_or_same(root_pid: int, candidate_pid: int) -> bool:
        if root_pid == candidate_pid:
            return True
        try:
            process = psutil.Process(candidate_pid)
            return any(parent.pid == root_pid for parent in process.parents())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def _reference_controls_listener(self, reference: ServiceProcessRef) -> bool:
        _target, _cwd, port = self._service_definition(reference.kind)
        listener_pid = listening_pid(port)
        return listener_pid is not None and self._is_descendant_or_same(
            reference.pid,
            listener_pid,
        )

    def _reference_from_process(
        self,
        process: psutil.Process,
        kind: str,
        *,
        adopted: bool,
        popen: Optional[subprocess.Popen[str]] = None,
    ) -> Optional[ServiceProcessRef]:
        try:
            reference = ServiceProcessRef(
                kind=kind,
                pid=process.pid,
                create_time=process.create_time(),
                popen=popen,
                adopted=adopted,
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        if not self._process_matches_service(process, kind):
            return None
        return reference

    def _reference_from_popen(
        self,
        process: subprocess.Popen[str],
        kind: str,
    ) -> ServiceProcessRef:
        reference = self._reference_from_process(
            psutil.Process(process.pid),
            kind,
            adopted=False,
            popen=process,
        )
        if reference is None:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass
            raise RuntimeError(f"无法登记{self.SOURCE_LABELS[kind]}进程身份")
        return reference

    def _discover_reference_from_listener(
        self,
        kind: str,
    ) -> Optional[ServiceProcessRef]:
        _target, _cwd, port = self._service_definition(kind)
        listener_pid = listening_pid(port)
        if listener_pid is None:
            return None
        try:
            listener = psutil.Process(listener_pid)
            candidates = [listener] + listener.parents()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

        # 选择最高层仍带有相同启动签名的进程，停止时才能覆盖其整个子树。
        reference = None
        for candidate in candidates:
            matched = self._reference_from_process(
                candidate,
                kind,
                adopted=True,
            )
            if matched is not None:
                reference = matched
        return reference

    def _load_saved_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.service_state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != SERVICE_STATE_VERSION:
            return {}
        saved_root = raw.get("project_root")
        if not isinstance(saved_root, str) or (
            self._normalized_path(saved_root)
            != self._normalized_path(self.project_root)
        ):
            return {}
        return raw

    def _reference_from_saved_state(
        self,
        saved: dict[str, Any],
        kind: str,
    ) -> Optional[ServiceProcessRef]:
        entry = saved.get(kind)
        if not isinstance(entry, dict):
            return None
        try:
            reference = ServiceProcessRef(
                kind=kind,
                pid=int(entry["pid"]),
                create_time=float(entry["create_time"]),
                adopted=True,
            )
        except (KeyError, TypeError, ValueError):
            return None
        process = self._validated_process(reference)
        if process is None or not self._reference_controls_listener(reference):
            return None
        return reference

    def _persist_service_state(self) -> None:
        backend, frontend = self._process_snapshot()
        entries: dict[str, Any] = {}
        for kind, reference in (("backend", backend), ("frontend", frontend)):
            if not self._is_running(reference):
                continue
            entries[kind] = {
                "pid": reference.pid,
                "create_time": reference.create_time,
            }
        if not entries:
            try:
                self.service_state_file.unlink()
            except FileNotFoundError:
                pass
            return

        payload = {
            "version": SERVICE_STATE_VERSION,
            "project_root": str(self.project_root.resolve()),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            **entries,
        }
        temporary = self.service_state_file.with_name(
            f"{self.service_state_file.name}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.service_state_file)

    def adopt_existing_services(
        self,
    ) -> tuple[tuple[bool, bool], list[str], list[str]]:
        """识别当前项目遗留服务；返回状态、已接管项和未知端口冲突。"""
        with self._operation_lock:
            saved = self._load_saved_state()
            adopted_names: list[str] = []
            conflicts: list[str] = []
            with self._process_lock:
                current = {
                    "backend": self._backend_process,
                    "frontend": self._frontend_process,
                }

            for kind, label, port in (
                ("backend", "后端", BACKEND_PORT),
                ("frontend", "前端", FRONTEND_PORT),
            ):
                reference = current[kind]
                if not self._is_running(reference):
                    reference = self._reference_from_saved_state(saved, kind)
                    if reference is None and port_is_listening(port):
                        reference = self._discover_reference_from_listener(kind)
                    if reference is not None:
                        with self._process_lock:
                            if kind == "backend":
                                self._backend_process = reference
                            else:
                                self._frontend_process = reference
                        adopted_names.append(f"{label}（PID {reference.pid}）")
                if port_is_listening(port) and reference is None:
                    pid = listening_pid(port)
                    conflicts.append(
                        f"{label}端口 {port}"
                        + (f"（PID {pid}）" if pid else "")
                    )

            self._persist_service_state()
            return self.process_status(), adopted_names, conflicts

    def start(self) -> tuple[bool, str]:
        with self._operation_lock:
            (backend_running, frontend_running), adopted, conflicts = (
                self.adopt_existing_services()
            )
            if backend_running or frontend_running:
                details = "、".join(adopted) if adopted else "当前项目服务"
                return (
                    False,
                    f"已识别到{details}正在运行，请使用“停止”或“重启”操作。",
                )

            if conflicts:
                return (
                    False,
                    "；".join(f"{item} 已被未知程序占用" for item in conflicts)
                    + "。为避免误关其他程序，管理器没有结束它。",
                )

            python_exe = self.project_root / "venv" / "Scripts" / "python.exe"
            runner = self.project_root / "backend" / "managed_server.py"
            vite = (
                self.project_root
                / "frontend"
                / "node_modules"
                / "vite"
                / "bin"
                / "vite.js"
            )
            node_exe = find_node_executable()
            missing = [
                str(path)
                for path in (python_exe, runner, vite)
                if not path.is_file()
            ]
            if node_exe is None:
                missing.append("node.exe")
            if missing:
                return False, "缺少运行文件：" + "、".join(missing)

            self._remove_stop_file()
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1",
                    "NO_COLOR": "1",
                    "FORCE_COLOR": "0",
                }
            )
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

            try:
                self.log("manager", "正在隐藏启动后端服务...")
                backend = subprocess.Popen(
                    [
                        str(python_exe),
                        "-u",
                        str(runner),
                        "--host",
                        BACKEND_HOST,
                        "--port",
                        str(BACKEND_PORT),
                        "--stop-file",
                        str(self.stop_file),
                    ],
                    cwd=str(self.project_root / "backend"),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                )
                with self._process_lock:
                    self._backend_process = self._reference_from_popen(
                        backend,
                        "backend",
                    )
                self._persist_service_state()
                self._start_output_pump("backend", backend)

                self.log("manager", "正在隐藏启动前端服务...")
                frontend = subprocess.Popen(
                    [
                        str(node_exe),
                        str(vite),
                        "--host",
                        FRONTEND_HOST,
                        "--port",
                        str(FRONTEND_PORT),
                        "--strictPort",
                    ],
                    cwd=str(self.project_root / "frontend"),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                )
                with self._process_lock:
                    self._frontend_process = self._reference_from_popen(
                        frontend,
                        "frontend",
                    )
                self._persist_service_state()
                self._start_output_pump("frontend", frontend)

                deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
                backend_ready = False
                frontend_ready = False
                while time.monotonic() < deadline:
                    if backend.poll() is not None:
                        raise RuntimeError(
                            f"后端进程已退出，退出码 {backend.returncode}"
                        )
                    if frontend.poll() is not None:
                        raise RuntimeError(
                            f"前端进程已退出，退出码 {frontend.returncode}"
                        )
                    backend_ready = backend_ready or url_is_ready(BACKEND_HEALTH_URL)
                    frontend_ready = frontend_ready or url_is_ready(FRONTEND_URL)
                    if backend_ready and frontend_ready:
                        self.log("manager", "前后端均已就绪")
                        return True, "服务已启动"
                    time.sleep(0.25)
                raise TimeoutError(
                    "服务启动超时："
                    f"后端={'就绪' if backend_ready else '未就绪'}，"
                    f"前端={'就绪' if frontend_ready else '未就绪'}"
                )
            except Exception as exc:
                self.log("manager", f"启动失败：{exc}")
                self._cleanup_after_failed_start()
                return False, str(exc)

    def stop(self, force: bool = False) -> tuple[bool, str]:
        with self._operation_lock:
            backend, frontend = self._process_snapshot()
            if not self._is_running(backend) and not self._is_running(frontend):
                self._clear_process_references()
                return True, "服务已停止"

            if force:
                self.log("manager", "正在强制结束已确认属于当前项目的进程...")
                frontend_stopped = self._terminate_process_tree(frontend, force=True)
                backend_stopped = self._terminate_process_tree(backend, force=True)
                self._clear_process_references()
                self._remove_stop_file()
                if not frontend_stopped or not backend_stopped:
                    return False, "部分进程无法结束，请确认管理器具有管理员权限"
                return True, "服务已强制停止"

            self.log("manager", "正在通知后端正常停止...")
            if self._is_running(backend):
                if self._validated_process(backend) is None:
                    return False, "后端进程身份复核失败，管理器拒绝停止"
                self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.stop_file.write_text("stop\n", encoding="utf-8")

            frontend_stopped = True
            if self._is_running(frontend):
                self.log("manager", "正在停止前端服务...")
                frontend_stopped = self._terminate_process_tree(
                    frontend,
                    force=False,
                )

            backend_stopped = (
                True
                if backend is None
                else self._wait_for_service_stop(
                    backend,
                    BACKEND_PORT,
                    GRACEFUL_STOP_TIMEOUT_SECONDS,
                )
            )
            if not backend_stopped:
                self.log("manager", "后端在 20 秒内未完成正常停止")
                return False, "后端未能在 20 秒内正常停止"
            if not frontend_stopped:
                return False, "前端进程未能正常停止"

            self._clear_process_references()
            self._remove_stop_file()
            self.log("manager", "服务已停止")
            return True, "服务已停止"

    def any_process_running(self) -> bool:
        backend, frontend = self._process_snapshot()
        return self._is_running(backend) or self._is_running(frontend)

    def process_status(self) -> tuple[bool, bool]:
        backend, frontend = self._process_snapshot()
        backend_running = self._is_running(backend)
        frontend_running = self._is_running(frontend)
        if (backend is not None and not backend_running) or (
            frontend is not None and not frontend_running
        ):
            self._clear_process_references()
        return backend_running, frontend_running

    def _process_snapshot(
        self,
    ) -> tuple[Optional[ServiceProcessRef], Optional[ServiceProcessRef]]:
        with self._process_lock:
            return self._backend_process, self._frontend_process

    def _is_running(self, reference: Optional[ServiceProcessRef]) -> bool:
        if reference is None:
            return False
        if reference.popen is not None and reference.popen.poll() is not None:
            return False
        return self._validated_process(reference) is not None

    def _start_output_pump(self, source: str, process: subprocess.Popen[str]) -> None:
        def pump() -> None:
            stream = process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    self.log(source, line)
            except Exception as exc:
                self.log(
                    "manager",
                    f"读取{self.SOURCE_LABELS[source]}日志失败：{exc}",
                )
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threading.Thread(
            target=pump,
            daemon=True,
            name=f"weix-{source}-log-pump",
        ).start()

    def _cleanup_after_failed_start(self) -> None:
        backend, frontend = self._process_snapshot()
        self._terminate_process_tree(frontend, force=True)
        if self._is_running(backend):
            try:
                self.stop_file.write_text("stop\n", encoding="utf-8")
                stopped = self._wait_for_service_stop(
                    backend,
                    BACKEND_PORT,
                    5.0,
                )
                if not stopped:
                    self._terminate_process_tree(backend, force=True)
            except Exception:
                self._terminate_process_tree(backend, force=True)
        self._clear_process_references()
        self._remove_stop_file()

    def _terminate_process_tree(
        self,
        reference: Optional[ServiceProcessRef],
        force: bool,
    ) -> bool:
        if not self._is_running(reference):
            return True
        process = self._validated_process(reference)
        if process is None:
            return False
        try:
            targets = process.children(recursive=True) + [process]
            for target in targets:
                try:
                    target.kill() if force else target.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, alive = psutil.wait_procs(targets, timeout=3.0)
            for target in alive:
                try:
                    target.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                psutil.wait_procs(alive, timeout=2.0)
        except psutil.NoSuchProcess:
            return True
        except (psutil.AccessDenied, OSError):
            return not self._is_running(reference)
        return not self._is_running(reference)

    def _wait_for_service_stop(
        self,
        reference: Optional[ServiceProcessRef],
        port: int,
        timeout: float,
    ) -> bool:
        if reference is None:
            return not port_is_listening(port)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._is_running(reference) and not port_is_listening(port):
                return True
            time.sleep(0.2)
        return not self._is_running(reference) and not port_is_listening(port)

    def _clear_process_references(self) -> None:
        with self._process_lock:
            if not self._is_running(self._backend_process):
                self._backend_process = None
            if not self._is_running(self._frontend_process):
                self._frontend_process = None
        self._persist_service_state()

    def _remove_stop_file(self) -> None:
        try:
            self.stop_file.unlink()
        except FileNotFoundError:
            pass


class MainWindow(QMainWindow):
    def __init__(
        self,
        project_root: Path,
        signals: LauncherSignals,
        instance_lock: QLockFile,
    ):
        super().__init__()
        self._signals = signals
        self._instance_lock = instance_lock
        self._project_root = project_root
        self._controller = ServiceController(project_root, signals)
        self._state = ServiceState.STOPPED
        self._restart_pending = False
        self._force_stop_in_progress = False
        self._quit_after_stop = False
        self._allow_close = False
        self._tray_notice_shown = False
        self._unexpected_failure_handled = False

        self._signals.log_received.connect(self._append_log)
        self._signals.start_finished.connect(self._on_start_finished)
        self._signals.stop_finished.connect(self._on_stop_finished)

        self._init_ui(project_root)
        self._init_tray()
        self._recognize_existing_services(initial=True)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_process_status)
        self._status_timer.start(1500)

    def _init_ui(self, project_root: Path) -> None:
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(820, 560)
        self.resize(940, 680)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 10)
        layout.setSpacing(10)

        status_layout = QHBoxLayout()
        self._overall_label = QLabel()
        self._backend_label = QLabel("后端：已停止")
        self._frontend_label = QLabel("前端：已停止")
        for label in (
            self._overall_label,
            self._backend_label,
            self._frontend_label,
        ):
            label.setFont(QFont("Microsoft YaHei", 10))
            status_layout.addWidget(label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        buttons = QHBoxLayout()
        self._btn_start = QPushButton("启动")
        self._btn_stop = QPushButton("停止")
        self._btn_restart = QPushButton("重启")
        self._btn_recognize = QPushButton("识别服务")
        self._btn_browser = QPushButton("打开网页")
        self._btn_calibrate = QPushButton("微信点击校准")
        self._btn_clear_view = QPushButton("清空显示")
        self._btn_clear_files = QPushButton("清理日志文件")
        self._btn_start.clicked.connect(self._begin_start)
        self._btn_stop.clicked.connect(self._begin_stop)
        self._btn_restart.clicked.connect(self._begin_restart)
        self._btn_recognize.clicked.connect(self._recognize_existing_services)
        self._btn_browser.clicked.connect(self._open_browser)
        self._btn_calibrate.clicked.connect(self._begin_calibration)
        self._btn_clear_view.clicked.connect(self._clear_log_view)
        self._btn_clear_files.clicked.connect(self._clear_log_files)
        for button in (
            self._btn_start,
            self._btn_stop,
            self._btn_restart,
            self._btn_recognize,
            self._btn_browser,
            self._btn_calibrate,
            self._btn_clear_view,
            self._btn_clear_files,
        ):
            button.setMinimumHeight(34)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFont(QFont("Consolas", 10))
        self._log_text.document().setMaximumBlockCount(5000)
        self._log_text.setStyleSheet(
            "QTextEdit { background:#1e1e1e; color:#d4d4d4; "
            "border:1px solid #3c3c3c; border-radius:5px; padding:8px; }"
        )
        layout.addWidget(self._log_text, stretch=1)

        path_label = QLabel(f"项目目录：{project_root}")
        path_label.setStyleSheet("color:#777;")
        layout.addWidget(path_label)

        self._statusbar = QStatusBar(self)
        self.setStatusBar(self._statusbar)

    def _init_tray(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip(APP_TITLE)
        menu = QMenu()
        self._tray.setContextMenu(menu)

        self._tray_show = QAction("显示窗口", self)
        self._tray_open = QAction("打开网页", self)
        self._tray_start = QAction("启动", self)
        self._tray_stop = QAction("停止", self)
        self._tray_restart = QAction("重启", self)
        self._tray_exit = QAction("退出", self)
        self._tray_show.triggered.connect(self._show_from_tray)
        self._tray_open.triggered.connect(self._open_browser)
        self._tray_start.triggered.connect(self._begin_start)
        self._tray_stop.triggered.connect(self._begin_stop)
        self._tray_restart.triggered.connect(self._begin_restart)
        self._tray_exit.triggered.connect(self._request_exit)
        menu.addAction(self._tray_show)
        menu.addAction(self._tray_open)
        menu.addSeparator()
        menu.addAction(self._tray_start)
        menu.addAction(self._tray_stop)
        menu.addAction(self._tray_restart)
        menu.addSeparator()
        menu.addAction(self._tray_exit)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _set_state(self, state: ServiceState, message: str = "") -> None:
        self._state = state
        backend_running, frontend_running = self._controller.process_status()
        any_process_running = backend_running or frontend_running
        mapping = {
            ServiceState.STOPPED: ("总体：已停止", True, False, False, False),
            ServiceState.STARTING: ("总体：启动中…", False, False, False, False),
            ServiceState.RUNNING: ("总体：运行中", False, True, True, True),
            ServiceState.PARTIAL: (
                "总体：部分运行",
                False,
                True,
                True,
                frontend_running,
            ),
            ServiceState.STOPPING: ("总体：停止中…", False, False, False, False),
            ServiceState.RESTARTING: ("总体：重启中…", False, False, False, False),
            ServiceState.ERROR: (
                "总体：异常",
                not any_process_running,
                any_process_running,
                False,
                False,
            ),
        }
        label, start_enabled, stop_enabled, restart_enabled, browser_enabled = mapping[
            state
        ]
        self._overall_label.setText(label)
        self._btn_start.setEnabled(start_enabled)
        self._btn_stop.setEnabled(stop_enabled)
        self._btn_restart.setEnabled(restart_enabled)
        self._btn_recognize.setEnabled(
            state in (ServiceState.STOPPED, ServiceState.PARTIAL, ServiceState.ERROR)
        )
        self._btn_browser.setEnabled(browser_enabled)
        self._btn_calibrate.setEnabled(
            state == ServiceState.STOPPED and not any_process_running
        )
        self._tray_start.setEnabled(start_enabled)
        self._tray_stop.setEnabled(stop_enabled)
        self._tray_restart.setEnabled(restart_enabled)
        self._tray_open.setEnabled(browser_enabled)
        self._statusbar.showMessage(message or label)
        self._tray.setToolTip(f"{APP_TITLE} - {label.replace('总体：', '')}")

    def _recognize_existing_services(self, initial: bool = False) -> None:
        if self._state in (
            ServiceState.STARTING,
            ServiceState.STOPPING,
            ServiceState.RESTARTING,
        ):
            return
        (backend_running, frontend_running), adopted, conflicts = (
            self._controller.adopt_existing_services()
        )
        if adopted:
            details = "、".join(adopted)
            self._controller.log("manager", f"已识别并接管遗留服务：{details}")
        self._backend_label.setText(
            f"后端：{'运行中' if backend_running else '已停止'}"
        )
        self._frontend_label.setText(
            f"前端：{'运行中' if frontend_running else '已停止'}"
        )

        if conflicts:
            message = (
                "；".join(f"{item} 被未知程序占用" for item in conflicts)
                + "。为避免误关其他程序，管理器不会接管。"
            )
            self._controller.log("manager", message)
            self._set_state(ServiceState.ERROR, message)
        elif backend_running and frontend_running:
            message = "已接管当前项目正在运行的前后端服务"
            self._set_state(ServiceState.RUNNING, message)
        elif backend_running or frontend_running:
            running_name = "后端" if backend_running else "前端"
            message = f"已接管当前项目的{running_name}；可直接停止或重启"
            self._set_state(ServiceState.PARTIAL, message)
        else:
            self._set_state(ServiceState.STOPPED, "未发现正在运行的本项目服务")

        if not initial:
            self._statusbar.showMessage(self._statusbar.currentMessage(), 5000)

    def _begin_start(self) -> None:
        if self._state not in (ServiceState.STOPPED, ServiceState.ERROR):
            return
        self._unexpected_failure_handled = False
        self._set_state(ServiceState.STARTING, "正在启动前后端服务...")
        self._controller.log("manager", "收到启动请求")

        def worker() -> None:
            ok, message = self._controller.start()
            self._signals.start_finished.emit(ok, message)

        threading.Thread(target=worker, daemon=True, name="weix-start-worker").start()

    def _on_start_finished(self, ok: bool, message: str) -> None:
        if ok:
            self._set_state(ServiceState.RUNNING, message)
            return
        backend_running, frontend_running = self._controller.process_status()
        if backend_running or frontend_running:
            target = (
                ServiceState.RUNNING
                if backend_running and frontend_running
                else ServiceState.PARTIAL
            )
            self._set_state(target, message)
            QMessageBox.information(self, "已识别现有服务", message)
            return
        self._restart_pending = False
        self._set_state(ServiceState.ERROR, message)
        QMessageBox.critical(self, "启动失败", message)

    def _begin_stop(self) -> None:
        if self._state in (
            ServiceState.STOPPED,
            ServiceState.STOPPING,
            ServiceState.RESTARTING,
        ):
            return
        target_state = (
            ServiceState.RESTARTING if self._restart_pending else ServiceState.STOPPING
        )
        self._set_state(target_state, "正在正常停止服务...")

        def worker() -> None:
            ok, message = self._controller.stop(force=False)
            self._signals.stop_finished.emit(ok, message)

        threading.Thread(target=worker, daemon=True, name="weix-stop-worker").start()

    def _begin_restart(self) -> None:
        if self._state not in (ServiceState.RUNNING, ServiceState.PARTIAL):
            return
        self._restart_pending = True
        self._controller.log("manager", "收到重启请求")
        self._begin_stop()

    def _on_stop_finished(self, ok: bool, message: str) -> None:
        if not ok:
            if self._force_stop_in_progress:
                self._force_stop_in_progress = False
                self._restart_pending = False
                self._quit_after_stop = False
                self._set_state(ServiceState.ERROR, message)
                QMessageBox.critical(self, "强制停止失败", message)
                return
            force = QMessageBox.question(
                self,
                "正常停止超时",
                f"{message}\n\n是否强制结束已确认属于当前项目的进程？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if force == QMessageBox.StandardButton.Yes:
                self._force_stop_in_progress = True
                self._set_state(ServiceState.STOPPING, "正在强制停止服务...")

                def worker() -> None:
                    forced_ok, forced_message = self._controller.stop(force=True)
                    self._signals.stop_finished.emit(forced_ok, forced_message)

                threading.Thread(
                    target=worker,
                    daemon=True,
                    name="weix-force-stop-worker",
                ).start()
                return
            self._restart_pending = False
            self._quit_after_stop = False
            self._set_state(ServiceState.ERROR, message)
            return

        should_restart = self._restart_pending
        self._force_stop_in_progress = False
        self._restart_pending = False
        self._set_state(ServiceState.STOPPED, message)
        if self._quit_after_stop:
            self._finish_quit()
        elif should_restart:
            QTimer.singleShot(300, self._begin_start)

    def _refresh_process_status(self) -> None:
        backend_running, frontend_running = self._controller.process_status()
        self._backend_label.setText(
            f"后端：{'运行中' if backend_running else '已停止'}"
        )
        self._frontend_label.setText(
            f"前端：{'运行中' if frontend_running else '已停止'}"
        )
        if (
            self._state == ServiceState.RUNNING
            and not self._unexpected_failure_handled
            and (not backend_running or not frontend_running)
        ):
            self._unexpected_failure_handled = True
            missing = "后端" if not backend_running else "前端"
            self._controller.log("manager", f"检测到{missing}进程意外退出")
            self._set_state(
                ServiceState.PARTIAL,
                f"{missing}进程意外退出，可点击重启恢复",
            )
        elif self._state == ServiceState.PARTIAL:
            if backend_running and frontend_running:
                self._unexpected_failure_handled = False
                self._set_state(ServiceState.RUNNING, "前后端均已恢复运行")
            elif not backend_running and not frontend_running:
                self._set_state(ServiceState.STOPPED, "服务已全部停止")

    def _append_log(self, source: str, text: str) -> None:
        label = ServiceController.SOURCE_LABELS.get(source, source)
        self._log_text.append(f"[{label}] {text}")
        cursor = self._log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log_text.setTextCursor(cursor)

    def _clear_log_view(self) -> None:
        self._log_text.clear()

    def _clear_log_files(self) -> None:
        reply = QMessageBox.question(
            self,
            "清理日志文件",
            "只会删除 logs 目录中由管理器维护的 manager/backend/frontend "
            "日志及轮转文件。\n\n确认清理吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.clear_logs()
            self._log_text.clear()
            self._controller.log("manager", "日志文件已清理")
        except Exception as exc:
            QMessageBox.critical(self, "清理失败", str(exc))

    def _begin_calibration(self) -> None:
        """隐藏管理器后只读截取微信客户区，不激活、不点击微信。"""
        if self._state != ServiceState.STOPPED or self._controller.any_process_running():
            QMessageBox.warning(self, "无法校准", "请先停止 Weix 前后端服务。")
            return
        if port_is_listening(BACKEND_PORT):
            pid = listening_pid(BACKEND_PORT)
            QMessageBox.warning(
                self,
                "无法校准",
                "检测到后端仍在运行"
                + (f"（PID {pid}）" if pid else "")
                + "，请先停止后再校准。",
            )
            return
        hwnd = find_wechat_main_window(visible_only=True)
        if not hwnd:
            QMessageBox.warning(
                self,
                "未找到可见微信",
                "请手动打开微信主窗口并恢复到正常大小，然后重新点击校准。",
            )
            return
        version = get_process_file_version(hwnd)
        if not version:
            QMessageBox.warning(
                self,
                "无法确认微信版本",
                "读取微信程序版本失败。为避免坐标过期，本次不允许保存校准。",
            )
            return
        try:
            geometry = get_client_geometry(hwnd)
        except Exception as exc:
            QMessageBox.warning(self, "微信窗口不可校准", str(exc))
            return

        QMessageBox.information(
            self,
            "准备只读截图",
            "请确保微信窗口没有被其他程序遮挡。管理器将暂时隐藏并只截取屏幕中的"
            "微信客户区，不会激活、点击或发送任何内容。",
        )
        self.hide()

        def capture() -> None:
            try:
                screenshot = _capture_client_pixmap(geometry)
            except Exception as exc:
                self.showNormal()
                self.raise_()
                QMessageBox.critical(self, "截取失败", str(exc))
                return
            self.showNormal()
            self.raise_()
            self.activateWindow()
            dialog = CalibrationDialog(
                self._project_root,
                screenshot,
                geometry,
                version,
                self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._controller.log(
                    "manager",
                    f"微信点击校准已保存：{dialog.saved_path}",
                )
                QMessageBox.information(
                    self,
                    "校准完成",
                    "坐标已保存。现在可以启动服务；微信版本变化后需要重新校准。",
                )

        QTimer.singleShot(400, capture)

    @staticmethod
    def _open_browser() -> None:
        webbrowser.open(FRONTEND_URL)

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _request_exit(self) -> None:
        if self._state in (
            ServiceState.STARTING,
            ServiceState.STOPPING,
            ServiceState.RESTARTING,
        ):
            QMessageBox.information(self, "请稍候", "当前操作完成后再退出管理器。")
            return
        if self._controller.any_process_running():
            reply = QMessageBox.question(
                self,
                "退出管理器",
                "退出会先正常停止前后端服务。确认退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._quit_after_stop = True
            self._begin_stop()
            return
        self._finish_quit()

    def _finish_quit(self) -> None:
        self._allow_close = True
        self._tray.hide()
        self._instance_lock.unlock()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
            if not self._tray_notice_shown:
                self._tray.showMessage(
                    APP_TITLE,
                    "管理器已隐藏到系统托盘，前后端会继续运行。",
                    QSystemTrayIcon.MessageIcon.Information,
                    3500,
                )
                self._tray_notice_shown = True
            return
        event.ignore()
        self._request_exit()


def is_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_admin_restart() -> None:
    import ctypes

    executable = sys.executable
    parameters = (
        subprocess.list2cmdline(sys.argv[1:])
        if getattr(sys, "frozen", False)
        else subprocess.list2cmdline(sys.argv)
    )
    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        parameters,
        str(Path.cwd()),
        1,
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WeixManager")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    try:
        project_root = discover_project_root()
    except Exception as exc:
        QMessageBox.critical(None, "无法启动", str(exc))
        return 1

    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(logs_dir / ".weix_manager.lock"))
    if not instance_lock.tryLock(100):
        QMessageBox.information(
            None,
            "管理器已运行",
            "Weix 服务管理器已经在运行，请检查系统托盘。",
        )
        return 0

    if sys.platform == "win32" and not is_admin():
        choice = QMessageBox.question(
            None,
            "权限提示",
            "读取微信数据库密钥通常需要管理员权限。\n\n"
            "是否以管理员身份重新启动管理器？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            instance_lock.unlock()
            request_admin_restart()
            return 0

    signals = LauncherSignals()
    window = MainWindow(project_root, signals, instance_lock)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
