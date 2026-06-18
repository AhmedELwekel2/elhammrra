"use strict";

/**
 * Authentication middleware.
 *
 * Reads `Authorization: Bearer <firebaseIdToken>`, verifies it with the Firebase
 * Admin SDK, then resolves the caller's role from Firestore `users/{uid}.role`
 * (cached in-memory for a short TTL). The verified profile is attached as
 * `req.user = { uid, email, name, role }` and mirrored into the Postgres `User`
 * table so usage/audit are trustworthy (role is never taken from the client).
 *
 * For local development without a service account, set AUTH_DISABLED=true to use
 * a fake user (DEV_FAKE_UID / DEV_FAKE_ROLE).
 */
const config = require("../config");
const { getAdmin } = require("../services/firebase");
const { User } = require("../models");

const ROLE_TTL_MS = 5 * 60 * 1000;
const roleCache = new Map(); // uid -> { role, name, email, ts }

async function resolveRole(admin, uid, token) {
  const cached = roleCache.get(uid);
  if (cached && Date.now() - cached.ts < ROLE_TTL_MS) return cached;

  let role = token.role || null; // honour a custom claim if present
  let name = token.name || null;
  const email = token.email || null;
  try {
    const snap = await admin.firestore().collection("users").doc(uid).get();
    if (snap.exists) {
      const data = snap.data() || {};
      role = data.role || role;
      name = data.name || name;
    }
  } catch (e) {
    // Firestore lookup is best-effort; fall back to claim/none.
    // eslint-disable-next-line no-console
    console.warn(`[auth] role lookup failed for ${uid}: ${e.message}`);
  }

  const entry = { role, name, email, ts: Date.now() };
  roleCache.set(uid, entry);
  return entry;
}

async function mirrorUser(user) {
  try {
    await User.upsert({
      firebaseUid: user.uid,
      email: user.email || null,
      name: user.name || null,
      role: user.role || null,
      lastSeenAt: new Date(),
    });
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn(`[auth] user upsert failed for ${user.uid}: ${e.message}`);
  }
}

async function authenticate(req, res, next) {
  try {
    if (config.authDisabled) {
      // Dev mode: trust a client-supplied identity hint (staff uid / pilgrim ID)
      // so usage + chat history are scoped per person. Spoofable — never use in
      // production; enable real token auth there.
      const hint = req.headers["x-tools-user"];
      const uid =
        typeof hint === "string" && hint.trim()
          ? hint.trim().replace(/[\r\n]/g, "").slice(0, 128)
          : config.devFakeUid;
      const isPilgrim = uid.startsWith("pilgrim:");
      req.user = {
        uid,
        email: `${config.devFakeUid}@dev.local`,
        name: isPilgrim ? "حاج" : "Dev User",
        role: isPilgrim ? "pilgrim" : config.devFakeRole,
      };
      await mirrorUser(req.user);
      return next();
    }

    const header = req.headers.authorization || "";
    const match = header.match(/^Bearer\s+(.+)$/i);
    if (!match) {
      return res.status(401).json({ error: "غير مصرح: رمز الدخول مفقود." });
    }

    const admin = getAdmin();
    const token = await admin.auth().verifyIdToken(match[1]);
    const profile = await resolveRole(admin, token.uid, token);

    req.user = {
      uid: token.uid,
      email: profile.email || token.email || null,
      name: profile.name || null,
      role: profile.role || null,
    };
    await mirrorUser(req.user);
    return next();
  } catch (e) {
    return res.status(401).json({ error: "غير مصرح: رمز الدخول غير صالح." });
  }
}

module.exports = { authenticate };
