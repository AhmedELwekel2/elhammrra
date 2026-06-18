"""
FastAPI service exposing the RAG generation step (retrieve + ground + answer).

Wraps src.generate.answer.answer() behind an HTTP endpoint. The expensive
handles — Chroma collection, BM25 index, and both Azure OpenAI clients — are
built once at startup and reused across requests (the same warm-handle pattern
the interactive CLI loop uses), so each request pays only retrieval + chat cost.

Run:  uvicorn src.generate.api:app --reload
      (or)  python -m src.generate.api          # serves on :8000

Endpoints:
  GET  /health        liveness + chunk count
  POST /ask           {"question": "…", "k": 6} -> {answer, hits}
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.embed.embed import build_bm25, get_collection, load_chunks, make_client
from src.generate.answer import DEFAULT_K, answer, make_chat_client

ROOT = Path(__file__).resolve().parents[2]

# Warm handles, populated on startup and reused by every request.
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build retrieval + generation handles once, before serving traffic."""
    load_dotenv(ROOT / ".env")
    chunks = load_chunks()
    embed_client, embed_deployment = make_client()
    chat_client, chat_deployment = make_chat_client()
    _state.update(
        col=get_collection(),
        chunks=chunks,
        bm25=build_bm25(chunks),
        embed_client=embed_client,
        embed_deployment=embed_deployment,
        chat_client=chat_client,
        chat_deployment=chat_deployment,
    )
    yield
    _state.clear()


app = FastAPI(
    title="Hajj RAG — generation",
    description="Retrieve top-k chunks and ground a GPT-5.2 answer on them.",
    version="1.0.0",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Arabic question about Hajj/Umrah.")
    k: int = Field(DEFAULT_K, ge=1, le=20, description="Number of chunks to retrieve.")


class Source(BaseModel):
    id: str
    score: float
    heading_path: str
    page_start: int
    page_end: int


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]


@app.get("/health")
def health() -> dict:
    if not _state:
        raise HTTPException(status_code=503, detail="service not ready")
    return {"status": "ok", "chunks": _state["col"].count(), "model": _state["chat_deployment"]}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    if not _state:
        raise HTTPException(status_code=503, detail="service not ready")
    try:
        result = answer(
            req.question,
            k=req.k,
            col=_state["col"],
            chunks=_state["chunks"],
            bm25=_state["bm25"],
            embed_client=_state["embed_client"],
            embed_deployment=_state["embed_deployment"],
            chat_client=_state["chat_client"],
            chat_deployment=_state["chat_deployment"],
        )
    except Exception as exc:  # surface upstream (Azure / Chroma) failures as 502
        raise HTTPException(status_code=502, detail=f"generation failed: {exc}") from exc

    sources = [
        Source(
            id=h["id"],
            score=h["score"],
            heading_path=h["meta"]["heading_path"],
            page_start=h["meta"]["page_start"],
            page_end=h["meta"]["page_end"],
        )
        for h in result["hits"]
    ]
    return AskResponse(answer=result["answer"], sources=sources)


def main() -> None:
    import uvicorn

    uvicorn.run("src.generate.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
