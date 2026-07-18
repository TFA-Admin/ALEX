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
but the result is no longer thrown away: after_response() either
auto-stores it (genuinely new, no approval needed — local/trusted) or,
if it conflicts with something already stored, flags it for the
creator's resolution instead of silently overwriting. Applies to
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
from core.text_utils import strip_trailing_punctuation, strip_emojis
from llm.ollama_client import ollama_manager
from db.db import (
    get_personality, fetch_active_knowledge, create_learned_knowledge,
    create_query_report, attach_search_findings, fetch_recent_memory,
    get_personality_hard_rules
)
from config.logger_config import logger

from systems.inquiry.system import _pending

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
        answer_threshold = FACTUAL_ANSWER_THRESHOLD if is_factual else CASUAL_ANSWER_THRESHOLD
        has_content = _has_content_words(user_input)

        session["_llm_match"] = (best_entry, best_sim, is_factual, has_content)

        if has_content and best_entry and best_sim >= answer_threshold:
            logger.info(
                f"[ACTION] Answered {user_id} from learned_knowledge #{best_entry['id']} "
                f"(similarity={best_sim:.2f}, {'factual' if is_factual else 'casual'}): {user_input!r}"
            )
            content = await self._reword_learned_answer(best_entry["content"])
            return {"type": "response", "content": content}

        fact_context = session.get("fact_context", "")
        memory_context = session.get("memory_context", "")

        context_blocks = []

        if fact_context:
            context_blocks.append(f"FACTS:\n{fact_context}")

        if memory_context:
            context_blocks.append(f"MEMORY:\n{memory_context}")

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
    - Never say "my" when referring to user data.

    - FACTS are the only source of truth for stored personal data (name,
      job, etc.) — MEMORY may be incomplete for that purpose.
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

        async def stream():
            gen_start = time.time()
            first_chunk_at = None
            async for chunk in ollama_manager.generate_stream(prompt):
                if first_chunk_at is None:
                    first_chunk_at = time.time()
                    logger.info(f"[TIMING] generation time-to-first-chunk: {first_chunk_at - gen_start:.2f}s")
                yield strip_emojis(chunk) if suppress_emojis else chunk
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
            # Casual: no conflict detection, ever. If something related
            # already exists, a near-duplicate isn't worth storing again;
            # otherwise, auto-store the new pattern. Either way, no
            # approval, no query_report — this is routine, not audited.
            if best_entry and best_sim >= RELATED_THRESHOLD:
                return

            vec = embed(user_input)
            kid = await create_learned_knowledge(user_input, response_text, None, None, vec, user=user_id)
            logger.info(f"[ACTION] Auto-stored new casual pattern #{kid} for {user_id}: {user_input!r}")
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

        # Nothing related exists yet — genuinely new, auto-stored without
        # approval (offline, local, trusted — per Craig's design; the
        # approval gate is specifically for the web search path, which
        # actually crosses a real trust boundary).
        vec = embed(user_input)
        kid = await create_learned_knowledge(user_input, response_text, None, None, vec, user=user_id)
        logger.info(f"[ACTION] Auto-stored new knowledge #{kid} for {user_id}: {user_input!r}")