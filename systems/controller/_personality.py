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
import re

from core.intent_classifier import classify_personality_set, merge_personality_change

# 2026-09-25 (Craig: "interactions have gone from about 1 second to about
# 5. Can we streamline anything?"). Measured over the afternoon's turns:
# classify_personality_set() — a model call — ran on EVERY creator message
# that was not a fixed phrase, a correction or an answer she was waiting
# for, at 0.85 s a turn, and answered "no" every time. A request to change
# how she is carries words for it. Only a message with one of these is
# worth the call; anything else is conversation and goes straight on.
_PERSONALITY_CUE_RE = re.compile(
    r"\b(?:personality|attitude|demeanou?r|tone|manners?|mannerisms?|persona|character|style of (?:talking|speaking)|"
    r"be (?:more|less|a (?:bit|little)|nicer|meaner|kinder|ruder|polite|politer|blunt|blunter|formal|casual|serious|funnier|funny|"
    r"sarcastic|snark\w*|warm\w*|cold\w*|upbeat|calm\w*|quiet\w*|brief|briefer|concise|verbose|direct|gentle\w*|harsh\w*|"
    r"sweet\w*|softer|louder|angrier|happier|cheer\w*|yourself)|"
    r"(?:act|talk|speak|sound|behave|respond|answer|reply|treat me) (?:like|more|less|as if|as though|in a|with)|"
    r"from now on|going forward|stop (?:being|saying|telling|offering|calling|asking|repeating|doing|acting|talking|apologi[sz]ing)|"
    r"(?:don'?t|do not|quit|no more) (?:be|being|keep|say|saying|tell|telling|call|calling|offer|offering|repeat|repeating)|"
    r"you (?:don'?t|do not) (?:need|have) to (?:keep|say|tell|offer|remind)|"
    r"(?:more|less) (?:sarcastic|sarcasm|snarky|polite|formal|casual|serious|verbose|talkative|warm|cold|hostile|aggressive|"
    r"friendly|helpful|blunt|direct|chatty|repetitive|patient|respectful|dramatic|emotional|robotic|human|playful|menacing|dark)|"
    r"(?:i want|i'?d like|i would like|i need) you to (?:be|act|talk|speak|sound|stop|start|become)|"
    r"you should (?:be|act|talk|speak|sound|stop|start|become)|"
    r"(?:set|change|adjust|update|make|switch|turn) (?:up |down )?your (?:personality|tone|attitude|voice|style|manner|sarcasm|warmth|humou?r)|"
    r"new personality|(?:a )?different personality|(?:more|less) like (?:a|an|the)\b|like a (?:pirate|butler|robot|human|friend|soldier|professor|teenager)|"
    r"call me (?:sir|master|boss|captain|lord|my)|"
    r"you'?re (?:too|being too|way too) \w+)\b", re.I)


def _might_be_personality_set(text: str) -> bool:
    return bool(_PERSONALITY_CUE_RE.search(text or ""))
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

    if new_desc is None and not _might_be_personality_set(exact_phrase_input):
        return None          # conversation; no classifier call (see _PERSONALITY_CUE_RE)

    if new_desc is None and await get_user_role(user_id) == "creator":
        result = await classify_personality_set(exact_phrase_input)
        if result.get("personality_command") == "set":
            raw_instruction = result.get("value")
            new_desc = raw_instruction
            if new_desc:
                new_desc = await merge_personality_change(await get_personality(), new_desc)

    if new_desc is not None:

        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        if not new_desc:
            return {
                "type": "response",
                "content": await get_phrase("personality_prompt_for_value")
            }

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
        # instruction ("without using emojis") got silently dropped the
        # very next time a different instruction was merged in. Storing
        # the raw instruction verbatim here, separate from that flowing
        # description, means it stays enforced even if the prose drifts —
        # see systems/llm/system.py's prompt assembly for where this
        # actually gets rendered.
        if raw_instruction:
            await add_personality_hard_rule(raw_instruction)

        await log_personality_change(new_desc, "creator override", kind="personality")
        logger.info(f"[PERSONALITY] Creator override: {new_desc}")

        return {
            "type": "response",
            "content": await get_phrase("personality_updated", new_desc=new_desc)
        }

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
