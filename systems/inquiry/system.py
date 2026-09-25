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
import re
import time
from datetime import datetime, timedelta, timezone

from core.system_base import BaseSystem
from core.text_utils import first_word, strip_trailing_punctuation, YES_WORDS, NO_WORDS, yes_or_no
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

# 2026-09-25: "I am authorizing you to do a search ON recent AI architectural
# changes" matched none of these, so the request was never recognised at all
# and fell to the model, which announced a search it had not run.
SEARCH_TRIGGERS = ("look up", "search for", "search the web for", "google",
                   "search on", "search about", "search regarding")


# 2026-09-25: his approvals of the search question, in his own words from
# the live conversation. Only ever read while a search question is pending,
# so "proceed" cannot mean anything else here.
_APPROVES_RE = re.compile(
    r"\b(?:proceed|go ahead|the search|search it|search now|look it up|run it|do it|"
    r"i (?:am )?authoriz\w*|authoriz\w* you|you (?:are|have my) (?:authoriz\w*|permission)|"
    r"permission granted|approved|green light)\b", re.I)


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

            # 2026-09-25 (live, 16:23): he restated the request — "it's
            # simple. do a search for the architectural changes in ai
            # recently" — and `yes_or_no` read the "don't" earlier in that
            # sentence as a NO, so the gate DECLINED the search he was
            # asking for. A message that itself names a search is not an
            # answer to the pending question: drop that question and let
            # the trigger detection below propose the new, better query.
            if pending["stage"] == "search" and _extract_query(text):
                logger.info(f"[ACTION] Search request #{pending['report_id']} restated — re-proposing with his new wording")
                await resolve_search_approval(pending["report_id"], False)
                del _pending[user_id]
                return await self._propose(user_id, text)

            # 2026-09-23: the whole answer, not its first word — see
            # core/text_utils.yes_or_no for the "In fact, that's a fact" case.
            answer = yes_or_no(msg)

            # 2026-09-25 (live, 16:20-16:26): "proceed", "proceed with the
            # search", "perform the search", "I am authorizing you... do it"
            # were none of yes, no, or a new request, so each cleared the
            # question and fell to the model — which then said the search
            # was running. They are approvals of the question she just
            # asked; nothing else is in the room.
            if answer is None and _APPROVES_RE.search(msg):
                logger.info(f"[ACTION] Read {msg[:60]!r} as approval of request #{pending['report_id']}")
                answer = "yes"

            # 2026-09-21: the shared sets — see core/text_utils.YES_WORDS
            # for why "keep it" and "fact" have to count.
            if answer == "yes":
                denial = await require_creator(user_id, session, text)
                if denial:
                    del _pending[user_id]
                    return denial

                if pending["stage"] == "search":
                    return await self._run_search_stage(user_id, pending)
                return await self._run_retain_stage(user_id, pending)

            if answer == "no":
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
            # 2026-09-25: she is told the question lapsed, so she does not
            # announce a search that never ran (systems/llm/system.py reads
            # this). Live: "the web surfacing operation has commenced".
            if pending["stage"] == "search":
                session["search_dropped"] = {"query": pending.get("query", ""), "t": time.time()}
            del _pending[user_id]
            return None

        # -------------------------
        # DETECT SEARCH TRIGGER
        # -------------------------
        query = _extract_query(text)
        if not query:
            return None
        return await self._propose(user_id, text)

    async def _propose(self, user_id: str, text: str):
        """Record a search request and ask him to approve it. Principle 4:
        nothing reaches the network until he says yes."""
        query = _extract_query(text)
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

        if not sources:
            # 2026-09-25 (live, 16:29): "No search results found. - Fact or
            # trash bin?" — nothing found was offered to him as something to
            # keep. One retry on a shortened query (his were long sentences),
            # then say plainly that it found nothing.
            try:
                short_q = " ".join(query.split()[:6])
                if short_q != query:
                    logger.info(f"[ACTION] Search #{report_id} found nothing; retrying as {short_q!r}")
                    findings, sources = await module.run_search(short_q)
            except Exception as e:
                logger.warning(f"[ACTION] Search retry for #{report_id} raised: {e}")
        if not sources:
            del _pending[user_id]
            logger.info(f"[ACTION] Search for request #{report_id} found nothing")
            return {"type": "response", "content": await get_phrase("search_nothing_found", query=query)}

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
async def retain_report(report_id: int, ttl_hours=RETAINED_SEARCH_TTL_HOURS):
    """Promotes a query_report's findings into learned_knowledge.
    Returns (kid, supersedes) — kid is None if the report doesn't exist.

    ttl_hours=None stores it with NO expiry. 2026-09-20: added for the
    "want me to keep this?" offers in systems/llm/system.py. The 24h
    default is right for a web search result, which is a snapshot of
    something that may still be changing; it is wrong for something the
    creator was asked about directly and said yes to. Craig, on expiry:
    "I do want her to retain knowledge, not have her entire memory wiped
    after a month." An explicit yes is the strongest keep signal there
    is — stronger than the retrieval-based renewal in
    systems/llm/system.py — so it is not put on a clock at all."""
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
    expires_at = None
    if ttl_hours is not None:
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")

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
