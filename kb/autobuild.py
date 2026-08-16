"""Derive the KB artifacts on demand from whatever is already on disk.

    python kb/autobuild.py          # build what is missing or stale
    python kb/autobuild.py --force  # rebuild the whole chain regardless

Only kb/raw/*.json is committed -- .gitignore drops site_kb.md, sitemap.json
and kb/index/ because they are derivatives. So a fresh clone, or this project
copied to another machine, starts with the scraped pages and nothing built from
them: no service index, no navigate_to allowlist, no vector store. The agent
still talks, but every answer is ungrounded, and the only signal was a WARNING
in the startup log telling the operator to run two commands by hand.

ensure() closes that gap by rebuilding exactly the missing or stale links of:

    kb/raw/*.json -> site_kb.md + sitemap.json -> chunks.jsonl -> site.faiss

All of it offline -- kb/raw/ is the cached scrape, so no page is refetched. That
is the one thing this cannot do: replace kb/kb_build.py, which reads the live
site. If kb/raw/ is empty there is nothing to derive and ensure() raises.

Staleness is by mtime, and a rebuilt link forces every link after it. Dropping
a new company page into kb/raw/ and restarting is therefore enough to get it
into the vector store; deleting one is caught by page count, which mtime cannot
see.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.config import get_settings  # noqa: E402

log = logging.getLogger("kb.autobuild")
SETTINGS = get_settings()


@dataclass
class Report:
    """What ensure() actually rebuilt. Empty stages means everything was current."""

    stages: list[str] = field(default_factory=list)
    pages: int = 0
    chunks: int = 0


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def _newest_raw() -> float:
    return max(
        (p.stat().st_mtime for p in SETTINGS.raw_dir.glob("*.json")), default=0.0
    )


def _chunk_rows() -> int:
    with SETTINGS.chunks_file.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _raw_pages() -> int:
    from kb import kb_build

    return len(kb_build.load_cached_pages())


def _sitemap_pages() -> int:
    try:
        data = json.loads(SETTINGS.sitemap_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return -1
    return int(data.get("page_count", -1))


def _kb_stale() -> bool:
    if not SETTINGS.kb_file.exists() or not SETTINGS.sitemap_file.exists():
        return True
    # Compare against the older artifact: both come from one emit_artifacts()
    # call, so if either predates a raw page, neither describes it.
    built = min(_mtime(SETTINGS.kb_file), _mtime(SETTINGS.sitemap_file))
    if _newest_raw() > built:
        return True
    # mtime cannot see a *deleted* page -- the artifacts stay newer than every
    # remaining file, so a removed page would keep answering questions and keep
    # its slot in the navigate_to allowlist. Counting catches that.
    #
    # Strictly fewer, not "different": strip_boilerplate() can empty a page and
    # drop it from the sitemap, and treating that as stale would rebuild on
    # every single startup, forever.
    return _raw_pages() < _sitemap_pages()


def _chunks_stale() -> bool:
    if not SETTINGS.chunks_file.exists():
        return True
    return _mtime(SETTINGS.kb_file) > _mtime(SETTINGS.chunks_file)


def _index_stale() -> bool:
    if not SETTINGS.faiss_file.exists():
        return True
    if _mtime(SETTINGS.chunks_file) > _mtime(SETTINGS.faiss_file):
        return True
    # Row i of the index must be chunks[i]. A count mismatch means it is not,
    # so every hit would return the wrong excerpt -- rebuild rather than let
    # api/retrieval.py refuse to load and leave the agent ungrounded.
    try:
        import faiss

        return faiss.read_index(str(SETTINGS.faiss_file)).ntotal != _chunk_rows()
    except Exception as exc:
        log.warning("cannot read %s (%s) -- rebuilding", SETTINGS.faiss_file.name, exc)
        return True


def _clear_kb_caches() -> None:
    """Drop api/kb.py's memoised sitemap.

    It caches for the life of the process, and on a cold start it can already
    have cached the *missing* one -- which is a system prompt with no service
    index and a navigate_to allowlist that refuses every real page.
    """
    from api import kb

    for cached in (kb._sitemap, kb.allowed_paths, kb.service_index, kb.system_prompt):
        cached.cache_clear()


def _build_kb(report: Report) -> None:
    from kb import kb_build

    pages = kb_build.load_cached_pages()
    if not pages:
        raise FileNotFoundError(
            f"no scraped pages in {SETTINGS.raw_dir} -- nothing to build the KB "
            "from. Run: python kb/kb_build.py  (needs the live site)"
        )
    log.info("building site_kb.md + sitemap.json from %d cached pages", len(pages))
    summary = kb_build.emit_artifacts(pages)
    report.pages = summary["canonical"]
    report.stages += ["site_kb.md", "sitemap.json"]
    _clear_kb_caches()


def _build_chunks(report: Report, model) -> None:
    from kb import chunk as chunk_module

    chunks = chunk_module.build()
    if not chunks:
        raise RuntimeError(
            f"{SETTINGS.kb_file.name} produced no chunks -- is it empty? "
            "Rebuild it with: python kb/kb_build.py --offline"
        )
    chunk_module.verify(chunks, model=model)
    chunk_module.write(chunks)
    log.info("chunked into %d passages", len(chunks))
    report.chunks = len(chunks)
    report.stages.append("chunks.jsonl")


def _build_index(report: Report, model) -> None:
    from kb import embed

    chunks = embed.load_chunks()
    log.info("embedding %d chunks with %s -- one-off, ~1min on cpu", len(chunks), SETTINGS.embed_model)
    index, dim = embed.build(chunks, model=model)
    embed.save(index, expected=len(chunks))
    log.info("wrote %s (%d vectors at dim %d)", SETTINGS.faiss_file.name, len(chunks), dim)
    report.chunks = len(chunks)
    report.stages.append("site.faiss")


def ensure(model=None, force: bool = False) -> Report:
    """Build every missing or stale artifact. Returns what it built.

    Raises FileNotFoundError or RuntimeError if the chain cannot be completed
    -- callers decide whether that is fatal (kb/embed.py exits non-zero) or a
    degraded mode (api/retrieval.py keeps serving without excerpts).

    `model` reuses an already-loaded SentenceTransformer. api/retrieval.py
    passes its own so the API does not hold two copies of the encoder.
    """
    report = Report()

    rebuild_kb = force or _kb_stale()
    # A rebuilt link invalidates every link after it: fresh chunks against an
    # old index is precisely the silent-wrong-excerpt failure above.
    rebuild_chunks = rebuild_kb or _chunks_stale()
    rebuild_index = rebuild_chunks or _index_stale()

    if not rebuild_index:
        return report

    if rebuild_kb:
        _build_kb(report)
    if rebuild_chunks:
        _build_chunks(report, model)
    if rebuild_index:
        _build_index(report, model)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true", help="rebuild the whole chain regardless"
    )
    args = parser.parse_args()

    try:
        report = ensure(force=args.force)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"  ! {exc}")
        return 1

    if report.stages:
        print(f"  built       {', '.join(report.stages)}")
        print(f"  pages       {report.pages or 'unchanged'}")
        print(f"  chunks      {report.chunks}")
    else:
        print("  up to date  site_kb.md, sitemap.json, chunks.jsonl, site.faiss all current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
