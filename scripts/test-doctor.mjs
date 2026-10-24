import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  EXPECTED_ASTRO,
  inspectNode,
  inspectProject,
  LATEST_REVIEWED_ASTRO,
  parseArgs,
  TESTED_NODE,
} from "./doctor.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const moduleRoot = path.join(here, "..");
const doctorPath = path.join(here, "doctor.mjs");
const nodeExe = process.execPath;

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function writeRaw(filePath, text) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, text, "utf8");
}

function seedAstroInstall(root, { version = "7.3.1", bin = { astro: "./bin/astro.mjs" } } = {}) {
  writeJson(path.join(root, "node_modules", "astro", "package.json"), {
    name: "astro",
    version,
    bin,
  });
  fs.mkdirSync(path.join(root, "node_modules", "astro", "bin"), { recursive: true });
  fs.writeFileSync(
    path.join(root, "node_modules", "astro", "bin", "astro.mjs"),
    "export {}\n",
  );
}

function seedNpmLock(root, { rootAstro = "7.3.1", locked = "7.3.1", rootField = "dependencies" } = {}) {
  writeJson(path.join(root, "package-lock.json"), {
    lockfileVersion: 3,
    packages: {
      "": { [rootField]: { astro: rootAstro } },
      "node_modules/astro": { version: locked },
    },
  });
}

function tmpProject(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `la-doctor-${name}-`));
}

function cleanup(root) {
  fs.rmSync(root, { recursive: true, force: true });
}

function codes(report) {
  return report.residuals.map((item) => item.code);
}

// 1. Node inspection: default vs strictPin
{
  const oddDefault = inspectNode("25.9.0", "", false);
  assert.equal(oddDefault.node.status, "odd_major_compatible");
  assert.equal(oddDefault.residuals.length, 0);
  assert.ok(oddDefault.warnings.some((w) => w.code === "node_odd_major_warning"));

  const oddStrict = inspectNode("25.9.0", "", true);
  assert.equal(oddStrict.node.status, "untested_by_kit");
  assert.ok(codes({ residuals: oddStrict.residuals }).includes("node_odd_major_untested_by_kit"));
  assert.match(oddStrict.residuals[0].message, /tested runtime Node 24\.15\.0/);

  const old = inspectNode("20.19.1", "", false);
  assert.equal(old.node.status, "engine_unsatisfied");
  assert.ok(codes({ residuals: old.residuals }).includes("node_engine_unsatisfied"));

  const untested = inspectNode("22.22.0", "", false);
  assert.equal(untested.node.status, "engine_compatible");
  assert.equal(untested.residuals.length, 0);
  assert.equal(untested.warnings[0].code, "node_engine_compatible_untested");

  const tested = inspectNode(TESTED_NODE, nodeExe, true);
  assert.equal(tested.node.status, "tested");
  assert.equal(tested.residuals.length, 0);
  console.log("ok: node inspect distinguishes tested / odd_major_compatible / strictPin / engine-unsatisfied");
}

// 2. Argv parsing
{
  const args = parseArgs(["node", "doctor.mjs", "--project", "C:\\x", "--json", "--strict-pin"]);
  assert.equal(args.project, "C:\\x");
  assert.equal(args.json, true);
  assert.equal(args.strictPin, true);
  const missing = parseArgs(["node", "doctor.mjs", "--json"]);
  assert.equal(missing.project, null);
  const unknown = parseArgs(["node", "doctor.mjs", "--project", "p", "--explode"]);
  assert.ok(unknown.residuals.some((item) => item.code === "unknown_flag"));
  console.log("ok: argv parsing");
}

// 3. Missing & empty project
{
  const report = inspectProject(path.join(os.tmpdir(), "la-doctor-no-such-dir"));
  assert.equal(report.ok, false);
  assert.ok(codes(report).includes("project_missing"));

  const root = tmpProject("empty");
  try {
    const r2 = inspectProject(root);
    assert.equal(r2.ok, false);
    assert.ok(codes(r2).includes("package_json_missing"));
    assert.equal(r2.config.status, "absent");
  } finally {
    cleanup(root);
  }
  console.log("ok: missing and empty project residuals");
}

// 4. Astro v7 compatibility in default mode vs strictPin mode
{
  const root = tmpProject("v7-latest");
  try {
    writeJson(path.join(root, "package.json"), {
      name: "v7-latest",
      private: true,
      dependencies: { astro: LATEST_REVIEWED_ASTRO },
    });
    seedNpmLock(root, { rootAstro: LATEST_REVIEWED_ASTRO, locked: LATEST_REVIEWED_ASTRO });
    seedAstroInstall(root, { version: LATEST_REVIEWED_ASTRO });

    const defaultReport = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(defaultReport.ok, true, JSON.stringify(defaultReport.residuals));

    const strictReport = inspectProject(root, { nodeVersion: TESTED_NODE, strictPin: true });
    assert.equal(strictReport.ok, false);
    assert.ok(codes(strictReport).includes("astro_declared_version_mismatch"));
    assert.ok(codes(strictReport).includes("astro_installed_version_mismatch"));
    assert.ok(codes(strictReport).includes("lock_astro_version_mismatch"));
  } finally {
    cleanup(root);
  }
  console.log("ok: Astro 7.4.2 passes default v7 mode and fails strict-pin 7.3.1 mode");
}

// 5. Legacy Astro v6 fails both modes
{
  const root = tmpProject("v6-old");
  try {
    writeJson(path.join(root, "package.json"), {
      name: "v6-old",
      private: true,
      dependencies: { astro: "6.1.0" },
    });
    seedNpmLock(root, { rootAstro: "6.1.0", locked: "6.1.0" });
    seedAstroInstall(root, { version: "6.1.0" });
    const report = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(report.ok, false);
    assert.ok(codes(report).includes("astro_declared_version_mismatch"));
    assert.ok(codes(report).includes("astro_installed_version_mismatch"));
  } finally {
    cleanup(root);
  }
  console.log("ok: Astro 6.x is rejected with version mismatch residual");
}

// 6. Astro v7 structural traps (legacy src/content/config.ts and prerender = false)
{
  const root = tmpProject("traps");
  try {
    writeJson(path.join(root, "package.json"), {
      name: "traps",
      private: true,
      dependencies: { astro: EXPECTED_ASTRO },
    });
    seedNpmLock(root);
    seedAstroInstall(root);
    writeRaw(path.join(root, "src", "content", "config.ts"), "export const collections = {};\n");
    writeRaw(
      path.join(root, "src", "pages", "ssr.astro"),
      "---\nexport const prerender = false;\n---\n<h1>SSR</h1>\n",
    );
    const report = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(report.ok, false);
    assert.ok(codes(report).includes("legacy_content_config_path"));
    assert.ok(report.warnings.some((w) => w.code === "on_demand_routes_detected"));
  } finally {
    cleanup(root);
  }
  console.log("ok: detects legacy src/content/config.ts residual and on-demand prerender=false warning");
}

// 7. Lockfile null/array/string and bin traversal
for (const raw of ["null", "[]", '"nope"']) {
  const root = tmpProject(`lock-shape-${raw.replace(/[^a-z]/g, "")}`);
  try {
    writeJson(path.join(root, "package.json"), {
      name: "lock-shape",
      private: true,
      dependencies: { astro: EXPECTED_ASTRO },
    });
    writeRaw(path.join(root, "package-lock.json"), `${raw}\n`);
    seedAstroInstall(root);
    const report = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(report.ok, false);
    assert.ok(codes(report).includes("lockfile_malformed"));
  } finally {
    cleanup(root);
  }
}

{
  const root = tmpProject("bin-traversal");
  try {
    writeJson(path.join(root, "package.json"), {
      name: "bin-traversal",
      private: true,
      dependencies: { astro: EXPECTED_ASTRO },
    });
    seedNpmLock(root);
    seedAstroInstall(root, { bin: { astro: "../../outside.mjs" } });
    const report = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(report.ok, false);
    assert.ok(codes(report).includes("astro_bin_nonstandard"));
    assert.equal(report.astro.bin.selected, "bin/astro.mjs");
    assert.equal(String(report.astro.bin.path).includes("outside.mjs"), false);
  } finally {
    cleanup(root);
  }
  console.log("ok: malformed lockfile shapes and nonstandard bin traversal rejected safely");
}

// 8. pnpm / yarn lockfiles: supported in default mode, flagged in strictPin mode
{
  const root = tmpProject("pnpm-only");
  try {
    writeJson(path.join(root, "package.json"), {
      name: "pnpm-only",
      private: true,
      dependencies: { astro: EXPECTED_ASTRO },
    });
    writeRaw(path.join(root, "pnpm-lock.yaml"), "lockfileVersion: '9.0'\npackages:\n  /astro@7.3.1:\n    resolution: {}\n");
    seedAstroInstall(root);

    const defReport = inspectProject(root, { nodeVersion: TESTED_NODE });
    assert.equal(defReport.ok, true, JSON.stringify(defReport.residuals));
    assert.equal(defReport.lockfile.kind, "pnpm");
    assert.ok(defReport.warnings.some((w) => w.code === "non_npm_lockfile_detected"));

    const strictReport = inspectProject(root, { nodeVersion: TESTED_NODE, strictPin: true });
    assert.equal(strictReport.ok, false);
    assert.ok(codes(strictReport).includes("lockfile_unsupported"));
  } finally {
    cleanup(root);
  }
  console.log("ok: pnpm lockfile accepted with warning in default mode and flagged in strictPin mode");
}

function runDoctor(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(nodeExe, [doctorPath, ...args], {
      cwd: moduleRoot,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("exit", (code) => resolve({ code, stdout, stderr }));
  });
}

{
  const missing = await runDoctor(["--json"]);
  assert.notEqual(missing.code, 0);
  const parsed = JSON.parse(missing.stdout);
  assert.equal(parsed.ok, false);
  assert.ok(parsed.residuals.some((item) => item.code === "project_missing"));
  console.log("ok: CLI missing --project is nonzero JSON residual");
}

{
  const root = tmpProject("cli-malformed");
  try {
    fs.writeFileSync(path.join(root, "package.json"), "null", "utf8");
    const result = await runDoctor(["--project", root, "--json"]);
    assert.notEqual(result.code, 0);
    const parsed = JSON.parse(result.stdout);
    assert.ok(parsed.residuals.some((item) => item.code === "package_json_malformed"));
  } finally {
    cleanup(root);
  }
  console.log("ok: CLI malformed package.json nonzero");
}

{
  const modResult = await runDoctor(["--module", "--json"]);
  assert.equal(modResult.code, 0, modResult.stdout);
  const parsed = JSON.parse(modResult.stdout);
  assert.equal(parsed.ok, true);
  assert.equal(parsed.mode, "module");
  assert.equal(parsed.snapshot.files, 27);
  console.log("ok: CLI --module readiness check passes");
}

console.log("\nALL PASS legends-astro doctor test suite");
