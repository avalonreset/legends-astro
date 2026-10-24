/**
 * Isolated HTML sender for the Astro canary dist/.
 * Not the CTO Legends origin handler. Not visual proof.
 * Does not load env files, copy ledgers, or listen on import.
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const CANARY_DIST = path.join(here, "..", "dist");
export const TAGGABLE_HOST = "canary.example.test";
export const ALLOWED_EVENTS = Object.freeze([
  "email_gate_view",
  "email_gate_complete",
  "tip_option_selected",
  "checkout_redirect",
  "checkout_return_confirmed",
  "checkout_return_canceled",
]);

const PAGE_FILES = Object.freeze({
  "/": "index.html",
  "/privacy": "privacy.html",
  "/terms": "terms.html",
});

const PUBLIC_JS = Object.freeze(["/gate-frame-guard-v1.js", "/gate-app-canary.js"]);

const FINGERPRINT_ASSET = /\.[A-Za-z0-9_-]{6,}\.(css|js|mjs|woff2|woff|ttf|svg|png|jpg|jpeg|webp)$/;

function cookieValue(req, name) {
  const header = String(req.headers.cookie || "");
  const prefix = `${name}=`;
  for (const part of header.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) return value.slice(prefix.length);
  }
  return "";
}

export function analyticsOptedOut(req) {
  return (
    String(req?.headers?.["sec-gpc"] || "").trim() === "1" ||
    cookieValue(req, "cto_analytics_opt_out") === "1"
  );
}

export function analyticsAllowed(req) {
  return !analyticsOptedOut(req) && cookieValue(req, "cto_analytics_choice") === "allow-v1";
}

export function ga4RequestAllowed(req, url, { enabled = true } = {}) {
  if (!enabled || !req || !url) return false;
  if (req.method !== "GET") return false;
  const host = String(req.headers.host || "").toLowerCase().split(":")[0];
  if (host !== TAGGABLE_HOST) return false;
  if (url.pathname !== "/" || url.search !== "") return false;
  if (!analyticsAllowed(req)) return false;
  return true;
}

export function ga4Bootstrap() {
  const allowedEvents = JSON.stringify(ALLOWED_EVENTS);
  return `<script async src="https://www.googletagmanager.com/gtag/js?id=G-CANARY"></script><script data-cto-ga4-scaffold>(()=>{window.dataLayer=window.dataLayer||[];const ctoGtag=function(){window.dataLayer.push(arguments)};ctoGtag("consent","default",{ad_storage:"denied",ad_user_data:"denied",ad_personalization:"denied",analytics_storage:"granted"});ctoGtag("js",new Date());ctoGtag("config","G-CANARY",{send_page_view:false,cookie_expires:31536000,cookie_update:false,allow_google_signals:false,allow_ad_personalization_signals:false});const allowed=new Set(${allowedEvents});window.ctoAnalyticsEvent=function(name){if(allowed.has(name))ctoGtag("event",name)};})();</script>`;
}

export function injectGa4(html, context) {
  const body = String(html);
  if (!ga4RequestAllowed(context.req, context.url, context)) return body;
  return body.includes("</head>")
    ? body.replace("</head>", `${ga4Bootstrap()}</head>`)
    : `${ga4Bootstrap()}${body}`;
}

export function htmlContentSecurityPolicy(context = {}) {
  const googleAnalyticsAllowed = ga4RequestAllowed(context.req, context.url, context);
  const googleScript = googleAnalyticsAllowed ? " https://www.googletagmanager.com" : "";
  const googleCollection = googleAnalyticsAllowed
    ? " https://www.google-analytics.com https://region1.google-analytics.com"
    : "";
  return [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    `script-src 'self' 'unsafe-inline'${googleScript}`,
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    `img-src 'self' data:${googleCollection}`,
    `connect-src 'self'${googleCollection}`,
  ].join("; ");
}

export const SENSITIVE_CSP =
  "default-src 'none'; base-uri 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'none'";

function send(res, status, body, headers) {
  res.writeHead(status, headers);
  res.end(body ?? "");
}

export function resolveUnder(rootDir, relativeUrlPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(relativeUrlPath);
  } catch {
    return null;
  }
  if (!decoded || decoded.includes("\0")) return null;
  const rel = decoded.replace(/^[/\\]+/, "");
  if (!rel) return null;
  const rootResolved = path.resolve(rootDir);
  const joined = path.resolve(rootResolved, rel);
  const prefix = rootResolved.endsWith(path.sep) ? rootResolved : rootResolved + path.sep;
  if (joined !== rootResolved && !joined.startsWith(prefix)) return null;
  return joined;
}

export function isFingerprintedAsset(filePath) {
  return FINGERPRINT_ASSET.test(path.basename(filePath));
}

function contentTypeFor(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  return (
    {
      ".html": "text/html; charset=utf-8",
      ".js": "text/javascript; charset=utf-8",
      ".mjs": "text/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8",
      ".svg": "image/svg+xml",
      ".png": "image/png",
      ".woff2": "font/woff2",
      ".woff": "font/woff",
      ".ttf": "font/ttf",
    }[ext] || "application/octet-stream"
  );
}

function sendHtmlFile(res, filePath, ctx) {
  const html = fs.readFileSync(filePath, "utf8");
  const tagged = injectGa4(html, ctx);
  send(res, 200, tagged, {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "content-security-policy": htmlContentSecurityPolicy(ctx),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
  });
}

export function handle(req, res, { distDir = CANARY_DIST, gaEnabled = true } = {}) {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const ctx = { req, url, enabled: gaEnabled };

  if (req.method !== "GET" && req.method !== "HEAD") {
    send(res, 405, "", { "cache-control": "no-store" });
    return;
  }

  if (url.pathname === "/index.html") {
    send(res, 301, "", { Location: "/", "cache-control": "no-store" });
    return;
  }
  if (url.pathname === "/privacy/" || url.pathname === "/privacy.html") {
    send(res, 301, "", { Location: "/privacy", "cache-control": "no-store" });
    return;
  }
  if (url.pathname === "/terms/" || url.pathname === "/terms.html") {
    send(res, 301, "", { Location: "/terms", "cache-control": "no-store" });
    return;
  }
  if (url.pathname === "/contribute" || url.pathname === "/contribute/") {
    send(res, 302, "", {
      Location: "/" + (url.search || ""),
      "cache-control": "no-store",
    });
    return;
  }

  const notesHtmlAlias = url.pathname.match(/^\/notes\/([^/]+)\.html$/);
  if (notesHtmlAlias) {
    send(res, 301, "", {
      Location: `/notes/${notesHtmlAlias[1]}`,
      "cache-control": "no-store",
    });
    return;
  }

  if (PUBLIC_JS.includes(url.pathname)) {
    const filePath = resolveUnder(distDir, path.basename(url.pathname));
    if (!filePath || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      send(res, 404, "", { "cache-control": "no-store" });
      return;
    }
    send(res, 200, fs.readFileSync(filePath), {
      "content-type": "text/javascript; charset=utf-8",
      "cache-control": "no-store",
    });
    return;
  }

  if (url.pathname === "/_astro" || url.pathname === "/_astro/") {
    send(res, 404, "", { "cache-control": "no-store" });
    return;
  }

  if (url.pathname.startsWith("/_astro/")) {
    const astroRoot = path.join(distDir, "_astro");
    const rel = url.pathname.slice("/_astro/".length);
    const filePath = resolveUnder(astroRoot, rel);
    if (!filePath || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      send(res, 404, "", { "cache-control": "no-store" });
      return;
    }
    const cache = isFingerprintedAsset(filePath)
      ? "public, max-age=31536000, immutable"
      : "no-store";
    send(res, 200, fs.readFileSync(filePath), {
      "content-type": contentTypeFor(filePath),
      "cache-control": cache,
    });
    return;
  }

  if (PAGE_FILES[url.pathname]) {
    const filePath = resolveUnder(distDir, PAGE_FILES[url.pathname]);
    if (!filePath || !fs.existsSync(filePath)) {
      send(res, 404, "", { "cache-control": "no-store" });
      return;
    }
    sendHtmlFile(res, filePath, ctx);
    return;
  }

  const noteMatch = url.pathname.match(/^\/notes\/([A-Za-z0-9_-]+)$/);
  if (noteMatch) {
    const filePath = resolveUnder(path.join(distDir, "notes"), `${noteMatch[1]}.html`);
    if (!filePath || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      send(res, 404, "", { "cache-control": "no-store" });
      return;
    }
    sendHtmlFile(res, filePath, ctx);
    return;
  }

  if (url.pathname === "/cancel") {
    send(res, 401, "<h1>manage recurring creator tip</h1>", {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy": SENSITIVE_CSP,
      "referrer-policy": "no-referrer",
      "x-robots-tag": "noindex, nofollow, noarchive",
    });
    return;
  }

  send(res, 404, "", { "cache-control": "no-store" });
}

export function createFixtureServer(options = {}) {
  return http.createServer((req, res) => handle(req, res, options));
}
