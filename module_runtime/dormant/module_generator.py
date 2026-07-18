import re
import ast
import builtins
import asyncio
from llm.ollama_client import ollama_manager
from module_runtime.validator import check_safety
from config.logger_config import logger


MAX_CYCLES = 3
GENERATION_TIMEOUT = 60

# deepseek-coder is the code-specialized model — it now does primary
# authorship (Stage 1). qwen2.5 (DEFAULT_MODEL in ollama_client.py) is the
# general chat model and is used only in the supporting continuation/
# refine/repair passes. This was previously inverted (qwen wrote the
# first draft, deepseek only ever continued/fixed it) with no clear
# reason found for the original split.
CODE_MODEL = "deepseek-coder:6.7b"


async def _consume_stream(prompt, **kwargs):
    result = ""
    async for chunk in ollama_manager.generate_stream(prompt, **kwargs):
        result += chunk
    return result


async def _generate_bounded(prompt, timeout=GENERATION_TIMEOUT, **kwargs):
    """Every model call in this file goes through this. Only the old
    Stage 2 call had any protection against a stalled/hung generation
    (manual token-timing loop) — Stage 1, refine_code(), and repair_code()
    had none, so a stalled Ollama response could hang a build (and, since
    builds now run one at a time through a single queue, everything behind
    it) indefinitely. Confirmed live: a build sat at 'syntax failed,
    repairing' with no further log output for 7+ minutes."""
    try:
        return await asyncio.wait_for(_consume_stream(prompt, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        logger.info(f"[ACTION] Generation timed out after {timeout}s, abandoning this attempt")
        return ""


# =========================
# 🚀 MAIN LOOP
# =========================
async def generate_module_code(name, description, model_override=None):

    # logger.info (not print) so this shows up in the Controller's A.L.E.X.
    # tab — a build genuinely takes a couple of minutes (it swaps between
    # two different Ollama models across up to 3 cycles), and there was
    # previously zero visibility into whether it was progressing or stuck.
    logger.info(f"[ACTION] Module generation starting for '{name}'")

    best_code = None
    best_score = -1

    for cycle in range(1, MAX_CYCLES + 1):

        logger.info(f"[ACTION] Module '{name}': generation cycle {cycle}/{MAX_CYCLES}")

        code = await generate_once(name, description)

        if not code:
            logger.info(f"[ACTION] Module '{name}': cycle {cycle} produced no code, skipping")
            continue

        code = clean_pipeline(code)

        score = score_module_quality(code, name)
        logger.info(f"[ACTION] Module '{name}': cycle {cycle} score {score}")

        if score > best_score:
            best_score = score
            best_code = code

        # ✅ Only validate when meaningful
        exec_error = None

        if score >= 6:

            if not is_syntax_valid(code):
                logger.info(f"[ACTION] Module '{name}': syntax failed, repairing")
                code = await repair_code(code)

            code = clean_pipeline(code)

            if code and is_syntax_valid(code):
                valid, _ = validate_module_code(code)

                if valid:
                    # Structural checks (parses, has init/handle) passing
                    # isn't enough on its own — confirmed live that an
                    # empty-but-structurally-plausible result can get this
                    # far. Actually run it before accepting.
                    ok, exec_error = execution_test(code)

                    if ok:
                        logger.info(f"[ACTION] Module '{name}': accepted on cycle {cycle} (execution-tested)")
                        return code

                    logger.info(f"[ACTION] Module '{name}': cycle {cycle} failed execution test: {exec_error}")

        logger.info(f"[ACTION] Module '{name}': refining cycle {cycle}")
        refined = await refine_code(code, name, description, error=exec_error)

        if not refined:
            logger.info(f"[ACTION] Module '{name}': refinement failed on cycle {cycle}")
            continue

        refined = clean_pipeline(refined)

        score = score_module_quality(refined, name)
        logger.info(f"[ACTION] Module '{name}': post-refine score {score}")

        if score > best_score:
            best_score = score
            best_code = refined

    logger.info(f"[ACTION] Module '{name}': no cycle produced a fully valid result, using best attempt")

    if best_code:
        best_code = clean_pipeline(best_code)

        if not is_syntax_valid(best_code):
            logger.info(f"[ACTION] Module '{name}': repairing best attempt")
            best_code = await repair_code(best_code)

        best_code = clean_pipeline(best_code)

        # is_syntax_valid() alone isn't enough here — an empty string (or
        # any fragment with no functions) parses as valid Python syntax,
        # so this used to silently "succeed" with a near-empty module.
        # Confirmed live: a real build reported success and installed a
        # 4-byte file (just blank lines) after all 3 cycles failed to
        # produce a real candidate. Requiring the same execution test the
        # normal path uses means a genuinely empty/broken result correctly
        # falls through to the caller's fallback-template instead.
        if best_code and is_syntax_valid(best_code):
            valid, _ = validate_module_code(best_code)

            if valid:
                ok, exec_error = execution_test(best_code)

                if ok:
                    return best_code

                logger.info(f"[ACTION] Module '{name}': best attempt failed execution test: {exec_error}")
            else:
                logger.info(f"[ACTION] Module '{name}': best attempt has no real init/handle functions")

    return None


# =========================
# 🧱 GENERATION
# =========================
async def generate_once(name, description=None):

    # The user's actual description used to be dropped entirely —
    # generate_module_code() took a `description` argument but never
    # passed it anywhere past itself, so every generation only ever saw
    # the short name slug ("egg_timer"), never what the user actually
    # asked for. Raw completion mode has no instruction channel, so this
    # has to be a comment ahead of the seed (same technique refine_code()
    # already uses for execution-test error feedback below).
    description_comment = ""
    if description:
        description_comment = "\n".join(
            f"# {line}" for line in description.strip().splitlines()
        ) + "\n"

    # "in command" rather than "== 'start'" — real user messages arrive
    # as full sentences ("start the egg timer"), not bare command words,
    # and the model imitates whatever matching style this seed uses.
    # Confirmed live: a module seeded (implicitly, by an earlier version
    # of this scaffold) toward exact-match command handling correctly
    # routed to but then rejected "start the egg timer" as "Unknown
    # command" since it only ever recognized the bare word "start".
    # execution_test() still calls handle("start", {}) directly, and
    # "start" in "start" is still True, so this stays compatible with the
    # existing acceptance check.
    base_prompt = f"""{description_comment}def init():
    return "{name} module ready"

def handle(command, state):
    if state is None:
        state = {{}}

    if "start" in command:
        state["status"] = "started"
        return "Started", state

"""

    print("🧠 [Stage 1] Generating (deepseek-coder)")

    response = await _generate_bounded(base_prompt, model_override=CODE_MODEL, raw_mode=True)

    # Raw completion mode doesn't echo the prompt back — the response is
    # only the continuation, and it inconsistently either re-states "def
    # init()/def handle()" from scratch or just continues the seed's own
    # last line with no signature at all. Always prepending base_prompt
    # guarantees the real def lines exist either way; if the model DID
    # restate them, extract_code() below just finds the first (real)
    # occurrence, which is this one. Confirmed live: without this, a
    # continuation-style response produced a fragment starting mid-`if`
    # with no enclosing function at all.
    code = extract_code(base_prompt + normalize_output(response))

    if not code:
        return None

    code = fix_signature(code)

    # =========================
    # 🧠 SECONDARY CONTINUATION (qwen — supporting pass, not primary author)
    # =========================
    print("🧠 [Stage 2] qwen continuation")

    refine_prompt = f"""{code}

"""

    refined = await _generate_bounded(
        refine_prompt,
        raw_mode=True
    )

    refined = strip_explanations(remove_invalid_tokens(refined))

    if not refined or len(refined.strip()) < 5:
        print("⚠️ qwen continuation empty — using base")
        return code

    return code + "\n" + refined


# =========================
# 🔁 REFINEMENT
# =========================
async def refine_code(code, name, description=None, error=None):

    # raw_mode has no chat template — for real failure feedback (from
    # execution_test, not just the heuristic score) to actually reach the
    # model, it has to be phrased as a code comment ahead of the
    # completion seed, since there's no separate instruction channel here.
    # Same reasoning for `description` — carried through here too so a
    # refinement pass doesn't lose track of what was actually asked for.
    description_comment = ""
    if description:
        description_comment = "\n".join(
            f"# {line}" for line in description.strip().splitlines()
        ) + "\n"

    if error:
        prompt = f"""{description_comment}# The code below was tested and failed: {error}
# Fix that specific problem, then continue the module below.
{code}

"""
    else:
        prompt = f"""{description_comment}{code}

"""

    refined = await _generate_bounded(
        prompt,
        raw_mode=True
    )

    refined = strip_explanations(remove_invalid_tokens(refined))

    if not refined:
        return code

    # Same reasoning as generate_once()'s Stage 1: the model's completion
    # doesn't reliably restate the seed's own def line, so extracting from
    # `refined` alone risks a headless fragment. Combining with the
    # original seed first guarantees a real signature is always present.
    return extract_code(normalize_output(code + "\n" + refined))


# =========================
# 🧠 DOMAIN DETECTION
# =========================
def detect_domain_features(code, target):

    c = code.lower()
    target = target.lower()

    domain_signals = {
        "game": ["board", "move", "turn", "win", "player"],
        "tictactoe": ["board", "x", "o", "win", "grid"],
        "chess": ["board", "move", "piece", "king"],
        "checkers": ["jump", "capture", "king", "board"],
        "calculator": ["add", "subtract", "multiply", "divide"],
        "song": ["lyrics", "verse", "chorus"]
    }

    # Only the 6 hardcoded categories above have a real keyword list —
    # anything else (a timer, a todo list, a unit converter — all real
    # examples from the module-gap classifier's own docstring) used to
    # silently fall back to GAME's keywords ("board"/"move"/"turn"/"win"/
    # "player"), which a timer has no reason to ever contain. That made
    # score_module_quality() hard-reject every module outside these 6
    # names regardless of actual code quality. Returning None here lets
    # the caller skip the domain gate entirely for unknown categories,
    # relying on the generic structure/logic checks already scored above
    # instead of an irrelevant keyword list.
    if target not in domain_signals:
        return None

    keywords = domain_signals[target]

    return sum(1 for k in keywords if k in c)


# =========================
# 🧠 SCORING
# =========================
def score_module_quality(code, target):

    if not code:
        return 0

    c = code.lower()
    score = 0

    # structure
    if "def init" in c: score += 1
    if "def handle" in c: score += 1
    if "return" in c: score += 1
    if "state" in c: score += 1

    # logic
    logic = c.count("if ") + c.count("elif ")
    score += min(3, logic)

    # command parsing
    if "split" in c or "startswith" in c:
        score += 2

    # 🔥 domain enforcement — only for the 6 categories with a real
    # keyword list; detect_domain_features() returns None for anything
    # else, and that's not a failure, just "no domain-specific check
    # available" (see its docstring for why this used to hard-reject
    # every other module type).
    domain_hits = detect_domain_features(code, target)

    if domain_hits is not None:
        if domain_hits == 0:
            print("❌ No domain logic detected")
            return 0

        score += domain_hits

    # penalties
    if "parts" in c and "split" not in c:
        score -= 3

    if "<" in c or ">" in c:
        score -= 3

    return score


# =========================
# 🔥 SYNTAX
# =========================
def is_syntax_valid(code):
    try:
        ast.parse(code)
        return True
    except Exception as e:
        print("❌ Syntax:", e)
        return False


async def repair_code(code):

    prompt = f"""Fix this Python code. Only return valid Python.

{code}
"""

    fixed = await _generate_bounded(
        prompt,
        raw_mode=True
    )

    return extract_code(normalize_output(strip_explanations(fixed)))


# =========================
# VALIDATION
# =========================
def validate_module_code(code):
    try:
        tree = ast.parse(code)

        has_init = any(isinstance(n, ast.FunctionDef) and n.name == "init" for n in tree.body)
        has_handle = any(isinstance(n, ast.FunctionDef) and n.name == "handle" for n in tree.body)

        return has_init and has_handle, None

    except Exception as e:
        return False, str(e)


def _find_undefined_calls(code):
    """Static check for calls to a bare name that's neither a Python
    builtin nor defined/assigned anywhere in this module — catches a
    helper function handle() depends on that never made it into the final
    code (deleted by an earlier cleaning bug, or just never actually
    defined by the model), without needing to guess the module's own
    command syntax to exercise it at runtime the way execution_test()'s
    single 'start' probe does. False negatives (a genuinely undefined
    name we fail to flag) are fine — this is a safety net on top of the
    real execution test, not a replacement for one."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    defined |= set(dir(builtins))

    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            defined.add(n.id)
        if isinstance(n, ast.arg):
            defined.add(n.arg)

    missing = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id not in defined:
                missing.add(n.func.id)

    return sorted(missing)


def execution_test(code):
    """
    Actually RUNS the generated code, rather than just checking it parses
    and has the right function names — that weaker check is exactly what
    let a completely empty module report success (confirmed live: a
    4-byte file with no real code passed every check that existed before
    this). Runs check_safety() first since this executes model output
    that hasn't gone through install_module()'s own safety gate yet.

    Returns (ok, reason) — reason is None on success, otherwise a short,
    concrete description of what actually broke, meant to be fed back
    into a refinement prompt as real signal instead of a guess.
    """
    safe, reason = check_safety(code)
    if not safe:
        return False, f"blocked by sandbox: {reason}"

    undefined = _find_undefined_calls(code)
    if undefined:
        return False, f"calls undefined name(s): {', '.join(undefined)}"

    namespace = {}
    try:
        exec(code, namespace)
    except Exception as e:
        return False, f"code raised an exception on load: {e}"

    handle_fn = namespace.get("handle")
    if not callable(handle_fn):
        return False, "no callable handle() after running the code"

    # Every generated module is seeded from the same scaffold (see
    # generate_once()'s base_prompt below) that always defines a "start"
    # command — a real module should handle it without crashing.
    try:
        result = handle_fn("start", {})
    except Exception as e:
        return False, f"handle('start', {{}}) raised: {e}"

    if not isinstance(result, tuple) or len(result) != 2:
        return False, f"handle() returned {result!r}, expected a (response, state) tuple"

    response, _ = result
    if not response:
        return False, "handle() ran but returned an empty response"

    return True, None


# =========================
# CLEANING PIPELINE
# =========================
def clean_pipeline(code):
    # repair_code()/refine_code() legitimately return None on an empty or
    # timed-out generation — every sub-cleaner needs to tolerate that, but
    # only some already did (confirmed live: fix_signature() didn't,
    # crashing generate_module_code() with a TypeError instead of letting
    # the caller's retry/fallback logic handle a failed attempt normally).
    if not code:
        return code

    code = enforce_single_module_structure(code)
    code = fix_signature(code)
    code = fix_common_runtime_issues(code)
    code = remove_invalid_tokens(code)
    return code


# =========================
# CLEANERS
# =========================
def normalize_output(text):
    text = re.sub(r"```.*?\n", "", text)
    text = text.replace("```", "")
    # rstrip, not strip — this is the actual root cause behind most of
    # tonight's broken generations. .strip() eats ALL leading whitespace
    # as one run, including newlines, which silently deleted the leading
    # indentation of raw-completion responses that continue mid-function
    # (e.g. "\n    if command == ...") — dedenting the first line to
    # column 0 while its own body stayed at 8 spaces, producing exactly
    # the "unindent does not match any outer indentation level" errors
    # seen live. Trailing whitespace is still safe to strip.
    return text.rstrip()


def extract_code(text):
    if not text:
        return None

    start = text.find("def init")
    if start != -1:
        return text[start:]

    if "def " in text:
        return text

    return None


def _is_prose_line(line):
    s = line.strip()
    if not s:
        return False
    # Markdown headers, numbered explanation steps, bold-led bullet lines —
    # the actual shapes seen in real generations. NOT a whitelist of code
    # keywords: that was the previous approach here, and it silently
    # deleted ordinary body lines (plain assignments/expressions like
    # `numbers = command.split(" ")[1:]` don't start with any of the
    # whitelisted keywords) — confirmed live via repeated "unindent does
    # not match any outer indentation level" / "expected an indented
    # block" errors traced back to this function gutting function bodies.
    if re.match(r"^#{2,}\s", s):
        return True
    if re.match(r"^\d+\.\s", s):
        return True
    if s.startswith("**"):
        return True
    return False


def strip_explanations(text):
    if not text:
        return ""

    return "\n".join(l for l in text.splitlines() if not _is_prose_line(l))


def remove_invalid_tokens(text):
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    return text


def fix_signature(code):
    return re.sub(
        r"def\s+handle\s*\(\s*command\s*,\s*state\s*=\s*None\s*\)",
        "def handle(command, state)",
        code
    )


# =========================
# STRUCTURE
# =========================
_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_all_function_blocks(code):
    """Pull every top-level `def NAME(...):` block out by indentation (the
    def line plus every following blank/indented line, stopping at the
    first line back at column 0), in first-seen order, deduplicated by
    name. Returns (blocks_by_name, order).

    Replaces the old init/handle-only extractor, which threw away any
    other top-level function the model defined — confirmed live: a
    generation split calculator logic into separate add()/subtract()
    helper functions called from handle(), and the old extractor deleted
    them, leaving handle() calling names that no longer existed anywhere.
    execution_test() only ever exercises the 'start' command, so that
    kind of breakage was invisible until a real command actually hit it.

    Still drops non-`def` top-level content the same as before (trailing
    prose/markdown, `if __name__ == "__main__":` demo blocks) — first-seen
    dedup means the seed's own init()/handle() (always first, after
    generate_once()/refine_code()'s seed-prepending fix) win over any
    later broken re-definition of the same name."""
    lines = code.splitlines()
    blocks = {}
    order = []

    i = 0
    while i < len(lines):
        m = _DEF_RE.match(lines[i])

        if not m:
            i += 1
            continue

        name = m.group(1)
        start = i
        end = len(lines)

        for j in range(start + 1, len(lines)):
            nxt = lines[j]
            if nxt.strip() == "":
                continue
            if not nxt[0].isspace():
                end = j
                break

        if name not in blocks:
            blocks[name] = "\n".join(lines[start:end]).rstrip()
            order.append(name)

        i = end

    return blocks, order


def enforce_single_module_structure(code):

    if not code:
        return code

    blocks, order = _extract_all_function_blocks(code)

    if "init" not in blocks or "handle" not in blocks:
        return code

    # init/handle first (the module contract the loader expects), then
    # any real helper functions in their original order.
    ordered_names = ["init", "handle"] + [n for n in order if n not in ("init", "handle")]

    return "\n\n".join(blocks[n] for n in ordered_names)


# =========================
# FIXES
# =========================
def fix_common_runtime_issues(code):

    if not code:
        return code

    if "startswith(\"move\")" in code and "split" not in code:
        code = code.replace(
            'elif command.startswith("move"):',
            '''elif command.startswith("move"):
        parts = command.split()
        if len(parts) < 3:
            return "Invalid move", state
        from_value = parts[1]
        to_value = parts[2]'''
        )

    return code