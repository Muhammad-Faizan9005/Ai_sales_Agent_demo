"""Build the agent's knowledge base from the real Systematic IT site.

    python kb/kb_build.py            # fetch (cached) + emit artifacts
    python kb/kb_build.py --dry-run  # list URLs only, no fetching
    python kb/kb_build.py --refresh  # ignore cache, re-fetch everything
    python kb/kb_build.py --offline  # rebuild from kb/raw/ only, no network

Two artifacts, two consumers:
  site_kb.md    -> the cached system-prompt payload (requirement 6)
  sitemap.json  -> the navigate_to allowlist, so a page the agent invents is
                   refused before it can send a visitor to a 404 (phase 4)

Both are gitignored derivatives of kb/raw/, which is what --offline exists for:
a clone has the scraped pages but neither artifact. kb/autobuild.py calls the
same code path on API startup.

Discovery reads the published sitemaps rather than crawling blind, so the URL
set is deterministic and every fetch is a page we already know we want.

No scraping API: the site is server-rendered WordPress, so a plain GET returns
the finished document. Firecrawl was the original plan and is not needed --
kb/extract.py reduces the HTML with the stdlib parser instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.config import get_settings  # noqa: E402
from kb.extract import html_to_markdown, meta_description, page_title  # noqa: E402

SETTINGS = get_settings()

# Sitemaps are remote, untrusted XML and we need exactly one tag from them, so
# we match <loc> directly instead of handing the document to an XML parser
# (stdlib ElementTree is open to XXE and billion-laughs).
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

# Live on the real site but not real content. These would otherwise land in the
# KB and, worse, in the navigate_to allowlist.
JUNK_SLUGS = {"12745178-2", "sdfjhsdui"}

# Boilerplate that repeats on all 39 pages. Stripping it is what keeps the KB
# inside the context budget.
CHROME_PATTERNS = [
    re.compile(r"^\s*\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*$", re.M),  # logo links
    re.compile(r"^\s*(?:Menu|Toggle Menu|Search|Close|Skip to content)\s*$", re.M | re.I),
    re.compile(r"^\s*Copyright\s*(?:©|&copy;).*$", re.M | re.I),
    re.compile(r"^\s*All Rights Reserved.*$", re.M | re.I),
    re.compile(r"\n{3,}"),
]


def fetch_sitemap_urls() -> list[str]:
    """Walk sitemap_index.xml -> child sitemaps -> page URLs."""
    base = SETTINGS.site_base_url.rstrip("/")
    urls: list[str] = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        index = client.get(f"{base}/sitemap_index.xml")
        index.raise_for_status()
        children = LOC_RE.findall(index.text)
        for child in children:
            # Author/category sitemaps describe listings, not sellable content.
            if any(skip in child for skip in ("author-sitemap", "category-sitemap")):
                continue
            try:
                res = client.get(child)
                res.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"  ! skipped {child}: {exc}")
                continue
            found = LOC_RE.findall(res.text)
            print(f"  {child.rsplit('/', 1)[-1]:28} {len(found):3} urls")
            urls.extend(found)
    return urls


def path_of(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url) or "/"
    return path if path.startswith("/") else f"/{path}"


def is_junk(url: str) -> bool:
    return any(slug in url for slug in JUNK_SLUGS)


def clean_urls(urls: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for url in urls:
        url = url.split("#")[0].rstrip("/") or url
        if is_junk(url):
            print(f"  - dropped junk: {path_of(url)}")
            continue
        seen.setdefault(url, None)
    return list(seen)


def strip_chrome(markdown: str) -> str:
    text = markdown
    for pattern in CHROME_PATTERNS[:-1]:
        text = pattern.sub("", text)
    return CHROME_PATTERNS[-1].sub("\n\n", text).strip()


# A line present on this share of pages is site furniture, not page content.
BOILERPLATE_SHARE = 0.6


def strip_boilerplate(pages: list[dict]) -> int:
    """Remove lines that repeat across most pages. Mutates pages in place.

    Elementor renders the nav twice (desktop + mobile drawer) plus a social bar
    and CTA strip on all 50 pages. Matching those by class name is a guessing
    game that breaks whenever the theme changes; counting how often a line
    recurs identifies furniture directly. Anything on >=60% of pages cannot be
    what distinguishes one service page from another.

    Headings are exempt: a repeated heading still marks real page structure.
    """
    counts: dict[str, int] = {}
    for page in pages:
        for line in {ln.strip() for ln in page["markdown"].splitlines() if ln.strip()}:
            counts[line] = counts.get(line, 0) + 1

    threshold = max(2, int(len(pages) * BOILERPLATE_SHARE))
    junk = {
        line
        for line, n in counts.items()
        if n >= threshold and not line.startswith("#")
    }

    for page in pages:
        kept = [ln for ln in page["markdown"].splitlines() if ln.strip() not in junk]
        page["markdown"] = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return len(junk)


def demote_headings(markdown: str) -> str:
    """Push page-internal headings below the '## <page>' delimiter level.

    Without this, a page's own h2 is indistinguishable from a page boundary and
    the model cannot tell where one page's facts stop and the next begins.
    """
    return re.sub(
        r"^(#{1,6}) ",
        lambda m: "#" * min(len(m.group(1)) + 2, 6) + " ",
        markdown,
        flags=re.M,
    )


def cache_path(url: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", path_of(url).lower()).strip("-") or "home"
    return SETTINGS.raw_dir / f"{slug}.json"


def scrape(url: str, refresh: bool = False) -> dict | None:
    """Fetch one page and reduce it to markdown. Cached in kb/raw/.

    Plain httpx, not Firecrawl: the site is server-rendered WordPress, so a GET
    returns the complete document. No API key, no rate limit, no per-page cost.
    """
    cached = cache_path(url)
    if cached.exists() and not refresh:
        return json.loads(cached.read_text(encoding="utf-8"))

    for attempt in range(3):
        try:
            res = httpx.get(
                url,
                timeout=45,
                follow_redirects=True,
                headers={"User-Agent": "SystematicIT-KB-Builder/1.0"},
            )
            res.raise_for_status()
            html = res.text
            record = {
                "url": url,
                "path": path_of(url),
                "title": page_title(html) or path_of(url),
                "description": meta_description(html),
                "markdown": strip_chrome(html_to_markdown(html)),
            }
            cached.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return record
        except httpx.HTTPError as exc:
            if attempt == 2:
                print(f"  ! failed {path_of(url)}: {exc}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


def dedupe(pages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Collapse byte-identical pages, keeping the shortest path as canonical.

    WordPress publishes 13 services twice -- /seo/local-seo and
    /services/seo/local-seo are the same document. Sending both to the model
    wastes ~40% of the prompt and teaches it that duplicates are meaningful.

    Only the KB is deduped. The allowlist keeps every alias, because a visitor
    can legitimately land on either URL and navigate_to must accept both.
    """
    by_hash: dict[str, list[dict]] = {}
    for page in pages:
        digest = hashlib.md5(page["markdown"].encode("utf-8")).hexdigest()
        by_hash.setdefault(digest, []).append(page)

    canonical: list[dict] = []
    aliases: dict[str, str] = {}
    for group in by_hash.values():
        group.sort(key=lambda p: (len(p["path"]), p["path"]))
        primary, rest = group[0], group[1:]
        canonical.append(primary)
        for other in rest:
            aliases[other["path"]] = primary["path"]
    return canonical, aliases


def build_kb(pages: list[dict], aliases: dict[str, str]) -> str:
    """One '## Title -- /path' block per page: the system-prompt payload."""
    out = [
        "# Systematic IT Solutions -- Website Knowledge Base",
        "",
        "Source: scraped from the live site. This is the agent's only source of",
        "truth about services. If a fact is not here, the agent must not assert it.",
        "",
    ]
    if aliases:
        out.append("Some pages are reachable at two URLs; the alias is listed with")
        out.append("its canonical page so navigation works from either.")
        out.append("")
    by_canonical: dict[str, list[str]] = {}
    for alias, target in aliases.items():
        by_canonical.setdefault(target, []).append(alias)

    for page in sorted(pages, key=lambda p: p["path"]):
        if not page["markdown"]:
            continue
        out.append(f"## {page['title']} -- {page['path']}")
        also = by_canonical.get(page["path"])
        if also:
            out.append(f"_Also at: {', '.join(sorted(also))}_")
        if page["description"]:
            out.append(f"_{page['description']}_")
        out.extend(["", demote_headings(page["markdown"]), ""])
    return "\n".join(out)


def build_sitemap(pages: list[dict], aliases: dict[str, str]) -> dict:
    """The navigate_to allowlist. A path absent here is refused, not fetched."""
    entries = [
        {"path": p["path"], "title": p["title"], "description": p["description"]}
        for p in pages
    ]
    entries += [
        {"path": alias, "title": "", "description": "", "alias_of": target}
        for alias, target in aliases.items()
    ]
    return {
        "generated_from": SETTINGS.site_base_url,
        "page_count": len(entries),
        "canonical_count": len(pages),
        "pages": sorted(entries, key=lambda e: e["path"]),
    }


def load_cached_pages() -> list[dict]:
    """Every page already scraped into kb/raw/. No network, no sitemap.

    kb/raw/ is the only committed part of the KB, so this is what a fresh clone
    has to rebuild from -- see kb/autobuild.py. Output order is irrelevant:
    dedupe(), build_kb() and build_sitemap() all sort by path.
    """
    pages: list[dict] = []
    for file in sorted(SETTINGS.raw_dir.glob("*.json")):
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  ! skipped unreadable {file.name}: {exc}")
            continue
        # A record without a path cannot be placed in the KB or the allowlist.
        if record.get("path") and record.get("markdown"):
            pages.append(record)
    return pages


def emit_artifacts(pages: list[dict]) -> dict:
    """pages -> site_kb.md + sitemap.json. Mutates pages (strips boilerplate).

    Shared by the crawl and by the offline rebuild so the two cannot produce
    different artifacts from the same pages.
    """
    # Order matters: stripping the nav changes which pages are byte-identical,
    # so boilerplate removal has to precede dedupe.
    junk_lines = strip_boilerplate(pages)
    empty = [p["path"] for p in pages if not p["markdown"]]
    canonical, aliases = dedupe([p for p in pages if p["markdown"]])
    kb_text = build_kb(canonical, aliases)

    SETTINGS.kb_file.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.kb_file.write_text(kb_text, encoding="utf-8")
    SETTINGS.sitemap_file.write_text(
        json.dumps(build_sitemap(canonical, aliases), indent=2), encoding="utf-8"
    )
    return {
        "junk_lines": junk_lines,
        "empty": empty,
        "canonical": len(canonical),
        "aliases": len(aliases),
        "chars": len(kb_text),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="list URLs, no scraping")
    parser.add_argument("--refresh", action="store_true", help="ignore cache")
    parser.add_argument(
        "--offline", action="store_true", help="rebuild from kb/raw/ only, no fetching"
    )
    args = parser.parse_args()

    if args.offline:
        pages = load_cached_pages()
        if not pages:
            print(f"No cached pages in {SETTINGS.raw_dir} -- run once without --offline.")
            return 1
        print(f"Rebuilding from {len(pages)} cached pages in {SETTINGS.raw_dir.name}/")
        fetched = 0
    else:
        print("Reading sitemaps...")
        urls = clean_urls(fetch_sitemap_urls())
        print(f"\n{len(urls)} real URLs after filtering")

        if args.dry_run:
            for url in sorted(urls, key=path_of):
                print(f"  {path_of(url)}")
            return 0

        SETTINGS.raw_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nScraping {len(urls)} pages (cached in {SETTINGS.raw_dir.name}/)...")
        with ThreadPoolExecutor(max_workers=5) as pool:
            pages = [p for p in pool.map(lambda u: scrape(u, args.refresh), urls) if p]
        fetched = len(urls)

    summary = emit_artifacts(pages)

    # ~4 chars per token is close enough to gate the phase-1 budget check.
    tokens = summary["chars"] // 4
    print(f"\n  pages       {len(pages)}{f'/{fetched} fetched' if fetched else ' from cache'}")
    print(f"  empty       {len(summary['empty'])} {summary['empty'] if summary['empty'] else ''}")
    print(f"  boilerplate {summary['junk_lines']} repeated lines stripped")
    print(f"  canonical   {summary['canonical']}  (+{summary['aliases']} aliases kept navigable)")
    print(f"  site_kb.md  {summary['chars']:,} chars  ~{tokens:,} tokens")
    print(f"  budget      {'OK (<60k)' if tokens < 60_000 else 'OVER 60k -- see phase 2'}")
    print("\nNext: python kb/embed.py  (rebuilds chunks.jsonl + site.faiss)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
