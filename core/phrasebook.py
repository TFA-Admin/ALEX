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
from db.db import get_learned_phrase

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
        "Changing my personality that way now requires your override code — say something like 'override code [code] reset your personality'.",
        "Tell the creator this specific personality action (set/reset personality, or reset phrases) now requires stating the override code in the same request, and give a short example of the phrasing."
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
    "search_failed": (
        "Something went wrong running that search — nothing was found or kept.",
        "Tell the person a web search failed to run, honestly, without inventing a result."
    ),
    "search_findings_ask_retain": (
        "{findings}\n\nWant me to remember this?",
        "Report the real findings from a web search, then ask whether to keep them as stored knowledge. {findings} is a placeholder for the actual search result content — it MUST stay completely verbatim, never paraphrased, summarized, or altered, since it's factual content from a real source. Only the 'want me to remember this' framing around it is yours to phrase."
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
}


async def get_phrase(key: str, **kwargs) -> str:
    default_text, _ = PHRASE_REGISTRY[key]
    text = await get_learned_phrase(key, default=default_text)

    try:
        return text.format(**kwargs)
    except Exception:
        # a rewritten phrase that broke its {placeholder} shouldn't ever
        # crash a live conversation — fall back to the known-good default
        return default_text.format(**kwargs)
