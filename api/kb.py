"""System prompt assembly.

Guardrails, persona, then a *service index* -- the name and path of every page
on the site, not the pages themselves. The body text now arrives per turn from
api/retrieval.py.

Why the index still exists after adding retrieval: top-k answers "tell me about
local SEO" well and "what do you offer?" badly, because six chunks cannot
represent fifty pages. The index costs ~600 tokens and closes that hole
permanently. It is the standard RAG breadth gap; retrieval alone does not fill
it -- see api/retrieval.py's search() docstring for the measured case.

Everything here is static for the life of the process, so it stays a stable
prefix. Per-visitor and per-turn content goes in user messages, never in here.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from api.config import get_settings

log = logging.getLogger("kb")
SETTINGS = get_settings()
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_MISSING_KB = (
    "The site index is missing. Answer only that you cannot look up service "
    "details right now and offer to connect the visitor with a specialist. Do "
    "not answer service questions from general knowledge."
)


def _read(path: Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        # Loud, because the failure is otherwise invisible: a missing
        # guardrails.md does not break anything, it just silently ships a
        # system prompt with no rules in it.
        log.warning("prompt file missing: %s -- system prompt is incomplete", path)
        return fallback


@lru_cache(maxsize=1)
def _sitemap() -> dict:
    try:
        return json.loads(SETTINGS.sitemap_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def allowed_paths() -> frozenset[str]:
    """Paths that exist on the real site.

    Checked before any path the model mentions reaches the visitor -- the model
    inventing /pricing/ is the known failure mode.
    """
    data = _sitemap()
    paths = {entry["path"] for entry in data.get("pages", []) if entry.get("path")}
    paths |= set(data.get("aliases", {}))
    return frozenset(paths)


@lru_cache(maxsize=1)
def service_index() -> str:
    """One line per page: `- Title (/path)`.

    Aliases are skipped -- they are the same page at a second URL, and listing
    both teaches the model the site is twice as big as it is.
    """
    pages = [
        entry
        for entry in _sitemap().get("pages", [])
        if entry.get("path") and not entry.get("alias_of")
    ]
    if not pages:
        return ""
    lines = [f"- {entry.get('title') or entry['path']} ({entry['path']})" for entry in pages]
    return "\n".join(sorted(lines))


@lru_cache(maxsize=1)
def system_prompt() -> str:
    index = service_index()
    parts = [
        _read(PROMPT_DIR / "guardrails.md"),
        _read(PROMPT_DIR / "persona.md"),
        "# Pages on this website",
        "",
        "The complete list. Use it to answer what the company offers and to "
        "name a real page when you point somewhere.",
        "",
        "You get the actual CONTENT of the relevant pages with each visitor "
        "message, under a knowledge-base heading. Answer from that content. "
        "This list tells you what exists -- not what any page says. Never "
        "describe a service using only its title from this list, and never "
        "mention a path that is not on it.",
        "",
        index or _MISSING_KB,
    ]
    return "\n\n".join(p for p in parts if p)


def demo() -> None:
    prompt = system_prompt()
    assert "Never state a price" in prompt, "guardrails missing"
    assert "Systematic IT Solutions" in prompt, "persona missing"
    assert "# Pages on this website" in prompt, "service index header missing"

    guard_at = prompt.index("Never state a price")
    index_at = prompt.index("# Pages on this website")
    assert guard_at < index_at, "guardrails must precede the index"

    paths = allowed_paths()
    assert "/" in paths, "homepage missing from allowlist"
    assert "/pricing" not in paths, "/pricing must never be allowlisted"
    assert "/seo/local-seo" in paths, "known service page missing"

    index = service_index()
    assert "/seo/local-seo" in index, "local SEO missing from the index"
    assert "/development/shopify-website" in index, "shopify missing from the index"
    assert "/pricing" not in index, "/pricing must never appear in the index"

    # The whole point of the change: the prompt must no longer carry the KB.
    tokens = len(prompt) // 4
    assert tokens < 8_000, f"prompt is {tokens} tokens -- KB leaked back in?"

    print(f"OK  system prompt {len(prompt):,} chars (~{tokens:,} tokens)")
    print(f"OK  service index {len(index.splitlines())} pages listed")
    print(f"OK  allowlist {len(paths)} paths, /pricing absent")


if __name__ == "__main__":
    demo()
