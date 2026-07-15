import sys
import os
import glob
import subprocess
import psutil
import re
import time
import asyncio

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTextEdit, QLabel, QHBoxLayout, QTabWidget,
    QTableWidget, QTableWidgetItem, QMessageBox
)
from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtGui import QGuiApplication

from db.db import (
    get_personality, set_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases
)

ALEX_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ALEX_DIR, "config", "Logs")
OLLAMA_LOG_PATH = os.path.join(LOG_DIR, "ollama_output.log")

# Ollama lives outside this repo — env var lets this run on a machine with
# a different drive/folder layout without editing code; default matches
# this machine's current setup. ALEX.py's own working directory is always
# this script's own folder, so that one doesn't need an env var at all.
OLLAMA_EXE_PATH = os.getenv("ALEX_OLLAMA_EXE", "D:/project_ALEX/Ollama/ollama.exe")


# -----------------------------
# 📄 Log File Tailer — watches a log file directly, so the Controller can
# see what's happening whether it launched the process itself or not (e.g.
# started manually, or by an external tool during development). Works for
# both ALEX (new timestamped file per run — pattern is a glob) and Ollama
# (one stable file — pattern is just its literal name).
# -----------------------------
class LogFileTailer(QThread):
    log_signal = Signal(str)

    def __init__(self, log_dir, pattern="alex_*.log", tag="ALEX", poll_interval=0.5):
        super().__init__()
        self.log_dir = log_dir
        self.pattern = pattern
        self.tag = tag
        self.poll_interval = poll_interval
        self._running = True
        self._current_file = None
        self._position = 0

    def _find_latest_log(self):
        files = glob.glob(os.path.join(self.log_dir, self.pattern))
        if not files:
            return None
        return max(files, key=os.path.getmtime)

    def run(self):
        while self._running:
            latest = self._find_latest_log()

            if latest and latest != self._current_file:
                self._current_file = latest
                self._position = 0
                self.log_signal.emit(f"[SYSTEM] Attached to log: {os.path.basename(latest)}")

            if self._current_file and os.path.exists(self._current_file):
                try:
                    with open(self._current_file, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(self._position)
                        new_data = f.read()
                        self._position = f.tell()

                    for line in new_data.splitlines():
                        if line.strip():
                            self.log_signal.emit(f"[{self.tag}] {line}")
                except Exception:
                    pass

            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False


def is_port_open(port: int) -> bool:
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return True
    except Exception:
        pass
    return False


def find_pid_by_port(port: int):
    # Lets Stop work on a process this Controller didn't launch itself
    # (e.g. started externally during development) — mirrors how log
    # tailing already works regardless of who started the process.
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        pass
    return None


# -----------------------------
# 🧠 Main UI
# -----------------------------
class AlexController(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("A.L.E.X Unified Controller")
        self.resize(1150, 700)

        # Processes (only set when the Controller itself launches them)
        self.ollama_proc = None
        self.alex_proc = None
        self.ollama_log_file = None  # kept open for the life of a Controller-launched Ollama process

        # Always-on log tailers — see activity regardless of who started the process
        self.alex_tailer = None
        self.ollama_tailer = None

        # Session tracking
        self.sessions = {}  # session_id -> start_time

        layout = QVBoxLayout()

        # ---------------- STATUS ----------------
        self.status_label = QLabel("Status: 🔴 Idle")
        layout.addWidget(self.status_label)

        # ---------------- METRICS ROW ----------------
        metrics = QHBoxLayout()

        self.cpu_label = QLabel("CPU: --%")
        self.ram_label = QLabel("RAM: --%")
        self.gpu_label = QLabel("GPU: --%")
        self.vram_label = QLabel("VRAM: -- MB")
        self.conn_label = QLabel("Connections: 0")

        for w in [self.cpu_label, self.ram_label, self.gpu_label, self.vram_label, self.conn_label]:
            metrics.addWidget(w)

        layout.addLayout(metrics)

        # ---------------- BUTTONS ----------------
        btns = QHBoxLayout()

        self.start_ollama_btn = QPushButton("Start Ollama")
        self.start_ollama_btn.clicked.connect(self.start_ollama)

        self.stop_ollama_btn = QPushButton("Stop Ollama")
        self.stop_ollama_btn.clicked.connect(self.stop_ollama)

        self.start_alex_btn = QPushButton("Start A.L.E.X")
        self.start_alex_btn.clicked.connect(self.start_alex)

        self.stop_alex_btn = QPushButton("Stop A.L.E.X")
        self.stop_alex_btn.clicked.connect(self.stop_alex)

        self.copy_btn = QPushButton("📋 Copy Current Tab")
        self.copy_btn.clicked.connect(self.copy_logs)

        for b in [self.start_ollama_btn, self.stop_ollama_btn,
                  self.start_alex_btn, self.stop_alex_btn, self.copy_btn]:
            btns.addWidget(b)

        layout.addLayout(btns)

        # ---------------- TABS ----------------
        self.tabs = QTabWidget()

        self.ollama_log = QTextEdit()
        self.alex_log = QTextEdit()
        self.system_log = QTextEdit()

        for t in [self.ollama_log, self.alex_log, self.system_log]:
            t.setReadOnly(True)

        # 👥 USERS TABLE
        self.user_table = QTableWidget()
        self.user_table.setColumnCount(2)
        self.user_table.setHorizontalHeaderLabels(["Session ID", "Connected (s)"])

        # 🎭 PERSONALITY TAB — hard resets belong here, not in chat/voice
        # (chat-based reset phrasing is fragile on a small local model; a
        # button is unambiguous)
        self.personality_tab = QWidget()
        p_layout = QVBoxLayout()

        p_layout.addWidget(QLabel("Current personality:"))

        self.personality_display = QTextEdit()
        self.personality_display.setReadOnly(True)
        p_layout.addWidget(self.personality_display)

        p_btns = QHBoxLayout()

        self.refresh_personality_btn = QPushButton("🔄 Refresh")
        self.refresh_personality_btn.clicked.connect(self.refresh_personality)

        self.reset_personality_btn = QPushButton("♻️ Reset Personality to Default")
        self.reset_personality_btn.clicked.connect(self.reset_personality)

        self.reset_phrases_btn = QPushButton("♻️ Reset All Phrases to Default")
        self.reset_phrases_btn.clicked.connect(self.reset_phrases)

        for b in [self.refresh_personality_btn, self.reset_personality_btn, self.reset_phrases_btn]:
            p_btns.addWidget(b)

        p_layout.addLayout(p_btns)
        self.personality_tab.setLayout(p_layout)

        self.tabs.addTab(self.ollama_log, "Ollama")
        self.tabs.addTab(self.alex_log, "A.L.E.X.")
        self.tabs.addTab(self.personality_tab, "Personality")
        self.tabs.addTab(self.user_table, "Users")
        self.tabs.addTab(self.system_log, "System")

        layout.addWidget(self.tabs)
        self.setLayout(layout)

        # ---------------- TIMERS ----------------
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(1000)

        # ---------------- ALWAYS-ON LOG TAILING ----------------
        # Starts watching regardless of whether this Controller launched
        # ALEX — so it stays useful even when she's started some other way.
        self.start_log_tailing()

        self.refresh_personality()

    # ---------------- STATUS ----------------
    def update_status(self):
        ollama_up = bool(self.ollama_proc) or is_port_open(11434)
        alex_up = bool(self.alex_proc) or is_port_open(5000)

        if ollama_up and alex_up:
            self.status_label.setText("Status: 🟢 Ollama + A.L.E.X Running")
        elif ollama_up:
            self.status_label.setText("Status: 🟡 Ollama Running")
        elif alex_up:
            self.status_label.setText("Status: 🟡 A.L.E.X Running")
        else:
            self.status_label.setText("Status: 🔴 Idle")

    # ---------------- METRICS ----------------
    def update_metrics(self):
        self.cpu_label.setText(f"CPU: {psutil.cpu_percent()}%")
        self.ram_label.setText(f"RAM: {psutil.virtual_memory().percent}%")

        try:
            result = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"]
            ).decode().strip()

            gpu, vram = result.split(",")
            self.gpu_label.setText(f"GPU: {gpu.strip()}%")
            self.vram_label.setText(f"VRAM: {vram.strip()} MB")
        except:
            self.gpu_label.setText("GPU: N/A")
            self.vram_label.setText("VRAM: N/A")

        # Update connections + table
        self.conn_label.setText(f"Connections: {len(self.sessions)}")
        self.update_user_table()
        self.update_status()

    # ---------------- USERS TABLE ----------------
    def update_user_table(self):
        self.user_table.setRowCount(len(self.sessions))

        for row, (sid, start) in enumerate(self.sessions.items()):
            elapsed = int(time.time() - start)

            self.user_table.setItem(row, 0, QTableWidgetItem(sid))
            self.user_table.setItem(row, 1, QTableWidgetItem(f"{elapsed}s"))

    # ---------------- LOG ROUTING ----------------
    def log(self, text):
        text = text.strip()

        # 👥 Detect session connect
        if "WS connected:" in text:
            match = re.search(r"WS connected:\s*([a-f0-9\-]+)", text)
            if match:
                sid = match.group(1)
                self.sessions[sid] = time.time()

            self.alex_log.append(text)
            return

        # 👥 Detect disconnect
        if "WS disconnected:" in text:
            match = re.search(r"WS disconnected:\s*([a-f0-9\-]+)", text)
            if match:
                sid = match.group(1)
                self.sessions.pop(sid, None)

            self.alex_log.append(text)
            return

        # 👥 HTTP / WS activity
        if "WebSocket /ws" in text or '"GET /' in text:
            return  # ignore spam, already tracked by session

        # 👥 New log file attached means ALEX's process (re)started — any
        # sessions we were tracking against the old process are necessarily
        # gone, since a fresh process can't have inherited live websockets.
        if text.startswith("[SYSTEM] Attached to log:") and self.sessions:
            self.sessions.clear()

        # ---------------- NORMAL ROUTING ----------------
        if "[Ollama]" in text:
            self.ollama_log.append(text)
        elif "[ALEX]" in text:
            self.alex_log.append(text)
        else:
            self.system_log.append(text)

    # ---------------- COPY ----------------
    def copy_logs(self):
        current = self.tabs.currentWidget()
        if isinstance(current, QTextEdit):
            QGuiApplication.clipboard().setText(current.toPlainText())
            self.system_log.append("Copied logs")

    # ---------------- PERSONALITY ----------------
    # Writes directly to the same sqlite DB the live ALEX process reads
    # from — no HTTP/WS round-trip needed since this Controller runs on the
    # same machine. Blocking asyncio.run() calls are fine here: these are
    # one-off admin actions from a button click, not a hot path.
    def refresh_personality(self):
        try:
            current = asyncio.run(get_personality())
            self.personality_display.setPlainText(current)
        except Exception as e:
            self.system_log.append(f"⚠️ Failed to load personality: {e}")

    def reset_personality(self):
        confirm = QMessageBox.question(
            self, "Reset Personality",
            "Reset A.L.E.X.'s personality to the default? This overrides anything she's "
            "developed on her own or that's been set previously.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            asyncio.run(set_personality(DEFAULT_PERSONALITY))
            asyncio.run(log_personality_change(DEFAULT_PERSONALITY, "creator reset via Controller", kind="personality"))
            self.system_log.append("[SYSTEM] Personality reset to default via Controller")
            self.refresh_personality()
        except Exception as e:
            self.system_log.append(f"⚠️ Failed to reset personality: {e}")

    def reset_phrases(self):
        confirm = QMessageBox.question(
            self, "Reset Phrases",
            "Reset all of A.L.E.X.'s learned/self-adjusted phrasing back to defaults?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            asyncio.run(reset_all_phrases())
            asyncio.run(log_personality_change("(all reset to defaults)", "creator reset via Controller", kind="phrases"))
            self.system_log.append("[SYSTEM] All phrases reset to default via Controller")
        except Exception as e:
            self.system_log.append(f"⚠️ Failed to reset phrases: {e}")

    # ---------------- ALWAYS-ON LOG TAILING ----------------
    def start_log_tailing(self):
        if not self.alex_tailer:
            self.alex_tailer = LogFileTailer(LOG_DIR, pattern="alex_*.log", tag="ALEX")
            self.alex_tailer.log_signal.connect(self.log)
            self.alex_tailer.start()

        if not self.ollama_tailer:
            self.ollama_tailer = LogFileTailer(LOG_DIR, pattern="ollama_output.log", tag="Ollama")
            self.ollama_tailer.log_signal.connect(self.log)
            self.ollama_tailer.start()

    # ---------------- OLLAMA ----------------
    def start_ollama(self):
        if self.ollama_proc:
            return

        self.log("[Ollama] Starting...")
        self.update_status()

        # Redirect to the same stable file the tailer watches, rather than
        # a PIPE — this way Ollama's output is visible the same way no
        # matter who launches the process, and an unread PIPE can't fill up
        # and block it.
        self.ollama_log_file = open(OLLAMA_LOG_PATH, "ab")

        self.ollama_proc = subprocess.Popen(
            [OLLAMA_EXE_PATH, "serve"],
            stdout=self.ollama_log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        self.update_status()

    def stop_ollama(self):
        if self.ollama_proc:
            self.ollama_proc.terminate()
            self.ollama_proc = None
        else:
            pid = find_pid_by_port(11434)
            if pid:
                try:
                    psutil.Process(pid).terminate()
                    self.log(f"[SYSTEM] Stopped externally-launched Ollama (PID {pid})")
                except Exception as e:
                    self.log(f"[SYSTEM] Failed to stop Ollama: {e}")

        if self.ollama_log_file:
            self.ollama_log_file.close()
            self.ollama_log_file = None

        self.update_status()

    # ---------------- ALEX ----------------
    def start_alex(self):
        if self.alex_proc:
            return

        self.log("[ALEX] Starting...")
        self.update_status()

        # stdout/stderr go to DEVNULL, not PIPE — nothing reads this pipe
        # (the log tailer watches the log file instead), and an unread
        # PIPE would eventually fill and block the whole process once its
        # buffer fills up.
        self.alex_proc = subprocess.Popen(
            ["python", "-X", "utf8", "ALEX.py"],
            cwd=ALEX_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        self.update_status()

    def stop_alex(self):
        if self.alex_proc:
            self.alex_proc.terminate()
            self.alex_proc = None
        else:
            pid = find_pid_by_port(5000)
            if pid:
                try:
                    psutil.Process(pid).terminate()
                    self.log(f"[SYSTEM] Stopped externally-launched A.L.E.X (PID {pid})")
                except Exception as e:
                    self.log(f"[SYSTEM] Failed to stop A.L.E.X: {e}")

        self.update_status()


# -----------------------------
# 🏁 RUN
# -----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AlexController()
    window.show()
    sys.exit(app.exec())
