"""Optional LLM disambiguation via Ollama — a salvage step for the acquirer.

The deterministic ranker (indexer.rank) is deliberately strict: recall×precision, required
multicart numbers, video-release rejection. That correctly refuses garbage, but it also
turns away the occasional real release whose title carries enough extra words to sink its
score below the floor. When enabled, this asks a local Ollama model to look at those
below-floor candidates and pick the one that is genuinely the same game — or none.

Design principles:
- OFF by default (`ROMCOM_LLM_ENABLED`). When off, `choose()` is never called.
- Fail OPEN: any error (Ollama down, bad JSON, timeout) returns None = "no confident
  match", so the pipeline just falls through to the direct sources as before. The LLM can
  only ever ADD a match the ranker missed; it can't break acquisition.
- Verdicts are cached (searchcache) keyed on the exact candidate set, so the same
  ambiguous item isn't re-asked every sweep.

Provider is Ollama (not Anthropic) per project choice; default model
`deepseek-v4.1-flash:cloud`. Uses the plain HTTP API via requests — no extra dependency.
"""
import json
import requests
from .config import settings

_SCHEMA = {"type": "object",
           "properties": {"index": {"type": "integer"}},
           "required": ["index"]}

_SYSTEM = (
    "You match a wanted retro game to candidate download release names. The candidate must "
    "be the SAME game — region, language, and revision do not matter, but it must not be a "
    "different game, a demo/prototype when the retail was asked for, a hack, or unrelated "
    "media. Reply with the 0-based index of the single best matching candidate, or -1 if "
    "none is the same game.")


def enabled():
    return bool(settings()["llm_enabled"])


def test():
    """Connectivity check for the Settings tab — list Ollama's models."""
    s = settings()
    r = requests.get(f"{s['llm_base']}/api/tags", timeout=s["llm_timeout"])
    r.raise_for_status()
    return True


def _ask(title, system, cand_titles):
    s = settings()
    lines = "\n".join(f"[{i}] {t}" for i, t in enumerate(cand_titles))
    prompt = (f"Wanted game: {title!r}" + (f" (system: {system})" if system else "") +
              f"\nCandidates:\n{lines}\n\nWhich candidate index is the same game? "
              "Respond as JSON {\"index\": N} with N the best index, or -1 for none.")
    r = requests.post(f"{s['llm_base']}/api/chat", timeout=s["llm_timeout"], json={
        "model": s["llm_model"], "stream": False, "format": _SCHEMA,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt}]})
    r.raise_for_status()
    content = (r.json().get("message") or {}).get("content") or "{}"
    return int(json.loads(content).get("index", -1))


def choose(title, system, candidates):
    """Return the candidate dict the model judges the same game, or None. `candidates` is a
    list of result dicts (each with a 'title'). Cached per exact candidate set."""
    if not enabled() or not candidates:
        return None
    cand_titles = [c.get("title", "") for c in candidates]
    from . import searchcache
    key = f"{title}|{system or ''}|" + "".join(cand_titles)

    def _fetch():
        try:
            return {"index": _ask(title, system, cand_titles)}
        except Exception:
            return {"index": -1}  # fail open — cache the miss briefly too

    idx = searchcache.cached("llm", key, _fetch).get("index", -1)
    return candidates[idx] if isinstance(idx, int) and 0 <= idx < len(candidates) else None
