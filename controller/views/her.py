# controller/views/her.py
"""Her — what she is made of, as it stands right now: personality and the
standing rules over it, what she believes, what she decided and why, what
she is curious about, her modules, and a health check.

Everything here reads the same database she does, so what this shows is
what her next turn will use. The parts that change her (reset, override,
mute, confirm, retract, roll back) are the ones the Controller was built
for: hard resets belong here, not in chat/voice, where the phrasing is
fragile on a small local model."""
import os
import asyncio
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QTableWidget, QAbstractItemView, QTextEdit, QLineEdit, QMessageBox,
)

from db.db import (
    get_personality, set_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases, add_personality_hard_rule,
    get_personality_hard_rules, remove_personality_hard_rule,
    fetch_recent_personality_changes,
    fetch_active_conclusions, fetch_decisions, fetch_curiosity_queue,
    list_module_registry, fetch_module_versions, get_module_version_code,
    get_module_registry_entry, register_module_version,
    persona_disabled, PERSONA_FLAG_PATH,
)
from core.intent_classifier import merge_personality_change
from module_runtime.module_installer import install_module

from controller.common import make_readable, fill_row, selected_rows, to_local
from controller import actions


class HerView(QWidget):
    def __init__(self, note):
        super().__init__()
        self.note = note
        self._beliefs = []
        self._versions_loaded_for = None

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        self.inner = QTabWidget()
        self.inner.addTab(self._build_personality(), "Personality")
        self.inner.addTab(self._build_beliefs(), "Beliefs")
        self.inner.addTab(self._build_decisions(), "Decisions")
        self.inner.addTab(self._build_curiosity(), "Curiosity")
        self.inner.addTab(self._build_modules(), "Modules")
        self.inner.addTab(self._build_health(), "Health")
        layout.addWidget(self.inner)
        self.setLayout(layout)

    # =====================================================================
    # PERSONALITY
    # =====================================================================
    def _build_personality(self):
        page = QWidget()
        lay = QVBoxLayout()

        lay.addWidget(QLabel("Her personality, as she reads it on every turn:"))
        self.personality_view = QTextEdit()
        self.personality_view.setReadOnly(True)
        self.personality_view.setMaximumHeight(110)
        lay.addWidget(self.personality_view)

        btns = QHBoxLayout()
        self.reset_personality_btn = QPushButton("♻️ Reset personality to default")
        self.reset_personality_btn.clicked.connect(self.reset_personality)
        self.reset_phrases_btn = QPushButton("♻️ Reset all phrases to default")
        self.reset_phrases_btn.clicked.connect(self.reset_phrases)
        # 2026-09-20: the persona switch. Not a personality EDIT — it mutes
        # her personality entirely, leaving function intact, and restores
        # it exactly on toggling back. Sits next to the personality
        # controls because that is where it will be looked for, but it
        # writes a file rather than the database precisely so it is not
        # stored beside the thing it disables.
        self.persona_toggle_btn = QPushButton()
        self.persona_toggle_btn.clicked.connect(self.toggle_persona)
        for b in (self.reset_personality_btn, self.reset_phrases_btn, self.persona_toggle_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        self.personality_refresh_btn = QPushButton("🔄 Refresh")
        self.personality_refresh_btn.clicked.connect(self.refresh_personality)
        btns.addWidget(self.personality_refresh_btn)
        lay.addLayout(btns)
        self._refresh_persona_button()

        # -------------------------
        # STANDING RULES (2026-09-20)
        # -------------------------
        # Craig: "but it's still in her hard rules - I must not be able to
        # view these from the controller." He could not. Every personality
        # override silently writes one of these, they are never touched by
        # any rewrite — which is the point of them — and there was no way
        # to see the list or remove a single entry. So rules set weeks ago
        # went on binding her invisibly, and editing the personality
        # description did nothing, because that is a different field.
        lay.addWidget(QLabel(
            "Standing rules (verbatim, never rewritten — these override her personality):"))
        self.hard_rules_list = QTableWidget()
        self.hard_rules_list.setColumnCount(1)
        self.hard_rules_list.setHorizontalHeaderLabels(["Rule"])
        self.hard_rules_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hard_rules_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hard_rules_list.setMaximumHeight(140)
        make_readable(self.hard_rules_list, wrap_column=0)
        lay.addWidget(self.hard_rules_list)

        rule_btns = QHBoxLayout()
        self.remove_hard_rule_btn = QPushButton("🗑️ Remove selected rule")
        self.remove_hard_rule_btn.clicked.connect(self.remove_selected_hard_rule)
        rule_btns.addWidget(self.remove_hard_rule_btn)
        rule_btns.addStretch(1)
        lay.addLayout(rule_btns)

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
        lay.addWidget(QLabel("Personality override (typed instruction, e.g. \"be more direct and stop using emojis\"):"))
        override_row = QHBoxLayout()
        self.personality_override_input = QLineEdit()
        self.personality_override_input.setPlaceholderText("Type an instruction and click Apply...")
        override_row.addWidget(self.personality_override_input)
        self.apply_personality_btn = QPushButton("✅ Apply")
        self.apply_personality_btn.clicked.connect(self.apply_personality_override)
        override_row.addWidget(self.apply_personality_btn)
        lay.addLayout(override_row)

        # What changed lately, hers and his — the acknowledged rows the
        # Inbox no longer shows.
        lay.addWidget(QLabel("Recent changes (hers by self-reflection, yours from here):"))
        self.personality_changes_table = QTableWidget()
        self.personality_changes_table.setColumnCount(4)
        self.personality_changes_table.setHorizontalHeaderLabels(["When", "Kind", "New value", "Reason"])
        self.personality_changes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.personality_changes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.personality_changes_table, wrap_column=2)
        lay.addWidget(self.personality_changes_table)

        page.setLayout(lay)
        return page

    def refresh_personality(self):
        try:
            current = asyncio.run(get_personality())
            self.personality_view.setPlainText(current or "")
        except Exception as e:
            self.personality_view.setPlainText(f"⚠️ Failed to load personality: {e}")
        self._refresh_persona_button()
        self.refresh_hard_rules()

        try:
            changes = asyncio.run(fetch_recent_personality_changes(limit=10))
        except Exception as e:
            self.note(f"⚠️ Failed to load personality changes: {e}")
            changes = []
        self.personality_changes_table.setRowCount(len(changes))
        for row, c in enumerate(changes):
            fill_row(self.personality_changes_table, row, [
                to_local(c.get("created_at")), c.get("kind"), c.get("new_value"), c.get("reason")])
        self.personality_changes_table.resizeRowsToContents()

    def _refresh_persona_button(self):
        if persona_disabled():
            self.persona_toggle_btn.setText("🎭 Personality: OFF — Restore")
            self.persona_toggle_btn.setToolTip(
                "Her personality is currently muted. She is running plain and functional.")
        else:
            self.persona_toggle_btn.setText("🔇 Disable personality")
            self.persona_toggle_btn.setToolTip(
                "Mute her personality entirely. Nothing she has developed is lost — "
                "it returns the moment this is switched back.")

    def toggle_persona(self):
        """Writes/removes the flag file db.persona_disabled() reads.

        Deliberately not a database write: `system_learning` holds the
        personality itself and is reachable by her reflection loop and by her
        own conversational personality path, so a switch stored there would sit
        beside the thing it disables. This file is the Controller's.

        Takes effect on her NEXT turn — get_personality() checks the switch at
        read time, so no restart is needed. Nothing is overwritten, so toggling
        back restores exactly what she had.
        """
        turning_off = not persona_disabled()

        if turning_off:
            confirm = QMessageBox.question(
                self, "Disable Personality",
                "Mute A.L.E.X.'s personality?\n\n"
                "She will answer plainly and functionally until this is switched back. "
                "Nothing she has developed is lost or overwritten, and self-reflection "
                "will not evolve her personality while muted.\n\n"
                "This does NOT reduce her safety refusals — it affects how she speaks, "
                "not what she is willing to do. The hard stop remains Stop A.L.E.X.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if confirm != QMessageBox.Yes:
                return

        try:
            if turning_off:
                os.makedirs(os.path.dirname(PERSONA_FLAG_PATH), exist_ok=True)
                with open(PERSONA_FLAG_PATH, "w", encoding="utf-8") as fh:
                    fh.write(f"disabled via Controller at {datetime.now().isoformat(timespec='seconds')}\n")
                self.note("[SYSTEM] Personality MUTED — she is running plain until restored")
            else:
                os.remove(PERSONA_FLAG_PATH)
                self.note("[SYSTEM] Personality restored")
        except OSError as e:
            self.note(f"⚠️ Failed to toggle persona switch: {e}")

        self._refresh_persona_button()

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
            self.note("[SYSTEM] Personality reset to default via Controller")
        except Exception as e:
            self.note(f"⚠️ Failed to reset personality: {e}")
        self.refresh_personality()

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

            self.note(f"[SYSTEM] Personality updated via Controller: {merged}")
            self.personality_override_input.clear()
        except Exception as e:
            self.note(f"⚠️ Failed to apply personality override: {e}")
        self.refresh_personality()

    def refresh_hard_rules(self):
        try:
            rules = asyncio.run(get_personality_hard_rules())
        except Exception as e:
            self.note(f"⚠️ Failed to load standing rules: {e}")
            rules = []

        self.hard_rules_list.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            fill_row(self.hard_rules_list, row, [str(rule)])
        self.hard_rules_list.resizeRowsToContents()

    def remove_selected_hard_rule(self):
        items = self.hard_rules_list.selectedItems()
        if not items:
            self.note("⚠️ Select a rule to remove first.")
            return

        rule = items[0].text()
        confirm = QMessageBox.question(
            self, "Remove standing rule",
            f"Remove this rule?\n\n{rule}\n\n"
            "She stops being bound by it immediately.",
            QMessageBox.Yes | QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        try:
            removed = asyncio.run(remove_personality_hard_rule(rule))
        except Exception as e:
            self.note(f"⚠️ Failed to remove standing rule: {e}")
            return

        self.note(
            f"[SYSTEM] Standing rule removed: {rule}" if removed
            else f"⚠️ That rule was not found: {rule}")
        self.refresh_hard_rules()

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
            self.note("[SYSTEM] All phrases reset to default via Controller")
        except Exception as e:
            self.note(f"⚠️ Failed to reset phrases: {e}")
        self.refresh_personality()

    # =====================================================================
    # BELIEFS (2026-09-21)
    # =====================================================================
    # Her live beliefs, and the two things only Craig can do to one. An
    # unconfirmed belief is hers to hold and revise and it steers nothing;
    # confirming it is what lets it reach her replies. Unconfirmed ones
    # also sit in the Inbox; this is the whole list, confirmed included.
    def _build_beliefs(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel(
            "What she currently believes. Nothing here reaches her replies "
            "until you confirm it. Retract what is wrong."))
        self.beliefs_table = QTableWidget()
        self.beliefs_table.setColumnCount(5)
        self.beliefs_table.setHorizontalHeaderLabels(["#", "Status", "About", "Belief", "Based on"])
        self.beliefs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.beliefs_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.beliefs_table, wrap_column=3)
        lay.addWidget(self.beliefs_table)

        btns = QHBoxLayout()
        self.belief_confirm_btn = QPushButton("✅ Confirm belief")
        self.belief_confirm_btn.clicked.connect(self.confirm_belief)
        btns.addWidget(self.belief_confirm_btn)
        self.belief_retract_btn = QPushButton("❌ Retract belief")
        self.belief_retract_btn.clicked.connect(self.retract_belief)
        btns.addWidget(self.belief_retract_btn)
        btns.addStretch(1)
        self.beliefs_refresh_btn = QPushButton("🔄 Refresh")
        self.beliefs_refresh_btn.clicked.connect(self.refresh_beliefs)
        btns.addWidget(self.beliefs_refresh_btn)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def refresh_beliefs(self):
        try:
            rows = asyncio.run(fetch_active_conclusions(limit=50))
        except Exception as e:
            self.note(f"⚠️ Failed to load her beliefs: {e}")
            rows = []

        self._beliefs = rows
        self.beliefs_table.setRowCount(len(rows))
        for row, c in enumerate(rows):
            fill_row(self.beliefs_table, row, [
                c["id"],
                "confirmed by you" if c.get("status") == "confirmed" else "hers, unconfirmed",
                c["kind"], c["statement"], c["evidence"] or ""])
        self.beliefs_table.resizeRowsToContents()

    def _selected_belief(self):
        row = self.beliefs_table.currentRow()
        if row < 0 or row >= len(self._beliefs):
            QMessageBox.information(self, "Beliefs", "Select a belief first.")
            return None
        return self._beliefs[row]

    def confirm_belief(self):
        belief = self._selected_belief()
        if belief and actions.confirm_belief(self, belief, self.note):
            self.refresh_beliefs()
            self.refresh_decisions()
            self.changed()

    def retract_belief(self):
        belief = self._selected_belief()
        if belief and actions.retract_belief(self, belief, self.note):
            self.refresh_beliefs()
            self.refresh_decisions()
            self.changed()

    def changed(self):
        """Replaced by the app: the Inbox count may have moved."""

    # =====================================================================
    # DECISIONS (2026-09-20)
    # =====================================================================
    # Craig, twice: "I'd like to see her thought process for everything
    # she does." Every mechanism already recorded its own outcome in its
    # own table, and the reasoning — where it existed — was an [ACTION]
    # line in a log file. Reading what she decided and why meant six
    # tabs and a text file, so in practice it was read by nobody.
    #
    # One list, newest first, with the reasoning as the wide column.
    # The `Why` text is deliberately explicit about whose reasoning it
    # is: some of these are hers, and some are a rule firing. Conflating
    # the two would make her look like she is thinking when she is not.
    def _build_decisions(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel("What she decided, and why. Newest first."))
        self.reasoning_table = QTableWidget()
        self.reasoning_table.setColumnCount(6)
        self.reasoning_table.setHorizontalHeaderLabels(
            ["When", "Kind", "What she decided", "Why", "Based on", "Result"])
        self.reasoning_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.reasoning_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.reasoning_table, wrap_column=3)
        lay.addWidget(self.reasoning_table)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.reasoning_refresh_btn = QPushButton("🔄 Refresh")
        self.reasoning_refresh_btn.clicked.connect(self.refresh_decisions)
        btns.addWidget(self.reasoning_refresh_btn)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def refresh_decisions(self):
        try:
            rows = asyncio.run(fetch_decisions(limit=200))
        except Exception as e:
            self.note(f"⚠️ Failed to load her reasoning: {e}")
            rows = []

        self.reasoning_table.setRowCount(len(rows))
        for row, d in enumerate(rows):
            fill_row(self.reasoning_table, row, [
                to_local(d["created_at"]), d["kind"], d["summary"],
                d["reasoning"] or "", d["evidence"] or "", d["outcome"] or ""])
        self.reasoning_table.resizeRowsToContents()

    # =====================================================================
    # CURIOSITY
    # =====================================================================
    # What she has wanted to ask, and what she was told. The questions
    # reach him in live chat (deliberately — they are hers, not an audit
    # report); this is the record.
    def _build_curiosity(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel("What she has been curious about, newest first."))
        self.curiosity_table = QTableWidget()
        self.curiosity_table.setColumnCount(5)
        self.curiosity_table.setHorizontalHeaderLabels(["When", "Topic", "Her question", "Status", "Answer"])
        self.curiosity_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.curiosity_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.curiosity_table, wrap_column=2)
        lay.addWidget(self.curiosity_table)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.curiosity_refresh_btn = QPushButton("🔄 Refresh")
        self.curiosity_refresh_btn.clicked.connect(self.refresh_curiosity)
        btns.addWidget(self.curiosity_refresh_btn)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def refresh_curiosity(self):
        try:
            rows = asyncio.run(fetch_curiosity_queue(limit=50))
        except Exception as e:
            self.note(f"⚠️ Failed to load her curiosity: {e}")
            rows = []

        self.curiosity_table.setRowCount(len(rows))
        for row, q in enumerate(rows):
            if q.get("answer"):
                status = f"answered {to_local(q.get('answered_at'))}"
            elif q.get("delivered"):
                status = f"asked {q['delivered']}x, no answer yet"
            else:
                status = "not asked yet"
            fill_row(self.curiosity_table, row, [
                to_local(q.get("created_at")), q.get("topic"), q.get("question"), status, q.get("answer") or ""])
        self.curiosity_table.resizeRowsToContents()

    # =====================================================================
    # MODULES
    # =====================================================================
    # What's actually installed and its live status, then the version
    # history of a selected module and rollback. Build requests live in
    # the Inbox (open) and its History (settled).
    def _build_modules(self):
        page = QWidget()
        lay = QVBoxLayout()

        lay.addWidget(QLabel("Installed modules:"))
        self.module_status_table = QTableWidget()
        self.module_status_table.setColumnCount(6)
        self.module_status_table.setHorizontalHeaderLabels(
            ["Name", "Version", "Status", "Source", "Access Scope", "Updated At"])
        self.module_status_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.module_status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.module_status_table, wrap_column=0)
        lay.addWidget(self.module_status_table)

        # 🕓 MODULE VERSION HISTORY + ROLLBACK (2026-07-18) —
        # register_module_version() has snapshotted every real build's
        # code since the versioning system was built, specifically "so
        # rollback has something real to restore" (its own docstring),
        # but nothing ever exposed that history or let anyone act on it.
        # Select a module above, Load Version History shows every past
        # version; Roll Back To Selected reinstalls that version's code
        # as current — recorded as a NEW version (via
        # register_module_version), never silently overwriting history.
        lay.addWidget(QLabel("Version history (select a module above, then load):"))
        self.module_versions_table = QTableWidget()
        self.module_versions_table.setColumnCount(2)
        self.module_versions_table.setHorizontalHeaderLabels(["Version", "Created At"])
        self.module_versions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.module_versions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.module_versions_table.setMaximumHeight(160)
        make_readable(self.module_versions_table, wrap_column=1)
        lay.addWidget(self.module_versions_table)

        btns = QHBoxLayout()
        self.load_versions_btn = QPushButton("📜 Load version history (selected module)")
        self.load_versions_btn.clicked.connect(self.load_module_versions)
        btns.addWidget(self.load_versions_btn)
        self.rollback_version_btn = QPushButton("⏪ Roll back to selected version")
        self.rollback_version_btn.clicked.connect(self.rollback_module_version)
        btns.addWidget(self.rollback_version_btn)
        btns.addStretch(1)
        self.module_status_refresh_btn = QPushButton("🔄 Refresh")
        self.module_status_refresh_btn.clicked.connect(self.refresh_module_status)
        btns.addWidget(self.module_status_refresh_btn)
        lay.addLayout(btns)

        page.setLayout(lay)
        return page

    def refresh_module_status(self):
        try:
            modules = asyncio.run(list_module_registry())
        except Exception as e:
            self.note(f"⚠️ Failed to load module status: {e}")
            return

        self.module_status_table.setRowCount(len(modules))
        for row, m in enumerate(modules):
            fill_row(self.module_status_table, row, [
                m["name"], m["version"], m["status"], m["source"] or "",
                m.get("access_scope") or "", to_local(m["updated_at"])])
        self.module_status_table.resizeRowsToContents()

    def load_module_versions(self):
        rows = selected_rows(self.module_status_table)

        if not rows:
            self.note("⚠️ Select a module in the table above first.")
            return

        if len(rows) > 1:
            self.note("⚠️ Select only one module at a time to load its version history.")
            return

        name_item = self.module_status_table.item(rows[0], 0)
        if not name_item:
            return

        module_name = name_item.text()
        # Remembered so rollback_module_version() knows which module the
        # version table below is actually showing — the versions table
        # itself has no module-name column, just version/created_at.
        self._versions_loaded_for = module_name

        try:
            versions = asyncio.run(fetch_module_versions(module_name))
        except Exception as e:
            self.note(f"⚠️ Failed to load versions for '{module_name}': {e}")
            return

        self.module_versions_table.setRowCount(len(versions))
        for row, v in enumerate(versions):
            fill_row(self.module_versions_table, row, [v["version"], to_local(v["created_at"])])

        self.note(f"[SYSTEM] Loaded {len(versions)} version(s) for '{module_name}'")

    def rollback_module_version(self):
        """Reinstalls a selected past version's code as current — recorded
        as a brand-new version (register_module_version), never a
        destructive overwrite of history. Reuses the module's own
        currently-granted access_scope (from get_module_registry_entry())
        for check_safety()'s re-validation — a module already trusted
        with e.g. db access shouldn't suddenly fail rollback validation
        just because install_module()'s default is fully sandboxed."""
        module_name = self._versions_loaded_for
        if not module_name:
            self.note("⚠️ Load a module's version history first (select it above, then Load Version History).")
            return

        rows = selected_rows(self.module_versions_table)
        if not rows:
            self.note("⚠️ Select a version in the version history table first.")
            return

        version_item = self.module_versions_table.item(rows[0], 0)
        if not version_item:
            return

        version = int(version_item.text())

        reply = QMessageBox.question(
            self, "Confirm Rollback",
            f"Roll back '{module_name}' to version {version}?\n\n"
            f"This installs that version's code as current and records the "
            f"rollback itself as a new version.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            code = asyncio.run(get_module_version_code(module_name, version))
            if not code:
                self.note(f"⚠️ No stored code found for '{module_name}' v{version}.")
                return

            registry_entry = asyncio.run(get_module_registry_entry(module_name)) or {}
            access_scope = registry_entry.get("access_scope")
            allowed_scopes = {access_scope} if access_scope else None

            success, result = asyncio.run(install_module(module_name, code, allowed_scopes=allowed_scopes))
            if not success:
                self.note(f"⚠️ Rollback install failed for '{module_name}': {result}")
                return

            new_version = asyncio.run(register_module_version(
                module_name, code, "controller_rollback",
                source="rollback", access_scope=access_scope
            ))
            self.note(
                f"[SYSTEM] Rolled back '{module_name}' to v{version}'s code (now v{new_version}) via Controller"
            )
        except Exception as e:
            self.note(f"⚠️ Rollback failed for '{module_name}': {e}")
            return

        self.refresh_module_status()
        self.load_module_versions()

    # =====================================================================
    # HEALTH
    # =====================================================================
    def _build_health(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel(
            "Proactive fault check — her own diagnostic_tool module, run on demand. "
            "It used to run at every connect; it is a real sweep, so it runs only when asked."))
        self.fault_check_output = QTextEdit()
        self.fault_check_output.setReadOnly(True)
        lay.addWidget(self.fault_check_output)
        btns = QHBoxLayout()
        self.run_fault_check_btn = QPushButton("🩺 Run diagnostic check now")
        self.run_fault_check_btn.clicked.connect(self.run_fault_check)
        btns.addWidget(self.run_fault_check_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def run_fault_check(self):
        self.fault_check_output.setPlainText("Running diagnostic check...")
        self.fault_check_output.repaint()
        self.fault_check_output.setPlainText(actions.run_fault_check())

    # =====================================================================
    def refresh_all(self):
        self.refresh_personality()
        self.refresh_beliefs()
        self.refresh_decisions()
        self.refresh_curiosity()
        self.refresh_module_status()
