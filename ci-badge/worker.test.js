// Tests for the public badge worker.
//
// Freshness: a result older than STALE_AFTER_MS must stop the badge vouching
// for main, and the JSON, the SVG and the status page all have to say so.
//
// Writes: POST /status is how the Tests workflow publishes a result, so it has
// to fail closed on a missing or wrong token, refuse malformed input before D1
// sees it, and never let an older run overwrite a newer one.
//
// These tests call the real fetch handler with a stubbed D1 binding, so the
// code under test is the code Cloudflare runs.
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

// ---------------------------------------------------------------------------
// POST /status: the write path. The endpoint is reachable by anyone, so every
// rejection below must happen before a statement reaches D1.
// ---------------------------------------------------------------------------

const TOKEN = "test-status-token-0123456789";
const COMMIT = "0123456789abcdef0123456789abcdef01234567";
const RUN_ID = 42630894266;
const VALID_BODY = { status: "passing", sha: COMMIT, run_id: RUN_ID };
const ORDERING_GUARD = "excluded.run_id >= ci_status.run_id";

// A stub D1 that keeps the single row the worker reads and writes, and that
// honours the upsert ordering guard only when the worker actually sends one:
// dropping the guard from the statement makes the older-run test overwrite and
// fail, so the ordering rule is covered by behaviour rather than by a mock that
// assumes it.
function fakeDb(row = null) {
  const state = {
    row: row ? { ...row } : null,
    writes: [],
    statements: [],
  };
  return {
    state,
    prepare(sql) {
      state.statements.push(sql);
      const statement = {
        args: [],
        bind(...args) {
          statement.args = args;
          return statement;
        },
        async run() {
          if (sql.startsWith("CREATE TABLE")) {
            return {};
          }
          state.writes.push({ sql, args: statement.args });
          const [status, sha, runId, updatedAt] = statement.args;
          const guarded = sql.includes(ORDERING_GUARD);
          if (!guarded || !state.row || runId >= state.row.run_id) {
            state.row = { status, sha, run_id: runId, updated_at: updatedAt };
          }
          return {};
        },
        async first() {
          return state.row ? { ...state.row } : null;
        },
      };
      return statement;
    },
  };
}

async function postStatus(body, options = {}) {
  const db = options.db || fakeDb();
  const env = options.env || { DB: db, STATUS_TOKEN: TOKEN };
  const headers = options.headers || { authorization: `Bearer ${TOKEN}` };
  const request = new Request("https://badge.example/status", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  const response = await worker.fetch(request, env);
  return { status: response.status, body: await response.json(), db };
}

function storedRow(overrides = {}) {
  return {
    status: "passing",
    sha: COMMIT,
    run_id: RUN_ID,
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

test("a valid status update is stored and echoed back", async () => {
  const db = fakeDb();
  const { status, body } = await postStatus(VALID_BODY, { db });
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.status, "passing");
  assert.equal(body.sha, COMMIT);
  assert.equal(body.run_id, RUN_ID);
  assert.ok(Number.isFinite(Date.parse(body.updated_at)));
  assert.deepEqual(db.state.row, {
    status: "passing",
    sha: COMMIT,
    run_id: RUN_ID,
    updated_at: body.updated_at,
  });
});

test("an uppercase SHA is stored lowercased", async () => {
  const db = fakeDb();
  const { status, body } = await postStatus(
    { ...VALID_BODY, sha: COMMIT.toUpperCase() },
    { db }
  );
  assert.equal(status, 200);
  assert.equal(body.sha, COMMIT);
  assert.equal(db.state.row.sha, COMMIT);
});

test("a wrong token is rejected before anything is written", async () => {
  const db = fakeDb(storedRow({ status: "failing", sha: "b".repeat(40) }));
  const { status, body } = await postStatus(VALID_BODY, {
    db,
    headers: { authorization: "Bearer not-the-token" },
  });
  assert.equal(status, 401);
  assert.deepEqual(body, { error: "unauthorized" });
  assert.equal(db.state.statements.length, 0);
  assert.equal(db.state.row.status, "failing");
});

test("a missing authorization header is rejected", async () => {
  const db = fakeDb();
  const { status, body } = await postStatus(VALID_BODY, { db, headers: {} });
  assert.equal(status, 401);
  assert.deepEqual(body, { error: "unauthorized" });
  assert.equal(db.state.statements.length, 0);
});

test("a non-bearer Authorization scheme is rejected", async () => {
  const db = fakeDb();
  const { status } = await postStatus(VALID_BODY, {
    db,
    headers: { authorization: `Basic ${TOKEN}` },
  });
  assert.equal(status, 401);
  assert.equal(db.state.statements.length, 0);
});

test("a token with the wrong length is rejected", async () => {
  const db = fakeDb();
  const { status } = await postStatus(VALID_BODY, {
    db,
    headers: { authorization: `Bearer ${TOKEN.slice(1)}` },
  });
  assert.equal(status, 401);
  assert.equal(db.state.statements.length, 0);
});

test("an unconfigured status token fails closed", async () => {
  const db = fakeDb();
  const { status, body } = await postStatus(VALID_BODY, { db, env: { DB: db } });
  assert.equal(status, 503);
  assert.deepEqual(body, { error: "status token is not configured" });
  assert.equal(db.state.statements.length, 0);
});

test("a body that is not JSON is rejected", async () => {
  const db = fakeDb();
  const { status, body } = await postStatus("{ not json", { db });
  assert.equal(status, 400);
  assert.deepEqual(body, { error: "body must be JSON" });
  assert.equal(db.state.writes.length, 0);
});

test("an oversized body is rejected before it is parsed", async () => {
  const db = fakeDb();
  const oversized = JSON.stringify({ ...VALID_BODY, pad: "x".repeat(4096) });
  assert.ok(oversized.length > 4096);
  const { status, body } = await postStatus(oversized, { db });
  assert.equal(status, 413);
  assert.deepEqual(body, { error: "request body is too large" });
  assert.equal(db.state.writes.length, 0);
});

test("a status other than passing or failing is rejected", async () => {
  for (const value of ["unknown", "PASSING", "", null, undefined]) {
    const db = fakeDb();
    const { status, body } = await postStatus(
      { ...VALID_BODY, status: value },
      { db }
    );
    assert.equal(status, 400, `status ${String(value)} must be rejected`);
    assert.equal(body.error, "status must be passing or failing");
    assert.equal(db.state.writes.length, 0);
  }
});

test("a SHA that is not 40 hex characters is rejected", async () => {
  for (const sha of ["", "abc", COMMIT.slice(1), `${COMMIT}0`, "z".repeat(40), 12345]) {
    const db = fakeDb();
    const { status, body } = await postStatus({ ...VALID_BODY, sha }, { db });
    assert.equal(status, 400, `sha ${JSON.stringify(sha)} must be rejected`);
    assert.equal(body.error, "sha must be a 40-character commit SHA");
    assert.equal(db.state.writes.length, 0);
  }
});

test("a run_id that is not a positive integer is rejected", async () => {
  for (const runId of [undefined, null, "abc", 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 2]) {
    const db = fakeDb();
    const { status, body } = await postStatus({ ...VALID_BODY, run_id: runId }, { db });
    assert.equal(status, 400, `run_id ${String(runId)} must be rejected`);
    assert.equal(body.error, "run_id must be a positive integer");
    assert.equal(db.state.writes.length, 0);
  }
});

test("the stored result is only replaced by the same or a newer run", async () => {
  const recorded = new Date(Date.now() - 60 * 1000).toISOString();
  const db = fakeDb(
    storedRow({ status: "failing", sha: "b".repeat(40), updated_at: recorded })
  );

  const older = await postStatus({ ...VALID_BODY, run_id: RUN_ID - 1 }, { db });
  assert.equal(older.status, 200);
  assert.equal(db.state.row.run_id, RUN_ID, "an older run must not overwrite");
  assert.equal(db.state.row.status, "failing");
  assert.equal(db.state.row.updated_at, recorded);

  const replay = await postStatus(VALID_BODY, { db });
  assert.equal(replay.status, 200);
  assert.equal(db.state.row.status, "passing");
  assert.equal(db.state.row.run_id, RUN_ID);
  assert.equal(db.state.row.updated_at, replay.body.updated_at);

  const upsert = db.state.writes.at(-1);
  assert.deepEqual(upsert.args.slice(0, 3), ["passing", COMMIT, RUN_ID]);
  assert.ok(upsert.sql.includes(ORDERING_GUARD));
});
