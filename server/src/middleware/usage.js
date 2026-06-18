"use strict";

/**
 * Per-user, per-day usage limits backed by the Postgres `tool_usage` table.
 *
 * Usage:
 *   router.post("/reports/weekly", authenticate, checkUsage("report_weekly"), handler)
 * and, after the upstream call succeeds, the handler calls
 *   await incrementUsage(req.user.uid, req.usageTool)
 * so a failed/upstream-errored request does not consume the user's quota.
 */
const config = require("../config");
const { ToolUsage } = require("../models");

function today() {
  return new Date().toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
}

async function currentCount(uid, tool) {
  const row = await ToolUsage.findOne({
    where: { firebaseUid: uid, tool, windowDate: today() },
  });
  return row ? row.count : 0;
}

function checkUsage(tool) {
  return async function (req, res, next) {
    try {
      const limit = config.usageLimits[tool];
      req.usageTool = tool;
      if (!limit || limit <= 0) return next(); // unlimited
      const used = await currentCount(req.user.uid, tool);
      if (used >= limit) {
        return res.status(429).json({
          error: `لقد تجاوزت الحد اليومي لهذه الأداة (${limit} مرات). حاول مجددًا غدًا.`,
          tool,
          limit,
          used,
        });
      }
      req.usageRemaining = limit - used;
      return next();
    } catch (e) {
      return next(e);
    }
  };
}

async function incrementUsage(uid, tool) {
  // Usage accounting must NEVER break the actual feature. A failure here (e.g. a
  // transient DB hiccup) is logged and swallowed so the user's request succeeds.
  try {
    const [row] = await ToolUsage.findOrCreate({
      where: { firebaseUid: uid, tool, windowDate: today() },
      defaults: { count: 0 },
    });
    await row.increment("count", { by: 1 });
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn(`[usage] increment failed for ${uid}/${tool}: ${e.message}`);
  }
}

module.exports = { checkUsage, incrementUsage, currentCount };
