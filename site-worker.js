import page from "./index.html";
import ciBadge from "./ci-badge/worker.js";

const DOWNLOAD_ASSET = "offgrid-portable.zip";
const DIGEST_ASSET = "/SHA256SUMS";
const DIGEST_ELEMENT = /<code class="release-digest" data-release-digest>[^<]*<\/code>/;
const DIGEST_TIMEOUT_MS = 3000;
const EDGE_CACHE_SECONDS = 300;
// Bump this when changing the rendered page or its cache behaviour so the
// Worker cache starts from a fresh key instead of reusing an older entry.
const CACHE_VERSION = "2026-11-14-4";
// Server-side cache inside one Worker isolate. The edge cache is shared across
// isolates; this one avoids another asset read on the first miss in each isolate.
let digestCache = { value: "", expires: 0 };

const HTML_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=300",
  "cdn-cache-control": `public, max-age=${EDGE_CACHE_SECONDS}`,
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
// serves, so it is read from the same static asset the button downloads. Any
// failure leaves the page saying where the file lives.
async function releaseDigest(env, url) {
  if (digestCache.value && Date.now() < digestCache.expires) {
    return digestCache.value;
  }
  try {
    const response = await env.ASSETS.fetch(
      new Request(new URL(DIGEST_ASSET, url).toString(), {
        headers: { accept: "text/plain" },
        signal: AbortSignal.timeout(DIGEST_TIMEOUT_MS),
      })
    );
    if (!response.ok) {
      return "";
    }
    for (const line of (await response.text()).split("\n")) {
      const [digest, name] = line.trim().split(/\s+\*?/);
      if (/^[0-9a-f]{64}$/.test(digest) && name === DOWNLOAD_ASSET) {
        digestCache = {
          value: digest,
          expires: Date.now() + EDGE_CACHE_SECONDS * 1000,
        };
        return digest;
      }
    }
  } catch {
    // A checksum that cannot be read is not a reason to fail the download.
  }
  return "";
}

function htmlCacheKey(url) {
  // One key for / and /index.html, so both entry points share the edge copy.
  return new Request(new URL(`/index.html?cache=${CACHE_VERSION}`, url).toString(), { method: "GET" });
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
  async fetch(request, env, ctx) {
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
      const cache = globalThis.caches?.default;
      const cacheKey = htmlCacheKey(url);
      if (cache) {
        try {
          const cached = await cache.match(cacheKey);
          if (cached) {
            return cached;
          }
        } catch {
          // A cache miss must never stop the page from being served.
        }
      }
      const digest = await releaseDigest(env, url);
      const headers = new Headers(HTML_HEADERS);
      if (!digest) {
        // A missing digest is a transient asset failure, not a stable page.
        // Serve it without caching so the next request can retry the manifest.
        headers.set("cache-control", "no-store");
        headers.set("cdn-cache-control", "no-store");
      }
      const response = new Response(withDigest(page, digest), { headers });
      if (digest && cache && ctx?.waitUntil) {
        ctx.waitUntil(cache.put(cacheKey, response.clone()));
      }
      return response;
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return methodNotAllowed();
    }

    const asset = await env.ASSETS.fetch(request);
    const headers = new Headers(asset.headers);
    headers.set("cache-control", "public, max-age=300");
    headers.set("cdn-cache-control", `public, max-age=${EDGE_CACHE_SECONDS}`);
    headers.set("x-content-type-options", "nosniff");
    headers.set("referrer-policy", "strict-origin-when-cross-origin");
    return new Response(asset.body, {
      status: asset.status,
      statusText: asset.statusText,
      headers,
    });
  },
};
