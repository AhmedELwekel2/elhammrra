"use strict";

/**
 * AI reports — proxies the Python news agent's POST /reports/{type}, records
 * every generated report in Postgres (shared library), enforces per-user daily
 * limits, and serves the resulting PDF back THROUGH the gateway so the browser
 * never contacts the Python service directly.
 */
const express = require("express");
const config = require("../config");
const { authenticate } = require("../middleware/auth");
const { currentCount, incrementUsage } = require("../middleware/usage");
const { newsReports, news, upstreamError } = require("../services/proxy");
const { Report } = require("../models");

const router = express.Router();
const TYPES = new Set(["daily", "weekly", "monthly", "magazine"]);

function toolForType(type) {
  return type === "magazine" ? "magazine" : `report_${type}`;
}

function basenameFromUrl(u) {
  if (!u) return null;
  const clean = String(u).split("?")[0];
  const parts = clean.split(/[\\/]/);
  return parts[parts.length - 1] || null;
}

// POST /api/reports/:type?format=file|pdf|json
router.post("/reports/:type", authenticate, async (req, res, next) => {
  const { type } = req.params;
  if (!TYPES.has(type)) {
    return res.status(422).json({ error: "نوع التقرير غير صحيح." });
  }
  const format = req.query.format || "file";
  if (!["file", "pdf", "json"].includes(format)) {
    return res.status(422).json({ error: "صيغة غير مدعومة (file | pdf | json)." });
  }

  const tool = toolForType(type);
  const limit = config.usageLimits[tool];
  if (limit && (await currentCount(req.user.uid, tool)) >= limit) {
    return res
      .status(429)
      .json({ error: `لقد تجاوزت الحد اليومي لهذا التقرير (${limit}). حاول مجددًا غدًا.`, tool, limit });
  }

  const body = req.body && Object.keys(req.body).length ? req.body : undefined;

  try {
    // format=pdf → stream the binary straight through.
    if (format === "pdf") {
      const upstream = await newsReports.post(`/reports/${type}`, body, {
        params: { format: "pdf" },
        responseType: "stream",
      });
      res.set("Content-Type", "application/pdf");
      await incrementUsage(req.user.uid, tool);
      await Report.create({
        firebaseUid: req.user.uid,
        type,
        format,
        status: "ok",
        params: body || null,
      });
      return upstream.data.pipe(res);
    }

    // format=file | json → JSON response.
    const upstream = await newsReports.post(`/reports/${type}`, body, {
      params: { format },
    });
    const data = upstream.data || {};

    let downloadUrl = null;
    if (format === "file") {
      const fname = basenameFromUrl(data.download_url) || basenameFromUrl(data.pdf_path);
      downloadUrl = fname ? `${config.publicBaseUrl}/api/files/${fname}` : null;
      data.download_url = downloadUrl; // rewrite so the browser hits the gateway
    }

    await incrementUsage(req.user.uid, tool);
    await Report.create({
      firebaseUid: req.user.uid,
      type,
      format,
      status: data.status || "ok",
      articleCount: data.article_count ?? null,
      enhancedCount: data.enhanced_count ?? null,
      pdfPath: data.pdf_path || null,
      downloadUrl,
      params: body || null,
    });

    return res.json(data);
  } catch (e) {
    if (e.response || e.code) {
      const { status, detail } = upstreamError(e);
      return res.status(status).json({ error: detail });
    }
    return next(e);
  }
});

// GET /api/reports — shared library of generated reports (all users).
router.get("/reports", authenticate, async (req, res, next) => {
  try {
    const rows = await Report.findAll({
      order: [["createdAt", "DESC"]],
      limit: 100,
    });
    res.json({ count: rows.length, reports: rows });
  } catch (e) {
    next(e);
  }
});

// GET /api/files/:name — proxy a generated PDF from the news agent.
router.get("/files/:name", authenticate, async (req, res, next) => {
  const name = req.params.name;
  if (!/^[\w.\-]+\.pdf$/i.test(name)) {
    return res.status(400).json({ error: "اسم ملف غير صالح." });
  }
  try {
    const upstream = await news.get(`/files/${encodeURIComponent(name)}`, {
      responseType: "stream",
    });
    res.set("Content-Type", "application/pdf");
    res.set("Content-Disposition", `inline; filename="${name}"`);
    return upstream.data.pipe(res);
  } catch (e) {
    if (e.response || e.code) {
      const { status, detail } = upstreamError(e);
      return res.status(status).json({ error: detail });
    }
    return next(e);
  }
});

module.exports = router;
