import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
assert.ok(fs.existsSync(path.join(dist, "index.html")), "dist/index.html missing; run astro build first");

const index = fs.readFileSync(path.join(dist, "index.html"), "utf8");
const privacy = fs.readFileSync(path.join(dist, "privacy.html"), "utf8");
const terms = fs.readFileSync(path.join(dist, "terms.html"), "utf8");
const app = fs.readFileSync(path.join(root, "public", "gate-app-canary.js"), "utf8");
const distApp = fs.readFileSync(path.join(dist, "gate-app-canary.js"), "utf8");
const distGuard = fs.readFileSync(path.join(dist, "gate-frame-guard-v1.js"), "utf8");

const htmlTag = index.match(/<html\b[^>]*>/i)?.[0] ?? "";
const firstScript = index.search(/<script\b/i);
const firstCriticalStyle = index.search(/<style\b[^>]*data-first-paint-black/i);
const beforeScript = firstScript >= 0 ? index.slice(0, firstScript) : index;

assert.match(htmlTag, /background\s*:\s*#000/i, "root paints black");
assert.match(htmlTag, /color-scheme\s*:\s*dark/i, "root declares dark scheme");
assert.ok(firstCriticalStyle >= 0 && firstCriticalStyle < firstScript, "critical style precedes scripts");
assert.match(
  beforeScript,
  /<style\b[^>]*data-first-paint-black[^>]*>\s*html,body\{margin:0;background:#000;color:#fff\}\s*<\/style>/i,
);
assert.match(beforeScript, /theme-color["']\s+content=["']#000000["']/i);
assert.match(index, /<script[^>]*defer[^>]*src="\/gate-frame-guard-v1\.js"/i);
assert.match(index, /<script[^>]*type="module"[^>]*src="\/gate-app-canary\.js"/i);
assert.match(index, /src="\/gate-frame-guard-v1\.js"/);
assert.match(index, /src="\/gate-app-canary\.js"/);

const requiredIds = [
  "analyticsDecision",
  "emailGate",
  "unlockBtn",
  "identityDoors",
  "doorGithub",
  "doorGoogle",
  "voicePanel",
  "shrinePanel",
  "rungsMonth",
  "rungsYear",
  "rungsOnce",
  "reviewModal",
  "reviewBack",
  "reviewContinue",
];
for (const id of requiredIds) {
  assert.ok(index.includes(`id="${id}"`), `built homepage has #${id}`);
  assert.match(app, new RegExp(`getElementById\\("${id}"\\)`), `canary app looks up ${id}`);
}

assert.equal(distApp, app);
assert.match(distGuard, /iframe,frame/);
const astroCssDir = path.join(dist, "_astro");
const cssFiles = fs.existsSync(astroCssDir)
  ? fs.readdirSync(astroCssDir).filter((name) => name.endsWith(".css"))
  : [];
assert.ok(cssFiles.length > 0, "expected generated global CSS under dist/_astro");
const bundledCss = cssFiles
  .map((name) => fs.readFileSync(path.join(astroCssDir, name), "utf8"))
  .join("\n");
assert.match(bundledCss, /\.rung-btn\{/);
assert.match(bundledCss, /body\.mode-shrine/);
assert.doesNotMatch(bundledCss, /\.rung-btn\[data-astro-cid/);
assert.match(index, /\/_astro\//);
assert.match(app, /createElement\("button"\)/);
assert.match(app, /className = "rung-btn"/);

assert.match(index, /<link rel="canonical" href="https:\/\/canary\.example\.test\/"\s*\/?>/);
assert.match(privacy, /<link rel="canonical" href="https:\/\/canary\.example\.test\/privacy"\s*\/?>/);
assert.match(terms, /<link rel="canonical" href="https:\/\/canary\.example\.test\/terms"\s*\/?>/);
assert.match(privacy, /data-analytics-choice="opt-out"/);
assert.match(privacy, /cto_analytics_choice/);
assert.match(privacy, /globalPrivacyControl/);
assert.doesNotMatch(index, /googletagmanager/);

assert.ok(index.includes("public note"));
assert.ok(index.includes("/notes/visible"));
assert.equal(index.includes("CANARY_DRAFT_NOTE_BODY"), false);
assert.equal(index.includes("secret-draft"), false);
assert.equal(index.includes("draft note"), false);

const visible = path.join(dist, "notes", "visible.html");
const draft = path.join(dist, "notes", "secret-draft.html");
assert.ok(fs.existsSync(visible), "published note route missing");
assert.equal(fs.existsSync(draft), false, "draft note must not be emitted");
const visibleHtml = fs.readFileSync(visible, "utf8");
assert.ok(visibleHtml.includes("CANARY_PUBLIC_NOTE_BODY"));
assert.equal(visibleHtml.includes("CANARY_DRAFT_NOTE_BODY"), false);

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}
const distText = walk(dist)
  .filter((file) => /\.(html|js|css|json|txt)$/i.test(file))
  .map((file) => fs.readFileSync(file, "utf8"))
  .join("\n");
assert.equal(distText.includes("CANARY_DRAFT_NOTE_BODY"), false, "draft body leaked into dist");

assert.doesNotMatch(index + privacy + terms + app, /sk_live|sk_test|whsec_|GHL_API_KEY|nsec/i);

console.log("PASS built canary static: first-paint, IDs, scripts, global CSS, public note, no draft");
