# El Hamra Tools Gateway (Express + Postgres)

Backend-for-frontend that fronts the three Python AI services for the El Hamra
Quality Platform. The browser only ever talks to this gateway; the Python
services stay on the internal network.

```
Platform pages ──Firebase ID token──► Express (:3000) ──► news agent (:8010)
                                                      ├──► CRAG agent (:8020)
                                                      └──► SOPs bot  (:8030)
Postgres (Sequelize): users, tool_usage, reports, news_cache, ask_log
Firebase: still the source of truth for QMS data + login (unchanged)
```

## Setup

```bash
cd server
npm install
cp .env.example .env          # then edit
# put the Firebase service-account JSON at the path in GOOGLE_APPLICATION_CREDENTIALS
npm start                     # or: npm run dev
```

Requires a reachable PostgreSQL (`DATABASE_URL`). On boot the gateway runs
`sequelize.sync({ alter:true })` to create the five tables.

### Local dev without Firebase

Set `AUTH_DISABLED=true` to skip token verification and run as a fake user
(`DEV_FAKE_UID` / `DEV_FAKE_ROLE`). Never enable in production.

## Endpoints (all under `/api`, all require `Authorization: Bearer <idToken>`)

| Method | Path | Notes |
|--------|------|-------|
| GET  | `/api/health` | gateway + DB + 3 upstream services |
| GET  | `/api/news/:period` | `daily\|weekly\|monthly`; `?days&category&limit`; cached |
| POST | `/api/reports/:type` | `daily\|weekly\|monthly\|magazine`; `?format=file\|pdf\|json`; long-running |
| GET  | `/api/reports` | shared report library |
| GET  | `/api/files/:name.pdf` | streams a generated PDF |
| POST | `/api/ask` | CRAG Hajj agent `{question,k}` → `{answer,sources,routing?}` |
| GET  | `/api/ask/recent` | last 5 CRAG turns for the user |
| POST | `/api/sops/ask` | SOPs Chat Bot `{question,k}` |
| GET  | `/api/sops/recent` | last 5 SOPs turns for the user |

Per-user daily limits live in `src/config.js` (`usageLimits`). The two chat
tools keep the 5 most recent turns per user per service and replay them as
follow-up context.

## Getting a token to test from the browser

While signed into the platform, in the dev console:

```js
await FB.auth.currentUser.getIdToken()
```

Send it as `Authorization: Bearer <token>`.
