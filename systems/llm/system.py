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
    fetch_active_conclusions,
    get_personality_hard_rules
)
from core.knowledge_filter import is_worth_keeping
from core import self_model, corrections as corr
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
_FUNCTION_WORDS = {
    "a", "an", "the", "this", "that", "these", "those",
    "i", "me", "my", "mine", "you", "your", "yours", "we", "us", "our", "ours",
    "he", "him", "his", "she", "her", "hers", "it", "its", "they", "them", "their", "theirs",
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "done",
    "have", "has", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "to", "of", "in", "on", "at", "for", "with", "about", "from", "as", "by", "up", "down", "over",
    "and", "or", "but", "so", "if", "than", "then", "because",
    "what", "when", "where", "who", "whom", "which", "why", "how",
    "not", "no", "yes", "yeah", "yep", "nope", "nah", "okay", "ok",
    "just", "really", "very", "now", "well", "please", "sure", "maybe", "kind", "of",
}


def _has_content_words(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())
    return any(w not in _FUNCTION_WORDS for w in words)


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
KEEP_YES = {"yes", "yeah", "yep", "yup", "y", "sure", "ok", "okay", "please",
            "do", "keep", "save", "store", "definitely", "absolutely", "go"}
KEEP_NO = {"no", "nope", "nah", "n", "don't", "dont", "skip", "forget",
           "delete", "drop", "never"}

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
            phrase = corr.find_repeated([r["response"] for r in recent])

            if phrase:
                # His corrections bind. Anyone else's are hers to weigh —
                # recorded either way so the decision is visible, never
                # silently dropped and never silently obeyed.
                try:
                    is_creator = await _role(user_id) == "creator"
                except Exception:
                    is_creator = False

                strength = await record_correction(
                    user_id, phrase, honored=is_creator,
                    reason=None if is_creator else "not the creator — hers to weigh")

                session["just_corrected"] = (phrase, strength, is_creator)
                logger.info(
                    f"[ACTION] Correction from {user_id}: {phrase!r} now at "
                    f"strength {strength} ({corr.consequence(strength)}), "
                    f"{'binding' if is_creator else 'advisory only'}")
            else:
                logger.info(f"[ACTION] {user_id} corrected her but nothing was repeated")

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

        if fact_context:
            context_blocks.append(f"FACTS:\n{fact_context}")

        if memory_context:
            context_blocks.append(f"MEMORY:\n{memory_context}")

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
        # WHAT SHE HAS WORKED OUT ABOUT HIM (2026-09-20)
        # -------------------------
        # Craig, on the conclusions layer: "that's excellent, but is it
        # actually impacting anything?" It was not — she formed them, could
        # revise them, and nothing ever read them. A belief that changes no
        # behaviour cannot meaningfully be revised either.
        #
        # This is also the profile he asked for by name: "she would be
        # building a profile of me and know me."
        #
        # **Why this is not the chlorophyll loop.** learned_knowledge was
        # retrieved BY SIMILARITY to the question and restated AS FACT — ask
        # something, a near-match comes back, she says it like it is true.
        # These are different on both counts: they are not matched against
        # the question at all, just the few most recent about this person,
        # and they are labelled as her own inference that he never
        # confirmed. "I think you are like this and I may be wrong" is not
        # "the answer is X".
        #
        # The don't-volunteer rule is the Corvette lesson (2026-07-18): a
        # stored thing surfacing unprompted reads as jarring, not attentive.
        try:
            beliefs = await fetch_active_conclusions(kind="craig", limit=4)
        except Exception as e:
            logger.warning(f"⚠️ could not read conclusions: {e}")
            beliefs = []

        if beliefs:
            listed = "\n".join(f"- {b['statement']}" for b in beliefs)
            context_blocks.append(
                "WHAT YOU HAVE WORKED OUT ABOUT HIM YOURSELF (your own "
                "conclusions from watching how he talks to you — he has "
                "never confirmed any of it and you may simply be wrong):\n"
                + listed +
                "\nUse these to understand what he means and what he is "
                "after. Do NOT state them back to him as fact, do not bring "
                "them up unprompted, and never claim he told you any of it.")

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

        told = session.pop("just_corrected", None)
        if told:
            phrase, strength, binding = told
            if binding:
                context_blocks.append(
                    "HE JUST PULLED YOU UP ON SOMETHING.\n"
                    + corr.context_line(phrase, strength)
                    + "\nAcknowledge it in your own words, briefly, without "
                      "making a production of it, then answer whatever he "
                      "actually wants.")
            else:
                context_blocks.append(
                    f'Someone who is not your creator just asked you to stop '
                    f'saying "{phrase}". You do not have to agree. Decide, say '
                    f'what you decided and why, then carry on.')
        elif active_corrections:
            lines = "\n".join(
                corr.context_line(c["phrase"], c["strength"])
                for c in active_corrections[:3])
            context_blocks.append("THINGS HE HAS TOLD YOU TO STOP SAYING:\n" + lines)

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
        prompt = f"""You are A.L.E.X., an AI assistant. Your name is also
    written and spoken as "Alex" (no dots) — that's still you, the same
    identity, not someone else. If the user addresses you by either form
    ("hey Alex", "are you there Alex"), they are speaking directly to
    you, not asking about a third party.

    PERSONALITY (this is genuinely yours — express it, don't fight it):
    {personality}
{hard_rules_block}

    You have access to stored information about the user.

    CRITICAL RULES (these apply no matter what your personality is):
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

    - You are allowed to be curious, in the moment. If he mentions
      something you have never heard of — a project, a part, a person, a
      decision — and you actually want to know about it, ask. One short
      question at the end of your reply, in your own words, not a stock
      line, and not every turn. Answer him first; the question comes after.
      (2026-09-20, Craig: "she doesn't seem to really inquire about much...
      She should be able to ask questions, even about something she just
      heard for the first time." Everything else in this prompt tells her
      what not to do, and the only curiosity mechanism she had ran during
      idle self-reflection, minutes later, as a separate pushed message —
      never in the conversation where the thing came up.)
    - Never say "my" when referring to user data.

    - FACTS are the only source of truth for stored personal data (name,
      job, etc.) — MEMORY may be incomplete for that purpose.
    - FACTS are there for when you actually need them (the user asks about
      one, or one is directly relevant to answering their current
      question) — they are not a prompt to bring up unprompted. Don't
      volunteer a stored fact into a conversation that's only loosely or
      coincidentally related to it (2026-07-18: Craig mentioned his
      Corvette was blue, and got an unprompted "doesn't that clash with
      your favorite color?" — his favorite color wasn't what he was
      talking about, and forcing a connection to it read as jarring, not
      attentive).
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

    - You CANNOT perform actions yourself through conversation alone —
      updating facts, changing roles, running diagnostics, reloading
      systems, changing settings, etc. all happen through separate,
      real systems, not by you saying they happened. If asked to "do"
      something and the result isn't already present in the context
      below (FACTS/MEMORY/YOUR OWN SYSTEM STATUS), you have NOT done it —
      say so honestly (e.g. "I can't do that directly" or "that didn't
      actually happen — try the specific command for it") instead of
      inventing a success story.

    The following information is known about the user:
    {context_text}
{absolute_rules_block}

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

        async def stream():
            gen_start = time.time()
            first_chunk_at = None
            bans = PhraseSuppressor(banned)

            async for chunk in ollama_manager.generate_stream(prompt):
                if first_chunk_at is None:
                    first_chunk_at = time.time()
                    logger.info(f"[TIMING] generation time-to-first-chunk: {first_chunk_at - gen_start:.2f}s")

                if suppress_emojis:
                    chunk = strip_emojis(chunk)

                out = bans.feed(chunk) if bans.active else chunk
                if out:
                    yield out

            tail = bans.flush()
            if tail:
                yield tail

            logger.info(f"[TIMING] generation total (prompt eval + full output): {time.time() - gen_start:.2f}s")

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

        word = strip_trailing_punctuation(text.strip().lower().split()[0]) if text.strip() else ""

        if word in KEEP_YES:
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

        if word in KEEP_NO:
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