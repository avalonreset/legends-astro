#!/usr/bin/env node
/**
 * Read-only offline verifier for vendor/astro-docs/manifest.json.
 * No network. No npm dependencies. Node built-ins only.
 *
 *   node ./scripts/verify-sources.mjs
 *   node ./scripts/verify-sources.mjs --root vendor/astro-docs --skip-self-test
 */
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import {
  dirname,
  isAbsolute,
  join,
  normalize,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const moduleRoot = resolve(scriptDir, "..");
const defaultRoot = join(moduleRoot, "vendor", "astro-docs");
const SHA_RE = /^[a-f0-9]{64}$/;
const commitRe = /^[a-f0-9]{40}$/;
const SUPPORTED_SCHEMAS = new Set([
  "legends-astro-vendor-docs/v2",
  "legends-astro-kit-vendor-docs/v1",
]);

class VerifyError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function parseArgs(argv) {
  const out = { root: defaultRoot, selfTest: true, json: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--root") {
      const value = argv[i + 1];
      if (!value) throw new VerifyError("args", "--root requires a path");
      out.root = resolve(value);
      i += 1;
    } else if (a === "--skip-self-test") {
      out.selfTest = false;
    } else if (a === "--self-test-only") {
      out.selfTestOnly = true;
    } else if (a === "--json") {
      out.json = true;
    } else if (a === "--help" || a === "-h") {
      out.help = true;
    } else {
      throw new VerifyError("args", `unknown argument: ${a}`);
    }
  }
  return out;
}

function posixLocalPath(localPath) {
  return String(localPath).replaceAll("\\", "/");
}

function assertSafeLocalPath(root, localPath) {
  if (typeof localPath !== "string" || localPath.length === 0) {
    throw new VerifyError("escape", "localPath must be a non-empty string");
  }
  if (localPath.includes("\0")) {
    throw new VerifyError("escape", "localPath contains a NUL byte");
  }
  const posix = posixLocalPath(localPath);
  if (isAbsolute(posix) || isAbsolute(localPath)) {
    throw new VerifyError("escape", `absolute localPath rejected: ${localPath}`);
  }
  if (/^[a-zA-Z]:/.test(posix) || posix.startsWith("//") || posix.startsWith("\\\\")) {
    throw new VerifyError("escape", `drive or UNC localPath rejected: ${localPath}`);
  }
  if (posix.startsWith("/")) {
    throw new VerifyError("escape", `root-absolute localPath rejected: ${localPath}`);
  }
  const parts = posix.split("/");
  if (parts.some((p) => p === "" || p === "." || p === "..")) {
    throw new VerifyError(
      "escape",
      `localPath must be canonical without empty, '.', or '..' segments: ${localPath}`,
    );
  }
  const rootResolved = resolve(root);
  const fileResolved = resolve(rootResolved, posix.split("/").join(sep));
  const rel = relative(rootResolved, fileResolved);
  if (rel.startsWith("..") || isAbsolute(rel)) {
    throw new VerifyError("escape", `localPath escapes snapshot root: ${localPath}`);
  }
  return fileResolved;
}

function sha256File(absPath) {
  return createHash("sha256").update(readFileSync(absPath)).digest("hex");
}

function loadManifest(root) {
  const manifestPath = join(root, "manifest.json");
  if (!existsSync(manifestPath)) {
    throw new VerifyError("missing", `manifest.json missing at ${manifestPath}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (err) {
    throw new VerifyError("manifest", `manifest.json is not valid JSON: ${err.message}`);
  }
  if (!SUPPORTED_SCHEMAS.has(parsed.schema)) {
    throw new VerifyError("manifest", `unexpected schema: ${parsed.schema}`);
  }
  if (!commitRe.test(String(parsed.commit || ""))) {
    throw new VerifyError("manifest", `unexpected commit: ${parsed.commit}`);
  }
  if (!Array.isArray(parsed.files) || parsed.files.length === 0) {
    throw new VerifyError("manifest", "manifest.files must be a non-empty array");
  }
  return parsed;
}

export function verifyRoot(root) {
  const manifest = loadManifest(root);
  const seen = new Set();
  const errors = [];

  for (const entry of manifest.files) {
    try {
      if (!entry || typeof entry !== "object") {
        throw new VerifyError("manifest", "file entry is not an object");
      }
      if (!SHA_RE.test(entry.sha256 || "")) {
        throw new VerifyError("manifest", `invalid sha256 for ${entry.id || entry.localPath}`);
      }
      const abs = assertSafeLocalPath(root, entry.localPath);
      const key = posixLocalPath(entry.localPath);
      if (seen.has(key)) {
        throw new VerifyError("manifest", `duplicate localPath: ${key}`);
      }
      seen.add(key);
      if (!existsSync(abs)) {
        throw new VerifyError("missing", `missing file: ${key}`);
      }
      const actual = sha256File(abs);
      if (actual !== entry.sha256) {
        throw new VerifyError(
          "tamper",
          `hash mismatch for ${key}: expected ${entry.sha256} actual ${actual}`,
        );
      }
      if (typeof entry.bytes === "number") {
        const size = readFileSync(abs).length;
        if (size !== entry.bytes) {
          throw new VerifyError(
            "tamper",
            `byte-length mismatch for ${key}: expected ${entry.bytes} actual ${size}`,
          );
        }
      }
    } catch (err) {
      if (err instanceof VerifyError) errors.push(err);
      else errors.push(new VerifyError("unknown", err.message));
    }
  }

  if (errors.length > 0) {
    const first = errors[0];
    first.errors = errors;
    throw first;
  }

  return {
    ok: true,
    root,
    schema: manifest.schema,
    commit: manifest.commit,
    commitDate: manifest.commitDate || null,
    files: manifest.files.length,
  };
}

function expectFail(fn, code) {
  try {
    fn();
  } catch (err) {
    if (err instanceof VerifyError && err.code === code) return err.message;
    throw new VerifyError(
      "self-test",
      `expected ${code} but got ${err.code || err.name}: ${err.message}`,
    );
  }
  throw new VerifyError("self-test", `expected failure code ${code} but verification passed`);
}

function runSelfTest(sourceRoot) {
  const scratch = mkdtempSync(join(tmpdir(), "legends-astro-docs-"));
  const copyRoot = join(scratch, "astro-docs");
  const results = [];
  try {
    cpSync(sourceRoot, copyRoot, { recursive: true });
    const clean = verifyRoot(copyRoot);
    results.push({ name: "copy-passes", ok: true, files: clean.files });

    const licensePath = join(copyRoot, "LICENSE");
    const licenseOrig = readFileSync(licensePath);
    writeFileSync(licensePath, Buffer.concat([licenseOrig, Buffer.from("tampered")]));
    const tamperMsg = expectFail(() => verifyRoot(copyRoot), "tamper");
    writeFileSync(licensePath, licenseOrig);
    results.push({ name: "tampered-file", ok: true, detail: tamperMsg });

    const missingRel = join("src", "content", "docs", "en", "basics", "astro-pages.mdx");
    const missingPath = join(copyRoot, missingRel);
    const missingOrig = readFileSync(missingPath);
    rmSync(missingPath);
    const missingMsg = expectFail(() => verifyRoot(copyRoot), "missing");
    writeFileSync(missingPath, missingOrig);
    results.push({ name: "missing-file", ok: true, detail: missingMsg });

    const manifestPath = join(copyRoot, "manifest.json");
    const manifestOrig = readFileSync(manifestPath);
    const escaped = JSON.parse(manifestOrig.toString("utf8"));
    escaped.files[0].localPath = "../../README.md";
    writeFileSync(manifestPath, JSON.stringify(escaped));
    const escapeMsg = expectFail(() => verifyRoot(copyRoot), "escape");
    writeFileSync(manifestPath, manifestOrig);
    results.push({ name: "path-escape", ok: true, detail: escapeMsg });
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
  return { ok: true, scratchRemoved: true, results };
}

function printHelp() {
  process.stdout.write(`Usage: node ./scripts/verify-sources.mjs [--root <dir>] [--skip-self-test] [--json]

Read-only offline check of vendor/astro-docs/manifest.json hashes.
Rejects path escapes. Self-test tampers/missing/escape copies live in os.tmpdir(), not vendor/.
`);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  const result = args.selfTestOnly ? null : verifyRoot(args.root);
  const self = args.selfTest ? runSelfTest(args.root) : null;
  if (args.json) {
    process.stdout.write(
      `${JSON.stringify({ ok: true, snapshot: result, selfTest: self }, null, 2)}\n`,
    );
    return;
  }
  if (result) {
    process.stdout.write(
      `PASS vendor snapshot ${result.commit} files=${result.files} root=${result.root}\n`,
    );
  }
  if (self) {
    for (const row of self.results) {
      process.stdout.write(`PASS self-test ${row.name}${row.detail ? ` (${row.detail})` : ""}\n`);
    }
  }
}

function isMain(metaUrl) {
  const self = normalize(fileURLToPath(metaUrl));
  const invoked = process.argv[1] ? normalize(resolve(process.argv[1])) : "";
  return Boolean(invoked) && self === invoked;
}

if (isMain(import.meta.url)) {
  try {
    main();
  } catch (err) {
    const code = err instanceof VerifyError ? err.code : "unknown";
    process.stderr.write(`FAIL ${code}: ${err.message}\n`);
    if (err.errors && err.errors.length > 1) {
      for (const extra of err.errors.slice(1)) {
        process.stderr.write(`FAIL ${extra.code}: ${extra.message}\n`);
      }
    }
    process.exitCode = 1;
  }
}
