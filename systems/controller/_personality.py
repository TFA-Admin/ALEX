# systems/controller/_personality.py

"""
Personality set/reset/query, phrase reset, and role grant/revoke
(creator only, own override code — "role" stays fully locked from the
generic fact-update flow in permissions/system.py; this is the one
deliberate, narrow path that can touch it, and only ever to/from
"super_user", refusing to ever touch anyone whose current role is
"creator"). Split out of system.py (2026-07-16).
"""

from db.db import (
    get_personality_locked,
    get_user_role, set_personality, get_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases, fetch_user_facts, update_fact,
    profile_exists, find_profile_by_prefix, add_personality_hard_rule,
    clear_personality_hard_rules
)
import time

from core.intent_classifier import classify_personality_set, merge_personality_change
from core.text_utils import first_word, YES_WORDS, NO_WORDS

from core.override_code import override_code_status, strip_override_code_mention
from core import corrections as corr
from config.logger_config import logger

from systems.controller._role_gates import require_creator
from core.phrasebook import get_phrase

# 🔒 Kept as a deterministic list (not LLM judgment) because "reset" is a
# small, enumerable phrase space, and — confirmed live — a probabilistic
# classifier asked to detect "reset" produced dangerous false positives on
# totally unrelated messages ("reset the router" -> would have reset her
# personality). False negatives here (an unrecognized reset phrasing) just
# mean the creator has to rephrase; false positives would silently corrupt
# state, so this stays fixed logic. See core/intent_classifier.py's
# classify_personality_set() docstring for the "set" side of this decision.
PERSONALITY_RESET_TRIGGERS = (
    "reset your personality",
    "go back to your default personality",
    "go back to default",
    "default personality",
)

# 2026-07-16 (Craig: "can we lock things like the set or reset overrides
# behind my override code?") — reset personality, set personality, and
# reset phrases now all require the override code stated somewhere in the
# same utterance, on top of the existing creator+voice-verification gate,
# not instead of it. Plain phrasing without the code is refused outright.
# See core/override_code.py for the shared check (also used by
# systems/modules/system.py's build-confirmation gate).


async def handle(session, user_id: str, text: str, msg: str):
    """Returns a response dict if this category handled the message,
    None otherwise (caller tries the next category)."""

    # -------------------------
    # "DID YOU MEAN THAT AS A CHANGE TO HOW I AM?" — his answer (2026-09-25)
    # -------------------------
    pending = session.pop("persona_confirm", None)
    if pending and time.time() - float(pending.get("t") or 0) < PERSONA_CONFIRM_S:
        word = first_word(msg)
        if word in YES_WORDS:
            result = await change_from(session, user_id, pending["text"], pending["msg"])
            if result:
                return result
            return {"type": "response", "content": await get_phrase("persona_not_a_change")}
        if word in NO_WORDS:
            logger.info(f"[PERSONALITY] Not a change, he says: {pending['text'][:80]!r}")
            return {"type": "response", "content": await get_phrase("persona_not_a_change")}
        # anything else: he moved on; the question lapses

    # -------------------------
    # GRANT / REVOKE super_user
    # -------------------------
    if "with override code" in msg:
        idx = msg.index("with override code")
        code = text.strip()[idx + len("with override code"):].strip()
        before = msg[:idx].strip()

        action, name = None, None

        if before.startswith("grant super user to "):
            action = "grant"
            name = before[len("grant super user to "):].strip()
        elif before.startswith("revoke super user from "):
            action = "revoke"
            name = before[len("revoke super user from "):].strip()

        if action and name:
            denial = await require_creator(user_id, session, text)
            if denial:
                return denial

            target = name if await profile_exists(name) else await find_profile_by_prefix(name)

            if not target:
                return {"type": "response", "content": await get_phrase("profile_not_found", name=name)}

            creator_facts = await fetch_user_facts(user_id)
            override_code = str(creator_facts.get("override_code", "")).strip()

            if not override_code or not code or code != override_code:
                return {"type": "response", "content": await get_phrase("invalid_override_code")}

            target_role = await get_user_role(target)

            if target_role == "creator":
                return {"type": "response", "content": await get_phrase("cannot_change_creator_role")}

            new_role = "super_user" if action == "grant" else "user"
            await update_fact(target, "role", new_role)

            logger.info(f"[ACTION] Role change: '{target}' -> {new_role} ({action} by creator {user_id})")

            phrase_key = "super_user_granted" if action == "grant" else "super_user_revoked"
            return {
                "type": "response",
                "content": await get_phrase(phrase_key, target=target)
            }

    # -------------------------
    # PERSONALITY OVERRIDE (creator only — she develops her own
    # personality autonomously, but the creator can always step in)
    # -------------------------
    if any(t in msg for t in PERSONALITY_RESET_TRIGGERS):

        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        status = await override_code_status(user_id, msg)
        if status == "absent":
            return {"type": "response", "content": await get_phrase("personality_override_code_required")}
        if status == "invalid":
            return {"type": "response", "content": await get_phrase("invalid_override_code")}

        if await get_personality_locked():
            return {"type": "response", "content": await get_phrase("personality_locked")}

        await set_personality(DEFAULT_PERSONALITY)
        await clear_personality_hard_rules()
        await log_personality_change(DEFAULT_PERSONALITY, "creator reset to default", kind="personality")
        logger.info("[PERSONALITY] Creator reset personality to default.")

        return {
            "type": "response",
            "content": await get_phrase("personality_reset")
        }

    new_desc = None
    raw_instruction = None

    # Stripped the same way as the classifier fallback below — "override
    # code X set your personality to Y" doesn't start with either trigger
    # literally, since "override code X" comes first; a plain .startswith()
    # here would miss it the same way the classifier did before the strip.
    exact_phrase_input = strip_override_code_mention(text, msg)
    exact_phrase_msg = exact_phrase_input.lower()

    for trigger in ("set your personality to", "override your personality to"):
        if exact_phrase_msg.startswith(trigger):
            new_desc = exact_phrase_input.strip()[len(trigger):].strip().strip('."\'')
            raw_instruction = new_desc
            break

    # Fallback for phrasing that isn't the exact literal command (e.g.
    # "be snarkier") — only asked for creator messages that didn't
    # already match a fixed phrase above, via a dedicated classifier
    # call (see core/intent_classifier.py's classify_personality_set()
    # docstring for why this is a separate call, not folded into the
    # shared one — and why the current-personality merge below is its
    # own separate step, not part of this classification call).
    # 2026-09-21: a correction is not a personality change. "Stop saying
    # hell and my name so much" was classified as a personality set here
    # (the classifier's own prompt lists "stop saying X" as one), gated
    # behind the override code, and answered with an invented code
    # ("CHANGEMYMODE123") — while the corrections system built for exactly
    # that sentence (core/corrections.py: no code, escalating, his) sat at
    # priority 100 and never saw it. Which system handled "stop saying X"
    # was a 7B judgment call. Craig, to her: "No, I'm not trying to
    # override you. I'm just trying to correct you."
    #
    # Deterministic: if it reads as a correction and carries no override
    # code, it is a correction, and it falls through to
    # systems/llm/system.py. The explicit forms — "set your personality
    # to ...", the reset phrases, anything said WITH the code — are
    # untouched, so a durable rule is still one sentence away when that is
    # what he means.
    if (new_desc is None and corr.is_correction(exact_phrase_input)
            and await override_code_status(user_id, msg) == "absent"):
        return None

    # 2026-09-23: an answer to her question is not a personality change.
    # She asked "what logic changes are you implementing when you say you
    # are making tweaks to my code?"; he answered "I'm improving you. I'm
    # trying to make you more reactive, more intelligent, more self-aware";
    # the classifier below read that as a personality set, she demanded
    # his override code, and the answer she had asked for was lost — the
    # next thing he said ("no, I'm not trying to change your personality")
    # was stored as the answer instead. While she is waiting for an answer
    # (systems/llm/system.py keeps the capture), what he says with no code
    # in it is the answer and falls through to be kept as one.
    if (new_desc is None and session.get("awaiting_curiosity_answer")
            and await override_code_status(user_id, msg) == "absent"):
        logger.info("[PERSONALITY] Not a change — she is waiting for his answer "
                    f"about {session.get('awaiting_curiosity_answer')!r}")
        return None

    # 2026-09-25: no classifier call here any more. Whether an ordinary
    # sentence is asking her to change is judged once per turn on the
    # intent-and-needs call (core/intent_classifier.py, "persona"), and
    # systems/intent/system.py hands the score to on_persona_score() below.
    # Measured: the dedicated call ran on every creator message at 0.85 s
    # and answered "no" every time.
    if new_desc is not None:
        return await _apply_change(session, user_id, text, msg, new_desc, raw_instruction)

    if msg.startswith("what is your personality") or msg.startswith("what's your personality"):

        current = await get_personality()

        return {
            "type": "response",
            "content": current
        }

    if exact_phrase_msg.startswith("reset your phrases") or exact_phrase_msg.startswith("reset how you talk"):

        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        status = await override_code_status(user_id, msg)
        if status == "absent":
            return {"type": "response", "content": await get_phrase("personality_override_code_required")}
        if status == "invalid":
            return {"type": "response", "content": await get_phrase("invalid_override_code")}

        await reset_all_phrases()
        await log_personality_change("(all reset to defaults)", "creator reset", kind="phrases")
        logger.info("[PERSONALITY] Creator reset all phrases to defaults.")

        return {
            "type": "response",
            "content": await get_phrase("phrases_reset")
        }

    return None


# ---------------------------------------------------------------------------
# A CHANGE TO HOW SHE IS, from a score (2026-09-25)
# ---------------------------------------------------------------------------
# Craig: "would it be possible for her to determine based on the sentence
# whether something is personality related and THEN kick on that process?,
# possibly even with a check before hand if it's the creator role talking
# then ask if that was meant to be a personality adjustment or not. If it's
# just a normal user she should likely just ignore it since a normal user
# can't adjust her personality."
#
# The sentence is judged once per turn, on the intent-and-needs call, as a
# 0-10 score ("persona"). Nobody but the creator gets past the first line.
# At PERSONA_SURE and above the careful classifier (0 false positives in 62
# adversarial trials) confirms it and the override-code gate follows, as
# before. Between PERSONA_MAYBE and PERSONA_SURE she asks him whether he
# meant it; his yes runs the same path, his no drops it, anything else
# lapses. Below PERSONA_MAYBE nothing happens. No word list anywhere.
PERSONA_SURE = 7
PERSONA_MAYBE = 4
PERSONA_CONFIRM_S = 120.0


async def _apply_change(session, user_id: str, text: str, msg: str, new_desc, raw_instruction):
    """The tail every change shares: creator, a value, the override code,
    the lock, then the write."""
    denial = await require_creator(user_id, session, text)
    if denial:
        return denial

    if not new_desc:
        return {"type": "response", "content": await get_phrase("personality_prompt_for_value")}

    status = await override_code_status(user_id, msg)
    if status == "absent":
        return {"type": "response", "content": await get_phrase("personality_override_code_required")}
    if status == "invalid":
        return {"type": "response", "content": await get_phrase("invalid_override_code")}

    # 2026-09-21: his written description stays his. See
    # db.get_personality_locked().
    if await get_personality_locked():
        return {"type": "response", "content": await get_phrase("personality_locked")}

    await set_personality(new_desc)

    # 2026-07-16: found live — merge_personality_change() re-summarizes
    # the whole flowing description from scratch each time, and a real
    # instruction ("without using emojis") got silently dropped the very
    # next time a different instruction was merged in. Storing the raw
    # instruction verbatim here, separate from that flowing description,
    # means it stays enforced even if the prose drifts — see
    # systems/llm/system.py's prompt assembly for where it is rendered.
    if raw_instruction:
        await add_personality_hard_rule(raw_instruction)

    await log_personality_change(new_desc, "creator override", kind="personality")
    logger.info(f"[PERSONALITY] Creator override: {new_desc}")
    return {"type": "response", "content": await get_phrase("personality_updated", new_desc=new_desc)}


async def change_from(session, user_id: str, text: str, msg: str):
    """The careful classifier on his sentence; a change if it says so,
    None if not."""
    exact = strip_override_code_mention(text, msg)
    result = await classify_personality_set(exact)
    if result.get("personality_command") != "set":
        return None
    raw_instruction = result.get("value")
    new_desc = raw_instruction
    if new_desc:
        new_desc = await merge_personality_change(await get_personality(), new_desc)
    return await _apply_change(session, user_id, text, msg, new_desc, raw_instruction)


async def on_persona_score(session, user_id: str, text: str, msg: str, score: int):
    """Called by systems/intent/system.py with the turn's persona score."""
    if await get_user_role(user_id) != "creator":
        return None                     # a normal user cannot adjust her; nothing to ask
    exact = strip_override_code_mention(text, msg)
    code_absent = await override_code_status(user_id, msg) == "absent"
    # an answer to her question is not a change (2026-09-23), nor is a
    # correction of one phrase (2026-09-21) — unless he says the code
    if code_absent and (session.get("awaiting_curiosity_answer") or corr.is_correction(exact)):
        return None
    if score >= PERSONA_SURE:
        logger.info(f"[PERSONALITY] persona {score}/10 — checking whether this is a change: {text[:80]!r}")
        return await change_from(session, user_id, text, msg)
    if score >= PERSONA_MAYBE:
        logger.info(f"[PERSONALITY] persona {score}/10 — asking whether he meant a change: {text[:80]!r}")
        session["persona_confirm"] = {"text": text, "msg": msg, "t": time.time()}
        return {"type": "response", "content": await get_phrase("persona_confirm")}
    return None
