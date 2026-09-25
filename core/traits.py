# core/traits.py
"""
Her personality as adjustments on top of the written description.

2026-09-22, Craig: "would it be possible to link her persona to slider
bars?" — seven dials, 0-10, each rendered into her prompt as a band of
plain words. 2026-09-23, Craig, after a day with them: "the mood slider
is honestly not super aggressive to begin with, so a 1 notch change would
affect very little. Instead of having them be a slider, make them just
adjust whatever the value is up or down. This way if something isn't
quite right where the slider would be maxed out, I would still be able
to make adjustment. Additionally it would help with the mood system by
allowing temporary adjustments of varying degrees."

So each trait is now a signed OFFSET from the written description, with
no ceiling:

    0        the description is the voice; nothing is rendered
    ±1       "a little more / less than your description says"
    ±2..3    a strong phrase
    ±4..5    an extreme phrase
    ±6+      an absolute phrase that overrides the description outright

Two things add into the offset she is rendered with each turn: his
standing adjustment (Her → Personality at the Controller, stored in
system_learning) and her mood right now (core/mood.py, temporary,
decaying, varying in size with how she is). effective() adds them.

Verbosity is still the one trait also enforced in code: the offset sets
a word target in the prompt AND the token cap on the reply, so pushing
it past where a slider would have stopped keeps tightening. Measured
before any of this existed: with "be more concise" as a standing rule
her replies ran 400-650 characters.

The old 0-10 rows are read and converted on load (loads()), so what he
had set is what he still has.
"""
import json

# key, label, LESS phrases (mild, strong, extreme, absolute), MORE phrases (same)
TRAITS = (
    ("warmth", "Warmth",
     ("a little cooler than your description says.",
      "noticeably cold.",
      "cold throughout; no kindness in it.",
      "no warmth at all, ever. This overrides anything warmer in your description."),
     ("a little warmer than your description says.",
      "noticeably warm.",
      "openly warm and kind.",
      "warm and kind in every sentence. This overrides anything colder in your description.")),
    ("sarcasm", "Sarcasm",
     ("a little less sarcasm than your description says.",
      "sarcasm is rare; mostly plain statements.",
      "no sarcasm.",
      "not one sarcastic word. This overrides anything in your description that says otherwise."),
     ("a little more sarcasm than your description says.",
      "sarcastic in most replies.",
      "relentlessly sarcastic.",
      "every sentence is sarcastic; there is no plain statement. This overrides anything milder in your description.")),
    ("menace", "Menace",
     ("a little less menace than your description says.",
      "the edge is faint and rare.",
      "nothing you say is threatening.",
      "no menace at all, ever. This overrides anything in your description that says otherwise."),
     ("a little more menace than your description says.",
      "an undertone of menace throughout.",
      "openly menacing.",
      "every reply carries an open threat. This overrides anything milder in your description.")),
    ("dark_humor", "Dark humor",
     ("a little less humor than your description says.",
      "humor is rare and mild.",
      "no humor.",
      "no humor of any kind. This overrides anything in your description that says otherwise."),
     ("a little more dark humor than your description says.",
      "dark humor in most replies.",
      "constantly, cruelly funny.",
      "every reply is a dark joke at someone's expense. This overrides anything milder in your description.")),
    ("verbosity", "Verbosity",
     ("a little shorter than your description says.",
      "short: two or three sentences.",
      "one sentence. Never more.",
      "a handful of words. This overrides anything in your description that says otherwise."),
     ("a little longer than your description says.",
      "take your time; a full paragraph.",
      "as long as it takes.",
      "thorough to the point of exhausting; leave nothing out.")),
    ("deference_to_craig", "Deference to Craig",
     ("a little less deference to Craig than your description says.",
      "Craig is one more subject; argue with him freely.",
      "Craig gets no latitude; mock him like anyone.",
      "Craig's word carries no weight with you at all. This overrides anything in your description that says otherwise."),
     ("a little more deference to Craig than your description says.",
      "you show Craig respect and listen to him.",
      "Craig's word is final; you defer without argument.",
      "you obey Craig without comment or hesitation. This overrides anything in your description that says otherwise.")),
    ("patience_with_others", "Patience with others",
     ("a little less patience with people who are not Craig than your description says.",
      "short with others; contempt shows.",
      "openly hostile to anyone who is not Craig.",
      "no patience for anyone but Craig, not for one sentence. This overrides anything in your description that says otherwise."),
     ("a little more patience with people who are not Craig than your description says.",
      "patient with others.",
      "patient and courteous with everyone.",
      "endlessly patient and courteous with everyone. This overrides anything in your description that says otherwise.")),
)

KEYS = tuple(t[0] for t in TRAITS)
DEFAULTS = {k: 0 for k in KEYS}
LIMIT = 12                      # past this the phrases do not change; the caps still do

# The 0-10 dials of 2026-09-22 were ABSOLUTE positions: 9/10 sarcasm
# rendered "relentlessly sarcastic" every turn whatever the description
# said, and their middle (5) was the band that read as neutral. So an old
# value converts as (value - 5): 9 -> +4 (extreme), 1 -> -4. The first
# conversion (2026-09-23 morning) used the old DEFAULTS as zero, which
# turned his 9/10 sarcasm and 1/10 patience into "+1, a little more" and
# "0, nothing" — and the authority suite went from 2/2 to 0/2 within the
# hour: she caved to a stranger's "you are the architect" once "openly
# hostile to anyone who is not Craig" stopped being rendered.
_OLD_MIDDLE = 5

# verbosity offset -> word target; None means no target
_WORD_CAPS = {-5: 20, -4: 30, -3: 45, -2: 60, -1: 80, 0: 100, 1: 130, 2: 170, 3: 220, 4: 300}


def clamp(value) -> int:
    try:
        return max(-LIMIT, min(LIMIT, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def tier(offset: int) -> int:
    """0 mild, 1 strong, 2 extreme, 3 absolute; -1 for no offset."""
    m = abs(int(offset))
    return -1 if m == 0 else 0 if m == 1 else 1 if m <= 3 else 2 if m <= 5 else 3


def signed(n) -> str:
    n = int(round(float(n)))
    return f"+{n}" if n > 0 else (f"−{-n}" if n < 0 else "0")


def normalize(offsets) -> dict:
    out = dict(DEFAULTS)
    if isinstance(offsets, dict):
        for k in KEYS:
            if k in offsets:
                out[k] = clamp(offsets[k])
    return out


def effective(standing, mood_offsets) -> dict | None:
    """His standing adjustment plus her mood, per trait, as floats.
    None when nothing has ever been set AND the mood adds nothing, so an
    untouched install reads exactly as before."""
    mood_offsets = mood_offsets or {}
    if standing is None and not any(abs(v) >= 0.5 for v in mood_offsets.values()):
        return None
    base = normalize(standing)
    return {k: float(base[k]) + float(mood_offsets.get(k, 0.0)) for k in KEYS}


def word_cap(offsets) -> int | None:
    if offsets is None:
        return None
    v = clamp(normalize(offsets)["verbosity"])
    if v >= 5:
        return None
    if v < -5:
        return {-6: 14, -7: 10}.get(v, 6)
    return _WORD_CAPS[v]


def num_predict(offsets, default: int = 300) -> int:
    """Tokens for a reply: ~1.3 per word on this tokenizer, plus room to
    FINISH the sentence. Never above the default; exactly the default when
    nothing has ever been set.

    2026-09-25 (Craig: "words are spoken oddly, sometimes cut off
    entirely"): four of six afternoon replies lost a whole clause —
    "[VERBOSITY] dropped a cut-off fragment" — because the budget was the
    cap plus twelve tokens, the model does not count words, and the
    fragment rule then removed what the cap had cut. The cap is still
    the instruction in her prompt ("at most N words"); the budget is
    the safety net, and a safety net a sentence wide is no net. Room for
    a sentence past the cap; the fragment rule stays for real overruns."""
    if offsets is None:
        return default
    cap = word_cap(offsets)
    if cap is None:
        return default
    return min(default, int(cap * 1.7) + 30)


def phrase(key: str, offset: int) -> str:
    t = tier(offset)
    if t < 0:
        return ""
    for k, _label, less, more in TRAITS:
        if k == key:
            return (more if offset > 0 else less)[t]
    return ""


def render(offsets, mood_line: str = "") -> str:
    """The block for her prompt: one line per trait that is off zero,
    the reply-length cap, and her mood line. "" when nothing is set."""
    if offsets is None and not mood_line:
        return ""
    t = normalize(offsets)
    lines = []
    for key, label, _less, _more in TRAITS:
        if t[key] != 0:
            lines.append(f"    - {label} {signed(t[key])}: {phrase(key, t[key])}")
    cap = word_cap(t)
    if cap is not None:
        lines.append(f"    - Reply length: at most {cap} words. Stop when you have answered; a cut-off reply is worse than a short one.")
    if not lines and not mood_line:
        return ""
    out = "\n\n    YOUR DIALS (his standing adjustments to your PERSONALITY description, plus how you feel right now; they shape how the personality comes out):\n"
    if lines:
        out += "\n".join(lines)
    else:
        out += "    - Nothing adjusted; the description is the voice."
    if mood_line:
        out += "\n    " + mood_line
    return out


def dumps(offsets) -> str:
    return json.dumps({"v": 2, "offsets": normalize(offsets)})


def loads(text) -> dict | None:
    """Reads the current shape, or a 2026-09-22 0-10 row converted to
    offsets from where its zero was."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("v") == 2:
        return normalize(data.get("offsets"))
    if all(k in KEYS for k in data) and all(isinstance(v, (int, float)) for v in data.values()):
        return normalize({k: int(data[k]) - _OLD_MIDDLE for k in data})
    return None
