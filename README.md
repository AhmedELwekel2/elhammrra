# El Hamra Quality Platform — AI Tools Integration

This repo wires three Python AI backends into the **El Hamra Quality Platform**
(static Firebase app) through a single **Express + PostgreSQL/Sequelize** gateway,
and exposes them as themed, in-platform GUI tools available to **any signed-in user**.

```
Platform pages (themed, RTL)            ┌─────────────────────────────┐
  tools/*.html  ──Firebase ID token──►  │  Express gateway  (:3000)   │
                                         │  auth · usage · history ·   │
                                         │  cache  (Postgres/Sequelize)│
                                         └───────────┬─────────────────┘
                         ┌───────────────────────────┼───────────────────────────┐
                         ▼                            ▼                           ▼
            News/Reports agent (:8010)        CRAG Agent (:8020)      SOPs Chat Bot (:8030)
            FastAPI · LangGraph · Bedrock     FastAPI · LangGraph      FastAPI · Azure · pgvector

Firebase (Firestore + Auth)  — unchanged; still the source of truth for QMS data + login.
```

The browser only ever talks to the gateway. Firebase keeps all platform/QMS data
and login; Postgres holds only the gateway's own data (users mirror, per-user
usage limits, generated-report records, news cache, RAG/SOPs chat history).

## Components

| Path | What | Port |
|------|------|------|
| `server/` | Express gateway (Sequelize models, auth, usage, proxy, routes) | 3000 |
| `elhajj…boooot-main/elhajj…boooot-main/` | News + AI reports (FastAPI/LangGraph/Bedrock) | 8010 |
| `hAJJJJ/` | CRAG Hajj agent (FastAPI/LangGraph/Azure/Chroma + islamweb fallback) | 8020 |
| `Chat Bot/` | SOPs Q&A (FastAPI/Azure/**pgvector**) — built from the stubs | 8030 |
| `منصة الجودة …/tools/` | Themed GUI pages (hub, news, two chats) | static |

## Quick start (local, without Docker)

1. **Postgres** running locally; create the gateway DB and enable pgvector:
   ```sql
   CREATE DATABASE elhamra_tools;
   CREATE EXTENSION IF NOT EXISTS vector;   -- in the DB the SOPs bot uses
   ```
2. **Gateway:** `cd server && npm install && cp .env.example .env` (edit it), then `npm start`.
   - For a quick demo without Firebase, set `AUTH_DISABLED=true` in `server/.env`.
   - Otherwise put the Firebase service-account JSON where `GOOGLE_APPLICATION_CREDENTIALS` points.
3. **News agent:** `cd elhajj…/elhajj… && pip install -r quality_bot/requirements.txt && cd quality_bot && python -m uvicorn agent.api:app --port 8010`
4. **CRAG Hajj agent:** `cd hAJJJJ && pip install -r requirements.txt`, build the index once (`python -m src.ingest.azure_ocr && python -m src.ingest.chunk && python -m src.embed.embed`), then `python -m uvicorn src.agent.api:app --port 8020`. In `server/.env`, use `RAG_URL=http://127.0.0.1:8020` and `RAG_CONTRACT=chat`.
5. **SOPs Chat Bot:** `cd "Chat Bot" && pip install -r requirements.txt && cp .env.example .env` (fill keys), ingest once (`python doc_ingestion_pipeline.py`), then `uvicorn server:app --port 8030`
6. **Front-end:** serve the platform folder statically and confirm `js/config.js → API_CONFIG.toolsBaseUrl` points at the gateway (`http://127.0.0.1:3000`). Sign in, then open **الأدوات الذكية** from any dashboard sidebar (or the navbar button).

## Quick start (Docker)

```bash
# create the .env files from each .env.example first, and add the Firebase
# service account at server/secrets/firebase-service-account.json
docker compose up --build
```

Brings up Postgres (pgvector), the three services, and the gateway.
**Ingestion still runs once** for the CRAG agent and SOPs indexes (see each service's README).

## Per-service docs

- Gateway endpoints & auth: [`server/README.md`](server/README.md)
- SOPs Chat Bot: [`Chat Bot/README.md`](Chat%20Bot/README.md)
- News agent: `elhajj…/elhajj…/quality_bot/API_README.md`

## Security notes

- Secrets live in `.env` files (gitignored). The original Chat Bot stub had an
  embedded API key/DB password — those were moved into `Chat Bot/.env`. **Rotate
  any key that was previously committed.**
- The three Python services default to open CORS; in production keep them on an
  internal network and expose only the gateway, with `CORS_ORIGIN` set to the
  platform's real origin.
# elhammrra
