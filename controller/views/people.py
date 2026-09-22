# controller/views/people.py
"""People — who is talking to her right now, by name, and everyone she
knows.

2026-09-21 (Craig: "Would that People include the names of who she's
talking to? I think right now it just shows the connection id"). It did.
The old Users tab parsed "WS connected: <uuid>" out of her log, so it
knew a socket existed and nothing else, and a client that dropped without
a clean close stayed listed until someone removed it by hand.

Now she writes a row to db.sessions when a connection resolves to a
person (ws/ws_handlers.py): name, role, whether the voice check passed,
when they connected, when she last heard them. She closes it on
disconnect and closes every leftover at her next start. This view only
reads — it never depends on her answering."""
import asyncio

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QTableWidget, QAbstractItemView, QCheckBox, QMessageBox, QComboBox,
)

from db.db import fetch_sessions, session_closed, fetch_profiles_overview, fetch_conversation_tail
from controller.common import make_readable, fill_row, selected_rows, to_local, ago


class PeopleView(QWidget):
    def __init__(self, note):
        super().__init__()
        self.note = note
        self._sessions = []

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 4, 0, 0)
        self.inner = QTabWidget()
        who = QWidget()
        layout = QVBoxLayout()

        # ---------------- TALKING TO HER NOW ----------------
        self.live_label = QLabel("Talking to her now:")
        layout.addWidget(self.live_label)

        self.sessions_table = QTableWidget()
        self.sessions_table.setColumnCount(7)
        self.sessions_table.setHorizontalHeaderLabels(
            ["Who", "Role", "Voice check", "Connected", "For", "Last heard", "Session"])
        self.sessions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sessions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.sessions_table, wrap_column=6)
        self.sessions_table.setMaximumHeight(220)
        layout.addWidget(self.sessions_table)

        session_btns = QHBoxLayout()
        # A client that drops without a clean close (crash, network loss)
        # leaves its row open until her next restart closes it. 2026-07-16
        # (Craig): manual removal for exactly that "we don't know the
        # cause" case — a deliberate creator override, not something to
        # infer automatically.
        self.close_session_btn = QPushButton("🗑️ Close selected (stale connection)")
        self.close_session_btn.clicked.connect(self.close_selected_session)
        session_btns.addWidget(self.close_session_btn)
        self.show_closed = QCheckBox("Also show recently disconnected")
        self.show_closed.stateChanged.connect(lambda _: self.refresh())
        session_btns.addWidget(self.show_closed)
        session_btns.addStretch(1)
        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        session_btns.addWidget(self.refresh_btn)
        layout.addLayout(session_btns)

        # ---------------- EVERYONE SHE KNOWS ----------------
        layout.addWidget(QLabel("Everyone she knows:"))

        self.profiles_table = QTableWidget()
        self.profiles_table.setColumnCount(6)
        self.profiles_table.setHorizontalHeaderLabels(
            ["Who", "Role", "Voice samples", "Profile verified", "Known since", "Last heard"])
        self.profiles_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.profiles_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.profiles_table, wrap_column=0)
        layout.addWidget(self.profiles_table)

        layout.addWidget(QLabel(
            "Roles and voice samples are hers to change through conversation "
            "(see Commands); this view reads them."))
        who.setLayout(layout)
        self.inner.addTab(who, "Who")
        self.inner.addTab(self._build_conversation(), "Conversation")
        outer.addWidget(self.inner)
        self.setLayout(outer)

    # =====================================================================
    # CONVERSATION (2026-09-21)
    # =====================================================================
    # Craig, watching a tester through the log: "is there a way for me to
    # drop in on their conversation and see it first hand?" Each turn is a
    # memory row the moment it completes; this shows the newest ones as
    # they land, everyone or one person, her unprompted lines included.
    # Read-only: watching is not joining.
    def _build_conversation(self):
        page = QWidget()
        lay = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(QLabel("Live — refreshes every few seconds. Show:"))
        self.conv_filter = QComboBox()
        self.conv_filter.addItem("everyone")
        self.conv_filter.currentTextChanged.connect(lambda _: self.refresh_conversation(full=True))
        top.addWidget(self.conv_filter)
        top.addStretch(1)
        self.conv_refresh_btn = QPushButton("🔄 Refresh")
        self.conv_refresh_btn.clicked.connect(lambda: self.refresh_conversation(full=True))
        top.addWidget(self.conv_refresh_btn)
        lay.addLayout(top)

        self.conv_table = QTableWidget()
        self.conv_table.setColumnCount(4)
        self.conv_table.setHorizontalHeaderLabels(["When", "Who", "They said", "She replied"])
        self.conv_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.conv_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.conv_table, wrap_column=3)
        self.conv_table.horizontalHeader().setSectionResizeMode(2, self.conv_table.horizontalHeader().sectionResizeMode(3))
        lay.addWidget(self.conv_table)
        self._conv_last_id = 0
        page.setLayout(lay)
        return page

    def refresh_conversation(self, full: bool = False):
        user = self.conv_filter.currentText()
        user = None if user in ("", "everyone") else user
        try:
            if full:
                rows = asyncio.run(fetch_conversation_tail(limit=40, user=user))
                self.conv_table.setRowCount(0)
                self._conv_last_id = 0
            else:
                rows = asyncio.run(fetch_conversation_tail(limit=40, user=user, after_id=self._conv_last_id))
        except Exception as e:
            self.note(f"⚠️ Failed to read the conversation: {e}")
            return
        if not rows:
            return
        for r in rows:
            row = self.conv_table.rowCount()
            self.conv_table.insertRow(row)
            said = r.get("prompt") or ""
            if said.startswith("(unprompted"):
                said = "(she spoke first)"
            fill_row(self.conv_table, row, [
                to_local(r.get("created_at"))[11:], r.get("user") or "?", said, r.get("response") or ""])
            self._conv_last_id = max(self._conv_last_id, int(r["id"]))
        # keep the last 60 rows so a long night does not grow the table forever
        while self.conv_table.rowCount() > 60:
            self.conv_table.removeRow(0)
        self.conv_table.resizeRowsToContents()
        self.conv_table.scrollToBottom()

    def _refresh_conv_filter(self, names):
        current = self.conv_filter.currentText()
        have = [self.conv_filter.itemText(i) for i in range(self.conv_filter.count())]
        for n in names:
            if n and n not in have:
                self.conv_filter.addItem(n)
        if current and self.conv_filter.currentText() != current:
            self.conv_filter.setCurrentText(current)

    def live_count(self) -> int:
        return sum(1 for s in self._sessions if not s.get("disconnected_at"))

    def refresh(self):
        try:
            self._sessions = asyncio.run(fetch_sessions(live_only=not self.show_closed.isChecked(), limit=50))
        except Exception as e:
            self.note(f"⚠️ Failed to load sessions: {e}")
            self._sessions = []

        live = self.live_count()
        self.live_label.setText(
            f"Talking to her now ({live}):" if live else
            "Talking to her now: nobody (rows appear once she has restarted with the 2026-09-21 build)")

        self.sessions_table.setRowCount(len(self._sessions))
        for row, s in enumerate(self._sessions):
            open_ = not s.get("disconnected_at")
            if open_:
                voice = "passed" if s.get("verified") else ("not required" if s.get("role") == "user" else "not passed")
                for_ = ago(s.get("connected_at"))
                heard = (ago(s.get("last_heard_at")) + " ago") if s.get("last_heard_at") else "not yet"
            else:
                voice = "passed" if s.get("verified") else ""
                for_ = f"ended {ago(s['disconnected_at'])} ago ({s.get('closed_by') or 'disconnect'})"
                heard = (ago(s.get("last_heard_at")) + " ago") if s.get("last_heard_at") else ""
            fill_row(self.sessions_table, row, [
                s.get("user") or "?", s.get("role") or "", voice,
                to_local(s.get("connected_at")), for_, heard, s["session_id"]])
        self.sessions_table.resizeRowsToContents()

        try:
            profiles = asyncio.run(fetch_profiles_overview())
        except Exception as e:
            self.note(f"⚠️ Failed to load profiles: {e}")
            profiles = []

        self.profiles_table.setRowCount(len(profiles))
        for row, p in enumerate(profiles):
            fill_row(self.profiles_table, row, [
                p["user"], p["role"], p["voice_samples"],
                "yes" if p.get("verified") else "no",
                to_local(p.get("created_at")),
                (ago(p["last_heard_at"]) + " ago") if p.get("last_heard_at") else "never"])
        self.profiles_table.resizeRowsToContents()
        self._refresh_conv_filter([p["user"] for p in profiles])
        self.refresh_conversation()

    def close_selected_session(self):
        rows = selected_rows(self.sessions_table)
        if not rows:
            return
        picked = [self._sessions[r] for r in rows if r < len(self._sessions) and not self._sessions[r].get("disconnected_at")]
        if not picked:
            self.note("⚠️ Select an open session first.")
            return

        confirm = QMessageBox.question(
            self, "Close session row",
            "Mark this connection as gone? This only edits her record of it — "
            "if the client is in fact still connected, it keeps talking to her.\n\n"
            + "\n".join(f"- {s.get('user')} ({s['session_id']})" for s in picked),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        for s in picked:
            try:
                asyncio.run(session_closed(s["session_id"], closed_by="controller (stale)"))
                self.note(f"[SYSTEM] Closed stale session row for {s.get('user')} ({s['session_id']}) via Controller")
            except Exception as e:
                self.note(f"⚠️ Failed to close session row: {e}")
        self.refresh()
