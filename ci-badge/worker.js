const REPOSITORY = "experiencingtheview/offgrid";
const WORKFLOW_URL =
  `https://github.com/${REPOSITORY}/actions/workflows/tests.yml`;
const MAX_BODY_BYTES = 4096;
const COLORS = {
  passing: "#2ea44f",
  failing: "#cf222e",
  unknown: "#6e7781",
};

let schemaReady = false;

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

async function ensureSchema(env) {
  if (schemaReady) {
    return;
  }
  await env.DB.prepare(
    `CREATE TABLE IF NOT EXISTS ci_status (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      status TEXT NOT NULL CHECK (status IN ('passing', 'failing')),
      sha TEXT NOT NULL,
      run_id INTEGER NOT NULL,
      updated_at TEXT NOT NULL
    )`
  ).run();
  schemaReady = true;
}

async function getStatus(env) {
  await ensureSchema(env);
  return env.DB.prepare(
    "SELECT status, sha, run_id, updated_at FROM ci_status WHERE id = 1"
  ).first();
}

function labelFor(status) {
  return status === "passing"
    ? "passing"
    : status === "failing"
      ? "failing"
      : "no status";
}

function badgeSvg(row) {
  const status = row && row.status ? row.status : "unknown";
  const label = labelFor(status);
  const color = COLORS[status] || COLORS.unknown;
  const width = 148;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="20" role="img" aria-label="tests: ${label}">
  <title>tests: ${label}</title>
  <rect width="${width}" height="20" rx="3" fill="#555"/>
  <rect x="48" width="100" height="20" rx="3" fill="${color}"/>
  <rect x="48" width="8" height="20" fill="${color}"/>
  <g fill="#fff" text-anchor="middle" font-family="Verdana, Geneva, sans-serif" font-size="11">
    <text x="24" y="14">tests</text>
    <text x="98" y="14">${label}</text>
  </g>
</svg>`;
}

function page(row) {
  const status = row && row.status ? row.status : "unknown";
  const label = labelFor(status);
  const color = COLORS[status] || COLORS.unknown;
  const sha = row && row.sha ? row.sha.slice(0, 12) : "not published yet";
  const run = row && row.run_id ? String(row.run_id) : "not available";
  const updated = row && row.updated_at
    ? new Date(row.updated_at).toUTCString()
    : "No result has been published yet.";
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OffGrid CI status</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; background: #0d1117; color: #c9d1d9; font: 16px/1.5 system-ui, sans-serif; }
    main { max-width: 38rem; margin: 12vh auto; padding: 2rem; }
    h1 { color: #f0f6fc; font-size: 1.7rem; margin: 0 0 .25rem; }
    .sub { color: #8b949e; margin-top: 0; }
    .status { display: inline-block; margin: 1.5rem 0; padding: .35rem .8rem; border-radius: 999px;
      background: ${color}; color: #fff; font-weight: 700; }
    dl { display: grid; grid-template-columns: max-content 1fr; gap: .5rem 1rem; }
    dt { color: #8b949e; } dd { margin: 0; font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
    a { color: #58a6ff; }
  </style>
</head>
<body>
  <main>
    <h1>OffGrid CI</h1>
    <p class="sub">Public read-only status for the main branch.</p>
    <p class="status">${label}</p>
    <dl>
      <dt>commit</dt><dd>${sha}</dd>
      <dt>run</dt><dd>${run}</dd>
      <dt>updated</dt><dd>${updated}</dd>
    </dl>
    <p><a href="${WORKFLOW_URL}">Workflow runs on GitHub</a> · <a href="/badge.svg">badge.svg</a> · <a href="/status.json">status.json</a></p>
  </main>
</body>
</html>`;
}

function tokenMatches(given, expected) {
  if (!given || !expected || given.length !== expected.length) {
    return false;
  }
  let difference = 0;
  for (let i = 0; i < given.length; i += 1) {
    difference |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return difference === 0;
}

async function updateStatus(request, env) {
  if (!env.STATUS_TOKEN) {
    return json({ error: "status token is not configured" }, 503);
  }
  const header = request.headers.get("authorization") || "";
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!tokenMatches(provided, env.STATUS_TOKEN)) {
    return json({ error: "unauthorized" }, 401);
  }

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) {
    return json({ error: "request body is too large" }, 413);
  }
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }
  const status = body && body.status;
  const sha = body && typeof body.sha === "string" ? body.sha.toLowerCase() : "";
  const runId = body && Number(body.run_id);
  if (status !== "passing" && status !== "failing") {
    return json({ error: "status must be passing or failing" }, 400);
  }
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    return json({ error: "sha must be a 40-character commit SHA" }, 400);
  }
  if (!Number.isSafeInteger(runId) || runId <= 0) {
    return json({ error: "run_id must be a positive integer" }, 400);
  }

  await ensureSchema(env);
  const updatedAt = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO ci_status (id, status, sha, run_id, updated_at)
     VALUES (1, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       status = excluded.status,
       sha = excluded.sha,
       run_id = excluded.run_id,
       updated_at = excluded.updated_at
     WHERE excluded.run_id >= ci_status.run_id`
  ).bind(status, sha, runId, updatedAt).run();
  return json({ ok: true, status, sha, run_id: runId, updated_at: updatedAt });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/badge.svg") {
      return new Response(badgeSvg(await getStatus(env)), {
        headers: {
          "content-type": "image/svg+xml; charset=utf-8",
          "cache-control": "public, max-age=60, s-maxage=60",
          "x-content-type-options": "nosniff",
        },
      });
    }

    if (request.method === "GET" && url.pathname === "/status.json") {
      return json((await getStatus(env)) || { status: "unknown" });
    }

    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(page(await getStatus(env)), {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-cache",
          "x-content-type-options": "nosniff",
        },
      });
    }

    if (request.method === "POST" && url.pathname === "/status") {
      return updateStatus(request, env);
    }

    return json({ error: "not found" }, 404);
  },
};
