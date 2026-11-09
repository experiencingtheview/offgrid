// Tests for the freshness rule in the public badge worker.
//
// A result older than STALE_AFTER_MS must stop the badge vouching for main:
// the JSON, the SVG and the status page all have to say so. These tests call
// the real fetch handler with a stubbed D1 binding, so the code under test is
// the code Cloudflare runs.
import assert from "node:assert/strict";
import test from "node:test";

import worker from "./worker.js";

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;
const STALE_NOTE = "No result has been published for more than a week";

function stored(overrides = {}) {
  return {
    status: "passing",
    sha: "a".repeat(40),
    run_id: 42627862092,
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function agedBy(milliseconds, overrides = {}) {
  return stored({
    updated_at: new Date(Date.now() - milliseconds).toISOString(),
    ...overrides,
  });
}

function envWith(row) {
  return {
    DB: {
      prepare() {
        return {
          async run() {
            return {};
          },
          async first() {
            return row;
          },
        };
      },
    },
  };
}

async function responseFor(path, row) {
  return worker.fetch(new Request(`https://badge.example${path}`), envWith(row));
}

async function jsonFor(path, row) {
  return (await responseFor(path, row)).json();
}

async function textFor(path, row) {
  return (await responseFor(path, row)).text();
}

test("a status.json result inside the week is current", async () => {
  const body = await jsonFor("/status.json", agedBy(7 * DAY - HOUR));
  assert.equal(body.stale, false);
  assert.equal(body.status, "passing");
});

test("a status.json result older than a week is stale", async () => {
  const body = await jsonFor("/status.json", agedBy(7 * DAY + HOUR));
  assert.equal(body.stale, true);
});

test("a stale status.json still reports the last recorded result", async () => {
  const body = await jsonFor("/status.json", agedBy(9 * DAY, { status: "failing" }));
  assert.equal(body.stale, true);
  assert.equal(body.status, "failing");
  assert.equal(body.run_id, 42627862092);
});

test("an unreadable timestamp counts as stale", async () => {
  const body = await jsonFor("/status.json", stored({ updated_at: "last Tuesday" }));
  assert.equal(body.stale, true);
});

test("a status.json without a stored result is not stale", async () => {
  assert.deepEqual(await jsonFor("/status.json", null), {
    status: "unknown",
    stale: false,
  });
});

test("a fresh badge shows the recorded colour", async () => {
  const passing = await textFor("/badge.svg", stored());
  assert.match(passing, /tests: passing/);
  assert.match(passing, /#2ea44f/);

  const failing = await textFor("/badge.svg", stored({ status: "failing" }));
  assert.match(failing, /tests: failing/);
  assert.match(failing, /#cf222e/);
});

test("a stale badge stops claiming a pass", async () => {
  const svg = await textFor("/badge.svg", agedBy(8 * DAY));
  assert.match(svg, /tests: stale/);
  assert.match(svg, /#9a6700/);
  assert.doesNotMatch(svg, /#2ea44f/);
});

test("a badge without a stored result says so", async () => {
  const svg = await textFor("/badge.svg", null);
  assert.match(svg, /tests: no status/);
  assert.match(svg, /#6e7781/);
});

test("a fresh page does not warn about staleness", async () => {
  const html = await textFor("/", stored());
  assert.match(html, /class="status">passing</);
  assert.doesNotMatch(html, new RegExp(STALE_NOTE));
});

test("a stale page names the stale state and the recorded result", async () => {
  const html = await textFor("/", agedBy(8 * DAY, { status: "failing" }));
  assert.match(html, /class="status">stale</);
  assert.match(html, new RegExp(STALE_NOTE));
  assert.match(html, /last recorded result was <strong>failing<\/strong>/);
});

test("a page without a stored result has no stale warning", async () => {
  const html = await textFor("/", null);
  assert.match(html, /class="status">no status</);
  assert.match(html, /No result has been published yet\./);
  assert.doesNotMatch(html, new RegExp(STALE_NOTE));
});
