import page from "./index.html";
import ciBadge from "./ci-badge/worker.js";
import { zipResponse } from "./site-archive.js";

const HTML_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=300",
  "content-security-policy":
    "default-src 'none'; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/badge.svg" || url.pathname === "/status.json" || url.pathname === "/status") {
      return ciBadge.fetch(request, env);
    }

    if (url.pathname === "/download/offgrid-setup.zip") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return methodNotAllowed();
      }
      const download = zipResponse();
      return request.method === "HEAD"
        ? new Response(null, { headers: download.headers })
        : download;
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return methodNotAllowed();
      }
      return new Response(request.method === "HEAD" ? null : page, {
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
