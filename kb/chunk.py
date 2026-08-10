"""Split site_kb.md into embeddable chunks.

    python kb/chunk.py            # build + histogram + assertions
    python kb/chunk.py --show 5   # also print the first 5 chunks

kb_build.py already emits one `## <title> -- <path>` block per page, so the page
boundary and its metadata come for free -- this only has to decide where to cut
*within* a page.

THE CEILING THAT MATTERS: all-MiniLM-L6-v2 truncates at 256 word pieces and
does it silently. No error, no warning -- an over-long chunk simply gets a
vector describing its first half, and retrieval quality degrades in a way that
looks like "the model is a bit dim" rather than a bug. So the target sits well
under that, and `verify()` asserts it with the real tokenizer rather than a
character estimate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.config import get_settings  # noqa: E402

SETTINGS = get_settings()

# Word pieces, not characters. The model's hard limit is 256; 200 leaves room
# for the "<title> -- <path>" prefix prepended to every chunk at embed time.
MAX_TOKENS = 200
# Enough to carry a sentence across a cut, so a fact split down the middle is
# still wholly present in one of the two chunks.
OVERLAP_TOKENS = 40
# Below this a chunk is a heading fragment or a stray "Get In Touch" -- noise
# that competes with real content for a top-k slot.
MIN_CHARS = 80

# "## Title -- /path"  (kb_build.py writes a literal double hyphen)
PAGE_RE = re.compile(r"^## (.+?) -- (/\S*)\s*$", re.M)

# ~4 chars per token is the standard English approximation. Only used to decide
# where to cut; the real tokenizer does the verifying.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    path: str
    title: str
    text: str

    def embed_text(self) -> str:
        """What actually gets embedded.

        The title and path go in the vector, not just the metadata: a visitor
        asking "do you do shopify" should match the Shopify page even when the
        body never repeats the word in the chunk that got cut.
        """
        return f"{self.title} -- {self.path}\n{self.text}"


def split_pages(markdown: str) -> list[tuple[str, str, str]]:
    """-> [(title, path, body)]. Anything before the first ## is the preamble."""
    matches = list(PAGE_RE.finditer(markdown))
    pages = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()
        pages.append((match.group(1).strip(), match.group(2), body))
    return pages


def split_body(body: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Cut a page body into windows, preferring paragraph boundaries.

    Paragraphs first because a scraped page is mostly short blocks and cutting
    between them keeps each chunk a coherent thought. A single paragraph longer
    than the window is then hard-split -- rare, but it must not silently exceed
    the ceiling.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    windows: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                windows.append(current)
                current = ""
            # Hard-split the oversized paragraph with overlap between slices.
            step = max_chars - overlap_chars
            for start in range(0, len(para), step):
                windows.append(para[start : start + max_chars])
                if start + max_chars >= len(para):
                    break
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            windows.append(current)
            # Carry the tail of the previous window forward so a fact spanning
            # the cut survives intact in at least one chunk.
            tail = current[-overlap_chars:] if overlap_chars else ""
            current = f"{tail}\n\n{para}".strip() if tail else para

    if current:
        windows.append(current)
    return windows


def build() -> list[Chunk]:
    markdown = SETTINGS.kb_file.read_text(encoding="utf-8")
    max_chars = MAX_TOKENS * CHARS_PER_TOKEN
    overlap_chars = OVERLAP_TOKENS * CHARS_PER_TOKEN

    chunks: list[Chunk] = []
    for title, path, body in split_pages(markdown):
        if not body:
            continue
        for window in split_body(body, max_chars, overlap_chars):
            if len(window) >= MIN_CHARS:
                chunks.append(Chunk(path=path, title=title, text=window))
    return chunks


def write(chunks: list[Chunk]) -> Path:
    out = SETTINGS.chunks_file
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
    return out


def verify(chunks: list[Chunk]) -> list[int]:
    """Assert every chunk fits, using the REAL tokenizer.

    A character estimate is what lets this regress quietly: markdown links and
    long URLs tokenize far worse than 4 chars/token, so a chunk that looks fine
    by length can still blow the 256 ceiling.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(SETTINGS.embed_model)
    limit = model.max_seq_length
    tokenizer = model.tokenizer

    lengths = [len(tokenizer.tokenize(c.embed_text())) for c in chunks]
    over = [(c, n) for c, n in zip(chunks, lengths) if n > limit]
    if over:
        worst = max(over, key=lambda pair: pair[1])
        raise AssertionError(
            f"{len(over)} chunk(s) exceed the model's {limit}-wordpiece limit "
            f"and would be silently truncated. Worst: {worst[1]} tokens on "
            f"{worst[0].path}. Lower MAX_TOKENS in kb/chunk.py."
        )
    return lengths


def histogram(lengths: list[int], limit: int, buckets: int = 8) -> None:
    if not lengths:
        return
    top = max(lengths)
    width = max(1, (top + buckets - 1) // buckets)
    print(f"\n  token lengths (model limit {limit}):")
    for b in range(buckets):
        lo, hi = b * width, (b + 1) * width
        count = sum(1 for n in lengths if lo <= n < hi)
        if count:
            print(f"    {lo:4}-{hi - 1:4}  {'#' * min(count, 50):50} {count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", type=int, default=0, help="print N sample chunks")
    args = parser.parse_args()

    chunks = build()
    if not chunks:
        print("No chunks produced. Has kb/kb_build.py been run?")
        return 1

    lengths = verify(chunks)
    out = write(chunks)

    pages = len({c.path for c in chunks})
    print(f"  pages       {pages}")
    print(f"  chunks      {len(chunks)}  ({len(chunks) / pages:.1f} per page)")
    print(f"  tokens      min {min(lengths)}  median {sorted(lengths)[len(lengths) // 2]}  max {max(lengths)}")
    print(f"  written     {out}")
    histogram(lengths, limit=256)

    for chunk in chunks[: args.show]:
        print(f"\n--- {chunk.path} ({chunk.title})\n{chunk.text[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
