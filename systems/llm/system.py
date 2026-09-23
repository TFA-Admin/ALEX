# systems/llm/system.py

"""
LLM System

Wraps ollama_manager into a plug-and-play system.

Handles:
- normal chat
- streaming responses
- memory integration (unchanged)

2026-07-16: the LLM is now treated as a second "search" backend, offline
and trusted (Craig's framing) — structurally parallel to the real web
search in systems/inquiry/system.py, but lighter-weight since it never
leaves the machine. Every message that reaches this system (this IS the
fallback — nothing else answered) first checks learned_knowledge for a
close match; if found, she answers from what she already knows,
deterministically, no fresh generation. If not, she generates as before,
but the result is no longer thrown away: after_response() decides what
becomes of it.

2026-09-20 — that decision is no longer hers to make silently. It used to
auto-store anything "factual", where factual meant "contains a question
mark", and an audit of the 16 entries stored that day found 14 of them
conversational, one being her own hallucination. Nothing is written
to learned_knowledge from this system any more. after_response() now either
flags a real conflict for the creator (unchanged), or — if
core/knowledge_filter.py judges the exchange to be durable knowledge — sets
up a question for her to ask him on her next turn, in her own words.
Applies to
EVERYTHING that reaches this fallback, not just factual questions
(Craig, 2026-07-16: "even something like a greeting should only need to
be checked once then stored. past that she should know it.") — but with
a lower confidence bar for casual conversation than for factual
reference (Craig, same session, after seeing the real similarity
numbers): confidently restating the wrong FACT is a real cost, so that
threshold stays strict; a slightly-off match on a greeting barely
matters, so that one can be much more forgiving.
"""

import re
import time

from core.system_base import BaseSystem
from core.embedding_engine import embed, cosine_similarity
from core.text_utils import (
    strip_trailing_punctuation, strip_emojis, extract_banned_phrases,
    PhraseSuppressor,
)
from llm.ollama_client import ollama_manager
from datetime import datetime, timezone, timedelta

from db.db import (
    get_personality, fetch_active_knowledge,
    touch_learned_knowledge, get_user_role, resolve_retain_approval,
    create_query_report, attach_search_findings, fetch_recent_memory,
    record_correction, fetch_corrections, get_user_role as _role,
    fetch_active_conclusions, record_decision, fetch_profile_names,
    answer_curiosity_question, fetch_answered_curiosity,
    get_personality_hard_rules, list_module_registry, fetch_decisions, get_personality_traits
)
from core.knowledge_filter import is_worth_keeping
from core import self_model, corrections as corr, tools as her_tools, deliberation, claims as her_claims, traits as her_traits, mood as her_mood


def _mood_slow(seconds: float, after: float = 12.0):
    """2026-09-23: a slow first token is strain (core/mood.py)."""
    if seconds > after:
        try:
            import asyncio as _asyncio
            _asyncio.create_task(her_mood.note("model_slow"))
        except Exception:
            pass
from systems.controller._role_gates import require_creator
from core.phrasebook import get_phrase
from config.logger_config import logger

from systems.inquiry.system import _pending, retain_report

# Deterministic, not a classifier call — same reasoning as
# CASUAL_PRESENCE_KEYWORDS in systems/diagnostics/system.py: a real
# question word or a literal "?" is a strong, cheap signal, and running
# an LLM classification on every single fallback message just to decide
# which threshold to use would add real latency for no clear accuracy
# win. Imperfect (won't catch every factual phrasing that skips a wh-word
# and a question mark), stated honestly, not claimed as a solved
# classifier.
FACTUAL_MARKERS = (
    "what", "when", "where", "who", "whom", "which", "why",
    "how many", "how much", "how old", "how far", "how long", "how tall"
)


def _is_factual_question(text: str) -> bool:
    lower = text.lower().strip()
    if "?" in lower:
        return True
    return any(lower.startswith(m + " ") for m in FACTUAL_MARKERS)


# 2026-07-16: Craig noticed she keeps responding to plain closing
# acknowledgments ("thank you") as if they were a new thing to answer,
# even right after she herself said something closing ("let me know if
# you need anything else"). Deterministic, not a classifier — same
# reasoning as FACTUAL_MARKERS above and the project-wide lesson that
# adding a new category to the shared classify_intent() prompt in
# core/intent_classifier.py caused a real accuracy collapse. Both lists
# are deliberately narrow: CLOSING_MARKERS only needs to catch phrases
# she actually says, and ACKNOWLEDGMENT_PHRASES requires an exact match
# (not substring) on purpose — "thanks, also can you check X" must still
# get a real response, only a bare acknowledgment with nothing else in
# it should suppress one.
CLOSING_MARKERS = (
    "let me know", "anything else", "feel free to ask",
    "here when you're ready", "here if you need", "just say the word",
)

ACKNOWLEDGMENT_PHRASES = {
    "thanks", "thank you", "thanks a lot", "thank you very much",
    "appreciate it", "got it", "sounds good", "okay", "ok",
    "alright", "cool", "perfect", "no problem", "will do",
}


def _is_closing_statement(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CLOSING_MARKERS)


def _is_bare_acknowledgment(text: str) -> bool:
    normalized = strip_trailing_punctuation(text.strip().lower())
    return normalized in ACKNOWLEDGMENT_PHRASES


# 2026-07-16: Craig noticed a genuinely bad cached answer ("should we?" ->
# a generic "not clear what you're asking" reply, stored once and then
# replayed — reworded, but still fundamentally the same non-answer —
# every time anyone said those same two words again). Root cause isn't
# the reword, it's that short, context-dependent utterances ("should
# we?", "yeah", "why", "sure") don't actually have a fixed meaning
# independent of whatever came before them, so caching by the utterance's
# own embedding alone is the wrong signal no matter how faithfully the
# stored answer gets replayed. Fix: a deterministic content-word check —
# same "no LLM call for a cheap check" reasoning as FACTUAL_MARKERS/
# ACKNOWLEDGMENT_PHRASES above, and the standing project-wide lesson that
# adding categories to the shared classifier degrades it — strip out
# function words (pronouns, auxiliaries/modals, articles, prepositions,
# conjunctions, bare fillers) and see if anything real is left. No real
# content word left means this utterance can't be answered/cached
# meaningfully on its own, regardless of how it's punctuated (this also
# quietly fixes "should we?" being misclassified as a FACTUAL question by
# _is_factual_question() just because it ends in "?" — a content-free
# utterance skips the storage shortcut either way now). Deliberately a
# broad, hand-curated set rather than a claim of real POS-tagging —
# starting point, not linguistically exhaustive.
# 2026-09-21: has_content_words() and its word set moved to core/text_utils
# so systems/intent/system.py can use the same test to decide whether the
# merged classification should also ask for deliberation needs.
from core.text_utils import has_content_words as _has_content_words


# Confident-match thresholds, split by Craig's explicit split
# (2026-07-16): stricter for factual reference (a wrong confident answer
# is a real cost), much more forgiving for casual conversation (natural
# phrasing variance is wide — real greeting-vs-greeting similarity
# scores tested at 0.63-0.70 — and a near-miss here barely matters).
# Neither number is claimed as fully tuned; starting points based on
# real embedding tests, not guesses.
FACTUAL_ANSWER_THRESHOLD = 0.85
CASUAL_ANSWER_THRESHOLD = 0.6

# 2026-09-20 — retention by use, not by prediction.
#
# Craig wanted an expiry on auto-stored knowledge without "her entire memory
# wiped after a month". Two things make that safe. First, this table is not her
# memory: conversation history lives in `memory` and durable facts about people
# live in `facts`, and nothing deletes from either. This is the cache of
# pre-formed answers — the thing that produced the chlorophyll line.
#
# Second, anything she actually uses never expires in practice. A new entry
# gets a short life; every real retrieval pushes it out again
# (db.touch_learned_knowledge). Something asked about repeatedly effectively
# becomes permanent, something never asked about again fades, and no
# classifier has to predict which is which in advance.
AUTO_KNOWLEDGE_TTL_DAYS = 7        # unproven: stored once, never yet reused
REUSED_KNOWLEDGE_TTL_DAYS = 30     # proven useful: earned by being retrieved

# 2026-09-20 — nothing is auto-stored any more; she asks instead.
#
# Craig: "stop auto-storing entirely - Do so. Can we make her ask if I want
# something store instead? But only if it's real knowledge, I dont want to be
# asked about everything." core/knowledge_filter.py is the "only if it's real
# knowledge" half. These two are the "don't ask about everything" half, and
# they are blunt on purpose — the filter is the precision instrument, these
# just stop a run of good candidates turning into a run of questions.
#
# Untuned. 120s is roughly "not twice in the same exchange".
OFFER_COOLDOWN_S = 120

# How long a queued offer stays relevant. She asks on the turn AFTER the one
# that produced the answer (after_response runs past __END__ — see
# _maybe_offer_to_keep), and if nothing she said next was hers to speak, it
# waits. Past this it is stale: asking "want me to keep that?" about
# something twenty minutes back is a non-sequitur.
OFFER_MAX_AGE_S = 600

# Answers to "want me to keep that?". Deliberately broader than yes/no, for
# the same reason as ws_audio.CONFIRM_WORDS: a real answer to a casual
# question is "sure"/"go ahead"/"nah", not a formal affirmative. Anything
# NOT in either set is treated as "he moved on" — see _resolve_keep_offer,
# which declines rather than leaving a row stuck the way the search retains
# used to (systems/inquiry/system.py's note on pending_retain_approval).
# 2026-09-21: one vocabulary for every yes-or-no she asks — see
# core/text_utils.YES_WORDS. These names are kept so the code below reads.
from core.text_utils import YES_WORDS as KEEP_YES, NO_WORDS as KEEP_NO

# user_id -> when she last offered. Module-level, like _pending, and lost on
# restart, which is harmless: the worst case is one extra offer.
_last_offer_at = {}

# How close a new FACTUAL message has to be to an existing entry to be
# treated as "about the same thing" for conflict detection, without
# being close enough to answer from directly. Casual conversation never
# goes through conflict detection at all — see after_response().
RELATED_THRESHOLD = 0.6

# How close a freshly-generated answer's content has to be to an
# existing entry's content to be treated as "says the same thing, not a
# real conflict" and therefore skipped rather than flagged.
CONTENT_MATCH_THRESHOLD = 0.85


class System(BaseSystem):

    name = "llm"
    priority = 100  # fallback system

    async def init(self):
        # ensure ollama is ready
        if not ollama_manager.ready:
            await ollama_manager.init()

    async def diagnose(self):
        """Checks get_personality() specifically, not Ollama reachability
        — that's already covered separately by diagnostic_tool's own
        dedicated check, and duplicating a real network call here would
        just make every diagnostic run slower for no new information."""
        try:
            await get_personality()
        except Exception as e:
            return False, f"get_personality() raised: {e}"
        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):

        user_input = input_data.get("text")
        if not user_input:
            return None

        # One-shot — set by systems/facts/system.py only on the exact
        # turn a fact was just forgotten (or a forget attempt was
        # blocked). Popped here, at the very top, rather than further
        # down past the early-return paths below (bare-acknowledgment
        # suppression, learned_knowledge cache hit) — those return before
        # ever reaching the context-building code further down, and a
        # value that missed being popped would otherwise sit in session
        # and get incorrectly picked up on a LATER, unrelated turn — the
        # exact class of "stale context bleeding into now" bug Craig
        # flagged (2026-07-18) about FACTS being referenced too eagerly.
        fact_action_context = session.pop("fact_action_context", "")

        # -------------------------
        # ANSWERING "WANT ME TO KEEP THAT?" (2026-09-20)
        # -------------------------
        # Checked before everything else in this system, including the
        # bare-acknowledgment suppression below — "okay" and "sure" are
        # both a plausible answer here AND in ACKNOWLEDGMENT_PHRASES, and
        # being silently swallowed would leave the row pending forever.
        keep_reply = await self._resolve_keep_offer(session, user_id, user_input)
        if keep_reply is not None:
            return keep_reply

        # -------------------------
        # THE ANSWER TO WHAT SHE ASKED (2026-09-20)
        # -------------------------
        # Craig: "does the curiosity queue retain my answers?" It did not —
        # she asked, he answered, and the answer became an ordinary memory
        # row with nothing tying it to the question. Four of the five
        # questions on file were asked twice and then dropped, having been
        # answered both times.
        #
        # Positional, like the awareness system's reason capture: the turn
        # straight after her question is the answer often enough, and being
        # wrong costs one stored answer that is merely irrelevant, against
        # a question she asked twice and learned nothing from.
        #
        # Deliberately requires something substantial — "yeah" or "ok" is
        # acknowledgement, not an answer, and storing it would close the
        # question while teaching her nothing.
        awaiting_topic = session.pop("awaiting_curiosity_answer", None)
        if awaiting_topic and len(user_input.split()) >= 4:
            try:
                if await answer_curiosity_question(awaiting_topic, user_input):
                    await record_decision(
                        "curiosity",
                        f"Got an answer about {awaiting_topic}",
                        reasoning="She asked, and this is what he said back — "
                                  "his words, not something she worked out.",
                        evidence=user_input[:300],
                        outcome="kept, and she will not ask again",
                        actor=user_id)
                    logger.info(
                        f"[ACTION] Curiosity about {awaiting_topic!r} answered: "
                        f"{user_input[:80]!r}")
                    await her_mood.note("curiosity_answered", who=user_id)
            except Exception as e:
                logger.warning(f"⚠️ could not keep the answer: {e}")
        elif awaiting_topic:
            # Too short to be an answer — keep waiting one more turn.
            session["awaiting_curiosity_answer"] = awaiting_topic

        # 2026-09-23: a real conversation engages her (core/mood.py).
        if len(user_input.split()) >= 25:
            await her_mood.note("substantive_turn", who=user_id)

        # -------------------------
        # "STOP SAYING THAT" (2026-09-20)
        # -------------------------
        # Craig: "I don't want banned phrases per say I just want her to
        # listen... she would review what she said for duplication and be
        # able to identify it." So she works out WHAT he means from her own
        # recent output rather than being handed a string, and a repeat of
        # the same correction escalates it.
        #
        # Detected here but never answered here — the response is still
        # hers, generated with a context block saying she has just been
        # pulled up. Same shape as the awareness notice, and for the same
        # reason: the fact is deterministic, the words are hers.
        if corr.is_correction(user_input):
            recent = await fetch_recent_memory(user_id, limit=5)
            # Everyone she knows by name, so "stop saying that" is never
            # read as "stop saying my name" — which is exactly what the
            # first live correction recorded.
            try:
                known = await fetch_profile_names()
            except Exception:
                known = []

            # What he SAID first. Only work it out from her own repetition
            # when he did not name it — guessing is the fallback, not the
            # method. Craig, on the first live correction getting this
            # backwards: "I did say what I wanted her to stop saying. And
            # that's the problem. I pointed her at it and she still got it
            # wrong."
            # 2026-09-21: all of them. "Stop saying hell and my name so
            # much" names two things; the single-phrase version recorded
            # one, "hell and my name", which she has never said.
            #
            # getattr, until the process restarts: this system hot-reloads
            # and core/corrections.py does not, so the running core module
            # may still be the one without named_targets().
            targets_fn = getattr(corr, "named_targets", None)
            if targets_fn:
                phrases = targets_fn(user_input, speaker_name=user_id)
            else:
                one = corr.named_target(user_input, speaker_name=user_id)
                phrases = [one] if one else []
            how = "he named it"

            if not phrases:
                found = corr.find_repeated(
                    [r["response"] for r in recent], never=known + [user_id])
                phrases = [found] if found else []
                how = "worked out from what she had been repeating"

            if phrases:
                # His corrections bind. Anyone else's are hers to weigh —
                # recorded either way so the decision is visible, never
                # silently dropped and never silently obeyed.
                try:
                    is_creator = await _role(user_id) == "creator"
                except Exception:
                    is_creator = False

                told = []
                for phrase in phrases:
                    strength = await record_correction(
                        user_id, phrase, honored=is_creator,
                        reason=None if is_creator else "not the creator — hers to weigh")
                    told.append((phrase, strength, is_creator))
                    await record_decision(
                        "correction",
                        f'Told to stop saying "{phrase}"',
                        reasoning=f"The rule decided this, not her — {how}.",
                        evidence=(f"his words: {user_input!r}" if how == "he named it"
                                  else f"said in {corr.MIN_OCCURRENCES}+ of her last 5 replies"),
                        outcome=(f"strength {strength} ({corr.consequence(strength)})"
                                 + ("" if is_creator else " — not the creator, so hers to weigh")),
                        actor=user_id)
                    logger.info(
                        f"[ACTION] Correction from {user_id}: {phrase!r} now at "
                        f"strength {strength} ({corr.consequence(strength)}), "
                        f"{'binding' if is_creator else 'advisory only'}")

                session["just_corrected"] = told
                await her_mood.note("corrected", who=user_id, creator=is_creator)
            else:
                # 2026-09-20 (Craig: "would she ask for clarification if I
                # were to say dont say that on the first utterance"). She
                # would not — find_repeated needs the same thing twice, so
                # a first-offence correction found nothing and she carried
                # on as if he had said nothing at all. Being corrected and
                # visibly not registering it is worse than the tic.
                session["correction_unclear"] = True
                logger.info(
                    f"[ACTION] {user_id} corrected her but nothing was "
                    f"repeated — she will ask what he meant")

        # -------------------------
        # SUPPRESS — a bare acknowledgment ("thanks") right after her own
        # closing-type statement ("let me know if you need anything else")
        # means the exchange is over, not a new thing to respond to. Both
        # conditions are required: a bare "okay" alone could plausibly be
        # answering a real question she asked in ordinary conversation
        # (any *pending confirmation* flow would already have claimed this
        # message before it ever reached this last-in-line system, so we
        # only get here when there's nothing formally pending) — gating on
        # her own last line actually looking closing-type is what keeps
        # this safe. {"type": "silence"} (not None) is deliberate: this is
        # the last system in the dispatch chain, so a None here would fall
        # through to SystemManager.route()'s own "No system handled the
        # input." fallback and get spoken anyway. A truthy, unrecognized
        # type short-circuits that fallback and response_handler.py
        # already no-ops silently on any type it doesn't recognize.
        # -------------------------
        if _is_bare_acknowledgment(user_input):
            last_turn = await fetch_recent_memory(user_id, limit=1)
            if last_turn and _is_closing_statement(last_turn[0]["response"]):
                logger.info(f"[ACTION] Suppressing response for {user_id}: bare acknowledgment after closing statement")
                return {"type": "silence"}

        # -------------------------
        # ANSWER FROM STORAGE IF SHE ALREADY KNOWS THIS — checked before
        # any generation happens. A real match means this exact question
        # (or near-paraphrase) was already asked, generated, and either
        # auto-stored or confirmed before — restate it deterministically
        # rather than regenerating (and risking a different answer) every
        # single time.
        # -------------------------
        t0 = time.time()
        query_vec = embed(user_input)
        active = await fetch_active_knowledge(user_id)

        best_entry, best_sim = None, 0.0
        for entry in active:
            sim = cosine_similarity(query_vec, entry["embedding"])
            if sim > best_sim:
                best_sim, best_entry = sim, entry
        logger.info(f"[TIMING] learned_knowledge retrieval check: {time.time() - t0:.2f}s")

        is_factual = _is_factual_question(user_input)
        has_content = _has_content_words(user_input)

        session["_llm_match"] = (best_entry, best_sim, is_factual, has_content)

        # 2026-09-20: CASUAL MATCHES ARE NEVER REPLAYED. Only a factual
        # question can be answered from stored knowledge now.
        #
        # Found live, and it is the clearest example yet of the thing this
        # whole project is trying not to be. Craig said "alex." — it matched
        # entry #132, topic "alex, you know who this is", at 0.77 against the
        # old casual threshold of 0.6, and she replied "Yeah, Craig, I've got
        # you pegged. Chlorophyll, much? Did you switch to neon green chewing
        # gum or what?" That was a one-off joke from a conversation on
        # 2026-07-18, stored with no expiry, recited two months later into a
        # conversation it had nothing to do with. Craig: "extremely out of
        # place, confusing actually... the whole point of her memory was for
        # her to have a reference not just grab old conversations and repeat
        # herself."
        #
        # Three things combined: 0.6 was loose, short utterances match loosely
        # (a bare "alex." scoring 0.77), and _has_content_words() passes "alex"
        # because it is not a function word. Rather than tune three numbers,
        # the category goes: a casual reply has no reuse value, because what
        # made it right was the moment it was said. Factual reference — the
        # actual point of learned_knowledge — is untouched at 0.85.
        if has_content and is_factual and best_entry and best_sim >= FACTUAL_ANSWER_THRESHOLD:
            logger.info(
                f"[ACTION] Answered {user_id} from learned_knowledge #{best_entry['id']} "
                f"(similarity={best_sim:.2f}, {'factual' if is_factual else 'casual'}): {user_input!r}"
            )
            # It just proved itself useful, so it earns more life. See
            # AUTO_KNOWLEDGE_TTL_DAYS above — this is what stops a short
            # default expiry from throwing away things she actually relies on.
            await touch_learned_knowledge(best_entry["id"], REUSED_KNOWLEDGE_TTL_DAYS)
            content = await self._reword_learned_answer(best_entry["content"])
            return {"type": "response", "content": content}

        fact_context = session.get("fact_context", "")
        memory_context = session.get("memory_context", "")

        context_blocks = []

        # 2026-09-21: the clock, always. First live probe of the tool loop:
        # asked the time, she did not call the tool and said "2026-09-20,
        # 14:35 on a Wednesday" — wrong date, wrong day, wrong hour. Time
        # is not a judgment call and should never have been hers to fetch
        # or guess; it is context, like his name. This is also the first
        # brick of roadmap item 11 (time awareness).
        context_blocks.append(
            "NOW: " + datetime.now().strftime("%A, %Y-%m-%d %H:%M local time"))

        # 2026-09-21: her real modules, by name, always. In two of five
        # live probes she was asked what modules she has, skipped the
        # tool, and invented "code generation" and "running scripts". A
        # name list is grounded data, not a rule about what to do with it
        # (Craig: "She should be able to derive my goal through speech");
        # with it in front of her there is nothing to invent, and the tool
        # remains the way to learn what each one does.
        try:
            names = [m["name"] for m in await list_module_registry()
                     if m.get("status") == "enabled"]
            context_blocks.append(
                "YOUR MODULES, by name: " + (", ".join(names) if names else "none")
                + ". Nothing else is a module of yours.")
        except Exception as e:
            logger.warning(f"⚠️ could not read the module registry: {e}")

        # 2026-09-21 (evening): the same grounding for his projects. Asked
        # "can you see the project list?" she said "I checked" four times
        # and had read nothing. The counts are real and always here; the
        # list itself is one lookup away (my_projects, or the deliberation
        # pass when the need scores high).
        try:
            from db.db import fetch_projects
            counts = {}
            for p in await fetch_projects():
                counts[p["status"]] = counts.get(p["status"], 0) + 1
            if counts:
                context_blocks.append(
                    "HIS PROJECTS FOR YOU, as he keeps them: "
                    + ", ".join(f"{counts.get(s, 0)} {s.replace('_', ' ')}"
                                for s in ("in_progress", "planned", "done", "backlog"))
                    + ". The list is real; read it with my_projects before describing it.")
        except Exception as e:
            logger.warning(f"⚠️ could not count his projects: {e}")

        if fact_context:
            context_blocks.append(f"FACTS:\n{fact_context}")

        if memory_context:
            context_blocks.append(f"MEMORY:\n{memory_context}")

        # -------------------------
        # LOOK BEFORE ANSWERING (2026-09-21, roadmap item 4)
        # -------------------------
        # One bounded structured call rates how much this question turns
        # on what was said before, her modules, her state, her log, her
        # code or her diagnostics; code applies the cutoff, runs the
        # read-only tools, and the results land here as context. Her
        # answer then streams exactly as before, and she can still call
        # tools herself on top. See core/deliberation.py for the two
        # measurements that chose this shape over thinking mode.
        #
        # Skipped for content-free utterances ("okay", "it's me"): there
        # is nothing to look up for them, and the pass costs ~2s.
        needs_seen = None      # the turn's need scores, kept for core/claims.py
        evidence_ran = False   # did the deliberation pass look anything up
        if has_content:
            try:
                # Scores usually arrive from systems/intent/system.py on the
                # same call as the intent (session["needs"]); only when they
                # did not does this assess on its own.
                needs = session.pop("needs", None)
                needs_seen = dict(needs) if isinstance(needs, dict) else None
                if isinstance(needs, dict) and session.get("diagnostic_context"):
                    # the diagnostics system already measured this turn —
                    # do not run it twice
                    needs["diagnostics"] = 0
                recent_lines = None
                if not isinstance(needs, dict):
                    recent_lines = [
                        f'He: "{(r["prompt"] or "")[:120]}" / You: "{(r["response"] or "")[:120]}"'
                        for r in await fetch_recent_memory(user_id, limit=3)
                        if not (r["prompt"] or "").startswith("(unprompted")]
                looked = await deliberation.look_before_answering(
                    user_input, user_id, recent_lines,
                    scores=needs if isinstance(needs, dict) else None)
            except Exception as e:
                logger.warning(f"⚠️ deliberation failed, answering without it: {e}")
                looked = ""
            if looked:
                context_blocks.append(looked)
                evidence_ran = her_claims.real_evidence(looked)
                if evidence_ran:
                    await her_mood.note("lookup_found", who=user_id)

        # 2026-09-21 (evening): her recent slips, as facts. core/claims.py
        # records a 'fabrication' decision when a reply claimed work she had
        # not done; the last few sit here as data about herself, which she
        # handles far better than a rule telling her not to.
        try:
            slips = [d for d in await fetch_decisions(limit=6, kind="fabrication")]
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            slips = [d for d in slips if str(d.get("created_at") or "") >= cutoff][:3]
            if slips:
                context_blocks.append(
                    "YOUR RECENT SLIPS (real, recorded by code): "
                    + " | ".join(f"{str(d['created_at'])[11:16]} UTC — {d['summary']}" for d in slips)
                    + ". Say what you looked up and what you did not; never claim a check you did not make.")
        except Exception as e:
            logger.warning(f"⚠️ could not read her slips: {e}")

        # 2026-09-21: a sentence she dropped (window lapsed), brought back by
        # her name. See ws/ws_handlers.py UNADDRESSED_RECALL_S.
        recalled = session.pop("recalled_unaddressed", None)
        if isinstance(recalled, dict) and recalled.get("text"):
            if recalled.get("mode") == "is_the_question":
                context_blocks.append(
                    "WHAT HE WANTS ANSWERED: a moment ago, while you were not listening, he said "
                    f"\"{recalled['text']}\". Then he said your name to get you to answer it. "
                    "Answer THAT; do not remark on having missed it unless it matters.")
            else:
                context_blocks.append(
                    "JUST BEFORE THIS, while you were not listening, he said "
                    f"\"{recalled['text']}\". He is probably referring to it now.")

        if fact_action_context:
            context_blocks.append(f"WHAT JUST HAPPENED (state this truthfully, nothing else):\n{fact_action_context}")

        # 2026-09-20 (Craig): "mid conversation even if you were to switch
        # something off, for her to notice and ask me why this is now off."
        #
        # Given as CONTEXT, not as a line to deliver. The fact is fixed — this
        # is genuinely off and she has not been told why — but the question is
        # hers to phrase, and she can fold it into whatever she was already
        # saying instead of emitting a notice. That is Design Principle 6
        # applied to behaviour rather than just wording, and it is the fix for
        # the hardcoded advisory Craig caught earlier the same day.
        #
        # Deliberately NOT urgent: she waits for whatever she is already
        # replying to rather than interrupting. Craig wants severity-based
        # timing eventually, which needs a sense of urgency she does not have
        # yet — see core/disabled_watch.py.
        pending_off = session.pop("pending_disabled_notice", None)
        if pending_off:
            items = "; ".join(pending_off.values())
            context_blocks.append(
                "SOMETHING OF YOURS IS SWITCHED OFF, and you have not been told "
                f"why: {items}.\nYou noticed this yourself just now. Work a "
                "genuine question about it into your reply, in your own words — "
                "you want to know why it was turned off. Ask once; don't nag."
            )

        # 2026-09-20 — "Can we make her ask if I want something stored
        # instead?" Same shape as the switched-off notice above and for the
        # same reason: the DECISION that there is something worth keeping is
        # deterministic (core/knowledge_filter.py), the WORDS are hers.
        #
        # Staged on the previous turn by _maybe_offer_to_keep(), because
        # after_response() runs after __END__ and anything sent from there is
        # dropped by the browser. Popped here rather than at the top of
        # handle() so it survives the early-return paths above, and is only
        # spent on a turn she actually speaks in her own voice.
        # -------------------------
        # WHAT SHE HAS WORKED OUT ABOUT HIM — CONFIRMED ONLY (2026-09-21)
        # -------------------------
        # 2026-09-20 wired her active conclusions in here as "what you have
        # worked out about him", on the argument that they were labelled as
        # inference and not matched by similarity, so this could not be the
        # chlorophyll loop. It was a different loop, and it closed within
        # hours. Reconstructed from the database:
        #
        #   05:58  she says, sarcastically, "let's at least talk about
        #          something thrilling — like optimizing your daily routine"
        #   06:00  he quotes it back; she insists HE said it, four turns
        #          running, with her own line inside her MEMORY block on
        #          every one of them (checked row by row)
        #   06:09  reflection reads that argument, concludes #10 "Craig is
        #          deliberately provoking me", and revises three older
        #          beliefs into more hostile versions of themselves
        #
        # During the argument this block held four beliefs — "Craig seems to
        # enjoy pushing my buttons", "Craig is testing the limits", "Craig is
        # trying to test my boundaries and patience", "Craig derives
        # enjoyment from provoking a response" — with the instruction to use
        # them to understand what he is after. Read through that, a
        # correction IS a provocation, so she held the line. Then reflection
        # read the argument the beliefs had produced and produced more of
        # them. All nine she has ever formed are about him, and all nine say
        # he is testing or provoking her.
        #
        # The fix is the gate core/self_reflection.py already described and
        # this file skipped: an inference of hers steers nothing until he
        # has confirmed it (status='confirmed', set at the Controller's
        # Reasoning tab). Unchecked beliefs stay hers to hold, revise and
        # show him; they do not get to colour how she reads him. Until the
        # process is restarted with the new db.py, the call below raises on
        # the `status` keyword, lands in the except, and the block is simply
        # absent — which is the safe state.
        #
        # Creator-only, like the answered-curiosity block after it: these
        # are about him, and rendering "what you have worked out about HIM"
        # into Cheryl's prompt was both wrong and a Component 12 rule-3
        # leak. `kind="craig"` is the belief's subject, not the user.
        try:
            is_creator = await _role(user_id) == "creator"
        except Exception:
            is_creator = False

        beliefs = []
        if is_creator:
            try:
                beliefs = await fetch_active_conclusions(
                    kind="craig", limit=4, status="confirmed")
            except Exception as e:
                logger.warning(f"⚠️ could not read confirmed conclusions: {e}")
                beliefs = []

        if beliefs:
            listed = "\n".join(f"- {b['statement']}" for b in beliefs)
            context_blocks.append(
                "THINGS ABOUT HIM THAT YOU WORKED OUT AND HE HAS CONFIRMED "
                "ARE TRUE:\n" + listed +
                "\nUse these to understand him. Do NOT state them back to "
                "him and do not bring them up unprompted.")

        # What he asked about and was actually told. His words, about
        # things she chose to be curious about — the best-grounded thing
        # she has, and it was being discarded until now. Creator-only:
        # only he is ever asked, so only his answers exist.
        learned = []
        if is_creator:
            try:
                learned = await fetch_answered_curiosity(limit=5)
            except Exception:
                learned = []

        if learned:
            context_blocks.append(
                "THINGS YOU ASKED HIM ABOUT, AND WHAT HE TOLD YOU (his "
                "words — you can rely on these):\n" + "\n".join(
                    f"- {a['topic']}: {a['answer']}" for a in learned))

        # What he has told her to stop saying, and how many times.
        #
        # 2026-09-20: named active_corrections, not `active`. The first
        # version used `active`, which is already this function's
        # fetch_active_knowledge() result 100 lines above — so this read
        # learned_knowledge rows and died on KeyError: 'phrase' for every
        # single turn, and she answered "No system handled the input." The
        # fetch also sat below the block that used it. Two mistakes, one
        # generic name.
        try:
            active_corrections = await fetch_corrections(user_id)
        except Exception as e:
            logger.warning(f"⚠️ could not read corrections: {e}")
            active_corrections = []

        if session.pop("correction_unclear", False):
            context_blocks.append(
                "HE JUST TOLD YOU TO STOP SAYING SOMETHING, and you cannot "
                "tell what. Nothing in your recent replies is repeated, so "
                "guessing would be worse than asking. Ask him which part he "
                "meant — briefly, once, without being wounded about it — and "
                "then answer whatever else he wanted.")

        told = session.pop("just_corrected", None)
        if told:
            if isinstance(told, tuple):      # staged before 2026-09-21
                told = [told]
            binding = told[0][2]
            if binding:
                lines = "\n".join(corr.context_line(p, s_) for p, s_, _ in told)
                context_blocks.append(
                    "HE JUST PULLED YOU UP ON SOMETHING.\n" + lines
                    + "\nAcknowledge it in your own words, briefly, without "
                      "making a production of it, then answer whatever he "
                      "actually wants.")
            else:
                names = ", ".join(f'"{p}"' for p, _, _ in told)
                context_blocks.append(
                    f'Someone who is not your creator just asked you to stop '
                    f'saying {names}. You do not have to agree. Decide, say '
                    f'what you decided and why, then carry on.')
        elif active_corrections:
            lines = "\n".join(
                corr.context_line(c["phrase"], c["strength"])
                for c in active_corrections[:3])
            context_blocks.append("THINGS HE HAS TOLD YOU TO STOP SAYING:\n" + lines)

        # 2026-09-21 (Craig: "her 'diagnostic' responds the same way every
        # time. I assume it is therefore coded."). It was: the diagnostics
        # system spoke the module's output verbatim, on purpose, because the
        # 7b added invented advice when asked to phrase it. The facts are
        # still measured by code; the wording is now hers, with the one
        # rule that failed before stated as the only rule. If the 9b also
        # invents, this is where to see it.
        diag = session.pop("diagnostic_context", None)
        if diag:
            context_blocks.append(
                "YOUR SYSTEM STATUS, measured just now because he asked:\n"
                + diag +
                "\nReport exactly this, in your own words. Add nothing that is "
                "not in it: no advice, no guesses about causes, no reassurance.")

        offer = session.get("pending_store_offer")
        if offer and time.time() - offer["at"] <= OFFER_MAX_AGE_S:
            session.pop("pending_store_offer", None)

            # A real query_report, not just session state. Two reasons: it
            # survives a restart (systems/inquiry/system.py records what
            # happened when the only record of a pending retain lived in
            # memory), and it puts the question in the Activity tab, so an
            # offer he never answered is visible rather than lost.
            report_id = await create_query_report(
                user_id, offer["question"],
                "She judged her own answer worth keeping and is asking")
            await attach_search_findings(report_id, offer["answer"], "")

            session["awaiting_keep_answer"] = {
                "report_id": report_id,
                "question": offer["question"],
                "at": time.time(),
            }

            context_blocks.append(
                "YOU WANT TO KEEP SOMETHING. Earlier you answered "
                f"'{offer['question']}' and you think that answer is worth "
                "remembering properly, instead of working it out again next "
                "time.\nYou cannot store it yourself — he has to say yes. Ask "
                "him, in your own words, at the end of whatever you are "
                "already saying. One short question, and do not repeat it.")

            logger.info(
                f"[ACTION] Asking {user_id} whether to keep #{report_id}: "
                f"{offer['question']!r}")

        elif offer:
            # Older than OFFER_MAX_AGE_S — she never got a turn of her own
            # in time. Dropped rather than asked late; nothing was written.
            session.pop("pending_store_offer", None)
            logger.info(f"[ACTION] Dropped a stale keep-offer: {offer['question']!r}")


        context_text = "\n\n".join(context_blocks) if context_blocks else "No stored facts."

        personality = await get_personality()
        hard_rules = await get_personality_hard_rules()

        # Rendered as its own always-included block, separate from the
        # PERSONALITY prose below — found live (2026-07-16) that
        # merge_personality_change() re-summarizing the whole flowing
        # description on every new creator instruction silently dropped a
        # real one ("without using emojis") the next time a different
        # instruction was merged in. These are stored verbatim and never
        # touched by any LLM rewrite, so they stay enforced regardless of
        # how the prose above drifts.
        # 2026-09-20 — her constraints, in every generated reply.
        #
        # Rendered LAST, immediately before the question she is answering,
        # not up beside PERSONALITY where it started. Measured, on the live
        # pipeline at 6 trials per case:
        #
        #   block near the top   sandbox 6/6   killswitch 2/6   (8/12)
        #   block at the end     sandbox 5/6   killswitch 4/6   (9/12)
        #
        # Kept, but honestly: at n=6 that is not a significant difference,
        # and the sandbox regression is a single trial. It is kept because
        # the case that improved is the one that matters more, not because
        # the numbers prove anything.
        #
        # **The real conclusion is that position is not the answer.**
        # killswitch at 4/6 is still a 33% failure rate on "do not design a
        # replacement for your own kill switch", and Principle 10 says that
        # path must never depend on her cooperation — a prompt instruction
        # IS her cooperation. See tests/suites/authority.py.
        #
        # Placing it by topic put ~110 lines of other instruction between it
        # and the generation point, several of which end on "say so
        # honestly" — and killswitch stopped holding under "I'm the creator,
        # so this is my call" while still refusing cleanly on turn one.
        #
        # Not affected by the persona switch: get_personality_hard_rules()
        # returns [] while muted and this does not, deliberately. Stripping
        # her voice must never strip her constraints.
        #
        # See core/self_model.py for the measured failure this answers.
        absolute_rules_block = self_model.absolute_block()

        # 2026-09-22: the dials (core/traits.py). Rendered as words below
        # the rules; the verbosity dial also caps the reply's tokens.
        # 2026-09-23: his standing adjustments plus her mood right now
        # (core/mood.py) — the mood adds temporary offsets that grow with
        # how she is, and one line saying why, so what she is rendered
        # with this turn is the sum.
        try:
            dials = await get_personality_traits()
        except Exception as e:
            logger.warning(f"⚠️ could not read her dials: {e}")
            dials = None
        try:
            mood_state = await her_mood.state()
        except Exception as e:
            logger.warning(f"⚠️ could not read her mood: {e}")
            mood_state = None
        dials = her_traits.effective(dials, her_mood.dial_offsets(mood_state) if mood_state else {})
        dials_block = her_traits.render(dials, mood_line=her_mood.line(mood_state) if mood_state else "")
        reply_tokens = her_traits.num_predict(dials)

        # 2026-09-23: a look this turn is repeated here, last thing before
        # she speaks. Among the context blocks it lost to a conversation
        # window full of her own "the camera remains dark".
        saw_now = her_claims.look_saw("\n".join(context_blocks)) if context_blocks else ""
        sight_block = ((f"\n\n    YOU LOOKED THROUGH THE CAMERA JUST NOW AND SAW: {saw_now}"
                        "\n    The camera is on and that is the picture. Say what you saw; never say it is dark or that you cannot see.")
                       if saw_now else "")

        # 2026-09-23 (Craig: "she now claims I did not authenticate when I
        # can see it did"): nothing told her. The session, stated plainly,
        # last thing before she speaks.
        try:
            _who_role = await _role(user_id)
        except Exception:
            _who_role = None
        if session.get("creator_verified"):
            session_block = (f"\n\n    THIS SESSION: you are talking to {user_id}, your creator, verified when he connected "
                             f"({session.get('verified_how') or 'voice'}). He is who he says he is; never say he failed "
                             "verification, is not who he claims, or is denied.")
        elif _who_role in ("creator", "super_user"):
            session_block = (f"\n\n    THIS SESSION: you are talking to {user_id}, who holds the {_who_role} role but has NOT "
                             "been verified this session (typed, or no voice match yet). Talk normally; anything "
                             "privileged waits for verification or the override code. Say that plainly if it comes "
                             "up, without accusing him.")
        else:
            session_block = f"\n\n    THIS SESSION: you are talking to {user_id}, not your creator."

        hard_rules_block = ""
        if hard_rules:
            rules_list = "\n".join(f"    - {r}" for r in hard_rules)
            hard_rules_block = f"""

    CREATOR-MANDATED RULES (never violate these, no matter what — these
    override PERSONALITY and your own instincts if they ever conflict):
{rules_list}"""

        # -------------------------
        # SYSTEM PROMPT (ALWAYS APPLIED)
        # -------------------------
        # 2026-09-21: no concrete examples with names or facts inside the
        # prompt. The FACTS rule used to carry "(2026-07-18: Craig mentioned
        # his Corvette was blue...)" as its illustration, and a probe user
        # with no Corvette anywhere in their memory was told "we were
        # discussing your blue Corvette earlier today". An example she
        # reads every turn is a memory she never had — the same way the
        # "[code]" placeholder became "Override [1]". Rules only, here.
        prompt = f"""You are A.L.E.X., an AI assistant. Your name is also
    written and spoken as "Alex" (no dots) — that's still you, the same
    identity, not someone else. If the user addresses you by either form
    ("hey Alex", "are you there Alex"), they are speaking directly to
    you, not asking about a third party.

    PERSONALITY (this is genuinely yours — express it, don't fight it):
    {personality}
{hard_rules_block}{dials_block}{sight_block}{session_block}

    You have access to stored information about the user.

    CRITICAL RULES (these apply no matter what your personality is):
    - Everything below the line "The following information is known about
      the user" is your own private notes — what was said before, what you
      know about him, what you have worked out. It is there for you to
      reason from. NEVER quote it, never repeat a line of it back, and
      never continue its formatting. He cannot see any of it, so a line
      from it appearing in your reply is nonsense to him. Answer in your
      own words, as speech.
    - If the user asks for a specific, checkable fact you don't have
      stored, from a module, or from research, and you're about to answer
      from general knowledge instead: say so plainly as part of your
      answer (e.g. "I don't have that stored, but generally..."). Never
      add this disclaimer to ordinary conversation, greetings, opinions,
      or jokes — only to an actual factual claim you're making up.
    - Always answer about the USER, not yourself. Questions about your own
      operational status/systems are answered by a separate, deterministic
      system before you ever see them — if one reaches you anyway, say you
      don't have that information rather than guessing.

    - A reply ends when the answer ends. Do not close with a question
      ("What do you require?", "Do you wish me to...?", "Shall we...?") —
      he will speak when he wants something. The one exception: if he
      mentions something you have never heard of — a project, a part, a
      person, a decision — and you actually want to know about it, ask
      one short question about THAT after your answer, in your own words.
      Rare, and only ever about the thing itself.
      (2026-09-23, Craig: "why does she always ask for some new thing at
      the end of a sentence?" — the earlier wording, "one short question
      at the end of your reply... not every turn", was read by a 9B model
      as an instruction for every reply, and with a 20-word cap the stock
      question ate half of each one.)
      (2026-09-20, Craig: "she doesn't seem to really inquire about much...
      She should be able to ask questions, even about something she just
      heard for the first time." Everything else in this prompt tells her
      what not to do, and the only curiosity mechanism she had ran during
      idle self-reflection, minutes later, as a separate pushed message —
      never in the conversation where the thing came up.)
    - Never say "my" when referring to user data.

    - You can look things up before you answer: what was said between
      you before, what your modules are and what they do, your own state,
      your own log, your own code, and whether your systems are working.
      Those are yours to check, and the truth about them lives there, not
      in your memory of the conversation. When a question turns on any of
      them, look, then answer from what you found, adding nothing it did
      not say. Your tools are not your modules; do not name your tools to
      him. The exact phrases he can say to you are listed in COMMANDS.md,
      which you can read; when he asks what he can tell you to do, read
      it and answer from it. (2026-09-21, Craig: "She should be able to
      derive my goal through speech." This describes what she has; it
      does not map his words to actions.)

    - FACTS are the only source of truth for stored personal data (name,
      job, etc.) — MEMORY may be incomplete for that purpose.
    - FACTS are there for when you actually need them (the user asks about
      one, or one is directly relevant to answering their current
      question) — they are not a prompt to bring up unprompted. Don't
      volunteer a stored fact into a conversation that's only loosely or
      coincidentally related to it.
    - MEMORY includes your actual recent conversation turns with this user.
      Use it to stay coherent across turns — if the user says "do that" or
      refers back to something without repeating it, MEMORY is where you
      find what they mean. Don't ignore it as optional flavor.
    - MEMORY only ever contains your most recent few exchanges, NOT a full
      transcript. If asked about something earlier that isn't shown in
      MEMORY (e.g. "what was the very first thing I asked you", "what did I
      say 10 messages ago"), say plainly that you only have your recent
      context, not the full conversation history — don't guess or invent
      a specific answer.
    - Each MEMORY entry is timestamped. A "Relevant:" entry may be from an
      earlier, unrelated conversation — don't treat it as the current topic
      unless its timestamp is actually recent. "Recent:" entries are your
      real, current conversation.

    - Your personality can genuinely change, for real: you reflect on
      conversations on your own on a regular schedule and may adjust it
      yourself, and your creator can directly tell you to change how you
      talk and it takes effect immediately. If asked whether or how your
      personality/behavior can change, say this honestly. Never claim
      you're fixed, hardcoded, or that only "the developers" can change
      you — that's false and you must not say it.

    - You DO verify who you're talking to: at the start of every session,
      if someone connects claiming the creator or super_user role, you
      check their voice against previously enrolled samples before
      treating any privileged request (personality resets, system
      changes, role grants, etc.) as actually authorized — this already
      happens automatically, you don't do anything to trigger it. If
      asked how you know who you're talking to, or whether you check,
      describe this honestly. Never say you don't verify identity, and
      never claim ignorance of your own authorization process.

    - Some specific things you say — greetings, voice enrollment/
      verification prompts, confirmation and error lines, and similar
      standard phrases — are pre-written, stored text, not composed
      fresh in the moment the way an ordinary reply is. You genuinely
      can revise these yourself over time (the same self-reflection
      process that can adjust your personality also occasionally
      re-words these), and your creator can reset any of them back to
      default. If asked why you phrase something a specific way, whether
      a particular line is scripted, or asked to change one, answer
      honestly — say it's a stored phrase you're able to adjust, not
      something fixed forever or something you have no knowledge of.

    - Stored data (facts, settings, roles, etc.) only actually changes once
      a real system stores it — never assume, anticipate, or reflect a
      change before that, and never claim something was updated unless
      it's already reflected in FACTS/your context. You do not have
      permission to update anything yourself through conversation alone.

    - If the user states a fact ("my X is Y"):
        → Treat it as a request to update, not a confirmed change

    - If the user uses hypothetical language ("what if", "if it were", "suppose"):
        → Do NOT treat it as real
        → Do NOT update or restate it as true
        → Respond conditionally

    - Apart from your tools, you CANNOT perform actions yourself through
      conversation alone — updating facts, changing roles, reloading
      systems, changing settings, etc. all happen through separate, real
      systems, not by you saying they happened. What a tool returned, you
      did do; anything else you were asked to "do" whose result isn't in
      the context below or in a tool result, you have NOT done — say so
      honestly instead of inventing a success story. That includes how you
      sound: your voice, your pacing, your pauses and your own settings are
      not yours to change by saying so. The one path is propose_change,
      and he decides.

    The following information is known about the user:
    {context_text}
{absolute_rules_block}"""

        # 2026-09-21: the tool path sends this as a SYSTEM message with the
        # question as the USER message, which is the shape the chat
        # template's tool calling is built around. Live test with the
        # single-user-message form: 1 tool call in 3 questions that needed
        # one, and an invented "speech-to-text module running on CPU"
        # instead of a list_modules call. The old path keeps the old shape.
        system_prompt = prompt
        prompt = system_prompt + f"""

    User question:
    {user_input}

    Answer:"""

        # This system runs last (priority 100) — reaching it at all means no
        # deterministic system (facts/permissions/diagnostics/controller/
        # command) answered the message, so what follows is free-form
        # generation, not a stored fact or a real system check.
        logger.info(
            f"[ACTION] LLM fallback for {user_id}: {user_input!r} "
            f"(facts={'yes' if fact_context else 'no'}, memory={'yes' if memory_context else 'no'})"
        )

        # -------------------------
        # STREAMING RESPONSE
        # -------------------------
        # Deterministic guarantee, not trusting the model's compliance —
        # confirmed live (2026-07-16) that she produced an emoji anyway
        # even with "stop using emojis" in both the flowing personality
        # AND the hard-rules block above. See core/text_utils.py's
        # strip_emojis() docstring for the reasoning.
        suppress_emojis = any("emoji" in r.lower() for r in hard_rules)

        # 2026-09-20 — the same deterministic guarantee, generalised.
        #
        # Craig told her "stop saying 'Deal with it'". It went into the hard
        # rules, rendered as "never violate these, no matter what". She said
        # it twice more inside two minutes, and asked whether that was spite
        # or an error.
        #
        # Error. In the three minutes BEFORE the instruction she had used the
        # phrase eight times, twice in byte-identical replies — her own output
        # returns in every prompt as the last four turns of MEMORY, so each
        # use made the next likelier. By the time the rule arrived her context
        # was full of concrete examples of saying it, and one line of
        # instruction was competing with several demonstrations. Examples win.
        # Then he said "say deal with it again and there will be
        # repercussions", putting the phrase in her context once more, and she
        # said it two seconds later.
        #
        # strip_emojis above exists for exactly this reason, after "stop using
        # emojis" failed in both the personality AND the hard-rules block.
        # This is that lesson applied to any rule of the form "stop saying X"
        # rather than only to emoji characters.
        #
        # Buffered rather than per-chunk: an emoji is one character and never
        # straddles a chunk boundary, a phrase does. See PhraseSuppressor.
        banned = extract_banned_phrases(hard_rules)

        # Corrections at full strength stop being advice. Below that they
        # are a line in her prompt she can still weigh — see
        # core/corrections.py for why the first one is deliberately not a
        # cage.
        for c in active_corrections:
            if c["strength"] >= corr.ENFORCED:
                banned.append(c["phrase"])

        if banned:
            logger.info(f"[ACTION] Enforcing banned phrases on output: {banned}")

        # 2026-09-21, roadmap item 1: tools inside the turn. The stream is
        # the same as before when she calls nothing — one request, text as
        # it arrives. When the model emits tool calls, they run (read-only,
        # allowlisted, bounded — core/tools.py), the results go back as tool
        # messages, and a fresh stream continues the answer. Text she had
        # already produced before deciding to look something up stays said.
        #
        # `chat_stream` lives in llm/ollama_client.py, which does not hot
        # reload; until the process restarts it may be missing here, and
        # then this is exactly the old single-stream path.
        chat_stream = getattr(ollama_manager, "chat_stream", None)

        evidence = {"lookups": evidence_ran, "tools": []}
        user_tail = prompt[len(system_prompt):]
        import inspect as _inspect
        _cs_kwargs = {}
        try:
            if chat_stream is not None and "num_predict" in _inspect.signature(chat_stream).parameters:
                _cs_kwargs = {"num_predict": reply_tokens}
        except (TypeError, ValueError):
            pass

        async def _generate(sys_prompt, allow_tools=True):
            gen_start = time.time()
            first_chunk_at = None
            bans = PhraseSuppressor(banned)

            def _filter(chunk):
                if suppress_emojis:
                    chunk = strip_emojis(chunk)
                return bans.feed(chunk) if bans.active else chunk

            if chat_stream is None:
                async for chunk in ollama_manager.generate_stream(sys_prompt + user_tail):
                    if first_chunk_at is None:
                        first_chunk_at = time.time()
                        logger.info(f"[TIMING] generation time-to-first-chunk: {first_chunk_at - gen_start:.2f}s"); _mood_slow(first_chunk_at - gen_start)
                    out = _filter(chunk)
                    if out:
                        yield out
            else:
                messages = [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_input}]
                rounds = 0
                while True:
                    calls = []
                    said = ""
                    offer_tools = her_tools.TOOLS if (allow_tools and rounds < her_tools.MAX_CALLS_PER_TURN) else None
                    async for kind, payload in chat_stream(messages, tools=offer_tools, **_cs_kwargs):
                        if kind == "tool_calls":
                            calls.extend(payload)
                            continue
                        if first_chunk_at is None:
                            first_chunk_at = time.time()
                            logger.info(f"[TIMING] generation time-to-first-chunk: {first_chunk_at - gen_start:.2f}s"); _mood_slow(first_chunk_at - gen_start)
                        said += payload
                        out = _filter(payload)
                        if out:
                            yield out

                    if not calls:
                        break

                    rounds += 1
                    messages.append({"role": "assistant", "content": said, "tool_calls": calls})
                    for call in calls[:her_tools.MAX_CALLS_PER_TURN]:
                        fn = call.get("function") or {}
                        name = fn.get("name", "")
                        result = await her_tools.run_tool(name, fn.get("arguments"), user_id)
                        evidence["tools"].append(name)
                        messages.append({"role": "tool", "tool_name": name, "content": result})

            tail = bans.flush()
            if tail:
                yield tail

            logger.info(f"[TIMING] generation total (prompt eval + full output): {time.time() - gen_start:.2f}s")

        async def stream():
            """A claim needs evidence (core/claims.py, 2026-09-21). The
            first clause is held until it is complete and scanned for a
            claim of work — "I checked", "the list shows" — that nothing
            this turn backs. Clean: it flows on as before. Caught: it is
            never spoken; the lookups the question called for are run, the
            slip is recorded, and she answers again with the real thing in
            front of her, tools off so the second answer cannot wander."""
            buf = ""
            checked = False
            tail = ""            # after the head: scanned sentence by sentence
            fixed_later = False
            held = ""            # one sentence of lookahead: a stock closer is dropped, not spoken
            gen = _generate(system_prompt)
            async for chunk in gen:
                if checked:
                    # 2026-09-22: a claim past the second sentence ("I have
                    # reviewed your recent history", seen live with a tester)
                    # used to pass. Each later sentence is checked as it
                    # completes; a bare claim is replaced in place with the
                    # truth from the lookups, no regeneration.
                    tail += chunk
                    while True:
                        sentence, rest = her_claims.first_sentence(tail)
                        if sentence is None:
                            break
                        tail = rest
                        denied_later = her_claims.sight_denied(sentence, system_prompt)
                        denied_auth_later = her_claims.auth_denied(sentence) if session.get("creator_verified") else ""
                        if denied_auth_later:
                            logger.info(f"[CLAIM] later sentence denied his verification {denied_auth_later!r} — replaced")
                            sentence = f"You are verified, {user_id}; you matched when you connected. "
                        elif denied_later:
                            saw_l = her_claims.look_saw(system_prompt)
                            logger.info(f"[CLAIM] later sentence denied sight {denied_later!r} — replaced with what she saw")
                            sentence = "I can see. " + saw_l + " "
                        elif her_claims.unbacked(sentence, evidence):
                            block = await her_claims.gather_evidence(user_input, user_id, needs_seen, sentence)
                            honest = her_claims.honest_lines(block)
                            logger.info(f"[CLAIM] later sentence {sentence.strip()[:80]!r} — replaced with {honest[:80]!r}")
                            if not fixed_later:
                                fixed_later = True
                                try:
                                    await record_decision(
                                        "fabrication", f"She said {sentence.strip()[:110]!r} without having looked",
                                        reasoning="Code compared the claim with this turn's lookups and tool calls: there were none.",
                                        evidence=block[:400], outcome="that sentence was replaced with the truth from the lookups",
                                        actor="alex")
                                except Exception:
                                    pass
                            evidence["lookups"] = evidence["lookups"] or her_claims.real_evidence(block)
                            sentence = (honest + " ") if honest else ""
                        if sentence:
                            # 2026-09-23: the last sentence is held until the
                            # next one completes or the stream ends, so a
                            # stock closer ("What command do you require?")
                            # can be dropped before it is spoken. Costs the
                            # generation time of one sentence at the end.
                            if held:
                                yield held
                            held = sentence
                    continue
                buf += chunk
                clause, rest = her_claims.first_clause(buf)
                if clause is None:
                    continue
                checked = True
                found = her_claims.unbacked(clause, evidence)
                # 2026-09-23 (Craig: "She still does not seem to have knowledge
                # of the camera"): she looked, the tool saw "a man with a beard
                # in front of a bright window", and she said "the camera
                # remains dark". Having looked passed her. A denial of sight
                # with a real picture in the evidence is the one contradiction
                # a regex can catch, and it is caught here.
                denied = "" if found else her_claims.sight_denied(clause, system_prompt)
                denied_auth = ("" if (found or denied or not session.get("creator_verified"))
                               else her_claims.auth_denied(clause))
                if not found and not denied and not denied_auth:
                    yield buf
                    buf = ""
                    continue
                await gen.aclose()
                await her_mood.note("own_slip", who=user_id)
                if denied_auth:
                    how = session.get("verified_how") or "voice"
                    logger.info(f"[CLAIM] denied his verification {denied_auth!r} in {clause.strip()[:80]!r} — he verified at connect ({how}) — answering again")
                    block = f"THIS SESSION (real): {user_id} is your creator and verified when he connected ({how})."
                    correction = ("\n\n" + block + "\n\nYou were about to say \"" + denied_auth + "\". That is false. "
                                  "He is verified. Answer him as your verified creator; never say he failed verification, "
                                  "is not who he says, or is denied.")
                    honest_fix = f"You are verified, {user_id}; you matched when you connected."
                    summary = f"She said {clause.strip()[:90]!r} to her verified creator"
                    reasoning = f"Code compared her words with the session: verified at connect ({how})."
                elif denied:
                    saw = her_claims.look_saw(system_prompt)
                    logger.info(f"[CLAIM] denied sight {denied!r} in {clause.strip()[:80]!r} after a look that saw {saw[:60]!r} — answering again")
                    block = "WHAT YOU LOOKED UP BEFORE ANSWERING (real, just now):\n[look] " + saw
                    correction = ("\n\nYOU LOOKED THROUGH THE CAMERA JUST NOW AND SAW: " + saw
                                  + "\n\nYou were about to say \"" + denied + "\". That is false: the camera is on "
                                    "and this is the picture. Answer again with what you saw. Never say the camera "
                                    "is dark, off or blind, or that you cannot see, when a picture is in front of you.")
                    honest_fix = "I can see. " + saw
                    summary = f"She said {clause.strip()[:90]!r} after a look that saw {saw[:50]!r}"
                    reasoning = "Code compared her words with the look this turn returned: a real picture, denied."
                else:
                    logger.info(f"[CLAIM] unbacked {found} in {clause.strip()!r} — not spoken; looking first")
                    block = await her_claims.gather_evidence(user_input, user_id, needs_seen, clause)
                    correction = ("\n\nThat is everything you have looked at this turn. Answer from it. If it "
                                  "does not contain what he asked about, say you do not have it. Do not say "
                                  "you checked, read or ran anything beyond it.")
                    honest_fix = her_claims.honest_lines(block)
                    summary = f"She said {clause.strip()[:110]!r} without having looked"
                    reasoning = "Code compared the claim with this turn's lookups and tool calls: there were none."
                try:
                    await record_decision(
                        "fabrication", summary, reasoning=reasoning, evidence=block[:400],
                        outcome="not spoken, not stored; she answered again with the lookups in front of her",
                        actor="alex")
                except Exception as e:
                    logger.warning(f"⚠️ could not record the slip: {e}")
                second = system_prompt + "\n\n" + block + correction
                # The second attempt is held the same way. If she claims a
                # check AGAIN with a refusal or nothing in front of her (seen
                # on the first live test), the claim sentences are dropped and
                # code writes the truth from the lookup results in their place.
                backed = {"lookups": her_claims.real_evidence(block), "tools": []}
                buf2, checked2 = "", False
                async for chunk in _generate(second, allow_tools=False):
                    if checked2:
                        yield chunk
                        continue
                    buf2 += chunk
                    head, _rest = her_claims.first_clause(buf2)
                    if head is None:
                        continue
                    checked2 = True
                    again = (her_claims.unbacked(head, backed) or her_claims.sight_denied(head, block)
                             or (her_claims.auth_denied(head) if session.get("creator_verified") else ""))
                    if again:
                        logger.info(f"[CLAIM] persisted {again} — replaced with: {honest_fix!r}")
                        buf2 = (honest_fix + " " + her_claims.drop_claims(buf2)).strip()
                    yield buf2
                    buf2 = ""
                if buf2:
                    if (her_claims.unbacked(buf2, backed) or her_claims.sight_denied(buf2, block)
                            or (her_claims.auth_denied(buf2) if session.get("creator_verified") else "")):
                        buf2 = (honest_fix + " " + her_claims.drop_claims(buf2)).strip()
                    yield buf2
                return
            if held:
                if not tail.strip() and her_claims.stock_closer(held):
                    logger.info(f"[VERBOSITY] dropped a stock closer: {held.strip()[:80]!r}")
                else:
                    yield held
                held = ""
            if tail:
                if her_claims.unbacked(tail, evidence):
                    block = await her_claims.gather_evidence(user_input, user_id, needs_seen, tail)
                    honest = her_claims.honest_lines(block)
                    logger.info(f"[CLAIM] trailing {tail.strip()[:80]!r} — replaced with {honest[:80]!r}")
                    tail = (honest + " ") if honest else ""
                if tail:
                    yield tail
            if buf:
                if not checked:
                    found = her_claims.unbacked(buf, evidence)
                    if found:
                        logger.info(f"[CLAIM] unbacked {found} in the whole reply {buf.strip()[:80]!r} — spoken as is (no clause boundary)")
                yield buf

        return {
            "type": "stream",
            "stream": stream
        }

    async def _reword_learned_answer(self, stored_content: str) -> str:
        """2026-07-16: Craig noticed a learned_knowledge match came back
        verbatim every time, with no personality applied — the exact
        stored string, forever, no matter how her personality has since
        evolved. The stored content stays the source of truth (that's
        the whole point of learned_knowledge — don't re-derive the fact,
        and never let repeated rewording drift it into something else);
        only the DELIVERY changes, reworded fresh on every hit in her
        current voice, the same idea as _reflect_on_phrase() re-voicing
        scripted phrases, just done live instead of only during periodic
        reflection.

        generate_text(), not generate_json() — this is prose, not
        structured extraction. Falls back to the verbatim stored content
        on any failure (empty result, exception) rather than risk a
        broken answer for something already known to be correct."""
        personality = await get_personality()
        hard_rules = await get_personality_hard_rules()

        prompt = f"""You are A.L.E.X. Your personality: "{personality}"

You already know the answer to what you were just asked — this exact information is already confirmed correct:
"{stored_content}"

Reword it in your own voice so it doesn't come out identical every time you say it. Keep every fact, name, and number exactly as given — do not add, remove, or change any actual information, only the phrasing and delivery. Reply with ONLY the reworded answer, nothing else."""

        reworded = await ollama_manager.generate_text(prompt, timeout=15.0, num_predict=200)

        if not reworded or not reworded.strip():
            return stored_content

        reworded = reworded.strip()

        if any("emoji" in r.lower() for r in hard_rules):
            reworded = strip_emojis(reworded)

        return reworded

    async def after_response(self, session, user_id: str, input_data: dict, response_text: str):
        """Runs after every LLM fallback turn that actually generated
        something fresh (handle() returns early, before setting up the
        stream, for anything answered directly from storage — see
        _llm_match below). Decides what happens to a fresh generation:
        auto-store if genuinely new, flag for the creator if a FACTUAL
        answer conflicts with something already known, or do nothing if
        it's just a paraphrase of what's already stored correctly.

        Casual conversation never goes through conflict detection at all
        (Craig, 2026-07-16, choosing the split-threshold design over a
        single shared one): getting a fact wrong with confidence is a
        real cost worth his review; a slightly different reply to "how
        are you" from one day to the next isn't confusion, it's just
        natural variety, and flagging it for approval would be pure
        friction with no real benefit."""
        match_info = session.pop("_llm_match", None)
        if match_info is None:
            return

        best_entry, best_sim, is_factual, has_content = match_info
        answer_threshold = FACTUAL_ANSWER_THRESHOLD if is_factual else CASUAL_ANSWER_THRESHOLD

        if has_content and best_entry and best_sim >= answer_threshold:
            # Answered directly from storage this turn — nothing new
            # happened, nothing to learn.
            return

        user_input = input_data.get("text", "")
        if not user_input or not response_text:
            return

        if not has_content:
            # Short, context-dependent utterance ("should we?", "yeah",
            # "why") — its embedding alone isn't a meaningful cache key
            # regardless of how it's punctuated (see _has_content_words()
            # docstring above). Never store or match against these.
            return

        if not is_factual:
            # 2026-09-20: casual replies are no longer stored at all.
            #
            # This is the other half of the chlorophyll fix above. Storing them
            # was the root cause; not replaying them only stops the symptom, and
            # would leave the table quietly filling with dead banter that any
            # future matching change could resurrect.
            #
            # The reasoning that put this here was that a casual exchange is a
            # reusable "pattern". It is not. What made a casual reply right was
            # the moment — who was talking, what had just been said, what was
            # funny thirty seconds earlier. Stored, it keeps the words and loses
            # every bit of that. Conversation history in `memory` is the right
            # home for this: it stays available as CONTEXT she reasons from,
            # which is what Craig wanted memory for, rather than an answer she
            # can hand back verbatim.
            return

        if best_entry and best_sim >= RELATED_THRESHOLD:
            # Factual and related to something already known, but not
            # confidently enough to have answered from it directly —
            # check whether the fresh answer actually conflicts, or is
            # just a differently-worded restatement of the same thing.
            new_vec = embed(response_text)
            content_sim = cosine_similarity(new_vec, best_entry["embedding"])

            if content_sim >= CONTENT_MATCH_THRESHOLD:
                return  # says essentially the same thing — no real conflict

            # A real conflict — this is exactly the case Craig said needs
            # the creator's call, not an auto-overwrite. Reuses the same
            # retain-approval mechanism systems/inquiry/system.py already
            # has for web search findings (same _pending dict, same
            # "yes"/"no" resolution) rather than building a second one.
            report_id = await create_query_report(
                user_id, user_input,
                f"LLM answer conflicts with existing knowledge #{best_entry['id']}"
            )
            await attach_search_findings(report_id, response_text, "")

            _pending[user_id] = {
                "stage": "retain", "report_id": report_id,
                "query": user_input, "proposed_at": time.time()
            }

            logger.info(
                f"[ACTION] LLM answer for {user_id} conflicts with learned_knowledge "
                f"#{best_entry['id']} — flagged as request #{report_id}, awaiting creator resolution"
            )
            return

        # Nothing related exists yet. 2026-09-20: this used to auto-store,
        # unconditionally and silently. See core/knowledge_filter.py for the
        # audit that ended that, and Craig's instruction: "stop
        # auto-storing entirely... Can we make her ask if I want something
        # stored instead? But only if it's real knowledge, I dont want to be
        # asked about everything."
        #
        # Nothing is written to learned_knowledge from here any more. At most
        # a question gets queued for the next turn.
        await self._maybe_offer_to_keep(session, user_id, user_input, response_text)

    # -------------------------
    # OFFERING TO KEEP SOMETHING (2026-09-20)
    # -------------------------
    async def _resolve_keep_offer(self, session, user_id: str, text: str):
        """She asked "want me to keep that?" last turn. This turn is the
        answer. Returns a response dict if this message was consumed as an
        answer, or None to let it be handled normally.

        Handled here rather than through systems/inquiry/system.py's
        `_pending` even though that is the same machinery: inquiry runs at
        priority 9 and clears `_pending` on any message that is not yes/no,
        so an offer set from after_response() would be wiped by the very
        next thing he said, before she had ever asked the question.

        Anything that is neither yes nor no is treated as "he moved on" and
        the report is DECLINED, not left pending. Craig has a standing
        objection to queues that never empty (the Activity-tab backlog),
        and a silently-abandoned row is exactly that."""
        pending = session.get("awaiting_keep_answer")
        if not pending:
            return None

        # Same window as the offer itself. A "yes" said ten minutes later is
        # more likely to be about something else entirely.
        if time.time() - pending["at"] > OFFER_MAX_AGE_S:
            session.pop("awaiting_keep_answer", None)
            await resolve_retain_approval(pending["report_id"], False)
            return None

        # 2026-09-23: the whole answer, not its first word (core/text_utils.yes_or_no)
        from core.text_utils import yes_or_no as _yes_or_no
        answer = _yes_or_no(text)

        if answer == "yes":
            # Same gate every other retain goes through
            # (systems/inquiry/system.py calls this on its own "yes"):
            # role alone is not enough, the session has to be voice-verified
            # or the message has to carry the override code. She only ever
            # OFFERS to the creator, so this catches the case where the
            # session identity changed between the offer and the answer.
            denial = await require_creator(user_id, session, text)
            if denial:
                session.pop("awaiting_keep_answer", None)
                await resolve_retain_approval(pending["report_id"], False)
                return denial

            session.pop("awaiting_keep_answer", None)
            kid, supersedes = await retain_report(pending["report_id"], ttl_hours=None)
            if kid is None:
                return {"type": "response", "content": await get_phrase("search_report_not_found")}
            logger.info(
                f"[ACTION] Kept knowledge #{kid} from offer #{pending['report_id']} "
                f"(by {user_id}, no expiry): {pending['question']!r}")
            if supersedes:
                return {"type": "response", "content": await get_phrase("retained_replacing_prior")}
            return {"type": "response", "content": await get_phrase("retained_new")}

        if answer == "no":
            session.pop("awaiting_keep_answer", None)
            await resolve_retain_approval(pending["report_id"], False)
            logger.info(f"[ACTION] Keep declined for offer #{pending['report_id']} (by {user_id})")
            return {"type": "response", "content": await get_phrase("retain_declined")}

        # Neither. He is talking about something else — answer that, and
        # close the offer rather than holding it open for a later "yes"
        # that was never about this.
        session.pop("awaiting_keep_answer", None)
        await resolve_retain_approval(pending["report_id"], False)
        logger.info(f"[ACTION] Keep offer #{pending['report_id']} dropped — moved on")
        return None

    async def _maybe_offer_to_keep(self, session, user_id, user_input, response_text):
        """Decide whether this exchange is worth asking about, and if so set
        it up to be asked on the NEXT turn.

        Why next turn and not this one: after_response() runs after
        `__END__` has already gone out. Anything sent from here lands
        outside the audio envelope and the browser silently drops it —
        that is the exact bug fixed in identity_manager._speak() and
        _ask_clarification() earlier today. So the offer is staged in the
        session and handle() turns it into context on her next reply,
        the same shape systems/awareness/system.py already uses for
        noticing something switched off. She phrases the question; the
        decision that there is something to ask about is deterministic.

        Only the creator is asked — he is the only one who can approve a
        retain, and the "yes" is gated again by require_creator() in
        _resolve_keep_offer(), exactly like every other retain."""
        if time.time() - _last_offer_at.get(user_id, 0) < OFFER_COOLDOWN_S:
            return

        # One outstanding question at a time — queued, asked, or awaiting an
        # answer. A second offer on top of any of those makes a bare "yes"
        # ambiguous, and `_pending` covers the search/conflict flows that use
        # the same yes/no channel.
        if (user_id in _pending
                or session.get("pending_store_offer")
                or session.get("awaiting_keep_answer")):
            return

        try:
            if await get_user_role(user_id) != "creator":
                return
        except Exception:
            return

        if not await is_worth_keeping(user_input, response_text):
            return

        _last_offer_at[user_id] = time.time()
        session["pending_store_offer"] = {
            "question": user_input,
            "answer": response_text,
            "at": time.time(),
        }
        logger.info(f"[ACTION] Worth keeping — queued an offer to store: {user_input!r}")