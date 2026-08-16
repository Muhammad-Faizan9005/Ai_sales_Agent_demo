"""Embed the chunks and build the FAISS index.

    python kb/embed.py            # build whatever is missing or stale
    python kb/embed.py --rechunk  # distrust disk: rebuild the whole chain

The chain is kb/raw/*.json -> site_kb.md -> chunks.jsonl -> site.faiss, and
kb/autobuild.py owns deciding which links of it need rebuilding. This module is
the last link plus the CLI over that decision, so running it on a clean clone
needs no other command first.

Output is a build artifact: kb/index/site.faiss, gitignored, rebuilt rather
than committed.

IndexFlatIP with normalised vectors, which makes the inner product exactly the
cosine similarity -- so scores are directly interpretable as 0..1 and the
threshold in config means something. It is exact brute force: at ~300 chunks
that is a sub-millisecond scan and needs no training, no nlist tuning, no
recall/latency tradeoff. IVF or HNSW here would be pure downside.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.config import get_settings  # noqa: E402
from kb.chunk import Chunk  # noqa: E402

SETTINGS = get_settings()

EXPECTED_DIM = 384  # all-MiniLM-L6-v2


def load_chunks() -> list[Chunk]:
    path = SETTINGS.chunks_file
    if not path.exists():
        raise FileNotFoundError(f"{path} missing -- run: python kb/chunk.py")
    with path.open(encoding="utf-8") as fh:
        return [Chunk(**json.loads(line)) for line in fh if line.strip()]


def build(chunks: list[Chunk], model=None) -> tuple[object, int]:
    """Encode the chunks into a FAISS index. `model` reuses a loaded encoder."""
    import faiss

    if model is None:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(SETTINGS.embed_model)
    # normalize_embeddings is an encode() argument, so there is no separate
    # numpy normalisation step to get wrong.
    vectors = model.encode(
        [c.embed_text() for c in chunks],
        batch_size=64,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    dim = vectors.shape[1]
    if dim != EXPECTED_DIM:
        raise AssertionError(
            f"expected {EXPECTED_DIM} dims from {SETTINGS.embed_model}, got {dim}. "
            "api/retrieval.py and any prebuilt index assume 384."
        )

    index = faiss.IndexFlatIP(dim)
    index.add(vectors.astype("float32"))
    return index, dim


def save(index, expected: int) -> Path:
    """Write the index and round-trip it.

    Reading it back rather than trusting the write: a truncated or unreadable
    index otherwise only surfaces on the first visitor question.
    """
    import faiss

    SETTINGS.index_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(SETTINGS.faiss_file))

    reloaded = faiss.read_index(str(SETTINGS.faiss_file))
    if reloaded.ntotal != expected:
        raise AssertionError(
            f"index has {reloaded.ntotal} vectors, expected {expected}"
        )
    if reloaded.d != EXPECTED_DIM:
        raise AssertionError(f"index dim {reloaded.d} != {EXPECTED_DIM}")
    return SETTINGS.faiss_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rechunk", action="store_true", help="rebuild the whole chain from kb/raw/"
    )
    args = parser.parse_args()

    from kb import autobuild

    try:
        report = autobuild.ensure(force=args.rechunk)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"  ! {exc}")
        return 1

    import faiss

    index = faiss.read_index(str(SETTINGS.faiss_file))
    size_kb = SETTINGS.faiss_file.stat().st_size / 1024
    print(f"  built       {', '.join(report.stages) if report.stages else 'nothing -- all current'}")
    print(f"  model       {SETTINGS.embed_model}")
    print(f"  chunks      {index.ntotal}")
    print(f"  dimension   {index.d}")
    print(f"  index       {SETTINGS.faiss_file}  ({size_kb:.0f} KB)")
    print(f"  verified    reloaded {index.ntotal} vectors at dim {index.d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
