import sys
import os
import glob
import subprocess
import sqlite3
import psutil
import re
import time
import asyncio

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTextEdit, QLabel, QHBoxLayout, QTabWidget,
    QTableWidget, QTableWidgetItem, QMessageBox, QComboBox,
    QAbstractItemView
)
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtGui import QGuiApplication

from db.db import (
    get_personality, set_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases
)

ALEX_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ALEX_DIR, "config", "Logs")
OLLAMA_LOG_PATH = os.path.join(LOG_DIR, "ollama_output.log")
DB_PATH = os.path.join(ALEX_DIR, "db", "memory.db")

# Columns the DB browser tab never edits/writes — pickled embeddings would
# be corrupted by round-tripping through a text cell, so they're shown as
# a placeholder and simply left out of every INSERT/UPDATE it builds.
DB_BLOB_COLUMNS = {
    ("vector_memory", "embedding"),
    ("voice_profiles", "embedding"),
}

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

        # 🎭 A.L.E.X. TAB — personality display/reset live here as
        # on-demand buttons rather than a separate tab: hard resets belong
        # in the Controller, not chat/voice (fragile phrasing on a small
        # local model), but they're still just actions on this one tab.
        self.alex_tab = QWidget()
        alex_layout = QVBoxLayout()
        alex_layout.setContentsMargins(0, 0, 0, 0)

        alex_layout.addWidget(self.alex_log)

        alex_btns = QHBoxLayout()

        self.show_personality_btn = QPushButton("🎭 Show Personality")
        self.show_personality_btn.clicked.connect(self.show_personality)

        self.reset_personality_btn = QPushButton("♻️ Reset Personality to Default")
        self.reset_personality_btn.clicked.connect(self.reset_personality)

        self.reset_phrases_btn = QPushButton("♻️ Reset All Phrases to Default")
        self.reset_phrases_btn.clicked.connect(self.reset_phrases)

        for b in [self.show_personality_btn, self.reset_personality_btn, self.reset_phrases_btn]:
            alex_btns.addWidget(b)

        alex_layout.addLayout(alex_btns)
        self.alex_tab.setLayout(alex_layout)

        # 🗄️ DATABASE TAB — direct read/write view of db/memory.db.
        # Uses plain sqlite3 (not db.py's aiosqlite helpers) since this is
        # a generic, table-agnostic browser rather than the specific
        # queries db.py exposes; button-triggered, not on any hot path.
        self.db_tab = QWidget()
        db_layout = QVBoxLayout()

        db_top = QHBoxLayout()
        db_top.addWidget(QLabel("Table:"))

        self.db_table_selector = QComboBox()
        self.db_table_selector.currentTextChanged.connect(self.load_db_table_data)
        db_top.addWidget(self.db_table_selector)

        self.db_refresh_btn = QPushButton("🔄 Refresh")
        self.db_refresh_btn.clicked.connect(self.load_db_table_data)
        db_top.addWidget(self.db_refresh_btn)

        db_layout.addLayout(db_top)

        self.db_table = QTableWidget()
        self.db_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        db_layout.addWidget(self.db_table)

        db_btns = QHBoxLayout()

        self.db_add_row_btn = QPushButton("➕ Add Row")
        self.db_add_row_btn.clicked.connect(self.add_db_row)

        self.db_delete_row_btn = QPushButton("🗑️ Delete Selected Row")
        self.db_delete_row_btn.clicked.connect(self.delete_db_row)

        self.db_save_btn = QPushButton("💾 Save Changes")
        self.db_save_btn.clicked.connect(self.save_db_changes)

        for b in [self.db_add_row_btn, self.db_delete_row_btn, self.db_save_btn]:
            db_btns.addWidget(b)

        db_layout.addLayout(db_btns)
        self.db_tab.setLayout(db_layout)

        self.tabs.addTab(self.ollama_log, "Ollama")
        self.tabs.addTab(self.alex_tab, "A.L.E.X.")
        self.tabs.addTab(self.db_tab, "Database")
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

        self.load_db_tables()

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

        # A.L.E.X. tab is a wrapper QWidget (log + buttons), not the
        # QTextEdit directly — copy its log content specifically.
        if current is self.alex_tab:
            current = self.alex_log

        if isinstance(current, QTextEdit):
            QGuiApplication.clipboard().setText(current.toPlainText())
            self.system_log.append("Copied logs")

    # ---------------- PERSONALITY (on-demand, lives on the A.L.E.X. tab) ----------------
    # Writes directly to the same sqlite DB the live ALEX process reads
    # from — no HTTP/WS round-trip needed since this Controller runs on the
    # same machine. Blocking asyncio.run() calls are fine here: these are
    # one-off admin actions from a button click, not a hot path.
    def show_personality(self):
        try:
            current = asyncio.run(get_personality())
            self.alex_log.append(f"[SYSTEM] Current personality: {current}")
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load personality: {e}")

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
            self.alex_log.append("[SYSTEM] Personality reset to default via Controller")
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to reset personality: {e}")

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
            self.alex_log.append("[SYSTEM] All phrases reset to default via Controller")
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to reset phrases: {e}")

    # ---------------- DATABASE BROWSER ----------------
    # Plain sqlite3, not db.py's aiosqlite helpers — this is a generic
    # table browser, not a specific query. Same DB file the live ALEX
    # process uses; sqlite handles the concurrent access fine for these
    # short, infrequent, button-triggered transactions.
    NEW_ROW_MARKER = "(new)"

    def load_db_tables(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [r[0] for r in cursor.fetchall()]
            conn.close()
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to list database tables: {e}")
            return

        self.db_table_selector.blockSignals(True)
        self.db_table_selector.clear()
        self.db_table_selector.addItems(tables)
        self.db_table_selector.blockSignals(False)

        self.load_db_table_data()

    def load_db_table_data(self):
        table = self.db_table_selector.currentText()
        if not table:
            self.db_table.setRowCount(0)
            self.db_table.setColumnCount(0)
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
            rows = conn.execute(f"SELECT rowid, * FROM '{table}'").fetchall()
            conn.close()
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load table '{table}': {e}")
            return

        self.db_current_table = table
        self.db_current_columns = columns
        blob_cols = {c for t, c in DB_BLOB_COLUMNS if t == table}
        self.db_current_blob_cols = blob_cols

        headers = ["rowid"] + columns
        self.db_table.setColumnCount(len(headers))
        self.db_table.setHorizontalHeaderLabels(headers)
        self.db_table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                col_name = headers[c]

                if col_name in blob_cols:
                    item = QTableWidgetItem(f"<{len(value) if value else 0} bytes>" if value is not None else "")
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                else:
                    item = QTableWidgetItem("" if value is None else str(value))

                if col_name == "rowid":
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)

                self.db_table.setItem(r, c, item)

    def add_db_row(self):
        if not getattr(self, "db_current_table", None):
            return

        headers = ["rowid"] + self.db_current_columns
        row = self.db_table.rowCount()
        self.db_table.insertRow(row)

        for c, col_name in enumerate(headers):
            if col_name == "rowid":
                item = QTableWidgetItem(self.NEW_ROW_MARKER)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            elif col_name in self.db_current_blob_cols:
                item = QTableWidgetItem("")
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            else:
                item = QTableWidgetItem("")

            self.db_table.setItem(row, c, item)

        self.db_table.scrollToBottom()

    def delete_db_row(self):
        table = getattr(self, "db_current_table", None)
        if not table:
            return

        rows = sorted({idx.row() for idx in self.db_table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return

        real_rowids = [
            self.db_table.item(r, 0).text() for r in rows
            if self.db_table.item(r, 0).text() != self.NEW_ROW_MARKER
        ]

        if real_rowids:
            confirm = QMessageBox.question(
                self, "Delete Row(s)",
                f"Permanently delete {len(real_rowids)} row(s) from '{table}'? This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if confirm != QMessageBox.Yes:
                return

            try:
                conn = sqlite3.connect(DB_PATH)
                for rowid in real_rowids:
                    conn.execute(f"DELETE FROM '{table}' WHERE rowid=?", (rowid,))
                conn.commit()
                conn.close()
                self.alex_log.append(f"[SYSTEM] Deleted {len(real_rowids)} row(s) from '{table}' via Controller")
            except Exception as e:
                self.alex_log.append(f"⚠️ Failed to delete row(s): {e}")

        # remove locally too (covers both real rows just deleted and any
        # not-yet-saved "(new)" rows the user wants to discard)
        for r in rows:
            self.db_table.removeRow(r)

    def save_db_changes(self):
        table = getattr(self, "db_current_table", None)
        if not table:
            return

        writable_cols = [c for c in self.db_current_columns if c not in self.db_current_blob_cols]
        inserted = updated = 0

        try:
            conn = sqlite3.connect(DB_PATH)

            for r in range(self.db_table.rowCount()):
                rowid_item = self.db_table.item(r, 0)
                rowid = rowid_item.text() if rowid_item else ""

                values = []
                for col_name in writable_cols:
                    c = (["rowid"] + self.db_current_columns).index(col_name)
                    cell = self.db_table.item(r, c)
                    values.append(cell.text() if cell else "")

                if rowid == self.NEW_ROW_MARKER:
                    placeholders = ",".join("?" for _ in writable_cols)
                    col_list = ",".join(f"'{c}'" for c in writable_cols)
                    conn.execute(
                        f"INSERT INTO '{table}' ({col_list}) VALUES ({placeholders})",
                        values
                    )
                    inserted += 1
                else:
                    set_clause = ",".join(f"'{c}'=?" for c in writable_cols)
                    conn.execute(
                        f"UPDATE '{table}' SET {set_clause} WHERE rowid=?",
                        values + [rowid]
                    )
                    updated += 1

            conn.commit()
            conn.close()
            self.alex_log.append(f"[SYSTEM] Saved '{table}': {inserted} inserted, {updated} updated (via Controller)")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", f"Failed to save changes to '{table}':\n{e}")
            return

        self.load_db_table_data()

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
