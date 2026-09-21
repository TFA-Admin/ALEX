# controller/
"""
The A.L.E.X. Controller — her window, split into one module per view.

2026-09-21. ALEX_Controller.py had grown to 2,637 lines in a single class
with ten tabs, and Craig said what anyone would: "It's gotten a bit messy
and busy... I would like a better cleaner ui without losing and possibly
even gaining functionality." Six views now, each its own file, every
function kept and the reasons behind them kept with the code that has
them:

    Run       start, stop, restart, model, Ollama, live consoles
    Inbox     everything waiting on him, in one list; settled items in History
    Her       personality, standing rules, beliefs, decisions, curiosity,
              modules, health
    People    who is talking to her right now, by name, and everyone she knows
    Data      the database browser
    Commands  the reference (COMMANDS.md)

ALEX_Controller.py is the launcher. The Controller is a separate process
from her and stays that way: it reads and writes the same database and
watches her log file, and the kill path (Stop A.L.E.X.) is an OS-level
terminate that nothing of hers can reach.

Import order matters here — see the comment on _preload_inquiry_system().
"""
import threading


# 2026-07-17: found live — crashed the instant Craig clicked "Decline
# Retention". Root cause: systems.inquiry.system (needed for
# retain_report()/decline_report()) transitively imports
# sentence_transformers -> sklearn, and importing that AFTER PySide6 is
# already loaded triggers a real, confirmed slow/broken interaction —
# PySide6/shiboken installs an import hook that inspects every class
# defined in every module imported afterward via inspect.getsource(),
# and sklearn's own imports get caught by it. An earlier attempt fixed
# this by importing it eagerly at module level instead, but that made
# EVERY Controller launch pay the full import cost just for two rarely-
# clicked buttons — moving it back to a lazy import inside the button
# handlers (a later attempt) just relocated the exact same hang to click
# time instead of launch time.
#
# Real fix: start the import in a background thread HERE, before PySide6
# is imported anywhere in this package — while the hook doesn't exist
# yet, so this can't be caught by it no matter how long it takes. It
# finishes (and gets cached in sys.modules) in parallel while the rest of
# the app starts up, so by the time a button handler actually needs it,
# it's normally just an instant cache hit. If a click somehow lands
# before this finishes, Python's own import lock makes that import call
# simply wait for this one to complete rather than starting a redundant,
# hook-exposed import of its own.
#
# It lives in the package __init__ so that ANY entry point (the launcher,
# a test that imports controller.app directly) gets the same ordering.
def _preload_inquiry_system():
    try:
        import systems.inquiry.system  # noqa: F401
    except Exception:
        pass


threading.Thread(target=_preload_inquiry_system, daemon=True).start()
