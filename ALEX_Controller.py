import sys
import os
import glob
import subprocess
import sqlite3
import psutil
import re
import time
import asyncio
import threading

# 2026-07-17: found live — crashed the instant Craig clicked "Decline
# Retention". Root cause: systems.inquiry.system (needed for
# retain_report()/decline_report()) transitively imports
# sentence_transformers -> sklearn, and importing that AFTER PySide6 is
# already loaded triggers a real, confirmed slow/broken interaction —
# PySide6/shiboken installs an import hook that inspects every class
# defined in every module imported afterward via inspect.getsource(),
# and sklearn's own imports get caught by it. An earlier attempt fixed
# this by importing it eagerly at module level instead, but that made
# EVERY Controller launch pay the full import cost just for two rarely-
# clicked buttons — moving it back to a lazy import inside the button
# handlers (a later attempt) just relocated the exact same hang to click
# time instead of launch time.
#
# Real fix: start the import in a background thread HERE, before PySide6
# is imported below — while the hook doesn't exist yet, so this can't be
# caught by it no matter how long it takes. It finishes (and gets cached
# in sys.modules) in parallel while the rest of the app starts up, so by
# the time a button handler actually needs it, it's normally just an
# instant cache hit. If a click somehow lands before this finishes,
# Python's own import lock makes that import call simply wait for this
# one to complete rather than starting a redundant, hook-exposed import
# of its own.
def _preload_inquiry_system():
    try:
        import systems.inquiry.system  # noqa: F401
    except Exception:
        pass


threading.Thread(target=_preload_inquiry_system, daemon=True).start()

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTextEdit, QLabel, QHBoxLayout, QTabWidget,
    QTableWidget, QTableWidgetItem, QMessageBox, QComboBox,
    QAbstractItemView, QLineEdit
)
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtGui import QGuiApplication

from db.db import (
    get_personality, set_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases, add_personality_hard_rule,
    resolve_module_build_request,
    fetch_recent_module_build_requests, approve_elevated_access,
    list_module_registry, fetch_recent_query_reports,
    fetch_unacknowledged_security_events, acknowledge_security_events,
    fetch_unacknowledged_personality_changes, acknowledge_personality_changes
)
from core.intent_classifier import merge_personality_change

ALEX_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ALEX_DIR, "config", "Logs")
OLLAMA_LOG_PATH = os.path.join(LOG_DIR, "ollama_output.log")
DB_PATH = os.path.join(ALEX_DIR, "db", "memory.db")

# Columns the DB browser tab never edits/writes — pickled embeddings would
# be corrupted by round-tripping through a text cell, so they're shown as
# a placeholder and simply left out of every INSERT/UPDATE it builds.
DB_BLOB_COLUMNS = {
    ("memory", "embedding"),
    ("voice_profiles", "embedding"),
    ("learned_knowledge", "embedding"),
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

        self.check_orphans_btn = QPushButton("🧹 Check for Orphans")
        self.check_orphans_btn.clicked.connect(lambda: self.check_for_orphans(prompt_if_none=True))

        for b in [self.start_alex_btn, self.stop_alex_btn,
                  self.start_ollama_btn, self.stop_ollama_btn, self.copy_btn,
                  self.check_orphans_btn]:
            btns.addWidget(b)

        layout.addLayout(btns)

        # ---------------- TABS ----------------
        self.tabs = QTabWidget()

        self.ollama_log = QTextEdit()
        self.alex_log = QTextEdit()
        self.system_log = QTextEdit()

        for t in [self.ollama_log, self.alex_log, self.system_log]:
            t.setReadOnly(True)

        # 👥 USERS TAB — self.sessions is populated/cleared by real "WS
        # connected:"/"WS disconnected:" log lines (see log()), but a
        # client that drops without a clean close (crash, network loss)
        # never logs a disconnect at all, leaving a phantom entry with no
        # way to know why it's stuck — only a full restart cleared these
        # before. 2026-07-16 (Craig): manual removal for exactly that
        # "we don't know the cause" case.
        self.users_tab = QWidget()
        users_layout = QVBoxLayout()

        self.user_table = QTableWidget()
        self.user_table.setColumnCount(2)
        self.user_table.setHorizontalHeaderLabels(["Session ID", "Connected (s)"])
        self.user_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.user_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        users_layout.addWidget(self.user_table)

        self.remove_session_btn = QPushButton("🗑️ Remove Selected (stale connection)")
        self.remove_session_btn.clicked.connect(self.remove_selected_session)
        users_layout.addWidget(self.remove_session_btn)

        self.users_tab.setLayout(users_layout)

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

        self.clear_console_btn = QPushButton("🧹 Clear Console")
        self.clear_console_btn.clicked.connect(self.alex_log.clear)

        for b in [self.show_personality_btn, self.reset_personality_btn, self.reset_phrases_btn, self.clear_console_btn]:
            alex_btns.addWidget(b)

        alex_layout.addLayout(alex_btns)

        # 2026-07-16 (Craig: "the vocal way seems very hit or miss") —
        # setting personality by voice/chat depends on classify_personality_set()
        # correctly reading open-ended phrasing on a small local model,
        # which isn't always reliable. This bypasses that classifier
        # entirely: same merge_personality_change() + add_personality_hard_rule()
        # pipeline the "be snarkier"-style chat path already uses, just
        # triggered directly from a dedicated text box instead of
        # depending on a classifier to first recognize intent. Takes
        # effect immediately, no restart — systems/llm/system.py reads
        # personality/hard rules fresh from the DB on every single turn.
        alex_layout.addWidget(QLabel("Personality override (typed instruction, e.g. \"be more direct and stop using emojis\"):"))

        personality_override_row = QHBoxLayout()

        self.personality_override_input = QLineEdit()
        self.personality_override_input.setPlaceholderText("Type an instruction and click Apply...")
        personality_override_row.addWidget(self.personality_override_input)

        self.apply_personality_btn = QPushButton("✅ Apply")
        self.apply_personality_btn.clicked.connect(self.apply_personality_override)
        personality_override_row.addWidget(self.apply_personality_btn)

        alex_layout.addLayout(personality_override_row)

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

        # 📋 MODULES TAB (2026-07-16, was "Requests" — Craig: "move
        # modules under their own tab, have it show their status etc as
        # well as the pending approvals") — everything module-related in
        # one place: what's actually installed and its live status, then
        # pending build requests, then recent activity/elevated-access
        # approvals. Internal variable/method names below still say
        # "requests_*" in places (e.g. self.requests_tab,
        # refresh_requests()) — renaming those is a pure-cosmetic,
        # file-wide mechanical change with zero effect on what Craig
        # actually sees, so left as-is rather than risking it for no
        # visible benefit.
        self.requests_tab = QWidget()
        requests_layout = QVBoxLayout()

        requests_layout.addWidget(QLabel("Installed modules:"))

        self.module_status_table = QTableWidget()
        self.module_status_table.setColumnCount(6)
        self.module_status_table.setHorizontalHeaderLabels(
            ["Name", "Version", "Status", "Source", "Access Scope", "Updated At"]
        )
        self.module_status_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.module_status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        requests_layout.addWidget(self.module_status_table)

        module_status_btns = QHBoxLayout()
        self.module_status_refresh_btn = QPushButton("🔄 Refresh")
        self.module_status_refresh_btn.clicked.connect(self.refresh_module_status)
        module_status_btns.addWidget(self.module_status_refresh_btn)
        requests_layout.addLayout(module_status_btns)

        # 2026-07-17: the "pending module build requests" table + Approve/
        # Deny buttons that used to live here are gone — nothing creates a
        # 'pending' request anymore. That path only ever existed for a
        # live classifier (systems/modules/system.py's classify_module_gap())
        # proposing builds through conversation; that classifier was
        # removed (it cost ~2s of LLM classification on every single turn,
        # for a feature that stopped mattering once building moved to
        # Claude authoring code directly). Claude-initiated requests now go
        # straight to 'approved' via tools/pending_builds.py's `propose`
        # command — same reasoning a creator's own confirmed request
        # always skipped 'pending' too. The "Recent build history" table
        # right below still shows everything, any status.
        #
        # 2026-07-16 (Craig: "a module build went under activity instead of
        # in the module tab") — a creator-confirmed build is auto-approved
        # immediately and never sits in 'pending' (see the Activity tab's
        # own comment below), so it never showed up anywhere on THIS tab at
        # all, only in the separate Activity tab. Rather than move it out
        # of Activity (which Craig separately asked to keep as its own
        # general cross-cutting log), this mirrors the same
        # fetch_recent_module_build_requests() data here too, so build
        # history is visible without leaving the Modules tab.
        requests_layout.addWidget(QLabel("Recent build history (any status):"))

        self.module_activity_table = QTableWidget()
        self.module_activity_table.setColumnCount(6)
        self.module_activity_table.setHorizontalHeaderLabels(
            ["ID", "Requested By", "Module", "Status", "Result", "Resolved At"]
        )
        self.module_activity_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.module_activity_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        requests_layout.addWidget(self.module_activity_table)

        self.requests_tab.setLayout(requests_layout)

        # 📊 ACTIVITY TAB (2026-07-16, kept as its own tab per Craig,
        # not folded into Modules) — separate from the pending-requests
        # table above because a creator-confirmed build never sits in
        # 'pending' at all (their "yes" IS the approval); this is the
        # only place to watch it go approved -> built/failed without
        # digging through logs, since builds now run in the background
        # instead of blocking chat.
        self.activity_tab = QWidget()
        activity_layout = QVBoxLayout()

        activity_layout.addWidget(QLabel("Recent activity (any status):"))

        self.activity_table = QTableWidget()
        self.activity_table.setColumnCount(9)
        self.activity_table.setHorizontalHeaderLabels(
            ["ID", "Requested By", "Module", "Status", "Result",
             "Resolved At", "Requested Access", "Access Approved", "Origin"]
        )
        self.activity_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.activity_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        activity_layout.addWidget(self.activity_table)

        activity_btns = QHBoxLayout()

        self.activity_refresh_btn = QPushButton("🔄 Refresh")
        self.activity_refresh_btn.clicked.connect(self.refresh_activity)
        activity_btns.addWidget(self.activity_refresh_btn)

        # A creator's own confirmed build is auto-approved immediately
        # (never sits in 'pending'), so it never appears in the Modules
        # tab's pending table and the Approve/Deny buttons there can
        # never reach it — there was previously no way to cancel one
        # before Claude picks it up.
        self.activity_cancel_btn = QPushButton("🚫 Cancel Selected (approved, not yet built)")
        self.activity_cancel_btn.clicked.connect(self.cancel_activity_request)
        activity_btns.addWidget(self.activity_cancel_btn)

        # Elevated access (2026-07-16 privilege-tier system) is a real,
        # separate approval from the build approval above — Claude flags
        # what a module actually needs via "Requested Access", and only
        # this grants it. Voice ("approve request N") has worked since
        # tonight's propose-then-confirm redesign, but there was still no
        # GUI equivalent at all — found live when Craig couldn't approve
        # request #18 any other way.
        self.activity_approve_access_btn = QPushButton("✅ Approve Elevated Access (Selected)")
        self.activity_approve_access_btn.clicked.connect(self.approve_activity_access)
        activity_btns.addWidget(self.activity_approve_access_btn)
        activity_layout.addLayout(activity_btns)

        # 2026-07-16 (Craig: "there is not web search activity under
        # activity") — db.fetch_recent_query_reports() already existed
        # ("Controller-facing visibility, mirrors
        # fetch_recent_module_build_requests" per its own docstring) but was
        # never actually wired into any Controller view — confirmed live,
        # zero references anywhere in this file before this. Same
        # any-status, newest-first pattern as the module activity table
        # above, just for systems/inquiry/system.py's query_reports instead.
        activity_layout.addWidget(QLabel("Recent web search activity (any status):"))

        self.search_activity_table = QTableWidget()
        self.search_activity_table.setColumnCount(7)
        self.search_activity_table.setHorizontalHeaderLabels(
            ["ID", "Requested By", "Query", "Status", "Created At",
             "Search Resolved At", "Retain Resolved At"]
        )
        self.search_activity_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.search_activity_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        activity_layout.addWidget(self.search_activity_table)

        # 2026-07-16 (Craig: "I want a controller way to approve the web
        # search retention") — several requests were found stuck in
        # 'pending_retain_approval' forever: the "waiting for yes/no"
        # state only ever lives in memory (systems/inquiry/system.py's
        # _pending), so once the process restarts (common tonight) or the
        # conversation just moves on before answering, there's no way back
        # into it through voice/chat at all. These two buttons resolve a
        # selected row directly by report ID instead, via the same
        # promote/decline logic the live "yes"/"no" path uses
        # (retain_report()/decline_report(), factored out for exactly
        # this reuse).
        search_activity_btns = QHBoxLayout()

        self.approve_retention_btn = QPushButton("✅ Approve Retention (Selected)")
        self.approve_retention_btn.clicked.connect(self.approve_search_retention)
        search_activity_btns.addWidget(self.approve_retention_btn)

        self.decline_retention_btn = QPushButton("❌ Decline Retention (Selected)")
        self.decline_retention_btn.clicked.connect(self.decline_search_retention)
        search_activity_btns.addWidget(self.decline_retention_btn)

        activity_layout.addLayout(search_activity_btns)

        self.activity_tab.setLayout(activity_layout)

        # 🦙 OLLAMA TAB — wrapped in a container (was just the bare
        # QTextEdit) so it can have a Clear Console button too, matching
        # the A.L.E.X. tab's (Craig, 2026-07-16 — asked for this one "at
        # some point" right after the A.L.E.X. tab's own button already
        # existed). self.ollama_log itself is unchanged — LogFileTailer
        # still appends to the same QTextEdit object regardless of what
        # widget now contains it.
        self.ollama_tab = QWidget()
        ollama_layout = QVBoxLayout()
        ollama_layout.setContentsMargins(0, 0, 0, 0)

        ollama_layout.addWidget(self.ollama_log)

        self.clear_ollama_console_btn = QPushButton("🧹 Clear Console")
        self.clear_ollama_console_btn.clicked.connect(self.ollama_log.clear)
        ollama_layout.addWidget(self.clear_ollama_console_btn)

        self.ollama_tab.setLayout(ollama_layout)

        # 🔔 NOTIFICATIONS TAB (2026-07-17, Craig: "her showing me what
        # she changed dismissed what she said prior... can we move that
        # to a notifications tab") — the security-event/personality-
        # change/proactive-fault-check briefings used to fire straight
        # into the live avatar chat at connect (ws/ws_handlers.py): each
        # one's own __START__/text/__END__ sequence pushed whatever real
        # conversational response had just been shown into "Previous
        # Messages" and replaced it with an administrative notice. That's
        # now surfaced here instead — same DB sources, acknowledged only
        # on a real click here rather than automatically at connect. The
        # curiosity-question briefing deliberately stayed in live chat
        # (it's framed as her own genuine conversational curiosity, not
        # an audit report, so Craig's complaint doesn't apply to it).
        self.notifications_tab = QWidget()
        notifications_layout = QVBoxLayout()

        notifications_layout.addWidget(QLabel("Unacknowledged security events (blocked build attempts):"))

        self.security_events_table = QTableWidget()
        self.security_events_table.setColumnCount(4)
        self.security_events_table.setHorizontalHeaderLabels(
            ["User", "Event Type", "Detail", "Created At"]
        )
        self.security_events_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.security_events_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        notifications_layout.addWidget(self.security_events_table)

        security_events_btns = QHBoxLayout()
        self.ack_security_events_btn = QPushButton("✅ Acknowledge All")
        self.ack_security_events_btn.clicked.connect(self.acknowledge_security_notifications)
        security_events_btns.addWidget(self.ack_security_events_btn)
        notifications_layout.addLayout(security_events_btns)

        notifications_layout.addWidget(QLabel("Unacknowledged personality changes (self-reflection):"))

        self.personality_changes_table = QTableWidget()
        self.personality_changes_table.setColumnCount(4)
        self.personality_changes_table.setHorizontalHeaderLabels(
            ["Kind", "New Value", "Reason", "Created At"]
        )
        self.personality_changes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.personality_changes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        notifications_layout.addWidget(self.personality_changes_table)

        personality_changes_btns = QHBoxLayout()
        self.ack_personality_changes_btn = QPushButton("✅ Acknowledge All")
        self.ack_personality_changes_btn.clicked.connect(self.acknowledge_personality_notifications)
        personality_changes_btns.addWidget(self.ack_personality_changes_btn)
        notifications_layout.addLayout(personality_changes_btns)

        # Not a stored/acknowledged queue like the two above — the fault
        # check is a live diagnostic_tool sweep, so this just re-runs it
        # on demand instead of showing stale history.
        notifications_layout.addWidget(QLabel("Proactive fault check (on demand — no longer runs automatically at connect):"))

        self.fault_check_output = QTextEdit()
        self.fault_check_output.setReadOnly(True)
        self.fault_check_output.setMaximumHeight(80)
        notifications_layout.addWidget(self.fault_check_output)

        fault_check_btns = QHBoxLayout()
        self.run_fault_check_btn = QPushButton("🩺 Run Diagnostic Check Now")
        self.run_fault_check_btn.clicked.connect(self.run_fault_check)
        fault_check_btns.addWidget(self.run_fault_check_btn)
        notifications_layout.addLayout(fault_check_btns)

        notifications_refresh_btns = QHBoxLayout()
        self.notifications_refresh_btn = QPushButton("🔄 Refresh")
        self.notifications_refresh_btn.clicked.connect(self.refresh_notifications)
        notifications_refresh_btns.addWidget(self.notifications_refresh_btn)
        notifications_layout.addLayout(notifications_refresh_btns)

        self.notifications_tab.setLayout(notifications_layout)

        self.tabs.addTab(self.alex_tab, "A.L.E.X.")
        self.tabs.addTab(self.requests_tab, "Modules")
        self.tabs.addTab(self.activity_tab, "Activity")
        self.tabs.addTab(self.notifications_tab, "Notifications")
        self.tabs.addTab(self.users_tab, "Users")
        self.tabs.addTab(self.ollama_tab, "Ollama")
        self.tabs.addTab(self.system_log, "System")
        self.tabs.addTab(self.db_tab, "Database")

        layout.addWidget(self.tabs)
        self.setLayout(layout)

        # ---------------- TIMERS ----------------
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(1000)

        # Separate, slower timer for the requests queue — no need to hit
        # the DB every second for something that changes rarely.
        self.requests_timer = QTimer()
        self.requests_timer.timeout.connect(self.refresh_requests)
        self.requests_timer.start(5000)

        # Same cadence as the requests queue above — notifications don't
        # need per-second polling either. The fault check is deliberately
        # NOT on this timer; it's a real diagnostic sweep, only run when
        # the button is clicked.
        self.notifications_timer = QTimer()
        self.notifications_timer.timeout.connect(self.refresh_notifications)
        self.notifications_timer.start(5000)

        # 2026-07-16: found live — orphaned runner processes only got
        # swept on an explicit Stop-Ollama click or the next Start, so
        # anything left over from an unclean exit (Controller crashed,
        # task-killed, closed without stopping Ollama first) sat there
        # silently eating GPU VRAM until someone happened to restart
        # things. Confirmed live: two such orphans alone left the card at
        # 12021/12288 MiB, and every generation request just hung
        # indefinitely with no error. Sweeping on a standing timer instead
        # of only at start/stop transitions means this can't quietly
        # reaccumulate during a long-running session either.
        self.orphan_sweep_timer = QTimer()
        self.orphan_sweep_timer.timeout.connect(self._cleanup_orphaned_ollama_runners)
        self.orphan_sweep_timer.start(60000)

        # ---------------- ALWAYS-ON LOG TAILING ----------------
        # Starts watching regardless of whether this Controller launched
        # ALEX — so it stays useful even when she's started some other way.
        self.start_log_tailing()

        self.load_db_tables()
        self.refresh_requests()
        self.refresh_notifications()

        # Silent unless it finds something — no need to nag on a clean start.
        self.check_for_orphans(prompt_if_none=False)

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

    def remove_selected_session(self):
        """Manual removal for a connection that's actually dead but never
        logged a real disconnect (client crash, network drop) — self.sessions
        has no way to detect that on its own, so this is a deliberate
        creator override, not something to infer automatically."""
        rows = sorted({idx.row() for idx in self.user_table.selectionModel().selectedRows()})

        if not rows:
            return

        for r in rows:
            id_item = self.user_table.item(r, 0)
            if not id_item:
                continue

            sid = id_item.text()
            if self.sessions.pop(sid, None) is not None:
                self.alex_log.append(f"[SYSTEM] Removed stale session {sid} via Controller")

        self.conn_label.setText(f"Connections: {len(self.sessions)}")
        self.update_user_table()

    # ---------------- LOG ROUTING ----------------
    def log(self, text):
        text = text.strip()

        # 👥 Detect session connect — tracked for the "Connections: N"
        # label and Users tab, but not echoed into the A.L.E.X. tab: that
        # count/table already shows this, so the raw line was redundant
        # (and got noisy fast during heavy restart/test cycles).
        if "WS connected:" in text:
            match = re.search(r"WS connected:\s*([a-f0-9\-]+)", text)
            if match:
                sid = match.group(1)
                self.sessions[sid] = time.time()

            return

        # 👥 Detect disconnect
        if "WS disconnected:" in text:
            match = re.search(r"WS disconnected:\s*([a-f0-9\-]+)", text)
            if match:
                sid = match.group(1)
                self.sessions.pop(sid, None)

            return

        # 👥 HTTP / WS activity
        if "WebSocket /ws" in text or '"GET /' in text:
            return  # ignore spam, already tracked by session

        # 👥 Same redundant info as "WS connected:" above, just phrased as
        # a debug message the browser also sees (ws_handlers.py's
        # send_debug() call on connect) — already covered by the
        # Connections count/Users tab, so drop it here too rather than
        # showing the same connect event twice in different wording.
        if "[DEBUG] 🟢 Connected:" in text:
            return

        # 👥 New log file attached means ALEX's process (re)started — any
        # sessions we were tracking against the old process are necessarily
        # gone, since a fresh process can't have inherited live websockets.
        if text.startswith("[SYSTEM] Attached to log:") and self.sessions:
            self.sessions.clear()

        # ---------------- NORMAL ROUTING ----------------
        if "[Ollama]" in text:
            self.ollama_log.append(text)
        elif "[ALEX]" in text:
            # 2026-07-16 (Craig: "things are getting pretty busy in
            # there") — mechanical per-utterance chatter (recording
            # start, failed transcriptions) has zero information value
            # once the mic pipeline is known to be working, and it fires
            # on every single utterance — the actual signal (what was
            # heard, what she did, what changed) was getting buried in
            # it. Filtered here, not at the source: the real log FILE
            # still has everything for actual debugging, this only trims
            # what floods this one tab.
            noisy = (
                "Captured" in text and "bytes" in text
                or "Couldn't make out any words" in text
            )
            if not noisy:
                self.alex_log.append(text)
        else:
            self.system_log.append(text)

    # ---------------- COPY ----------------
    def copy_logs(self):
        current = self.tabs.currentWidget()

        # A.L.E.X./Ollama tabs are wrapper QWidgets (log + buttons), not
        # the QTextEdit directly — copy their log content specifically.
        if current is self.alex_tab:
            current = self.alex_log
        elif current is self.ollama_tab:
            current = self.ollama_log

        if isinstance(current, QTextEdit):
            QGuiApplication.clipboard().setText(current.toPlainText())
            self.system_log.append("Copied logs")

    # ---------------- SHUTDOWN ----------------
    def closeEvent(self, event):
        """2026-07-16: found live — LogFileTailer.stop() already existed
        (just sets self._running = False so its run() loop exits on its
        own next poll) but nothing ever called it. Closing the window
        without stopping these first means Qt destroys a QThread object
        while its thread is still actually running underneath — undefined
        behavior, surfaces as "QThread: Destroyed while thread '' is
        still running" and can abort the process instead of exiting
        cleanly. .wait() blocks briefly (at most one poll_interval, 0.5s)
        for each thread to actually finish before letting the window
        close for real."""
        for tailer in (self.alex_tailer, self.ollama_tailer):
            if tailer:
                tailer.stop()
                tailer.wait(2000)
        super().closeEvent(event)

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

    def apply_personality_override(self):
        instruction = self.personality_override_input.text().strip()
        if not instruction:
            return

        try:
            current = asyncio.run(get_personality())
            merged = asyncio.run(merge_personality_change(current, instruction))

            asyncio.run(set_personality(merged))

            # Same hard-rule storage the chat path uses (verbatim,
            # never LLM-touched) so this stays enforced even if the
            # flowing description above drifts on a later merge.
            asyncio.run(add_personality_hard_rule(instruction))

            asyncio.run(log_personality_change(merged, "creator override via Controller", kind="personality"))

            self.alex_log.append(f"[SYSTEM] Personality updated via Controller: {merged}")
            self.personality_override_input.clear()
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to apply personality override: {e}")

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

    # ---------------- MODULE BUILD REQUESTS ----------------
    def refresh_module_status(self):
        try:
            modules = asyncio.run(list_module_registry())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load module status: {e}")
            return

        self.module_status_table.setRowCount(len(modules))

        for row, m in enumerate(modules):
            values = [
                m["name"], m["version"], m["status"], m["source"] or "",
                m.get("access_scope") or "", m["updated_at"]
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.module_status_table.setItem(row, col, item)

    def refresh_requests(self):
        self.refresh_module_status()
        self.refresh_activity()

    def refresh_module_activity(self):
        """Mirrors the same fetch_recent_module_build_requests() data the
        Activity tab uses, shown on the Modules tab too (Craig, 2026-07-16:
        "a module build went under activity instead of in the module
        tab") — a creator-confirmed build skips 'pending' entirely, so it
        never appeared in this tab's own requests_table at all."""
        try:
            requests = asyncio.run(fetch_recent_module_build_requests())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load module build history: {e}")
            return

        self.module_activity_table.setRowCount(len(requests))

        for row, r in enumerate(requests):
            values = [r["id"], r["requested_by"], r["module_name"], r["status"],
                      r["result"] or "", r["resolved_at"] or ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.module_activity_table.setItem(row, col, item)

    def refresh_search_activity(self):
        """db.fetch_recent_query_reports() — existed since inquiry was
        built but was never wired into any Controller view (Craig,
        2026-07-16: "there is not web search activity under activity")."""
        try:
            reports = asyncio.run(fetch_recent_query_reports())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load search activity: {e}")
            return

        self.search_activity_table.setRowCount(len(reports))

        for row, r in enumerate(reports):
            values = [
                r["id"], r["requested_by"], r["query"], r["status"],
                r["created_at"], r.get("search_resolved_at") or "",
                r.get("retain_resolved_at") or ""
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.search_activity_table.setItem(row, col, item)

    def refresh_activity(self):
        self.refresh_module_activity()
        self.refresh_search_activity()

        try:
            requests = asyncio.run(fetch_recent_module_build_requests())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load build activity: {e}")
            return

        self.activity_table.setRowCount(len(requests))

        for row, r in enumerate(requests):
            origin = r.get("origin") or "live_conversation"
            origin_label = "Claude (session)" if origin == "claude_session" else "Her (live conversation)"

            # "pending" must also check status — a denied/built request
            # with access_approved still 0 isn't awaiting a decision
            # anymore, it's finished. Missing that check (found live,
            # 2026-07-16) mislabeled denied requests #17/#20 as
            # perpetually "pending", and approve_activity_access() keys
            # off this exact label to decide what it's allowed to act on.
            if not r.get("requested_access"):
                access_label = ""
            elif r.get("access_approved"):
                access_label = "yes"
            elif r["status"] == "approved":
                access_label = "pending"
            else:
                access_label = "—"

            values = [
                r["id"], r["requested_by"], r["module_name"], r["status"],
                r["result"] or "", r["resolved_at"] or "",
                r.get("requested_access") or "",
                access_label,
                origin_label
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.activity_table.setItem(row, col, item)

    def cancel_activity_request(self):
        """Cancels an already-approved build request before Claude has
        built it — the gap the Approve/Deny buttons above can't reach,
        since a creator's own confirmed request skips 'pending' entirely
        (see comment above activity_table). Only acts on rows still
        'approved'; refuses anything already built/failed/denied so this
        can't silently rewrite a completed build's history."""
        rows = sorted({idx.row() for idx in self.activity_table.selectionModel().selectedRows()})

        if not rows:
            return

        for r in rows:
            id_item = self.activity_table.item(r, 0)
            module_item = self.activity_table.item(r, 2)
            status_item = self.activity_table.item(r, 3)
            if not id_item or not status_item:
                continue

            if status_item.text() != "approved":
                self.alex_log.append(
                    f"⚠️ Request #{id_item.text()} is '{status_item.text()}', not 'approved' — skipped."
                )
                continue

            request_id = int(id_item.text())
            module_name = module_item.text() if module_item else "?"

            try:
                asyncio.run(resolve_module_build_request(request_id, "denied"))
                self.alex_log.append(
                    f"[SYSTEM] Build request #{request_id} ('{module_name}') canceled via Controller"
                )
            except Exception as e:
                self.alex_log.append(f"⚠️ Failed to cancel request #{request_id}: {e}")

        self.refresh_requests()

    def approve_activity_access(self):
        """Grants elevated access for a module Claude has flagged as
        needing real access beyond the plain sandbox — the second,
        separate approval from the build approval above (see
        SELF_MODIFICATION_ARCHITECTURE.md's privilege-tier system). Only
        acts on rows whose 'Access Approved' column reads 'pending'
        (has a real requested_access, not yet granted) — refuses rows
        with nothing requested, or already approved, so this can't
        double-grant or act on an unrelated row by accident."""
        rows = sorted({idx.row() for idx in self.activity_table.selectionModel().selectedRows()})

        if not rows:
            return

        for r in rows:
            id_item = self.activity_table.item(r, 0)
            module_item = self.activity_table.item(r, 2)
            access_item = self.activity_table.item(r, 6)
            approved_item = self.activity_table.item(r, 7)
            if not id_item or not approved_item:
                continue

            if approved_item.text() != "pending":
                self.alex_log.append(
                    f"⚠️ Request #{id_item.text()} access status is '{approved_item.text()}', not 'pending' — skipped."
                )
                continue

            request_id = int(id_item.text())
            module_name = module_item.text() if module_item else "?"
            access_desc = access_item.text() if access_item else ""

            try:
                asyncio.run(approve_elevated_access(request_id))
                self.alex_log.append(
                    f"[SYSTEM] Elevated access approved for request #{request_id} ('{module_name}': {access_desc}) via Controller"
                )
            except Exception as e:
                self.alex_log.append(f"⚠️ Failed to approve access for request #{request_id}: {e}")

        self.refresh_requests()

    def approve_search_retention(self):
        """Resolves a selected search_activity_table row's retain approval
        directly by report ID (see search_activity_btns' comment above for
        why this exists — several requests were found stuck in
        'pending_retain_approval' forever with no way back through
        conversation). Only acts on rows whose Status column actually
        reads 'pending_retain_approval', so this can't double-promote an
        already-resolved row or act on an unrelated one by accident.

        Imports retain_report() locally, not at module level — found live
        (2026-07-16) that importing it at the top of this file dragged in
        sentence_transformers -> sklearn (for embedding search results)
        unconditionally on EVERY Controller launch, over a UNC network
        path, just to have this rarely-used button available. Paying that
        cost only the first time this button is actually clicked is a much
        better trade than paying it on every single startup."""
        from systems.inquiry.system import retain_report

        rows = sorted({idx.row() for idx in self.search_activity_table.selectionModel().selectedRows()})
        if not rows:
            return

        for r in rows:
            id_item = self.search_activity_table.item(r, 0)
            status_item = self.search_activity_table.item(r, 3)
            if not id_item or not status_item:
                continue

            if status_item.text() != "pending_retain_approval":
                self.alex_log.append(
                    f"⚠️ Request #{id_item.text()} status is '{status_item.text()}', not 'pending_retain_approval' — skipped."
                )
                continue

            report_id = int(id_item.text())

            try:
                kid, supersedes = asyncio.run(retain_report(report_id))
                if kid is None:
                    self.alex_log.append(f"⚠️ Request #{report_id} no longer exists — skipped.")
                    continue
                note = " (superseded a prior belief)" if supersedes else ""
                self.alex_log.append(f"[SYSTEM] Retained knowledge #{kid} from request #{report_id} via Controller{note}")
            except Exception as e:
                self.alex_log.append(f"⚠️ Failed to retain request #{report_id}: {e}")

        self.refresh_search_activity()

    def decline_search_retention(self):
        # Also a local import — decline_report() doesn't itself need
        # embed(), but importing it from the same module as retain_report()
        # would still trigger the same heavy transitive import chain. See
        # approve_search_retention()'s docstring above for the reasoning.
        from systems.inquiry.system import decline_report

        rows = sorted({idx.row() for idx in self.search_activity_table.selectionModel().selectedRows()})
        if not rows:
            return

        for r in rows:
            id_item = self.search_activity_table.item(r, 0)
            status_item = self.search_activity_table.item(r, 3)
            if not id_item or not status_item:
                continue

            if status_item.text() != "pending_retain_approval":
                self.alex_log.append(
                    f"⚠️ Request #{id_item.text()} status is '{status_item.text()}', not 'pending_retain_approval' — skipped."
                )
                continue

            report_id = int(id_item.text())

            try:
                ok = asyncio.run(decline_report(report_id))
                if not ok:
                    self.alex_log.append(f"⚠️ Request #{report_id} no longer exists — skipped.")
                    continue
                self.alex_log.append(f"[SYSTEM] Retention declined for request #{report_id} via Controller")
            except Exception as e:
                self.alex_log.append(f"⚠️ Failed to decline request #{report_id}: {e}")

        self.refresh_search_activity()

    def refresh_notifications(self):
        """Populates the Notifications tab's two tables — the exact same
        data ws/ws_handlers.py's connect-time briefings used to read and
        push straight into the live avatar chat (2026-07-17 move, see the
        comment above the Notifications tab's construction)."""
        try:
            events = asyncio.run(fetch_unacknowledged_security_events())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load security events: {e}")
            events = []

        self.security_events_table.setRowCount(len(events))
        for row, ev in enumerate(events):
            values = [ev["user"], ev["event_type"], ev["detail"], ev["created_at"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.security_events_table.setItem(row, col, item)

        try:
            changes = asyncio.run(fetch_unacknowledged_personality_changes())
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to load personality changes: {e}")
            changes = []

        self.personality_changes_table.setRowCount(len(changes))
        for row, c in enumerate(changes):
            values = [c["kind"], c["new_value"], c["reason"], c["created_at"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.personality_changes_table.setItem(row, col, item)

    def acknowledge_security_notifications(self):
        try:
            asyncio.run(acknowledge_security_events())
            self.alex_log.append("[SYSTEM] Security events acknowledged via Controller")
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to acknowledge security events: {e}")
        self.refresh_notifications()

    def acknowledge_personality_notifications(self):
        try:
            asyncio.run(acknowledge_personality_changes())
            self.alex_log.append("[SYSTEM] Personality changes acknowledged via Controller")
        except Exception as e:
            self.alex_log.append(f"⚠️ Failed to acknowledge personality changes: {e}")
        self.refresh_notifications()

    def run_fault_check(self):
        """Re-runs the same diagnostic_tool sweep that used to fire
        automatically at every creator connect. Local imports for the
        same reason approve_search_retention() above uses one — the
        module-runtime machinery isn't needed anywhere else in this file,
        so there's no reason to pay its import cost on every launch just
        for a button that's clicked on demand."""
        from module_runtime.module_loader import load_module
        from module_runtime.module_executor import run_module

        self.fault_check_output.setPlainText("Running diagnostic check...")

        async def _run():
            module = await load_module("diagnostic_tool")
            text, _ = await run_module(module, "run a diagnostic check", {}, "creator")
            return text

        try:
            result = asyncio.run(_run())
            self.fault_check_output.setPlainText(result or "No issues found.")
        except Exception as e:
            self.fault_check_output.setPlainText(f"⚠️ Diagnostic check failed: {e}")

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

        # 2026-07-16: found live — real, active outage, not a hypothetical.
        # Two "runner" processes from an earlier crash cycle survived an
        # unclean shutdown (Controller closed without going through Stop
        # Ollama, so stop_ollama()'s own cleanup call below never ran) and
        # sat there holding GPU VRAM. By the next restart, that plus two
        # freshly-loaded models left the card at 12021/12288 MiB — nearly
        # full — and every generation request hung indefinitely (0% GPU
        # utilization, no error, no timeout, just stuck) instead of
        # completing or failing cleanly. Confirmed directly: killing those
        # two PIDs dropped VRAM to 5059 MiB and a test call that had been
        # hanging for 90+ seconds completed in 1.6s immediately after.
        # Running this check here too (not just in stop_ollama()) catches
        # orphans left over from ANY unclean prior exit before they can
        # starve the fresh instance about to launch.
        self._cleanup_orphaned_ollama_runners()

        # Redirect to the same stable file the tailer watches, rather than
        # a PIPE — this way Ollama's output is visible the same way no
        # matter who launches the process, and an unread PIPE can't fill up
        # and block it.
        self.ollama_log_file = open(OLLAMA_LOG_PATH, "ab")

        # Chat (qwen2.5:7b) and module builds (deepseek-coder:6.7b) use
        # different models. Ollama's default keeps only one model resident
        # and evicts/reloads on every switch — confirmed live via
        # /api/ps: a build in progress held deepseek in VRAM while a chat
        # message sat waiting ~30-50s for qwen to reload. The GPU has
        # room for both (~4.3GB each on a 12GB card), so let Ollama keep
        # both loaded instead of swapping.
        ollama_env = os.environ.copy()
        ollama_env.setdefault("OLLAMA_MAX_LOADED_MODELS", "2")

        # 2026-07-16: found live — "CUDA error: the launch timed out and
        # was terminated" (a Windows GPU-driver watchdog killing a CUDA
        # kernel that didn't return in time), crashing the llama runner
        # mid-generation and returning an empty response. Happened twice
        # earlier the same night too, well before any of tonight's other
        # changes, so this isn't caused by app code — it's the GPU itself.
        # The model load log showed "Flash Attention was auto, set to
        # enabled" — FA's CUDA kernels are tuned for newer GPUs, and this
        # Titan X (2015, compute capability 5.2) is well outside where
        # that path is normally validated, making it a plausible trigger
        # for exactly this kind of stall-and-kill. Disabling it here is a
        # real test, not a confirmed fix — costs some generation speed if
        # FA wasn't actually the trigger, worth it if it stops the crashes.
        ollama_env.setdefault("OLLAMA_FLASH_ATTENTION", "0")

        self.ollama_proc = subprocess.Popen(
            [OLLAMA_EXE_PATH, "serve"],
            stdout=self.ollama_log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=ollama_env
        )

        self.update_status()

    def _cleanup_orphaned_ollama_runners(self):
        """Windows doesn't kill child processes when a parent dies — every
        "ollama serve" that stops (this button, a crash, or the tray app's
        own respawn cycle before tonight's fix) can leave its "ollama
        runner" child behind, still holding a full model resident in
        VRAM. Confirmed live (2026-07-16, Craig: "we keep getting orphans
        and I'm not sure why") — found a live runner process whose parent
        PID didn't exist at all anymore. Sweeps for ANY runner whose
        parent isn't a currently-running ollama.exe, not just ones this
        specific Stop click just orphaned, so it also catches leftovers
        from earlier crashes/restarts tonight."""
        live_ollama_pids = set()
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                if (proc.info["name"] or "").lower() == "ollama.exe":
                    live_ollama_pids.add(proc.info["pid"])
        except Exception:
            pass

        for proc in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != "ollama.exe":
                    continue
                if "runner" not in (proc.info.get("cmdline") or []):
                    continue
                if proc.info["ppid"] not in live_ollama_pids:
                    proc.kill()
                    self.log(f"[SYSTEM] Cleaned up orphaned Ollama runner (PID {proc.info['pid']}, parent {proc.info['ppid']} no longer exists)")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

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

        # "ollama app.exe" is a separate Windows tray supervisor (launched
        # at login, independent of anything ALEX/the Controller starts) —
        # confirmed live (2026-07-16, Craig: "stop ollama in the controller
        # doesn't seem to work, it just restarts") that it respawns
        # "ollama serve" within ~2 seconds of it dying, making Stop look
        # broken when it was actually working correctly on the process it
        # knew about. Also terminating the tray app here is what makes
        # Stop actually stick — accepted tradeoff: its systray icon closes,
        # and Ollama won't auto-launch again at the next Windows login
        # until it's relaunched manually or the machine reboots.
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").lower() == "ollama app.exe":
                    proc.terminate()
                    self.log(f"[SYSTEM] Stopped Ollama's tray supervisor (PID {proc.info['pid']}) so it can't respawn the server")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                self.log(f"[SYSTEM] Failed to stop Ollama tray supervisor: {e}")

        # Give the just-terminated serve process a moment to actually exit
        # before checking which runners are now truly parentless.
        time.sleep(0.5)
        self._cleanup_orphaned_ollama_runners()

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

    # ---------------- ORPHAN PROCESS CHECK ----------------
    def check_for_orphans(self, prompt_if_none=False):
        orphans = find_orphan_processes()

        if not orphans:
            if prompt_if_none:
                QMessageBox.information(self, "No Orphans Found", "No orphaned Ollama/A.L.E.X. processes found.")
            return

        lines = "\n".join(f"- {label} (PID {pid})" for label, pid in orphans)

        confirm = QMessageBox.question(
            self, "Orphaned Processes Found",
            f"Found {len(orphans)} orphaned process(es) not serving their expected port "
            f"(leftover from a restart that wasn't cleanly stopped):\n\n{lines}\n\n"
            f"Terminate them now? Each one can be holding a full model copy in VRAM.\n\n"
            f"Caution: in one observed case, terminating a confirmed orphan A.L.E.X. "
            f"process also took down the real active server, cause unclear — be ready "
            f"to restart A.L.E.X. afterward if that happens.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )

        if confirm != QMessageBox.Yes:
            return

        for label, pid in orphans:
            try:
                psutil.Process(pid).terminate()
                self.log(f"[SYSTEM] Terminated orphaned {label} process (PID {pid})")
            except Exception as e:
                self.log(f"[SYSTEM] Failed to terminate orphaned {label} process (PID {pid}): {e}")


# -----------------------------
# 🏁 RUN
# -----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AlexController()
    window.show()
    sys.exit(app.exec())
