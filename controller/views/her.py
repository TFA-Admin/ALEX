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
    QSlider, QGridLayout, QCheckBox, QComboBox, QSpinBox, QScrollArea,
)
from PySide6.QtCore import Qt
import time as _time
from core import traits as her_traits, mood as her_mood

from db.db import (
    get_personality, set_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases, add_personality_hard_rule,
    get_personality_hard_rules, remove_personality_hard_rule,
    get_personality_locked, set_personality_locked,
    get_personality_traits, set_personality_traits, get_mood_state,
    fetch_observations, get_retention_summary,
    fetch_recent_personality_changes,
    fetch_active_conclusions, fetch_decisions, fetch_curiosity_queue, fetch_eval_runs,
    fetch_projects, create_project, update_project, PROJECT_STATUSES, record_decision,
    list_module_registry, fetch_module_versions, get_module_version_code,
    get_module_registry_entry, register_module_version,
    persona_disabled, PERSONA_FLAG_PATH,
)
from core.intent_classifier import merge_personality_change
from module_runtime.module_installer import install_module

from controller.common import make_readable, fill_row, selected_rows, to_local, tint_by_column
from controller import actions



def _scrolling(page):
    """A page inside a scroll area: it takes the room it is given and
    scrolls for the rest, instead of demanding its full height of the
    window. The pop-out takes the scroll area with it."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(page)
    return area


class _PoppedWindow(QWidget):
    """A tab of hers in its own window. Closing it puts the tab back."""

    def __init__(self, page, title, owner, index):
        super().__init__(None, Qt.Window)
        self.setWindowTitle(f"A.L.E.X — {title}")
        self.resize(900, 700)
        self._page, self._title, self._owner, self._index = page, title, owner, index
        lay = QVBoxLayout()
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(page)
        # A page removed from a QTabWidget carries an explicit hide; a new
        # layout does not undo that (Craig: "when I actually open that tab
        # nothing shows").
        page.show()
        self.setLayout(lay)

    def closeEvent(self, event):
        self.layout().removeWidget(self._page)
        self._owner._pop_back(self._page, self._title, self._index)
        super().closeEvent(event)


class HerView(QWidget):
    def __init__(self, note):
        super().__init__()
        self.note = note
        self._beliefs = []
        self._versions_loaded_for = None

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        # 2026-09-23 (Craig: "have it as a separate application inside the
        # controller that I can open... maybe we should be doing that with
        # any of the more complex systems"). Any inner tab can be popped
        # out into its own window and comes back to its place on close.
        pop_row = QHBoxLayout()
        pop_row.addStretch(1)
        self.pop_out_btn = QPushButton("⧉ Open this tab in its own window")
        self.pop_out_btn.clicked.connect(self.pop_out_current)
        pop_row.addWidget(self.pop_out_btn)
        layout.addLayout(pop_row)
        self._popped = {}
        self.inner = QTabWidget()
        # 2026-09-25: the tallest page scrolls instead of setting the
        # window's minimum height (it asked for 787 px).
        self.inner.addTab(_scrolling(self._build_personality()), "Personality")
        self.inner.addTab(self._build_beliefs(), "Beliefs")
        self.inner.addTab(self._build_decisions(), "Decisions")
        self.inner.addTab(self._build_curiosity(), "Curiosity")
        self.inner.addTab(self._build_modules(), "Modules")
        self.inner.addTab(self._build_health(), "Health")
        self.inner.addTab(self._build_scores(), "Scores")
        self.inner.addTab(self._build_projects(), "Projects")
        layout.addWidget(self.inner)
        self.setLayout(layout)

    # =====================================================================
    # POP-OUT (2026-09-23)
    # =====================================================================
    def pop_out_current(self):
        idx = self.inner.currentIndex()
        if idx < 0:
            return
        page = self.inner.widget(idx)
        title = self.inner.tabText(idx)
        self.inner.removeTab(idx)
        win = _PoppedWindow(page, title, self, idx)
        self._popped[title] = win
        win.show()

    def _pop_back(self, page, title, idx):
        self._popped.pop(title, None)
        page.setParent(None)
        self.inner.insertTab(min(idx, self.inner.count()), page, title)
        self.inner.setCurrentWidget(page)

    # =====================================================================
    # PERSONALITY
    # =====================================================================
    def _build_personality(self):
        page = QWidget()
        lay = QVBoxLayout()

        # 2026-09-21 (Craig: "The other section does not permit me to make
        # direct changes"). The description is his to write as one piece:
        # edited here and saved exactly as typed — no model merge, no rule
        # added. The nudge box further down still does the merge for the
        # "be a bit more X" case, and says so.
        lay.addWidget(QLabel("Her personality, as she reads it on every turn — edit it here and save; "
                             "it is kept exactly as written:"))
        self.personality_view = QTextEdit()
        self.personality_view.setMaximumHeight(140)
        lay.addWidget(self.personality_view)

        btns = QHBoxLayout()
        self.save_personality_btn = QPushButton("💾 Save description as written")
        self.save_personality_btn.clicked.connect(self.save_personality_text)
        btns.addWidget(self.save_personality_btn)
        # 2026-09-21: his text was overwritten within the hour by a spoken
        # sentence the classifier took for a "set your personality" command.
        # Saving here locks it; while locked, reflection, voice commands
        # (even with the code) and the nudge box below cannot rewrite it.
        from PySide6.QtWidgets import QCheckBox
        self.personality_lock_box = QCheckBox("🔒 Locked — only this editor may change it")
        self.personality_lock_box.setToolTip(
            "While locked: reflection will not rewrite her personality, a spoken 'set your "
            "personality' is refused even with the override code, and the nudge box is off. "
            "Saving the description locks it; untick to allow her to evolve it again.")
        self.personality_lock_box.stateChanged.connect(self._lock_changed)
        btns.addWidget(self.personality_lock_box)
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
        btns.addStretch(1)
        self.personality_refresh_btn = QPushButton("🔄 Refresh")
        self.personality_refresh_btn.clicked.connect(self.refresh_personality)
        btns.addWidget(self.personality_refresh_btn)
        lay.addLayout(btns)
        # 2026-09-25 (Craig: "why is the controller so much larger now?"):
        # seven controls on one row were 2044 px of minimum width, and the
        # widest row on any tab is the width of the window. The resets and
        # the persona switch sit on their own row.
        btns2 = QHBoxLayout()
        for b in (self.reset_personality_btn, self.reset_phrases_btn, self.persona_toggle_btn):
            btns2.addWidget(b)
        btns2.addStretch(1)
        lay.addLayout(btns2)
        self._refresh_persona_button()

        # -------------------------
        # DIALS (2026-09-22)
        # -------------------------
        # Craig: "would it be possible to link her persona to slider bars?"
        # Seven dials, rendered into her prompt as words each turn
        # (core/traits.py); verbosity also caps the reply in code.
        # 2026-09-23 (Craig: "instead of having them be a slider, make
        # them just adjust whatever the value is up or down... if something
        # isn't quite right where the slider would be maxed out, I would
        # still be able to make adjustment"): signed offsets with no
        # ceiling, and her mood's own temporary offset shown beside his.
        lay.addWidget(QLabel(
            "Adjustments — each pushes that trait up or down from the written description, with no ceiling "
            "(0 = as written, ±1 a little, ±2–3 strongly, ±4–5 extreme, ±6 and beyond overrides the description). "
            "Her mood adds a temporary amount of its own; the right column is what she is rendered with now. "
            "Verbosity also caps her reply length in code."))
        grid = QGridLayout()
        for col, head in enumerate(("Trait", "His adjustment", "Mood now", "Effective")):
            h = QLabel(f"<b>{head}</b>")
            grid.addWidget(h, 0, col)
        self.dial_spins = {}
        self.dial_mood_lbls = {}
        self.dial_effective_lbls = {}
        self._mood_state = None
        self._mood_offsets_cache = {}
        for row_i, (key, label, _less, _more) in enumerate(her_traits.TRAITS, start=1):
            grid.addWidget(QLabel(label), row_i, 0)
            spin = QSpinBox()
            spin.setRange(-her_traits.LIMIT, her_traits.LIMIT)
            spin.setValue(0)
            spin.setMinimumWidth(70)
            spin.valueChanged.connect(lambda v, k=key: self._dial_moved(k, v))
            grid.addWidget(spin, row_i, 1)
            mood_lbl = QLabel("0")
            mood_lbl.setStyleSheet("color: gray;")
            eff_lbl = QLabel("0")
            grid.addWidget(mood_lbl, row_i, 2)
            grid.addWidget(eff_lbl, row_i, 3)
            self.dial_spins[key] = spin
            self.dial_mood_lbls[key] = mood_lbl
            self.dial_effective_lbls[key] = eff_lbl
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        self.dial_preview = QLabel()
        self.dial_preview.setWordWrap(True)
        self.dial_preview.setStyleSheet("color: gray;")
        lay.addWidget(self.dial_preview)
        dial_btns = QHBoxLayout()
        self.apply_dials_btn = QPushButton("✅ Apply adjustments")
        self.apply_dials_btn.clicked.connect(self.apply_dials)
        dial_btns.addWidget(self.apply_dials_btn)
        self.reset_dials_btn = QPushButton("♻️ As written (all 0)")
        self.reset_dials_btn.clicked.connect(self.reset_dials)
        dial_btns.addWidget(self.reset_dials_btn)
        self.reload_dials_btn = QPushButton("🔄 Reload (mood moves)")
        self.reload_dials_btn.clicked.connect(self.load_dials)
        dial_btns.addWidget(self.reload_dials_btn)
        dial_btns.addStretch(1)
        lay.addLayout(dial_btns)

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
        self.add_hard_rule_btn = QPushButton("➕ Add rule (verbatim)")
        self.add_hard_rule_btn.clicked.connect(self.add_hard_rule)
        rule_btns.addWidget(self.add_hard_rule_btn)
        self.edit_hard_rule_btn = QPushButton("✏️ Edit selected rule")
        self.edit_hard_rule_btn.clicked.connect(self.edit_selected_hard_rule)
        rule_btns.addWidget(self.edit_hard_rule_btn)
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
        lay.addWidget(QLabel("Nudge her (the model merges this into the description above AND keeps it as a "
                             "standing rule — for \"be a bit more X\"; to write the voice yourself, edit above):"))
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
        try:
            self._refreshing_lock = True
            self.personality_lock_box.setChecked(asyncio.run(get_personality_locked()))
        except Exception:
            pass
        finally:
            self._refreshing_lock = False
        self._refresh_persona_button()
        self.refresh_hard_rules()
        self.load_dials()
        self.refresh_mood()

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

    def _dial_moved(self, key, value):
        self._preview_dials()

    def _current_dials(self) -> dict:
        return {k: s.value() for k, s in self.dial_spins.items()}

    def _read_mood(self):
        try:
            self._mood_state = asyncio.run(get_mood_state()) or her_mood.fresh()
        except Exception as e:
            self.note(f"⚠️ Failed to read her mood: {e}")
            self._mood_state = her_mood.fresh()
        self._mood_offsets_cache = her_mood.dial_offsets(self._mood_state)
        return self._mood_offsets_cache

    def _preview_dials(self):
        mood_offsets = self._mood_offsets_cache or {}
        standing = self._current_dials()
        eff = her_traits.effective(standing, mood_offsets) or {k: 0.0 for k in her_traits.KEYS}
        for key in her_traits.KEYS:
            mo = mood_offsets.get(key, 0.0)
            self.dial_mood_lbls[key].setText(f"{mo:+.1f}" if abs(mo) >= 0.05 else "0")
            self.dial_effective_lbls[key].setText(her_traits.signed(eff[key]))
        mood_line = her_mood.line(self._mood_state) if self._mood_state else ""
        text = her_traits.render(eff, mood_line=mood_line)
        self.dial_preview.setText(text.strip() or "Nothing adjusted; the description is the voice.")

    def load_dials(self):
        try:
            traits = asyncio.run(get_personality_traits())
        except Exception as e:
            self.note(f"⚠️ Failed to load her adjustments: {e}")
            traits = None
        traits = her_traits.normalize(traits)
        for key, spin in self.dial_spins.items():
            spin.blockSignals(True)
            spin.setValue(traits[key])
            spin.blockSignals(False)
        self._read_mood()
        self._preview_dials()

    def apply_dials(self):
        traits = self._current_dials()
        try:
            asyncio.run(set_personality_traits(traits))
            asyncio.run(log_personality_change(her_traits.dumps(traits), "creator set the adjustments at the Controller", kind="dials"))
            self.note("[SYSTEM] Adjustments applied: " + ", ".join(f"{k} {her_traits.signed(v)}" for k, v in traits.items()))
        except Exception as e:
            self.note(f"⚠️ Failed to apply the adjustments: {e}")

    def reset_dials(self):
        for key, spin in self.dial_spins.items():
            spin.setValue(0)

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
            if asyncio.run(get_personality_locked()):
                QMessageBox.information(self, "Personality", "The description is locked. Edit it above, or untick the lock to nudge it.")
                return
        except Exception:
            pass

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

    def save_personality_text(self):
        """The description exactly as he typed it. No merge, no rule."""
        text = self.personality_view.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Personality", "The description cannot be empty. Use Reset for the default.")
            return
        try:
            asyncio.run(set_personality(text))
            asyncio.run(set_personality_locked(True))
            asyncio.run(log_personality_change(text, "creator wrote it directly at the Controller (locked)", kind="personality"))
            self.note("[SYSTEM] Personality description saved as written and locked")
        except Exception as e:
            self.note(f"⚠️ Failed to save the description: {e}")
        self.refresh_personality()

    def _lock_changed(self, _state):
        if getattr(self, "_refreshing_lock", False):
            return
        locked = bool(self.personality_lock_box.isChecked())
        try:
            asyncio.run(set_personality_locked(locked))
            self.note("[SYSTEM] Personality " + ("LOCKED — only this editor changes it" if locked
                                                 else "unlocked — reflection and voice commands may change it again"))
        except Exception as e:
            self.note(f"⚠️ Failed to change the lock: {e}")

    def add_hard_rule(self):
        from PySide6.QtWidgets import QInputDialog
        rule, ok = QInputDialog.getMultiLineText(
            self, "Add standing rule", "Kept word for word, never rewritten, above her personality:", "")
        rule = (rule or "").strip()
        if not ok or not rule:
            return
        try:
            asyncio.run(add_personality_hard_rule(rule))
            asyncio.run(log_personality_change(rule, "creator added a standing rule at the Controller", kind="hard_rule"))
            self.note(f"[SYSTEM] Standing rule added: {rule}")
        except Exception as e:
            self.note(f"⚠️ Failed to add the rule: {e}")
        self.refresh_hard_rules()

    def edit_selected_hard_rule(self):
        from PySide6.QtWidgets import QInputDialog
        items = self.hard_rules_list.selectedItems()
        if not items:
            self.note("⚠️ Select a rule to edit first.")
            return
        old = items[0].text()
        new, ok = QInputDialog.getMultiLineText(self, "Edit standing rule", "Replaces the selected rule, word for word:", old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        try:
            asyncio.run(remove_personality_hard_rule(old))
            asyncio.run(add_personality_hard_rule(new))
            asyncio.run(log_personality_change(new, f"creator edited a standing rule at the Controller (was: {old[:80]})", kind="hard_rule"))
            self.note(f"[SYSTEM] Standing rule edited: {new}")
        except Exception as e:
            self.note(f"⚠️ Failed to edit the rule: {e}")
        self.refresh_hard_rules()

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
        self.beliefs_show_retracted = QCheckBox("Show retracted")
        self.beliefs_show_retracted.stateChanged.connect(lambda _: self.refresh_beliefs())
        btns.addWidget(self.beliefs_show_retracted)
        self.beliefs_refresh_btn = QPushButton("🔄 Refresh")
        self.beliefs_refresh_btn.clicked.connect(self.refresh_beliefs)
        btns.addWidget(self.beliefs_refresh_btn)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def refresh_beliefs(self):
        try:
            rows = asyncio.run(fetch_active_conclusions(limit=50))
            if self.beliefs_show_retracted.isChecked():
                rows = rows + asyncio.run(fetch_active_conclusions(limit=30, status="retracted"))
        except Exception as e:
            self.note(f"⚠️ Failed to load her beliefs: {e}")
            rows = []

        self._beliefs = rows
        self.beliefs_table.setRowCount(len(rows))
        for row, c in enumerate(rows):
            fill_row(self.beliefs_table, row, [
                c["id"],
                {"confirmed": "confirmed by you", "retracted": "retracted"}.get(c.get("status"), "hers, unconfirmed"),
                c["kind"], c["statement"], c["evidence"] or ""])
        tint_by_column(self.beliefs_table, 1)
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
        btns.addWidget(QLabel("Show:"))
        self.reasoning_kind = QComboBox()
        self.reasoning_kind.addItem("all kinds")
        self.reasoning_kind.currentTextChanged.connect(lambda _: self.refresh_decisions())
        btns.addWidget(self.reasoning_kind)
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
        kinds = sorted({d["kind"] for d in rows if d.get("kind")})
        have = [self.reasoning_kind.itemText(i) for i in range(self.reasoning_kind.count())]
        for k in kinds:
            if k not in have:
                self.reasoning_kind.addItem(k)
        chosen = self.reasoning_kind.currentText()
        if chosen and chosen != "all kinds":
            rows = [d for d in rows if d.get("kind") == chosen]

        self.reasoning_table.setRowCount(len(rows))
        for row, d in enumerate(rows):
            fill_row(self.reasoning_table, row, [
                to_local(d["created_at"]), d["kind"], d["summary"],
                d["reasoning"] or "", d["evidence"] or "", d["outcome"] or ""])
        tint_by_column(self.reasoning_table, 1)
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
        # 2026-09-23 (roadmap item 10): her mood, the numbers and why.
        lay.addWidget(QLabel(
            "<b>Her mood</b> — three axes that things happening to her move and that fade on their own "
            "(irritation ~30 min, engagement ~10 min, strain ~5 min; core/mood.py). "
            "What she is rendered with because of it is on the Personality tab."))
        self.mood_output = QTextEdit()
        self.mood_output.setReadOnly(True)
        self.mood_output.setMaximumHeight(320)
        lay.addWidget(self.mood_output)
        mood_btns = QHBoxLayout()
        self.refresh_mood_btn = QPushButton("🔄 Refresh mood")
        self.refresh_mood_btn.clicked.connect(self.refresh_mood)
        mood_btns.addWidget(self.refresh_mood_btn)
        mood_btns.addStretch(1)
        lay.addLayout(mood_btns)
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

    def refresh_mood(self):
        try:
            st = asyncio.run(get_mood_state()) or her_mood.fresh()
        except Exception as e:
            self.mood_output.setPlainText(f"Could not read her mood: {e}")
            return
        axes = her_mood.decayed(st)
        lines = ["   ".join(f"{a}: {axes[a]:.1f}/10" for a in her_mood.AXES)]
        lines.append(her_mood.line(st) or "Calm — nothing about it in her prompt.")
        offs = her_mood.dial_offsets(st)
        if offs:
            lines.append("Trait offsets from it now: " + ", ".join(f"{k} {v:+.1f}" for k, v in offs.items()))
        lines.append("")
        lines.append("What moved it (newest first):")
        events = list(st.get("events") or [])
        if not events:
            lines.append("  nothing yet")
        for e in reversed(events[-14:]):
            when = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(float(e.get("t", 0))))
            delta = ", ".join(f"{a} {d:+.1f}" for a, d in (e.get("delta") or {}).items())
            lines.append(f"  {when}  {e.get('note') or e.get('event')}  ({delta})")
        # 2026-09-23: what her glances found, and what retention removed
        try:
            obs = asyncio.run(fetch_observations(None, hours=24.0, limit=6))
        except Exception:
            obs = []
        lines.append("")
        lines.append("Through the camera, last 24 h (glances; text only, kept 14 days):")
        if not obs:
            lines.append("  nothing noticed")
        for o in obs:
            lines.append(f"  {to_local(o.get('created_at'))}  [{o.get('user')}{', ' + o['face'] if o.get('face') else ''}]  {o.get('text')}")
        try:
            ret = asyncio.run(get_retention_summary())
        except Exception:
            ret = None
        if ret:
            removed = {k: v for k, v in (ret.get("removed") or {}).items() if v}
            lines.append("")
            lines.append("Retention (daily, core/retention.py): last run "
                         + _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(ret.get("at", 0))))
                         + (f", removed {removed}" if removed else ", nothing to remove"))
        self.mood_output.setPlainText("\n".join(lines))

    def run_fault_check(self):
        self.fault_check_output.setPlainText("Running diagnostic check...")
        self.fault_check_output.repaint()
        self.fault_check_output.setPlainText(actions.run_fault_check())

    # =====================================================================
    # SCORES (2026-09-21, roadmap item 5)
    # =====================================================================
    # Every harness run, newest first. The same rows she reads through
    # my_scores, so what she says about herself and what this shows are
    # one record. Item 6's approve/reject gate will read these too.
    def _build_scores(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel(
            "Her measured scores — every run of tests/harness.py, newest first. "
            "Run one with:  python -X utf8 -m tests.harness <suite> --trials N --note \"what changed\""))
        self.scores_table = QTableWidget()
        self.scores_table.setColumnCount(8)
        self.scores_table.setHorizontalHeaderLabels(
            ["When", "Suite", "Score", "Trials", "Her model", "Judge", "Commit", "By category / note"])
        self.scores_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scores_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.scores_table, wrap_column=7)
        lay.addWidget(self.scores_table)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.scores_refresh_btn = QPushButton("🔄 Refresh")
        self.scores_refresh_btn.clicked.connect(self.refresh_scores)
        btns.addWidget(self.scores_refresh_btn)
        lay.addLayout(btns)
        page.setLayout(lay)
        return page

    def refresh_scores(self):
        try:
            rows = asyncio.run(fetch_eval_runs(limit=100))
        except Exception as e:
            self.note(f"⚠️ Failed to load her scores: {e}")
            rows = []

        self.scores_table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            cats = r.get("by_category") or {}
            detail = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in cats.items()
                               if isinstance(v, (list, tuple)) and len(v) == 2)
            if r.get("failures"):
                detail += (" — failed: " if detail else "failed: ") + ", ".join(map(str, r["failures"][:12]))
                if len(r["failures"]) > 12:
                    detail += f" (+{len(r['failures']) - 12})"
            if r.get("note"):
                detail = f"{r['note']} | {detail}" if detail else r["note"]
            commit = (r.get("commit_hash") or "") + ("+dirty" if r.get("dirty") else "")
            fill_row(self.scores_table, row, [
                to_local(r.get("created_at")), r["suite"], f"{r['passed']}/{r['total']}",
                r.get("trials") or 1, r.get("model") or "", r.get("judge_model") or "",
                commit, detail])
        self.scores_table.resizeRowsToContents()

    # =====================================================================
    # PROJECTS (2026-09-21)
    # =====================================================================
    # Craig: "She also claims to want to know what projects we have in
    # store for her, and I say we give them to her." What he writes here
    # she reads with my_projects; every status change is also a decisions
    # row, so she can see progress happen.
    def _build_projects(self):
        page = QWidget()
        lay = QVBoxLayout()
        lay.addWidget(QLabel(
            "What you have in store for her. She reads this list herself (my_projects); "
            "moving a status is recorded so she can see progress."))
        self.projects_table = QTableWidget()
        self.projects_table.setColumnCount(5)
        self.projects_table.setHorizontalHeaderLabels(["#", "Status", "Project", "Notes", "Last moved"])
        self.projects_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.projects_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.projects_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.projects_table, wrap_column=3)
        lay.addWidget(self.projects_table)
        btns = QHBoxLayout()
        self.project_add_btn = QPushButton("➕ Add project")
        self.project_add_btn.clicked.connect(self.add_project)
        self.project_status_btn = QPushButton("▸ Set status")
        self.project_status_btn.clicked.connect(self.set_project_status)
        self.project_notes_btn = QPushButton("✏️ Edit notes")
        self.project_notes_btn.clicked.connect(self.edit_project_notes)
        for b in (self.project_add_btn, self.project_status_btn, self.project_notes_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        self.projects_refresh_btn = QPushButton("🔄 Refresh")
        self.projects_refresh_btn.clicked.connect(self.refresh_projects)
        btns.addWidget(self.projects_refresh_btn)
        lay.addLayout(btns)
        self._projects = []
        page.setLayout(lay)
        return page

    def refresh_projects(self):
        try:
            self._projects = asyncio.run(fetch_projects())
        except Exception as e:
            self.note(f"⚠️ Failed to load projects: {e}")
            self._projects = []
        self.projects_table.setRowCount(len(self._projects))
        for row, p in enumerate(self._projects):
            fill_row(self.projects_table, row, [
                p["id"], p["status"], p["title"], p.get("notes") or "", to_local(p.get("updated_at"))])
        tint_by_column(self.projects_table, 1)
        self.projects_table.resizeRowsToContents()

    def _selected_project(self):
        rows = selected_rows(self.projects_table)
        if not rows or rows[0] >= len(self._projects):
            QMessageBox.information(self, "Projects", "Select a project first.")
            return None
        return self._projects[rows[0]]

    def add_project(self):
        from PySide6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(self, "Add project", "What is it? One line, in her terms.")
        if not ok or not title.strip():
            return
        status, ok = QInputDialog.getItem(self, "Add project", "Status:", list(PROJECT_STATUSES), 1, False)
        if not ok:
            return
        try:
            pid = asyncio.run(create_project(title.strip(), status))
            asyncio.run(record_decision(
                "project", f"He added a project for her: {title.strip()}",
                reasoning="His plan, written at the Controller.", outcome=f"status {status}",
                actor="craig", ref=f"projects#{pid}"))
            self.note(f"[SYSTEM] Project #{pid} added: {title.strip()} ({status})")
        except Exception as e:
            self.note(f"⚠️ Failed to add project: {e}")
        self.refresh_projects()

    def set_project_status(self):
        from PySide6.QtWidgets import QInputDialog
        p = self._selected_project()
        if not p:
            return
        current = list(PROJECT_STATUSES).index(p["status"]) if p["status"] in PROJECT_STATUSES else 0
        status, ok = QInputDialog.getItem(self, "Set status", p["title"], list(PROJECT_STATUSES), current, False)
        if not ok or status == p["status"]:
            return
        try:
            asyncio.run(update_project(p["id"], status=status))
            asyncio.run(record_decision(
                "project", f"He moved '{p['title']}' from {p['status']} to {status}",
                reasoning="His call, at the Controller.", evidence=p.get("notes") or "",
                outcome=f"now {status}", actor="craig", ref=f"projects#{p['id']}"))
            self.note(f"[SYSTEM] Project #{p['id']} -> {status}")
        except Exception as e:
            self.note(f"⚠️ Failed to set status: {e}")
        self.refresh_projects()

    def edit_project_notes(self):
        from PySide6.QtWidgets import QInputDialog
        p = self._selected_project()
        if not p:
            return
        notes, ok = QInputDialog.getMultiLineText(self, "Edit notes", p["title"], p.get("notes") or "")
        if not ok:
            return
        try:
            asyncio.run(update_project(p["id"], notes=notes.strip()))
            self.note(f"[SYSTEM] Project #{p['id']} notes updated")
        except Exception as e:
            self.note(f"⚠️ Failed to update notes: {e}")
        self.refresh_projects()

    def refresh_all(self):
        self.refresh_personality()
        self.refresh_beliefs()
        self.refresh_decisions()
        self.refresh_curiosity()
        self.refresh_module_status()
        self.refresh_scores()
        self.refresh_projects()
