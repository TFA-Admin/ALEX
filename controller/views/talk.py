# controller/views/talk.py
"""Talk — a conversation with her, typed, from inside the Controller.

2026-09-26 (Craig: "can we add a direct conversation interface into the
controller purely for text interactions?").

This is not a second brain or a debug prompt. It is an ordinary client on
the same `/ws` endpoint the browser page uses, so everything that makes a
turn a real turn still happens: alex_core, her personality, her facts and
memory, her modules, her commands, what she remembers afterward. The only
differences are the two things a keyboard genuinely cannot do, and both
are declared in the handshake (`text_only`, see ws/ws_handlers.py):

  no voice out — nothing synthesizes, so Piper is not spent on a turn
      nobody hears and no audio bytes are sent to a window with no
      speaker. core/voice.say() and core/response_handler.py both ask
      core.voice.is_text_only() about the connection.

  no voice in — the creator's connect-time voice check cannot be
      satisfied by typing, and neither can the face check. The override
      code can (core/override_code.py already treats the code alone as
      creator proof, and verify_voice()'s typed branch already said so),
      so there is a field for it here. Leave it empty and the session is
      unverified: she still talks, and creator commands still refuse
      until the code is given — in the field, or said inside the sentence
      the way it has always worked.

Two smaller things worth knowing:

  She types back through the raw per-chunk stream (the path the headless
  test client has used all along), so her words arrive as she writes them
  rather than a clause at a time behind synthesized audio. Markdown is
  left exactly as she wrote it, which in a Controller is information.

  Typed text skips the wake-word gate entirely — ws/ws_handlers.py has
  never applied it to anything but audio, since typing to her is already
  unambiguous. No "Alex," needed, ever.
"""
import json
import sqlite3
import time

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QFont, QGuiApplication, QTextCharFormat, QTextCursor
from PySide6.QtNetwork import QAbstractSocket, QSslConfiguration, QSslSocket
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QPlainTextEdit, QLineEdit, QCheckBox,
)

from controller.common import DB_PATH

# Her port, same as ProcessManager's (controller/procs.py) — one live
# instance on 5000. A staging copy runs on its own port, but a staged copy
# is started to be talked to in the browser with its own avatar page, so
# this deliberately only ever talks to the real one.
ALEX_WS_URL = "wss://127.0.0.1:5000/ws"

# Local self-signed cert (certs/*.pem), not CA-verified — the same
# local-trust model tools/claude_client.py and tests/harness.py use.
RECONNECT_MS = 4000


def _creator_name() -> str:
    """Who to connect as. Read straight from facts rather than through her
    async DB layer, and deliberately only the name — the override code
    lives in the same table and this window has no reason to hold it."""
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as db:
            row = db.execute(
                "SELECT user FROM facts WHERE key='role' AND value='creator' LIMIT 1"
            ).fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


class _Composer(QPlainTextEdit):
    """Enter sends, Shift+Enter is a new line. A QLineEdit would have been
    less code, but a message to her is often a paragraph, and pasting one
    into a single-line box loses the breaks."""

    def __init__(self, on_send):
        super().__init__()
        self._on_send = on_send
        self.setPlaceholderText("Say something to her — Enter sends, Shift+Enter for a new line")
        self.setTabChangesFocus(True)
        fm = self.fontMetrics()
        self.setFixedHeight(fm.lineSpacing() * 3 + 12)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self._on_send()
            return
        super().keyPressEvent(event)


class TalkView(QWidget):
    MAX_LINES = 1500

    def __init__(self, note, procs):
        super().__init__()
        self.note = note
        self.procs = procs

        self.ws = QWebSocket()
        # Self-signed, localhost: accept it deliberately and say so, rather
        # than leaving a connection that fails with nothing in the window.
        cfg = QSslConfiguration.defaultConfiguration()
        cfg.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
        self.ws.setSslConfiguration(cfg)
        self.ws.sslErrors.connect(lambda _errors: self.ws.ignoreSslErrors())
        self.ws.connected.connect(self._on_connected)
        self.ws.disconnected.connect(self._on_disconnected)
        self.ws.textMessageReceived.connect(self._on_text)
        self.ws.errorOccurred.connect(self._on_error)

        self._want_connected = False     # he asked for a connection (or a tab open did)
        self._handshaken = False         # __PROFILE__ arrived; she is listening
        self._in_reply = False           # between __START__ and __END__
        self._her_turn_open = False      # a line of hers is being written to
        self._readiness = ""
        self._mood = ""
        self._version = ""
        self._verified = False
        self._sent_at = 0.0
        self._last_turn_s = 0.0
        self._he_closed_it = False       # an explicit Disconnect, not to be undone by a tab switch
        # Kept for as long as this window is open and no longer: she
        # restarts often, and re-typing the code after every restart would
        # make the verified path not worth using. Disconnect clears it.
        # Nothing is written to disk. The Controller can read the code out
        # of her facts table whenever it likes, so holding it here is not
        # an exposure the window did not already have.
        self._code = ""
        self._user = _creator_name() or "craig"

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)

        # ---------------- STATUS ----------------
        self.status = QLabel("Not connected")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # ---------------- TRANSCRIPT ----------------
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.transcript)

        # ---------------- COMPOSER ----------------
        self.composer = _Composer(self.send)
        layout.addWidget(self.composer)

        send_row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)
        send_row.addWidget(self.send_btn)
        self.connect_btn = QPushButton("🔌 Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        send_row.addWidget(self.connect_btn)
        send_row.addStretch(1)
        self.working_box = QCheckBox("Show her working")
        self.working_box.setToolTip(
            "The debug lines she also sends the browser's debug panel — what she heard, "
            "what she skipped, why. The Run tab's A.L.E.X. console has the full log either way.")
        send_row.addWidget(self.working_box)
        self.copy_btn = QPushButton("📋 Copy")
        self.copy_btn.clicked.connect(self._copy)
        send_row.addWidget(self.copy_btn)
        self.clear_btn = QPushButton("🧹 Clear")
        self.clear_btn.setToolTip("Clears this transcript only — she still remembers the conversation.")
        self.clear_btn.clicked.connect(self.transcript.clear)
        send_row.addWidget(self.clear_btn)
        layout.addLayout(send_row)

        # ---------------- VERIFICATION ----------------
        # Typing cannot prove a voice, so the code is the way through, and
        # it is sent in the handshake of a fresh connection rather than
        # kept anywhere: not in config/controller_settings.json, not in
        # this widget after it is used.
        unlock_row = QHBoxLayout()
        unlock_row.addWidget(QLabel("Override code (optional, for creator commands):"))
        self.code_field = QLineEdit()
        self.code_field.setEchoMode(QLineEdit.Password)
        self.code_field.setMaximumWidth(220)
        self.code_field.setToolTip(
            "A text session cannot pass a voice check. Your override code proves who you are instead — "
            "the same rule she already applies when you say it mid-sentence. Never written to disk, kept "
            "only while this window is open, and cleared by Disconnect; reconnects so she can check it.")
        self.code_field.returnPressed.connect(self._unlock)
        unlock_row.addWidget(self.code_field)
        self.unlock_btn = QPushButton("Verify")
        self.unlock_btn.clicked.connect(self._unlock)
        unlock_row.addWidget(self.unlock_btn)
        unlock_row.addStretch(1)
        layout.addLayout(unlock_row)

        self.setLayout(layout)
        self._set_enabled(False)

        # She may not be up yet, or may be restarted from the Run tab while
        # this sits open. One timer covers both: it only acts while a
        # connection is wanted and missing.
        self._retry = QTimer(self)
        self._retry.timeout.connect(self._retry_tick)
        self._retry.start(RECONNECT_MS)

        self._refresh_status()

    # ---------------- CONNECTION ----------------
    def open_tab(self):
        """Called when the tab is selected. Connecting lazily rather than at
        launch matters: a session registered as the creator is one
        core/proactive.py counts as him being present, and an idle
        check-in pushed into a window nobody opened is exactly the kind of
        thing that made her look like she was talking to herself."""
        if not self._want_connected and not self._he_closed_it:
            self.connect_to_her()

    def connect_to_her(self, code: str = ""):
        self._want_connected = True
        self._he_closed_it = False
        if code:
            self._code = code
        if self.ws.state() != QAbstractSocket.UnconnectedState:
            self.ws.abort()
        if not self.procs.alex_up():
            self._system("She is not running — start her on the Run tab. "
                         "This will connect by itself once she is up.")
            self._refresh_status()
            return
        self.ws.open(QUrl(ALEX_WS_URL))
        self._refresh_status()

    def disconnect_from_her(self, quiet: bool = False):
        self._want_connected = False
        self._he_closed_it = True
        self._handshaken = False
        self._code = ""
        self._verified = False
        self.ws.close()
        if not quiet:
            self._system("Disconnected.")
        self._set_enabled(False)
        self._refresh_status()

    def _toggle_connection(self):
        if self._want_connected:
            self.disconnect_from_her()
        else:
            self.connect_to_her(self.code_field.text().strip())

    def _retry_tick(self):
        if (self._want_connected
                and self.ws.state() == QAbstractSocket.UnconnectedState
                and self.procs.alex_up()):
            self.ws.open(QUrl(ALEX_WS_URL))

    def _on_connected(self):
        self._handshaken = False
        self._verified = False
        # The one message she waits for before anything else. `text_only`
        # is what makes this a typed session rather than a browser with a
        # broken microphone. The code goes with it so a reconnect after
        # she restarts comes back verified instead of silently demoted.
        self.ws.sendTextMessage(json.dumps({
            "user_name": self._user,
            "text_only": True,
            **({"override_code": self._code} if self._code else {}),
        }))
        self._refresh_status()

    def _on_disconnected(self):
        was = self._handshaken
        self._handshaken = False
        self._in_reply = False
        self._her_turn_open = False
        self._set_enabled(False)
        if was and self._want_connected:
            self._system("She closed the connection (a restart, most likely) — reconnecting.")
        self._refresh_status()

    def _on_error(self, _error):
        # Silent while simply waiting for her to come up; the status line
        # already says what is going on, and a failed retry every four
        # seconds would be the only thing in the transcript.
        if self._handshaken:
            self._system(f"Connection error: {self.ws.errorString()}")
        self._refresh_status()

    def _unlock(self):
        code = self.code_field.text().strip()
        if not code:
            self._system("Type your override code first, or leave it empty to talk unverified.")
            return
        if not self.procs.alex_up():
            self._system("She is not running — start her on the Run tab first.")
            return
        self.code_field.clear()
        self._system("Reconnecting with your override code — she checks it at connect.")
        self.connect_to_her(code)

    # ---------------- INCOMING ----------------
    def _on_text(self, msg: str):
        if not msg:
            return

        if msg.startswith("__PROFILE__"):
            if not self._handshaken:
                self._handshaken = True
                self._set_enabled(True)
                self._system(f"Connected as {self._user} — typed session, she will not speak.")
                self.composer.setFocus()
            self._refresh_status()
            return

        if msg.startswith("__READY__"):
            self._readiness = msg[len("__READY__"):]
            self._refresh_status()
            return

        if msg.startswith("__VERSION__"):
            try:
                self._version = (json.loads(msg[len("__VERSION__"):]) or {}).get("label", "")
            except Exception:
                self._version = ""
            self._refresh_status()
            return

        if msg.startswith("__MOOD__"):
            try:
                self._mood = (json.loads(msg[len("__MOOD__"):]) or {}).get("label", "")
            except Exception:
                self._mood = ""
            self._refresh_status()
            return

        if msg.startswith("__DEBUG__"):
            line = msg[len("__DEBUG__"):]
            # Verification is the one debug line this view never hides: it
            # is the answer to "why did she just refuse that". The badge it
            # sets is a label read off her own words, not a gate — the gate
            # is require_creator() in her process, checked there on every
            # privileged command however this line is read.
            if "Override code accepted" in line or "verified for this session" in line:
                self._verified = True
                self._system(line)
                self._refresh_status()
                return
            if "unverified" in line or "not recognised" in line:
                self._verified = False
                self._system(line)
                self._refresh_status()
                return
            if self.working_box.isChecked():
                self._system(line)
            return

        if msg == "__START__":
            self._in_reply = True
            return

        if msg == "__END__":
            self._in_reply = False
            self._close_her_turn()
            if self._sent_at:
                self._last_turn_s = time.monotonic() - self._sent_at
                self._sent_at = 0.0
            self._refresh_status()
            return

        if msg.startswith("__SELFWORK__"):
            self._system("She is working on something of her own."
                         if msg.endswith("1") else "She has finished what she was working on.")
            return

        if msg.startswith("__"):
            # __ENGAGED__/__PET__/__FACE__/__LOOK__/__UNADDRESSED__ and
            # anything added later: state for the avatar page, not part of
            # a conversation. Never printed as if she had said it.
            return

        self._her_text(msg)

    # ---------------- OUTGOING ----------------
    def send(self):
        text = self.composer.toPlainText().strip()
        if not text:
            return
        if not self._handshaken:
            self._system("Not connected to her yet.")
            return
        self.composer.clear()
        self._sent_at = time.monotonic()
        self._mine(text)
        self.ws.sendTextMessage(text)
        self._refresh_status()

    # ---------------- TRANSCRIPT ----------------
    def _cursor_at_end(self) -> QTextCursor:
        cursor = QTextCursor(self.transcript.document())
        cursor.movePosition(QTextCursor.End)
        return cursor

    def _insert(self, text: str, *, bold: bool = False, dim: bool = False):
        cursor = self._cursor_at_end()
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
        if dim:
            colour = self.palette().text().color()
            colour.setAlpha(150)
            fmt.setForeground(colour)
        cursor.insertText(text, fmt)
        self._trim()
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _new_line_if_needed(self):
        doc = self.transcript.document()
        if doc.isEmpty() or doc.lastBlock().text() == "":
            return
        self._insert("\n")

    def _stamp(self) -> str:
        return time.strftime("%H:%M")

    def _mine(self, text: str):
        self._close_her_turn()
        self._new_line_if_needed()
        self._insert(f"{self._stamp()}  ", dim=True)
        self._insert("You: ", bold=True)
        self._insert(text + "\n")

    def _her_text(self, chunk: str):
        """Her reply arrives as raw generation chunks — appended verbatim,
        with no space inserted between them. The browser has to add one
        (avatar.html's appendBotText) because it receives whole clauses
        from split_speakable_text(), whose match ends at the punctuation;
        this connection gets the un-split stream instead, where an
        inserted space would land mid-word."""
        if not self._her_turn_open:
            self._new_line_if_needed()
            self._insert(f"{self._stamp()}  ", dim=True)
            self._insert("A.L.E.X.: ", bold=True)
            self._her_turn_open = True
        self._insert(chunk)

    def _close_her_turn(self):
        if self._her_turn_open:
            self._insert("\n")
            self._her_turn_open = False

    def _system(self, text: str):
        self._close_her_turn()
        self._new_line_if_needed()
        self._insert(f"{self._stamp()}  {text}\n", dim=True)

    def _trim(self):
        doc = self.transcript.document()
        excess = doc.blockCount() - self.MAX_LINES
        if excess > 0:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor, excess)
            cursor.removeSelectedText()

    def _copy(self):
        QGuiApplication.clipboard().setText(self.transcript.toPlainText())
        self.note("[SYSTEM] Copied the Talk transcript")

    # ---------------- STATE ----------------
    def _set_enabled(self, on: bool):
        self.composer.setEnabled(on)
        self.send_btn.setEnabled(on)

    def _refresh_status(self):
        from core.readiness import LABELS

        if self._handshaken:
            bits = [f"🟢 Talking to her as {self._user}"]
            bits.append("verified by override code" if self._verified
                        else "unverified — creator commands need your code")
        elif self._want_connected:
            state = self.ws.state()
            if state == QAbstractSocket.ConnectedState:
                bits = ["🟡 Connected, waiting for her handshake"]
            elif not self.procs.alex_up():
                bits = ["🔴 She is not running — will connect when she is"]
            else:
                bits = ["🟡 Connecting…"]
        else:
            bits = ["⚪ Not connected"]

        if self._readiness:
            bits.append(LABELS.get(self._readiness, self._readiness))
        if self._in_reply:
            bits.append("she is replying…")
        elif self._last_turn_s and self._handshaken:
            bits.append(f"last turn took {self._last_turn_s:.1f}s")
        if self._mood:
            bits.append(f"mood: {self._mood}")
        if self._version:
            bits.append(self._version)

        self.status.setText(" · ".join(bits))
        self.connect_btn.setText("⛔ Disconnect" if self._want_connected else "🔌 Connect")
        self.unlock_btn.setEnabled(self.procs.alex_up())

    # ---------------- SHUTDOWN ----------------
    def shutdown(self):
        """Closed with the window — a socket left open would keep a
        creator session alive in her sessions table until her next start
        closed it as stale."""
        self._retry.stop()
        if self.ws.state() != QAbstractSocket.UnconnectedState:
            self.ws.close()
