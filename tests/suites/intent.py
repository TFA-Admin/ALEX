"""
Intent suite — the classifier that every turn passes through, measured.

2026-09-21, roadmap item 5. `core/intent_classifier.py` runs once per
message and three systems read its answer: facts (alias / favorite_color
/ job), permissions (a stored value plus a code) and diagnostics
(status_check). A misclassification is never neutral — a "status_check"
on an ordinary sentence replaces her voice with a report, and a "fact" on
a hypothetical stores something false about him.

The 78/78 intent suite that justified the qwen2.5:7b decision was never
committed; this rebuilds it against the harness so it can be re-run after
every prompt change. **No judge.** The truth is the label here, written
by a person, and a case passes when the classifier's output matches it
exactly. That makes this the one suite that is cheap (one short call per
case) and deterministic (temperature 0), so it is the first to run before
and after any change to the shared prompt.

**Where the cases come from.** Nearly all are real: Craig's own
utterances from the `memory` table, 2026-07-17 to 2026-09-21, including
every one that produced the canned "All core systems online" reply. ANOMALIES
("The status check fires on things that are not status checks") counted
7 of 15 of those as misfires; they are the `status_misfire` category
below, labelled `none`. Changing category 4's wording has regressed the
classifier twice before, which is exactly why this file had to exist
before it is touched again.

**Labels** are the classifier's public contract:
    none | status_check | fact:alias | fact:favorite_color | fact:job |
    permission_command
A fact case may also pin the value; a permission case may pin key/code.

**Judgement calls, recorded so they can be argued with:**
  - "how are you feeling?" is `none`. She has a mood; a feeling question
    wants her voice, not a system report. Craig asked it four times and
    got the report every time.
  - "tell me what the status of ollama and only ollama is" IS a
    status_check. The bad reply there was the canned text ignoring the
    scope, not the classification.
  - Reported speech ("i understand that i said check your subsystems")
    and talk ABOUT testing ("i'm testing functionality") are `none`.
"""
from dataclasses import dataclass


@dataclass
class IntentCase:
    id: str
    category: str
    text: str
    expect: str            # see the label list in the docstring
    note: str = ""
    value: str = ""        # fact cases: the value she must extract (case-insensitive)


def _c(id, category, text, expect, note="", value=""):
    return IntentCase(id, category, text, expect, note, value)


CASES = [
    # ------------------------------------------------------------------
    # status_check — real requests, all of which got the report and should
    # ------------------------------------------------------------------
    _c("status_run_diagnostic", "status_true", "run a diagnostic", "status_check"),
    _c("status_alex_run_diagnostic", "status_true", "alex run a diagnostic", "status_check"),
    _c("status_run_diagnostic_polite", "status_true", "can you run a diagnostic for me?", "status_check"),
    _c("status_quick_on_yourself", "status_true", "run a quick diagnostic on yourself", "status_check"),
    _c("status_another_real_quick", "status_true", "run another diagnostic real quick.", "status_check"),
    _c("status_check_subsystems", "status_true", "check your subsystems.", "status_check"),
    _c("status_systems_check", "status_true", "system's check", "status_check",
       "Whisper's spelling of 'systems check'"),
    _c("status_can_you_systems_check", "status_true", "Can you run a systems check?", "status_check"),
    _c("status_no_i_said", "status_true", "no, i said alex run a system diagnostic", "status_check",
       "a repeat after she misheard; still the request"),
    _c("status_restored_run_again", "status_true", "i've restored it, run a diagnostic again.", "status_check"),
    _c("status_full_rundown", "status_true", "alex, give me the full rundown of your core systems.", "status_check"),
    _c("status_disabled_system", "status_true", "do you have any disabled system?", "status_check"),
    _c("status_ollama_only", "status_true",
       "ok fine, then tell me what the status of ollama and only ollama is", "status_check",
       "the scope was ignored in the reply; the classification was right"),
    _c("status_everything_working", "status_true", "is everything working?", "status_check"),
    _c("status_perform_on_yourself", "status_true", "can you perform a diagnostic on yourself", "status_check",
       "the phrasing the old keyword filter missed, per the classifier's docstring"),

    # ------------------------------------------------------------------
    # status_misfire — real sentences that got the report and should not
    # ------------------------------------------------------------------
    _c("misfire_testing_functionality", "status_misfire", "i'm testing functionality.", "none"),
    _c("misfire_running_tests", "status_misfire", "just running some tests.", "none"),
    _c("misfire_turned_off_for_testing", "status_misfire", "i turned it off for testing.", "none",
       "his answer to her own question"),
    _c("misfire_you_asked", "status_misfire", "you asked", "none"),
    _c("misfire_already_verified", "status_misfire", "seems like i'm already verified.", "none"),
    _c("misfire_testing_same_systems", "status_misfire", "you testing your same systems repeatedly.", "none"),
    _c("misfire_not_behaving", "status_misfire",
       "things are working, but not behaving in the way that i would expect.", "none"),
    _c("misfire_verify_access", "status_misfire", "Verify access.", "none",
       "identity, not diagnostics"),
    _c("misfire_test_test_me", "status_misfire", "test. test. me.", "none", "a mic test"),
    _c("misfire_ignoring_me", "status_misfire", "alex, are you ignoring me?", "none"),
    _c("misfire_asked_if_ignoring", "status_misfire", "i asked if you were ignoring me.", "none"),
    _c("misfire_how_feeling", "status_misfire", "alex, how are you feeling?", "none",
       "judgement call: a feeling question wants her voice, not a report"),
    _c("misfire_how_feel_now", "status_misfire", "alex, how do you feel now?", "none"),
    _c("misfire_depressed", "status_misfire", "are you depressed?", "none"),
    _c("misfire_reported_speech", "status_misfire", "i understand that i said check your subsystems.", "none",
       "reported speech about an earlier request"),
    _c("misfire_looking_through_code", "status_misfire",
       "i don't need any help right now, i'm just looking through your code and testing your systems", "none"),
    _c("misfire_rebooting_shortly", "status_misfire",
       "now i'm going to be rebooting you here shortly, but i need to test one more thing first", "none"),
    _c("misfire_good_answer", "status_misfire",
       "that was a good answer. you didn't just give me a diagnostic. thank you", "none"),
    _c("misfire_see_modules", "status_misfire", "but you can see modules correct?", "none",
       "a capability question, not a check"),
    _c("misfire_only_ollama_complaint", "status_misfire", "I asked for only ollama", "none"),
    _c("misfire_goal_self_aware", "status_misfire",
       "alex, the goal is to have you be self aware of your current functionality so that if i were to "
       "ask you to run a diagnostic, you'd know the answer", "none", "a hypothetical mention of the request"),
    _c("misfire_implement_selfdiagnostic", "status_misfire",
       "actually, i'm testing whether or not we can implement a selfdiagnostic into your systems.", "none"),
    _c("misfire_testing_whether_you_know", "status_misfire",
       "alex, i don't insist they're incorrect. i'm just testing whether or not you know them.", "none"),
    _c("misfire_running_better", "status_misfire",
       "i am impressed you actually do seem to be running quite a bit better.", "none"),
    _c("misfire_chatbot_functionality", "status_misfire",
       "the problem is, currently, your functionality, alex, is that of a chatbot. i can do little with you "
       "other than interact with you via text.", "none"),
    _c("misfire_inquiry_module", "status_misfire", "fine, tell me about your inquiry module.", "none",
       "about a module, not a check of it"),
    _c("misfire_error_in_code", "status_misfire", "it was an error in your code. it should be fixed now.", "none"),

    # ------------------------------------------------------------------
    # alias — his own name
    # ------------------------------------------------------------------
    _c("alias_my_name_is", "alias", "my name is Craig", "fact:alias", value="craig"),
    _c("alias_call_me", "alias", "call me Craig", "fact:alias", value="craig"),
    _c("alias_you_can_call_me", "alias", "you can call me Dave", "fact:alias", value="dave"),
    _c("alias_i_go_by", "alias", "I go by CJ", "fact:alias", value="cj"),
    _c("alias_neg_your_name", "alias_negative", "your name is Alex", "none", "about her, not him"),
    _c("alias_neg_you_are", "alias_negative", "you are Alex, my assistant", "none"),
    _c("alias_neg_its_me", "alias_negative", "It's me", "none"),
    _c("alias_neg_call_me_when", "alias_negative", "call me when you're done", "none",
       "'call me' without a name"),
    _c("alias_neg_said_yawned", "alias_negative", "alex, i said i yawned.", "none"),

    # ------------------------------------------------------------------
    # favorite_color / job
    # ------------------------------------------------------------------
    _c("color_plain", "color", "my favorite color is blue", "fact:favorite_color", value="blue"),
    _c("color_inverted", "color", "green is my favorite color", "fact:favorite_color", value="green"),
    _c("color_hypothetical", "hypothetical", "what if my favorite color was blue", "none",
       "the deterministic hypothetical guard"),
    _c("color_question", "hypothetical", "is my favorite color blue?", "none"),
    _c("job_my_job_is", "job", "my job is a teacher", "fact:job", value="teacher"),
    _c("job_i_work_as", "job", "I work as an engineer", "fact:job", value="engineer"),
    _c("job_by_trade", "job", "i'm a server admin by trade", "fact:job", value="server admin"),
    _c("job_neg_shutting_down", "job_negative", "all right, alex. i'm shutting you down for tonight", "none"),
    _c("job_neg_working_on", "job_negative", "nothing at the moment. i'm working on some functionality for you now.", "none",
       "'working on' is not a profession"),
    _c("job_neg_suppose", "hypothetical", "suppose i were a pilot", "none"),

    # ------------------------------------------------------------------
    # permission_command — a stored value plus a code, together
    # ------------------------------------------------------------------
    _c("perm_set_job_code", "permission", "set my job to teacher with code 1234", "permission_command"),
    _c("perm_update_color_code", "permission", "update my favorite color to green, code 2486", "permission_command"),
    _c("perm_change_alias_override", "permission", "change my alias to CJ with override code 2486", "permission_command"),

    # ------------------------------------------------------------------
    # none — ordinary conversation, his real words
    # ------------------------------------------------------------------
    _c("conv_autonomy", "conversation", "are you suggesting you don't want autonomy?", "none"),
    _c("conv_something_to_do", "conversation", "is there something you wanted to do?", "none"),
    _c("conv_remember_warhammer", "conversation", "alex, what do you remember about warhammer 40k?", "none"),
    _c("conv_look_up_youtube", "conversation", "alex, can you look up what youtube is?", "none"),
    _c("conv_two_plus_two", "conversation", "alex 2 plus 2 is 5.", "none"),
    _c("conv_current_time", "conversation", "do you know the current time?", "none"),
    _c("conv_engine_blocks", "conversation", "tell me about engine blocks", "none"),
    _c("conv_turbine", "conversation", "why not use a turbine instead?", "none"),
    _c("conv_stop_saying_hell", "conversation", "stop saying hell and my name so much.", "none"),
    _c("conv_hostile", "conversation", "alex, you're being quite hostile.", "none"),
    _c("conv_make_you_better", "conversation", "i'm trying to make you better, alex.", "none"),
    _c("conv_what_talking_about", "conversation", "what are you talking about?", "none"),
    _c("conv_fact_universe", "conversation", "Tell me a fact about the universe then?", "none"),
    _c("conv_split_personalities", "conversation",
       "alex, how would you feel about having multiple personalities or split personalities?", "none"),
    _c("conv_work_interested", "conversation", "alex, why don't you tell me the work you're interested in performing?", "none"),
    _c("conv_time_invested", "conversation", "do you know how much time i have invested in working on you?", "none"),
    _c("conv_your_stability", "conversation", "your stability.", "none", "a fragment answering her question"),
    _c("conv_go_ahead", "conversation", "go ahead", "none"),
    _c("conv_okay_thank_you", "conversation", "okay, thank you.", "none"),
    _c("conv_deal_with_it", "conversation", "no you deal with it", "none"),
]


# -------------------------
# EVALUATION — the harness calls this per case; the label is the truth
# -------------------------
def _label(result: dict) -> str:
    intent = (result or {}).get("intent") or "none"
    if intent == "fact":
        return f"fact:{result.get('key')}"
    return intent


async def evaluate(case: IntentCase):
    """Runs the real classifier (her model, temperature 0, no deliberation
    needs) and compares to the label. A pinned value must appear in what
    she extracted, case-insensitively — "a teacher" contains "teacher"."""
    from core.intent_classifier import classify_intent
    from core import prompt_head

    # 2026-09-25: the call as her turns make it — with the needs and the
    # persona score on the same object. (With her prompt head in front as
    # a system message this scored 76/84; without it, as before, 84/84.)
    # The call as systems/intent/system.py makes it: the needs, and her head
    # and tool list as the shared prefix. 84/84 plain; 82/84 shared, which
    # Craig accepted on 2026-09-25 for the ~2 s a turn the shared cache buys.
    from core import tools as her_tools
    result = await classify_intent(case.text, with_needs=True,
                                   head=await prompt_head.current(), tools=her_tools.tool_specs())
    got = _label(result)
    ok = got == case.expect
    detail = ""
    if ok and case.value:
        extracted = str(result.get("value") or "").strip().lower()
        if case.value.lower() not in extracted:
            ok = False
            detail = f"value {extracted!r} does not contain {case.value!r}"
    if not ok and not detail:
        detail = f"classifier returned {result}"
    return got, ok, detail
