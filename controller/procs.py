# controller/procs.py
"""Starting and stopping her and Ollama. This is the kill path: an
OS-level terminate from a process that is not her, which is why it stays
exactly as it was through the 2026-09-21 split — only the class around it
is new."""
import os
import time
import subprocess

import psutil
from PySide6.QtWidgets import QMessageBox

from controller.common import (
    ALEX_DIR, OLLAMA_EXE_PATH, OLLAMA_LOG_PATH,
    is_port_open, find_pid_by_port, find_orphan_processes,
)


class ProcessManager:
    """Owns the two processes when the Controller launched them, and finds
    them by port when it did not. `log` is the Controller's log router;
    `selected_model` returns the model name to hand her; `parent` is the
    window the confirmation dialogs belong to."""

    def __init__(self, log, selected_model, parent=None):
        self.log = log
        self._selected_model = selected_model
        self.parent = parent

        # Processes (only set when the Controller itself launches them)
        self.ollama_proc = None
        self.alex_proc = None
        self.ollama_log_file = None  # kept open for the life of a Controller-launched Ollama process

    def ollama_up(self) -> bool:
        return bool(self.ollama_proc) or is_port_open(11434)

    def alex_up(self) -> bool:
        return bool(self.alex_proc) or is_port_open(5000)

    # ---------------- OLLAMA ----------------
    def start_ollama(self):
        # 2026-09-23: a handle whose process has exited must not block
        # Start (the same stale handle that made Stop need two clicks).
        if self.ollama_proc and self.ollama_proc.poll() is None:
            return
        self.ollama_proc = None

        self.log("[Ollama] Starting...")

        # 2026-07-16: found live — real, active outage, not a hypothetical.
        # Two "runner" processes from an earlier crash cycle survived an
        # unclean shutdown (Controller closed without going through Stop
        # Ollama, so stop_ollama()'s own cleanup call below never ran) and
        # sat there holding GPU VRAM. By the next restart, that plus two
        # freshly-loaded models left the card at 12021/12288 MiB — nearly
        # full — and every generation request hung indefinitely (0% GPU
        # utilization, no error, no timeout, just stuck) instead of
        # completing or failing cleanly. Confirmed directly: killing those
        # two PIDs dropped VRAM to 5059 MiB and a test call that had been
        # hanging for 90+ seconds completed in 1.6s immediately after.
        # Running this check here too (not just in stop_ollama()) catches
        # orphans left over from ANY unclean prior exit before they can
        # starve the fresh instance about to launch.
        self.cleanup_orphaned_ollama_runners()

        # Redirect to the same stable file the tailer watches, rather than
        # a PIPE — this way Ollama's output is visible the same way no
        # matter who launches the process, and an unread PIPE can't fill up
        # and block it.
        self.ollama_log_file = open(OLLAMA_LOG_PATH, "ab")

        # Was 2, so chat (qwen2.5:7b) and module builds
        # (deepseek-coder:6.7b) could stay resident together rather than
        # Ollama evicting and reloading on every switch (confirmed live
        # via /api/ps: a build in progress held deepseek in VRAM while a
        # chat message sat waiting ~30-50s for qwen to reload). That
        # workflow is gone — deepseek was retired as the builder on
        # 2026-07-16 when module authoring moved to Claude, and every
        # call site in llm/ollama_client.py resolves to the single
        # DEFAULT_MODEL, so exactly one model is ever requested.
        #
        # Pinned to 1 on 2026-09-20 with the RTX 3080 swap rather than
        # dropped: unset means auto (Ollama reports 0 and scales with
        # the GPU), which is looser, not tighter. At 10GB instead of
        # 12GB, with Whisper now sharing the card, a second resident
        # model is no longer free headroom — it matters most when
        # comparing models, where a ceiling of 2 would let Ollama hold
        # the old and new one at once (~9.4GB of weights) while
        # ALEX_LLM_MODEL is swapped. Also set to 1 as a Windows User
        # env var on this machine; setdefault() means that copy wins,
        # so the two are kept in agreement deliberately.
        ollama_env = os.environ.copy()
        ollama_env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")

        # 2026-09-20 (RTX 3080 swap): flash attention back on. It was
        # pinned to 0 on 2026-07-16 after a live "CUDA error: the launch
        # timed out and was terminated" — a Windows GPU-driver watchdog
        # killing a kernel that did not return in time — on the reasoning
        # that FA kernels are tuned for newer GPUs and the Titan X (2015,
        # compute capability 5.2) sat well outside where that path is
        # normally validated. That was logged at the time as "a real test,
        # not a confirmed fix." Ampere (cc 8.6) is precisely what FA is
        # tuned for, so the original rationale no longer applies.
        ollama_env.setdefault("OLLAMA_FLASH_ATTENTION", "1")

        # Requires flash attention, so this was unavailable until now.
        # q8_0 roughly halves the KV cache, clawing back part of the 2GB
        # lost in the 12GB -> 10GB swap.
        ollama_env.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")

        self.ollama_proc = subprocess.Popen(
            [OLLAMA_EXE_PATH, "serve"],
            stdout=self.ollama_log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=ollama_env
        )

    def cleanup_orphaned_ollama_runners(self):
        """Windows doesn't kill child processes when a parent dies — every
        "ollama serve" that stops (this button, a crash, or the tray app's
        own respawn cycle before tonight's fix) can leave its "ollama
        runner" child behind, still holding a full model resident in
        VRAM. Confirmed live (2026-07-16, Craig: "we keep getting orphans
        and I'm not sure why") — found a live runner process whose parent
        PID didn't exist at all anymore. Sweeps for ANY runner whose
        parent isn't a currently-running ollama.exe, not just ones this
        specific Stop click just orphaned, so it also catches leftovers
        from earlier crashes/restarts tonight."""
        live_ollama_pids = set()
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                if (proc.info["name"] or "").lower() == "ollama.exe":
                    live_ollama_pids.add(proc.info["pid"])
        except Exception:
            pass

        for proc in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != "ollama.exe":
                    continue
                if "runner" not in (proc.info.get("cmdline") or []):
                    continue
                if proc.info["ppid"] not in live_ollama_pids:
                    proc.kill()
                    self.log(f"[SYSTEM] Cleaned up orphaned Ollama runner (PID {proc.info['pid']}, parent {proc.info['ppid']} no longer exists)")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    # 2026-09-23 (Craig: "when I click to stop ALEX or Ollama it sometimes
    # doesn't stop, requiring an additional push of the button"). Stop
    # trusted the handle it had started: terminate() on it, forget it,
    # done. When that process had already been replaced — every headless
    # restart from a shell does this, and Ollama's tray app respawns its
    # server — the handle was a dead process, terminate() was a silent
    # no-op, and only the SECOND click (handle gone, so "find by port")
    # reached the live one. Now: whatever serves the port is the target,
    # plus the handle if it is still alive; then wait for the port to
    # clear, and kill outright if it has not.
    def _stop_port(self, label: str, port: int, handle, grace: float = 6.0):
        pids = set()
        if handle is not None:
            if handle.poll() is None:
                pids.add(handle.pid)
            else:
                self.log(f"[SYSTEM] The {label} process this Controller started (PID {handle.pid}) had already "
                         f"exited; stopping whatever serves port {port} instead")
        pid = find_pid_by_port(port)
        if pid:
            pids.add(pid)
        if not pids:
            self.log(f"[SYSTEM] Nothing is serving port {port}; {label} was not running")
            return False
        for pid in sorted(pids):
            try:
                psutil.Process(pid).terminate()
                self.log(f"[SYSTEM] Stopped {label} (PID {pid})")
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                self.log(f"[SYSTEM] Failed to stop {label} (PID {pid}): {e}")
        deadline = time.time() + grace
        while is_port_open(port) and time.time() < deadline:
            time.sleep(0.2)
        if is_port_open(port):
            pid = find_pid_by_port(port)
            if pid:
                try:
                    psutil.Process(pid).kill()
                    self.log(f"[SYSTEM] {label} (PID {pid}) ignored terminate for {grace:.0f}s — killed")
                except Exception as e:
                    self.log(f"[SYSTEM] {label} (PID {pid}) still holds port {port} and could not be killed: {e}")
                    return False
        return True

    def stop_ollama(self):
        handle, self.ollama_proc = self.ollama_proc, None
        self._stop_port("Ollama", 11434, handle)

        # "ollama app.exe" is a separate Windows tray supervisor (launched
        # at login, independent of anything ALEX/the Controller starts) —
        # confirmed live (2026-07-16, Craig: "stop ollama in the controller
        # doesn't seem to work, it just restarts") that it respawns
        # "ollama serve" within ~2 seconds of it dying, making Stop look
        # broken when it was actually working correctly on the process it
        # knew about. Also terminating the tray app here is what makes
        # Stop actually stick — accepted tradeoff: its systray icon closes,
        # and Ollama won't auto-launch again at the next Windows login
        # until it's relaunched manually or the machine reboots.
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").lower() == "ollama app.exe":
                    proc.terminate()
                    self.log(f"[SYSTEM] Stopped Ollama's tray supervisor (PID {proc.info['pid']}) so it can't respawn the server")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                self.log(f"[SYSTEM] Failed to stop Ollama tray supervisor: {e}")

        # Give the just-terminated serve process a moment to actually exit
        # before checking which runners are now truly parentless.
        time.sleep(0.5)
        self.cleanup_orphaned_ollama_runners()

        if self.ollama_log_file:
            self.ollama_log_file.close()
            self.ollama_log_file = None

    # ---------------- ALEX ----------------
    def start_alex(self):
        if self.alex_proc and self.alex_proc.poll() is None:
            return
        self.alex_proc = None

        self.log("[ALEX] Starting...")

        # stdout/stderr go to DEVNULL, not PIPE — nothing reads this pipe
        # (the log tailer watches the log file instead), and an unread
        # PIPE would eventually fill and block the whole process once its
        # buffer fills up.
        # 2026-09-21: her model travels with the process. Everything else in
        # the environment is inherited as before.
        alex_env = os.environ.copy()
        alex_env["ALEX_LLM_MODEL"] = self._selected_model()
        self.log(f"[ALEX] Model: {alex_env['ALEX_LLM_MODEL']}")

        self.alex_proc = subprocess.Popen(
            ["python", "-X", "utf8", "ALEX.py"],
            cwd=ALEX_DIR,
            env=alex_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

    def stop_alex(self):
        handle, self.alex_proc = self.alex_proc, None
        self._stop_port("A.L.E.X", 5000, handle)

    def restart_alex(self):
        """Fast restart — the server process only, not Ollama or this
        Controller. 2026-07-18 (roadmap: "infra-layer hot-reload" — db.py,
        tts_engine.py, ollama_client.py, alex_core.py etc. still need a
        full restart to pick up a code edit). True in-place hot-reload
        was considered and rejected: those files are imported everywhere
        via `from db.db import X`-style names (28 files for db.py alone),
        so reloading the module itself wouldn't actually reach any of its
        callers — and several of them hold live state (alex_core.py's
        active sessions, ollama_client.py's locked_fields/
        pending_profile_changes) that a reload would destroy regardless.
        Craig chose this instead: a fresh process trivially picks up every
        code change correctly, and testing a change doesn't also mean
        waiting for Ollama to warm back up.

        Waits for port 5000 to actually clear before starting the new
        process — .terminate() asks the process to exit but doesn't
        block until it actually has, and starting immediately risks a
        bind failure racing the old process's own shutdown."""
        self.log("[ALEX] Restarting (server process only — Ollama and this Controller stay up)...")

        self.stop_alex()

        deadline = time.time() + 10
        while is_port_open(5000) and time.time() < deadline:
            time.sleep(0.2)

        if is_port_open(5000):
            self.log("[ALEX] ⚠️ Restart aborted — port 5000 still in use 10s after stopping. Check for a stuck process.")
            return

        self.start_alex()
        self.log("[ALEX] Restart complete.")

    # ---------------- ORPHAN PROCESS CHECK ----------------
    def check_for_orphans(self, prompt_if_none=False):
        orphans = find_orphan_processes()

        if not orphans:
            if prompt_if_none:
                QMessageBox.information(self.parent, "No Orphans Found", "No orphaned Ollama/A.L.E.X. processes found.")
            return

        lines = "\n".join(f"- {label} (PID {pid})" for label, pid in orphans)

        confirm = QMessageBox.question(
            self.parent, "Orphaned Processes Found",
            f"Found {len(orphans)} orphaned process(es) not serving their expected port "
            f"(leftover from a restart that wasn't cleanly stopped):\n\n{lines}\n\n"
            f"Terminate them now? Each one can be holding a full model copy in VRAM.\n\n"
            f"Caution: in one observed case, terminating a confirmed orphan A.L.E.X. "
            f"process also took down the real active server, cause unclear — be ready "
            f"to restart A.L.E.X. afterward if that happens.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )

        if confirm != QMessageBox.Yes:
            return

        for label, pid in orphans:
            try:
                psutil.Process(pid).terminate()
                self.log(f"[SYSTEM] Terminated orphaned {label} process (PID {pid})")
            except Exception as e:
                self.log(f"[SYSTEM] Failed to terminate orphaned {label} process (PID {pid}): {e}")
