"use strict";

/**
 * The two chat tools (CRAG Hajj agent + SOPs Chat Bot).
 *
 * Each keeps the 5 most recent turns per user, per service, in Postgres
 * (`ask_log`). Those recent turns are replayed to the UI on reopen via
 * GET /recent. Legacy /ask services also receive the turns as lightweight
 * follow-up context. hAJJJJ gets the current question only, because its CRAG
 * scraper uses the message text directly as an external search query.
 */
const express = require("express");
const config = require("../config");
const { authenticate } = require("../middleware/auth");
const { checkUsage, incrementUsage } = require("../middleware/usage");
const { rag, sops, upstreamError } = require("../services/proxy");
const { AskLog } = require("../models");

const router = express.Router();
const LIMIT = config.recentMessagesLimit;

// service => { client, tool }
const SERVICES = {
  rag: { client: rag, tool: "ask" },
  sops: { client: sops, tool: "sops" },
};

async function recentTurns(uid, service) {
  const rows = await AskLog.findAll({
    where: { firebaseUid: uid, service },
    order: [["createdAt", "DESC"]],
    limit: LIMIT,
  });
  return rows; // newest-first
}

function buildContextualQuestion(question, turnsNewestFirst) {
  if (!turnsNewestFirst.length) return question;
  // Oldest → newest for natural reading order.
  const lines = turnsNewestFirst
    .slice()
    .reverse()
    .map((t) => `س: ${t.question}\nج: ${t.answer || ""}`)
    .join("\n\n");
  return `سياق المحادثة السابقة (للمساعدة في فهم الأسئلة المترابطة فقط):\n${lines}\n\nالسؤال الحالي: ${question}`;
}

function normalizeSources(sources) {
  if (!Array.isArray(sources)) return [];
  return sources.map((s) => ({
    title: s.title || s.name || "",
    location: s.location || s.url || s.source || "",
    origin: s.origin || "",
  }));
}

async function askUpstream(service, client, question, k) {
  if (service === "rag" && config.services.ragContract === "chat") {
    const upstream = await client.post("/chat", { message: question });
    const data = upstream.data || {};
    return {
      answer: data.answer || "",
      sources: normalizeSources(data.sources),
      routing: data.routing || undefined,
    };
  }

  const upstream = await client.post("/ask", {
    question,
    ...(k ? { k } : {}),
  });
  const data = upstream.data || {};
  return {
    answer: data.answer || "",
    sources: normalizeSources(data.sources),
  };
}

function makeAskHandler(service) {
  const { client, tool } = SERVICES[service];
  return async function (req, res, next) {
    const question = (req.body && req.body.question ? String(req.body.question) : "").trim();
    const k = req.body && req.body.k ? req.body.k : undefined;
    if (!question) {
      return res.status(422).json({ error: "السؤال مطلوب." });
    }
    try {
      const recent = await recentTurns(req.user.uid, service);
      const contextual = buildContextualQuestion(question, recent);
      const upstreamQuestion =
        service === "rag" && config.services.ragContract === "chat"
          ? question
          : contextual;

      const data = await askUpstream(service, client, upstreamQuestion, k);

      await AskLog.create({
        firebaseUid: req.user.uid,
        service,
        question, // store the ORIGINAL question, not the context-augmented one
        answer: data.answer || "",
        sources: data.sources || [],
      });
      await incrementUsage(req.user.uid, tool);

      return res.json({
        answer: data.answer,
        sources: data.sources || [],
        ...(data.routing ? { routing: data.routing } : {}),
      });
    } catch (e) {
      if (e.response || e.code) {
        const { status, detail } = upstreamError(e);
        return res.status(status).json({ error: detail });
      }
      return next(e);
    }
  };
}

function makeRecentHandler(service) {
  return async function (req, res, next) {
    try {
      const rows = await recentTurns(req.user.uid, service);
      res.json({
        count: rows.length,
        turns: rows.map((r) => ({
          id: r.id,
          question: r.question,
          answer: r.answer,
          sources: r.sources || [],
          createdAt: r.createdAt,
        })),
      });
    } catch (e) {
      next(e);
    }
  };
}

// CRAG Hajj agent
router.post("/ask", authenticate, checkUsage("ask"), makeAskHandler("rag"));
router.get("/ask/recent", authenticate, makeRecentHandler("rag"));

// SOPs Chat Bot
router.post("/sops/ask", authenticate, checkUsage("sops"), makeAskHandler("sops"));
router.get("/sops/recent", authenticate, makeRecentHandler("sops"));

module.exports = router;
