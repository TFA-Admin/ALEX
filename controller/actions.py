# controller/actions.py
"""The things Craig does to her from the Controller that touch the
database — one function each, shared by the Inbox (where an item waits
for him) and by Her (where the same item is reviewed later). Each returns
after logging what it did through `note`, which appends to the A.L.E.X.
console.

Writes go directly to the same sqlite DB the live ALEX process reads —
no HTTP/WS round-trip needed since this Controller runs on the same
machine. Blocking asyncio.run() calls are fine here: these are one-off
admin actions from a button click, not a hot path."""
import asyncio

from PySide6.QtWidgets import (
    QMessageBox, QInputDialog, QDialog, QVBoxLayout, QLabel, QTextEdit, QPushButton,
)

from db.db import (
    resolve_module_build_request, approve_elevated_access,
    confirm_conclusion, retract_conclusion, record_decision,
    resolve_search_approval, attach_search_findings, get_query_report,
    acknowledge_security_events, acknowledge_personality_changes,
)


# ---------------- BELIEFS ----------------
def confirm_belief(parent, belief: dict, note) -> bool:
    """Craig says a belief of hers is true. From then on it is part of
    how she reads him — see systems/llm/system.py, which reads
    status='confirmed' only. The gate exists because the unconfirmed
    version was in her prompt for one night (2026-09-20/21) and she spent
    it arguing: four beliefs that he was "testing" and "provoking" her had
    her reading a plain correction as provocation, and reflection then
    concluded from the argument that he was provoking her."""
    if belief.get("status") == "confirmed":
        QMessageBox.information(parent, "Beliefs", "Already confirmed.")
        return False

    confirm = QMessageBox.question(
        parent, "Confirm belief",
        "Confirm this as true? From now on it shapes how she reads "
        "you.\n\n" + belief["statement"],
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if confirm != QMessageBox.Yes:
        return False

    try:
        ok = asyncio.run(confirm_conclusion(belief["id"]))
        if ok:
            asyncio.run(record_decision(
                "confirmation", f"He confirmed: {belief['statement']}",
                reasoning="His call, at the Controller — not hers.",
                outcome="from now on this reaches her replies",
                actor="craig", ref=f"conclusions#{belief['id']}"))
            note(f"[SYSTEM] Confirmed belief #{belief['id']}")
        else:
            note(f"[SYSTEM] Belief #{belief['id']} could not be confirmed — it is no longer active")
        return bool(ok)
    except Exception as e:
        note(f"⚠️ Failed to confirm belief: {e}")
        return False


def retract_belief(parent, belief: dict, note) -> bool:
    """Craig says a belief of hers is wrong. She keeps the reason."""
    reason, ok = QInputDialog.getText(
        parent, "Retract belief",
        "Why is this wrong? She keeps the reason.\n\n" + belief["statement"])
    if not ok:
        return False
    reason = reason.strip() or "retracted by Craig at the Controller"

    try:
        done = asyncio.run(retract_conclusion(belief["id"], reason))
        if done:
            asyncio.run(record_decision(
                "retraction", f"He retracted: {belief['statement']}",
                reasoning=reason,
                outcome="no longer held, and it never reaches her replies",
                actor="craig", ref=f"conclusions#{belief['id']}"))
            note(f"[SYSTEM] Retracted belief #{belief['id']}")
        else:
            note(f"[SYSTEM] Belief #{belief['id']} could not be retracted — it is no longer live")
        return bool(done)
    except Exception as e:
        note(f"⚠️ Failed to retract belief: {e}")
        return False


# ---------------- MODULE BUILDS ----------------
def cancel_build(request_id: int, module_name: str, note) -> bool:
    """Cancels an already-approved build request before Claude has built
    it. A creator's own confirmed build is auto-approved immediately (their
    "yes" IS the approval) and never sits in 'pending', so before this
    existed there was no way to cancel one before Claude picked it up.
    The caller only offers this on rows still 'approved'; anything already
    built/failed/denied is refused upstream so this can't silently rewrite
    a completed build's history."""
    try:
        asyncio.run(resolve_module_build_request(request_id, "denied"))
        note(f"[SYSTEM] Build request #{request_id} ('{module_name}') canceled via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to cancel request #{request_id}: {e}")
        return False


def approve_access(request_id: int, module_name: str, access_desc: str, note) -> bool:
    """Grants elevated access for a module Claude has flagged as needing
    real access beyond the plain sandbox — the second, separate approval
    from the build approval (see SELF_MODIFICATION_ARCHITECTURE.md's
    privilege-tier system). Voice ("approve request N") has worked since
    the propose-then-confirm redesign, but there was no GUI equivalent at
    all — found live when Craig couldn't approve request #18 any other
    way. Only offered on rows with a real requested_access not yet
    granted, so this can't double-grant or act on an unrelated row."""
    try:
        asyncio.run(approve_elevated_access(request_id))
        note(f"[SYSTEM] Elevated access approved for request #{request_id} ('{module_name}': {access_desc}) via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to approve access for request #{request_id}: {e}")
        return False


# ---------------- WEB SEARCH (two stages) ----------------
def run_search(report_id: int, query: str, note) -> bool:
    """Resolves a search's FIRST-stage approval directly by report ID —
    the actual search execution, not the later retain decision. Found live
    (2026-07-18): a non-creator user can propose a search but
    require_creator() blocks them from ever confirming one, and there was
    no Controller path back into that stuck 'pending_search_approval'
    state at all — same problem the retain buttons solve, one stage
    earlier. Mirrors _run_search_stage()'s real logic (resolve approval,
    run the actual search, attach findings) directly, rather than
    duplicating it as a second, drifting copy.

    Local import for the module load/run — run_search() has its own import
    weight, no reason to pay it on every Controller launch for a
    rarely-used button."""
    from module_runtime.module_loader import load_module

    async def _run():
        await resolve_search_approval(report_id, True)
        module = await load_module("inquiry")
        if not module:
            return None, "inquiry module unavailable"
        findings, sources = await module.run_search(query)
        await attach_search_findings(report_id, findings, sources)
        return findings, None

    try:
        findings, error = asyncio.run(_run())
        if error:
            note(f"⚠️ Search #{report_id} approved but failed to run: {error}")
            return False
        preview = (findings or "")[:200]
        note(f"[SYSTEM] Search #{report_id} ('{query}') ran via Controller — findings: {preview}")
        return True
    except Exception as e:
        note(f"⚠️ Failed to run search #{report_id}: {e}")
        return False


def decline_search(report_id: int, note) -> bool:
    try:
        asyncio.run(resolve_search_approval(report_id, False))
        note(f"[SYSTEM] Search #{report_id} declined via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to decline search #{report_id}: {e}")
        return False


def approve_retention(report_id: int, note) -> bool:
    """Resolves a search's retain approval directly by report ID. 2026-07-16
    (Craig: "I want a controller way to approve the web search
    retention") — several requests were found stuck in
    'pending_retain_approval' forever: the "waiting for yes/no" state only
    ever lives in memory (systems/inquiry/system.py's _pending), so once
    the process restarts or the conversation moves on before answering,
    there's no way back into it through voice/chat at all. Same
    promote/decline logic the live "yes"/"no" path uses
    (retain_report()/decline_report(), factored out for exactly this).

    Imports retain_report() locally, not at module level — found live
    (2026-07-16) that importing it at the top of the Controller dragged in
    sentence_transformers -> sklearn (for embedding search results)
    unconditionally on EVERY launch, over a UNC network path, just to have
    this rarely-used button available. See controller/__init__.py for the
    background preload that makes this import a cache hit."""
    from systems.inquiry.system import retain_report

    try:
        kid, supersedes = asyncio.run(retain_report(report_id))
        if kid is None:
            note(f"⚠️ Request #{report_id} no longer exists — skipped.")
            return False
        suffix = " (superseded a prior belief)" if supersedes else ""
        note(f"[SYSTEM] Retained knowledge #{kid} from request #{report_id} via Controller{suffix}")
        return True
    except Exception as e:
        note(f"⚠️ Failed to retain request #{report_id}: {e}")
        return False


def decline_retention(report_id: int, note) -> bool:
    # Also a local import — decline_report() doesn't itself need embed(),
    # but importing it from the same module as retain_report() would still
    # trigger the same heavy transitive import chain.
    from systems.inquiry.system import decline_report

    try:
        ok = asyncio.run(decline_report(report_id))
        if not ok:
            note(f"⚠️ Request #{report_id} no longer exists — skipped.")
            return False
        note(f"[SYSTEM] Retention declined for request #{report_id} via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to decline request #{report_id}: {e}")
        return False


def view_findings(parent, report_id: int, note):
    """Shows a search's actual findings/sources before anything has to be
    decided about it. 2026-07-18 (Craig, right after retaining a search
    result with no way to see it first: "I have no way to see the results
    of the search either so I would in this scenario be flying blind...
    Not great") — the two-stage retain approval is meaningless if the
    second stage can't be reviewed before deciding. The periodic list
    never fetches findings/sources (kept cheap on purpose); this fetches
    them on demand for one row. Read-only; safe on a row in any status (a
    not-yet-searched row just shows 'no findings yet')."""
    try:
        report = asyncio.run(get_query_report(report_id))
    except Exception as e:
        note(f"⚠️ Failed to load findings for request #{report_id}: {e}")
        return

    if not report:
        note(f"⚠️ Request #{report_id} no longer exists.")
        return

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Search Findings — Request #{report_id}")
    dialog.resize(640, 420)

    layout = QVBoxLayout()
    layout.addWidget(QLabel(f"Query: {report['query']}"))

    text = QTextEdit()
    text.setReadOnly(True)
    text.setPlainText(
        (report["findings"] or "(no findings yet — search hasn't run)")
        + "\n\nSources:\n"
        + (report["sources"] or "(none)")
    )
    layout.addWidget(text)

    close_btn = QPushButton("Close")
    close_btn.clicked.connect(dialog.accept)
    layout.addWidget(close_btn)

    dialog.setLayout(layout)
    dialog.exec()


# ---------------- NOTICES ----------------
# The security-event / personality-change briefings used to fire straight
# into the live avatar chat at connect (ws/ws_handlers.py): each one's own
# __START__/text/__END__ sequence pushed whatever real conversational
# response had just been shown into "Previous Messages" and replaced it
# with an administrative notice (2026-07-17, Craig: "her showing me what
# she changed dismissed what she said prior"). Surfaced in the Inbox
# instead — same DB sources, acknowledged only on a real click.
def ack_security(note) -> bool:
    try:
        asyncio.run(acknowledge_security_events())
        note("[SYSTEM] Security events acknowledged via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to acknowledge security events: {e}")
        return False


def ack_personality(note) -> bool:
    try:
        asyncio.run(acknowledge_personality_changes())
        note("[SYSTEM] Personality changes acknowledged via Controller")
        return True
    except Exception as e:
        note(f"⚠️ Failed to acknowledge personality changes: {e}")
        return False


# ---------------- HEALTH ----------------
def run_fault_check() -> str:
    """Re-runs the same diagnostic_tool sweep that used to fire
    automatically at every creator connect. Not a stored queue: it is a
    live sweep, so it runs on demand rather than showing stale history.
    Local imports — the module-runtime machinery isn't needed anywhere
    else in the Controller, so there's no reason to pay its import cost
    on every launch for a button that's clicked on demand."""
    from module_runtime.module_loader import load_module
    from module_runtime.module_executor import run_module

    async def _run():
        module = await load_module("diagnostic_tool")
        text, _ = await run_module(module, "run a diagnostic check", {}, "creator")
        return text

    try:
        result = asyncio.run(_run())
        return result or "No issues found."
    except Exception as e:
        return f"⚠️ Diagnostic check failed: {e}"
