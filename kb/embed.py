"""Embed the chunks and build the FAISS index.

    python kb/embed.py            # rebuild from kb/index/chunks.jsonl
    python kb/embed.py --rechunk  # run chunk.py first, then embed

Run after kb/kb_build.py or kb/chunk.py. Output is a build artifact:
kb/index/site.faiss, gitignored, rebuilt rather than committed.

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
        raise SystemExit(f"{path} missing -- run: python kb/chunk.py")
    with path.open(encoding="utf-8") as fh:
        return [Chunk(**json.loads(line)) for line in fh if line.strip()]


def build(chunks: list[Chunk]) -> tuple[object, int]:
    import faiss
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rechunk", action="store_true", help="run chunk.py first")
    args = parser.parse_args()

    if args.rechunk:
        from kb import chunk as chunk_module

        chunks = chunk_module.build()
        chunk_module.verify(chunks)
        chunk_module.write(chunks)
        print(f"  rechunked   {len(chunks)} chunks")

    import faiss

    chunks = load_chunks()
    index, dim = build(chunks)

    SETTINGS.index_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(SETTINGS.faiss_file))

    size_kb = SETTINGS.faiss_file.stat().st_size / 1024
    print(f"  model       {SETTINGS.embed_model}")
    print(f"  chunks      {len(chunks)}")
    print(f"  dimension   {dim}")
    print(f"  index       {SETTINGS.faiss_file}  ({size_kb:.0f} KB)")

    # Round-trip the index rather than trusting the write: a truncated or
    # unreadable index otherwise only surfaces on the first visitor question.
    reloaded = faiss.read_index(str(SETTINGS.faiss_file))
    assert reloaded.ntotal == len(chunks), (
        f"index has {reloaded.ntotal} vectors, expected {len(chunks)}"
    )
    assert reloaded.d == EXPECTED_DIM, f"index dim {reloaded.d} != {EXPECTED_DIM}"
    print(f"  verified    reloaded {reloaded.ntotal} vectors at dim {reloaded.d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
