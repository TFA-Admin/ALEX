# controller/app.py
"""The window: status strip, six views, timers, and the log router that
feeds the Run consoles. See controller/__init__.py for the shape and why."""
import sys
import subprocess

import psutil

from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget

from PySide6.QtCore import QTimer

from controller.common import LOG_DIR, LogFileTailer
from controller.procs import ProcessManager
from controller.views.run import RunView
from controller.views.inbox import InboxView
from controller.views.her import HerView
from controller.views.people import PeopleView
from controller.views.data import DataView
from controller.views.commands import CommandsView


class AlexController(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("A.L.E.X Controller")
        self.resize(1150, 720)

        # Always-on log tailers — see activity regardless of who started the process
        self.alex_tailer = None
        self.ollama_tailer = None

        layout = QVBoxLayout()

        # ---------------- STATUS STRIP ----------------
        # Always visible, above the views: is she up, is Ollama up, what
        # the machine is doing, how many people are talking to her.
        strip = QHBoxLayout()
        self.status_label = QLabel("Status: 🔴 Idle")
        self.cpu_label = QLabel("CPU: --%")
        self.ram_label = QLabel("RAM: --%")
        self.gpu_label = QLabel("GPU: --%")
        self.vram_label = QLabel("VRAM: -- MB")
        self.conn_label = QLabel("Talking to her: 0")
        strip.addWidget(self.status_label)
        strip.addStretch(1)
        for w in [self.cpu_label, self.ram_label, self.gpu_label, self.vram_label, self.conn_label]:
            strip.addWidget(w)
        layout.addLayout(strip)

        # ---------------- VIEWS ----------------
        self.procs = ProcessManager(log=self.log, selected_model=self._selected_model, parent=self)

        self.run = RunView(self.procs, log=self.log)
        self.run.status_changed = self.update_status
        note = self.run.note

        self.inbox = InboxView(note)
        self.her = HerView(note)
        self.people = PeopleView(note)
        self.data = DataView(note)
        self.commands = CommandsView()

        self.inbox.changed = self._inbox_changed
        self.her.changed = self._inbox_changed

        self.tabs = QTabWidget()
        self.tabs.addTab(self.run, "Run")
        self.tabs.addTab(self.inbox, "Inbox")
        self.tabs.addTab(self.her, "Her")
        self.tabs.addTab(self.people, "People")
        self.tabs.addTab(self.data, "Data")
        self.tabs.addTab(self.commands, "Commands")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)
        self.setLayout(layout)

        # ---------------- TIMERS ----------------
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(1000)

        # Separate, slower timer for what changes rarely — no need to hit
        # the DB every second. The Inbox and People refresh on it; Her
        # refreshes when its tab is opened or its Refresh is clicked.
        self.slow_timer = QTimer()
        self.slow_timer.timeout.connect(self.refresh_slow)
        self.slow_timer.start(5000)

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
        self.orphan_sweep_timer.timeout.connect(self.procs.cleanup_orphaned_ollama_runners)
        self.orphan_sweep_timer.start(60000)

        # ---------------- ALWAYS-ON LOG TAILING ----------------
        # Starts watching regardless of whether this Controller launched
        # ALEX — so it stays useful even when she's started some other way.
        self.start_log_tailing()

        self.data.load_db_tables()
        self.commands.refresh()
        self.her.refresh_all()
        self.refresh_slow()
        self.update_status()

        # Silent unless it finds something — no need to nag on a clean start.
        self.procs.check_for_orphans(prompt_if_none=False)

    def _selected_model(self) -> str:
        return self.run.selected_model()

    # ---------------- STATUS ----------------
    def update_status(self):
        ollama_up = self.procs.ollama_up()
        alex_up = self.procs.alex_up()

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
                 "--format=csv,noheader,nounits"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).decode().strip()

            gpu, vram = result.split(",")
            self.gpu_label.setText(f"GPU: {gpu.strip()}%")
            self.vram_label.setText(f"VRAM: {vram.strip()} MB")
        except Exception:
            self.gpu_label.setText("GPU: N/A")
            self.vram_label.setText("VRAM: N/A")

        self.update_status()

    def refresh_slow(self):
        self.inbox.refresh()
        self._inbox_changed()
        self.people.refresh()
        self.conn_label.setText(f"Talking to her: {self.people.live_count()}")

    def _inbox_changed(self):
        n = self.inbox.count()
        self.tabs.setTabText(self.tabs.indexOf(self.inbox), f"Inbox ({n})" if n else "Inbox")

    def _tab_changed(self, index: int):
        widget = self.tabs.widget(index)
        if widget is self.her:
            self.her.refresh_all()
        elif widget is self.inbox:
            self.inbox.refresh()
            self._inbox_changed()
        elif widget is self.people:
            self.people.refresh()

    # ---------------- LOG ROUTING ----------------
    def log(self, text):
        text = text.strip()

        # 👥 Session connect/disconnect lines are not echoed into the
        # A.L.E.X. console: the People view shows who is connected, by
        # name, from her database (2026-09-21), and the raw line was
        # redundant (and got noisy fast during heavy restart/test cycles).
        if "WS connected:" in text or "WS disconnected:" in text:
            return

        # 👥 HTTP / WS activity
        if "WebSocket /ws" in text or '"GET /' in text:
            return  # ignore spam, already tracked by session

        # 👥 Same redundant info as "WS connected:" above, just phrased as
        # a debug message the browser also sees (ws_handlers.py's
        # send_debug() call on connect) — already covered by People, so
        # drop it here too rather than showing the same connect event
        # twice in different wording.
        if "[DEBUG] 🟢 Connected:" in text:
            return

        # ---------------- NORMAL ROUTING ----------------
        if "[Ollama]" in text:
            self.run.append(self.run.ollama_console, text)
        elif "[ALEX]" in text:
            # 2026-07-16 (Craig: "things are getting pretty busy in
            # there") — mechanical per-utterance chatter (recording
            # start, failed transcriptions) has zero information value
            # once the mic pipeline is known to be working, and it fires
            # on every single utterance — the actual signal (what was
            # heard, what she did, what changed) was getting buried in
            # it. Filtered here, not at the source: the real log FILE
            # still has everything for actual debugging, this only trims
            # what floods this one console.
            noisy = (
                "Captured" in text and "bytes" in text
                or "Couldn't make out any words" in text
            )
            if not noisy:
                self.run.append(self.run.alex_console, text)
        else:
            self.run.append(self.run.system_console, text)

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


def main():
    app = QApplication(sys.argv)
    window = AlexController()
    window.show()
    sys.exit(app.exec())
