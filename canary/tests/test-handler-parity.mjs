import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  createFixtureServer,
  ga4RequestAllowed,
  htmlContentSecurityPolicy,
  injectGa4,
  isFingerprintedAsset,
  resolveUnder,
  TAGGABLE_HOST,
} from "../handler/send-html-stub.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
assert.ok(fs.existsSync(path.join(dist, "index.html")), "dist/index.html missing; run astro build first");

const stubSource = fs.readFileSync(path.join(root, "handler", "send-html-stub.mjs"), "utf8");
assert.doesNotMatch(stubSource, /\.env\.local/);
assert.doesNotMatch(stubSource, /migrateLegacy/);
assert.doesNotMatch(stubSource, /PERSISTENT_STATE/);
assert.doesNotMatch(stubSource, /ensureDurableEmailStateRecovered/);
assert.doesNotMatch(stubSource, /cto-legends-gate/);

function fakeReq({ path: route = "/", host = TAGGABLE_HOST, cookie = "", gpc = false, method = "GET" } = {}) {
  const url = new URL(route, `http://${host}`);
  const headers = { host, cookie };
  if (gpc) headers["sec-gpc"] = "1";
  return { method, headers, url };
}

const allow = "cto_analytics_choice=allow-v1";
const html = "<html><head></head><body>ok</body></html>";

{
  const req = fakeReq();
  const url = new URL("http://canary.example.test/");
  assert.equal(ga4RequestAllowed(req, url), false);
  assert.equal(injectGa4(html, { req, url, enabled: true }).includes("gtag"), false);
  assert.doesNotMatch(htmlContentSecurityPolicy({ req, url, enabled: true }), /googletagmanager/);
}

{
  const req = fakeReq({ cookie: allow });
  const url = new URL("http://canary.example.test/");
  const tagged = injectGa4(html, { req, url, enabled: true });
  assert.ok(tagged.includes("data-cto-ga4-scaffold"));
  assert.match(htmlContentSecurityPolicy({ req, url, enabled: true }), /googletagmanager/);
}

{
  const req = fakeReq({ cookie: allow, gpc: true });
  const url = new URL("http://canary.example.test/");
  assert.equal(ga4RequestAllowed(req, url), false);
}

assert.equal(resolveUnder(dist, "../package.json"), null);
assert.equal(resolveUnder(path.join(dist, "_astro"), "../index.html"), null);
assert.equal(resolveUnder(path.join(dist, "_astro"), "..%2findex.html"), null);

function request(server, route, headers = {}) {
  return new Promise((resolve, reject) => {
    const address = server.address();
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: address.port,
        path: route,
        method: "GET",
        headers: { Host: TAGGABLE_HOST, ...headers },
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    req.once("error", reject);
    req.end();
  });
}

const astroDir = path.join(dist, "_astro");
assert.ok(fs.existsSync(astroDir), "dist/_astro missing after build");
const astroFiles = fs.readdirSync(astroDir).filter((name) => isFingerprintedAsset(name));
assert.ok(astroFiles.length > 0, "expected hashed /_astro assets from the build");
const hashedCss = astroFiles.find((name) => name.endsWith(".css")) || astroFiles[0];

const server = createFixtureServer({ distDir: dist });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
try {
  const fresh = await request(server, "/");
  assert.equal(fresh.status, 200);
  assert.equal(fresh.headers["cache-control"], "no-store");
  assert.equal(fresh.body.includes("googletagmanager"), false);
  assert.doesNotMatch(fresh.headers["content-security-policy"], /google(tagmanager|-analytics)/i);
  assert.ok(fresh.body.includes('id="analyticsDecision"'));
  assert.ok(fresh.body.includes("data-first-paint-black"));
  assert.ok(fresh.body.includes("/notes/visible"));
  assert.equal(fresh.body.includes("CANARY_DRAFT_NOTE_BODY"), false);

  const tagged = await request(server, "/", { Cookie: allow });
  assert.ok(tagged.body.includes("data-cto-ga4-scaffold"));
  assert.match(tagged.headers["content-security-policy"], /googletagmanager/);

  const exclusions = [
    await request(server, "/?token=secret", { Cookie: allow }),
    await request(server, "/privacy", { Cookie: allow }),
    await request(server, "/terms", { Cookie: allow }),
    await request(server, "/notes/visible", { Cookie: allow }),
    await request(server, "/", { Cookie: `${allow}; cto_analytics_opt_out=1` }),
    await request(server, "/", { "Sec-GPC": "1", Cookie: allow }),
  ];
  for (const response of exclusions) {
    assert.equal(response.status, 200, `exclusion status ${response.status}`);
    assert.equal(response.body.includes("data-cto-ga4-scaffold"), false);
  }

  const indexAlias = await request(server, "/index.html");
  assert.equal(indexAlias.status, 301);
  assert.equal(indexAlias.headers.location, "/");

  const privacyAlias = await request(server, "/privacy.html");
  assert.equal(privacyAlias.status, 301);
  assert.equal(privacyAlias.headers.location, "/privacy");

  const contribute = await request(server, "/contribute?foo=1");
  assert.equal(contribute.status, 302);
  assert.equal(contribute.headers.location, "/?foo=1");

  const js = await request(server, "/gate-app-canary.js");
  assert.equal(js.status, 200);
  assert.equal(js.headers["cache-control"], "no-store");

  const note = await request(server, "/notes/visible");
  assert.equal(note.status, 200);
  assert.ok(note.body.includes("CANARY_PUBLIC_NOTE_BODY"));
  assert.equal(note.headers["cache-control"], "no-store");

  const draft = await request(server, "/notes/secret-draft");
  assert.equal(draft.status, 404);

  const draftHtml = await request(server, "/notes/secret-draft.html");
  assert.equal(draftHtml.status, 301);

  const asset = await request(server, `/_astro/${hashedCss}`);
  assert.equal(asset.status, 200);
  assert.equal(asset.headers["cache-control"], "public, max-age=31536000, immutable");

  const missingAsset = await request(server, "/_astro/not-real.B12345.css");
  assert.equal(missingAsset.status, 404);
  assert.equal(missingAsset.headers["cache-control"], "no-store");

  const traversal = await request(server, "/_astro/../index.html");
  assert.notEqual(traversal.headers["cache-control"], "public, max-age=31536000, immutable");
  assert.ok(traversal.status === 301 || traversal.status === 404);

  const encoded = await request(server, "/_astro/%2e%2e/index.html");
  assert.ok(encoded.status === 301 || encoded.status === 404);
  assert.notEqual(encoded.headers["cache-control"], "public, max-age=31536000, immutable");

  const astroDirListing = await request(server, "/_astro/");
  assert.equal(astroDirListing.status, 404);

  const ops = await request(server, "/ops.html");
  assert.equal(ops.status, 404);

  const backup = await request(server, "/ops.html.bak-useless-20260809141004");
  assert.equal(backup.status, 404);

  const unknown = await request(server, "/not-a-route");
  assert.equal(unknown.status, 404);

  const cancel = await request(server, "/cancel", { Cookie: allow });
  assert.equal(cancel.status, 401);
  assert.equal(cancel.body.includes("googletagmanager"), false);
  assert.match(cancel.headers["content-security-policy"], /connect-src 'none'/);
  assert.match(cancel.headers["x-robots-tag"], /noindex/);
} finally {
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

console.log("PASS canary stub HTTP: consent/GPC/CSP/aliases/allowlist/draft-404 (not CTO handler, not visual proof)");
