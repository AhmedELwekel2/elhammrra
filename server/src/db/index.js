"use strict";

const path = require("path");
const fs = require("fs");
const { Sequelize } = require("sequelize");
const config = require("../config");

/**
 * The gateway is dialect-agnostic. It uses PostgreSQL in production (set
 * DATABASE_URL=postgres://…) and falls back to a local SQLite file for
 * zero-config local development (DATABASE_URL=sqlite:./data/gateway.sqlite).
 * Either way the same five tables and queries work — this data is the gateway's
 * own (usage, reports, news cache, chat history); the platform's QMS data stays
 * in Firebase.
 */
const url = config.databaseUrl || "";
let sequelize;

if (url.startsWith("sqlite")) {
  // sqlite:./data/gateway.sqlite | sqlite::memory:
  let storage = url.replace(/^sqlite:(\/\/)?/, "");
  if (!storage || storage === ":memory:") {
    storage = ":memory:";
  } else {
    storage = path.isAbsolute(storage) ? storage : path.join(__dirname, "..", "..", storage);
    fs.mkdirSync(path.dirname(storage), { recursive: true });
  }
  sequelize = new Sequelize({ dialect: "sqlite", storage, logging: false });
} else {
  sequelize = new Sequelize(url, {
    dialect: "postgres",
    logging: false,
    dialectOptions: config.dbSsl ? { ssl: { require: true, rejectUnauthorized: false } } : {},
    pool: { max: 10, min: 0, acquire: 30000, idle: 10000 },
  });
}

module.exports = { sequelize, Sequelize };
