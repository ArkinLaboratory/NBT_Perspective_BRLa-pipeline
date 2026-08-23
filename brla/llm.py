"""LLM access via the CBorg LiteLLM proxy (OpenAI-compatible).

One client, many models: Claude / Gemini / GPT are all reachable through the
same base_url; the model string in config.yaml decides which provider serves
the call. Token usage is accumulated per module for cost visibility.
"""
import json
import re
import time
from collections import defaultdict

from openai import OpenAI

from .config import env

# module_name -> {"input": int, "output": int, "calls": int}
USAGE = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})

# Tavily: {"queries": int, "credits": int}
TAVILY_USAGE = {"queries": 0, "credits": 0}


def record_tavily_query(search_depth: str = "basic"):
    TAVILY_USAGE["queries"] += 1
    TAVILY_USAGE["credits"] += 2 if search_depth == "advanced" else 1


def get_client(cfg: dict) -> OpenAI:
    return OpenAI(
        base_url=env(cfg["cborg"], "base_url_env"),
        api_key=env(cfg["cborg"], "api_key_env"),
    )


def list_models(cfg: dict) -> list[str]:
    client = get_client(cfg)
    return sorted(m.id for m in client.models.list())


def chat(
    client: OpenAI,
    model: str,
    system: str,
    user: str,
    json_mode: bool = False,
    temperature: float = 0.0,
    module: str = "misc",
    max_retries: int = 3,
    *,
    collect: list[dict] | None = None,
) -> str:
    """Single-turn chat with retry/backoff. Returns the raw text content.

    If `collect` is given, one dict per HTTP round trip (including failed
    ones) is appended to it. This is an opt-in list rather than a module-level
    log so each caller (e.g. a future per-page thread-pool worker in M5) owns
    its own list, making it thread-safe by construction with no rework needed
    when M5 gets parallelized.
    """
    kwargs = {}
    if json_mode:
        # Some proxied models may reject response_format; we fall back below.
        kwargs["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(max_retries):
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **kwargs,
            )
            elapsed = time.perf_counter() - t0
            prompt_tokens = 0
            completion_tokens = 0
            if resp.usage:
                prompt_tokens = resp.usage.prompt_tokens or 0
                completion_tokens = resp.usage.completion_tokens or 0
                USAGE[module]["input"] += prompt_tokens
                USAGE[module]["output"] += completion_tokens
            USAGE[module]["calls"] += 1
            if collect is not None:
                collect.append({
                    "model": model, "module": module,
                    "elapsed_s": round(elapsed, 3),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "status": "ok", "parsed": None,
                })
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - proxy errors vary widely
            elapsed = time.perf_counter() - t0
            if collect is not None:
                collect.append({
                    "model": model, "module": module,
                    "elapsed_s": round(elapsed, 3),
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "status": f"api_error:{type(e).__name__}", "parsed": None,
                })
            last_err = e
            msg = str(e).lower()
            if json_mode and ("response_format" in msg or "json_object" in msg):
                # Model/proxy doesn't support JSON mode: enforce via prompt.
                kwargs.pop("response_format", None)
                system = system + "\nRespond with a single valid JSON object and nothing else."
                continue
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_err}")


# ```json ... ``` or bare ``` ... ``` blocks, non-greedy so the first block wins.
_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _match_braces(text: str) -> str | None:
    """Return the substring from the first `{` to its matching `}`, or None.

    Braces inside JSON string literals (including escaped quotes) are ignored,
    so `{"a": "}"}` is handled correctly.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(raw: str) -> str:
    """Best-effort extraction of a JSON payload from a raw LLM response.

    Tries, in order: the whole response, the contents of any markdown code
    fence, and a brace-matched slice of the response (and of each fence body).
    The first candidate that parses is returned. If nothing parses, the most
    promising candidate is returned anyway so the caller's `json.loads` raises
    and the existing repair logic can take over.
    """
    stripped = raw.strip()
    candidates = [stripped]
    for block in _FENCE_RE.findall(raw):
        block = block.strip()
        if block:
            candidates.append(block)
            matched = _match_braces(block)
            if matched:
                candidates.append(matched)
    matched = _match_braces(stripped)
    if matched:
        candidates.append(matched)

    for candidate in candidates:
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    # Nothing parsed: hand back the narrowest candidate for a useful error.
    return candidates[-1]


def chat_json(
    client, model, system, user, module="misc", max_repair=1,
    *, collect: list[dict] | None = None,
) -> dict:
    """chat() + parse; on parse failure, one repair round-trip."""
    raw = chat(client, model, system, user, json_mode=True, module=module, collect=collect)
    for _ in range(max_repair + 1):
        try:
            result = json.loads(_extract_json(raw))
            if collect is not None:
                collect[-1]["parsed"] = True
            return result
        except json.JSONDecodeError as e:
            if collect is not None:
                collect[-1]["parsed"] = False
            raw = chat(
                client, model, system,
                f"Your previous output was not valid JSON ({e}). "
                f"Reproduce it as a single valid JSON object:\n\n{raw}",
                json_mode=True, module=module, collect=collect,
            )
    raise ValueError(f"Unparseable JSON from {model}")


def get_usage() -> dict:
    """Return a snapshot of accumulated usage for cross-process aggregation."""
    return {
        "llm": {k: dict(v) for k, v in USAGE.items()},
        "tavily": dict(TAVILY_USAGE),
    }


def merge_usage(snapshots: list[dict]) -> None:
    """Merge worker usage snapshots into this process's accumulators."""
    for snap in snapshots:
        for mod, u in snap.get("llm", {}).items():
            USAGE[mod]["input"] += u.get("input", 0)
            USAGE[mod]["output"] += u.get("output", 0)
            USAGE[mod]["calls"] += u.get("calls", 0)
        tav = snap.get("tavily", {})
        TAVILY_USAGE["queries"] += tav.get("queries", 0)
        TAVILY_USAGE["credits"] += tav.get("credits", 0)


def usage_report() -> str:
    lines = ["Token usage by module:"]
    for mod, u in sorted(USAGE.items()):
        lines.append(
            f"  {mod:<14} calls={u['calls']:<5} in={u['input']:,} out={u['output']:,}"
        )
    if TAVILY_USAGE["queries"] > 0:
        lines.append(
            f"\nTavily usage: {TAVILY_USAGE['queries']} queries, "
            f"~{TAVILY_USAGE['credits']} credits"
        )
    return "\n".join(lines) if len(lines) > 1 else "No LLM calls made this run."
