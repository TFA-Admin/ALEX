# controller/views/run.py
"""Run — start, stop, restart, her model, Ollama, and the three live
consoles. The buttons act through ProcessManager; the consoles are fed by
the app's log router (AlexController.log)."""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QTextEdit, QComboBox,
)
from PySide6.QtGui import QGuiApplication, QTextCursor

from controller.common import (
    load_controller_settings, save_controller_settings, installed_ollama_models,
    DEFAULT_ALEX_MODEL,
)


class RunView(QWidget):
    # 2026-07-18 (Craig: "the ollama tab lags before it lets me in") —
    # even after fixing LogFileTailer's replay-on-attach burst, these
    # widgets grow completely unbounded for as long as ALEX/Ollama keep
    # running (confirmed live: 50,000+ lines in one session for Ollama
    # alone) — a QTextEdit's rendering/layout cost scales with document
    # size, so an ever-growing one gets slower to display over a long
    # session regardless of the initial-burst fix. Caps each widget to
    # the most recent MAX_LOG_LINES, trimming from the start — the real
    # log FILE on disk is untouched, this only bounds what's kept in the
    # live in-memory widget.
    MAX_LOG_LINES = 2000

    def __init__(self, procs, log):
        super().__init__()
        self.procs = procs
        self.log = log

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)

        # ---------------- BUTTONS ----------------
        btns = QHBoxLayout()

        self.start_alex_btn = QPushButton("▶ Start A.L.E.X")
        self.start_alex_btn.clicked.connect(self._start_alex)

        self.stop_alex_btn = QPushButton("⏹ Stop A.L.E.X")
        self.stop_alex_btn.clicked.connect(self._stop_alex)

        self.restart_alex_btn = QPushButton("🔄 Restart A.L.E.X")
        self.restart_alex_btn.setToolTip(
            "Restarts her server process only — Ollama and this Controller stay up. "
            "Picks up every code change; the fastest way to test one.")
        self.restart_alex_btn.clicked.connect(self._restart_alex)

        self.start_ollama_btn = QPushButton("▶ Start Ollama")
        self.start_ollama_btn.clicked.connect(self._start_ollama)

        self.stop_ollama_btn = QPushButton("⏹ Stop Ollama")
        self.stop_ollama_btn.clicked.connect(self._stop_ollama)

        self.check_orphans_btn = QPushButton("🧹 Check for Orphans")
        self.check_orphans_btn.clicked.connect(lambda: self.procs.check_for_orphans(prompt_if_none=True))

        for b in [self.start_alex_btn, self.stop_alex_btn, self.restart_alex_btn,
                  self.start_ollama_btn, self.stop_ollama_btn, self.check_orphans_btn]:
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        # ---------------- MODEL ----------------
        # 2026-09-21: her model, chosen here. Takes effect on her next
        # start or restart (the value goes into her process environment as
        # ALEX_LLM_MODEL). Editable, so a tag not yet on disk can be typed.
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Her model (applies on next start/restart):"))
        self.model_selector = QComboBox()
        self.model_selector.setEditable(True)
        installed = installed_ollama_models()
        current = load_controller_settings().get("alex_llm_model", DEFAULT_ALEX_MODEL)
        for name in installed:
            self.model_selector.addItem(name)
        if current not in installed:
            self.model_selector.addItem(current)
        self.model_selector.setCurrentText(current)
        self.model_selector.currentTextChanged.connect(self._model_choice_changed)
        model_row.addWidget(self.model_selector)
        model_row.addStretch(1)
        layout.addLayout(model_row)

        # ---------------- CONSOLES ----------------
        self.consoles = QTabWidget()
        self.alex_console = QTextEdit()
        self.ollama_console = QTextEdit()
        self.system_console = QTextEdit()
        for name, widget in (("A.L.E.X.", self.alex_console),
                             ("Ollama", self.ollama_console),
                             ("System", self.system_console)):
            widget.setReadOnly(True)
            self.consoles.addTab(widget, name)
        layout.addWidget(self.consoles)

        console_btns = QHBoxLayout()
        self.copy_btn = QPushButton("📋 Copy Console")
        self.copy_btn.clicked.connect(self.copy_console)
        console_btns.addWidget(self.copy_btn)
        self.clear_btn = QPushButton("🧹 Clear Console")
        self.clear_btn.clicked.connect(lambda: self.consoles.currentWidget().clear())
        console_btns.addWidget(self.clear_btn)
        console_btns.addStretch(1)
        layout.addLayout(console_btns)

        self.setLayout(layout)

    # ---------------- PROCESS BUTTONS ----------------
    # Each returns through the app's status refresh, which is why they go
    # through these thin wrappers rather than straight to ProcessManager.
    def _start_alex(self):
        self.procs.start_alex()
        self.status_changed()

    def _stop_alex(self):
        self.procs.stop_alex()
        self.status_changed()

    def _restart_alex(self):
        self.procs.restart_alex()
        self.status_changed()

    def _start_ollama(self):
        self.procs.start_ollama()
        self.status_changed()

    def _stop_ollama(self):
        self.procs.stop_ollama()
        self.status_changed()

    def status_changed(self):
        """Replaced by the app with its own status refresh."""

    # ---------------- MODEL ----------------
    def selected_model(self) -> str:
        text = self.model_selector.currentText().strip()
        return text or load_controller_settings().get("alex_llm_model", DEFAULT_ALEX_MODEL)

    def _model_choice_changed(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        settings = load_controller_settings()
        settings["alex_llm_model"] = text
        save_controller_settings(settings)
        self.log(f"[SYSTEM] Her model set to {text} — restart her to apply")

    # ---------------- CONSOLES ----------------
    def note(self, text: str):
        """A Controller action, into the A.L.E.X. console (where every
        view's "[SYSTEM] ..." line has always gone)."""
        self.append(self.alex_console, text)

    def append(self, widget, text):
        widget.append(text)
        doc = widget.document()
        excess = doc.blockCount() - self.MAX_LOG_LINES
        if excess > 0:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor, excess)
            cursor.removeSelectedText()

        # 2026-07-18 (Craig: "all I see is you talking... not what she
        # responds with") — confirmed live her replies WERE being logged
        # and routed correctly (checked the raw log file directly), just
        # not visible: nothing here ever scrolled the view, so if it had
        # scrolled away from the bottom for any reason, new lines kept
        # arriving off-screen with nothing pulling the view down to show
        # them. A live log tailer should always show the newest line,
        # same expectation as `tail -f`.
        scrollbar = widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def copy_console(self):
        current = self.consoles.currentWidget()
        if isinstance(current, QTextEdit):
            QGuiApplication.clipboard().setText(current.toPlainText())
            self.append(self.system_console, "Copied console")
