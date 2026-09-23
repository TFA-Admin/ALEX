# controller/common.py
"""Paths, settings, table helpers, the log tailer and the process-finding
functions every view shares. Nothing here touches her database schema."""
import os
import json
import glob
import time
from datetime import datetime, timezone

import psutil

from PySide6.QtWidgets import QTableWidgetItem, QHeaderView
from PySide6.QtCore import QThread, Signal, Qt

ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-09-21: which model she runs is chosen here and passed to her process
# as ALEX_LLM_MODEL (llm/ollama_client.py reads it). It used to be whatever
# the Controller's own environment happened to carry, which meant editing
# a Windows user variable and restarting everything to compare models —
# the same trap OLLAMA_MAX_LOADED_MODELS turned out to be. Persisted in a
# small JSON file so a choice survives a Controller restart.
CONTROLLER_SETTINGS_PATH = os.path.join(ALEX_DIR, "config", "controller_settings.json")
DEFAULT_ALEX_MODEL = "qwen2.5:7b"
OLLAMA_MODELS_DIR = os.getenv("OLLAMA_MODELS", "D:/project_ALEX/Ollama Models")

LOG_DIR = os.path.join(ALEX_DIR, "config", "Logs")
OLLAMA_LOG_PATH = os.path.join(LOG_DIR, "ollama_output.log")
DB_PATH = os.path.join(ALEX_DIR, "db", "memory.db")

# Columns the DB browser never edits/writes — pickled embeddings would be
# corrupted by round-tripping through a text cell, so they're shown as a
# placeholder and simply left out of every INSERT/UPDATE it builds.
DB_BLOB_COLUMNS = {
    ("memory", "embedding"),
    ("voice_profiles", "embedding"),
    ("learned_knowledge", "embedding"),
}

# Ollama lives outside this repo — env var lets this run on a machine with
# a different drive/folder layout without editing code; default matches
# this machine's current setup. ALEX.py's own working directory is always
# the project folder, so that one doesn't need an env var at all.
OLLAMA_EXE_PATH = os.getenv("ALEX_OLLAMA_EXE", "D:/project_ALEX/Ollama/ollama.exe")


def load_controller_settings() -> dict:
    try:
        with open(CONTROLLER_SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_controller_settings(data: dict):
    try:
        with open(CONTROLLER_SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def installed_ollama_models() -> list:
    """Model names from Ollama's manifests on disk, so the list is right
    even while Ollama is stopped. Empty if the directory is not where
    OLLAMA_MODELS says; the selector is editable either way."""
    root = os.path.join(OLLAMA_MODELS_DIR, "manifests", "registry.ollama.ai", "library")
    found = []
    try:
        for name in sorted(os.listdir(root)):
            for tag in sorted(os.listdir(os.path.join(root, name))):
                found.append(f"{name}:{tag}")
    except OSError:
        pass
    return found


# -----------------------------
# Time. Her database stamps rows with sqlite's CURRENT_TIMESTAMP, which is
# UTC; her log is local. The Controller shows local, and says how long ago.
# -----------------------------
def parse_utc(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_local(ts) -> str:
    dt = parse_utc(ts)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else ("" if ts is None else str(ts))


def ago(ts) -> str:
    dt = parse_utc(ts)
    if not dt:
        return ""
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


# -----------------------------
# Tables
# -----------------------------
def make_readable(table, wrap_column: int):
    """Long values in these tables were unreadable — every column sized
    itself equally and Qt elided the rest.

    2026-09-20 (Craig, on the Notifications tab): "I can't actually read
    all these." The personality-change rows are the worst case: the whole
    point of the row is the new personality text and the reason it changed,
    and both came out as "Be more concise, direct, and to the point. Use
    humor sparingly, only when contextually fitting and ..." next to
    "Reduced ...".

    Three changes, together: the content column takes the leftover width
    while the short ones size to their contents, rows grow to fit wrapped
    text, and every cell carries its full value as a tooltip so nothing is
    lost even when a row is still too narrow."""
    header = table.horizontalHeader()
    for col in range(table.columnCount()):
        header.setSectionResizeMode(
            col, QHeaderView.Stretch if col == wrap_column
            else QHeaderView.ResizeToContents)
    table.setWordWrap(True)
    table.verticalHeader().setVisible(False)


def fill_row(table, row, values):
    """Sets a row and gives every cell the full text as a tooltip."""
    for col, value in enumerate(values):
        text = "" if value is None else str(value)
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setToolTip(text)
        table.setItem(row, col, item)


# -----------------------------
# Colour by state (2026-09-23). Craig: "There doesn't really seem to be
# any distinguishing between something that's in or got rejected... Is
# there a way to split out the old from the new, the rejected from the
# approved? Maybe some color coding?" One palette for every table, keyed
# by the words that already appear in its status/kind column, so a row's
# state is visible before it is read. Translucent, so it works on light
# and dark themes.
# -----------------------------
from PySide6.QtGui import QColor, QBrush

TINTS = {
    "open":    QColor(255, 179, 0, 55),     # amber: waiting on him
    "active":  QColor(66, 133, 244, 55),    # blue: in progress / being tried
    "good":    QColor(46, 160, 67, 60),     # green: accepted / done / confirmed
    "bad":     QColor(220, 53, 69, 60),     # red: rejected / retracted / a slip
    "settled": QColor(128, 128, 128, 45),   # grey: old, backlog, declined
}

# status or kind text -> tint key (matched by substring, first hit wins)
_STATE_WORDS = (
    ("unconfirmed", "open"), ("requested", "open"), ("authored", "open"), ("proposed", "open"),
    ("planned", "open"), ("pending", "open"),
    ("gated", "active"), ("in_progress", "active"), ("in progress", "active"), ("running", "active"),
    ("confirmed", "good"), ("merged", "good"), ("done", "good"), ("approval", "good"),
    ("confirmation", "good"), ("built", "good"), ("retained", "good"),
    ("rejected", "bad"), ("rejection", "bad"), ("retracted", "bad"), ("retraction", "bad"),
    ("fabrication", "bad"), ("denied", "bad"), ("failed", "bad"),
    ("declined", "settled"), ("backlog", "settled"), ("speaker", "settled"),
)


def tint_for(text) -> QColor | None:
    low = (text or "").lower()
    for word, key in _STATE_WORDS:
        if word in low:
            return TINTS[key]
    return None


def tint_row(table, row, color):
    if color is None:
        return
    brush = QBrush(color)
    for col in range(table.columnCount()):
        item = table.item(row, col)
        if item is not None:
            item.setBackground(brush)


def tint_by_column(table, col):
    """Tints every row from the text in `col` (its status or kind)."""
    for row in range(table.rowCount()):
        item = table.item(row, col)
        if item is not None:
            tint_row(table, row, tint_for(item.text()))


def selected_rows(table) -> list:
    """Row indexes of the current selection, ascending."""
    model = table.selectionModel()
    if model is None:
        return []
    return sorted({idx.row() for idx in model.selectedRows()})


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
                # 2026-07-18 (Craig: "the ollama tab lags before it lets
                # me in") — found live: ollama_output.log is a single,
                # never-rotated file that just keeps growing for the
                # server's entire uptime (confirmed: 50,000+ lines this
                # session alone), unlike ALEX's own per-run timestamped
                # logs. Starting at position 0 on every attach replayed
                # the ENTIRE file into the QTextEdit in one burst every
                # time the Controller (re)launched — that's what was
                # actually causing the lag, not anything about how much
                # is visible on screen at once. Seeking to the current
                # end-of-file instead means only genuinely NEW lines ever
                # get tailed, same as `tail -f` with no -n. Trade-off:
                # a few lines written between the file appearing and this
                # seek (a handful of ms, this poll interval is 0.5s) can
                # be missed — the real log file on disk still has
                # everything, this only affects what's replayed into the
                # Controller's live view.
                try:
                    self._position = os.path.getsize(latest)
                except OSError:
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


# -----------------------------
# Processes and ports
# -----------------------------
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


def find_orphan_processes():
    """
    Finds stray Ollama/ALEX processes that aren't the one actually serving
    their port — leftovers from restarts that were never cleanly killed.
    Confirmed real: earlier this session two orphaned "ollama" processes
    were each holding a full model copy in VRAM (~5GB each) despite
    neither one owning port 11434 — the active listener was a third,
    separate process. Only matches the exact "ollama.exe" binary name
    (not the "ollama app" tray/updater helper, a legitimate singleton) and
    "python.exe" processes whose command line names ALEX.py specifically.

    Deliberately requires "serve" in the command line for Ollama —
    confirmed live that Ollama spawns a separate "ollama.exe runner ..."
    child process per loaded model, which also reports as "ollama.exe" by
    name but is NOT a duplicate server; matching on name alone would have
    let this flag (and let someone terminate) a legitimately in-use model
    runner, not an orphan.

    Also walks the active A.L.E.X. process's full parent chain and
    excludes every ancestor — confirmed live (root-caused after an
    earlier incident where killing a "confirmed orphan" also took the
    real server down) that launching "python ALEX.py" through Windows'
    Python launcher stub (PyManager's python.exe) runs the real
    interpreter as a CHILD process; the parent must stay alive for the
    child to keep running, so it's not a true orphan even though it
    doesn't itself own port 5000.
    """
    active_ollama_pid = find_pid_by_port(11434)
    active_alex_pid = find_pid_by_port(5000)

    protected_alex_pids = {active_alex_pid}
    try:
        if active_alex_pid:
            proc = psutil.Process(active_alex_pid)
            while True:
                proc = proc.parent()
                if not proc:
                    break
                protected_alex_pids.add(proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    orphans = []

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            pid = proc.info["pid"]
            cmdline = proc.info["cmdline"] or []
            cmdline_str = " ".join(str(c) for c in cmdline).lower()

            if name == "ollama.exe" and "serve" in cmdline_str and pid != active_ollama_pid:
                orphans.append(("Ollama", pid))

            elif name == "python.exe":
                if any("alex.py" in str(c).lower() for c in cmdline) and pid not in protected_alex_pids:
                    orphans.append(("A.L.E.X", pid))

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return orphans
