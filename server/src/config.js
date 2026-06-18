"use strict";

/**
 * Centralised configuration, read once from the environment at startup.
 * Load .env from the server/ root regardless of the current working directory.
 */
const path = require("path");
require("dotenv").config({ path: path.join(__dirname, "..", ".env") });

function bool(v, def = false) {
  if (v === undefined || v === null || v === "") return def;
  return String(v).toLowerCase() === "true" || v === "1";
}

function int(v, def) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
}

const config = {
  port: int(process.env.PORT, 4000),
  publicBaseUrl: (process.env.PUBLIC_BASE_URL || "http://127.0.0.1:4000").replace(/\/+$/, ""),
  corsOrigin: (process.env.CORS_ORIGIN || "*")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),

  databaseUrl: process.env.DATABASE_URL || "postgres://postgres:postgres@127.0.0.1:5432/elhamra_tools",
  dbSsl: bool(process.env.DB_SSL, false),

  firebaseProjectId: process.env.FIREBASE_PROJECT_ID || undefined,
  authDisabled: bool(process.env.AUTH_DISABLED, false),
  devFakeUid: process.env.DEV_FAKE_UID || "dev-user",
  devFakeRole: process.env.DEV_FAKE_ROLE || "supervisor",

  services: {
    news: (process.env.NEWS_AGENT_URL || "http://127.0.0.1:8010").replace(/\/+$/, ""),
    rag: (process.env.RAG_URL || "http://127.0.0.1:8020").replace(/\/+$/, ""),
    ragContract: (process.env.RAG_CONTRACT || "chat").toLowerCase(),
    sops: (process.env.CHATBOT_URL || "http://127.0.0.1:8030").replace(/\/+$/, ""),
  },

  proxyTimeoutMs: int(process.env.PROXY_TIMEOUT_MS, 30000),
  reportTimeoutMs: int(process.env.REPORT_TIMEOUT_MS, 600000),
  newsCacheTtlSeconds: int(process.env.NEWS_CACHE_TTL_SECONDS, 600),

  /**
   * Per-user, per-day usage limits keyed by "tool".
   * Ported from telegram_bot_hajj.py (weekly 4, monthly 2, magazine 2) and
   * extended for the gateway's own tools. 0 / undefined => unlimited.
   */
  usageLimits: {
    news: 200, // GET /api/news/* listing (cheap + cached)
    report_daily: 10,
    report_weekly: 4,
    report_monthly: 2,
    magazine: 2,
    ask: 100, // CRAG Hajj agent
    sops: 100, // SOPs Chat Bot
  },

  // How many recent chat turns to keep/replay per user, per RAG service.
  recentMessagesLimit: 5,
};

module.exports = config;
