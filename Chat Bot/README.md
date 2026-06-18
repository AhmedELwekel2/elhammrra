# SOPs Chat Bot

Grounded Arabic Q&A over the field-quality SOPs document
(`منصة_الجودة_الميدانية_SOPs.pdf`). Built on LangChain + **Chroma** (local, on-disk
vector store — no database server) + Azure OpenAI. Exposes the same HTTP contract
as the `haj_rag` service so the Express gateway treats both RAG tools identically.

## Setup

```bash
cd "Chat Bot"
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env          # then fill in SOPS_EMBED_KEY and SOPS_CHAT_KEY
```

## 1. Ingest the PDF (run once)

```bash
python doc_ingestion_pipeline.py
```

Loads the PDF, chunks it (500 chars, 200 overlap), embeds with Azure
`text-embedding-3-small`, and stores the vectors in a local Chroma store under
`data/chroma/` (collection `document_chunks`).

## 2. Serve

```bash
uvicorn server:app --host 0.0.0.0 --port 8030
# or: python server.py
```

## Endpoints

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET  | `/health` | — | `{status, service, collection}` |
| POST | `/ask` | `{"question":"…","k":5}` | `{answer, sources[]}` |
| POST | `/chat` | `?query=…&k=5` (legacy) | `{answer, sources[]}` |

`sources[]` items: `{id, score, heading_path, page_start, page_end}`.

## Notes

- Secrets live in `.env` (gitignored), never in source.
- The gateway calls `POST /ask`; `/chat` is kept for backward compatibility.
- Embeddings use the `alhmra-resource` Azure resource; chat uses
  `quality-project-resource`. Both are configurable via `.env`.
