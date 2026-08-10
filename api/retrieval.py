"""FAISS retrieval over the site knowledge base.

Replaces shipping the whole 38k-token KB in the system prompt. That only ever
made sense under prompt caching; Ollama has none, so every turn paid for the
entire KB in full.

Model and index load once, lazily, behind lru_cache -- warmed in main.py's
lifespan so the first visitor does not eat the ~2s cold load.

Retrieval failures are non-fatal by design: a missing index degrades the agent
to "cannot look up specifics" instead of 500ing the turn. The guardrails still
stop it inventing an answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from api.config import get_settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger("retrieval")
SETTINGS = get_settings()

# At most this many chunks from any one page, so a question spanning two
# services cannot have every slot taken by whichever service the site happens
# to have written more pages about.
MAX_PER_PATH = 2
# Fetch this multiple of k before capping, so the cap has something to promote.
OVERFETCH = 5


@dataclass(frozen=True)
class Hit:
    path: str
    title: str
    text: str
    score: float


@lru_cache(maxsize=1)
def _model() -> "SentenceTransformer":
    from sentence_transformers import SentenceTransformer

    log.info("loading embedding model %s", SETTINGS.embed_model)
    try:
        # Cache only. Without this the loader revalidates against
        # huggingface.co on every startup -- six HTTP round trips before the
        # app can serve, and a hang if the network is slow rather than down.
        # The weights are already local; there is nothing to fetch.
        return SentenceTransformer(SETTINGS.embed_model, local_files_only=True)
    except Exception:
        # First run on a clean machine (or in a Docker build layer) has no
        # cache yet, so fall back to downloading it once.
        log.info("%s not cached -- downloading once", SETTINGS.embed_model)
        return SentenceTransformer(SETTINGS.embed_model)


@lru_cache(maxsize=1)
def _index() -> tuple[object, list[dict]]:
    """(faiss index, chunk metadata). Row i of the index is chunks[i]."""
    import faiss

    if not SETTINGS.faiss_file.exists() or not SETTINGS.chunks_file.exists():
        raise FileNotFoundError(
            f"{SETTINGS.faiss_file.name} or {SETTINGS.chunks_file.name} missing -- "
            "run: python kb/chunk.py && python kb/embed.py"
        )

    index = faiss.read_index(str(SETTINGS.faiss_file))
    with SETTINGS.chunks_file.open(encoding="utf-8") as fh:
        chunks = [json.loads(line) for line in fh if line.strip()]

    # A stale index against fresh chunks silently returns the wrong text for
    # every hit -- the worst possible failure, because it looks like it worked.
    if index.ntotal != len(chunks):
        raise RuntimeError(
            f"index has {index.ntotal} vectors but chunks.jsonl has {len(chunks)} "
            "rows -- they are out of sync. Rebuild: python kb/embed.py --rechunk"
        )
    log.info("loaded %d chunks at dim %d", index.ntotal, index.d)
    return index, chunks


def warm() -> bool:
    """Preload model + index. Returns False if retrieval is unavailable."""
    try:
        _model()
        _index()
        return True
    except Exception as exc:
        log.warning("retrieval unavailable: %s", exc)
        return False


def is_ready() -> bool:
    try:
        _index()
        return True
    except Exception:
        return False


def chunk_count() -> int:
    try:
        return _index()[0].ntotal
    except Exception:
        return 0


def indexed_paths() -> frozenset[str]:
    """Every path present in the index. Used to catch tests that assert on a
    page the crawl never produced -- a broken case, not a broken index."""
    try:
        return frozenset(c["path"] for c in _index()[1])
    except Exception:
        return frozenset()


def search(query: str, k: int | None = None) -> list[Hit]:
    """Top-k chunks above the score floor, capped per source page.

    Never raises -- a retrieval failure returns [] and the turn continues
    without excerpts rather than 500ing.

    The per-page cap is why this overfetches. Without it "do you do local
    seo?" returned the same page six times -- six slots, one page's worth of
    information. The cap spends them on distinct pages instead.

    It does not fix compound questions. "do you do seo and web design?" still
    returns four SEO pages: /development/website-development sits at rank 11
    (0.526 against a 0.561 top score), losing on lexical dominance of "seo",
    and the slots fill before reaching it. Breadth is the service index's job
    in api/kb.py, not retrieval's -- the model sees every service page name in
    the system prompt regardless of what comes back here.
    """
    query = (query or "").strip()
    if not query:
        return []

    k = k or SETTINGS.retrieval_top_k
    try:
        index, chunks = _index()
        vector = _model().encode(
            [query], normalize_embeddings=True, convert_to_numpy=True
        )
        # Overfetch so the cap has alternatives to promote, bounded by corpus size.
        scores, ids = index.search(
            vector.astype("float32"), min(k * OVERFETCH, index.ntotal)
        )
    except Exception:
        log.exception("retrieval failed for %r", query[:80])
        return []

    hits: list[Hit] = []
    per_path: dict[str, int] = {}
    for score, idx in zip(scores[0], ids[0]):
        # faiss returns -1 to pad when fewer than k vectors exist.
        if idx < 0 or score < SETTINGS.retrieval_min_score:
            continue
        chunk = chunks[idx]
        path = chunk["path"]
        if per_path.get(path, 0) >= MAX_PER_PATH:
            continue
        per_path[path] = per_path.get(path, 0) + 1
        hits.append(
            Hit(path=path, title=chunk["title"], text=chunk["text"], score=float(score))
        )
        if len(hits) >= k:
            break
    return hits


def as_context(hits: list[Hit]) -> str:
    """Render hits for the prompt, grouped by source page.

    Each block is labelled with its path so the model can cite where a fact
    came from, and so a visitor asking "where do I read more" gets a real URL
    rather than an invented one.
    """
    if not hits:
        return ""
    lines = [
        "[Knowledge base excerpts for this question. These are your only source "
        "of truth about services. If the answer is not here, say you will have "
        "a specialist confirm it -- do not fill the gap from general knowledge.]",
        "",
    ]
    for hit in hits:
        lines.append(f"### {hit.title} ({hit.path})")
        lines.append(hit.text)
        lines.append("")
    return "\n".join(lines).strip()


def demo() -> None:
    assert is_ready(), "index missing -- run kb/chunk.py then kb/embed.py"

    hits = search("do you do local seo?")
    assert hits, "no hits for a question the site definitely answers"
    assert all(0.0 <= h.score <= 1.0001 for h in hits), "scores must be cosines"
    assert hits == sorted(hits, key=lambda h: -h.score), "hits must be ranked"

    # Normalised vectors + IndexFlatIP means a chunk matched against itself
    # scores ~1.0. If this drifts, normalisation was lost at build time.
    _, chunks = _index()
    exact = search(f"{chunks[0]['title']} -- {chunks[0]['path']}\n{chunks[0]['text']}", k=1)
    assert exact and exact[0].score > 0.85, f"self-match too low: {exact[0].score if exact else 0}"

    assert search("") == [], "empty query must return nothing, not top-k noise"

    # The cap must hold, and must buy breadth on a single-topic question that
    # would otherwise return one page six times over.
    from collections import Counter

    counts = Counter(h.path for h in search("do you do local seo?"))
    assert max(counts.values()) <= MAX_PER_PATH, f"cap breached: {counts}"
    assert len(counts) >= 3, f"expected >=3 distinct pages, got {dict(counts)}"

    context = as_context(hits)
    assert "only source" in context, "context must carry the grounding instruction"
    assert hits[0].path in context, "context must cite the source path"

    print(f"OK  {chunk_count()} chunks indexed")
    print(f"OK  self-match {exact[0].score:.3f}, ranked, cosine-bounded")
    print(f"OK  '{'do you do local seo?'}' -> {[h.path for h in hits]}")
    print(f"OK  context {len(context):,} chars (~{len(context) // 4} tokens)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo()
