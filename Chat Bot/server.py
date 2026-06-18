"""
SOPs Chat Bot — FastAPI service.

Exposes the same contract as the haj_rag service so the Express gateway can treat
both RAG tools identically:

  GET  /health      liveness
  POST /ask         {"question": "…", "k": 5} -> {answer, sources[]}
  POST /chat        (legacy alias kept for backward compatibility)

Retrieval is pgvector similarity search over the SOPs collection; the answer is
grounded by an Azure OpenAI chat deployment. Config comes from .env.

Run (from the `Chat Bot` directory):
  uvicorn server:app --host 0.0.0.0 --port 8030
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from doc_ingestion_pipeline import COLLECTION, get_vectorstore

load_dotenv()

# ── chat config (from environment) ───────────────────────────────────────────
CHAT_ENDPOINT = os.getenv("SOPS_CHAT_ENDPOINT", "https://quality-project-resource.openai.azure.com").rstrip("/")
CHAT_DEPLOYMENT = os.getenv("SOPS_CHAT_DEPLOYMENT", "gpt-5.2-chat-2")
CHAT_API_VERSION = os.getenv("SOPS_CHAT_API_VERSION", "2024-10-21")
CHAT_KEY = os.getenv("SOPS_CHAT_KEY", "")
CHAT_URL = f"{CHAT_ENDPOINT}/openai/deployments/{CHAT_DEPLOYMENT}/chat/completions?api-version={CHAT_API_VERSION}"

SYSTEM_PROMPT = (
    "أنت مساعد متخصص في إجراءات الجودة الميدانية بشركة الحمراء لخدمات حجاج الداخل. "
    "أجب بناءً على السياق المرفق فقط. إذا لم تجد الإجابة في السياق، فقل صراحةً: "
    "«لا يتناول الدليل هذه المسألة في المقاطع المتاحة». أجب بالعربية الفصحى بوضوح."
)

app = FastAPI(title="SOPs Chat Bot", version="1.0.0")

# Build the vector store once and reuse it across requests.
vectorstore = get_vectorstore()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    k: int = Field(5, ge=1, le=20)


async def call_chat(system: str, context: str, question: str) -> str:
    if not CHAT_KEY:
        raise RuntimeError("SOPS_CHAT_KEY is not set (see .env.example).")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            CHAT_URL,
            headers={"api-key": CHAT_KEY, "Content-Type": "application/json"},
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"السياق:\n{context}\n\nالسؤال:\n{question}"},
                ],
                "max_completion_tokens": 1000,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def run_ask(question: str, k: int = 5) -> dict:
    try:
        results = vectorstore.similarity_search_with_score(question, k=k)
    except Exception as exc:  # retrieval / DB failure
        raise HTTPException(status_code=502, detail=f"retrieval failed: {exc}") from exc

    context = "\n\n".join(doc.page_content for doc, _ in results)
    try:
        answer = await call_chat(SYSTEM_PROMPT, context, question)
    except Exception as exc:  # upstream Azure failure
        raise HTTPException(status_code=502, detail=f"generation failed: {exc}") from exc

    sources = []
    for doc, score in results:
        page = doc.metadata.get("page")
        page_no = (page + 1) if isinstance(page, int) else 0  # PyMuPDF page is 0-based
        sources.append({
            "id": str(doc.metadata.get("source") or "") + f"#p{page_no}",
            "score": round(float(score), 4),
            "heading_path": os.path.basename(str(doc.metadata.get("source") or "SOPs")),
            "page_start": page_no,
            "page_end": page_no,
        })
    return {"answer": answer, "sources": sources}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "sops", "collection": COLLECTION}


@app.post("/ask")
async def ask(req: AskRequest) -> dict:
    return await run_ask(req.question, req.k)


@app.post("/chat")
async def chat(query: str, k: int = 5) -> dict:
    """Legacy endpoint (query param). Prefer POST /ask with a JSON body."""
    return await run_ask(query, k)


def main() -> None:
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8030")))


if __name__ == "__main__":
    main()
