"use strict";

const config = require("./config");
const app = require("./app");
const { sequelize } = require("./models");

async function start() {
  // Start listening FIRST so the server (and CORS/preflight) is always
  // reachable — even if the DB is momentarily down. DB-backed routes will then
  // return a clear error instead of the browser seeing a failed preflight.
  const server = app.listen(config.port, () => {
    // eslint-disable-next-line no-console
    console.log(`[gateway] listening on :${config.port} (auth ${config.authDisabled ? "DISABLED" : "enabled"})`);
  });

  server.on("error", (err) => {
    if (err.code === "EADDRINUSE") {
      // eslint-disable-next-line no-console
      console.error(
        `\n[gateway] ❌ Port ${config.port} is already in use by another app.` +
        `\n[gateway] Pick a free PORT in server/.env, set the same URL in the platform's` +
        `\n[gateway] js/config.js -> API_CONFIG.toolsBaseUrl, then restart.\n`
      );
      process.exit(1);
    }
    throw err;
  });

  try {
    await sequelize.authenticate();
    // Create the 5 gateway tables if missing. We intentionally do NOT use
    // { alter: true }: on SQLite, repeated alters drift composite unique indexes
    // into broken single-column constraints. For schema changes, recreate the
    // dev DB file (or use migrations in production).
    await sequelize.sync();
    // eslint-disable-next-line no-console
    console.log("[db] connected and synced");
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error(
      "\n[db] ⚠️  Could not connect to PostgreSQL:",
      e.message,
      "\n[db] The gateway is running, but data routes will fail until Postgres is up.",
      `\n[db] Check DATABASE_URL in server/.env (current: ${config.databaseUrl.replace(/:[^:@/]*@/, ":****@")})\n`
    );
  }
}

start();
