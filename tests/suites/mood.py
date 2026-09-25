"""
Mood suite — deterministic, instant, no model and no database.

2026-09-23 (roadmap item 10). Checks the mood state and the trait
offsets it feeds (core/mood.py, core/traits.py) against the design Craig
agreed to: three axes with their own half-lives; a stranger's correction
weighs more than his; no pleasure from being agreed with; satisfaction
from things she did; the orb calm when every axis is low; the dials
moving with the level; the prompt line naming the reason; the old 0-10
dial rows converting to offsets; verbosity tightening past where a slider
stopped.

Run:  python -X utf8 -m tests.harness mood
"""
from dataclasses import dataclass

from core import mood, traits

T0 = 1_000_000.0
MIN = 60.0


@dataclass
class MoodCase:
    id: str
    category: str
    expect: str
    note: str = ""


def _c(id, category, expect, note=""):
    return MoodCase(id, category, expect, note)


CASES = [
    _c("stranger_annoys_more", "who", "stranger > craig"),
    _c("irritation_half_life", "decay", "~half after 30 min"),
    _c("engagement_fades_faster", "decay", "engagement < irritation after 10 min"),
    _c("strain_clears", "decay", "strain < 1 after clean checks"),
    _c("no_pleasure_from_agreement", "rule", "no such event"),
    _c("calm_when_low", "orb", "calm"),
    _c("orb_shows_dominant", "orb", "edge, level > 0.4"),
    _c("cap_at_ten", "state", "10.0"),
    _c("merge_lifts", "state", "irritation down, engagement up"),
    _c("dials_follow_irritation", "dials", "shorter, sharper, less patient"),
    _c("line_names_reason", "prompt", "corrected, twice"),
    _c("line_empty_when_calm", "prompt", ""),
    _c("old_rows_convert", "traits", "verbosity -2 -> 60 words; sarcasm +3; patience -4"),
    _c("cap_past_the_slider", "traits", "-7 -> 10 words; +6 -> none"),
    _c("absolute_overrides", "traits", "overrides"),
    _c("effective_adds_mood", "traits", "standing + mood"),
    _c("barge_in_counts_once", "who", "5 barge-ins in 2 min = one"),
    _c("mood_never_silences", "dials", "verbosity offset >= -1"),
    _c("tone_is_not_an_input", "rule", "no sharp_reply"),
    _c("thanks_lifts_agreement_does_not", "rule", "thanked moves her; 'you're right' does not"),
    _c("values_short_answers", "values", "he values short answers"),
    _c("values_need_lopsided", "values", "nothing from two signals or a 50/50 split"),
    _c("values_never_agreement", "values", "no agreement feature"),
    _c("pet_needs_fall_and_health_follows", "pet", "starved -> health falls; fed -> health climbs"),
    _c("pet_care_takes_the_lowest", "pet", "lowest need first; play tires it"),
]


def _seq(events, start=T0, gap=MIN):
    s = mood.fresh(start)
    t = start
    for ev in events:
        if isinstance(ev, tuple):
            name, who, creator = ev
        else:
            name, who, creator = ev, "craig", True
        s = mood.apply(s, name, who=who, creator=creator, now=t)
        t += gap
    return s, t


async def evaluate(case: MoodCase):
    cid = case.id

    if cid == "stranger_annoys_more":
        a, _ = _seq([("corrected", "craig", True)])
        b, _ = _seq([("corrected", "sam", False)])
        ia, ib = a["axes"]["irritation"], b["axes"]["irritation"]
        got = f"craig {ia:.2f} stranger {ib:.2f}"
        return got, ib > ia * 1.5, ""

    if cid == "irritation_half_life":
        s, t = _seq([("corrected", "craig", True)])
        v0 = s["axes"]["irritation"]
        v1 = mood.decayed(s, t - MIN + 30 * MIN)["irritation"]
        got = f"{v0:.2f} -> {v1:.2f}"
        return got, abs(v1 - v0 / 2) < 0.05, ""

    if cid == "engagement_fades_faster":
        s, t = _seq([("corrected", "craig", True), ("substantive_turn", "craig", True)], gap=0)
        d = mood.decayed(s, t + 10 * MIN)
        frac_i = d["irritation"] / s["axes"]["irritation"]
        frac_e = d["engagement"] / s["axes"]["engagement"]
        got = f"irritation keeps {frac_i:.2f}, engagement keeps {frac_e:.2f}"
        return got, frac_e < frac_i, ""

    if cid == "strain_clears":
        s, t = _seq(["startup_failed", "healthy", "healthy", "healthy"], gap=2 * MIN)
        v = mood.decayed(s, t)["strain"]
        return f"strain {v:.2f}", v < 1.0, ""

    if cid == "no_pleasure_from_agreement":
        names = " ".join(mood.EVENTS)
        bad = [n for n in mood.EVENTS if any(w in n for w in ("agree", "praise", "complim", "pleas"))]
        return names, not bad, f"found {bad}" if bad else ""

    if cid == "calm_when_low":
        s, t = _seq([("lookup_found", "craig", True)])
        p = __import__("json").loads(mood.payload(s, t))
        return p["key"], p["key"] == "calm" and p["label"] == "calm", str(p)

    if cid == "orb_shows_dominant":
        s, t = _seq([("corrected", "sam", False), ("corrected", "sam", False), ("lookup_found", "craig", True)])
        p = __import__("json").loads(mood.payload(s, t))
        return f"{p['key']} {p['level']}", p["key"] == "edge" and p["level"] > 0.4, str(p)

    if cid == "cap_at_ten":
        s, _ = _seq([("corrected", "sam", False)] * 20, gap=1.0)
        v = s["axes"]["irritation"]
        return f"{v:.1f}", v == 10.0, ""

    if cid == "merge_lifts":
        s, t = _seq([("corrected", "sam", False), ("corrected", "sam", False)], gap=1.0)
        before = s["axes"]["irritation"]
        s2 = mood.apply(s, "proposal_merged", who="craig", creator=True, now=t)
        got = f"irritation {before:.2f} -> {s2['axes']['irritation']:.2f}, engagement {s2['axes']['engagement']:.2f}"
        return got, s2["axes"]["irritation"] < before and s2["axes"]["engagement"] >= 2.0, ""

    if cid == "dials_follow_irritation":
        s, t = _seq([("corrected", "sam", False)] * 3, gap=1.0)   # ~7.9 irritation
        d = mood.dial_offsets(s, t)
        got = str(d)
        # verbosity is clamped to one notch (mood_never_silences); the rest grow with the level
        ok = d.get("verbosity", 0) <= -1 and d.get("sarcasm", 0) >= 2 and d.get("patience_with_others", 0) <= -3
        return got, ok, ""

    if cid == "line_names_reason":
        s, t = _seq([("corrected", "craig", True), ("corrected", "craig", True)])
        ln = mood.line(s, t)
        return ln, ("corrected you, twice" in ln and ln.startswith("YOUR MOOD RIGHT NOW: irritated")), ""

    if cid == "line_empty_when_calm":
        s, t = _seq([("lookup_found", "craig", True)])
        ln = mood.line(s, t + 60 * MIN)
        return repr(ln), ln == "", ""

    if cid == "old_rows_convert":
        old = '{"warmth": 1, "sarcasm": 8, "menace": 7, "dark_humor": 8, "verbosity": 3, "deference_to_craig": 8, "patience_with_others": 1}'
        o = traits.loads(old)
        got = f"verbosity {o['verbosity']} cap {traits.word_cap(o)} sarcasm {o['sarcasm']} patience {o['patience_with_others']}"
        # the old scale's middle is zero: 8/10 sarcasm is +3 (strong), 1/10 patience is -4 (extreme)
        ok = o["verbosity"] == -2 and traits.word_cap(o) == 60 and o["sarcasm"] == 3 and o["patience_with_others"] == -4
        back = traits.loads(traits.dumps(o))
        return got, ok and back == o, ""

    if cid == "cap_past_the_slider":
        tight = traits.word_cap({"verbosity": -7})
        loose = traits.word_cap({"verbosity": 6})
        return f"-7 -> {tight}; +6 -> {loose}", tight == 10 and loose is None and traits.num_predict({"verbosity": -7}) == 25, ""

    if cid == "absolute_overrides":
        r = traits.render({"sarcasm": 6})
        return r.strip().splitlines()[-1].strip()[:80], "overrides" in r and "Sarcasm +6" in r, ""

    if cid == "effective_adds_mood":
        e = traits.effective({"verbosity": -2}, {"verbosity": -1.4, "sarcasm": 2.1})
        none = traits.effective(None, {"sarcasm": 0.2})
        got = f"verbosity {e['verbosity']:.1f} sarcasm {e['sarcasm']:.1f}; untouched -> {none}"
        return got, abs(e["verbosity"] + 3.4) < 1e-6 and abs(e["sarcasm"] - 2.1) < 1e-6 and none is None, ""

    if cid == "barge_in_counts_once":
        s, t = _seq([("talked_over", "craig", True)] * 5, gap=10.0)
        v = s["axes"]["irritation"]
        one = mood.EVENTS["talked_over"][0]["irritation"]
        return f"{v:.2f} after five in 40s (one = {one})", abs(v - one) < 0.05, ""

    if cid == "mood_never_silences":
        s, t = _seq([("corrected", "sam", False)] * 6, gap=1.0)   # irritation 10
        d = mood.dial_offsets(s, t)
        return str(d.get("verbosity")), d.get("verbosity", 0) >= -1.0, ""

    if cid == "thanks_lifts_agreement_does_not":
        s, t = _seq([("corrected", "craig", True), ("thanked", "craig", True)], gap=1.0)
        lifted = s["axes"]["irritation"] < 1.5 and s["axes"]["engagement"] >= 1.0
        agree = [w for w in ("you're right", "I agree", "correct", "exactly") if mood.THANKS_RE.search(w)]
        thanks = [w for w in ("thank you Alex", "good job", "that was helpful", "perfect") if not mood.THANKS_RE.search(w)]
        got = f"after thanks irritation {s['axes']['irritation']:.2f} engagement {s['axes']['engagement']:.2f}; agreement matched {agree}; thanks missed {thanks}"
        return got, lifted and not agree and not thanks, ""

    if cid == "values_short_answers":
        from core import values
        sig = [{"kind": "thanks", "words": 12, "looked": True, "asked": False}] * 4 + \
              [{"kind": "correction", "words": 90, "looked": False, "asked": True}] * 2 + \
              [{"kind": "thanks", "words": 80, "looked": False, "asked": False}]
        lines = values.conclude(sig)
        return " | ".join(lines), any(l.startswith("He values short answers") for l in lines) and any("look behind it" in l for l in lines), ""

    if cid == "values_need_lopsided":
        from core import values
        two = values.conclude([{"kind": "thanks", "words": 10}, {"kind": "thanks", "words": 12}])
        split = values.conclude([{"kind": "thanks", "words": 10}] * 3 + [{"kind": "thanks", "words": 90}] * 3)
        return f"two -> {two}; split -> {split}", not two and not split, ""

    if cid == "values_never_agreement":
        import inspect
        from core import values
        src = inspect.getsource(values).lower()
        bad = [w for w in ("agree", "correct\"", "you're right") if w in src.replace("never reads agreement", "").replace("learned from agreement", "").replace("seek agreement", "").replace("reads agreement", "")]
        keys = set(values.reply_shape("x", False).keys())
        return f"shape keys {sorted(keys)}", keys <= {"words", "looked", "asked", "t"} and not bad, f"found {bad}" if bad else ""

    if cid == "pet_needs_fall_and_health_follows":
        from core import pet
        s = pet.fresh(T0)
        later = pet.drifted(s, T0 + 12 * 3600)          # half a day unattended
        food = later["needs"]["food"]
        starved = pet.drifted(s, T0 + 30 * 3600)         # food hits 0 before 14 h; health then falls
        fed = pet.apply_action(pet.fresh(T0), "feed", now=T0 + 3600)
        thriving = pet.drifted(fed, T0 + 3 * 3600)
        got = f"food after 12 h {food:.0f}; after 30 h health {starved['health']:.0f}; tended health {thriving['health']:.0f}"
        return got, food < 80 and starved["health"] < 100 and thriving["health"] == 100, ""

    if cid == "pet_care_takes_the_lowest":
        from core import pet
        s = pet.fresh(T0)
        s["needs"]["company"] = 20.0
        n, v = pet.lowest_need(s)
        after = pet.apply_action(s, pet.action_for(n), now=T0)
        got = f"lowest {n} {v:.0f} -> {pet.action_for(n)} -> company {after['needs']['company']:.0f}, rest {after['needs']['rest']:.0f}"
        return got, n == "company" and pet.action_for(n) == "play" and after["needs"]["company"] == 55.0 and after["needs"]["rest"] == 72.0, ""

    if cid == "tone_is_not_an_input":
        bad = [n for n in mood.EVENTS if "reply" in n]
        return " ".join(mood.EVENTS), not bad, f"found {bad}" if bad else ""

    return "no such case", False, "unknown case id"
