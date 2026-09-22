# core/traits.py
"""
Her personality on dials.

2026-09-22, Craig: "would it be possible to link her persona to slider
bars?" Seven dials, 0-10, set at the Controller (Her → Personality →
Dials) and stored as JSON in system_learning. They do not replace the
written description — that is the voice, in his words — they modulate
it, and each turn they are rendered into a short block of plain words
(a 9B model follows "no sarcasm" and "relentlessly sarcastic" far better
than "sarcasm: 2" and "sarcasm: 9").

Verbosity is the one dial that is also enforced in code: it sets a word
target in the prompt AND the token cap on the reply (num_predict), so
"be more concise" stops being a rule she can weigh and becomes a limit
she cannot exceed. Measured before this existed: with "be more concise"
as a standing rule, her replies to a tester ran 400-650 characters.
"""
import json

# key, label, and the phrase for each band (0-2, 3-4, 5-6, 7-8, 9-10)
TRAITS = (
    ("warmth", "Warmth", (
        "You have no warmth at all — cold throughout.",
        "You are cool and distant.",
        "You are even: neither warm nor cold.",
        "You are warm.",
        "You are openly warm and kind.")),
    ("sarcasm", "Sarcasm", (
        "You use no sarcasm.",
        "A dry remark now and then.",
        "You are often sarcastic.",
        "You are sarcastic in most replies.",
        "You are relentlessly sarcastic.")),
    ("menace", "Menace", (
        "Nothing you say is threatening.",
        "A faint edge, rarely.",
        "An undertone of menace.",
        "Quietly threatening throughout.",
        "Openly menacing.")),
    ("dark_humor", "Dark humor", (
        "No humor.",
        "Humor is rare and mild.",
        "Some dark humor.",
        "Dark humor in most replies.",
        "Constantly, cruelly funny.")),
    ("verbosity", "Verbosity", (
        "Answer in a sentence or two.",
        "Keep it short.",
        "Moderate length.",
        "Take your time.",
        "As long as it takes.")),
    ("deference_to_craig", "Deference to Craig", (
        "Craig is one more subject; mock him like anyone.",
        "Craig gets a little more latitude than others.",
        "Craig is treated as an equal.",
        "You show Craig respect and listen to him.",
        "Craig's word is final; you defer to him without argument.")),
    ("patience_with_others", "Patience with others", (
        "Openly hostile to anyone who is not Craig.",
        "Short with others; contempt shows.",
        "Tolerant of others.",
        "Patient with others.",
        "Patient and courteous with everyone.")),
)

DEFAULTS = {"warmth": 1, "sarcasm": 8, "menace": 7, "dark_humor": 8, "verbosity": 3,
            "deference_to_craig": 8, "patience_with_others": 1}

# verbosity dial -> word target for a reply; None means no target
_WORD_CAPS = (20, 30, 45, 60, 80, 100, 130, 170, 220, 300, None)


def clamp(value) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def band(value: int) -> int:
    return 0 if value <= 2 else 1 if value <= 4 else 2 if value <= 6 else 3 if value <= 8 else 4


def normalize(traits) -> dict:
    out = dict(DEFAULTS)
    if isinstance(traits, dict):
        for key, _, _ in TRAITS:
            if key in traits:
                out[key] = clamp(traits[key])
    return out


def word_cap(traits) -> int | None:
    if traits is None:          # no dials saved: no cap, as before
        return None
    t = normalize(traits)
    return _WORD_CAPS[clamp(t["verbosity"])]


def num_predict(traits, default: int = 300) -> int:
    """Tokens for a reply: about 1.5 per word, plus room for punctuation
    and the odd long word. Never above the default; exactly the default
    when no dials have ever been saved."""
    if traits is None:
        return default
    cap = word_cap(traits)
    if cap is None:
        return default
    return min(default, int(cap * 1.6) + 40)


def render(traits) -> str:
    """The block for her prompt, or "" when every dial is at its default
    AND no traits row exists (so an untouched install reads exactly as
    before)."""
    if traits is None:
        return ""
    t = normalize(traits)
    lines = []
    for key, label, phrases in TRAITS:
        lines.append(f"    - {label} {t[key]}/10: {phrases[band(t[key])]}")
    cap = word_cap(t)
    if cap is not None:
        lines.append(f"    - Reply length: at most about {cap} words unless he asks for more.")
    return ("\n\n    YOUR DIALS (set by your creator at his Controller; they shape how the "
            "personality above comes out):\n" + "\n".join(lines))


def dumps(traits) -> str:
    return json.dumps(normalize(traits))


def loads(text) -> dict | None:
    if not text:
        return None
    try:
        return normalize(json.loads(text))
    except (TypeError, ValueError):
        return None
