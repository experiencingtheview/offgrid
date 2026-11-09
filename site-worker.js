import page from "./index.html";
import ciBadge from "./ci-badge/worker.js";

const RELEASE = "https://github.com/experiencingtheview/offgrid/releases/latest/download";
const DOWNLOAD_ASSET = "offgrid-portable.zip";
const DIGEST_ELEMENT = /<code class="release-digest" data-release-digest>[^<]*<\/code>/;
const DIGEST_TIMEOUT_MS = 3000;

const HTML_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=300",
  "content-security-policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
  "referrer-policy": "strict-origin-when-cross-origin",
  "x-content-type-options": "nosniff",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "strict-transport-security": "max-age=31536000",
};

function methodNotAllowed() {
  return new Response("Method not allowed\n", {
    status: 405,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "allow": "GET, HEAD",
      "x-content-type-options": "nosniff",
    },
  });
}

// The page must not advertise a digest for a different build than the button
// serves, so it is read from the release's own SHA256SUMS instead of copied
// into this file. Any failure leaves the page saying where the file lives.
async function releaseDigest() {
  try {
    const response = await fetch(`${RELEASE}/SHA256SUMS`, {
      headers: { accept: "text/plain" },
      cf: { cacheTtl: 300, cacheEverything: true },
      signal: AbortSignal.timeout(DIGEST_TIMEOUT_MS),
    });
    if (!response.ok) {
      return "";
    }
    for (const line of (await response.text()).split("\n")) {
      const [digest, name] = line.trim().split(/\s+\*?/);
      if (/^[0-9a-f]{64}$/.test(digest) && name === DOWNLOAD_ASSET) {
        return digest;
      }
    }
  } catch {
    // A checksum that cannot be read is not a reason to fail the download.
  }
  return "";
}

function withDigest(html, digest) {
  if (!digest) {
    return html;
  }
  return html.replace(
    DIGEST_ELEMENT,
    `<code class="release-digest" data-release-digest>${digest}</code>`
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/badge.svg" || url.pathname === "/status.json" || url.pathname === "/status") {
      return ciBadge.fetch(request, env);
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return methodNotAllowed();
      }
      if (request.method === "HEAD") {
        return new Response(null, { headers: HTML_HEADERS });
      }
      return new Response(withDigest(page, await releaseDigest()), {
        headers: HTML_HEADERS,
      });
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return methodNotAllowed();
    }

    const asset = await env.ASSETS.fetch(request);
    const headers = new Headers(asset.headers);
    headers.set("x-content-type-options", "nosniff");
    headers.set("referrer-policy", "strict-origin-when-cross-origin");
    return new Response(asset.body, {
      status: asset.status,
      statusText: asset.statusText,
      headers,
    });
  },
};
