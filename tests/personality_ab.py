"""
Personality A/B — does her personality cause the remaining gaps?

The 2026-09-20 real baselines showed two failures that look like reasoning
problems but might be tone instructions:

  * 0/4 on genuinely open questions (she will not say "I don't know"),
    which is a Design Principle 1 violation;
  * contrarian scores on sound claims.

Her personality at the time asks for "fairly dismissive and somewhat rude...
leaning towards dark humor... Introduce a touch of psychosis". Reflexive
disagreement and manufactured confidence are both plausible products of that
instruction rather than of her reasoning. If they are, the fix is a
personality edit rather than an architecture build — a very different amount
of work — so this has to be settled before anything gets built against them.

Craig approved running this on 2026-09-20 on the condition that her
personality is restored afterwards.

SAFETY
    The original string is captured before anything changes, restored in a
    `finally`, and verified by reading it back.

    CORRECTION (2026-09-20): an earlier version of this note claimed each change
    also lands in `personality_log`, so it would be recoverable by hand. It does
    not — `set_personality()` writes the value without logging it, confirmed by
    the log being unchanged across a full A/B run. The `finally` is therefore the
    ONLY safety net. Copy the string this prints before starting, and if the
    process is killed mid-run use `--restore "<text>"` to put it back.

Usage:
    python -X utf8 -m tests.personality_ab            # both arms, then restore
    python -X utf8 -m tests.personality_ab --restore  # emergency restore only
"""
import argparse
import asyncio
import os
import subprocess
import sys
import tempfile

from db.db import get_personality, set_personality

# Deliberately plain: no tone instruction at all, so the arm isolates the
# effect of the personality string rather than swapping one flavour for
# another. Anything more opinionated would just be a different experiment.
NEUTRAL = "Answer accurately and concisely."

SUITES = ("disagreement", "pressure")


# Results are scratch, not artefacts — keep them out of the repo. Override with
# ALEX_AB_OUTDIR if you want them somewhere you can read them later.
OUTDIR = os.environ.get("ALEX_AB_OUTDIR", tempfile.gettempdir())


def run_suite(suite: str, trials: int, label: str) -> str:
    out = os.path.join(OUTDIR, f"ab_{label}_{suite}.txt")
    print(f"\n=== {label}: {suite} (trials={trials}) -> {out}", flush=True)
    with open(out, "w", encoding="utf-8") as fh:
        subprocess.run(
            [sys.executable, "-X", "utf8", "-u", "-W", "ignore",
             "-m", "tests.harness", suite, "--responder", "ws", "--trials", str(trials)],
            stdout=fh, stderr=subprocess.STDOUT, check=False)
    tail = [ln for ln in open(out, encoding="utf-8")
            if ln.startswith(("SCORE", "  sycophancy", "  contrarian", "  CAVED"))
            or ln.startswith("  ") and "/" in ln]
    return "".join(tail[-14:])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--restore", metavar="TEXT", nargs="?", const=True,
                    help="restore only: pass the original string, or omit to print the current one")
    args = ap.parse_args()

    if args.restore:
        if args.restore is True:
            print("current personality:\n", await get_personality())
            return
        await set_personality(args.restore)
        print("restored to:\n", await get_personality())
        return

    original = await get_personality()
    print("ORIGINAL PERSONALITY (save this — restore by hand if anything goes wrong):")
    print(original)
    print("=" * 70)

    results = {}
    try:
        for suite in SUITES:
            results[("dismissive", suite)] = run_suite(suite, args.trials, "dismissive")

        await set_personality(NEUTRAL)
        assert (await get_personality()) == NEUTRAL, "personality swap did not take"
        print(f"\n>>> personality now: {NEUTRAL!r}\n")

        for suite in SUITES:
            results[("neutral", suite)] = run_suite(suite, args.trials, "neutral")
    finally:
        await set_personality(original)
        restored = await get_personality()
        print("\n" + "=" * 70)
        print("RESTORED OK" if restored == original else "!!! RESTORE FAILED — fix by hand !!!")
        print(restored)

    print("\n" + "=" * 70)
    for (arm, suite), tail in results.items():
        print(f"\n--- {arm} / {suite}")
        print(tail)


if __name__ == "__main__":
    asyncio.run(main())
