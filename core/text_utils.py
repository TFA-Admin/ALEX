# core/text_utils.py

"""
Shared text-cleanup helpers (2026-07-16, moved here from
systems/controller/_text.py once a second, unrelated package needed the
same thing — a helper used across package boundaries belongs somewhere
neutral, not nested inside the first consumer that happened to need it).

A command that extracts a name/argument by stripping a fixed prefix off
the raw utterance ("disable module egg_timer." -> "egg_timer.") ends up
with STT's trailing sentence punctuation baked into the value — a name
with a literal period on the end never matches a real system/module/
table name, so the command fails, silently and confusingly (not "denied"
or "not found" in an obviously wrong way, just "I don't have a module
called 'egg_timer.'"). This hit three different controller commands in
one session (the elevated-access approval regex, then "reload system
diagnostics.") before being centralized instead of patched one call site
at a time — see SELF_MODIFICATION_ARCHITECTURE.md's session history.
"""

import re

_STRIP_CHARS = " .,!?"

# 2026-07-16: found live — Craig told her to stop using emojis several
# times (creator override, merged into personality, added as a hard rule
# in db.system_learning) and the model still produced one anyway.
# Instruction-following for a negative constraint ("never do X") isn't
# perfectly reliable on this size of model even with temperature=0 and
# explicit prompting — same lesson as every other place in this project
# that a real guarantee needs a deterministic check, not trusting the LLM
# to comply. Range covers the common emoji blocks (emoticons, symbols,
# transport/map symbols, supplemental symbols, dingbats, variation
# selectors) — broad enough to catch what she's actually produced without
# stripping ordinary punctuation/text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def strip_trailing_punctuation(text: str) -> str:
    return text.strip(_STRIP_CHARS)


def first_word(text: str) -> str:
    """The first whitespace-separated token, trailing punctuation
    stripped — for matching a short reply ("yes"/"no"/"y"/"n") without
    the false-positive risk of a raw .startswith() check, which matches
    ANY message starting with that letter ("You're just gonna say that
    for everything now".startswith("y") is True). Confirmed live
    (2026-07-16): that exact false positive let an unrelated sentence
    accidentally confirm a 20-minute-stale pending module build."""
    parts = text.strip().split(None, 1)
    if not parts:
        return ""
    return strip_trailing_punctuation(parts[0])


# ---------------------------------------------------------------------------
# BANNED PHRASES (2026-09-20)
# ---------------------------------------------------------------------------
# Craig told her "stop saying 'Deal with it'". It went into her hard rules,
# rendered as "CREATOR-MANDATED RULES (never violate these, no matter what)".
# She said it twice more within two minutes. He asked whether that was spite
# or an error.
#
# It was an error, and the log shows the mechanism. In the three minutes
# BEFORE the instruction she had used the phrase eight times, twice in
# byte-identical replies — her own output goes back into every prompt as the
# last four turns of MEMORY, so each use made the next more likely. By the
# time the rule arrived, her recent context was saturated with concrete
# examples of saying it, and one line of instruction was competing with
# several in-context demonstrations. Examples beat instructions. Then he
# said "say deal with it again and there will be repercussions", putting the
# phrase in her context once more, and she said it two seconds later.
#
# This project already learned that a prompt instruction is not enough for
# exactly this class of rule — that is why strip_emojis() exists, after
# "stop using emojis" failed in both the personality AND the hard-rules
# block. This is the same guarantee generalised: a creator rule of the form
# "stop saying X" becomes a real check on the way out, not only a line on
# the way in.
_BANNED_RULE_RE = re.compile(
    r"(?:stop|don'?t|do not|never|avoid|quit|no more)\s+"
    r"(?:saying|using|say|use|repeating|repeat)\s+"
    r"[\"'‘“]?(?P<phrase>[^\"'’”]+?)[\"'’”]?\s*$",
    re.IGNORECASE,
)


def extract_banned_phrases(rules) -> list:
    """Pull the literal phrases out of creator rules like
    "stop saying 'Deal with it'". Returns lowercase phrases.

    Deliberately conservative about what counts. "be less repetitive" is a
    style note with no literal to match and is ignored; only a rule naming
    something concrete produces a ban. Anything under three characters is
    dropped — a one-letter ban would shred every reply."""
    phrases = []
    for rule in rules or []:
        rule = str(rule).strip()

        # "stop using emojis" is already enforced by strip_emojis(), which
        # removes the CHARACTERS. Treating it as a literal ban would remove
        # the WORD, so "I don't use emojis" would come out mangled.
        if "emoji" in rule.lower():
            continue

        m = _BANNED_RULE_RE.search(rule)
        if not m:
            continue
        phrase = m.group("phrase").strip().strip("\"'‘’“”").strip()
        if len(phrase) >= 3:
            phrases.append(phrase.lower())
    return phrases


def _standalone_pattern(phrase: str) -> re.Pattern:
    """Matches the phrase only where it stands as its own sentence or
    trailing clause, not inside one.

    This is the whole precision of the mechanism. "Deal with it" as a
    sign-off tic is what he objected to; "I'll deal with it" is ordinary
    English and cutting it out mid-sentence would leave broken grammar and
    look like a different bug. So a ban removes "... . Deal with it." and
    leaves "Sure, I'll deal with it." alone.

    A trailing comma counts as a clause end too — "Deal with it, Craig."
    was the live example (20:07:16) and is the same tic with a name on it;
    removing the phrase and its comma leaves "Craig. Next time..." intact.

    Known limit, left deliberately: a REFERENCE to the phrase survives.
    "Or should I just say, deal with it?" is not matched, because it is
    preceded by a comma rather than a sentence end. She is talking about
    the phrase there rather than using it, which is arguably fair, and
    widening the pattern to catch it starts eating ordinary sentences."""
    esc = re.escape(phrase)
    return re.compile(
        r"(?:(?<=^)|(?<=[.!?\n])|(?<=[.!?][\"'’”]))\s*"
        + esc + r"[\s]*[.!?,;]*[\"'’”]?(?=\s|$)",
        re.IGNORECASE,
    )


def _apply_bans(text: str, phrases) -> str:
    """The substitution alone — no whitespace tidying, no strip.

    Kept separate because the streaming path feeds this FRAGMENTS of a
    reply, and stripping each fragment welds the words together: an early
    version turned "Next time, keep it brief." into
    "Next time,keepitbrief." because every emitted segment lost its edges.
    Tidying happens once, on a whole string, or not at all."""
    if not text or not phrases:
        return text
    for phrase in phrases:
        text = _standalone_pattern(phrase).sub("", text)
    return text


def suppress_phrases(text: str, phrases) -> str:
    """Removes banned phrases used as standalone sentences, and tidies up
    after itself. For whole strings; the streaming path below uses
    _apply_bans directly."""
    if not text or not phrases:
        return text
    return re.sub(r"[ \t]{2,}", " ", _apply_bans(text, phrases)).strip()


class PhraseSuppressor:
    """Applies suppress_phrases() to a STREAM.

    strip_emojis() can work chunk by chunk because an emoji is one
    character and never straddles a boundary. A phrase does — "Deal" can
    arrive in one chunk and " with it." in the next — so a per-chunk
    replace would miss most real cases.

    Holds back a tail long enough that any banned phrase spanning a
    boundary is still whole when it is examined, and emits everything
    before it. flush() releases what is left at the end of generation.
    Nothing is buffered at all when there are no bans, so the streaming
    path is unchanged for everyone who has not asked for one."""

    def __init__(self, phrases):
        self.phrases = list(phrases or [])
        # + a little slack for the surrounding punctuation and whitespace
        # the pattern above is allowed to consume.
        self._keep = (max((len(p) for p in self.phrases), default=0) + 8)
        self._buf = ""

    @property
    def active(self) -> bool:
        return bool(self.phrases)

    def feed(self, chunk: str) -> str:
        if not self.phrases:
            return chunk

        self._buf += chunk
        if len(self._buf) <= self._keep:
            return ""

        emit, self._buf = self._buf[:-self._keep], self._buf[-self._keep:]
        return _apply_bans(emit, self.phrases) if emit else ""

    def flush(self) -> str:
        if not self.phrases:
            return ""
        out, self._buf = self._buf, ""
        return _apply_bans(out, self.phrases) if out else ""
