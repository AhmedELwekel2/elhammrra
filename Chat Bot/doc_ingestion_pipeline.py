"""
SOPs Chat Bot — document ingestion pipeline.

Loads the field-quality SOPs PDF, splits it into chunks, embeds them with the
Azure OpenAI embeddings deployment, and stores the vectors in a local Chroma
store (no database server needed — same approach as the haj_rag service).

All endpoints / keys come from the environment (.env) — nothing is hardcoded.
See .env.example.

Run (from the `Chat Bot` directory):
  python doc_ingestion_pipeline.py
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv
from langchain_core.embeddings import Embeddings

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))

# ── config (from environment) ────────────────────────────────────────────────
EMBED_ENDPOINT = os.getenv("SOPS_EMBED_ENDPOINT", "https://alhmra-resource.cognitiveservices.azure.com").rstrip("/")
EMBED_DEPLOYMENT = os.getenv("SOPS_EMBED_DEPLOYMENT", "text-embedding-3-small")
EMBED_API_VERSION = os.getenv("SOPS_EMBED_API_VERSION", "2023-05-15")
EMBED_KEY = os.getenv("SOPS_EMBED_KEY", "")

# Local Chroma vector store (persisted on disk) — no Postgres/pgvector required.
# Use `or` so an empty env value (SOPS_CHROMA_DIR=) falls back to the default.
CHROMA_DIR = os.getenv("SOPS_CHROMA_DIR") or os.path.join(HERE, "data", "chroma")
COLLECTION = os.getenv("SOPS_COLLECTION") or "document_chunks"

PDF_PATH = os.getenv("SOPS_PDF_PATH", os.path.join(HERE, "منصة_الجودة_الميدانية_SOPs.pdf"))

EMBED_URL = f"{EMBED_ENDPOINT}/openai/deployments/{EMBED_DEPLOYMENT}/embeddings?api-version={EMBED_API_VERSION}"


class MyEmbeddings(Embeddings):
    """Azure OpenAI embeddings via the REST API (text-embedding-3-small)."""

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not EMBED_KEY:
            raise RuntimeError("SOPS_EMBED_KEY is not set (see .env.example).")
        resp = httpx.post(
            EMBED_URL,
            headers={"api-key": EMBED_KEY, "Content-Type": "application/json"},
            json={"input": texts},  # Azure expects {"input": ...}
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def get_vectorstore():
    """Return a Chroma handle bound to the SOPs collection (persisted on disk)."""
    from langchain_chroma import Chroma

    os.makedirs(CHROMA_DIR, exist_ok=True)
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=MyEmbeddings(),
        persist_directory=CHROMA_DIR,
    )


def load_and_chunk(doc_path: str):
    """Load a PDF and split it into overlapping chunks."""
    from langchain_community.document_loaders import PyMuPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    loader = PyMuPDFLoader(doc_path)
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=200,
        separators=["\n\n", "\n", ".", " "],
    )
    chunks = splitter.split_documents(docs)
    print(f"[ingest] chunked into {len(chunks)} pieces", flush=True)
    return chunks


def ingest() -> None:
    from langchain_chroma import Chroma

    if not os.path.exists(PDF_PATH):
        raise SystemExit(f"PDF not found: {PDF_PATH}")
    chunks = load_and_chunk(PDF_PATH)
    os.makedirs(CHROMA_DIR, exist_ok=True)
    Chroma.from_documents(
        documents=chunks,
        embedding=MyEmbeddings(),
        collection_name=COLLECTION,
        persist_directory=CHROMA_DIR,
    )
    print(f"[ingest] stored {len(chunks)} chunks in '{COLLECTION}' -> {CHROMA_DIR} ✓", flush=True)


# Guard: importing this module (e.g. from server.py) must NOT trigger ingestion.
if __name__ == "__main__":
    ingest()
