"use strict";

const express = require("express");
const cors = require("cors");
const morgan = require("morgan");
const config = require("./config");

const healthRoutes = require("./routes/health");
const newsRoutes = require("./routes/news");
const reportRoutes = require("./routes/reports");
const askRoutes = require("./routes/ask");

const app = express();

// ── CORS ──────────────────────────────────────────────────────────────────
// In dev, CORS_ORIGIN=* reflects whatever origin the browser sends (so the
// platform works from http://localhost:5500, http://127.0.0.1:5500, file
// servers, etc.). In production, list the exact origin(s) in CORS_ORIGIN.
const allowAll = config.corsOrigin.length === 1 && config.corsOrigin[0] === "*";
const corsOptions = {
  origin: allowAll
    ? true // reflect request origin
    : function (origin, cb) {
        // Allow same-origin / non-browser (no Origin header) and any listed origin.
        if (!origin || config.corsOrigin.includes(origin)) return cb(null, true);
        return cb(null, false);
      },
  methods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
  allowedHeaders: ["Content-Type", "Authorization", "X-Tools-User"],
  credentials: false,
  optionsSuccessStatus: 204,
};
app.use(cors(corsOptions));
// Answer every preflight explicitly, before anything else can interfere.
app.options("*", cors(corsOptions));

app.use(express.json({ limit: "1mb" }));
app.use(morgan("tiny"));

// All gateway routes live under /api.
app.use("/api", healthRoutes);
app.use("/api", newsRoutes);
app.use("/api", reportRoutes);
app.use("/api", askRoutes);

// 404
app.use((req, res) => {
  res.status(404).json({ error: "المسار غير موجود." });
});

// Central error handler
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  // eslint-disable-next-line no-console
  console.error("[error]", err);
  res.status(err.status || 500).json({ error: "حدث خطأ داخلي في الخادم." });
});

module.exports = app;
