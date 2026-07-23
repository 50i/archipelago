from __future__ import annotations

import html
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

try:
    import requests
except ImportError:  # handled in the worker so the GUI can still open
    requests = None

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


ARCHIPELAGO_UPLOADS_URL = "https://archipelago.gg/uploads"
ARCHIPELAGO_NEW_ROOM_URL = "https://archipelago.gg/new_room/{seed_id}"
CONNECT_RE = re.compile(r"/connect\s+archipelago\.gg:\d+")
ARCHIPELAGO_LINK_RE = re.compile(r"archipelago://[^@\"'<>\s]+@(?P<host>archipelago\.gg):(?P<port>\d+)")
PORT_RE = re.compile(r"(?:with port|port is)\s+(?P<port>\d+)", re.IGNORECASE)
SEED_RE = re.compile(r"/seed/([^/?#]+)")


class SeedWorker(QObject):
    log = pyqtSignal(str)
    result = pyqtSignal(str, str, str, str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, python_exe: Path, archipelago_dir: Path, source_yaml: Path, output_root: Path):
        super().__init__()
        self.python_exe = python_exe
        self.archipelago_dir = archipelago_dir
        self.source_yaml = source_yaml
        self.output_root = output_root

    def run(self) -> None:
        try:
            self._validate()
            if requests is None:
                raise RuntimeError("The Python environment running this GUI needs the 'requests' package installed.")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            work_root = self.output_root / f"run_{stamp}"
            work_root.mkdir(parents=True, exist_ok=True)

            for player_name in ("Max", "Henry"):
                self._log("")
                self._log(f"=== {player_name} ===")
                zip_path = self._generate_player_seed(player_name, work_root)
                seed_url, seed_id = self._upload_seed(zip_path)
                room_url, connect_command = self._create_room(seed_id)
                self.result.emit(player_name, connect_command, seed_url, room_url)
                self._log(f"{player_name} connect command: {connect_command}")

            self._log("")
            self._log("Done.")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def _validate(self) -> None:
        if not self.python_exe.is_file():
            raise FileNotFoundError(f"Python executable not found: {self.python_exe}")
        if not self.archipelago_dir.is_dir():
            raise FileNotFoundError(f"Archipelago folder not found: {self.archipelago_dir}")
        if not (self.archipelago_dir / "Generate.py").is_file():
            raise FileNotFoundError(f"Generate.py not found in: {self.archipelago_dir}")
        if not self.source_yaml.is_file():
            raise FileNotFoundError(f"Source YAML not found: {self.source_yaml}")

    def _generate_player_seed(self, player_name: str, work_root: Path) -> Path:
        player_dir = work_root / f"{player_name}_Players"
        output_dir = work_root / f"{player_name}_Output"
        player_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        player_yaml = player_dir / f"{player_name}.yaml"
        self._write_player_yaml(player_yaml, player_name)
        self._log(f"Wrote {player_yaml}")

        command = [
            str(self.python_exe),
            "Generate.py",
            "--player_files_path",
            str(player_dir),
            "--outputpath",
            str(output_dir),
        ]
        self._log("Generating seed...")
        self._run_command(command)

        zips = sorted(output_dir.glob("AP_*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not zips:
            raise RuntimeError(f"Generator finished, but no AP_*.zip appeared in {output_dir}")

        self._log(f"Generated {zips[0]}")
        return zips[0]

    def _write_player_yaml(self, destination: Path, player_name: str) -> None:
        text = self.source_yaml.read_text(encoding="utf-8-sig")
        text = self._clean_yaml_text(text)
        lines = text.splitlines(keepends=True)

        for index, line in enumerate(lines):
            if re.match(r"^\s*name\s*:", line):
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                prefix = re.match(r"^(\s*name\s*:\s*)", line).group(1)
                lines[index] = f"{prefix}{player_name}{ending}"
                destination.write_text("".join(lines), encoding="utf-8")
                return

        raise RuntimeError(f"Could not find a 'name:' line in {self.source_yaml}")

    def _clean_yaml_text(self, text: str) -> str:
        return re.sub(r"(?m)^(\s*[A-Za-z0-9_-]+\s*:.*?)\s+`\s*$", r"\1", text)

    def _run_command(self, command: list[str]) -> None:
        process = subprocess.Popen(
            command,
            cwd=str(self.archipelago_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        assert process.stdout is not None
        for line in process.stdout:
            self._log(line.rstrip())

        return_code = process.wait()
        if return_code:
            raise RuntimeError(f"Generate.py failed with exit code {return_code}.")

    def _upload_seed(self, zip_path: Path) -> tuple[str, str]:
        self._log("Uploading to archipelago.gg...")
        session = requests.Session()
        session.get(ARCHIPELAGO_UPLOADS_URL, timeout=30)
        with zip_path.open("rb") as zip_file:
            response = session.post(
                ARCHIPELAGO_UPLOADS_URL,
                files={"file": (zip_path.name, zip_file, "application/zip")},
                allow_redirects=True,
                timeout=120,
            )

        response.raise_for_status()
        seed_match = SEED_RE.search(response.url)
        if not seed_match:
            raise RuntimeError(f"Upload did not redirect to a seed page. Last URL: {response.url}")

        seed_id = seed_match.group(1)
        self._log(f"Uploaded seed: {response.url}")
        self._session = session
        return response.url, seed_id

    def _create_room(self, seed_id: str) -> tuple[str, str]:
        self._log("Creating room and waiting for the server port...")
        session = self._session
        room_url = ARCHIPELAGO_NEW_ROOM_URL.format(seed_id=seed_id)
        response = session.get(room_url, allow_redirects=True, timeout=30)
        response.raise_for_status()
        room_url = response.url

        for attempt in range(1, 6):
            page = html.unescape(response.text)
            connect_command = self._extract_connect_command(page)
            if connect_command:
                return room_url, self._display_connect_command(connect_command)

            self._log(f"Room is still starting... ({attempt}/5)")
            time.sleep(1.5)
            response = session.get(room_url, allow_redirects=True, timeout=30)
            response.raise_for_status()
            room_url = response.url

        raise RuntimeError(f"Room was created, but no connect command appeared yet: {room_url}")

    def _extract_connect_command(self, page: str) -> Optional[str]:
        connect_match = CONNECT_RE.search(page)
        if connect_match and self._connect_command_has_real_port(connect_match.group(0)):
            return connect_match.group(0)

        link_match = ARCHIPELAGO_LINK_RE.search(page)
        if link_match and int(link_match.group("port")) > 0:
            return f"/connect {link_match.group('host')}:{link_match.group('port')}"

        port_match = PORT_RE.search(page)
        if port_match and int(port_match.group("port")) > 0 and "archipelago.gg" in page:
            return f"/connect archipelago.gg:{port_match.group('port')}"

        return None

    def _connect_command_has_real_port(self, command: str) -> bool:
        port = command.rsplit(":", 1)[-1]
        return port.isdigit() and int(port) > 0

    def _display_connect_command(self, command: str) -> str:
        return re.sub(r"^/connect\b", "!connect", command)

    def _log(self, message: str) -> None:
        self.log.emit(message)


class SeedGui(QMainWindow):
    def __init__(self):
        super().__init__()
        self.thread: Optional[QThread] = None
        self.worker: Optional[SeedWorker] = None
        self.result_fields: Dict[str, QLineEdit] = {}
        self.url_fields: Dict[str, QLineEdit] = {}

        self.setWindowTitle("Toontown Archipelago Seed Generator")
        self.resize(900, 680)

        archipelago_dir = Path(__file__).resolve().parent
        default_python = Path(r"c:\Users\max\AppData\Local\Programs\Python\Python387\python.exe")
        default_yaml = archipelago_dir.parent / "toontown-archipelago" / "launch" / "windows" / "Default.yaml"
        default_output = archipelago_dir / "output" / "seed_gui"

        root = QWidget()
        layout = QVBoxLayout(root)

        title = QLabel("Toontown Seed Generator")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        form_box = QGroupBox("Paths")
        form = QFormLayout(form_box)
        self.python_edit = self._path_row(form, "Python:", default_python, self._pick_python)
        self.archipelago_edit = self._path_row(form, "Archipelago folder:", archipelago_dir, self._pick_archipelago)
        self.yaml_edit = self._path_row(form, "Source YAML:", default_yaml, self._pick_yaml)
        self.output_edit = self._path_row(form, "Output folder:", default_output, self._pick_output)
        layout.addWidget(form_box)

        self.start_button = QPushButton("Generate + Upload Max and Henry")
        self.start_button.clicked.connect(self.start_generation)
        layout.addWidget(self.start_button)

        result_box = QGroupBox("Connect Commands")
        result_layout = QGridLayout(result_box)
        result_layout.addWidget(QLabel("Player"), 0, 0)
        result_layout.addWidget(QLabel("Connect Command"), 0, 1)
        result_layout.addWidget(QLabel("Seed Page"), 0, 2)
        for row, player_name in enumerate(("Max", "Henry"), start=1):
            result_layout.addWidget(QLabel(player_name), row, 0)
            command_edit = QLineEdit()
            command_edit.setReadOnly(True)
            seed_edit = QLineEdit()
            seed_edit.setReadOnly(True)
            copy_button = QPushButton("Copy")
            copy_button.clicked.connect(lambda _checked=False, name=player_name: self.copy_command(name))
            result_layout.addWidget(command_edit, row, 1)
            result_layout.addWidget(seed_edit, row, 2)
            result_layout.addWidget(copy_button, row, 3)
            self.result_fields[player_name] = command_edit
            self.url_fields[player_name] = seed_edit
        layout.addWidget(result_box)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit, stretch=1)

        self.setCentralWidget(root)

    def _path_row(self, form: QFormLayout, label: str, default: Path, picker) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit(str(default))
        button = QPushButton("Browse...")
        button.clicked.connect(picker)
        row.addWidget(edit, stretch=1)
        row.addWidget(button)
        form.addRow(label, row)
        return edit

    def _pick_python(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Python executable", self.python_edit.text())
        if path:
            self.python_edit.setText(path)

    def _pick_archipelago(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose Archipelago folder", self.archipelago_edit.text())
        if path:
            self.archipelago_edit.setText(path)

    def _pick_yaml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose source YAML", self.yaml_edit.text(), "YAML (*.yaml *.yml)")
        if path:
            self.yaml_edit.setText(path)

    def _pick_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def start_generation(self) -> None:
        self.start_button.setEnabled(False)
        self.log_edit.clear()
        for field in list(self.result_fields.values()) + list(self.url_fields.values()):
            field.clear()

        self.thread = QThread()
        self.worker = SeedWorker(
            Path(self.python_edit.text()).expanduser(),
            Path(self.archipelago_edit.text()).expanduser(),
            Path(self.yaml_edit.text()).expanduser(),
            Path(self.output_edit.text()).expanduser(),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.result.connect(self.set_result)
        self.worker.failed.connect(self.show_failure)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self.start_button.setEnabled(True))
        self.thread.start()

    def append_log(self, message: str) -> None:
        self.log_edit.append(message)

    def set_result(self, player_name: str, command: str, seed_url: str, room_url: str) -> None:
        self.result_fields[player_name].setText(command)
        self.url_fields[player_name].setText(seed_url)
        self.append_log(f"{player_name} room: {room_url}")

    def show_failure(self, message: str) -> None:
        self.append_log("")
        self.append_log(f"ERROR: {message}")
        QMessageBox.critical(self, "Seed generation failed", message)

    def copy_command(self, player_name: str) -> None:
        command = self.result_fields[player_name].text()
        if command:
            QApplication.clipboard().setText(command)


def main() -> int:
    app = QApplication(sys.argv)
    window = SeedGui()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
