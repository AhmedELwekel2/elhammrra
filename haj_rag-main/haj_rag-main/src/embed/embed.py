"""
Embed data/processed/chunks.jsonl into a local Chroma vector store.

Each chunk's normalized text (`text_norm`) is embedded with Azure OpenAI
text-embedding-3-small, and stored in a persistent Chroma collection together
with the human-readable display `text` (what the answer-LLM will read) and the
retrieval metadata (section_type, heading_path, page span, fatwa question, …).

Embedding `text_norm` — not the raw text — is deliberate: the same
normalize_arabic() is applied to queries at search time, so query and document
land in the same orthographic space (no tashkeel / alef-ya variation to split
otherwise-identical words). See src/ingest/normalize.py.

Idempotent: chunks already present in the collection are skipped (no re-bill)
unless --force is passed. Cosine space.

Config (.env):
  AZURE_OPENAI_ENDPOINT       e.g. https://<resource>.openai.azure.com
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_API_VERSION    optional (default 2024-10-21)
  AZURE_OPENAI_EMBED_DEPLOYMENT  optional (default text-embedding-3-small)

Run:  python -m src.embed.embed            (embed everything new)
      python -m src.embed.embed --force    (re-embed all)
      python -m src.embed.embed --query "هل يجوز الذبح خارج الحرم؟"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.ingest.normalize import normalize_arabic

# --- paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
PROC_DIR = ROOT / "data" / "processed"
CHUNKS_JSONL_PATH = PROC_DIR / "chunks.jsonl"
CHROMA_DIR = PROC_DIR / "chroma"

# --- tunables ----------------------------------------------------------------
COLLECTION = "hajj_chunks"
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_DEPLOYMENT = "text-embedding-3-small"
BATCH_SIZE = 100  # text-embedding-3-* accept many inputs per call

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    print(f"[embed] {msg}", flush=True)


# --- Azure OpenAI client -----------------------------------------------------
def make_client() -> tuple[object, str]:
    """Return (AzureOpenAI client, deployment). Fail fast on missing config."""
    from openai import AzureOpenAI

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)
    deployment = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", DEFAULT_DEPLOYMENT)

    missing = [
        n for n, v in (("AZURE_OPENAI_ENDPOINT", endpoint), ("AZURE_OPENAI_API_KEY", key))
        if not v
    ]
    if missing:
        raise SystemExit(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Set them in {ROOT / '.env'} (see the module docstring)."
        )

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
    return client, deployment


def embed_texts(client, deployment: str, texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in batches, preserving order."""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = client.embeddings.create(model=deployment, input=batch)
        # API guarantees response order matches input order, but sort to be safe.
        for item in sorted(resp.data, key=lambda d: d.index):
            vectors.append(item.embedding)
        log(f"embedded {min(i + BATCH_SIZE, len(texts))}/{len(texts)}")
    return vectors


# --- chunk -> chroma record --------------------------------------------------
def load_chunks() -> list[dict]:
    if not CHUNKS_JSONL_PATH.exists():
        raise SystemExit(f"missing input: {CHUNKS_JSONL_PATH} (run src.ingest.chunk first)")
    with CHUNKS_JSONL_PATH.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def to_metadata(c: dict) -> dict:
    """Chroma metadata must be flat str/int/float/bool — no lists or None."""
    return {
        "section_type": c["section_type"],
        "heading_path": " > ".join(c["heading_path"]),
        "topic_title": c["topic_title"] or "",
        "question": c["question"] or "",
        "page_start": c["page_start"],
        "page_end": c["page_end"],
        "part_index": c["part_index"],
        "part_total": c["part_total"],
        "n_citations": len(c["citations"]),
    }


def get_collection():
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}
    )


# --- index -------------------------------------------------------------------
def build_index(force: bool) -> None:
    chunks = load_chunks()
    col = get_collection()
    log(f"loaded {len(chunks)} chunks | collection holds {col.count()}")

    chunk_ids = {c["chunk_id"] for c in chunks}
    existing = set(col.get(include=[])["ids"])

    # Reconcile deletions: drop stale ids no longer produced by the chunker
    # (e.g. TOC chunks removed, or a multi-part unit collapsed to one part).
    orphans = existing - chunk_ids
    if orphans:
        col.delete(ids=list(orphans))
        log(f"deleted {len(orphans)} orphaned chunk(s) from the store")

    todo = chunks if force else [c for c in chunks if c["chunk_id"] not in existing]

    if not todo:
        log("nothing new to embed (use --force to re-embed all) ✓")
        return

    client, deployment = make_client()
    log(f"embedding {len(todo)} chunks with {deployment}")
    vectors = embed_texts(client, deployment, [c["text_norm"] for c in todo])

    col.upsert(
        ids=[c["chunk_id"] for c in todo],
        embeddings=vectors,
        documents=[c["text"] for c in todo],
        metadatas=[to_metadata(c) for c in todo],
    )
    log(f"upserted {len(todo)} chunks -> {CHROMA_DIR} (total {col.count()}) ✓")


# --- query (smoke test / reusable search) ------------------------------------
def search(query: str, k: int = 5, section_type: str | None = None,
           col=None, client=None, deployment: str | None = None) -> list[dict]:
    """Pure-vector search. Embed a query (normalized identically to chunks) and
    return top-k hits.

    Pass warm `col`/`client`/`deployment` to reuse handles across many queries
    (the interactive loop does this so it pays setup cost only once).
    """
    if col is None:
        col = get_collection()
    if client is None:
        client, deployment = make_client()
    qvec = embed_texts(client, deployment, [normalize_arabic(query)])[0]
    where = {"section_type": section_type} if section_type else None
    res = col.query(query_embeddings=[qvec], n_results=k, where=where)
    hits = []
    for cid, doc, meta, dist in zip(
        res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        hits.append({"id": cid, "score": 1 - dist, "meta": meta, "text": doc})
    return hits


# --- BM25 lexical retriever --------------------------------------------------
# Word tokens: Arabic letters + ASCII alphanumerics. text_norm is already
# tashkeel-stripped and letter-folded, so query and corpus tokenize the same way.
_TOKEN_RE = re.compile(r"[0-9A-Za-z؀-ۿ]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def build_bm25(chunks: list[dict]):
    """In-memory BM25 over the 191 chunks' normalized text (cheap to rebuild)."""
    from rank_bm25 import BM25Okapi

    corpus = [tokenize(c["text_norm"]) for c in chunks]
    return BM25Okapi(corpus)


# --- hybrid search (vector + BM25, fused with RRF) ---------------------------
RRF_K = 60  # standard reciprocal-rank-fusion constant


def hybrid_search(query: str, k: int = 5, pool: int = 30, section_type: str | None = None,
                  col=None, client=None, deployment: str | None = None,
                  chunks: list[dict] | None = None, bm25=None) -> list[dict]:
    """Fuse vector similarity and BM25 keyword ranking via Reciprocal Rank Fusion.

    Each retriever contributes 1/(RRF_K + rank) per document; summing the two
    rank-based scores avoids having to reconcile cosine (0-1) with BM25's
    unbounded scores. `pool` is the candidate depth taken from each retriever.
    """
    if chunks is None:
        chunks = load_chunks()
    if bm25 is None:
        bm25 = build_bm25(chunks)
    if col is None:
        col = get_collection()
    if client is None:
        client, deployment = make_client()

    by_id = {c["chunk_id"]: c for c in chunks}
    ids = [c["chunk_id"] for c in chunks]
    keep = {c["chunk_id"] for c in chunks
            if not section_type or c["section_type"] == section_type}
    qn = normalize_arabic(query)
    depth = min(pool, len(ids))

    # Vector ranking.
    qvec = embed_texts(client, deployment, [qn])[0]
    where = {"section_type": section_type} if section_type else None
    vres = col.query(query_embeddings=[qvec], n_results=depth, where=where)
    vec_ids = vres["ids"][0]

    # BM25 ranking.
    scores = bm25.get_scores(tokenize(qn))
    order = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)
    bm_ids = [ids[i] for i in order if ids[i] in keep][:depth]

    # Reciprocal Rank Fusion.
    fused: dict[str, float] = {}
    vrank = {cid: r for r, cid in enumerate(vec_ids)}
    brank = {cid: r for r, cid in enumerate(bm_ids)}
    for cid in set(vec_ids) | set(bm_ids):
        s = 0.0
        if cid in vrank:
            s += 1.0 / (RRF_K + vrank[cid])
        if cid in brank:
            s += 1.0 / (RRF_K + brank[cid])
        fused[cid] = s

    hits = []
    for cid in sorted(fused, key=lambda c: fused[c], reverse=True)[:k]:
        c = by_id[cid]
        hits.append({
            "id": cid,
            "score": fused[cid],
            "meta": to_metadata(c),
            "text": c["text"],
            "vec_rank": vrank.get(cid),
            "bm_rank": brank.get(cid),
        })
    return hits


def print_hits(hits: list[dict]) -> None:
    for h in hits:
        m = h["meta"]
        # Hybrid hits carry retriever ranks; label the score accordingly
        # (RRF fusion score vs. raw cosine for pure-vector hits).
        if "vec_rank" in h:
            v = "-" if h["vec_rank"] is None else h["vec_rank"] + 1
            b = "-" if h["bm_rank"] is None else h["bm_rank"] + 1
            label = f"rrf={h['score']:.4f} (vec#{v} bm25#{b})"
        else:
            label = f"cos={h['score']:.3f}"
        log(f"[{h['id']}] {label}  p{m['page_start']}-{m['page_end']}  {m['heading_path']}")
        if m["question"]:
            log(f"    Q: {m['question'][:90]}")
        preview = h["text"].replace("\n", " ")[:140]
        log(f"    …{preview}")


def run_query(query: str, k: int, vector_only: bool = False) -> None:
    log(f"query: {query!r}  mode={'vector' if vector_only else 'hybrid'}")
    hits = search(query, k=k) if vector_only else hybrid_search(query, k=k)
    print_hits(hits)


def run_interactive(k: int, vector_only: bool = False) -> None:
    """Keep prompting for queries until the user exits — handles stay warm."""
    col = get_collection()
    client, deployment = make_client()
    chunks = None if vector_only else load_chunks()
    bm25 = None if vector_only else build_bm25(chunks)
    mode = "vector" if vector_only else "hybrid (vector + BM25)"
    log(f"ready ({col.count()} chunks, {mode}). Type an Arabic question and press Enter.")
    log("Blank line, 'exit', or Ctrl+C to quit.")
    while True:
        try:
            query = input("\n🔎 سؤال> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query or query.lower() in ("exit", "quit", "q"):
            break
        if vector_only:
            hits = search(query, k=k, col=col, client=client, deployment=deployment)
        else:
            hits = hybrid_search(query, k=k, col=col, client=client,
                                 deployment=deployment, chunks=chunks, bm25=bm25)
        print_hits(hits)
    log("bye 👋")


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed chunks into Chroma / query the store.")
    ap.add_argument("--force", action="store_true", help="re-embed all chunks")
    ap.add_argument("--query", type=str, help="run a single search and exit")
    ap.add_argument("--ask", action="store_true", help="interactive search loop (stays open)")
    ap.add_argument("--vector", action="store_true", help="pure vector search (default is hybrid)")
    ap.add_argument("-k", type=int, default=5, help="top-k for --query / --ask")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    if args.ask:
        run_interactive(args.k, vector_only=args.vector)
    elif args.query:
        run_query(args.query, args.k, vector_only=args.vector)
    else:
        build_index(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
