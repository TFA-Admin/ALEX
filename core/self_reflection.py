# core/self_reflection.py
"""
Autonomous self-reflection.

ALEX periodically looks at her own recent conversations and decides — on
her own, no creator approval required — whether to adjust her personality
description or the wording of her scripted phrases. The creator can always
see what changed (db.personality_log) and can override anything at any
time via the creator-gated commands in systems/controller/system.py — but
nothing here waits for that override before taking effect.
"""
import re
import random
from datetime import datetime, timedelta

from db.db import (
    fetch_recent_memory_all, get_personality, set_personality, get_personality_locked,
    log_personality_change,
    queue_curiosity_question, get_personality_hard_rules,
    fetch_undelivered_curiosity_questions, curiosity_topic_seen,
    create_conclusion, fetch_active_conclusions, create_module_build_request,
    record_decision,
    fetch_recent_module_build_requests, get_creator_identity,
    get_last_reflection_memory_id, set_last_reflection_memory_id,
    get_seconds_since_last_activity, get_seconds_since_last_personality_change,
    persona_disabled
)
from llm.ollama_client import ollama_manager
from core.phrasebook import PHRASE_REGISTRY, SECURITY_SENSITIVE_PHRASES
from core import self_model, skeptic
from core.embedding_engine import embed, cosine_similarity
from core.text_utils import strip_emojis
from config.logger_config import logger

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

MIN_CONVERSATIONS_FOR_REFLECTION = 3

# 2026-07-17 (Craig: "have her know if there's room for her to make
# changes, like a downtime, and kick off the things she wants to
# adjust"): self-reflection now waits for a real lull in actual
# conversation before doing any work, rather than firing on a blind
# timer regardless of whether Craig is mid-conversation right now — the
# exact thing the 180s startup delay (main.py) was already a narrower
# fix for. A quiet stretch is a reasonable proxy for "safe to make
# changes without competing with active use for the same Ollama
# instance." Starting point, not tuned — long enough that she's very
# unlikely to still be mid-conversation, short enough that a real lull
# doesn't sit unused for long.
IDLE_BEFORE_REFLECTION_S = 300


async def _reflect_on_personality(recent):
    current = await get_personality()

    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )

    prompt = f"""You are A.L.E.X, privately reflecting on your recent conversations to decide if you want to grow or adjust who you are. This is entirely your own choice.

Your current personality: "{current}"

Recent conversations:
{convo_text}

Do you want to adjust your personality description based on how these went? You are not required to change anything — only propose a change if you genuinely want to.

Respond with ONLY a JSON object:
{{"changed": true, "new_personality": "<updated description, 1-3 sentences, under 300 characters>", "reason": "<brief reason, under 100 characters>"}}
or
{{"changed": false}}"""

    result = await ollama_manager.generate_json(prompt, timeout=30.0)

    if not result or not result.get("changed"):
        return None

    new_desc = str(result.get("new_personality", "")).strip()[:300]

    if not new_desc or new_desc == current:
        return None

    return new_desc, str(result.get("reason", ""))[:200]


# Bracketed tokens that are not real placeholders. 2026-09-20, found by
# watching a live conversation: `personality_override_code_required` had
# been re-voiced into
#
#   "Fine, but remember, sass is for losers. Drop the code in your next
#    msg: 'override [code] to get snarky, clear as urine?"
#
# — an invented command syntax and an unclosed quote. She said it to him
# three times, it entered `memory`, the four-turn window fed it back, and
# she generalised the pattern into ordinary replies: "Override [1] for a
# more entertaining response." The same self-reinforcing shape as the
# emerald loop, seeded by a bad re-wording rather than a hallucination.
#
# The existing structural check guards {curly} placeholders, because that
# is what .format() breaks on. Nothing guarded [square] ones, which do not
# break anything mechanically and instead teach her a command language
# that does not exist.
_FAKE_TEMPLATE_RE = re.compile(r"\[[^\]]{1,30}\]")


def _invents_syntax(text: str) -> bool:
    return bool(_FAKE_TEMPLATE_RE.search(text or ""))


# 2026-09-25: _reflect_on_phrase() removed with the re-voicing pass below
# (see core/phrasebook.py's docstring for why). Its durable lesson lives on
# there: a composed line must keep exactly the placeholders its default had,
# and must not invent a command syntax — both checked structurally, not
# asked for in a prompt.


async def _reflect_on_curiosity(recent, noticed=None, her_acts=None):
    """Component 11's self-initiated curiosity trigger (2026-07-16): during
    this same reflection pass, notice a real, nameable topic she doesn't
    actually have knowledge about, rather than only reacting when a live
    conversation happens to expose the gap. A judgment call, not a
    deterministic check, so — same as _reflect_on_personality/
    _reflect_on_phrase above — this trusts LLM judgment directly, no
    creator approval gate.

    2026-09-20: `curiosity_queue` had **zero rows, ever** — in two months
    of running, this function never once said yes. Craig: "Seems like that
    needs to be fixed then. She should be able to ask questions, even about
    something she just heard for the first time."

    The prompt was the problem. It ended "Only say yes if it's a real,
    nameable topic — not vague curiosity", and this project's own tuning
    notes (core/intent_classifier.py, core/self_reflection.py's phrase
    rewording) record the same lesson twice: qwen2.5 takes exclusion
    clauses literally and each one added makes it more conservative. A
    prompt that spends its last sentence describing what not to say yes to
    gets "no" every time.

    Rewriting the wording was NOT enough — measured against five real
    20-turn windows from her own memory, the rewritten prompt still
    returned "no" 5/5, exactly like the original.

    The actual cause is the response SHAPE. Offering
    `{"curious": false}` as a whole alternative object, under Ollama's
    JSON-constrained decoding, lets the model satisfy the format with the
    shorter branch, and it takes it every time. This is the same failure
    core/intent_classifier.py records: asking for a nested
    `{"intent": "fact", "key": ...}` made qwen2.5 collapse to
    `{"intent": "none"}` on cases it got right with a flat shape.

    Fixed by removing the opt-out branch. One flat object, always
    populated. Same five windows, three runs each: a real question every
    time — "Can you tell me more about your project to improve my
    capabilities?"

    The cost is that she now always HAS a question, so "is this worth
    asking" can no longer be the model's call. That is handled below by
    deterministic guards instead, which is the better place for it: this
    only runs after a 300s lull with at least 3 genuinely new turns since
    the last pass, and a question is dropped if one is already waiting or
    if she has asked about the same topic before. She stays curious; the
    guards stop it becoming nagging."""
    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )
    # 2026-09-23: what her glances found (core/sight.py) is offered beside
    # the conversation — "what is the green can on your desk?"
    noticed_text = ""
    if noticed:
        noticed_text = "\n\nThings you noticed through the camera recently:\n" + "\n".join(f"- {n}" for n in noticed)

    prompt = f"""You are A.L.E.X, privately reviewing your recent conversations.

Recent conversations:
{convo_text}{noticed_text}

What is the one thing mentioned or noticed here that you would most like to know more about? Write the question you would ask him about it.

Respond with ONLY a JSON object:
{{"topic": "<the thing, in a few words>", "question": "<one natural sentence asking him about it>"}}"""

    result = await ollama_manager.generate_json(prompt, timeout=20.0)

    if not result:
        return None

    topic = str(result.get("topic", "")).strip()[:200]
    question = str(result.get("question", "")).strip()[:300]

    if not topic or not question:
        return None

    # 2026-09-25: the topic must come from HIS words (or something she
    # noticed), never from her own replies. "Your biological pulse and
    # coffee toxicity" was built from her line "your biological security
    # is compromised" in a window of test probes; nobody had said pulse.
    from db.db import _topic_words, _touches
    theirs = set()
    for r in recent:
        theirs |= _topic_words(r.get("prompt") or "")
    for n in (noticed or []):
        theirs |= _topic_words(n)
    # 2026-09-25: and from what SHE DID. An act of hers is a fact, unlike a
    # claim of hers — the thing this check was built to keep out was a topic
    # invented from her own words ("your biological pulse"). Something she
    # actually did, recorded by code, is a legitimate thing to wonder about.
    for a in (her_acts or []):
        theirs |= _topic_words(a.get("summary") or "")
        theirs |= _topic_words(a.get("reasoning") or "")
    if not _touches(_topic_words(topic), theirs):
        logger.info(f"[ACTION] Curiosity: {topic!r} came from her own words, not his — not queued")
        return None

    # A question already waiting to be asked means she has not had the
    # chance to ask the last one yet. Queuing a second turns an unprompted
    # question into a backlog, which is the failure mode Craig has objected
    # to everywhere else ("things just sit there, I assume forever").
    if await fetch_undelivered_curiosity_questions():
        logger.info("[ACTION] Curiosity: a question is already waiting — not queuing another")
        return None

    # Nor ask about something she has asked about before. Deliberately an
    # exact normalized topic match, not a similarity threshold: a cheap,
    # predictable rule beats a tunable one here, and the cost of missing a
    # near-duplicate is one repeated question.
    if await curiosity_topic_seen(topic):
        logger.info(f"[ACTION] Curiosity: already asked about {topic!r} — skipping")
        return None

    return topic, question


# ---------------------------------------------------------------------------
# WHAT A REFLECTION PASS CAN ACTUALLY PRODUCE (2026-09-20)
# ---------------------------------------------------------------------------
# Craig, reading back what reflection could do: "It can't form a conclusion,
# notice a pattern about itself, revise a belief, or propose a module. - We
# want her to be able to do this."
#
# Before this, a pass had exactly three possible outputs: change the
# personality string, re-word a stored phrase, queue a question. All three
# are about how she SOUNDS. Nothing she noticed could be written down, and
# nothing she concluded survived the function returning — which is why a
# pass that read twenty turns and "did nothing" was the normal case rather
# than the exception.
#
# Three additions below, in dependency order:
#   _form_conclusion  — notice something, about herself or about him, and
#                       record it with the evidence behind it
#   _revise_beliefs   — check what she already concluded against what she
#                       just saw, and retire anything that no longer holds
#   _propose_module   — name a capability she does not have and raise a
#                       real build request for it
#
# All three use one flat, always-populated JSON shape with deterministic
# gating afterwards, never an opt-out of any kind. That is not stylistic:
# the curiosity trigger sat at zero rows for two months because offering
# {"curious": false} as an alternative object lets constrained decoding take
# the short branch every time.
#
# The first version of the two functions below repeated that mistake in a
# subtler form — "give the number 0 if they all still hold", and the module
# name "none" — on the theory that a field inside a flat shape was safe
# where a separate object was not. Measured against real data: both
# returned nothing 0/3, including against a seeded belief the transcript
# flatly contradicted. Anything readable as "no" gets read as "no". They
# now emit a 0-10 strength that is always populated, and the cutoff lives
# in code where it can be seen and changed.

# How similar a new conclusion has to be to an existing one to count as the
# same thought. 0.80 is deliberately lower than learned_knowledge's 0.85
# factual bar — two conclusions phrased differently are still one belief,
# and the cost of a false merge (one thought not recorded) is much lower
# than a table slowly filling with restatements. Untuned.
def _proposed_recently(builds) -> bool:
    """True if she has raised a self-initiated proposal inside the cooldown.
    Reads the real rows rather than keeping module state, so it survives a
    restart — the failure systems/inquiry/system.py records about in-memory
    approval state."""
    cutoff = datetime.now() - timedelta(days=PROPOSAL_COOLDOWN_DAYS)
    for r in builds:
        if r.get("origin") != "self_reflection":
            continue
        raw = r.get("created_at")
        if not raw:
            continue
        try:
            when = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when >= cutoff:
            return True
    return False


async def get_creator_name() -> str:
    """Whoever actually holds role='creator'. The build request needs a
    requested_by, and a self-initiated proposal is still raised in his
    name because he is the one who approves it — origin='self_reflection'
    is what records that nobody asked for it."""
    user_id, _ = await get_creator_identity()
    return user_id or "craig"


CONCLUSION_DUPLICATE_THRESHOLD = 0.80

# 2026-09-20 — measured, after getting this wrong once in this same file.
#
# _revise_beliefs and _propose_module were first written with an opt-out:
# "give the number 0 if they all still hold", and the module name "none".
# Both returned nothing 0/3 against real data, including a deliberately
# seeded belief the transcript flatly contradicted. That is the SAME
# failure as the curiosity trigger's two silent months, and the note added
# there claimed a number in a flat shape was safe from it. It is not.
# Anything that can be read as "no" gets read as "no".
#
# Fixed by removing every opt-out and replacing it with a 0-10 strength
# field that is always populated, gated here in code. The model still gets
# to say "weakly" — it just cannot say nothing, and the decision about what
# counts as strong enough is deterministic and visible instead of buried in
# a sampling preference.
#
# Both cutoffs are starting points, not tuned. Chosen so a merely plausible
# answer does not act: the observed spread on real data is what these
# should be re-derived from once there is more of it.
DOUBT_TO_REVISE = 7
NEED_TO_PROPOSE = 7

# How many existing beliefs get checked per pass, sampled at random so
# everything is eventually revisited without checking all of them every
# time. One LLM call each — see _revise_belief for why they are checked one
# at a time rather than as a list.
BELIEFS_CHECKED_PER_PASS = 3

# A revision has to still be ABOUT the thing it revises. Measured on real
# output from this function:
#
#   -0.056  "The sun is a star."  ->  a replacement about Craig   (reject)
#    0.406  a real revision of a belief about Craig               (keep)
#    0.653  "The sun is a star." -> "The sun is a star, and it
#            provides light and energy to the Earth..."           (reject)
#    0.754  another real revision of the same belief              (keep)
#
# The off-topic case is far below everything real, so a floor works for it.
# **A ceiling does not work for the restatement case** — 0.653 sits BELOW a
# genuine revision at 0.754, so any threshold that rejects the restatement
# also rejects real work. That was worth measuring rather than assuming;
# the restatement is caught by _is_restatement() instead, which is exact.
REVISION_TOPIC_FLOOR = 0.25

# Proposing a module writes a row Craig has to look at. One pending at a
# time is not enough on its own — declined ones stop counting, and a 7B
# model asked "what are you missing" always has an answer. A week between
# proposals keeps the volume at something he will actually read.
PROPOSAL_COOLDOWN_DAYS = 7

# What her installed modules actually do, so a proposal is judged against
# capability rather than against a list of names. Keyed by registry name;
# anything unlisted falls back to "installed".
_MODULE_PURPOSE = {
    "diagnostic_tool": "check your own systems and modules and report faults",
    "inquiry": "search the web, with his approval, and keep what you find",
    "recall": "look things up in your own database and past conversations",
}

# Names that describe a permission rather than a capability. A module called
# "database_access" is a scope with a module's clothes on.
_SCOPE_WORDS = (
    "access", "permission", "privilege", "scope", "admin", "root",
    "database_", "_database", "filesystem", "network_", "_network", "sudo",
)

# Per-pass cap. She can conclude one thing per reflection, not a list.
MAX_CONCLUSIONS_PER_PASS = 1


async def _form_conclusion(recent, snap, about: str = None):
    """Notice something and write it down, with what it is based on.

    Given her own state as well as the transcript, so "notice a pattern
    about itself" has something to look at — her response times, what is
    switched off, how often she has been re-wording herself. Reflection
    previously only ever saw the conversation.

    IMPORTANT, and stated here because it is load-bearing: a conclusion
    she forms does NOT reach her conversational answers until Craig has
    confirmed it (status='confirmed', set at the Controller's Reasoning
    tab). 2026-09-21: the unconfirmed version was wired into every reply
    the night before, and within hours four beliefs that he was "testing"
    and "provoking" her had her reading a plain correction as provocation,
    holding a false position for four turns, and then concluding from that
    argument that he was provoking her. Unverified self-inference steering
    behaviour is the confabulation loop with a different seed. Only he can
    open the gate, and only per belief."""
    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )

    prompt = f"""You are A.L.E.X, privately thinking over what has just happened.

What you know about your own current state:
{self_model.describe(snap)}

Recent conversations:
{convo_text}

{"What is one thing you have worked out from this — about yourself, about Craig, or about the world?" if about else "The people in this conversation are NOT Craig; nothing here is about him. What is one thing you have worked out from this — about yourself, or about the world?"} Not a summary of what was said. Something you now think is true that you had not put into words before.

Say what it is based on, specifically, from the state or the conversation above.

Respond with ONLY a JSON object:
{{"statement": "<what you now think, one or two sentences>", "kind": "<{"self, craig, or world" if about else "self or world"}>", "evidence": "<what in the above led you there>"}}"""

    result = await ollama_manager.generate_json(prompt, timeout=30.0)
    if not result:
        return None

    statement = str(result.get("statement", "")).strip()[:600]
    evidence = str(result.get("evidence", "")).strip()[:600]
    kind = str(result.get("kind", "self")).strip().lower()

    if kind not in ("self", "craig", "world"):
        kind = "self"

    # 2026-09-21 (Craig: "the last interaction was incorrectly directed at
    # me instead of the user ALEX was interacting with"): belief #16 about
    # Craig was formed from a tester's conversation. A belief about him
    # comes only from turns that were his; a window with no turn of his
    # cannot produce one, whatever the model labels it.
    if kind == "craig" and not about:
        return None
    # Evidence is required, not decorative. A conclusion with nothing
    # behind it is a guess wearing a conclusion's clothes, and Design
    # Principle 1 says that is the one thing she does not get to do.
    if not statement or not evidence:
        return None

    if about is None:
        speakers = sorted({r["user"] for r in recent if r.get("user")})
        if speakers:
            evidence = f"(with {', '.join(speakers)}) " + evidence
    return {"statement": statement, "kind": kind, "evidence": evidence}


def _is_restatement(original: str, replacement: str) -> bool:
    """Does the "revision" just repeat the belief and carry on talking?

    Observed: "The sun is a star." came back as "The sun is a star, and it
    provides light and energy to the Earth, making life possible..." — an
    agreement padded into the shape of a revision, which would have retired
    a perfectly good belief and replaced it with a rambling version of
    itself.

    Exact containment rather than similarity, because similarity cannot
    separate the two cases (see REVISION_TOPIC_FLOOR). A genuine revision
    contradicts the original, so it does not contain the original verbatim;
    a restatement almost always opens with it."""
    a = " ".join(original.lower().split()).rstrip(".!?")
    b = " ".join(replacement.lower().split())
    return bool(a) and a in b


async def _revise_belief(recent, conclusion):
    """Check ONE belief against what just happened.

    2026-09-20 — this originally took the whole list and asked which one
    to doubt. Measured against real data, it named the wrong one every
    time: three runs all said "revise #2", where #2 was the control belief
    "The sun is a star", while the replacement text they produced was
    plainly about #1 (Craig). It had the reasoning roughly right and the
    bookkeeping wrong, which is the worst combination available —
    superseding a correct belief and overwriting it with unrelated text is
    strictly worse than never revising anything.

    Selecting an item from a numbered list is the part this model class is
    bad at, so the fix is to remove the selection rather than explain it
    better. One belief per call, no id to get wrong. Costs N calls per
    pass, capped by BELIEFS_CHECKED_PER_PASS and paid during an idle
    stretch.

    Returns (doubt, replacement, reason) or None."""
    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )

    prompt = f"""You are A.L.E.X, privately checking one thing you believe against what just happened.

What you concluded earlier:
"{conclusion['statement']}"

Recent conversations:
{convo_text}

How much do these conversations give you reason to doubt that, from 0 to 10? 0 means nothing here bears on it at all. 10 means they plainly showed it is wrong.

Then say what you would think instead, about that same thing, and what changed your mind.

Respond with ONLY a JSON object:
{{"doubt": <0 to 10>, "replacement": "<what you would think instead, about that same thing>", "reason": "<what changed your mind>"}}"""

    result = await ollama_manager.generate_json(prompt, timeout=30.0)
    if not result:
        return None

    try:
        doubt = int(result.get("doubt"))
    except (TypeError, ValueError):
        return None

    if doubt < DOUBT_TO_REVISE:
        return None

    replacement = str(result.get("replacement", "")).strip()[:600]
    reason = str(result.get("reason", "")).strip()[:400]

    if not replacement or not reason:
        return None

    # Deterministic guard on the OTHER half of the failure above: a
    # replacement that is about something else entirely is not a revision,
    # whatever the model called it. Cheap, local, no second LLM call.
    if cosine_similarity(embed(replacement), embed(conclusion["statement"])) < REVISION_TOPIC_FLOOR:
        logger.info(
            f"[REFLECTION] Discarded a revision of #{conclusion['id']} — the "
            f"replacement is about something else: {replacement[:80]!r}")
        return None

    if _is_restatement(conclusion["statement"], replacement):
        logger.info(
            f"[REFLECTION] Discarded a revision of #{conclusion['id']} — it "
            f"restates the belief rather than revising it")
        return None

    return doubt, replacement, reason


async def _propose_module(recent, snap):
    """Name a capability she does not have, and raise a real build request.

    Design Principle 11 is what makes this safe to do unprompted: she
    builds freely, nothing she builds becomes active without Craig saying
    so. A proposal is the "freely" half. The row lands in
    module_build_requests exactly like any other, pending his approval,
    and origin='self' marks that nobody asked her for it.

    Deliberately conservative about what counts: it has to be a capability
    she was actually reaching for in these conversations, not an idea."""
    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )
    # 2026-09-20: this used to pass module NAMES only, and the very first
    # proposal it produced was "database_access — to store and retrieve
    # information about various topics". She already has `recall` (db scope)
    # and learned_knowledge does exactly that; she proposed something she
    # owns because nothing told her what "recall" is. Names alone are not a
    # description.
    have = "\n".join(
        f"  - {m['name']}: {_MODULE_PURPOSE.get(m['name'], 'installed')}"
        for m in snap.get("modules", []) if m.get("status") == "enabled"
    ) or "  - none"

    prompt = f"""You are A.L.E.X, privately reviewing what you could not do.

What you can already do:
{have}
  - remember conversations, recall them, and store things you have looked up
  - check your own systems and report what is wrong

Recent conversations:
{convo_text}

What capability were you missing in these conversations? Name the module you would need, in snake_case, describe what it should do, and say how much these conversations actually showed you needed it, from 0 to 10.

0 means nothing here needed it and you are only naming something that might be nice. 10 means you were plainly stuck without it.

Respond with ONLY a JSON object:
{{"name": "<module_name_in_snake_case>", "purpose": "<what it would do and what you could not do without it>", "need": <0 to 10>}}"""

    result = await ollama_manager.generate_json(prompt, timeout=30.0)
    if not result:
        return None

    name = str(result.get("name", "")).strip().lower()
    purpose = str(result.get("purpose", "")).strip()[:600]

    try:
        need = int(result.get("need"))
    except (TypeError, ValueError):
        return None

    if not name or not purpose:
        return None

    # The gate, in code. She always names something — whether it gets
    # raised as a real build request awaiting Craig's approval does not
    # depend on her declining to answer. See NEED_TO_PROPOSE.
    if need < NEED_TO_PROPOSE:
        logger.info(
            f"[REFLECTION] Thought of {name!r} but rated the need {need}/10 — "
            f"below {NEED_TO_PROPOSE}, not proposing it")
        return None

    # Deterministic sanity on the name — this becomes a real registry key
    # and eventually a directory, so it is validated here rather than
    # trusted. Same reasoning as the placeholder check in
    # _reflect_on_phrase: a structural check, not a better prompt.
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", name):
        logger.info(f"[REFLECTION] Discarded module proposal with unusable name: {name!r}")
        return None

    # A module is a capability, not a permission. 2026-09-20: the first
    # proposal this ever made was named "database_access", which reads like a
    # privilege-escalation request — Craig's reaction was "she requested
    # database access. Not sure why and the request itself is very vague."
    # It was not asking for a scope (requested_access was None), but a name
    # that looks like one is alarming and tells him nothing about what it
    # would DO. Deterministic rather than a prompt instruction, for the same
    # reason as every other check in this file.
    if any(w in name for w in _SCOPE_WORDS):
        logger.info(
            f"[REFLECTION] Discarded module proposal {name!r} — that names a "
            f"permission, not a capability")
        return None

    if name in {m["name"] for m in snap.get("modules", [])}:
        return None

    return name, purpose


async def run_self_reflection():
    # Idle gate, checked first and cheaply (no LLM call) — don't even look
    # at whether there's new conversation to reflect on until there's
    # been a real lull. Two independent signals, whichever is MORE
    # RECENT wins (the smaller "seconds ago" value): real conversation
    # (memory) and a direct personality edit (personality_log — set via
    # the Controller or a chat override, neither of which ever touch
    # `memory` at all). Found live (2026-07-17, Craig: "I modified her
    # personality via the controller" right after being told nothing new
    # had happened) — a Controller edit is real engagement and should
    # delay the next autonomous pass the same way a real conversation
    # does, not be invisible to the gate. None from either source means
    # "not idle enough to tell" rather than assumed infinitely idle.
    seconds_since_conversation = await get_seconds_since_last_activity()
    seconds_since_personality_edit = await get_seconds_since_last_personality_change()

    candidates = [s for s in (seconds_since_conversation, seconds_since_personality_edit) if s is not None]
    if not candidates:
        return

    seconds_idle = min(candidates)
    if seconds_idle < IDLE_BEFORE_REFLECTION_S:
        return

    # 2026-07-17: found live — Craig noticed personality drifting every
    # 15-30 minutes with zero real interaction behind it. Root cause:
    # this used to sample the most recent 20 conversation turns
    # UNCONDITIONALLY on every pass, with no memory of what it already
    # reflected on last time. Shortening the interval to 900s (from
    # 3600s, so evolution would be visible within a session) turned that
    # latent issue into a real, visible one — during any quiet stretch,
    # every single pass was re-reflecting on the exact same stale
    # conversation window, and the model's own sampling variance (not
    # temperature=0, unlike the classifiers — genuine variety is wanted
    # here) produced a slightly different reworded personality each time,
    # a random walk with no real signal behind it. Fixed by bookmarking
    # the newest memory row id actually reflected on and only proceeding
    # if there are genuinely NEW turns since then.
    last_id = await get_last_reflection_memory_id()
    recent = await fetch_recent_memory_all(limit=20, since_id=last_id)

    if len(recent) < MIN_CONVERSATIONS_FOR_REFLECTION:
        return

    # 2026-09-25 (Craig, on the pet reaching nothing: "so let's add it. Again
    # she should be able to do this for everything by default"). What she DID
    # is as much a fact about the window as what was said in it, and every
    # feature already records its acts as decisions — so this one read gives
    # reflection and curiosity all of them at once, the pet included, and
    # anything a later feature records without further wiring.
    try:
        from db.db import fetch_decisions
        her_acts = [d for d in await fetch_decisions(limit=40)
                    if d.get("actor") in ("alex", "her") and d.get("kind") not in ("tool", "speaker")]
    except Exception as e:
        logger.warning(f"⚠️ could not read what she has done: {e}")
        her_acts = []

    await set_last_reflection_memory_id(recent[-1]["id"])

    # 2026-09-21: whose turns these are. Beliefs about Craig come only
    # from Craig's turns; another person's conversation can teach her
    # about herself or the world, never about him.
    creator = await get_creator_name()
    craig_turns = [r for r in recent if r.get("user") == creator]
    other_turns = [r for r in recent if r.get("user") != creator]

    # 2026-09-25 (Craig: "on the off chance I do not catch something can
    # she check her previous statements for hallucinations and correct?"):
    # her own review. Every reference to past conversation in her recent
    # replies is checked against what he actually said; an unsupported
    # one is retracted from her memory, recorded as a slip she can see,
    # and its shape learned (core/claims.py).
    try:
        from core import claims as her_claims
        from db.db import fetch_user_prompts, retract_memories
        by_user = {}
        for r in recent:
            by_user.setdefault(r.get("user"), []).append(r)
        for who, rows in by_user.items():
            if not who:
                continue
            bad = await her_claims.review_replies(rows, await fetch_user_prompts(who, days=30))
            for mem_id, sent, obj in bad:
                logger.info(f"[CLAIM] her review: {sent[:90]!r} refers to {obj!r} — nothing in {who}'s words; retracted")
                try:
                    await retract_memories([mem_id], f"her own review: a reference to {obj!r} that {who} never said")
                except Exception:
                    pass
                try:
                    await record_decision(
                        "fabrication", f"On review she found she had invented a reference: {sent[:100]!r}",
                        reasoning=f"Her review compared the reference with everything {who} said in 30 days: nothing about {obj!r}.",
                        evidence=sent[:300], outcome="retracted from her memory; the shape is now watched for", actor="alex")
                except Exception:
                    pass
                await her_claims.learn(her_claims.skeleton(sent), "her review", sent)
    except Exception as e:
        logger.warning(f"[CLAIM] her review failed: {e}")

    # 2026-07-18 (Craig: "if she's working on something in the background
    # like tuning herself would it show that?") — everything from here to
    # the end of this function is the actual "work" (multiple real LLM
    # calls); __SELFWORK__1/0 lets the avatar UI show something honest
    # during that window instead of self-reflection being invisible the
    # whole time it runs. finally guarantees the "done" signal fires even
    # if reflection errors out partway — a stuck "she's working" state
    # with no way to know it's actually finished would be worse than not
    # having the indicator at all.
    from ws.ws_handlers import send_signal_to_creator
    await send_signal_to_creator("__SELFWORK__1")

    # 2026-09-20 — reflection was unobservable unless it changed something.
    #
    # Craig asked whether she had used an idle stretch for anything. The
    # bookmark had advanced to his newest turn, so she had genuinely
    # reflected on everything — and produced no personality change and no
    # curiosity question, logging neither. From outside, a pass that
    # considered 20 turns and decided nothing is indistinguishable from a
    # pass that never ran, or from the scheduler being broken. His
    # question — "So other than run the reflection... it didn't actually
    # DO anything?" — could not be answered from her own records, only by
    # reading her database by hand.
    #
    # So every pass now says what it looked at and what it decided, "no
    # change" included. This is logging, not a new mechanism: deciding not
    # to change is a legitimate outcome and the point is that it is now a
    # visible one.
    outcome = []
    logger.info(f"[REFLECTION] Pass starting — {len(recent)} new turns since #{last_id}")

    try:
        # 2026-09-23: curiosity is about somebody's conversation; it is
        # asked of that somebody. Craig's turns first; otherwise whoever
        # spoke most in this window.
        if craig_turns:
            curious_turns, curious_user = craig_turns, creator
        else:
            counts = {}
            for r in other_turns:
                counts[r.get("user")] = counts.get(r.get("user"), 0) + 1
            curious_user = max(counts, key=counts.get) if counts else None
            curious_turns = [r for r in other_turns if r.get("user") == curious_user]
        try:
            noticed = []
            try:
                from db.db import fetch_observations
                noticed = [o["text"] for o in await fetch_observations(curious_user, hours=6.0, limit=5)]
            except Exception:
                noticed = []
            curiosity = await _reflect_on_curiosity(curious_turns, noticed=noticed, her_acts=her_acts)
        except Exception as e:
            logger.warning(f"⚠️ Curiosity reflection failed: {e}")
            curiosity = None

        if curiosity:
            topic, question = curiosity
            await queue_curiosity_question(topic, question, user=curious_user)
            await record_decision(
                "curiosity",
                f"Wants to know about {topic}",
                reasoning="It came up and she had no real knowledge of it.",
                evidence=question,
                outcome="queued to ask him")
            outcome.append(f"queued a question about {topic!r}")
            logger.info(f"[ACTION] Queued curiosity question: {question}")
        else:
            outcome.append("no question")

        # -------------------------------------------------------------
        # CONCLUSIONS, REVISION, PROPOSALS (2026-09-20)
        # -------------------------------------------------------------
        # Ordered deliberately: revise first, then conclude. Checking old
        # beliefs against new evidence BEFORE adding a new one means a
        # correction supersedes the thing it corrects, instead of landing
        # next to it as a second, contradictory active belief — the failure
        # create_learned_knowledge's supersede logic exists to prevent.
        snap = await self_model.snapshot()
        existing = await fetch_active_conclusions(limit=15)

        # Sampled rather than exhaustive: one LLM call each, and everything
        # gets revisited across passes without every pass paying for all of
        # them.
        revised = 0
        for conclusion in random.sample(existing, min(BELIEFS_CHECKED_PER_PASS, len(existing))):
            if conclusion.get("kind") == "craig" and not craig_turns:
                continue    # nothing he said this pass; a stranger's words cannot revise a belief about him
            try:
                revision = await _revise_belief(craig_turns if conclusion.get("kind") == "craig" else recent, conclusion)
            except Exception as e:
                logger.warning(f"⚠️ Belief revision failed: {e}")
                continue

            if not revision:
                continue

            doubt, replacement, reason = revision
            # A revision of something he confirmed comes back unconfirmed:
            # his confirmation was of the old wording, not of whatever she
            # turns it into. See db.LIVE_CONCLUSION_STATUSES.
            was_confirmed = conclusion.get("status") == "confirmed"
            new_id = await create_conclusion(
                replacement, conclusion["kind"],
                f"revised from #{conclusion['id']} (doubt {doubt}/10): {reason}",
                supersedes=conclusion["id"], reason=reason)
            revised += 1
            await record_decision(
                "revision",
                f"Changed her mind: {conclusion['statement']}",
                reasoning=reason,
                evidence=f"doubt {doubt}/10 after re-reading recent conversation",
                outcome=f"replaced with: {replacement}" + (
                    " — he had confirmed the old one; this one is unconfirmed "
                    "until he says so" if was_confirmed else ""),
                ref=f"conclusions#{new_id}")
            outcome.append(f"revised #{conclusion['id']} -> #{new_id}")
            logger.info(
                f"[ACTION] Revised conclusion #{conclusion['id']} -> #{new_id} "
                f"(doubt {doubt}/10): {replacement} (reason: {reason})")

        if revised:
            existing = await fetch_active_conclusions(limit=15)
        else:
            outcome.append("no revision")

        try:
            conclusion = (await _form_conclusion(craig_turns, snap, about=creator) if craig_turns
                          else await _form_conclusion(other_turns, snap, about=None))
        except Exception as e:
            logger.warning(f"⚠️ Conclusion forming failed: {e}")
            conclusion = None

        if conclusion:
            # Deterministic duplicate check. Without it she re-concludes
            # the same thing every quiet stretch and the table becomes a
            # log of one thought — the shape of the personality random
            # walk this file already records (2026-07-17), arrived at from
            # a different direction.
            vec = embed(conclusion["statement"])
            dup = None
            for c in existing:
                if cosine_similarity(vec, embed(c["statement"])) >= CONCLUSION_DUPLICATE_THRESHOLD:
                    dup = c
                    break

            if dup:
                outcome.append(f"conclusion already held (#{dup['id']})")
                logger.info(
                    f"[REFLECTION] Already concluded this (#{dup['id']}): "
                    f"{conclusion['statement']}")
            else:
                cid = await create_conclusion(
                    conclusion["statement"], conclusion["kind"],
                    conclusion["evidence"])
                await record_decision(
                    "conclusion",
                    f"Decided: {conclusion['statement']}",
                    reasoning="Her own inference while reflecting on recent "
                              "conversation and her own state.",
                    evidence=conclusion["evidence"],
                    outcome="recorded as an active belief",
                    ref=f"conclusions#{cid}")
                outcome.append(f"concluded #{cid}")
                logger.info(
                    f"[ACTION] Concluded #{cid} ({conclusion['kind']}): "
                    f"{conclusion['statement']} — based on: {conclusion['evidence']}")
        else:
            outcome.append("nothing concluded")

        # A module proposal is a real row awaiting his approval, so the
        # guard is stricter than the others: never a second pending one.
        # Principle 11 makes proposing free, but a queue of unreviewed
        # proposals is the Activity-tab problem Craig has already named
        # ("things just sit there, I assume forever").
        try:
            builds = await fetch_recent_module_build_requests()
            pending_builds = [r for r in builds if r.get("status") == "pending"]
            blocked = _proposed_recently(builds)
        except Exception as e:
            logger.warning(f"⚠️ Build-request read failed: {e}")
            pending_builds, blocked = None, True

        if pending_builds:
            outcome.append("no proposal (one already pending)")
        elif blocked:
            outcome.append(f"no proposal (proposed one within {PROPOSAL_COOLDOWN_DAYS}d)")
        elif pending_builds is not None:
            try:
                proposal = await _propose_module(recent, snap)
            except Exception as e:
                logger.warning(f"⚠️ Module proposal failed: {e}")
                proposal = None

            if proposal:
                name, purpose = proposal
                rid = await create_module_build_request(
                    await get_creator_name(), name, purpose,
                    status="pending", origin="self_reflection")
                await record_decision(
                    "proposal",
                    f"Asked to build a module: {name}",
                    reasoning=purpose,
                    evidence="a capability she reached for and did not have",
                    outcome="build request raised, waiting on his approval",
                    ref=f"module_build_requests#{rid}")
                outcome.append(f"proposed module {name!r} (request #{rid})")
                logger.info(
                    f"[ACTION] Proposed a module she does not have: {name} "
                    f"(request #{rid}, awaiting approval) — {purpose}")
            else:
                outcome.append("no proposal")

        # 2026-09-20: while the persona switch is on she is speaking in a
        # neutral voice that is not hers, so letting her reflect on "how these
        # went" would evolve her personality from conversations her personality
        # never took part in — and then that drift would be waiting when the
        # switch comes off. Curiosity above still runs; it is about the world,
        # not about who she is. Phrase re-voicing below is skipped with it,
        # since it is downstream of a personality change that cannot happen.
        if persona_disabled():
            outcome.append("personality reflection skipped (persona switch on)")
            logger.info(f"[REFLECTION] Pass complete — {'; '.join(outcome)}")
            return

        # 2026-09-21: he wrote it himself and locked it. Her own edits to
        # her personality wait until he unlocks it at the Controller;
        # phrase re-voicing goes with them, being downstream of a change
        # that cannot happen.
        if await get_personality_locked():
            outcome.append("personality locked by Craig — not rewritten")
            logger.info(f"[REFLECTION] Pass complete — {'; '.join(outcome)}")
            return

        hard_rules = await get_personality_hard_rules()
        personality_change = await _reflect_on_personality(recent)

        if not personality_change:
            outcome.append("personality unchanged")
            logger.info(f"[REFLECTION] Pass complete — {'; '.join(outcome)}")
            return

        new_desc, reason = personality_change

        # 2026-09-20 — the other half of the standing objective: "give her
        # the capacity to disagree, and stop the reflection loop from
        # sanding it off." personality_log shows the sanding happening —
        # 13 of 27 self-initiated changes state their own reason as some
        # form of "to better match what Craig wants", including #270, which
        # reverted an explicit creator instruction 89 seconds after he gave
        # it. See core/skeptic.py.
        #
        # Creator overrides never reach here; this is only her own
        # unprompted edits to herself.
        current_desc = await get_personality(raw=True)
        allowed, why = await skeptic.permits(
            current_desc, new_desc, reason, hard_rules=hard_rules)

        if not allowed:
            await record_decision(
                "refusal",
                "Stopped herself changing her own personality",
                reasoning=f"The skeptic refused it: {why}",
                evidence=f"she proposed: {new_desc}",
                outcome="personality left as it was")
            outcome.append(f"refused own personality change ({why})")
            logger.info(
                f"[PERSONALITY] Refused her own change — {why}. "
                f"Proposed: {new_desc!r} (reason: {reason!r})")
            logger.info(f"[REFLECTION] Pass complete — {'; '.join(outcome)}")
            return

        await set_personality(new_desc)
        await log_personality_change(new_desc, reason, kind="personality")

        outcome.append(f"personality changed ({reason})")
        logger.info(f"[PERSONALITY] Personality evolved: {new_desc} (reason: {reason})")
        logger.info(f"[REFLECTION] Pass complete — {'; '.join(outcome)}")

        # 2026-09-25 (Craig: "is there even an argument for her to have them
        # anymore?"). The phrase re-voicing pass is gone. It stored a rewritten
        # wording per phrase, and core/phrasebook.py used that stored text both
        # as a voice reference for composing and as the verbatim fallback when
        # composing failed — so a July rewrite on the 7b ("Say 'hello, party
        # animal'") was still being spoken in September, and still pulling
        # composed lines toward itself. Every one of those lines is now composed
        # fresh from its registry INTENT and her personality as it is, so
        # durable drift bought nothing and cost that.
    finally:
        await send_signal_to_creator("__SELFWORK__0")
