"""Retrieval quality gate: does a real question surface the right page?

    python api/test_retrieval.py

No pytest -- one file, asserts, runs anywhere. The cases below are phrased the
way a visitor types, lowercase and unpunctuated included, because that is the
input distribution that matters. If a rewrite of chunk.py or a model swap
regresses retrieval, this fails loudly instead of the agent quietly answering
from general knowledge.

Each case names the path that MUST appear in top-k. A case asserting on a page
that no longer exists is a broken test, not a broken index, so the paths are
verified against the KB before any search runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import retrieval  # noqa: E402

# (question, path that must be retrieved)
#
# Paths are taken from kb/sitemap.json, not from the requirements PDF. The PDF
# names services the site has no page for -- there is no /web-design, no /ppc,
# no /case-studies and no /industries/*. Asserting on those would test the
# brochure rather than the corpus.
#
# Cases must also assert on wording the page actually uses. Two dropped for
# that reason, both worth knowing about:
#
#   "can you fix my site speed" -> /seo/technical-seo
#       The page never says speed, page speed, or core web vitals. It says
#       "Maximize Your Website's Performance". Nothing to retrieve on.
#   "who are you people" -> /about
#       Too idiomatic. /about opens "ABOUT US ... full-stack service digital
#       marketing agency"; MiniLM does not bridge that gap on 384 dims.
#
# Both are corpus limits, not retrieval bugs. Fix them by improving the pages,
# not by loosening this gate.
CASES: list[tuple[str, str]] = [
    ("what is local seo", "/seo/local-seo"),
    ("how do i rank higher on google maps", "/seo/local-seo"),
    ("my website has technical seo problems", "/seo/technical-seo"),
    ("what is your link building process", "/seo/link-building"),
    ("can you audit my seo", "/seo/seo-audits"),
    ("do you build websites", "/development/website-development"),
    ("i want to sell online with shopify", "/development/shopify-website"),
    ("can you make a mobile app", "/development/mobile-apps"),
    ("i need a logo designed", "/designing/graphic-design"),
    ("i need help with google ads", "/advertisements/google-ads"),
    ("do you run facebook and instagram ads", "/advertisements/meta-ads"),
    ("do you manage social media", "/marketing/social-media-marketing"),
    ("can you send marketing emails for us", "/marketing/email-marketing"),
    ("do you write website copy", "/content-writing/copywriting"),
    ("tell me about your company", "/about"),
    ("how do i get in touch", "/contact"),
    ("are you hiring", "/career"),
]


def check_paths_exist() -> list[str]:
    """A case pointing at a page the crawl never produced fails for the wrong
    reason -- it would look like a retrieval regression."""
    known = retrieval.indexed_paths()
    return sorted({p for _, p in CASES if p not in known})


def main() -> int:
    if not retrieval.warm():
        print("FAIL: no index. Run: python kb/chunk.py && python kb/embed.py")
        return 1

    missing = check_paths_exist()
    if missing:
        print("BROKEN TEST -- these paths are not in the KB at all:")
        for p in missing:
            print(f"  {p}")
        print("\nFix the cases (or the KB), then re-run.")
        return 1

    k = 6
    failures: list[tuple[str, str, list[str]]] = []
    top1 = 0

    print(f"{len(CASES)} cases, k={k}, {retrieval.chunk_count()} chunks\n")
    for question, want in CASES:
        paths = [h.path for h in retrieval.search(question, k=k)]
        hit = want in paths
        if hit and paths[0] == want:
            top1 += 1
        mark = "ok  " if hit else "MISS"
        rank = f"#{paths.index(want) + 1}" if hit else "--"
        print(f"  {mark} {rank:<3} {question:<38} -> {want}")
        if not hit:
            failures.append((question, want, paths))

    total = len(CASES)
    print(f"\nrecall@{k}: {total - len(failures)}/{total}   top-1: {top1}/{total}")

    if failures:
        print("\nMisses in detail:")
        for question, want, paths in failures:
            print(f"\n  {question!r}\n    wanted: {want}\n    got:")
            for i, p in enumerate(paths, 1):
                print(f"      {i}. {p}")

    # Every case must hit. A miss means the visitor asked a plain question about
    # a real service and got nothing -- there is no acceptable pass rate below
    # 100% for a set this small and this obvious.
    assert not failures, f"{len(failures)}/{total} cases failed to retrieve their page"
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
