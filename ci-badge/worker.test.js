// Tests for the public badge worker.
//
// Freshness: a result older than STALE_AFTER_MS must stop the badge vouching
// for main, and the JSON, the SVG and the status page all have to say so.
//
// Writes: POST /status is how the Tests workflow publishes a result, so it has
// to fail closed on a missing or wrong token, refuse malformed input before D1
// sees it, and never let an older run overwrite a newer one.
//
// These tests call the real fetch handler; the write path runs against a real
// in-memory SQLite database through Node's built-in driver, so the code under
// test is the code Cloudflare runs.
import assert from "node:assert/strict";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";

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
// rejection below must happen before a statement reaches the database.
// ---------------------------------------------------------------------------

const TOKEN = "test-status-token-0123456789";
const COMMIT = "0123456789abcdef0123456789abcdef01234567";
const RUN_ID = 42630894266;
const VALID_BODY = { status: "passing", sha: COMMIT, run_id: RUN_ID };
const ORDERING_GUARD = "excluded.run_id >= ci_status.run_id";

let workerCopies = 0;

// A fresh worker instance per test. The worker remembers "schema created" at
// module level, and each test owns a new in-memory database that still needs
// its table, so every test imports its own copy.
async function freshWorker() {
  workerCopies += 1;
  const module = await import(`./worker.js?copy=${workerCopies}`);
  return module.default;
}

// A D1 binding over real SQLite, through Node's built-in driver. The upsert and
// its ON CONFLICT ... WHERE guard are evaluated by SQLite itself, so deleting
// the guard makes the older run overwrite and the ordering test fail, rather
// than a JavaScript stand-in agreeing with whatever the worker happened to
// send. D1's prepare().bind().run()/.first() shape is reproduced, and the
// statements are recorded so a rejected request can still show the database was
// never asked to write.
function sqliteDb() {
  const sqlite = new DatabaseSync(":memory:");
  const state = { statements: [], writes: [] };
  return {
    state,
    prepare(sql) {
      state.statements.push(sql);
      const statement = sqlite.prepare(sql);
      const bound = { args: [] };
      return {
        bind(...args) {
          bound.args = args;
          return this;
        },
        async run() {
          if (sql.includes("INSERT INTO ci_status")) {
            state.writes.push({ sql, args: bound.args });
          }
          statement.run(...bound.args);
          return { success: true, meta: {} };
        },
        async first() {
          return statement.get(...bound.args) ?? null;
        },
      };
    },
    close() {
      sqlite.close();
    },
  };
}

// The single row the public endpoints read, fetched through D1's interface.
// node:sqlite hands back null-prototype rows, so they are copied into ordinary
// objects before comparison.
async function readRow(db) {
  const row = await db
    .prepare("SELECT status, sha, run_id, updated_at FROM ci_status WHERE id = 1")
    .first();
  return row ? { ...row } : null;
}

async function postStatus(body, options = {}) {
  const db = options.db || sqliteDb();
  const worker = options.worker || (await freshWorker());
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

test("a valid status update is stored and echoed back", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status, body } = await postStatus(VALID_BODY, { db });
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.status, "passing");
  assert.equal(body.sha, COMMIT);
  assert.equal(body.run_id, RUN_ID);
  assert.ok(Number.isFinite(Date.parse(body.updated_at)));
  assert.deepEqual(await readRow(db), {
    status: "passing",
    sha: COMMIT,
    run_id: RUN_ID,
    updated_at: body.updated_at,
  });
});

test("an uppercase SHA is stored lowercased", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status, body } = await postStatus(
    { ...VALID_BODY, sha: COMMIT.toUpperCase() },
    { db }
  );
  assert.equal(status, 200);
  assert.equal(body.sha, COMMIT);
  assert.equal((await readRow(db)).sha, COMMIT);
});

test("a wrong token is rejected before anything is written", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  await postStatus(VALID_BODY, { db });
  const recorded = await readRow(db);

  const { status, body } = await postStatus(
    { status: "failing", sha: "b".repeat(40), run_id: RUN_ID + 1 },
    { db, headers: { authorization: "Bearer not-the-token" } }
  );
  assert.equal(status, 401);
  assert.deepEqual(body, { error: "unauthorized" });
  assert.deepEqual(await readRow(db), recorded);
  assert.equal(db.state.writes.length, 1, "only the seeding update may write");
});

test("a missing authorization header is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status, body } = await postStatus(VALID_BODY, { db, headers: {} });
  assert.equal(status, 401);
  assert.deepEqual(body, { error: "unauthorized" });
  assert.equal(db.state.statements.length, 0);
});

test("a non-bearer Authorization scheme is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status } = await postStatus(VALID_BODY, {
    db,
    headers: { authorization: `Basic ${TOKEN}` },
  });
  assert.equal(status, 401);
  assert.equal(db.state.statements.length, 0);
});

test("a token with the wrong length is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status } = await postStatus(VALID_BODY, {
    db,
    headers: { authorization: `Bearer ${TOKEN.slice(1)}` },
  });
  assert.equal(status, 401);
  assert.equal(db.state.statements.length, 0);
});

test("an unconfigured status token fails closed", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status, body } = await postStatus(VALID_BODY, { db, env: { DB: db } });
  assert.equal(status, 503);
  assert.deepEqual(body, { error: "status token is not configured" });
  assert.equal(db.state.statements.length, 0);
});

test("a body that is not JSON is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const { status, body } = await postStatus("{ not json", { db });
  assert.equal(status, 400);
  assert.deepEqual(body, { error: "body must be JSON" });
  assert.equal(db.state.writes.length, 0);
});

test("an oversized body is rejected before it is parsed", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  const oversized = JSON.stringify({ ...VALID_BODY, pad: "x".repeat(4096) });
  assert.ok(oversized.length > 4096);
  const { status, body } = await postStatus(oversized, { db });
  assert.equal(status, 413);
  assert.deepEqual(body, { error: "request body is too large" });
  assert.equal(db.state.writes.length, 0);
});

test("a status other than passing or failing is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  for (const value of ["unknown", "PASSING", "", null, undefined]) {
    const { status, body } = await postStatus(
      { ...VALID_BODY, status: value },
      { db }
    );
    assert.equal(status, 400, `status ${String(value)} must be rejected`);
    assert.equal(body.error, "status must be passing or failing");
    assert.equal(db.state.writes.length, 0);
  }
});

test("a SHA that is not 40 hex characters is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  for (const sha of ["", "abc", COMMIT.slice(1), `${COMMIT}0`, "z".repeat(40), 12345]) {
    const { status, body } = await postStatus({ ...VALID_BODY, sha }, { db });
    assert.equal(status, 400, `sha ${JSON.stringify(sha)} must be rejected`);
    assert.equal(body.error, "sha must be a 40-character commit SHA");
    assert.equal(db.state.writes.length, 0);
  }
});

test("a run_id that is not a positive integer is rejected", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());
  for (const runId of [undefined, null, "abc", 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 2]) {
    const { status, body } = await postStatus({ ...VALID_BODY, run_id: runId }, { db });
    assert.equal(status, 400, `run_id ${String(runId)} must be rejected`);
    assert.equal(body.error, "run_id must be a positive integer");
    assert.equal(db.state.writes.length, 0);
  }
});

test("the stored result is only replaced by the same or a newer run", async (t) => {
  const db = sqliteDb();
  t.after(() => db.close());

  const first = await postStatus(VALID_BODY, { db });
  assert.equal(first.status, 200);

  const older = await postStatus(
    { status: "failing", sha: "b".repeat(40), run_id: RUN_ID - 1 },
    { db }
  );
  assert.equal(older.status, 200);
  assert.deepEqual(
    await readRow(db),
    {
      status: "passing",
      sha: COMMIT,
      run_id: RUN_ID,
      updated_at: first.body.updated_at,
    },
    "an older run must not overwrite the newer result"
  );

  const replay = await postStatus(
    { status: "failing", sha: "c".repeat(40), run_id: RUN_ID },
    { db }
  );
  assert.equal(replay.status, 200);
  assert.deepEqual(await readRow(db), {
    status: "failing",
    sha: "c".repeat(40),
    run_id: RUN_ID,
    updated_at: replay.body.updated_at,
  });

  const upsert = db.state.writes.at(-1);
  assert.deepEqual(upsert.args.slice(0, 3), ["failing", "c".repeat(40), RUN_ID]);
  assert.ok(upsert.sql.includes(ORDERING_GUARD));
});
