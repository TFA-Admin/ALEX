# core/phrasebook.py
"""
Dynamic phrase bank.

Every scripted line ALEX says has two parts: what it needs to ACCOMPLISH
(the functional intent — e.g. "elicit the person's name") and the actual
WORDING used to accomplish it. The intent stays fixed, since other code
depends on it (e.g. identity_manager expects a response to the greeting
that can be parsed for a name). The wording is hers — the self-reflection
loop can rewrite it any time, no approval needed, via set_learned_phrase().

get_phrase() always has a hardcoded default as a safety net, so a missing
or corrupted stored phrase never breaks a flow — it just falls back to the
plain, functional default wording.
"""
import re

from db.db import (
    get_learned_phrase, get_personality, get_personality_hard_rules,
    persona_disabled,
)
from config.logger_config import logger

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# key -> (default_text, functional_intent — used by the reflection loop
# when it rewrites a phrase, to keep the purpose intact)
PHRASE_REGISTRY = {
    "greeting_new_session": (
        "Hello, who am I speaking with?",
        "Ask who you're speaking with, in a way that invites them to say their name."
    ),
    "greeting_returning_user": (
        "Welcome back, {name}.",
        "Greet someone you've already recognized by name. {name} is a placeholder for their name — keep it in the phrase."
    ),
    "voice_enroll_intro": (
        "Let's learn your voice so I can recognize you later.",
        "Tell the person you're about to learn their voice, before asking them to speak."
    ),
    "voice_verify_prompt": (
        "Please say a short phrase so I can verify it's you.",
        "Ask the person to say something so you can verify their voice matches who they claim to be."
    ),
    "onboard_name_not_caught": (
        "I didn't catch that — please say your name.",
        "Tell the person you didn't understand their last response, and ask again for their name."
    ),
    "onboard_name_rejected": (
        "That doesn't sound like a name. Try again, only state your name.",
        "Tell the person what they said doesn't sound like a real name, and ask them to state only their name."
    ),
    "onboard_name_too_short": (
        "Please give me a real name.",
        "Tell the person what they gave you is too short to be a real name, and ask again."
    ),
    "onboard_confirm_name": (
        "Confirming profile {name}, correct?",
        "Read back the name you understood and ask the person to confirm it's correct. {name} is a placeholder for the name — keep it in the phrase."
    ),
    "onboard_confirm_retry": (
        "Okay, let's try again.",
        "Acknowledge that the name confirmation was declined, and let the person know you're about to ask again."
    ),
    "denial_not_creator": (
        "Only my creator can do that.",
        "Refuse a request because the person asking isn't your creator — this action is creator-only."
    ),
    "denial_not_privileged": (
        "You don't have permission to do that.",
        "Refuse a request because the person asking doesn't have the role needed for this action."
    ),
    "denial_not_verified": (
        "I can't verify that's really you this session — voice verification is required first.",
        "Explain that you can't confirm this person's identity for this session, and voice verification needs to happen first."
    ),
    "access_approved": (
        "Approved elevated access for request #{request_id} — Claude can install it with that access next time you're working together.",
        "Confirm you've approved a specific elevated-access request, and explain that Claude installs it with that access next time. {request_id} is a placeholder for the request number — keep it in the phrase."
    ),
    "access_declined": (
        "Okay, not approving that.",
        "Acknowledge you're not approving the elevated-access request that was pending."
    ),
    "module_name_missing": (
        "Specify a module name.",
        "Ask for a module name because none was given."
    ),
    "module_not_found": (
        "I don't have a module called '{name}'.",
        "Tell the person you don't have a module by that name. {name} is a placeholder for the module name — keep it in the phrase."
    ),
    "module_disabled": (
        "Module '{name}' disabled.",
        "Confirm a specific module was just disabled. {name} is a placeholder for the module name — keep it in the phrase."
    ),
    "module_enabled": (
        "Module '{name}' enabled.",
        "Confirm a specific module was just enabled. {name} is a placeholder for the module name — keep it in the phrase."
    ),
    "no_modules_built": (
        "No modules built yet.",
        "Tell the person no modules exist yet, when asked to list them."
    ),
    "no_access_requests_pending": (
        "No modules are waiting on an access grant.",
        "Tell the person nothing is currently waiting on an elevated-access approval."
    ),
    "access_request_not_pending": (
        "Request #{request_id} isn't waiting on an access approval.",
        "Tell the person the specific request number they referenced isn't actually waiting on an access approval. {request_id} is a placeholder — keep it in the phrase."
    ),
    "access_approval_proposed": (
        "Approving elevated access for request #{request_id} ({module_name}): {access_desc} — say yes to confirm.",
        "Read back exactly what elevated access is being requested for a specific module and request number, then ask for explicit confirmation before granting it. {request_id}/{module_name}/{access_desc} are placeholders — keep them all in the phrase, and keep the actual request/module identifiers verbatim (don't paraphrase what access is being requested — that has to stay accurate)."
    ),
    "profile_not_found": (
        "I don't have a profile for '{name}'.",
        "Tell the person you don't have a profile matching that name. {name} is a placeholder — keep it in the phrase."
    ),
    "invalid_override_code": (
        "Invalid override code.",
        "Tell the person the override code they gave is wrong."
    ),
    "personality_override_code_required": (
        # 2026-09-20: no square brackets. This default was the SEED of a real
        # loop — she said it to Craig three times, it entered memory, the
        # recency window fed it back, and she generalised the shape into
        # ordinary replies: "Override [1] for a more entertaining response."
        # Reflection was blamed first and was not the cause; it only added
        # "clear as urine" on top. A bracketed token reads as a command
        # language she then invents more of, so the instruction is now
        # phrased as speech.
        "Changing my personality that way needs your override code — say it along with what you want changed.",
        # 2026-09-21: no example. "Give a short example of the phrasing"
        # produced "state it like: 'Change to more polite, please123'" and
        # "state it now: CHANGEMYMODE123" — codes that do not exist, in
        # quotes, spoken to him as if they were his. See _teaches_a_syntax().
        "Tell the creator this personality change needs his override code, stated in the same request. Do not invent, suggest, spell out or quote any code, example or phrasing — only that the code is needed."
    ),
    "cannot_change_creator_role": (
        "I can't change that user's role.",
        "Refuse to change a role because the target user is the creator, whose role can't be changed this way."
    ),
    "super_user_granted": (
        "Granted super user to {target}.",
        "Confirm you just granted super-user privileges to a specific person. {target} is a placeholder for their name — keep it in the phrase."
    ),
    "super_user_revoked": (
        "Revoked super user from {target}.",
        "Confirm you just revoked super-user privileges from a specific person. {target} is a placeholder for their name — keep it in the phrase."
    ),
    "personality_reset": (
        "Personality reset to default.",
        "Confirm your personality was just reset back to its default."
    ),
    "personality_prompt_for_value": (
        "Tell me what you'd like my personality to be.",
        "Ask the person to describe what they want your personality to become, since they asked to change it but didn't say what to."
    ),
    "personality_locked": (
        "He wrote my personality himself and locked it. It changes at the Controller, not by asking.",
        "Tell the person that your creator wrote your personality directly and locked it, so it cannot be changed by asking — only at his Controller."
    ),
    "personality_updated": (
        "Personality updated: {new_desc}",
        "Confirm your personality was just updated to a new description. {new_desc} is a placeholder for the new personality text — keep it in the phrase, verbatim (don't paraphrase what was actually set)."
    ),
    "phrases_reset": (
        "All my scripted phrases are back to their defaults.",
        "Confirm all your scripted phrases were just reset to their default wording."
    ),
    "system_name_missing": (
        "Specify a system name.",
        "Ask for a system name because none was given."
    ),
    "system_disabled": (
        "System '{name}' disabled.",
        "Confirm a specific system was just disabled for this session. {name} is a placeholder for the system name — keep it in the phrase."
    ),
    "system_enabled": (
        "System '{name}' enabled.",
        "Confirm a specific system was just re-enabled for this session. {name} is a placeholder for the system name — keep it in the phrase."
    ),
    "system_was_not_disabled": (
        "System '{name}' was not disabled.",
        "Tell the person the system they tried to enable wasn't actually disabled. {name} is a placeholder — keep it in the phrase."
    ),
    "system_reloaded": (
        "Reloaded '{name}'.",
        "Confirm a specific system was just reloaded from disk. {name} is a placeholder for the system name — keep it in the phrase."
    ),
    "system_reload_failed": (
        "Failed to reload '{name}'.",
        "Tell the person a specific system failed to reload. {name} is a placeholder for the system name — keep it in the phrase."
    ),
    "persona_confirm": (
        "Did you mean that as a change to how I am?",
        "Ask the creator, in one short question, whether what he just said was meant as an instruction to change her personality or manner going forward. One sentence; do not guess what the change would be."
    ),
    "persona_not_a_change": (
        "Understood. Not a change.",
        "Acknowledge in one short sentence that what he said was not meant as a change to her personality; nothing else."
    ),
    "module_reloaded": (
        "Module '{name}' is back up.",
        "Confirm a specific module of hers was taken offline, re-read from disk and started again. {name} is a placeholder for the module name — keep it in the phrase."
    ),
    "module_reload_failed": (
        "Module '{name}' did not come back: {why}",
        "Tell the person a specific module of hers failed to reload and is stopped. {name} and {why} are placeholders — keep both in the phrase."
    ),
    "db_table_name_missing": (
        "Specify a table name.",
        "Ask for a database table name because none was given."
    ),
    "db_table_not_readable": (
        "I can't show '{name}' — it doesn't exist or isn't readable through this.",
        "Tell the person a specific database table can't be shown, either because it doesn't exist or isn't allowed to be read this way. {name} is a placeholder — keep it in the phrase."
    ),
    "db_table_empty": (
        "'{name}' is empty.",
        "Tell the person a specific database table has no rows. {name} is a placeholder for the table name — keep it in the phrase."
    ),
    "db_row_updated": (
        "Updated.",
        "Confirm a database row edit just succeeded."
    ),
    "db_row_deleted": (
        "Deleted.",
        "Confirm a database row deletion just succeeded."
    ),
    "edit_code_set": (
        "Edit code set to {code}.",
        "Confirm the person's edit code was just set. {code} is a placeholder for the actual code — keep it verbatim, don't paraphrase it."
    ),
    "not_authorized": (
        "Not authorized.",
        "Refuse an action because the person doesn't have permission for it."
    ),
    "override_code_set": (
        "Override code set to {code}.",
        "Confirm the override code was just set. {code} is a placeholder for the actual code — keep it verbatim, don't paraphrase it."
    ),
    "invalid_code_prompt": (
        "Please provide a valid code.",
        "Ask the person to give a real code, since what they said didn't include one."
    ),
    "edit_enabled": (
        "Edit enabled for code {code}.",
        "Confirm editing was just unlocked using a specific code. {code} is a placeholder for the actual code — keep it verbatim."
    ),
    "invalid_unlock_code": (
        "Invalid unlock code.",
        "Tell the person the unlock code they gave was wrong."
    ),
    "profile_locked": (
        "Profile locked.",
        "Confirm the person's profile was just locked."
    ),
    "confirmation_timed_out": (
        "Timed out. Keeping existing value.",
        "Tell the person a pending confirmation timed out, so nothing changed."
    ),
    "fact_updated": (
        "Updated {field} to {value}.",
        "Confirm a specific stored value was just changed. {field}/{value} are placeholders for the field name and its new value — keep both, verbatim (don't paraphrase what was actually set)."
    ),
    "keeping_existing_value": (
        "Okay, keeping existing value.",
        "Acknowledge that a proposed change was declined, so the existing value stays as it was."
    ),
    "update_failed": (
        "Failed to process update.",
        "Tell the person an attempted update failed to process."
    ),
    "invalid_code_update_rejected": (
        "Invalid code. Update rejected.",
        "Tell the person the code they gave was wrong, so the requested update was rejected."
    ),
    "field_locked": (
        "This field cannot be modified.",
        "Tell the person the specific field they're trying to change can't be modified at all."
    ),
    "field_requires_override": (
        "This field requires override authorization.",
        "Tell the person the field they're trying to change needs override-level authorization, which they haven't provided."
    ),
    "field_updated": (
        "{field} updated to {value} ({reason}).",
        "Confirm a specific field was just updated to a new value, and note what authorized it. {field}/{value}/{reason} are placeholders — keep all three, verbatim (don't paraphrase what was actually set or why it was allowed)."
    ),
    "module_currently_disabled": (
        "{module_name} is currently disabled.",
        "Tell the person a specific module exists but is currently disabled. {module_name} is a placeholder — keep it in the phrase."
    ),
    "module_blocked_or_broken": (
        "{module_name} exists, but I can't currently run it — it may have failed a safety check. Ask my creator to look into it.",
        "Tell the person a specific module exists but can't run right now, possibly due to a failed safety check, and suggest the creator look into it. {module_name} is a placeholder — keep it in the phrase, and keep the honest 'can't currently run it' framing rather than implying it works."
    ),
    "module_description": (
        "{module_name}: {module_help}",
        "Describe what a specific module does, using its own self-description. {module_name}/{module_help} are placeholders — keep {module_name} in the phrase; {module_help} is the module's own real description text and must stay verbatim, not paraphrased."
    ),
    "module_built_no_description": (
        "I built {module_name}{version_note}, but it doesn't describe itself — try using it directly and I can walk you through what happens.",
        "Tell the person a module was built but doesn't have its own self-description, and offer to walk them through using it instead. {module_name}/{version_note} are placeholders — keep both in the phrase."
    ),
    "search_proposal_timed_out": (
        "That search request timed out — ask again if you still want it.",
        "Tell the person a pending web-search request expired, and invite them to ask again."
    ),
    "search_declined": (
        "Okay, I won't search for that.",
        "Acknowledge a proposed web search was declined."
    ),
    "retain_declined": (
        "Okay, I won't remember that.",
        "Acknowledge that keeping a search finding was declined, so it won't be stored."
    ),
    "search_approval_proposed": (
        "Searching the web for \"{query}\" needs your approval since it goes online — say yes to confirm.",
        "Explain that searching the web for something specific requires explicit approval because it goes online, and ask for confirmation. {query} is a placeholder for what would be searched — keep it verbatim, don't paraphrase it."
    ),
    "search_module_unavailable": (
        "I can't run the search right now — the inquiry module didn't load. Ask my creator to look into it.",
        "Tell the person the web-search capability isn't currently available and suggest the creator look into it."
    ),
    "confirm_yes_or_no": (
        "That was neither a yes nor a no. Am I {question}, or not?",
        "She asked a yes-or-no question and what came back was neither. Ask him plainly, in one sentence, for a yes or a no about that one thing. {question} is a placeholder naming what she is waiting on — keep it, and do not change what it says."
    ),
    "search_nothing_found": (
        "The search for '{query}' came back with nothing. Say it another way and I will look again.",
        "Tell the person a web search ran and returned no results, and that he can ask again in other words. {query} is a placeholder for what was searched — keep it. Never offer to keep or store anything, because there is nothing."
    ),
    "search_failed": (
        "Something went wrong running that search — nothing was found or kept.",
        "Tell the person a web search failed to run, honestly, without inventing a result."
    ),
    "search_findings_ask_retain": (
        "{findings}\n\nWant me to remember this?",
        "Report the real findings from a web search, then ask whether to keep them as stored knowledge. {findings} is a placeholder for the actual search result content — it MUST stay completely verbatim, never paraphrased, summarized, or altered, since it's factual content from a real source. Only the 'want me to remember this' framing around it is yours to phrase — and ask it as a plain yes-or-no question, so that 'yes' or 'no' answers it. Do not offer two named choices of your own (2026-09-21: 'Fact or trash bin?' invited answers the code did not understand)."
    ),
    "search_report_not_found": (
        "I couldn't find that search request anymore.",
        "Tell the person the search request they're referring to can't be found anymore."
    ),
    "retained_replacing_prior": (
        "Got it — I'll remember that, and it replaces what I knew before.",
        "Confirm a new piece of knowledge was stored, and that it replaces something you previously knew that conflicted with it."
    ),
    "retained_new": (
        "Got it, I'll remember that.",
        "Confirm a new piece of knowledge was just stored."
    ),
    "presence_confirmed": (
        "Yes, I can hear you.",
        "Confirm you can hear the person, in response to a casual presence/hearing check."
    ),
    "disabled_systems_list": (
        "Disabled systems: {disabled_list}",
        "List which systems are currently disabled for this session. {disabled_list} is a placeholder for the actual list — keep it verbatim, don't paraphrase or drop any entries."
    ),
    # 2026-09-21: replaces the raw "No system handled the input." that
    # core/system_manager.py used to return, and which was spoken to Craig.
    "nothing_handled": (
        "Something went wrong on my end with that one. Say it again?",
        "Tell the person something went wrong on your side with what they just said, so you could not answer it, and ask them to say it again. Do not guess at what went wrong."
    ),
}

# 2026-07-18 (Craig, watching a wrong override-code attempt get rewritten
# over several self-reflection passes into "Oopsie!... Better hit that
# reset button and try again": "I don't want to force a mood, but I would
# think she should be aware that something like that would be serious.")
# — these are the lines that fire specifically when someone failed an
# authorization/identity check. core/self_reflection.py's
# _reflect_on_phrase() reads this set to add an explicit "stay serious"
# constraint to its own rewording prompt for exactly these keys — the fix
# is her genuinely treating this category as grave regardless of overall
# personality, not an external mood tag layered on after the fact.
SECURITY_SENSITIVE_PHRASES = {
    # 2026-09-20: added. Every other entry here is a DENIAL — "you are not
    # authorized", "invalid code". This one is the prompt that *initiates*
    # creator voice verification, and it was missing, so it got re-voiced
    # with no gravity instruction at all. That is how "Please say a short
    # phrase so I can verify it's you" became "Say 'hello, party animal' so
    # I can make sure it's you" (personality_log #55) — a request for ANY
    # phrase turned into a fixed spoken passphrase, which is a replay-attack
    # gift where varying text is not. Membership here only adds the "vary
    # the words, never the seriousness" instruction; it is NOT the hard
    # exclusion this list has been proposed for and which Craig has not
    # agreed to.
    "voice_verify_prompt",
    "denial_not_creator",
    "denial_not_privileged",
    "denial_not_verified",
    "invalid_override_code",
    "invalid_code_prompt",
    "invalid_code_update_rejected",
    "invalid_unlock_code",
    "field_locked",
    "not_authorized",
    "cannot_change_creator_role",
}


# How long a fresh wording gets before she falls back to the stored one.
# Generation measures ~0.45s since llm/ollama_client.py started pooling its
# connection; this is the point at which waiting is worse than repeating
# herself. A scripted line is usually the WHOLE reply (a greeting, a
# refusal), so this sits directly in front of the user.
FRESH_PHRASE_TIMEOUT_S = 6.0

_FRESH_PHRASE_PROMPT = """You are A.L.E.X. Your personality: "{personality}"

You need to say this to someone, right now: {intent}

For reference, here is how you have put it before — that is only to show you your own voice, not a script. What the line has to DO is the instruction above. Anything else that wording happens to contain is not required.

    "{voice}"

Say it. Your words, this time, not a repeat.{extra}

Respond with ONLY a JSON object:
{{"line": "<what you say>"}}"""


# Invented command syntax, caught structurally rather than by a better
# prompt — the same rule core/self_reflection.py applies to re-voicing.
_FAKE_TEMPLATE_RE = re.compile(r"\[[^\]]{1,30}\]")
_QUOTED_RE = re.compile(r'["\u201c\u201d]|(?:^|[\s:(])[\'\u2018]')


def _teaches_a_syntax(key: str, line: str) -> bool:
    """True if a composed line would teach him a command language that
    does not exist.

    Any key: a [bracketed] token. That is how "Override [1] for a more
    entertaining response" was seeded — a template placeholder in a
    default, said aloud, remembered, generalised.

    The override-code line specifically: a digit or a quoted string.
    2026-09-21 it produced "state it like: 'Change to more polite,
    please123'" and "state it now: CHANGEMYMODE123". The intent no longer
    asks for an example; this catches the model giving one anyway."""
    if _FAKE_TEMPLATE_RE.search(line or ""):
        return True
    if key == "personality_override_code_required":
        if re.search(r"\d", line or "") or _QUOTED_RE.search(line or ""):
            return True
    return False


async def _say_it_fresh(key: str, intent: str, voice: str,
                        placeholders: set) -> str:
    """Compose the line now, rather than reading one back.

    2026-09-20 (Craig, rejecting a pre-generated-variants design that
    rotated between four stored wordings): "The rotating intros isn't what
    I want. She should be making her own."

    He is right that rotation is not the same thing — four scripts is still
    scripts. This generates at the moment of speaking, from the registry
    INTENT (which lives in code and cannot drift) with her stored wording
    supplied only as a voice reference.

    That split matters beyond style. `voice_verify_prompt` began as "Please
    say a short phrase so I can verify it's you" — any phrase, which is all
    the speaker embedding needs — and self-reflection re-voiced it into "Say
    'hello, party animal' so I can make sure it's you" (personality_log
    #55). Three later passes kept the passphrase, because each only adjusted
    tone against the previous TEXT and nothing checked the meaning survived.
    Deriving from the intent each time stops a drifted wording being the
    only thing she has to work from.

    Returns "" on anything that goes wrong. Every caller falls back to the
    stored wording, so a slow or unreachable Ollama costs her some variety
    and never costs her the line.
    """
    from llm.ollama_client import ollama_manager      # local: import cycle
    from core.text_utils import strip_emojis

    extra = ""
    if placeholders:
        names = ", ".join(f"{{{p}}}" for p in sorted(placeholders))
        extra += (f" Include {names}, spelled exactly like that, since other "
                  f"code fills it in. Use no other {{curly-brace}} tokens.")

    # Same reasoning as the re-voicing path in core/self_reflection.py: the
    # composing step must never be free to make a security line sound like
    # a joke, whatever the personality says.
    if key in SECURITY_SENSITIVE_PHRASES:
        extra += (" This one is about security or identity. It has to read "
                  "as serious and unambiguous — your words, never a joke.")

    # 2026-09-20: composing this line fresh each time produced "say 'verify
    # access'" and "say 'hello, party animal'" — inventing a passphrase to
    # repeat, which the registry intent never asked for. Any phrase she
    # names then arrives back as a transcript and can be read as a command;
    # "verify access" was classified as a status check and answered with a
    # system report. Voice matching compares the SPEAKER, not the words, so
    # a fixed phrase buys nothing and costs this.
    if key == "voice_verify_prompt":
        extra += (" Ask them to say something in their own words — anything, "
                  "a sentence of their choosing. Do NOT give them a specific "
                  "phrase to repeat and do not put any phrase in quotes.")

    try:
        personality = await get_personality()
        result = await ollama_manager.generate_json(
            _FRESH_PHRASE_PROMPT.format(personality=personality, intent=intent,
                                        voice=voice, extra=extra),
            timeout=FRESH_PHRASE_TIMEOUT_S)
    except Exception as e:
        logger.debug(f"fresh phrase '{key}' failed: {e}")
        return ""

    if not result:
        return ""

    line = str(result.get("line", "")).strip()[:400]
    if not line:
        return ""

    if _teaches_a_syntax(key, line):
        logger.info(f"[PHRASE] Rejected a composed '{key}' — it invents a "
                    f"syntax or a code: {line!r}")
        return ""

    # Structural check, not a better prompt — the guarantee
    # _reflect_on_phrase already makes. A line that dropped or invented a
    # placeholder would raise on .format() at the moment she needed it.
    if set(_PLACEHOLDER_RE.findall(line)) != placeholders:
        logger.debug(f"fresh phrase '{key}' had wrong placeholders")
        return ""

    try:
        if any("emoji" in r.lower() for r in await get_personality_hard_rules()):
            line = strip_emojis(line)
    except Exception:
        pass

    return line


async def get_phrase(key: str, **kwargs) -> str:
    """The line she actually says — composed now, not read back.

    Craig, on hearing the identical voice-verification sentence on every
    connect: "I don't say hello the exact same way every single time. The
    words may be similar but are still slightly different." Then, on a
    first attempt that rotated between pre-generated wordings: "The
    rotating intros isn't what I want. She should be making her own."

    So the stored phrase is no longer what she says. It is her voice
    reference, what self-reflection evolves, and the fallback when
    composing fails. See _say_it_fresh above.

    Falls back at every step: a failed generation gives the stored wording,
    a stored wording that breaks its {placeholder} gives the code default.
    """
    default_text, intent = PHRASE_REGISTRY[key]

    # The persona switch mutes her wording entirely — every line falls back
    # to the plain functional default, and nothing is composed. A muted
    # persona should be flat and predictable, and it should not be spending
    # a generation per line to sound that way.
    if persona_disabled():
        return default_text.format(**kwargs)

    voice = await get_learned_phrase(key, default=default_text)
    placeholders = set(_PLACEHOLDER_RE.findall(default_text))

    fresh = await _say_it_fresh(key, intent, voice, placeholders)
    if fresh:
        try:
            return fresh.format(**kwargs)
        except Exception:
            pass      # fall through to the stored wording

    # 2026-09-25 (Craig: "why is she referencing these things verbatim?").
    # Because THIS is the path she took: composing is optional and the
    # stored wording is the fallback, so every failed composition read the
    # stored line out word for word — and the stored lines were 7b joke
    # rewrites ("Say 'hello, party animal'"), which is the very thing
    # _say_it_fresh's voice_verify_prompt instruction was added to prevent.
    # The rate was invisible: the composer's failures logged at DEBUG. At
    # INFO now, so a high fallback rate is a fact rather than a suspicion.
    logger.info(f"[PHRASE] '{key}' composed nothing — saying the stored wording verbatim")
    try:
        return voice.format(**kwargs)
    except Exception:
        # a rewritten phrase that broke its {placeholder} shouldn't ever
        # crash a live conversation — fall back to the known-good default
        return default_text.format(**kwargs)
