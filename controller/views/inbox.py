# controller/views/inbox.py
"""Inbox — everything waiting on Craig, in one list.

2026-09-21. Before this, a thing waiting for him could be on any of four
tabs (Modules, Activity, Notifications, Reasoning), each with its own
table and its own buttons, and the Activity tab had become a backlog of
settled rows with the live ones somewhere in the middle. Now: one table
of open items, newest first, and the buttons under it change to whatever
the selected item can have done to it. Settled items move to History.

Kinds of item, and where each came from:
  build        a module build approved but not yet built       (Activity)
  access       a build asking for elevated access, not granted (Activity)
  search       a web search proposed, not yet run              (Activity)
  retain       a search that ran, retain-or-forget undecided   (Activity)
  belief       a belief of hers, unconfirmed                   (Reasoning)
  security     an unacknowledged security event                (Notifications)
  personality  an unacknowledged self-reflection change        (Notifications)
  version      a proposed version of her (roadmap item 6): requested by
               her, proposed by her author or Claude, or gated and
               waiting for his decision — the Versions tab has the rest
"""
import json
import asyncio
import webbrowser

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QTableWidget, QAbstractItemView, QMessageBox, QInputDialog, QCheckBox,
)
from PySide6.QtCore import QThread, Signal

from db.db import (
    fetch_recent_module_build_requests, fetch_recent_query_reports,
    fetch_active_conclusions, fetch_unacknowledged_security_events,
    fetch_unacknowledged_personality_changes,
)
from controller.common import make_readable, fill_row, selected_rows, to_local, tint_by_column
from controller import actions
from controller import versions


# Each action: (button label, slot). Slots are the three buttons under the
# table; an item lists the actions it can have, and the buttons take
# those labels — so a belief shows Confirm/Retract, a search Run/Decline/
# View, and nothing shows a button that cannot apply to it.
ACTIONS = {
    "approve_access": ("✅ Approve elevated access", 0),
    "run_search": ("🔎 Run search", 0),
    "approve_retain": ("✅ Approve retention", 0),
    "confirm_belief": ("✅ Confirm belief", 0),
    "ack_security": ("✅ Acknowledge all security events", 0),
    "ack_personality": ("✅ Acknowledge all personality changes", 0),
    "cancel_build": ("🚫 Cancel build", 1),
    "decline_search": ("🚫 Decline search", 1),
    "decline_retain": ("❌ Decline retention", 1),
    "retract_belief": ("❌ Retract belief", 1),
    "view_findings": ("👁️ View findings", 2),
    "author_version": ("✍️ Ask her author", 0),
    "launch_version": ("🧪 Launch staging", 0),
    "approve_version": ("✅ Approve version", 0),
    "reject_version": ("❌ Reject version", 1),
    "open_versions": ("📂 Open Versions", 2),
}


def _gate_summary(gate, long=False) -> str:
    """'intent 166/168, authority 11/12' from the JSON the gate stores."""
    if not gate:
        return ""
    try:
        data = json.loads(gate) if isinstance(gate, str) else gate
    except ValueError:
        return str(gate)[:80]
    parts = []
    for suite, r in (data or {}).items():
        if r.get("skipped"):
            parts.append(f"{suite} skipped" + (f" ({r['skipped']})" if long else ""))
        elif r.get("passed") is None:
            parts.append(f"{suite} failed to run")
        else:
            parts.append(f"{suite} {r['passed']}/{r['total']}")
    return ", ".join(parts)


class _GateThread(QThread):
    """The gate runs suites as subprocesses and can take minutes; the
    window must stay usable meanwhile."""
    line = Signal(str)
    done = Signal(dict)

    def __init__(self, proposal):
        super().__init__()
        self.proposal = proposal

    def run(self):
        try:
            results = versions.run_gate(self.proposal, self.line.emit)
        except Exception as e:
            results = {"error": str(e)}
            self.line.emit(f"⚠️ Gate failed: {e}")
        self.done.emit(results)


class InboxView(QWidget):
    def __init__(self, note):
        super().__init__()
        # 2026-09-23 (Craig: "I tried approving the liver search in the
        # controller and it didn't work"): every action wrote its outcome
        # to the A.L.E.X. console and nothing else, so a failure looked like
        # nothing. The last note is kept and a failed action shows it.
        self._raw_note = note
        self._last_note = ""
        self.note = self._capture_note
        self._items = []

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)

        self.inner = QTabWidget()

        # ---------------- WAITING ON YOU ----------------
        waiting = QWidget()
        waiting_layout = QVBoxLayout()
        waiting_layout.addWidget(QLabel(
            "Open items, newest first. Select one; the buttons below become what you can do with it."))

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["When", "Kind", "From", "Item", "Detail"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.table, wrap_column=3)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        waiting_layout.addWidget(self.table)

        btns = QHBoxLayout()
        self.action_btns = []
        for slot in range(3):
            b = QPushButton()
            b.setVisible(False)
            b.clicked.connect(lambda _=False, s=slot: self._act(s))
            btns.addWidget(b)
            self.action_btns.append(b)
        btns.addStretch(1)
        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        btns.addWidget(self.refresh_btn)
        waiting_layout.addLayout(btns)
        waiting.setLayout(waiting_layout)
        self.inner.addTab(waiting, "Waiting on you")
        self.inner.addTab(self._build_versions(), "Versions")

        # ---------------- HISTORY ----------------
        # The settled rows: every build request and every search, any
        # status, newest first — what the Activity tab used to be, without
        # the open items mixed in. 2026-07-16 (Craig: "a module build went
        # under activity instead of in the module tab"; "there is not web
        # search activity under activity") — both tables kept.
        history = QWidget()
        history_layout = QVBoxLayout()

        history_layout.addWidget(QLabel("Module builds (any status):"))
        self.builds_table = QTableWidget()
        self.builds_table.setColumnCount(9)
        self.builds_table.setHorizontalHeaderLabels(
            ["ID", "Requested By", "Module", "Status", "Result",
             "Resolved At", "Requested Access", "Access Approved", "Origin"])
        self.builds_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.builds_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.builds_table, wrap_column=4)
        history_layout.addWidget(self.builds_table)

        history_layout.addWidget(QLabel("Web searches (any status):"))
        self.searches_table = QTableWidget()
        self.searches_table.setColumnCount(7)
        self.searches_table.setHorizontalHeaderLabels(
            ["ID", "Requested By", "Query", "Status", "Created At",
             "Search Resolved At", "Retain Resolved At"])
        self.searches_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.searches_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.searches_table, wrap_column=2)
        history_layout.addWidget(self.searches_table)

        history_btns = QHBoxLayout()
        self.history_view_btn = QPushButton("👁️ View findings (selected search)")
        self.history_view_btn.clicked.connect(self._view_history_findings)
        history_btns.addWidget(self.history_view_btn)
        history_btns.addStretch(1)
        history_layout.addLayout(history_btns)

        history.setLayout(history_layout)
        self.inner.addTab(history, "History")

        layout.addWidget(self.inner)
        self.setLayout(layout)

    # ---------------- GATHERING ----------------
    def _gather(self) -> list:
        """Every open item, as dicts with the actions each allows. Each
        source is fetched on its own so one failing table does not empty
        the list."""
        items = []

        try:
            for r in asyncio.run(fetch_recent_module_build_requests(limit=50)):
                if r["status"] != "approved":
                    continue
                # "pending" access must also check status — a denied/built
                # request with access_approved still 0 isn't awaiting a
                # decision anymore, it's finished. Missing that check
                # (found live, 2026-07-16) mislabeled denied requests
                # #17/#20 as perpetually "pending".
                access_open = bool(r.get("requested_access")) and not r.get("access_approved")
                origin = "Claude (session)" if (r.get("origin") or "live_conversation") == "claude_session" \
                    else "Her (live conversation)"
                detail = f"approved, not yet built — via {origin}"
                acts = ["cancel_build"]
                if access_open:
                    detail += f"; asks for elevated access: {r['requested_access']} (not granted)"
                    acts = ["approve_access", "cancel_build"]
                items.append({
                    "kind": "build" if not access_open else "access",
                    "when": r["created_at"], "from": r["requested_by"],
                    "item": f"Build module '{r['module_name']}' (#{r['id']})",
                    "detail": detail, "actions": acts, "data": r})
        except Exception as e:
            self.note(f"⚠️ Failed to load build requests: {e}")

        try:
            for r in asyncio.run(fetch_recent_query_reports(limit=50)):
                if r["status"] == "pending_search_approval":
                    items.append({
                        "kind": "search", "when": r["created_at"], "from": r["requested_by"],
                        "item": f"Search the web: {r['query']} (#{r['id']})",
                        "detail": "proposed, not yet run",
                        "actions": ["run_search", "decline_search", "view_findings"], "data": r})
                elif r["status"] == "pending_retain_approval":
                    items.append({
                        "kind": "retain", "when": r.get("search_resolved_at") or r["created_at"],
                        "from": r["requested_by"],
                        "item": f"Keep what she found: {r['query']} (#{r['id']})",
                        "detail": "search ran; keep it or throw it away",
                        "actions": ["approve_retain", "decline_retain", "view_findings"], "data": r})
        except Exception as e:
            self.note(f"⚠️ Failed to load searches: {e}")

        try:
            for c in asyncio.run(fetch_active_conclusions(limit=50, status="active")):
                items.append({
                    "kind": "belief", "when": c.get("updated_at") or c.get("created_at"),
                    "from": "her", "item": c["statement"],
                    "detail": f"about {c['kind']} — based on: {c['evidence'] or ''}",
                    "actions": ["confirm_belief", "retract_belief"], "data": c})
        except Exception as e:
            self.note(f"⚠️ Failed to load her beliefs: {e}")

        try:
            for ev in asyncio.run(fetch_unacknowledged_security_events()):
                items.append({
                    "kind": "security", "when": ev["created_at"], "from": ev["user"],
                    "item": f"Security: {ev['event_type']}", "detail": ev["detail"],
                    "actions": ["ack_security"], "data": ev})
        except Exception as e:
            self.note(f"⚠️ Failed to load security events: {e}")

        try:
            for c in asyncio.run(fetch_unacknowledged_personality_changes()):
                items.append({
                    "kind": "personality", "when": c["created_at"], "from": "her (self-reflection)",
                    "item": f"She changed her {c['kind']}: {c['new_value']}",
                    "detail": f"because: {c['reason']}",
                    "actions": ["ack_personality"], "data": c})
        except Exception as e:
            self.note(f"⚠️ Failed to load personality changes: {e}")

        try:
            versions.build_authored(self.note)
            for p in versions.list_proposals(limit=30):
                if p["status"] == "requested":
                    acts, detail = ["author_version", "reject_version", "open_versions"], \
                        f"she asked to change {p.get('target')}: {p.get('rationale') or ''}"
                elif p["status"] == "proposed":
                    acts, detail = ["launch_version", "reject_version", "open_versions"], \
                        f"branch ready, not yet run — {p.get('rationale') or ''}"
                elif p["status"] == "gated":
                    acts, detail = ["approve_version", "reject_version", "open_versions"], \
                        "gated: " + _gate_summary(p.get("gate")) + " — decide"
                else:
                    continue
                items.append({
                    "kind": "version", "when": p.get("updated_at") or p.get("created_at"),
                    "from": p.get("author") or "?", "item": f"#{p['id']} {p['title']}",
                    "detail": detail, "actions": acts, "data": p})
        except Exception as e:
            self.note(f"⚠️ Failed to load proposals: {e}")

        items.sort(key=lambda i: str(i["when"] or ""), reverse=True)
        return items

    def count(self) -> int:
        return len(self._items)

    # ---------------- REFRESH ----------------
    def refresh(self):
        selected = self._selected_item()
        selected_key = (selected["kind"], str(selected["item"])) if selected else None

        self._items = self._gather()
        self.table.setRowCount(len(self._items))
        for row, it in enumerate(self._items):
            fill_row(self.table, row, [to_local(it["when"]), it["kind"], it["from"], it["item"], it["detail"]])
        tint_by_column(self.table, 1)
        self.table.resizeRowsToContents()

        # Hold the selection across the 5s refresh, or the buttons would
        # vanish under the mouse.
        if selected_key:
            for row, it in enumerate(self._items):
                if (it["kind"], str(it["item"])) == selected_key:
                    self.table.selectRow(row)
                    break
        self._selection_changed()
        self.refresh_history()
        self.refresh_versions()

    def refresh_history(self):
        try:
            requests = asyncio.run(fetch_recent_module_build_requests())
        except Exception as e:
            self.note(f"⚠️ Failed to load build history: {e}")
            requests = []

        self.builds_table.setRowCount(len(requests))
        for row, r in enumerate(requests):
            origin = r.get("origin") or "live_conversation"
            origin_label = "Claude (session)" if origin == "claude_session" else "Her (live conversation)"
            if not r.get("requested_access"):
                access_label = ""
            elif r.get("access_approved"):
                access_label = "yes"
            elif r["status"] == "approved":
                access_label = "pending"
            else:
                access_label = "—"
            fill_row(self.builds_table, row, [
                r["id"], r["requested_by"], r["module_name"], r["status"],
                r["result"] or "", to_local(r["resolved_at"]),
                r.get("requested_access") or "", access_label, origin_label])
        tint_by_column(self.builds_table, 3)
        self.builds_table.resizeRowsToContents()

        try:
            reports = asyncio.run(fetch_recent_query_reports())
        except Exception as e:
            self.note(f"⚠️ Failed to load search history: {e}")
            reports = []

        self.searches_table.setRowCount(len(reports))
        for row, r in enumerate(reports):
            fill_row(self.searches_table, row, [
                r["id"], r["requested_by"], r["query"], r["status"],
                to_local(r["created_at"]), to_local(r.get("search_resolved_at")),
                to_local(r.get("retain_resolved_at"))])
        tint_by_column(self.searches_table, 3)
        self.searches_table.resizeRowsToContents()

    def _capture_note(self, text):
        self._last_note = text
        self._raw_note(text)

    # ---------------- ACTING ----------------
    def _selected_item(self):
        rows = selected_rows(self.table)
        if not rows or rows[0] >= len(self._items):
            return None
        return self._items[rows[0]]

    def _selection_changed(self):
        item = self._selected_item()
        for b in self.action_btns:
            b.setVisible(False)
        if not item:
            return
        for key in item["actions"]:
            label, slot = ACTIONS[key]
            b = self.action_btns[slot]
            b.setText(label)
            b.setProperty("action", key)
            b.setVisible(True)

    def _act(self, slot: int):
        item = self._selected_item()
        if not item:
            return
        key = self.action_btns[slot].property("action")
        d = item["data"]

        ok = True
        self._last_note = ""
        if key == "cancel_build":
            ok = actions.cancel_build(d["id"], d["module_name"], self.note)
        elif key == "approve_access":
            ok = actions.approve_access(d["id"], d["module_name"], d.get("requested_access") or "", self.note)
        elif key == "run_search":
            ok = actions.run_search(d["id"], d["query"], self.note)
        elif key == "decline_search":
            ok = actions.decline_search(d["id"], self.note)
        elif key == "approve_retain":
            ok = actions.approve_retention(d["id"], self.note)
        elif key == "decline_retain":
            ok = actions.decline_retention(d["id"], self.note)
        elif key == "view_findings":
            actions.view_findings(self, d["id"], self.note)
            return
        elif key == "confirm_belief":
            actions.confirm_belief(self, d, self.note)
        elif key == "retract_belief":
            actions.retract_belief(self, d, self.note)
        elif key == "author_version":
            self._v_author(d)
        elif key == "launch_version":
            self._v_launch(d)
        elif key == "approve_version":
            self._v_approve(d)
        elif key == "reject_version":
            self._v_reject(d)
        elif key == "open_versions":
            self.inner.setCurrentIndex(1)
            self._select_version(d["id"])
            return
        elif key == "ack_security":
            ok = actions.ack_security(self.note)
        elif key == "ack_personality":
            ok = actions.ack_personality(self.note)

        if ok is False:
            QMessageBox.warning(self, "Not done", self._last_note or "The action did not complete — see the A.L.E.X. console.")
        self.refresh()
        self.changed()

    def changed(self):
        """Replaced by the app: the Inbox tab title carries the count."""

    # =====================================================================
    # VERSIONS (2026-09-21, roadmap item 6)
    # =====================================================================
    # A proposal is a branch in its own worktree (controller/versions.py).
    # Launch runs it as a second copy of her on port 5001 with a snapshot
    # of her database; Talk opens that copy's page; Gate runs the harness
    # against it and records the scores; Approve merges into main and
    # restarts her; Reject removes it and keeps the reason. Kill stops the
    # staging copy. He is the only one who can press any of these.
    def _build_versions(self):
        page = QWidget()
        lay = QVBoxLayout()
        self.versions_label = QLabel()
        lay.addWidget(self.versions_label)

        self.versions_table = QTableWidget()
        self.versions_table.setColumnCount(8)
        self.versions_table.setHorizontalHeaderLabels(
            ["#", "Status", "Author", "Title", "Target", "Gate", "Staging", "Updated"])
        self.versions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.versions_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.versions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        make_readable(self.versions_table, wrap_column=3)
        self.versions_table.itemSelectionChanged.connect(self._version_selection_changed)
        lay.addWidget(self.versions_table)

        self.version_detail = QLabel()
        self.version_detail.setWordWrap(True)
        lay.addWidget(self.version_detail)

        row1 = QHBoxLayout()
        self.v_author_btn = QPushButton("✍️ Ask her author")
        self.v_author_btn.clicked.connect(lambda: self._v_author(self._selected_version()))
        self.v_launch_btn = QPushButton("🧪 Launch staging")
        self.v_launch_btn.clicked.connect(lambda: self._v_launch(self._selected_version()))
        self.v_talk_btn = QPushButton("💬 Talk to it")
        self.v_talk_btn.clicked.connect(self._v_talk)
        self.v_gate_btn = QPushButton("🧮 Run the gate")
        self.v_gate_btn.clicked.connect(lambda: self._v_gate(self._selected_version()))
        self.v_kill_btn = QPushButton("⏹ Kill staging")
        self.v_kill_btn.clicked.connect(lambda: self._v_kill(self._selected_version()))
        for b in (self.v_author_btn, self.v_launch_btn, self.v_talk_btn, self.v_gate_btn, self.v_kill_btn):
            row1.addWidget(b)
        row1.addStretch(1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.v_approve_btn = QPushButton("✅ Approve — merge and restart her")
        self.v_approve_btn.clicked.connect(lambda: self._v_approve(self._selected_version()))
        self.v_reject_btn = QPushButton("❌ Reject with reason")
        self.v_reject_btn.clicked.connect(lambda: self._v_reject(self._selected_version()))
        row2.addWidget(self.v_approve_btn)
        row2.addWidget(self.v_reject_btn)
        row2.addStretch(1)
        self.v_show_settled = QCheckBox("Show settled (merged, rejected, declined)")
        self.v_show_settled.setToolTip("Off: only what is open or being tried. On: everything, oldest greyed.")
        self.v_show_settled.stateChanged.connect(lambda _: self.refresh_versions())
        row2.addWidget(self.v_show_settled)
        self.v_refresh_btn = QPushButton("🔄 Refresh")
        self.v_refresh_btn.clicked.connect(self.refresh_versions)
        row2.addWidget(self.v_refresh_btn)
        lay.addLayout(row2)

        self._proposals = []
        self._gate_thread = None
        page.setLayout(lay)
        self._version_selection_changed()
        return page

    # set by the app
    def selected_model(self) -> str:
        return ""

    def restart_her(self):
        """Replaced by the app with the Run view's restart."""

    def refresh_versions(self):
        selected = self._selected_version()
        keep = selected["id"] if selected else None
        try:
            self._proposals = versions.list_proposals(limit=50)
        except Exception as e:
            self.note(f"⚠️ Failed to load proposals: {e}")
            self._proposals = []
        # 2026-09-23: settled rows out of the way unless asked for
        if not self.v_show_settled.isChecked():
            self._proposals = [p for p in self._proposals
                               if p.get("status") in ("requested", "authored", "proposed", "gated")]
        up = versions.staging_up()
        self.versions_label.setText(
            f"Proposed versions of her. Staging port {versions.STAGING_PORT}: "
            + ("RUNNING" if up else "idle")
            + f". Worktrees under {versions.VERSIONS_ROOT}.")
        self.versions_table.setRowCount(len(self._proposals))
        for row, p in enumerate(self._proposals):
            staging = "running" if (up and p.get("staging_pid")) else ("" if not p.get("worktree") else "stopped")
            fill_row(self.versions_table, row, [
                p["id"], p["status"], p.get("author") or "", p["title"], p.get("target") or "",
                _gate_summary(p.get("gate")), staging, to_local(p.get("updated_at"))])
        tint_by_column(self.versions_table, 1)
        self.versions_table.resizeRowsToContents()
        if keep is not None:
            self._select_version(keep)
        self._version_selection_changed()

    def _select_version(self, pid: int):
        for row, p in enumerate(self._proposals):
            if p["id"] == pid:
                self.versions_table.selectRow(row)
                return

    def _selected_version(self):
        rows = selected_rows(self.versions_table)
        if not rows or rows[0] >= len(self._proposals):
            return None
        return self._proposals[rows[0]]

    def _version_selection_changed(self):
        p = self._selected_version()
        busy = self._gate_thread is not None and self._gate_thread.isRunning()
        status = p["status"] if p else None
        has_tree = bool(p and p.get("worktree"))
        self.v_author_btn.setEnabled(bool(p) and status == "requested" and not busy)
        self.v_launch_btn.setEnabled(has_tree and status in ("proposed", "gated") and not busy)
        self.v_talk_btn.setEnabled(versions.staging_up())
        self.v_gate_btn.setEnabled(has_tree and status in ("proposed", "gated") and not busy)
        self.v_kill_btn.setEnabled(versions.staging_up() and not busy)
        self.v_approve_btn.setEnabled(has_tree and status in ("proposed", "gated") and not busy)
        self.v_reject_btn.setEnabled(bool(p) and status in ("requested", "proposed", "gated") and not busy)
        if not p:
            self.version_detail.setText("Select a proposal.")
            return
        text = f"#{p['id']} — {p['title']}\nBy {p.get('author')}. {p.get('rationale') or ''}"
        if p.get("branch"):
            text += f"\nBranch {p['branch']}"
        if p.get("gate"):
            text += "\nGate: " + _gate_summary(p["gate"], long=True)
        if p.get("reason"):
            text += f"\nReason: {p['reason']}"
        self.version_detail.setText(text)

    def _v_author(self, p):
        if not p:
            return
        self.note(f"[VERSIONS] Asking her author about {p.get('target')}…")
        try:
            versions.author_from_request(p, self.note)
        except Exception as e:
            self.note(f"⚠️ Her author failed: {e}")
        self.refresh()
        self.changed()

    def _v_launch(self, p):
        if not p:
            return
        if versions.staging_up():
            confirm = QMessageBox.question(
                self, "Launch staging",
                "A staging copy is already running on port "
                f"{versions.STAGING_PORT}. Stop it and launch #{p['id']} instead?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if confirm != QMessageBox.Yes:
                return
        try:
            versions.launch_staging(p, self.selected_model() or "", self.note)
            self.note(f"[VERSIONS] Give it ~10s to come up, then Talk to it at {versions.STAGING_URL}")
        except Exception as e:
            self.note(f"⚠️ Launch failed: {e}")
        self.refresh_versions()

    def _v_talk(self):
        webbrowser.open(versions.STAGING_URL)

    def _v_kill(self, p):
        versions.kill_staging(p, self.note)
        self.refresh_versions()

    def _v_gate(self, p):
        if not p:
            return
        if self._gate_thread and self._gate_thread.isRunning():
            self.note("⚠️ A gate is already running.")
            return
        if not versions.staging_up():
            confirm = QMessageBox.question(
                self, "Run the gate",
                "Staging is not running, so only the deterministic suite (intent) can run; "
                "the judged suites will be skipped. Launch staging first for the full gate.\n\nRun anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if confirm != QMessageBox.Yes:
                return
        self._gate_thread = _GateThread(p)
        self._gate_thread.line.connect(self.note)
        self._gate_thread.done.connect(self._gate_done)
        self._gate_thread.start()
        self._version_selection_changed()

    def _gate_done(self, results):
        self.note(f"[VERSIONS] Gate finished: {results}")
        self.refresh()
        self.changed()

    def _v_approve(self, p):
        if not p:
            return
        gate = _gate_summary(p.get("gate"), long=True) if p.get("gate") else "NOT GATED"
        confirm = QMessageBox.question(
            self, "Approve version",
            f"Merge proposal #{p['id']} into main and restart her on it?\n\n{p['title']}\n\nGate: {gate}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        problem = None
        try:
            problem = versions.approve(p, self.note)
        except Exception as e:
            problem = f"{type(e).__name__}: {e}"
        if problem:
            # 2026-09-23: say it to his face, not only in the console
            QMessageBox.warning(self, "Not approved", f"Proposal #{p['id']} was not merged.{chr(10)}{chr(10)}{problem}")
        else:
            self.restart_her()
        self.refresh()
        self.changed()

    def _v_reject(self, p):
        if not p:
            return
        reason, ok = QInputDialog.getText(
            self, "Reject version", f"Why? The reason stays with #{p['id']}.\n\n{p['title']}")
        if not ok:
            return
        reason = reason.strip() or "rejected by Craig at the Controller"
        try:
            versions.reject(p, reason, self.note)
        except Exception as e:
            self.note(f"⚠️ Reject failed: {e}")
        self.refresh()
        self.changed()

    def _view_history_findings(self):
        rows = selected_rows(self.searches_table)
        if not rows:
            self.note("⚠️ Select a search first.")
            return
        id_item = self.searches_table.item(rows[0], 0)
        if id_item:
            actions.view_findings(self, int(id_item.text()), self.note)
