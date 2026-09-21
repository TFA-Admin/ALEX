# controller/views/commands.py
"""Commands — COMMANDS.md, rendered. 2026-09-21 (Craig: "I won't remember
them all... I need some kind of reference"). The file is the one source
(tools/commands_drift.py keeps it honest against the code); this only
shows it. Also at /commands on her page."""
import os

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit

from controller.common import ALEX_DIR


class CommandsView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(QLabel(
            "Every fixed phrase she listens for, who may say it, and what "
            "gate applies. From COMMANDS.md."))
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        layout.addWidget(self.view)
        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        btns.addWidget(self.refresh_btn)
        btns.addStretch(1)
        layout.addLayout(btns)
        self.setLayout(layout)

    def refresh(self):
        """Qt's own markdown renderer handles the headings and tables it uses."""
        try:
            with open(os.path.join(ALEX_DIR, "COMMANDS.md"), encoding="utf-8") as fh:
                self.view.setMarkdown(fh.read())
        except Exception as e:
            self.view.setPlainText(f"Could not read COMMANDS.md: {e}")
