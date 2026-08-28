"""Weix Windows 图形管理器。

管理当前项目目录中的 FastAPI 后端与 Vite 前端。管理器本身不包含配置、
数据或密钥；PyInstaller --windowed 打包后也不会创建命令行窗口。
"""

from __future__ import annotations

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
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import psutil
from PyQt6.QtCore import QObject, QLockFile, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
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
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ServiceState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    RESTARTING = auto()
    ERROR = auto()


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
    """只管理当前管理器启动的两个子进程。"""

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
        self._sinks = {
            source: RotatingLogSink(self.logs_dir / f"{source}.log")
            for source in self.SOURCE_LABELS
        }
        self._backend_process: Optional[subprocess.Popen[str]] = None
        self._frontend_process: Optional[subprocess.Popen[str]] = None
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

    def start(self) -> tuple[bool, str]:
        with self._operation_lock:
            if self.any_process_running():
                return False, "管理器已启动服务，不能重复启动"

            conflicts = []
            for name, port in (("后端", BACKEND_PORT), ("前端", FRONTEND_PORT)):
                if port_is_listening(port):
                    pid = listening_pid(port)
                    conflicts.append(
                        f"{name}端口 {port} 已被占用"
                        + (f"（PID {pid}）" if pid else "")
                    )
            if conflicts:
                return (
                    False,
                    "；".join(conflicts)
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
                    self._backend_process = backend
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
                    self._frontend_process = frontend
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
                self.log("manager", "正在强制结束本轮启动的进程...")
                self._terminate_process_tree(frontend, force=True)
                self._terminate_process_tree(backend, force=True)
                self._clear_process_references()
                self._remove_stop_file()
                return True, "服务已强制停止"

            self.log("manager", "正在通知后端正常停止...")
            if self._is_running(backend):
                self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.stop_file.write_text("stop\n", encoding="utf-8")

            if self._is_running(frontend):
                self.log("manager", "正在停止前端服务...")
                self._terminate_process_tree(frontend, force=False)

            if self._is_running(backend):
                try:
                    backend.wait(timeout=GRACEFUL_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self.log("manager", "后端在 20 秒内未完成正常停止")
                    return False, "后端未能在 20 秒内正常停止"

            self._clear_process_references()
            self._remove_stop_file()
            self.log("manager", "服务已停止")
            return True, "服务已停止"

    def any_process_running(self) -> bool:
        backend, frontend = self._process_snapshot()
        return self._is_running(backend) or self._is_running(frontend)

    def process_status(self) -> tuple[bool, bool]:
        backend, frontend = self._process_snapshot()
        return self._is_running(backend), self._is_running(frontend)

    def _process_snapshot(
        self,
    ) -> tuple[Optional[subprocess.Popen[str]], Optional[subprocess.Popen[str]]]:
        with self._process_lock:
            return self._backend_process, self._frontend_process

    @staticmethod
    def _is_running(process: Optional[subprocess.Popen[str]]) -> bool:
        return process is not None and process.poll() is None

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
                backend.wait(timeout=5)
            except Exception:
                self._terminate_process_tree(backend, force=True)
        self._clear_process_references()
        self._remove_stop_file()

    @staticmethod
    def _terminate_process_tree(
        process: Optional[subprocess.Popen[str]],
        force: bool,
    ) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            parent = psutil.Process(process.pid)
            targets = parent.children(recursive=True) + [parent]
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
        except psutil.NoSuchProcess:
            pass

    def _clear_process_references(self) -> None:
        with self._process_lock:
            if not self._is_running(self._backend_process):
                self._backend_process = None
            if not self._is_running(self._frontend_process):
                self._frontend_process = None

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
        self._controller = ServiceController(project_root, signals)
        self._state = ServiceState.STOPPED
        self._restart_pending = False
        self._quit_after_stop = False
        self._allow_close = False
        self._tray_notice_shown = False
        self._unexpected_failure_handled = False

        self._signals.log_received.connect(self._append_log)
        self._signals.start_finished.connect(self._on_start_finished)
        self._signals.stop_finished.connect(self._on_stop_finished)

        self._init_ui(project_root)
        self._init_tray()
        self._set_state(ServiceState.STOPPED)

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
        self._btn_browser = QPushButton("打开网页")
        self._btn_clear_view = QPushButton("清空显示")
        self._btn_clear_files = QPushButton("清理日志文件")
        self._btn_start.clicked.connect(self._begin_start)
        self._btn_stop.clicked.connect(self._begin_stop)
        self._btn_restart.clicked.connect(self._begin_restart)
        self._btn_browser.clicked.connect(self._open_browser)
        self._btn_clear_view.clicked.connect(self._clear_log_view)
        self._btn_clear_files.clicked.connect(self._clear_log_files)
        for button in (
            self._btn_start,
            self._btn_stop,
            self._btn_restart,
            self._btn_browser,
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
        any_process_running = self._controller.any_process_running()
        mapping = {
            ServiceState.STOPPED: ("总体：已停止", True, False, False, False),
            ServiceState.STARTING: ("总体：启动中…", False, False, False, False),
            ServiceState.RUNNING: ("总体：运行中", False, True, True, True),
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
        self._btn_browser.setEnabled(browser_enabled)
        self._tray_start.setEnabled(start_enabled)
        self._tray_stop.setEnabled(stop_enabled)
        self._tray_restart.setEnabled(restart_enabled)
        self._tray_open.setEnabled(browser_enabled)
        self._statusbar.showMessage(message or label)
        self._tray.setToolTip(f"{APP_TITLE} - {label.replace('总体：', '')}")

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
        if self._state != ServiceState.RUNNING:
            return
        self._restart_pending = True
        self._controller.log("manager", "收到重启请求")
        self._begin_stop()

    def _on_stop_finished(self, ok: bool, message: str) -> None:
        if not ok:
            force = QMessageBox.question(
                self,
                "正常停止超时",
                f"{message}\n\n是否强制结束管理器本轮启动的进程？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if force == QMessageBox.StandardButton.Yes:
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
            self._set_state(ServiceState.ERROR, f"{missing}进程意外退出")

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
