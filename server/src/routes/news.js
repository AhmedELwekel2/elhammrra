"use strict";

/**
 * News listing — proxies the Python news agent's GET /news/{period} with a
 * short-TTL Postgres cache so repeated views don't re-scrape.
 *
 * Cache hits are served free; the per-user daily limit is only enforced on a
 * real upstream miss (a cheap cached read shouldn't consume quota).
 */
const express = require("express");
const config = require("../config");
const { authenticate } = require("../middleware/auth");
const { currentCount, incrementUsage } = require("../middleware/usage");
const { news, upstreamError } = require("../services/proxy");
const { NewsCache } = require("../models");

const router = express.Router();
const PERIODS = new Set(["daily", "weekly", "monthly"]);
const TOOL = "news";

// GET /api/news/:period?days=&category=&limit=
router.get("/news/:period", authenticate, async (req, res, next) => {
  const { period } = req.params;
  if (!PERIODS.has(period)) {
    return res.status(422).json({ error: "الفترة غير صحيحة (daily | weekly | monthly)." });
  }

  const { days, category, limit } = req.query;
  const cacheKey = `${period}|${category || ""}|${days || ""}|${limit || ""}`;

  try {
    // 1. Serve a fresh cache entry without touching the user's quota.
    const cached = await NewsCache.findOne({ where: { cacheKey } });
    if (cached) {
      const ageSec = (Date.now() - new Date(cached.fetchedAt).getTime()) / 1000;
      if (ageSec < config.newsCacheTtlSeconds) {
        res.set("X-Cache", "HIT");
        return res.json(cached.payload);
      }
    }

    // 2. Cache miss → enforce the daily limit before scraping upstream.
    const lim = config.usageLimits[TOOL];
    if (lim && (await currentCount(req.user.uid, TOOL)) >= lim) {
      return res.status(429).json({
        error: `لقد تجاوزت الحد اليومي لتصفّح الأخبار (${lim}). حاول مجددًا غدًا.`,
        tool: TOOL,
        limit: lim,
      });
    }

    const params = {};
    if (days) params.days = days;
    if (category) params.category = category;
    if (limit) params.limit = limit;

    const upstream = await news.get(`/news/${period}`, { params });
    const payload = upstream.data;

    await NewsCache.upsert({ cacheKey, payload, fetchedAt: new Date() });
    await incrementUsage(req.user.uid, TOOL);

    res.set("X-Cache", "MISS");
    return res.json(payload);
  } catch (e) {
    if (e.response || e.code) {
      const { status, detail } = upstreamError(e);
      return res.status(status).json({ error: detail });
    }
    return next(e);
  }
});

module.exports = router;
