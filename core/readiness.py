"""
Startup readiness.

2026-09-20 (Craig, watching her first turn after a Controller launch): the
transcript showed his message immediately and she then sat silent for 9.3
seconds. Nothing was wrong — Ollama had only just started, and the first
request had to pull qwen2.5:7b into VRAM. The log is unambiguous:

    13:27:48  Heard: Alex.
    13:27:57  [TIMING] intent classification: 0.86s

Nine of those seconds were the model loading; the classification itself took
under a second, and the next turn completed in 4.04s. So it is a one-time cost
per launch — but it lands on the very first thing anyone says, every single
time, and looks exactly like her ignoring you.

Craig: "if it's still loading should we build some kind of stoppage to prevent
interaction until it's loaded fully and show that load status in the webui?"

Two halves, and the first is what actually removes the delay:

  1. WARM UP so the wait happens before anyone is talking, not during. A
     throwaway generation at startup pulls the weights in while the UI is still
     connecting. By the time a real turn arrives the model is resident.
  2. SAY SO, so the remaining window is legible instead of looking broken. The
     state is broadcast to every connected client, not only the creator, since
     anyone connecting mid-load sees the same silence.

The states are deliberately coarse. "Which of four things is she doing" is all
the UI needs; anything finer would be describing Ollama's internals to someone
who just wants to know whether to start talking.
"""
import asyncio

from config.logger_config import logger

STARTING = "starting"      # process up, Ollama not yet reachable
LOADING = "loading"        # Ollama up, pulling the model into VRAM
READY = "ready"            # a real generation has completed end to end
FAILED = "failed"          # warm-up errored; she may still work, degraded

_state = STARTING

# Human-facing, sent verbatim to the UI. Kept here rather than in the page so
# the wording is in one place and cannot drift between them.
LABELS = {
    STARTING: "Starting up...",
    LOADING: "Loading language model...",
    READY: "Ready",
    FAILED: "Started with errors - check the log",
}


def get_state() -> str:
    return _state


def is_ready() -> bool:
    return _state == READY


def signal_text() -> str:
    return f"__READY__{_state}"


async def set_state(state: str):
    """Set and broadcast. Import of ws_handlers is deferred because it imports
    most of the app — a module-level import here would be circular."""
    global _state
    if state == _state:
        return
    _state = state
    logger.info(f"[READY] {state} - {LABELS.get(state, state)}")
    if state == "failed":
        # 2026-09-23: starting with errors is strain (core/mood.py)
        try:
            from core import mood as _mood
            await _mood.note("startup_failed")
        except Exception:
            pass

    try:
        from ws.ws_handlers import broadcast_signal
        await broadcast_signal(signal_text())
    except Exception as e:
        # A UI that missed one transition is a cosmetic problem; it must never
        # take down startup.
        logger.warning(f"Could not broadcast readiness state: {e}")


async def warm_up(ollama_manager, model: str = None):
    """Force the model resident, then mark ready.

    The generation is deliberately tiny (`num_predict: 1`): the point is paying
    the load cost, not producing anything. Failure is logged and marked FAILED
    rather than raised — she is still usable without a warm cache, just slow on
    the first turn, which is exactly the old behaviour.
    """
    await set_state(LOADING)
    try:
        t0 = asyncio.get_event_loop().time()
        await ollama_manager.generate_text("hi", timeout=300.0, num_predict=1)
        logger.info(f"[READY] model warm in {asyncio.get_event_loop().time() - t0:.1f}s")
        await set_state(READY)
    except Exception as e:
        logger.warning(f"Model warm-up failed, first turn will be slow: {e}")
        await set_state(FAILED)
