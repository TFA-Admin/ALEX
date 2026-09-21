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
"""
import asyncio

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget,
    QTableWidget, QAbstractItemView,
)

from db.db import (
    fetch_recent_module_build_requests, fetch_recent_query_reports,
    fetch_active_conclusions, fetch_unacknowledged_security_events,
    fetch_unacknowledged_personality_changes,
)
from controller.common import make_readable, fill_row, selected_rows, to_local
from controller import actions


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
}


class InboxView(QWidget):
    def __init__(self, note):
        super().__init__()
        self.note = note
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
        self.searches_table.resizeRowsToContents()

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

        if key == "cancel_build":
            actions.cancel_build(d["id"], d["module_name"], self.note)
        elif key == "approve_access":
            actions.approve_access(d["id"], d["module_name"], d.get("requested_access") or "", self.note)
        elif key == "run_search":
            actions.run_search(d["id"], d["query"], self.note)
        elif key == "decline_search":
            actions.decline_search(d["id"], self.note)
        elif key == "approve_retain":
            actions.approve_retention(d["id"], self.note)
        elif key == "decline_retain":
            actions.decline_retention(d["id"], self.note)
        elif key == "view_findings":
            actions.view_findings(self, d["id"], self.note)
            return
        elif key == "confirm_belief":
            actions.confirm_belief(self, d, self.note)
        elif key == "retract_belief":
            actions.retract_belief(self, d, self.note)
        elif key == "ack_security":
            actions.ack_security(self.note)
        elif key == "ack_personality":
            actions.ack_personality(self.note)

        self.refresh()
        self.changed()

    def changed(self):
        """Replaced by the app: the Inbox tab title carries the count."""

    def _view_history_findings(self):
        rows = selected_rows(self.searches_table)
        if not rows:
            self.note("⚠️ Select a search first.")
            return
        id_item = self.searches_table.item(rows[0], 0)
        if id_item:
            actions.view_findings(self, int(id_item.text()), self.note)
