#!/usr/bin/env node
/**
 * Reusable Playwright Visual + Computed-Style Digital Twin capture worker for legends-astro.
 *
 * Zero mandatory npm dependencies: Playwright is discovered optionally at runtime via:
 *   1. --playwright-from <package.json or directory>
 *   2. process.env.PLAYWRIGHT_PACKAGE_JSON
 *   3. local project node_modules/playwright or global resolution
 *
 * Targets either:
 *   - --url <https://...> with --allow-host <domain> (SSRF + page.route IP guard)
 *   - --dist <dist-dir> (ephemeral in-process 127.0.0.1:0 read-only static server closed in finally)
 *
 * Never spawns detached daemons, never opens a browser window, never reads user browser profiles.
 */

import dns from "node:dns/promises";
import fs from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import http from "node:http";
import { createRequire } from "node:module";
import net from "node:net";
import path from "node:path";

const DESKTOP_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1";
const FULL_PAGE_MAX_HEIGHT = 16000;

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".xml": "application/xml; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
};

function sanitizeNoDashTypography(text) {
  return String(text ?? "")
    .replace(/\u2014/g, "-")
    .replace(/\u2013/g, "-");
}

function mdCell(value, maxLen = 96) {
  const clean = sanitizeNoDashTypography(String(value ?? ""))
    .replace(/\r?\n+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\|/g, "\\|")
    .trim();
  if (!clean) return "-";
  return clean.length > maxLen ? `${clean.slice(0, maxLen - 3)}...` : clean;
}

function isForbiddenIPv4(ip) {
  const parts = ip.split(".").map((n) => Number(n));
  if (parts.length !== 4 || parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return true;
  const [a, b] = parts;
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a >= 224) return true;
  return false;
}

function isForbiddenIp(rawIp) {
  const ip = String(rawIp || "").split("%")[0].toLowerCase().trim();
  if (!ip) return true;
  if (net.isIPv4(ip)) return isForbiddenIPv4(ip);
  if (net.isIPv6(ip)) {
    if (ip === "::" || ip === "::1") return true;
    const mapped = ip.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (mapped) return isForbiddenIPv4(mapped[1]);
    if (ip.startsWith("fc") || ip.startsWith("fd")) return true;
    if (ip.startsWith("fe8") || ip.startsWith("fe9") || ip.startsWith("fea") || ip.startsWith("feb")) return true;
    if (ip.startsWith("ff")) return true;
    return false;
  }
  return true;
}

async function assertRemoteHostSafe(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/\.$/, "");
  if (!host || host === "localhost" || host.endsWith(".localhost")) {
    throw new Error(`Refusing forbidden hostname (CWE-918): ${hostname}`);
  }
  if (net.isIP(host.replace(/^\[|\]$/g, ""))) {
    if (isForbiddenIp(host.replace(/^\[|\]$/g, ""))) {
      throw new Error(`Refusing forbidden IP literal (CWE-918): ${hostname}`);
    }
    return;
  }
  const records = await dns.lookup(host, { all: true });
  if (!records.length) {
    throw new Error(`DNS resolution returned no addresses for ${host}`);
  }
  for (const rec of records) {
    if (isForbiddenIp(rec.address)) {
      throw new Error(`Refusing SSRF-unsafe resolved IP ${rec.address} for ${host} (CWE-918)`);
    }
  }
}

function hostMatchesAllowlist(hostname, allowHosts) {
  const host = String(hostname || "").toLowerCase().replace(/\.$/, "");
  for (const entry of allowHosts) {
    const p = String(entry || "").toLowerCase().replace(/\.$/, "");
    if (!p) continue;
    if (p.startsWith("*.")) {
      const suffix = p.slice(1);
      if (host.endsWith(suffix) && host.length > suffix.length) return true;
    } else if (host === p) {
      return true;
    }
  }
  return false;
}

function routeToSlug(route) {
  const clean = String(route || "/").split("#")[0].split("?")[0].trim();
  if (!clean || clean === "/" || clean === "/index.html") return "home";
  return clean
    .replace(/^\/+|\/+$/g, "")
    .replace(/\.html?$/i, "")
    .replace(/[^A-Za-z0-9_-]+/g, "__") || "page";
}

function parseArgs(argv) {
  const out = {
    url: null,
    dist: null,
    outDir: null,
    routes: [],
    allowHosts: [],
    crops: [],
    timeoutMs: 45000,
    noMobile: false,
    noFullPage: false,
    allowHttp: false,
    playwrightFrom: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--url" && argv[i + 1]) {
      out.url = argv[++i];
    } else if (arg === "--dist" && argv[i + 1]) {
      out.dist = path.resolve(argv[++i]);
    } else if (arg === "--out" && argv[i + 1]) {
      out.outDir = path.resolve(argv[++i]);
    } else if (arg === "--route" && argv[i + 1]) {
      out.routes.push(argv[++i]);
    } else if (arg === "--allow-host" && argv[i + 1]) {
      out.allowHosts.push(argv[++i]);
    } else if (arg === "--crop" && argv[i + 1]) {
      out.crops.push(argv[++i]);
    } else if (arg === "--timeout" && argv[i + 1]) {
      out.timeoutMs = Number(argv[++i]) || out.timeoutMs;
    } else if (arg === "--no-mobile") {
      out.noMobile = true;
    } else if (arg === "--no-full-page") {
      out.noFullPage = true;
    } else if (arg === "--allow-http") {
      out.allowHttp = true;
    } else if (arg === "--playwright-from" && argv[i + 1]) {
      out.playwrightFrom = argv[++i];
    }
  }
  return out;
}

async function loadPlaywright(playwrightFrom) {
  const attempts = [];
  if (playwrightFrom) {
    const resolved = path.resolve(playwrightFrom);
    const pkgAnchor = fs.existsSync(resolved) && fs.statSync(resolved).isDirectory()
      ? path.join(resolved, "package.json")
      : resolved;
    try {
      const req = createRequire(pkgAnchor);
      return req("playwright");
    } catch (err) {
      attempts.push(`--playwright-from (${pkgAnchor}): ${err.message}`);
    }
  }

  try {
    return await import("playwright");
  } catch (err) {
    attempts.push(`default import('playwright'): ${err.message}`);
  }

  const envPkg = process.env.PLAYWRIGHT_PACKAGE_JSON;
  if (envPkg && fs.existsSync(envPkg)) {
    try {
      const req = createRequire(path.resolve(envPkg));
      return req("playwright");
    } catch (err) {
      attempts.push(`PLAYWRIGHT_PACKAGE_JSON (${envPkg}): ${err.message}`);
    }
  }

  const cwdPkg = path.join(process.cwd(), "package.json");
  if (fs.existsSync(cwdPkg)) {
    try {
      const req = createRequire(cwdPkg);
      return req("playwright");
    } catch (err) {
      attempts.push(`cwd package.json (${cwdPkg}): ${err.message}`);
    }
  }

  const diagnostic = {
    ok: false,
    code: "playwright_not_installed",
    message:
      "Optional Playwright runtime is not installed. Install Playwright in your target project (`npm install -D playwright && npx playwright install chromium`) or pass `--playwright-from <dir-with-playwright>` / set `PLAYWRIGHT_PACKAGE_JSON`.",
    attempts,
  };
  process.stdout.write(`${JSON.stringify(diagnostic, null, 2)}\n`);
  process.exit(2);
}

function isSafeDistFile(distRoot, candidatePath) {
  try {
    const lstat = fs.lstatSync(candidatePath);
    if (lstat.isSymbolicLink() || !lstat.isFile()) return false;
    const realRoot = fs.realpathSync(distRoot);
    const realCand = fs.realpathSync(candidatePath);
    const rel = path.relative(realRoot, realCand);
    if (rel.startsWith("..") || path.isAbsolute(rel)) return false;
    return true;
  } catch {
    return false;
  }
}

function resolveDistRequest(distRoot, urlPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(urlPath.split("?")[0].split("#")[0]);
  } catch {
    return null;
  }
  if (decoded.includes("\0")) return null;
  const rel = decoded.replace(/^\/+/, "");
  const candidates = [];
  if (!rel) {
    candidates.push(path.join(distRoot, "index.html"));
  } else {
    candidates.push(path.join(distRoot, rel));
    const trimmed = rel.replace(/\/+$/, "");
    candidates.push(path.join(distRoot, `${trimmed}.html`));
    candidates.push(path.join(distRoot, trimmed, "index.html"));
  }
  for (const cand of candidates) {
    if (isSafeDistFile(distRoot, cand)) return cand;
  }
  return null;
}

function discoverDistRoutes(distRoot) {
  const routes = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith(".html")) {
        const rel = path.relative(distRoot, full).replaceAll("\\", "/");
        if (rel === "404.html" || rel === "404/index.html") continue;
        if (rel === "index.html") routes.push("/");
        else if (rel.endsWith("/index.html")) routes.push(`/${rel.slice(0, -"index.html".length)}`);
        else routes.push(`/${rel}`);
      }
    }
  };
  walk(distRoot);
  return routes.sort();
}

function startEphemeralDistServer(distRoot) {
  const resolvedRoot = path.resolve(distRoot);
  const server = http.createServer((req, res) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.writeHead(405, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Method Not Allowed");
      return;
    }
    const targetFile = resolveDistRequest(resolvedRoot, req.url || "/");
    if (targetFile) {
      const ext = path.extname(targetFile).toLowerCase();
      const mime = MIME_TYPES[ext] || "application/octet-stream";
      const body = fs.readFileSync(targetFile);
      res.writeHead(200, { "Content-Type": mime, "Cache-Control": "no-store" });
      res.end(req.method === "HEAD" ? undefined : body);
      return;
    }
    const notFoundFile = path.join(resolvedRoot, "404.html");
    if (isSafeDistFile(resolvedRoot, notFoundFile)) {
      res.writeHead(404, { "Content-Type": "text/html; charset=utf-8" });
      res.end(req.method === "HEAD" ? undefined : fs.readFileSync(notFoundFile));
      return;
    }
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Not Found");
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      resolve({ server, port: addr.port, baseUrl: `http://127.0.0.1:${addr.port}` });
    });
  });
}

function detectCasing(text) {
  const clean = String(text ?? "").replace(/\s+/g, " ").trim();
  const lettersOnly = clean.replace(/[^a-zA-Z]/g, "");
  if (!lettersOnly) return "Icon/Symbol";
  if (lettersOnly === lettersOnly.toLowerCase()) return "lowercase";
  if (lettersOnly === lettersOnly.toUpperCase()) return "UPPERCASE";

  const words = clean
    .split(/[\s/]+/)
    .map((w) => w.replace(/^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$/g, ""))
    .filter((w) => /[a-zA-Z]/.test(w));
  if (words.length === 0) return "Icon/Symbol";

  const minorWords = new Set([
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "per", "the", "to", "via", "with",
  ]);
  const isCapWord = (w) => /^[A-Z][a-z0-9'.-]*$/.test(w) || /^[A-Z]{2,}[0-9]*$/.test(w);
  if (words.length === 1) return isCapWord(words[0]) ? "Title Case" : "Mixed";

  const titleMatches = words.every((w, idx) => (idx === 0 ? isCapWord(w) : minorWords.has(w.toLowerCase()) || isCapWord(w)));
  const cappedMajorCount = words.filter((w, idx) => idx > 0 && !minorWords.has(w.toLowerCase()) && isCapWord(w)).length;
  if (titleMatches && cappedMajorCount > 0) return "Title Case";

  const sentenceLike =
    /^[A-Z]/.test(words[0]) &&
    words.slice(1).every((w) => w === w.toLowerCase() || /^[A-Z]{2,}[0-9]*s?$/.test(w));
  if (sentenceLike) return "Sentence";

  return "Mixed";
}

async function settlePage(page) {
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await page
    .evaluate(async () => {
      if (document.fonts && document.fonts.ready) {
        await document.fonts.ready;
      }
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    })
    .catch(() => {});
  await page.waitForTimeout(500);
}

async function triggerLazyAndReveals(page) {
  await page
    .evaluate(async () => {
      const maxScroll = Math.min(document.documentElement.scrollHeight, 16000);
      const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
      for (let y = 0; y <= maxScroll; y += step) {
        window.scrollTo(0, y);
        await new Promise((r) => setTimeout(r, 90));
      }
      window.scrollTo(0, 0);
      await new Promise((r) => setTimeout(r, 200));
    })
    .catch(() => {});
  await page.waitForTimeout(250);
}

async function takeFullPageScreenshot(page, filePath) {
  await triggerLazyAndReveals(page);
  const dims = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
    innerWidth: window.innerWidth,
  }));
  if (dims.scrollHeight <= FULL_PAGE_MAX_HEIGHT) {
    await page.screenshot({ path: filePath, fullPage: true });
    return { fullHeight: dims.scrollHeight, capped: false, capturedHeight: dims.scrollHeight };
  }
  await page.screenshot({
    path: filePath,
    fullPage: true,
    clip: { x: 0, y: 0, width: dims.innerWidth, height: FULL_PAGE_MAX_HEIGHT },
  });
  return { fullHeight: dims.scrollHeight, capped: true, capturedHeight: FULL_PAGE_MAX_HEIGHT };
}

async function extractDomAndStyles(page, pageMeta) {
  return page.evaluate((meta) => {
    function parseRgba(str) {
      if (!str || str === "transparent") return { r: 0, g: 0, b: 0, a: 0, raw: str || "transparent", hex: null };
      const m = str.match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)/i);
      if (!m) {
        const mModern = str.match(/rgba?\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.%]+))?\s*\)/i);
        if (!mModern) return { r: null, g: null, b: null, a: 1, raw: str, hex: null };
        const r = Math.round(Number(mModern[1]));
        const g = Math.round(Number(mModern[2]));
        const b = Math.round(Number(mModern[3]));
        let a = 1;
        if (mModern[4] !== undefined) {
          a = mModern[4].endsWith("%") ? parseFloat(mModern[4]) / 100 : parseFloat(mModern[4]);
        }
        const hex = "#" + [r, g, b].map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
        return { r, g, b, a, raw: str, hex };
      }
      const r = Math.round(Number(m[1]));
      const g = Math.round(Number(m[2]));
      const b = Math.round(Number(m[3]));
      const a = m[4] === undefined ? 1 : Number(m[4]);
      const hex = "#" + [r, g, b].map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
      return { r, g, b, a, raw: str, hex };
    }

    function shortSelector(el) {
      if (!el || !el.tagName) return "";
      const tag = el.tagName.toLowerCase();
      const id = el.id ? `#${el.id}` : "";
      const classes = Array.from(el.classList || [])
        .filter((c) => c && !c.startsWith("hover:") && !c.startsWith("focus:"))
        .slice(0, 3)
        .map((c) => `.${c}`)
        .join("");
      return `${tag}${id}${classes}`;
    }

    function isVisible(el, style, rect) {
      if (!el || !style || !rect) return false;
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (parseFloat(style.opacity || "1") <= 0.01) return false;
      if (rect.width <= 0.5 || rect.height <= 0.5) return false;
      return true;
    }

    function getBackdrop(style) {
      const bf = style.backdropFilter || style.webkitBackdropFilter || "none";
      return bf === "" ? "none" : bf;
    }

    function applyTextTransform(raw, transform) {
      const text = String(raw || "").replace(/\s+/g, " ").trim();
      if (transform === "uppercase") return text.toUpperCase();
      if (transform === "lowercase") return text.toLowerCase();
      if (transform === "capitalize") return text.replace(/\b\w/g, (ch) => ch.toUpperCase());
      return text;
    }

    function classifyRadius(radiusStr) {
      if (!radiusStr || radiusStr === "0px") return { category: "sharp (0px)", maxPx: 0 };
      const tokens = radiusStr.split(/\s+/).filter((t) => t !== "/");
      let maxPx = 0;
      let hasPct = false;
      let maxPct = 0;
      for (const t of tokens) {
        if (t.endsWith("%")) {
          hasPct = true;
          maxPct = Math.max(maxPct, parseFloat(t) || 0);
        } else if (t.endsWith("px")) {
          maxPx = Math.max(maxPx, parseFloat(t) || 0);
        }
      }
      if ((hasPct && maxPct >= 45) || maxPx >= 99) return { category: "pill/circle (>=99px or >=45%)", maxPx: Math.round(maxPx * 100) / 100 };
      if (maxPx >= 8) return { category: "bubbly (>=8px)", maxPx: Math.round(maxPx * 100) / 100 };
      if (maxPx >= 2 && maxPx <= 6) return { category: "crisp (2-6px)", maxPx: Math.round(maxPx * 100) / 100 };
      if (maxPx === 0) return { category: "sharp (0px)", maxPx: 0 };
      return { category: "transitional (other)", maxPx: Math.round(maxPx * 100) / 100 };
    }

    const scrollY = window.scrollY || window.pageYOffset || 0;
    const docEl = document.documentElement;
    const body = document.body;
    const bodyStyle = body ? getComputedStyle(body) : null;
    const docStyle = docEl ? getComputedStyle(docEl) : null;

    const overflowPx = Math.max(0, docEl.scrollWidth - window.innerWidth);
    const overflowOffenders = [];
    const borderRadiusMap = new Map();
    const buttons = [];
    const surfaces = [];
    const headings = [];
    const footers = [];
    const sections = [];

    for (const el of Array.from(document.querySelectorAll("*"))) {
      const tag = el.tagName.toLowerCase();
      if (["script", "style", "link", "meta", "head", "title", "noscript", "svg", "path", "g", "defs", "clippath", "rect", "circle", "line", "polyline", "polygon"].includes(tag)) {
        continue;
      }
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (!isVisible(el, style, rect)) continue;

      const sel = shortSelector(el);
      if (overflowPx > 0 && rect.right > window.innerWidth + 1 && overflowOffenders.length < 10) {
        overflowOffenders.push({
          selector: sel,
          right: Math.round(rect.right),
          width: Math.round(rect.width),
          viewportWidth: window.innerWidth,
        });
      }

      const bgParsed = parseRgba(style.backgroundColor);
      const hasBg = bgParsed.a > 0.001 || (style.backgroundImage && style.backgroundImage !== "none");
      const hasBorder =
        ((parseFloat(style.borderTopWidth) || 0) > 0 ||
          (parseFloat(style.borderRightWidth) || 0) > 0 ||
          (parseFloat(style.borderBottomWidth) || 0) > 0 ||
          (parseFloat(style.borderLeftWidth) || 0) > 0) &&
        (parseRgba(style.borderTopColor).a > 0.001 ||
          parseRgba(style.borderRightColor).a > 0.001 ||
          parseRgba(style.borderBottomColor).a > 0.001 ||
          parseRgba(style.borderLeftColor).a > 0.001);
      const hasShadow = style.boxShadow && style.boxShadow !== "none";
      const radius = style.borderRadius;

      if (radius && radius !== "0px" && (hasBg || hasBorder || hasShadow || ["img", "video", "canvas", "input", "textarea", "select", "button"].includes(tag))) {
        const info = classifyRadius(radius);
        if (!borderRadiusMap.has(radius)) {
          borderRadiusMap.set(radius, {
            value: radius,
            category: info.category,
            maxPx: info.maxPx,
            count: 0,
            examples: [],
          });
        }
        const entry = borderRadiusMap.get(radius);
        entry.count += 1;
        if (entry.examples.length < 5 && !entry.examples.includes(sel)) entry.examples.push(sel);
      }

      if (tag === "h1" || tag === "h2" || tag === "h3") {
        const rawText = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        const renderedText = applyTextTransform(rawText, style.textTransform);
        headings.push({
          page: meta.id,
          route: meta.route,
          tag,
          selector: sel,
          rawText: rawText.slice(0, 140),
          renderedText: renderedText.slice(0, 140),
          fontFamily: style.fontFamily,
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
          lineHeight: style.lineHeight,
          letterSpacing: style.letterSpacing,
          textTransform: style.textTransform,
          color: style.color,
          colorHex: parseRgba(style.color).hex,
          y: Math.round(rect.top + scrollY),
        });
      }

      const role = el.getAttribute("role");
      const clsStr = typeof el.className === "string" ? el.className : "";
      const isNativeButton =
        tag === "button" ||
        role === "button" ||
        (tag === "input" && ["submit", "button", "reset"].includes((el.getAttribute("type") || "").toLowerCase()));
      const isStyledLinkCta =
        tag === "a" &&
        (hasBg || hasBorder || hasShadow || /\b(btn|button|cta|action|pill|primary|secondary)\b/i.test(clsStr)) &&
        rect.height <= 96 &&
        rect.width >= 32;

      if (isNativeButton || isStyledLinkCta) {
        const rawText = (el.innerText || el.getAttribute("value") || el.getAttribute("aria-label") || el.getAttribute("title") || "")
          .replace(/\s+/g, " ")
          .trim();
        const renderedText = applyTextTransform(rawText, style.textTransform);
        buttons.push({
          page: meta.id,
          route: meta.route,
          tag,
          selector: sel,
          href: tag === "a" ? el.getAttribute("href") : null,
          rawText: rawText.slice(0, 100),
          renderedText: renderedText.slice(0, 100),
          textTransform: style.textTransform,
          height: Math.round(rect.height),
          width: Math.round(rect.width),
          borderRadius: style.borderRadius,
          backgroundColor: style.backgroundColor,
          backgroundHex: bgParsed.a >= 0.999 ? bgParsed.hex : null,
          backgroundImage: style.backgroundImage && style.backgroundImage !== "none" ? style.backgroundImage : null,
          boxShadow: style.boxShadow,
          borderColor: hasBorder ? style.borderTopColor : "none",
          borderWidth: hasBorder ? style.borderTopWidth : "0px",
          color: style.color,
          fontFamily: style.fontFamily,
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
          letterSpacing: style.letterSpacing,
          y: Math.round(rect.top + scrollY),
        });
      }

      const backdrop = getBackdrop(style);
      const isContainerTag = ["section", "article", "aside", "header", "nav", "footer", "main", "form", "div", "dialog"].includes(tag);
      if (isContainerTag && rect.width >= 120 && rect.height >= 36 && (hasBg || backdrop !== "none")) {
        const hasGradient = Boolean(style.backgroundImage && style.backgroundImage !== "none");
        let materiality = "Other";
        if (bgParsed.a > 0.001 && bgParsed.a < 0.999 && backdrop !== "none") materiality = "Translucent + Backdrop Blur";
        else if (bgParsed.a > 0.001 && bgParsed.a < 0.999) materiality = "Translucent (No Blur)";
        else if (bgParsed.a >= 0.999 && !hasGradient) materiality = "Opaque Solid Slab";
        else if (hasGradient && backdrop !== "none") materiality = "Gradient + Backdrop Blur";
        else if (hasGradient) materiality = "Gradient Surface";
        else if (backdrop !== "none") materiality = "Transparent + Backdrop Blur";

        surfaces.push({
          page: meta.id,
          route: meta.route,
          selector: sel,
          tag,
          materiality,
          backgroundColor: style.backgroundColor,
          backgroundHex: bgParsed.hex,
          alpha: Math.round(bgParsed.a * 1000) / 1000,
          backgroundImage: hasGradient ? style.backgroundImage.slice(0, 140) : "none",
          backdropFilter: backdrop,
          borderColor: hasBorder ? style.borderTopColor : "none",
          borderRadius: style.borderRadius,
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          y: Math.round(rect.top + scrollY),
        });
      }

      if (tag === "footer" || role === "contentinfo") {
        const footerLinks = el.querySelectorAll("a").length;
        const footerText = (el.innerText || "").replace(/\s+/g, " ").trim();
        footers.push({
          page: meta.id,
          route: meta.route,
          selector: sel,
          backgroundColor: style.backgroundColor,
          backgroundHex: bgParsed.hex,
          alpha: Math.round(bgParsed.a * 1000) / 1000,
          backgroundImage: style.backgroundImage !== "none" ? style.backgroundImage.slice(0, 140) : "none",
          backdropFilter: backdrop,
          borderTop: `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`,
          height: Math.round(rect.height),
          width: Math.round(rect.width),
          y: Math.round(rect.top + scrollY),
          color: style.color,
          linkCount: footerLinks,
          textSnippet: footerText.slice(0, 160),
        });
      }
    }

    const sectionCandidates = [];
    const seenEls = new Set();
    const pushCandidate = (el) => {
      if (!el || seenEls.has(el)) return;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (!isVisible(el, style, rect)) return;
      if (rect.height < 24 || rect.width < 200) return;
      seenEls.add(el);
      const headingEl = el.querySelector("h1, h2, h3");
      const headingText = headingEl
        ? (headingEl.innerText || headingEl.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120)
        : el.getAttribute("aria-label") || (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80);
      const bgParsed = parseRgba(style.backgroundColor);
      sectionCandidates.push({
        selector: shortSelector(el),
        id: el.id || null,
        tag: el.tagName.toLowerCase(),
        y: Math.round(rect.top + scrollY),
        height: Math.round(rect.height),
        width: Math.round(rect.width),
        headingText,
        backgroundColor: style.backgroundColor,
        backgroundHex: bgParsed.hex,
        alpha: Math.round(bgParsed.a * 1000) / 1000,
        backdropFilter: getBackdrop(style),
        borderTop: `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`,
      });
    };

    const headerEl = document.querySelector("header, nav");
    if (headerEl) pushCandidate(headerEl);
    const mainEl = document.querySelector("main");
    if (mainEl) {
      const directChildren = Array.from(mainEl.children);
      if (directChildren.length >= 2) {
        for (const ch of directChildren) {
          pushCandidate(ch);
          if (ch.tagName.toLowerCase() === "div" && !ch.id) {
            for (const ns of Array.from(ch.querySelectorAll(":scope > section, :scope > div[id]"))) {
              pushCandidate(ns);
            }
          }
        }
      } else {
        for (const sec of Array.from(mainEl.querySelectorAll("section, [id]"))) {
          if (sec.tagName.toLowerCase() === "section" || sec.getBoundingClientRect().height >= 120) {
            pushCandidate(sec);
          }
        }
      }
    } else {
      for (const sec of Array.from(document.querySelectorAll("section, article, form"))) {
        pushCandidate(sec);
      }
    }
    for (const f of Array.from(document.querySelectorAll("footer"))) pushCandidate(f);

    sectionCandidates.sort((a, b) => a.y - b.y || b.height - a.height);
    sections.push(...sectionCandidates);

    const loadedFonts = Array.from(new Set(
      Array.from(document.fonts || [])
        .filter((f) => f.status === "loaded")
        .map((f) => `${f.family} (${f.weight} ${f.style})`),
    ));

    return {
      title: document.title,
      finalUrl: window.location.href,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      documentSize: { scrollWidth: docEl.scrollWidth, scrollHeight: docEl.scrollHeight },
      horizontalOverflow: { hasOverflow: overflowPx > 0, overflowPx, offenders: overflowOffenders },
      bodyBackground: bodyStyle ? bodyStyle.backgroundColor : null,
      bodyBackgroundHex: bodyStyle ? parseRgba(bodyStyle.backgroundColor).hex : null,
      bodyColor: bodyStyle ? bodyStyle.color : null,
      bodyFontFamily: bodyStyle ? bodyStyle.fontFamily : null,
      htmlBackground: docStyle ? docStyle.backgroundColor : null,
      loadedFonts,
      sections,
      borderRadii: Array.from(borderRadiusMap.values()).sort((a, b) => b.count - a.count),
      buttons,
      surfaces,
      footers,
      headings,
    };
  }, pageMeta);
}

function aggregateInventory(pagesData) {
  const radiusAgg = new Map();
  const buttonsAll = [];
  const surfaceAgg = new Map();
  const footersAll = [];
  const headingsAll = [];

  for (const p of pagesData) {
    const ex = p.desktop?.extraction;
    if (!ex) continue;

    for (const r of ex.borderRadii) {
      if (!radiusAgg.has(r.value)) {
        radiusAgg.set(r.value, { value: r.value, category: r.category, maxPx: r.maxPx, totalCount: 0, pages: {}, examples: [] });
      }
      const entry = radiusAgg.get(r.value);
      entry.totalCount += r.count;
      entry.pages[p.id] = (entry.pages[p.id] || 0) + r.count;
      for (const exSel of r.examples) {
        const tagged = `${p.id}: ${exSel}`;
        if (entry.examples.length < 6 && !entry.examples.includes(tagged)) entry.examples.push(tagged);
      }
    }

    for (const btn of ex.buttons) {
      buttonsAll.push({
        ...btn,
        detectedCasing: detectCasing(btn.renderedText),
        backgroundSummary: btn.backgroundImage
          ? `gradient (${btn.backgroundImage.slice(0, 70)})`
          : btn.backgroundHex
            ? `${btn.backgroundHex} (${btn.backgroundColor})`
            : btn.backgroundColor,
      });
    }

    for (const s of ex.surfaces) {
      const bgKey = s.materiality === "Opaque Solid Slab" && s.backgroundHex
        ? `${s.backgroundHex} (${s.backgroundColor})`
        : s.backgroundImage !== "none"
          ? `${s.backgroundColor} + gradient`
          : s.backgroundColor;
      const key = `${s.materiality}||${bgKey}||${s.backdropFilter}`;
      if (!surfaceAgg.has(key)) {
        surfaceAgg.set(key, {
          materiality: s.materiality,
          background: bgKey,
          backgroundHex: s.backgroundHex,
          alpha: s.alpha,
          backdropFilter: s.backdropFilter,
          count: 0,
          pages: new Set(),
          examples: [],
        });
      }
      const entry = surfaceAgg.get(key);
      entry.count += 1;
      entry.pages.add(p.id);
      const tag = `${p.id}: ${s.selector}`;
      if (entry.examples.length < 5 && !entry.examples.includes(tag)) entry.examples.push(tag);
    }

    if (ex.footers.length === 0) {
      footersAll.push({ page: p.id, route: p.route, found: false, selector: "none", backgroundColor: "none", backdropFilter: "none", borderTop: "none", height: 0 });
    } else {
      for (const f of ex.footers) footersAll.push({ ...f, found: true });
    }

    for (const h of ex.headings) {
      headingsAll.push({ ...h, detectedCasing: detectCasing(h.renderedText) });
    }
  }

  return {
    borderRadiiAll: Array.from(radiusAgg.values()).sort((a, b) => b.totalCount - a.totalCount),
    buttonsAll,
    surfacesSummary: Array.from(surfaceAgg.values()).map((e) => ({ ...e, pages: Array.from(e.pages) })).sort((a, b) => b.count - a.count),
    footersAll,
    headingsAll,
  };
}

function buildAuditMarkdown(twin) {
  const lines = [];
  lines.push("# Visual Digital Twin Audit Summary");
  lines.push("");
  lines.push(`- Captured at: ${twin.capturedAt}`);
  lines.push(`- Target mode: ${twin.mode}`);
  lines.push(`- Pages audited: ${twin.pages.length}`);
  lines.push(`- Screenshots generated: ${twin.screenshots.length}`);
  lines.push("");

  lines.push("## 1. Page Health, Horizontal Overflow, and Runtime Errors");
  lines.push("");
  lines.push("| Route | Viewport | HTTP | Doc Size (WxH) | Overflow | Page Errors | Console Errors | Request Failures |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- | --- |");
  for (const p of twin.pages) {
    for (const mode of ["desktop", "mobile"]) {
      const run = p[mode];
      if (!run) continue;
      const ex = run.extraction;
      const vp = mode === "desktop" ? "1440x900" : "390x844";
      const docSize = ex ? `${ex.documentSize.scrollWidth}x${ex.documentSize.scrollHeight}` : "n/a";
      const overflowStr = ex
        ? ex.horizontalOverflow.hasOverflow
          ? `YES (+${ex.horizontalOverflow.overflowPx}px)`
          : "0px (clean)"
        : "n/a";
      lines.push(
        `| ${mdCell(p.route)} | ${vp} | ${run.status ?? "err"} | ${docSize} | ${overflowStr} | ${run.errors.pageErrors.length} | ${run.errors.consoleErrors.length} | ${run.errors.requestFailures.length} |`,
      );
    }
  }
  lines.push("");

  lines.push("## 2. Border-Radius Inventory");
  lines.push("");
  lines.push("| Border-Radius | Category | Total Count | Example Selectors |");
  lines.push("| --- | --- | --- | --- |");
  for (const r of twin.aggregates.borderRadiiAll.slice(0, 30)) {
    lines.push(`| \`${mdCell(r.value, 28)}\` | ${mdCell(r.category, 30)} | ${r.totalCount} | ${mdCell(r.examples.join("; "), 80)} |`);
  }
  lines.push("");

  lines.push("## 3. Heading Typography Inventory");
  lines.push("");
  lines.push("| Route | Tag | Text | Size | Weight | Casing | Font Family |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- |");
  for (const h of twin.aggregates.headingsAll.slice(0, 60)) {
    lines.push(`| ${mdCell(h.route, 24)} | ${h.tag} | ${mdCell(h.renderedText, 44)} | ${h.fontSize} | ${h.fontWeight} | ${h.detectedCasing} | ${mdCell(h.fontFamily, 36)} |`);
  }
  lines.push("");

  lines.push("## 4. Screenshot Manifest");
  lines.push("");
  lines.push("| File | Route | Kind | Target | Notes |");
  lines.push("| --- | --- | --- | --- | --- |");
  for (const sc of twin.screenshots) {
    lines.push(`| \`${sc.file}\` | ${mdCell(sc.route, 24)} | ${sc.kind} | ${mdCell(sc.target, 30)} | ${mdCell(sc.note || "ok", 40)} |`);
  }
  lines.push("");

  return sanitizeNoDashTypography(lines.join("\n"));
}

async function runCaptureForViewport(browser, pageCfg, mode, opts, screenshots, isLocalDist) {
  const isDesktop = mode === "desktop";
  const viewport = isDesktop ? { width: 1440, height: 900 } : { width: 390, height: 844 };
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: isDesktop ? 1 : 2,
    userAgent: isDesktop ? DESKTOP_UA : MOBILE_UA,
    locale: "en-US",
    extraHTTPHeaders: { "Accept-Language": "en-US,en;q=0.9" },
  });

  const page = await context.newPage();

  // SSRF defense on remote captures: block sub-requests to loopback/private/metadata IPs
  if (!isLocalDist) {
    await page.route("**/*", async (route) => {
      const reqUrl = route.request().url();
      if (reqUrl.startsWith("data:") || reqUrl.startsWith("blob:") || reqUrl.startsWith("about:")) {
        return route.continue();
      }
      try {
        const u = new URL(reqUrl);
        if (u.protocol !== "https:" && !(opts.allowHttp && u.protocol === "http:")) {
          return route.abort("blockedbyclient");
        }
        await assertRemoteHostSafe(u.hostname);
        return route.continue();
      } catch {
        return route.abort("blockedbyclient");
      }
    });
  }

  const errors = {
    pageErrors: [],
    consoleErrors: [],
    consoleWarnings: [],
    requestFailures: [],
  };

  page.on("pageerror", (err) => errors.pageErrors.push(err?.message || String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.consoleErrors.push(msg.text());
    else if (msg.type() === "warning") errors.consoleWarnings.push(msg.text());
  });
  page.on("requestfailed", (req) => {
    const failure = req.failure()?.errorText || "failed";
    if (failure === "net::ERR_ABORTED") return;
    errors.requestFailures.push(`${req.method()} ${req.url()} (${failure})`);
  });

  let status = null;
  let finalUrl = pageCfg.url;
  let extraction = null;

  try {
    const resp = await page.goto(pageCfg.url, { waitUntil: "domcontentloaded", timeout: opts.timeoutMs });
    status = resp ? resp.status() : null;
    await settlePage(page);
    finalUrl = page.url();

    extraction = await extractDomAndStyles(page, { id: pageCfg.id, route: pageCfg.route });

    const vpFile = `${pageCfg.id}-${mode}-${viewport.width}-viewport.png`;
    await page.screenshot({ path: path.join(opts.outDir, vpFile), fullPage: false });
    screenshots.push({
      file: vpFile,
      page: pageCfg.id,
      route: pageCfg.route,
      kind: `${mode}-viewport`,
      target: `${viewport.width}x${viewport.height}`,
      note: `HTTP ${status ?? "n/a"}`,
    });

    if (isDesktop && opts.crops.length > 0) {
      for (let idx = 0; idx < opts.crops.length; idx += 1) {
        const selector = opts.crops[idx];
        const cropFile = `${pageCfg.id}-desktop-crop-${idx + 1}.png`;
        const loc = page.locator(selector).first();
        if ((await loc.count()) > 0) {
          await loc.scrollIntoViewIfNeeded().catch(() => {});
          await page.waitForTimeout(300);
          await loc.screenshot({ path: path.join(opts.outDir, cropFile) });
          const box = await loc.boundingBox().catch(() => null);
          screenshots.push({
            file: cropFile,
            page: pageCfg.id,
            route: pageCfg.route,
            kind: "element-crop",
            target: selector,
            note: box ? `${Math.round(box.width)}x${Math.round(box.height)}px` : "captured",
          });
        } else {
          errors.pageErrors.push(`Missing crop selector: ${selector}`);
        }
      }
      await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
    }

    if (isDesktop && !opts.noFullPage) {
      const fullFile = `${pageCfg.id}-desktop-1440-full.png`;
      const fullInfo = await takeFullPageScreenshot(page, path.join(opts.outDir, fullFile));
      screenshots.push({
        file: fullFile,
        page: pageCfg.id,
        route: pageCfg.route,
        kind: "desktop-full",
        target: `1440x${fullInfo.capturedHeight}`,
        note: fullInfo.capped ? `capped from ${fullInfo.fullHeight}px` : "full height",
      });
    }
  } finally {
    await context.close();
  }

  return { status, finalUrl, viewport, errors, extraction };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!opts.outDir) throw new Error("--out <dir> is required");
  if ((!opts.url && !opts.dist) || (opts.url && opts.dist)) {
    throw new Error("Specify exactly one of --url <https://...> or --dist <dist-dir>");
  }

  const isLocalDist = Boolean(opts.dist);
  const { chromium } = await loadPlaywright(opts.playwrightFrom);
  await mkdir(opts.outDir, { recursive: true });

  let ephemeral = null;
  let baseUrl = null;
  let routeList = [];

  if (isLocalDist) {
    if (!fs.existsSync(opts.dist) || !fs.statSync(opts.dist).isDirectory()) {
      throw new Error(`--dist directory does not exist: ${opts.dist}`);
    }
    ephemeral = await startEphemeralDistServer(opts.dist);
    baseUrl = ephemeral.baseUrl;
    routeList = opts.routes.length > 0 ? opts.routes : discoverDistRoutes(opts.dist);
    if (routeList.length === 0) routeList = ["/"];
  } else {
    const parsed = new URL(opts.url);
    if (parsed.protocol !== "https:" && !(opts.allowHttp && parsed.protocol === "http:")) {
      throw new Error(`Refusing URL scheme ${parsed.protocol} (use https:// or pass --allow-http)`);
    }
    const effectiveAllow = opts.allowHosts.length > 0 ? opts.allowHosts : [parsed.hostname];
    if (!hostMatchesAllowlist(parsed.hostname, effectiveAllow)) {
      throw new Error(`Target host ${parsed.hostname} is not in --allow-host allowlist`);
    }
    await assertRemoteHostSafe(parsed.hostname);
    baseUrl = `${parsed.protocol}//${parsed.host}`;
    routeList = opts.routes.length > 0 ? opts.routes : [parsed.pathname || "/"];
  }

  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-blink-features=AutomationControlled"],
  });

  const capturedAt = new Date().toISOString();
  const screenshots = [];
  const pagesData = [];

  try {
    for (const rawRoute of routeList) {
      const route = rawRoute.startsWith("/") ? rawRoute : `/${rawRoute}`;
      const targetUrl = new URL(route, baseUrl).toString();
      const id = routeToSlug(route);
      const pageCfg = { id, route, url: targetUrl };

      const entry = { id, route, url: targetUrl, desktop: null, mobile: null };
      entry.desktop = await runCaptureForViewport(browser, pageCfg, "desktop", opts, screenshots, isLocalDist);
      if (!opts.noMobile) {
        entry.mobile = await runCaptureForViewport(browser, pageCfg, "mobile", opts, screenshots, isLocalDist);
      }
      pagesData.push(entry);
    }
  } finally {
    await browser.close().catch(() => {});
    if (ephemeral) {
      await new Promise((resolve) => ephemeral.server.close(() => resolve()));
    }
  }

  const aggregates = aggregateInventory(pagesData);
  const twin = {
    schema: "legends.astro-design-twin/v1",
    mode: isLocalDist ? "dist" : "url",
    target: isLocalDist ? opts.dist : opts.url,
    capturedAt,
    outDir: opts.outDir,
    screenshots,
    pages: pagesData,
    aggregates,
  };

  const twinJsonPath = path.join(opts.outDir, "twin.json");
  const summaryMdPath = path.join(opts.outDir, "twin-audit-summary.md");
  await writeFile(twinJsonPath, `${JSON.stringify(twin, null, 2)}\n`, "utf8");
  await writeFile(summaryMdPath, `${buildAuditMarkdown(twin)}\n`, "utf8");

  process.stdout.write(
    `${JSON.stringify({
      ok: true,
      mode: twin.mode,
      pageCount: pagesData.length,
      screenshotCount: screenshots.length,
      twinJsonPath,
      summaryMdPath,
    }, null, 2)}\n`,
  );
}

main().catch((err) => {
  process.stdout.write(
    `${JSON.stringify({ ok: false, code: "twin_error", message: String(err?.message || err) }, null, 2)}\n`,
  );
  process.exit(1);
});
