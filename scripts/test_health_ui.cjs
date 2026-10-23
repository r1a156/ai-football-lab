"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const crypto = require("node:crypto");
const vm = require("node:vm");

const app = fs.readFileSync("assets/app.js", "utf8");
const index = fs.readFileSync("index.html", "utf8");
const start = app.indexOf("function renderHealth(");
const end = app.indexOf("function renderModelStats(", start);
assert(start >= 0 && end > start, "Health renderer must exist");

function checkHealth(quotaPlan, expectedStatus, expectedQuota) {
  const healthGrid = { innerHTML: "" };
  const fields = {};
  const state = { meta: { updatedAt: "2026-10-23T07:51:17Z", apiHealth: { status: "GREEN" } } };
  const report = { diagnostics: {
    freeDataMesh: { sources: { a: "GREEN", b: "GREEN", c: "GREEN", d: "GREEN", e: "RED" } },
    theSportsDbCurrentHistory: { status: "GREEN" },
    quotaPlan,
  } };
  const context = {
    state, report,
    document: { getElementById: id => id === "healthGrid" ? healthGrid : null },
    object: value => value && typeof value === "object" && !Array.isArray(value) ? value : {},
    sourceLabel: value => value,
    healthClass: value => value,
    healthLabel: value => value,
    escapeHtml: value => String(value),
    setText: (key, value) => { fields[key] = value; },
    formatShortDateTime: value => value,
    number: value => Number(value) || 0,
    formatNumber: value => String(value),
  };
  vm.runInNewContext(app.slice(start, end) + "\nrenderHealth(state, report);", context);
  assert.match(healthGrid.innerHTML, /Котировки букмекеров/, "Odds provider must be visible after all other sources");
  const oddsRow = healthGrid.innerHTML.split("Котировки букмекеров")[1];
  assert(oddsRow.includes(expectedStatus), "Odds provider status must match quota evidence");
  assert.equal(fields.quotaStatus, expectedQuota);
}

checkHealth({ quotaExhaustedAfterAcquisition: true, quotaRemainingBeforeOdds: 0 }, "LIMIT", "Квота: 0 · лимит исчерпан");
checkHealth({ quotaLifecycleStatus: "WAITING_FOR_ODDS_QUOTA_RESET", quotaRemainingBeforeOdds: 0 }, "LIMIT", "Квота: 0 · лимит исчерпан");
checkHealth({ quotaExhaustedAfterAcquisition: false, quotaRemainingBeforeOdds: 12 }, "GREEN", "Квота: 12");
checkHealth({}, "GREEN", "Квота: нет данных");

const blobSha = crypto.createHash("sha1")
  .update("blob " + Buffer.byteLength(app) + "\0")
  .update(app).digest("hex").slice(0, 12);
assert(index.includes("assets/app.js?v=" + blobSha), "HTML must reference current JS Git blob SHA for cache busting");
console.log("UI_HEALTH_REGRESSION=GREEN (5 checks)");
