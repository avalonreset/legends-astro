#!/usr/bin/env node
/**
 * Reusable Playwright Visual + Computed-Style Digital Twin capture worker for legends-astro.
 *
 * Zero mandatory npm dependencies: Playwright is discovered optionally at runtime via:
 *   1. --playwright-from <dir-with-node_modules/playwright or package.json>
 *   2. default module-scoped import("playwright")
 *   3. process.env.PLAYWRIGHT_PACKAGE_JSON
 *   (Never falls back implicitly to process.cwd() or root package self-referencing.)
 *
 * Targets either:
 *   - --url <https://...> with --allow-host <domain> (SSRF + page.route IP guard)
 *   - --dist <dist-dir> (ephemeral in-process 127.0.0.1:0 read-only static server closed in finally)
 *
 * Never spawns detached daemons, never opens a browser window, never reads user browser profiles.
 */

import crypto from "node:crypto";
import dns from "node:dns/promises";
import fs from "node:fs";
import { mkdir } from "node:fs/promises";
import http from "node:http";
import https from "node:https";
import { createRequire } from "node:module";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
  const [a, b, c] = parts;
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return true;
  if (a === 192 && b === 168) return true;
  if (a === 198 && (b === 18 || b === 19)) return true;
  if (a === 198 && b === 51 && c === 100) return true;
  if (a === 203 && b === 0 && c === 113) return true;
  if (a >= 224) return true;
  return false;
}

export function parseIPv6Groups(rawIp) {
  let ip = String(rawIp || "").trim().replace(/^\[|\]$/g, "").split("%")[0].toLowerCase();
  if (!ip) return null;

  // Expand embedded IPv4 dotted-quad suffix (e.g. ::ffff:127.0.0.1)
  const lastColon = ip.lastIndexOf(":");
  if (lastColon !== -1 && ip.slice(lastColon + 1).includes(".")) {
    const v4Part = ip.slice(lastColon + 1);
    if (!net.isIPv4(v4Part)) return null;
    const octets = v4Part.split(".").map(Number);
    const hi = ((octets[0] << 8) | octets[1]).toString(16);
    const lo = ((octets[2] << 8) | octets[3]).toString(16);
    ip = `${ip.slice(0, lastColon + 1)}${hi}:${lo}`;
  }

  const doubleParts = ip.split("::");
  if (doubleParts.length > 2) return null;

  let left = [];
  let right = [];
  if (doubleParts.length === 2) {
    left = doubleParts[0] ? doubleParts[0].split(":") : [];
    right = doubleParts[1] ? doubleParts[1].split(":") : [];
    const missing = 8 - (left.length + right.length);
    if (missing < 1) return null;
    const expanded = [...left, ...new Array(missing).fill("0"), ...right];
    const groups = expanded.map((g) => (/^[0-9a-f]{1,4}$/.test(g) ? parseInt(g, 16) : NaN));
    return groups.length === 8 && groups.every((n) => Number.isInteger(n) && n >= 0 && n <= 0xffff) ? groups : null;
  }

  const rawGroups = ip.split(":");
  if (rawGroups.length !== 8) return null;
  const groups = rawGroups.map((g) => (/^[0-9a-f]{1,4}$/.test(g) ? parseInt(g, 16) : NaN));
  return groups.every((n) => Number.isInteger(n) && n >= 0 && n <= 0xffff) ? groups : null;
}

export function isForbiddenIp(rawIp) {
  const ip = String(rawIp || "").trim().replace(/^\[|\]$/g, "").split("%")[0].toLowerCase();
  if (!ip) return true;
  if (net.isIPv4(ip)) return isForbiddenIPv4(ip);
  if (net.isIPv6(ip)) {
    const groups = parseIPv6Groups(ip);
    if (!groups) return true;
    const [g0, g1, g2] = groups;
    // Allow ONLY global unicast 2000::/3 (automatically rejects ::, ::1, 0:0:...:1, ::ffff:0:0/96,
    // ::ffff:0:0:0/96, 64:ff9b::/96, 64:ff9b:1::/48, 100::/64, fc00::/7, fe80::/10, ff00::/8)
    if ((g0 & 0xe000) !== 0x2000) return true;
    // Reject special/transition/doc prefixes inside 2000::/3
    if (g0 === 0x2002) return true; // 6to4 2002::/16
    if (g0 === 0x2001 && g1 === 0x0000) return true; // Teredo 2001:0000::/32
    if (g0 === 0x2001 && g1 === 0x0db8) return true; // Documentation 2001:db8::/32
    if (g0 === 0x2001 && g1 === 0x0002 && g2 === 0x0000) return true; // Benchmarking 2001:2::/48
    if (g0 === 0x2001 && (g1 & 0xfff0) === 0x0010) return true; // ORCHID 2001:10::/28
    if (g0 === 0x2001 && (g1 & 0xfff0) === 0x0020) return true; // ORCHIDv2 2001:20::/28
    return false;
  }
  return true;
}

export async function assertRemoteHostSafe(hostname) {
  const host = String(hostname || "").trim().toLowerCase().replace(/\.$/, "");
  const unbracketed = host.replace(/^\[|\]$/g, "");
  if (!unbracketed || unbracketed === "localhost" || unbracketed.endsWith(".localhost")) {
    throw new Error(`Refusing forbidden hostname (CWE-918): ${hostname}`);
  }
  if (net.isIP(unbracketed)) {
    if (isForbiddenIp(unbracketed)) {
      throw new Error(`Refusing forbidden IP literal (CWE-918): ${hostname}`);
    }
    return [unbracketed];
  }
  const records = await dns.lookup(unbracketed, { all: true });
  if (!records.length) {
    throw new Error(`DNS resolution returned no addresses for ${unbracketed}`);
  }
  const v4 = [];
  const v6 = [];
  for (const rec of records) {
    if (isForbiddenIp(rec.address)) {
      throw new Error(`Refusing SSRF-unsafe resolved IP ${rec.address} for ${unbracketed} (CWE-918)`);
    }
    if (rec.family === 4) v4.push(rec.address);
    else v6.push(rec.address);
  }
  return [...v4, ...v6];
}

export function hostMatchesAllowlist(hostname, allowHosts) {
  const host = String(hostname || "").trim().toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (!host) return false;
  for (const entry of allowHosts) {
    const p = String(entry || "").trim().toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
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

export function validateRoute(rawRoute, baseUrl) {
  const r = String(rawRoute ?? "").trim();
  if (!r) return "/";
  if (/[\x00-\x1f\x7f]/.test(r)) {
    throw new Error(`Refusing --route with control characters (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  if (r.includes("\\")) {
    throw new Error(`Refusing --route with backslash character (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  if (r.startsWith("//")) {
    throw new Error(`Refusing protocol-relative --route (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(r)) {
    throw new Error(`Refusing absolute/scheme --route (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  const normalized = r.startsWith("/") ? r : `/${r}`;
  if (normalized.startsWith("//")) {
    throw new Error(`Refusing protocol-relative --route (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  const baseObj = new URL(baseUrl);
  const targetObj = new URL(normalized, baseObj);
  if (targetObj.origin !== baseObj.origin) {
    throw new Error(`Refusing --route escaping target origin (CWE-918): ${JSON.stringify(rawRoute)}`);
  }
  return normalized;
}

export function routeToSlug(route, usedIds = null) {
  const clean = String(route || "/").split("#")[0].split("?")[0].trim();
  let base;
  if (!clean || clean === "/" || clean === "/index.html") {
    base = "home";
  } else {
    base =
      clean
        .replace(/^\/+|\/+$/g, "")
        .replace(/\.html?$/i, "")
        .replace(/[^A-Za-z0-9_-]+/g, "__") || "page";
  }
  if (!usedIds) return base;
  let slug = base;
  let counter = 2;
  while (usedIds.has(slug.toLowerCase())) {
    slug = `${base}__${counter}`;
    counter += 1;
  }
  usedIds.add(slug.toLowerCase());
  return slug;
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
    overwrite: false,
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
    } else if (arg === "--overwrite") {
      out.overwrite = true;
    } else if (arg === "--playwright-from" && argv[i + 1]) {
      out.playwrightFrom = argv[++i];
    }
  }
  return out;
}

const MACOS_SYSTEM_SYMLINKS = new Set(["/var", "/tmp", "/etc", "/private/var", "/private/tmp", "/private/etc"]);

function normForRealpathCompare(p) {
  const resolved = path.resolve(p);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

export function isReparseOrSymlink(targetPath) {
  try {
    const lstat = fs.lstatSync(targetPath);
    if (lstat.isSymbolicLink()) return true;
    const real = fs.realpathSync(targetPath);
    const parentReal = fs.realpathSync(path.dirname(path.resolve(targetPath)));
    const expectedUnderParent = path.join(parentReal, path.basename(path.resolve(targetPath)));
    if (normForRealpathCompare(real) !== normForRealpathCompare(expectedUnderParent)) {
      return true;
    }
    return false;
  } catch (err) {
    if (err && (err.code === "ENOENT" || err.code === "ENOTDIR")) return false;
    return true;
  }
}

export function rejectSymlinkChain(targetPath) {
  let cur = path.resolve(targetPath);
  while (true) {
    const parent = path.dirname(cur);
    if (parent === cur) break;
    if (!(process.platform === "darwin" && MACOS_SYSTEM_SYMLINKS.has(cur))) {
      if (isReparseOrSymlink(cur)) {
        throw new Error(`Refusing symlink or reparse-point path (CWE-59): ${cur}`);
      }
    }
    cur = parent;
  }
}

function assertSafeOutputTarget(targetPath, overwrite) {
  rejectSymlinkChain(targetPath);
  let exists = false;
  try {
    const st = fs.lstatSync(targetPath);
    if (st.isSymbolicLink() || isReparseOrSymlink(targetPath)) {
      throw new Error(`Refusing symlink or reparse-point output path (CWE-59): ${targetPath}`);
    }
    if (!st.isFile()) {
      throw new Error(`Refusing non-regular output target: ${targetPath}`);
    }
    exists = true;
  } catch (err) {
    if (err && err.code !== "ENOENT") throw err;
  }
  if (exists && !overwrite) {
    throw new Error(`Refusing to overwrite existing file without --overwrite: ${targetPath}`);
  }
}

function safeWriteFileSync(targetPath, data, overwrite) {
  assertSafeOutputTarget(targetPath, overwrite);
  if (!overwrite) {
    fs.writeFileSync(targetPath, data, { flag: "wx", mode: 0o600 });
    return;
  }
  const dir = path.dirname(targetPath);
  const base = path.basename(targetPath);
  let tmpPath = null;
  for (let i = 0; i < 16; i += 1) {
    const cand = path.join(dir, `.${base}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`);
    try {
      fs.writeFileSync(cand, data, { flag: "wx", mode: 0o600 });
      tmpPath = cand;
      break;
    } catch (err) {
      if (err && err.code === "EEXIST") continue;
      throw err;
    }
  }
  if (!tmpPath) {
    throw new Error(`Unable to allocate temporary file in ${dir}`);
  }
  try {
    if (isReparseOrSymlink(dir) || isReparseOrSymlink(targetPath)) {
      throw new Error(`Refusing symlink or reparse-point output path (CWE-59): ${targetPath}`);
    }
    fs.renameSync(tmpPath, targetPath);
  } catch (err) {
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      // ignore cleanup failure
    }
    throw err;
  }
}

function loadPlaywrightFromAnchor(rawAnchor, sourceLabel) {
  rejectSymlinkChain(rawAnchor);
  const resolved = path.resolve(rawAnchor);
  if (!fs.existsSync(resolved)) {
    throw new Error(`${sourceLabel} does not exist: ${resolved}`);
  }
  const st = fs.lstatSync(resolved);
  if (st.isSymbolicLink() || isReparseOrSymlink(resolved)) {
    throw new Error(`Refusing symlink or junction in ${sourceLabel} (CWE-59): ${resolved}`);
  }
  const anchorDir = st.isDirectory() ? resolved : path.dirname(resolved);
  // Never pass a consumer root package.json to createRequire() (prevents Node Package Self-Referencing RCE, CWE-94).
  const alreadyInNodeModules =
    path.basename(anchorDir) === "playwright" &&
    path.basename(path.dirname(anchorDir)) === "node_modules";
  const nodeModulesDir = alreadyInNodeModules
    ? path.dirname(anchorDir)
    : path.join(anchorDir, "node_modules");
  const pwDir = alreadyInNodeModules
    ? anchorDir
    : path.join(nodeModulesDir, "playwright");

  rejectSymlinkChain(nodeModulesDir);
  if (!fs.existsSync(pwDir)) {
    throw new Error(`No verified node_modules/playwright/package.json found under ${anchorDir}`);
  }

  let verifiedPwDir = pwDir;
  if (isReparseOrSymlink(pwDir)) {
    // Allow strictly contained pnpm store links: node_modules/playwright -> node_modules/.pnpm/.../node_modules/playwright
    const realNm = fs.realpathSync(nodeModulesDir);
    const realPw = fs.realpathSync(pwDir);
    const rel = path.relative(realNm, realPw);
    const segs = rel.split(path.sep).filter(Boolean);
    const isContainedPnpmLink =
      !rel.startsWith("..") &&
      !path.isAbsolute(rel) &&
      segs[0] === ".pnpm" &&
      path.basename(realPw) === "playwright" &&
      path.basename(path.dirname(realPw)) === "node_modules";
    if (!isContainedPnpmLink) {
      throw new Error(`Refusing symlink or reparse-point path (CWE-59): ${pwDir}`);
    }
    verifiedPwDir = realPw;
  }

  const pwPkgPath = path.join(verifiedPwDir, "package.json");
  if (!fs.existsSync(pwPkgPath)) {
    throw new Error(`No verified node_modules/playwright/package.json found under ${anchorDir}`);
  }
  const pkgSt = fs.lstatSync(pwPkgPath);
  if (!pkgSt.isFile() || pkgSt.isSymbolicLink() || isReparseOrSymlink(pwPkgPath)) {
    throw new Error(`Refusing non-regular or symlinked playwright package.json: ${pwPkgPath}`);
  }
  const pkgJson = JSON.parse(fs.readFileSync(pwPkgPath, "utf8"));
  if (!pkgJson || pkgJson.name !== "playwright") {
    throw new Error(`Refusing package at ${pwPkgPath}: expected name === "playwright"`);
  }
  const req = createRequire(pwPkgPath);
  return req(verifiedPwDir);
}

export async function loadPlaywright(playwrightFrom, { exitOnMissing = true } = {}) {
  const attempts = [];
  if (playwrightFrom) {
    try {
      return loadPlaywrightFromAnchor(playwrightFrom, "--playwright-from");
    } catch (err) {
      attempts.push(`--playwright-from (${playwrightFrom}): ${err.message}`);
    }
  }

  try {
    return await import("playwright");
  } catch (err) {
    attempts.push(`default import('playwright'): ${err.message}`);
  }

  const envPkg = process.env.PLAYWRIGHT_PACKAGE_JSON;
  if (envPkg) {
    try {
      return loadPlaywrightFromAnchor(envPkg, "PLAYWRIGHT_PACKAGE_JSON");
    } catch (err) {
      attempts.push(`PLAYWRIGHT_PACKAGE_JSON (${envPkg}): ${err.message}`);
    }
  }

  // Implicit process.cwd() fallback intentionally removed (C-01 / CWE-94 / CWE-426).
  const diagnostic = {
    ok: false,
    code: "playwright_not_installed",
    message:
      "Optional Playwright runtime is not installed. Install Playwright in your target project (`npm install -D playwright && npx playwright install chromium`) and pass `--playwright-from <dir-with-playwright>` or set `PLAYWRIGHT_PACKAGE_JSON`.",
    attempts,
  };
  if (!exitOnMissing) {
    const err = new Error(diagnostic.message);
    err.diagnostic = diagnostic;
    throw err;
  }
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
    // Reject symlinks or NTFS junctions along any path segment inside distRoot
    const relFromDist = path.relative(path.resolve(distRoot), path.resolve(candidatePath));
    if (relFromDist.startsWith("..") || path.isAbsolute(relFromDist)) return false;
    let cur = path.resolve(distRoot);
    for (const seg of relFromDist.split(path.sep).filter(Boolean)) {
      cur = path.join(cur, seg);
      if (isReparseOrSymlink(cur)) return false;
    }
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
      if (entry.isSymbolicLink() || isReparseOrSymlink(full)) continue;
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

async function takeFullPageScreenshot(page, filePath, overwrite) {
  await triggerLazyAndReveals(page);
  const dims = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
    innerWidth: window.innerWidth,
  }));
  assertSafeOutputTarget(filePath, overwrite);
  if (dims.scrollHeight <= FULL_PAGE_MAX_HEIGHT) {
    const buf = await page.screenshot({ fullPage: true });
    safeWriteFileSync(filePath, buf, overwrite);
    return { fullHeight: dims.scrollHeight, capped: false, capturedHeight: dims.scrollHeight };
  }
  const buf = await page.screenshot({
    fullPage: true,
    clip: { x: 0, y: 0, width: dims.innerWidth, height: FULL_PAGE_MAX_HEIGHT },
  });
  safeWriteFileSync(filePath, buf, overwrite);
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

const MAX_PINNED_RESPONSE_BYTES = 50 * 1024 * 1024;
const PINNED_REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);

async function fetchPinnedHttpResponse(initialUrl, reqMethod, reqHeaders, postBody, opts, effectiveAllow, maxRedirects = 5) {
  let currentUrl = initialUrl;
  let method = reqMethod || "GET";
  let bodyToSend = postBody || null;
  let headersToSend = { ...reqHeaders };

  for (let hop = 0; hop <= maxRedirects; hop += 1) {
    const u = new URL(currentUrl);
    if (u.protocol !== "https:" && !(opts.allowHttp && u.protocol === "http:")) {
      throw new Error(`Refusing scheme ${u.protocol}`);
    }
    if (!hostMatchesAllowlist(u.hostname, effectiveAllow)) {
      throw new Error(`Host ${u.hostname} outside --allow-host allowlist`);
    }
    const ips = await assertRemoteHostSafe(u.hostname);
    const pinnedIp = ips[0];
    const family = net.isIP(pinnedIp) === 6 ? 6 : 4;
    const mod = u.protocol === "https:" ? https : http;

    const cleanHeaders = {};
    for (const [k, v] of Object.entries(headersToSend)) {
      const kl = k.toLowerCase();
      if (kl === "host" || kl === "connection" || kl === "content-length" || kl === "accept-encoding") continue;
      cleanHeaders[k] = v;
    }
    cleanHeaders.host = u.host;
    cleanHeaders["accept-encoding"] = "identity";

    const resp = await new Promise((resolve, reject) => {
      const req = mod.request(
        {
          protocol: u.protocol,
          hostname: u.hostname.replace(/^\[|\]$/g, ""),
          port: u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80),
          path: `${u.pathname}${u.search}`,
          method,
          headers: cleanHeaders,
          timeout: opts.timeoutMs,
          lookup: (_h, lookupOpts, cb) => {
            if (lookupOpts && lookupOpts.all) {
              cb(null, [{ address: pinnedIp, family }]);
            } else {
              cb(null, pinnedIp, family);
            }
          },
          servername: u.protocol === "https:" && !net.isIP(u.hostname.replace(/^\[|\]$/g, "")) ? u.hostname : undefined,
        },
        (res) => {
          const chunks = [];
          let total = 0;
          res.on("data", (chunk) => {
            total += chunk.length;
            if (total > MAX_PINNED_RESPONSE_BYTES) {
              req.destroy(new Error("Pinned response exceeded size cap"));
              return;
            }
            chunks.push(chunk);
          });
          res.on("end", () => {
            resolve({
              status: res.statusCode || 200,
              headers: res.headers,
              body: Buffer.concat(chunks),
            });
          });
        },
      );
      req.on("timeout", () => req.destroy(new Error("Pinned request timed out")));
      req.on("error", reject);
      if (bodyToSend) req.write(bodyToSend);
      req.end();
    });

    if (PINNED_REDIRECT_CODES.has(resp.status) && resp.headers.location) {
      if (hop >= maxRedirects) throw new Error("Too many redirects");
      currentUrl = new URL(String(resp.headers.location), currentUrl).toString();
      if (resp.status === 303) {
        method = "GET";
        bodyToSend = null;
      }
      const nextHeaders = {};
      for (const [k, v] of Object.entries(headersToSend)) {
        const kl = k.toLowerCase();
        if (kl === "authorization" || kl === "proxy-authorization" || kl === "cookie") continue;
        nextHeaders[k] = v;
      }
      headersToSend = nextHeaders;
      continue;
    }

    const flatHeaders = {};
    for (const [k, v] of Object.entries(resp.headers)) {
      if (v === undefined) continue;
      if (k.toLowerCase() === "transfer-encoding") continue;
      flatHeaders[k] = Array.isArray(v) ? v.join(", ") : String(v);
    }
    return { status: resp.status, headers: flatHeaders, body: resp.body };
  }
  throw new Error("Too many redirects");
}

async function runCaptureForViewport(browser, pageCfg, mode, opts, screenshots, isLocalDist, netCtx) {
  const isDesktop = mode === "desktop";
  const viewport = isDesktop ? { width: 1440, height: 900 } : { width: 390, height: 844 };
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: isDesktop ? 1 : 2,
    userAgent: isDesktop ? DESKTOP_UA : MOBILE_UA,
    locale: "en-US",
    extraHTTPHeaders: { "Accept-Language": "en-US,en;q=0.9" },
  });

  if (isLocalDist) {
    // Strict local-dist network isolation: permit ONLY the ephemeral 127.0.0.1:<port> server + data/blob/about:blank
    const allowedOrigin = netCtx.ephemeralOrigin;
    await context.route("**/*", async (route) => {
      const reqUrl = route.request().url();
      if (reqUrl.startsWith("data:") || reqUrl.startsWith("blob:") || reqUrl === "about:blank") {
        return route.continue();
      }
      try {
        const u = new URL(reqUrl);
        if (u.origin === allowedOrigin) {
          return route.continue();
        }
      } catch {
        // fall through to abort
      }
      return route.abort("blockedbyclient");
    });
    if (typeof context.routeWebSocket === "function") {
      await context.routeWebSocket("**/*", (ws) => {
        ws.close();
      });
    }
  } else {
    // Strict SSRF + allowlist + DNS-rebinding defense on remote captures
    const { effectiveAllow, pinnedHosts } = netCtx;
    await context.route("**/*", async (route) => {
      const req = route.request();
      const reqUrl = req.url();
      if (reqUrl.startsWith("data:") || reqUrl.startsWith("blob:") || reqUrl === "about:blank") {
        return route.continue();
      }
      try {
        const u = new URL(reqUrl);
        if (u.protocol !== "https:" && !(opts.allowHttp && u.protocol === "http:")) {
          return route.abort("blockedbyclient");
        }
        const hostKey = u.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
        if (!hostMatchesAllowlist(hostKey, effectiveAllow)) {
          return route.abort("blockedbyclient");
        }
        if (pinnedHosts.has(hostKey)) {
          // Host is pinned in Chromium via --host-resolver-rules to its pre-validated global IP
          return route.continue();
        }
        if (net.isIP(hostKey)) {
          if (isForbiddenIp(hostKey)) return route.abort("blockedbyclient");
          return route.continue();
        }
        // Wildcard-matched host not pre-pinned in Chromium: fulfill via Node IP-pinned transport (zero DNS TOCTOU)
        const fulfilled = await fetchPinnedHttpResponse(
          reqUrl,
          req.method(),
          req.headers(),
          req.postDataBuffer(),
          opts,
          effectiveAllow,
        );
        return route.fulfill({
          status: fulfilled.status,
          headers: fulfilled.headers,
          body: fulfilled.body,
        });
      } catch {
        return route.abort("blockedbyclient");
      }
    });
    if (typeof context.routeWebSocket === "function") {
      await context.routeWebSocket("**/*", (ws) => {
        try {
          const u = new URL(ws.url());
          const schemeOk = u.protocol === "wss:" || (opts.allowHttp && u.protocol === "ws:");
          const hostKey = u.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
          if (!schemeOk || !hostMatchesAllowlist(hostKey, effectiveAllow)) {
            ws.close();
            return;
          }
          if (pinnedHosts.has(hostKey) || (net.isIP(hostKey) && !isForbiddenIp(hostKey))) {
            ws.connectToServer();
            return;
          }
        } catch {
          // close below
        }
        ws.close();
      });
    }
  }

  const page = await context.newPage();

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
    const vpPath = path.join(opts.outDir, vpFile);
    assertSafeOutputTarget(vpPath, opts.overwrite);
    const vpBuf = await page.screenshot({ fullPage: false });
    safeWriteFileSync(vpPath, vpBuf, opts.overwrite);
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
        const cropPath = path.join(opts.outDir, cropFile);
        const loc = page.locator(selector).first();
        if ((await loc.count()) > 0) {
          await loc.scrollIntoViewIfNeeded().catch(() => {});
          await page.waitForTimeout(300);
          assertSafeOutputTarget(cropPath, opts.overwrite);
          const cropBuf = await loc.screenshot();
          safeWriteFileSync(cropPath, cropBuf, opts.overwrite);
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
      const fullInfo = await takeFullPageScreenshot(page, path.join(opts.outDir, fullFile), opts.overwrite);
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

  // Validate ancestor symlink/junction chains BEFORE creating any directory or launching Playwright
  rejectSymlinkChain(opts.outDir);
  if (opts.dist) {
    rejectSymlinkChain(opts.dist);
  }

  const isLocalDist = Boolean(opts.dist);
  let ephemeral = null;
  let baseUrl = null;
  let routeList = [];
  let netCtx = {};
  const launchArgs = ["--disable-blink-features=AutomationControlled"];

  if (isLocalDist) {
    if (!fs.existsSync(opts.dist) || !fs.statSync(opts.dist).isDirectory()) {
      throw new Error(`--dist directory does not exist: ${opts.dist}`);
    }
    const rawRoutes = opts.routes.length > 0 ? opts.routes : discoverDistRoutes(opts.dist);
    for (const rr of rawRoutes) {
      validateRoute(rr, "http://127.0.0.1:8080");
    }
  } else {
    const parsed = new URL(opts.url);
    if (parsed.protocol !== "https:" && !(opts.allowHttp && parsed.protocol === "http:")) {
      throw new Error(`Refusing URL scheme ${parsed.protocol} (use https:// or pass --allow-http)`);
    }
    if (parsed.username || parsed.password) {
      throw new Error("Refusing URL with embedded credentials");
    }
    const effectiveAllow = opts.allowHosts.length > 0 ? opts.allowHosts : [parsed.hostname];
    if (!hostMatchesAllowlist(parsed.hostname, effectiveAllow)) {
      throw new Error(`Target host ${parsed.hostname} is not in --allow-host allowlist`);
    }
    const targetIps = await assertRemoteHostSafe(parsed.hostname);
    baseUrl = `${parsed.protocol}//${parsed.host}`;
    const rawRoutes = opts.routes.length > 0 ? opts.routes : [parsed.pathname || "/"];
    routeList = rawRoutes.map((rr) => validateRoute(rr, baseUrl));

    // Pre-resolve target host and all exact allowlisted hosts to pin Chromium DNS via --host-resolver-rules
    const pinnedHosts = new Map();
    const targetHostKey = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
    if (!net.isIP(targetHostKey)) {
      pinnedHosts.set(targetHostKey, targetIps[0]);
    }
    for (const entry of effectiveAllow) {
      const cleaned = String(entry || "").trim().toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
      if (!cleaned || cleaned.startsWith("*.") || net.isIP(cleaned) || pinnedHosts.has(cleaned)) continue;
      try {
        const ips = await assertRemoteHostSafe(cleaned);
        if (ips.length > 0) pinnedHosts.set(cleaned, ips[0]);
      } catch (err) {
        if (String(err?.message || "").includes("CWE-918")) {
          throw err;
        }
        // Non-target optional allowlist host failed DNS lookup; leave unpinned so runtime route handler handles/aborts
      }
    }
    if (pinnedHosts.size > 0) {
      const rules = [];
      for (const [h, ip] of pinnedHosts.entries()) {
        const fmtIp = ip.includes(":") ? `[${ip}]` : ip;
        rules.push(`MAP ${h} ${fmtIp}`);
      }
      launchArgs.push(`--host-resolver-rules=${rules.join(", ")}`);
    }
    netCtx = { effectiveAllow, pinnedHosts };
  }

  const { chromium } = await loadPlaywright(opts.playwrightFrom);
  await mkdir(opts.outDir, { recursive: true });
  rejectSymlinkChain(opts.outDir);

  if (isLocalDist) {
    ephemeral = await startEphemeralDistServer(opts.dist);
    baseUrl = ephemeral.baseUrl;
    const rawRoutes = opts.routes.length > 0 ? opts.routes : discoverDistRoutes(opts.dist);
    routeList = (rawRoutes.length > 0 ? rawRoutes : ["/"]).map((rr) => validateRoute(rr, baseUrl));
    netCtx = { ephemeralOrigin: ephemeral.baseUrl };
  }

  // Fail-fast check on twin.json and twin-audit-summary.md before launching Chromium
  const twinJsonPath = path.join(opts.outDir, "twin.json");
  const summaryMdPath = path.join(opts.outDir, "twin-audit-summary.md");
  assertSafeOutputTarget(twinJsonPath, opts.overwrite);
  assertSafeOutputTarget(summaryMdPath, opts.overwrite);

  const browser = await chromium.launch({
    headless: true,
    args: launchArgs,
  });

  const capturedAt = new Date().toISOString();
  const screenshots = [];
  const pagesData = [];

  const usedIds = new Set();
  try {
    for (const route of routeList) {
      const targetUrl = new URL(route, baseUrl).toString();
      const id = routeToSlug(route, usedIds);
      const pageCfg = { id, route, url: targetUrl };

      const entry = { id, route, url: targetUrl, desktop: null, mobile: null };
      entry.desktop = await runCaptureForViewport(browser, pageCfg, "desktop", opts, screenshots, isLocalDist, netCtx);
      if (!opts.noMobile) {
        entry.mobile = await runCaptureForViewport(browser, pageCfg, "mobile", opts, screenshots, isLocalDist, netCtx);
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

  safeWriteFileSync(twinJsonPath, `${JSON.stringify(twin, null, 2)}\n`, opts.overwrite);
  safeWriteFileSync(summaryMdPath, `${buildAuditMarkdown(twin)}\n`, opts.overwrite);

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

const isEntrypoint =
  Boolean(process.argv[1]) &&
  normForRealpathCompare(fileURLToPath(import.meta.url)) === normForRealpathCompare(process.argv[1]);

if (isEntrypoint) {
  main().catch((err) => {
    process.stdout.write(
      `${JSON.stringify({ ok: false, code: "twin_error", message: String(err?.message || err) }, null, 2)}\n`,
    );
    process.exit(1);
  });
}
