"use strict";

/**
 * Sequelize model registry. These five tables are the ONLY data the gateway
 * owns in Postgres — the platform's QMS data and auth stay in Firebase.
 */
const { sequelize, Sequelize } = require("../db");
const { DataTypes } = Sequelize;

// ── User: a thin mirror of the Firebase user, upserted on each authed call ────
const User = sequelize.define(
  "User",
  {
    firebaseUid: { type: DataTypes.STRING, primaryKey: true },
    email: { type: DataTypes.STRING },
    name: { type: DataTypes.STRING },
    role: { type: DataTypes.STRING },
    lastSeenAt: { type: DataTypes.DATE },
  },
  { tableName: "users", timestamps: true }
);

// ── ToolUsage: per-user, per-day counters (replaces user_usage.json) ──────────
const ToolUsage = sequelize.define(
  "ToolUsage",
  {
    id: { type: DataTypes.INTEGER, primaryKey: true, autoIncrement: true },
    firebaseUid: { type: DataTypes.STRING, allowNull: false },
    tool: { type: DataTypes.STRING, allowNull: false },
    windowDate: { type: DataTypes.DATEONLY, allowNull: false },
    count: { type: DataTypes.INTEGER, allowNull: false, defaultValue: 0 },
  },
  {
    tableName: "tool_usage",
    timestamps: true,
    indexes: [
      { unique: true, fields: ["firebaseUid", "tool", "windowDate"] },
    ],
  }
);

// ── Report: a record of every generated report (shared library) ───────────────
const Report = sequelize.define(
  "Report",
  {
    id: { type: DataTypes.INTEGER, primaryKey: true, autoIncrement: true },
    firebaseUid: { type: DataTypes.STRING, allowNull: false },
    type: { type: DataTypes.STRING, allowNull: false }, // daily|weekly|monthly|magazine
    format: { type: DataTypes.STRING }, // file|pdf|json
    status: { type: DataTypes.STRING, defaultValue: "ok" },
    articleCount: { type: DataTypes.INTEGER },
    enhancedCount: { type: DataTypes.INTEGER },
    pdfPath: { type: DataTypes.TEXT },
    downloadUrl: { type: DataTypes.TEXT },
    params: { type: DataTypes.JSON },
  },
  {
    tableName: "reports",
    timestamps: true,
    indexes: [{ fields: ["createdAt"] }, { fields: ["firebaseUid"] }],
  }
);

// ── NewsCache: short-TTL cache of /news/* responses ───────────────────────────
const NewsCache = sequelize.define(
  "NewsCache",
  {
    id: { type: DataTypes.INTEGER, primaryKey: true, autoIncrement: true },
    cacheKey: { type: DataTypes.STRING, allowNull: false, unique: true },
    payload: { type: DataTypes.JSON, allowNull: false },
    fetchedAt: { type: DataTypes.DATE, allowNull: false },
  },
  { tableName: "news_cache", timestamps: true }
);

// ── AskLog: every RAG/SOPs Q&A turn; also the per-user chat memory store ───────
const AskLog = sequelize.define(
  "AskLog",
  {
    id: { type: DataTypes.INTEGER, primaryKey: true, autoIncrement: true },
    firebaseUid: { type: DataTypes.STRING, allowNull: false },
    service: { type: DataTypes.STRING, allowNull: false }, // rag|sops
    question: { type: DataTypes.TEXT, allowNull: false },
    answer: { type: DataTypes.TEXT },
    sources: { type: DataTypes.JSON },
  },
  {
    tableName: "ask_log",
    timestamps: true,
    indexes: [{ fields: ["firebaseUid", "service", "createdAt"] }],
  }
);

module.exports = { sequelize, User, ToolUsage, Report, NewsCache, AskLog };
