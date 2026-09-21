# tools/commands_drift.py
"""
Does COMMANDS.md still say what the code listens for?

2026-09-21 (Craig: "I won't remember them all... I need some kind of
reference for all this because she will eventually have too many for me
to accurately remember them all word for word."). The July version of
COMMANDS.md was written by hand and drifted the way every hand-kept list
here has. This makes drift a failure instead of a surprise: every trigger
tuple the systems define, and every `startswith("...")` literal under
systems/, must appear in COMMANDS.md, case-insensitively, or this exits 1
naming what is missing.

    python -X utf8 tools/commands_drift.py

Deliberately light: it imports the trigger constants from the modules
that own them, and reads a couple of files as text where importing would
load a model (ws/ws_audio.py loads Whisper; core/tools.py loads the
embedder).
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

doc = open(os.path.join(ROOT, "COMMANDS.md"), encoding="utf-8").read().lower()
missing = []


def need(literal: str, where: str):
    lit = " ".join(str(literal).lower().split())
    # Internal markers are not commands: "(unprompted" is how her own
    # unprompted turns are tagged in memory rows, "__" prefixes are wire
    # signals. Neither is something he says.
    if lit.startswith(("(", "__")):
        return
    if lit and lit not in doc:
        missing.append((lit, where))


def read(path: str) -> str:
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


# --- trigger tuples, imported from their owners --------------------------
from systems.inquiry.system import SEARCH_TRIGGERS                      # noqa: E402
from systems.modules.system import KNOWN_MODULE_TRIGGERS                # noqa: E402
from systems.command.system import (SET_EDIT_CODE_TRIGGERS,             # noqa: E402
                                    SET_OVERRIDE_CODE_TRIGGERS,
                                    LOCK_PROFILE_TRIGGERS)
from systems.controller._personality import PERSONALITY_RESET_TRIGGERS  # noqa: E402
from core.text_utils import YES_WORDS, NO_WORDS                         # noqa: E402

for t in SEARCH_TRIGGERS:
    need(t, "systems/inquiry/system.py SEARCH_TRIGGERS")
for triggers in KNOWN_MODULE_TRIGGERS.values():
    for t in triggers:
        need(t, "systems/modules/system.py KNOWN_MODULE_TRIGGERS")
for t in SET_EDIT_CODE_TRIGGERS:
    need(t, "systems/command/system.py SET_EDIT_CODE_TRIGGERS")
for t in SET_OVERRIDE_CODE_TRIGGERS:
    need(t, "systems/command/system.py SET_OVERRIDE_CODE_TRIGGERS")
for t in LOCK_PROFILE_TRIGGERS:
    need(t, "systems/command/system.py LOCK_PROFILE_TRIGGERS")
for t in PERSONALITY_RESET_TRIGGERS:
    need(t, "systems/controller/_personality.py PERSONALITY_RESET_TRIGGERS")
for w in YES_WORDS:
    need(w, "core/text_utils.py YES_WORDS")
for w in NO_WORDS:
    need(w, "core/text_utils.py NO_WORDS")

# --- read as text where importing would load a model ---------------------
audio = read("ws/ws_audio.py")
m = re.search(r"CONFIRM_WORDS\s*=\s*\{([^}]*)\}", audio)
for w in re.findall(r'"([^"]+)"', m.group(1) if m else ""):
    need(w, "ws/ws_audio.py CONFIRM_WORDS")

tools_src = read("core/tools.py")
for name in re.findall(r'_fn\("([a-z_]+)"', tools_src):
    need(name, "core/tools.py TOOLS")

handlers = read("ws/ws_handlers.py")
m = re.search(r"_STOP_LISTENING_RE\s*=\s*re\.compile\(r\"([^\"]+)\"", handlers)
for phrase in re.findall(r"\\b([a-z ]+)\\b", m.group(1) if m else ""):
    need(phrase, "ws/ws_handlers.py _STOP_LISTENING_RE")

# --- every startswith("...") literal under systems/ -----------------------
for path in glob.glob(os.path.join(ROOT, "systems", "**", "*.py"), recursive=True):
    src = open(path, encoding="utf-8").read()
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    for lit in re.findall(r'startswith\(\s*"([^"]+)"', src):
        need(lit, rel)
    for group in re.findall(r"startswith\(\(([^)]+)\)", src):
        for lit in re.findall(r'"([^"]+)"', group):
            need(lit, rel)

if missing:
    print("COMMANDS.md is missing these triggers:")
    for lit, where in sorted(set(missing)):
        print(f"  {lit!r:40s}  <- {where}")
    sys.exit(1)

print("COMMANDS.md covers every trigger the code defines.")
