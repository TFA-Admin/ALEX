# systems/inquiry/system.py

"""
Inquiry System (2026-07-16)

Detects an explicit request to search the web ("look up X", "search for
X") and runs it through the real two-stage gate: one approval to
actually go online and search, a separate approval to retain what was
found. Mirrors the propose-then-confirm pattern already proven reliable
tonight for elevated-access approval and module builds — loose trigger
detection (STT noise on connective words doesn't matter, it only
produces a proposal), commit only on an explicit "yes".

Explicit-trigger only, deliberately — she never searches on her own
initiative yet (Craig, 2026-07-16: "explicit-only now, self-triggered
later"). Plain LLM-knowledge answers are untouched by this — this system
only ever fires for a real, stated request to search.
"""
import time
from datetime import datetime, timedelta, timezone

from core.system_base import BaseSystem
from core.text_utils import first_word, strip_trailing_punctuation
from core.embedding_engine import embed
from core.phrasebook import get_phrase
from module_runtime.module_loader import load_module
from config.logger_config import logger

from systems.controller._role_gates import require_creator

from db.db import (
    create_query_report, resolve_search_approval, attach_search_findings,
    resolve_retain_approval, get_query_report, find_related_knowledge,
    create_learned_knowledge, fetch_pending_search_approvals
)

# user_id -> {"stage": "search"|"retain", "report_id": int, "query": str,
# "proposed_at": float} — same short-lived, module-level, timeout-backed
# pending-confirmation shape as systems/modules/system.py's pending_builds
# and systems/controller/_module_admin.py's _pending_access_approvals.
_pending = {}

PENDING_TIMEOUT = 60

# 2026-07-18 (Craig, after a retained finding said race results "aren't
# posted yet": "what happens when that information changes... isn't
# that an issue?") — a live web search is inherently a snapshot of a
# moment in time, not a timeless fact; without this, the exact same
# question later would just replay the stale answer forever instead of
# reflecting that the world moved on. 24h is a reasoned starting point
# (long enough to answer a same-day follow-up, short enough that stale
# current-events info doesn't linger) — not tuned against real usage.
RETAINED_SEARCH_TTL_HOURS = 24

SEARCH_TRIGGERS = ("look up", "search for", "search the web for", "google")


def _extract_query(text: str):
    lower = text.lower()
    for trigger in SEARCH_TRIGGERS:
        if trigger in lower:
            idx = lower.rfind(trigger)
            after = text[idx + len(trigger):].strip()
            after = strip_trailing_punctuation(after)
            if after:
                return after
    return None


class System(BaseSystem):

    name = "inquiry"
    priority = 9  # after diagnostics(9)/memory(8), before modules(10)/llm(100) — actual order is core/alex_core.py's init_systems() call sequence, not this number

    async def init(self):
        print("🔎 Inquiry system ready")

    async def diagnose(self):
        """Real check: confirms the inquiry module actually loads
        (validates its network-scope grant is intact) and that the
        query_reports table is reachable — the two things every branch
        below depends on."""
        module = await load_module("inquiry")
        if not module:
            return False, "inquiry module failed to load (missing, or failed its scope check)"

        try:
            await fetch_pending_search_approvals()
        except Exception as e:
            return False, f"fetch_pending_search_approvals() raised: {e}"

        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):
        text = input_data.get("text", "")
        if not text:
            return None

        msg = text.lower().strip()

        # -------------------------
        # PENDING CONFIRMATION (search approval, or retain approval) —
        # checked first, independent of everything else, same reasoning
        # as every other pending-confirmation flow tonight.
        # -------------------------
        if user_id in _pending:
            pending = _pending[user_id]

            if time.time() - pending["proposed_at"] > PENDING_TIMEOUT:
                # 2026-07-16: found live — this used to unconditionally
                # answer with the "timed out" phrase, which meant whatever
                # the user actually said next (a real example: a brand
                # new "look up X" request, said ~70s after an earlier
                # unresolved retain prompt) got silently swallowed and
                # replaced with "your search expired" instead of ever
                # reaching search-trigger detection below. Same bug the
                # "neither yes nor no" branch further down was already
                # fixed for, just missed here — a stale pending question
                # shouldn't eat a genuinely new, unrelated message. Clear
                # it and fall through instead of replying here.
                del _pending[user_id]
                return None

            word = first_word(msg)

            if word in ("yes", "y", "yeah", "confirm"):
                denial = await require_creator(user_id, session, text)
                if denial:
                    del _pending[user_id]
                    return denial

                if pending["stage"] == "search":
                    return await self._run_search_stage(user_id, pending)
                return await self._run_retain_stage(user_id, pending)

            if word in ("no", "n"):
                report_id = pending["report_id"]
                stage = pending["stage"]
                del _pending[user_id]

                if stage == "search":
                    await resolve_search_approval(report_id, False)
                    logger.info(f"[ACTION] Search declined for request #{report_id} (by {user_id})")
                    return {"type": "response", "content": await get_phrase("search_declined")}

                await resolve_retain_approval(report_id, False)
                logger.info(f"[ACTION] Retain declined for request #{report_id} (by {user_id})")
                return {"type": "response", "content": await get_phrase("retain_declined")}

            # Neither yes nor no. 2026-07-16 (Craig: noticing she might
            # mistake an unrelated later reply for an answer to this) —
            # used to leave this pending for up to PENDING_TIMEOUT more
            # seconds, meaning a genuinely unrelated "yes" to something
            # else entirely, said within that window, would have wrongly
            # resolved THIS stale question instead. A reply that isn't
            # yes/no means the person has moved on, not that they're
            # still mid-answer — clear it now rather than leave a window
            # for a later unrelated confirmation to be misattributed.
            # This message still falls through normally (e.g. to the LLM
            # fallback for something like "thank you") — it's just no
            # longer treated as a non-answer to the search/retain
            # question.
            del _pending[user_id]
            return None

        # -------------------------
        # DETECT SEARCH TRIGGER
        # -------------------------
        query = _extract_query(text)
        if not query:
            return None

        report_id = await create_query_report(user_id, query, text)
        _pending[user_id] = {
            "stage": "search", "report_id": report_id,
            "query": query, "proposed_at": time.time()
        }

        logger.info(f"[ACTION] Search proposed for {user_id}: '{query}' (request #{report_id}, awaiting approval)")

        return {
            "type": "response",
            "content": await get_phrase("search_approval_proposed", query=query)
        }

    async def _run_search_stage(self, user_id: str, pending: dict):
        report_id = pending["report_id"]
        query = pending["query"]

        await resolve_search_approval(report_id, True)
        logger.info(f"[ACTION] Search approved for request #{report_id} (by {user_id})")

        module = await load_module("inquiry")
        if not module:
            del _pending[user_id]
            return {
                "type": "response",
                "content": await get_phrase("search_module_unavailable")
            }

        try:
            findings, sources = await module.run_search(query)
        except Exception as e:
            del _pending[user_id]
            logger.warning(f"[ACTION] Search for request #{report_id} raised: {e}")
            return {
                "type": "response",
                "content": await get_phrase("search_failed")
            }

        await attach_search_findings(report_id, findings, sources)

        _pending[user_id] = {
            "stage": "retain", "report_id": report_id,
            "query": query, "proposed_at": time.time()
        }

        return {
            "type": "response",
            "content": await get_phrase("search_findings_ask_retain", findings=findings)
        }

    async def _run_retain_stage(self, user_id: str, pending: dict):
        report_id = pending["report_id"]
        del _pending[user_id]

        kid, supersedes = await retain_report(report_id)
        if kid is None:
            return {"type": "response", "content": await get_phrase("search_report_not_found")}

        logger.info(f"[ACTION] Retained knowledge #{kid} from request #{report_id} (by {user_id})")

        if supersedes:
            return {"type": "response", "content": await get_phrase("retained_replacing_prior")}
        return {"type": "response", "content": await get_phrase("retained_new")}


# 2026-07-16: Craig noticed several query_reports stuck in
# 'pending_retain_approval' forever (real example: he asked to search for
# something, then moved on to a different topic before ever saying yes/no
# to the retain question). Root cause: the "waiting for yes/no" state
# (_pending above) only ever lives in memory, tied to this process — once
# it restarts (common during active development), that state is gone with
# no way back into it through conversation, and the DB row is stuck
# forever. These two module-level functions are the same promote/decline
# logic _run_retain_stage() above uses, factored out so
# ALEX_Controller.py can resolve a stale one directly by report ID,
# without needing a live _pending entry or going through conversation at
# all.
async def retain_report(report_id: int):
    """Promotes a query_report's findings into learned_knowledge.
    Returns (kid, supersedes) — kid is None if the report doesn't exist."""
    report = await get_query_report(report_id)
    if not report:
        return None, None

    await resolve_retain_approval(report_id, True)

    # Supersede detection — a real correction should replace what she
    # already believed, not just pile up a second, contradictory entry
    # next to it (Component 11's belief-revision requirement).
    related = await find_related_knowledge(report["query"])
    supersedes = related[0]["id"] if related else None

    # Stored as a plain "YYYY-MM-DD HH:MM:SS" string in UTC, matching
    # SQLite's own datetime('now') default (also UTC) — fetch_active_knowledge()'s
    # comparison depends on both sides using the same clock.
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=RETAINED_SEARCH_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    vec = embed(report["findings"])
    kid = await create_learned_knowledge(
        report["query"], report["findings"], report["sources"],
        report_id, vec, supersedes=supersedes, user=report["requested_by"],
        expires_at=expires_at
    )

    return kid, supersedes


async def decline_report(report_id: int) -> bool:
    """Resolves a stuck retain approval without promoting anything.
    Returns False if the report doesn't exist."""
    report = await get_query_report(report_id)
    if not report:
        return False
    await resolve_retain_approval(report_id, False)
    return True
