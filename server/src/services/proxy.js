"use strict";

/**
 * Thin axios clients for the three upstream Python services. The browser never
 * talks to these directly — only this gateway does.
 */
const axios = require("axios");
const config = require("../config");

const news = axios.create({ baseURL: config.services.news, timeout: config.proxyTimeoutMs });
const rag = axios.create({ baseURL: config.services.rag, timeout: config.proxyTimeoutMs });
const sops = axios.create({ baseURL: config.services.sops, timeout: config.proxyTimeoutMs });

// Report generation is long-running (1–6 min); give it a much larger timeout.
const newsReports = axios.create({ baseURL: config.services.news, timeout: config.reportTimeoutMs });

/** Normalise an upstream axios error into an { status, detail } pair. */
function upstreamError(e) {
  if (e.response) {
    const detail =
      (e.response.data && (e.response.data.detail || e.response.data.error)) ||
      e.response.statusText ||
      "upstream error";
    return { status: e.response.status, detail };
  }
  if (e.code === "ECONNABORTED") return { status: 504, detail: "انتهت مهلة الخدمة." };
  return { status: 502, detail: "تعذّر الوصول إلى الخدمة الخلفية." };
}

module.exports = { news, newsReports, rag, sops, upstreamError, axios };
