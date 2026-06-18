"use strict";

const express = require("express");
const { news, rag, sops } = require("../services/proxy");
const { sequelize } = require("../models");

const router = express.Router();

async function ping(client) {
  try {
    const r = await client.get("/health", { timeout: 4000 });
    return { up: true, ...r.data };
  } catch (e) {
    return { up: false, error: e.message };
  }
}

// GET /api/health — gateway + DB + the three upstream services.
router.get("/health", async (req, res) => {
  let db = "ok";
  try {
    await sequelize.authenticate();
  } catch (e) {
    db = `error: ${e.message}`;
  }

  const [newsHealth, ragHealth, sopsHealth] = await Promise.all([
    ping(news),
    ping(rag),
    ping(sops),
  ]);

  res.json({
    status: "ok",
    db,
    services: { news: newsHealth, rag: ragHealth, sops: sopsHealth },
  });
});

module.exports = router;
