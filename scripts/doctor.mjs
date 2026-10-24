#!/usr/bin/env node
/**
 * Portable read-only Astro readiness and project inspector for legends-astro.
 * Inspects Node runtime, vendored docs snapshot, local Astro v7 installation,
 * lockfile consistency (npm, pnpm, yarn, bun), optional @astrojs/check + TypeScript,
 * and Astro v7 structural traps (legacy src/content/config.ts, missing src/pages, SSR routes).
 * Does not import consumer config, read env secrets, install, build, or use the network.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyRoot } from "./verify-sources.mjs";

export const EXPECTED_ASTRO = "7.3.1";
export const LATEST_REVIEWED_ASTRO = "7.4.2";
export const SUPPORTED_ASTRO_MAJOR = 7;
export const EXPECTED_BIN = "bin/astro.mjs";
export const ENGINE_MIN_NODE = "22.12.0";
export const ENGINE_MIN_NODE_7_4 = "22.23.4";
export const TESTED_NODE = "24.15.0";
export const CONFIG_CANDIDATES = Object.freeze([
  "astro.config.mjs",
  "astro.config.js",
  "astro.config.ts",
  "astro.config.mts",
  "astro.config.cjs",
  "astro.config.cts",
]);

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const moduleRoot = path.resolve(scriptDir, "..");

function residual(code, message, extra = {}) {
  return { code, message, ...extra };
}

function isPlainObject(value) {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function specString(value) {
  if (value == null) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed || null;
  }
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

export function parseVersion(raw) {
  const text = String(raw ?? "")
    .trim()
    .replace(/^v/i, "");
  const match = text.match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!match) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    text: `${match[1]}.${match[2]}.${match[3]}`,
  };
}

function cmpVersion(a, b) {
  if (a.major !== b.major) return a.major - b.major;
  if (a.minor !== b.minor) return a.minor - b.minor;
  return a.patch - b.patch;
}

export function versionGte(a, b) {
  return cmpVersion(a, b) >= 0;
}

export function declaredAstroVersion(spec) {
  if (spec == null) return null;
  const raw = String(spec).trim();
  if (!raw) return null;
  const exact = raw.match(/^v?(\d+\.\d+\.\d+)$/);
  if (exact) return { text: exact[1], exact: true, spec: raw };
  const coerced = raw.match(/(\d+\.\d+\.\d+)/);
  return {
    text: coerced ? coerced[1] : raw,
    exact: false,
    spec: raw,
  };
}

export function inspectNode(versionRaw, execPath = "", strictPin = false) {
  const tested = parseVersion(TESTED_NODE);
  const engineMin = parseVersion(ENGINE_MIN_NODE);
  const parsed = parseVersion(versionRaw);
  const report = {
    version: parsed ? `v${parsed.text}` : String(versionRaw || ""),
    execPath: execPath || null,
    enginesRequired: `>=${ENGINE_MIN_NODE}`,
    testedBaseline: TESTED_NODE,
    status: "unknown",
  };
  const residuals = [];
  const warnings = [];
  if (!parsed) {
    residuals.push(
      residual("node_version_unparsed", `cannot parse Node version ${String(versionRaw)}`),
    );
    report.status = "unparsed";
    return { node: report, residuals, warnings };
  }
  report.major = parsed.major;
  report.minor = parsed.minor;
  report.patch = parsed.patch;
  const odd = parsed.major % 2 === 1;
  const meetsEngine = versionGte(parsed, engineMin);
  const isTested =
    parsed.major === tested.major &&
    parsed.minor === tested.minor &&
    parsed.patch === tested.patch;

  if (!meetsEngine) {
    report.status = "engine_unsatisfied";
    residuals.push(
      residual(
        "node_engine_unsatisfied",
        `Node v${parsed.text} is below Astro 7 engines (>=${ENGINE_MIN_NODE})`,
      ),
    );
    return { node: report, residuals, warnings };
  }

  if (odd) {
    report.status = "odd_major_compatible";
    report.guidance = `prefer even LTS Node ${TESTED_NODE} for reproducible release proofs`;
    if (strictPin) {
      report.status = "untested_by_kit";
      residuals.push(
        residual(
          "node_odd_major_untested_by_kit",
          `Node v${parsed.text} is an odd major and strict-pin mode requires tested runtime Node ${TESTED_NODE}.`,
        ),
      );
    } else {
      warnings.push({
        code: "node_odd_major_warning",
        message: `Node v${parsed.text} is an odd major. It satisfies >=${ENGINE_MIN_NODE} and runs Astro v7 CLI builds, but even LTS (Node ${TESTED_NODE}) is recommended for canonical release receipts.`,
      });
    }
    return { node: report, residuals, warnings };
  }

  if (isTested) {
    report.status = "tested";
    return { node: report, residuals, warnings };
  }

  report.status = "engine_compatible";
  report.guidance = `tested baseline is Node ${TESTED_NODE}`;
  warnings.push({
    code: "node_engine_compatible_untested",
    message: `Node v${parsed.text} meets Astro engines (>=${ENGINE_MIN_NODE}); canonical canary baseline is ${TESTED_NODE}.`,
  });
  return { node: report, residuals, warnings };
}

function readJsonFile(filePath) {
  let text;
  try {
    text = fs.readFileSync(filePath, "utf8");
  } catch (error) {
    return { ok: false, code: "unreadable", message: String(error?.message || error) };
  }
  try {
    return { ok: true, value: JSON.parse(text), text };
  } catch (error) {
    return { ok: false, code: "malformed", message: String(error?.message || error) };
  }
}

function depSpec(pkg, name) {
  if (!isPlainObject(pkg)) return null;
  for (const key of ["dependencies", "devDependencies", "optionalDependencies"]) {
    const map = pkg[key];
    if (!isPlainObject(map)) continue;
    const spec = specString(map[name]);
    if (spec) return spec;
  }
  return null;
}

function installedPackage(projectRoot, name) {
  const pkgPath = path.join(projectRoot, "node_modules", ...name.split("/"), "package.json");
  if (!fs.existsSync(pkgPath)) return { present: false, path: pkgPath };
  const parsed = readJsonFile(pkgPath);
  if (!parsed.ok) {
    return { present: true, path: pkgPath, malformed: true, error: parsed.message };
  }
  if (!isPlainObject(parsed.value)) {
    return {
      present: true,
      path: pkgPath,
      malformed: true,
      error: `root must be an object (got ${parsed.value === null ? "null" : Array.isArray(parsed.value) ? "array" : typeof parsed.value})`,
    };
  }
  const version = specString(parsed.value.version);
  return {
    present: true,
    path: pkgPath,
    version,
    bin: parsed.value.bin ?? null,
    engines: parsed.value.engines ?? null,
    name: specString(parsed.value.name) ?? name,
  };
}

function declaredBinRel(bin) {
  if (typeof bin === "string") return specString(bin.replaceAll("\\", "/").replace(/^\.\//, ""));
  if (!isPlainObject(bin)) return null;
  const astro = bin.astro;
  if (typeof astro !== "string") return null;
  return specString(astro.replaceAll("\\", "/").replace(/^\.\//, ""));
}

function resolveExpectedAstroBin(projectRoot) {
  const astroRoot = path.resolve(projectRoot, "node_modules", "astro");
  const selected = path.resolve(astroRoot, EXPECTED_BIN);
  const prefix = astroRoot.endsWith(path.sep) ? astroRoot : astroRoot + path.sep;
  if (selected !== astroRoot && !selected.startsWith(prefix)) {
    return { path: null, exists: false, selected: EXPECTED_BIN };
  }
  let exists = false;
  try {
    exists = fs.existsSync(selected) && fs.statSync(selected).isFile();
  } catch {
    exists = false;
  }
  return { path: selected, exists, selected: EXPECTED_BIN };
}

function detectNonNpmLocks(projectRoot) {
  const found = [];
  const mapping = [
    ["pnpm-lock.yaml", "pnpm"],
    ["yarn.lock", "yarn"],
    ["bun.lock", "bun"],
    ["bun.lockb", "bun"],
  ];
  for (const [name, kind] of mapping) {
    const full = path.join(projectRoot, name);
    if (fs.existsSync(full)) found.push({ name, kind, path: full });
  }
  return found;
}

function parseAlternativeLockAstroVersion(lockItem) {
  try {
    if (lockItem.name === "bun.lockb") return null;
    const text = fs.readFileSync(lockItem.path, "utf8");
    if (lockItem.kind === "pnpm") {
      const m = text.match(/\/astro@(\d+\.\d+\.\d+)/) || text.match(/astro@(\d+\.\d+\.\d+)/);
      return m ? m[1] : null;
    }
    if (lockItem.kind === "yarn") {
      const m = text.match(/"?astro@[^"\n]+"?:\s*\r?\n\s+version[:\s]+"?(\d+\.\d+\.\d+)"?/);
      return m ? m[1] : null;
    }
    if (lockItem.kind === "bun") {
      const m = text.match(/"astro"\s*:\s*\[\s*"astro@(\d+\.\d+\.\d+)"/);
      return m ? m[1] : null;
    }
  } catch {
    return null;
  }
  return null;
}

function lockRootAstro(rootEntry) {
  if (!isPlainObject(rootEntry)) {
    return { spec: null, from: null, conflict: false };
  }
  const dep = isPlainObject(rootEntry.dependencies)
    ? specString(rootEntry.dependencies.astro)
    : null;
  const dev = isPlainObject(rootEntry.devDependencies)
    ? specString(rootEntry.devDependencies.astro)
    : null;
  if (dep && dev && dep !== dev) {
    return { spec: null, from: "both", conflict: true, dep, dev };
  }
  if (dep) return { spec: dep, from: "dependencies", conflict: false };
  if (dev) return { spec: dev, from: "devDependencies", conflict: false };
  return { spec: null, from: null, conflict: false };
}

function isSupportedAstroVersion(verStr, strictPin = false) {
  if (!verStr) return false;
  if (strictPin) return verStr === EXPECTED_ASTRO;
  const p = parseVersion(verStr);
  return Boolean(p && p.major === SUPPORTED_ASTRO_MAJOR);
}

function inspectLockfile(projectRoot, declared, installedVer, strictPin = false) {
  const lockPath = path.join(projectRoot, "package-lock.json");
  const others = detectNonNpmLocks(projectRoot);
  const warnings = [];
  const residuals = [];
  const hasNpmLock = fs.existsSync(lockPath);

  if (!hasNpmLock && others.length) {
    const primary = others[0];
    const altVersion = parseAlternativeLockAstroVersion(primary) || installedVer || declared?.text || null;
    if (strictPin) {
      const kinds = [...new Set(others.map((item) => item.kind))];
      const names = others.map((item) => item.name).join(", ");
      residuals.push(
        residual(
          "lockfile_unsupported",
          `strict-pin mode reads npm package-lock.json only. Found ${names} (${kinds.join("/")}).`,
        ),
      );
      return {
        lockfile: {
          path: primary.path,
          present: true,
          kind: primary.kind,
          astro: altVersion,
          consistent: false,
          unsupported: true,
          found: others.map((item) => item.name),
        },
        residuals,
        warnings,
      };
    }
    warnings.push({
      code: "non_npm_lockfile_detected",
      message: `Using ${primary.name} (${primary.kind}). Do not replace the project's lockfile with npm package-lock.json.`,
    });
    return {
      lockfile: {
        path: primary.path,
        present: true,
        kind: primary.kind,
        astro: altVersion,
        consistent: Boolean(altVersion && isSupportedAstroVersion(altVersion, false)),
        found: others.map((item) => item.name),
      },
      residuals,
      warnings,
    };
  }

  if (others.length && hasNpmLock) {
    warnings.push({
      code: "extra_lockfile",
      message: `additional lockfile(s) present (${others.map((item) => item.name).join(", ")}); consistency check uses npm package-lock.json`,
    });
  }

  if (!hasNpmLock) {
    residuals.push(
      residual("lockfile_missing", "no lockfile (package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock) found"),
    );
    return {
      lockfile: { path: lockPath, present: false, kind: "npm", consistent: false },
      residuals,
      warnings,
    };
  }

  const parsed = readJsonFile(lockPath);
  if (!parsed.ok) {
    residuals.push(
      residual("lockfile_malformed", `package-lock.json is not valid JSON: ${parsed.message}`),
    );
    return {
      lockfile: { path: lockPath, present: true, kind: "npm", consistent: false },
      residuals,
      warnings,
    };
  }
  if (!isPlainObject(parsed.value)) {
    residuals.push(
      residual(
        "lockfile_malformed",
        `package-lock.json root must be an object (got ${parsed.value === null ? "null" : Array.isArray(parsed.value) ? "array" : typeof parsed.value})`,
      ),
    );
    return {
      lockfile: { path: lockPath, present: true, kind: "npm", consistent: false },
      residuals,
      warnings,
    };
  }

  const lock = parsed.value;
  const lockfileVersion = Number(lock.lockfileVersion);
  const packages = isPlainObject(lock.packages) ? lock.packages : null;
  const lockedEntry = packages ? packages["node_modules/astro"] : null;
  const lockedAstro = isPlainObject(lockedEntry)
    ? specString(lockedEntry.version)
    : isPlainObject(lock.dependencies) && isPlainObject(lock.dependencies.astro)
      ? specString(lock.dependencies.astro.version)
      : null;
  const rootAstro = lockRootAstro(packages ? packages[""] : null);
  const lockedVersion = lockedAstro ? lockedAstro.replace(/^v/, "") : null;
  const report = {
    path: lockPath,
    present: true,
    kind: "npm",
    lockfileVersion: Number.isFinite(lockfileVersion) ? lockfileVersion : null,
    astro: lockedVersion,
    rootDeclared: rootAstro.spec,
    rootDeclaredFrom: rootAstro.from,
    consistent: false,
  };

  if (rootAstro.conflict) {
    residuals.push(
      residual(
        "lock_inconsistent",
        `package-lock packages[""] lists astro in both dependencies (${rootAstro.dep}) and devDependencies (${rootAstro.dev})`,
      ),
    );
  }

  if (declared?.spec && rootAstro.spec && rootAstro.spec !== declared.spec) {
    residuals.push(
      residual(
        "lock_inconsistent",
        `package.json astro (${declared.spec}) does not match lock packages[""] ${rootAstro.from} (${rootAstro.spec})`,
      ),
    );
  } else if (declared?.spec && !rootAstro.spec) {
    residuals.push(
      residual(
        "lock_inconsistent",
        `package.json declares astro (${declared.spec}) but lock packages[""] has no dependencies/devDependencies astro entry`,
      ),
    );
  }

  if (!lockedVersion) {
    residuals.push(
      residual("lock_astro_missing", "package-lock.json has no node_modules/astro version"),
    );
    return { lockfile: report, residuals, warnings };
  }
  if (!isSupportedAstroVersion(lockedVersion, strictPin)) {
    residuals.push(
      residual(
        "lock_astro_version_mismatch",
        strictPin
          ? `lock astro@${lockedVersion} is not strict pin ${EXPECTED_ASTRO}`
          : `lock astro@${lockedVersion} is not a supported Astro v${SUPPORTED_ASTRO_MAJOR}.x release`,
      ),
    );
  }
  if (declared?.exact && declared?.text && lockedVersion !== declared.text) {
    residuals.push(
      residual(
        "lock_inconsistent",
        `package.json astro (${declared.spec}) does not match lock node_modules/astro@${lockedVersion}`,
      ),
    );
  }

  report.consistent =
    residuals.filter((item) => item.code.startsWith("lock_")).length === 0 &&
    isSupportedAstroVersion(lockedVersion, strictPin);
  return { lockfile: report, residuals, warnings };
}

function inspectConfig(projectRoot) {
  const found = CONFIG_CANDIDATES.filter((name) =>
    fs.existsSync(path.join(projectRoot, name)),
  );
  if (!found.length) {
    return {
      config: {
        status: "absent",
        path: null,
        candidates: [],
        note: "Astro config is optional. Absence is not a residual. Not imported.",
      },
    };
  }
  return {
    config: {
      status: "unresolved",
      path: found[0],
      candidates: found,
      note: "Path is a hint only. Doctor does not import, evaluate, or trust this file as resolved config.",
    },
  };
}

function inspectStructureAndTraps(projectRoot) {
  const warnings = [];
  const residuals = [];
  const legacyContentConfigs = [
    "src/content/config.ts",
    "src/content/config.js",
    "src/content/config.mjs",
  ].filter((rel) => fs.existsSync(path.join(projectRoot, ...rel.split("/"))));

  const modernContentConfigs = [
    "src/content.config.ts",
    "src/content.config.js",
    "src/content.config.mjs",
  ].filter((rel) => fs.existsSync(path.join(projectRoot, ...rel.split("/"))));

  if (legacyContentConfigs.length > 0) {
    residuals.push(
      residual(
        "legacy_content_config_path",
        `Found legacy Astro v4/v5 content config at ${legacyContentConfigs.join(", ")}. Astro v6/v7 requires src/content.config.ts with glob() loader and astro/zod.`,
      ),
    );
  }

  const pagesDir = path.join(projectRoot, "src", "pages");
  let pagesPresent = false;
  let pageFiles = [];
  let onDemandRoutes = [];
  if (fs.existsSync(pagesDir) && fs.statSync(pagesDir).isDirectory()) {
    pagesPresent = true;
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (/\.(astro|md|mdx|html|js|ts)$/.test(entry.name)) {
          const rel = path.relative(projectRoot, full).replaceAll("\\", "/");
          pageFiles.push(rel);
          try {
            const content = fs.readFileSync(full, "utf8");
            if (/export\s+const\s+prerender\s*=\s*false/.test(content)) {
              onDemandRoutes.push(rel);
            }
          } catch {
            // ignore unreadable
          }
        }
      }
    };
    walk(pagesDir);
  } else {
    warnings.push({
      code: "src_pages_missing",
      message: "src/pages/ directory is absent; add at least one route (e.g. src/pages/index.astro) before running astro build.",
    });
  }

  if (onDemandRoutes.length > 0) {
    warnings.push({
      code: "on_demand_routes_detected",
      message: `Found ${onDemandRoutes.length} route(s) with prerender = false (${onDemandRoutes.join(", ")}). Do not delete them to force a static build; an SSR adapter is required to build them.`,
    });
  }

  return {
    structure: {
      pagesPresent,
      pageCount: pageFiles.length,
      modernContentConfig: modernContentConfigs[0] || null,
      legacyContentConfig: legacyContentConfigs[0] || null,
      onDemandRoutes,
    },
    warnings,
    residuals,
  };
}

function inspectOptionalCheck(projectRoot, pkg) {
  const checkSpec = depSpec(pkg, "@astrojs/check");
  const tsSpec = depSpec(pkg, "typescript");
  const checkInstalled = installedPackage(projectRoot, "@astrojs/check");
  const tsInstalled = installedPackage(projectRoot, "typescript");
  const warnings = [];
  const residuals = [];
  if (checkInstalled.malformed) {
    residuals.push(
      residual(
        "optional_check_malformed",
        `node_modules/@astrojs/check/package.json is malformed: ${checkInstalled.error}`,
      ),
    );
  }
  if (tsInstalled.malformed) {
    residuals.push(
      residual(
        "optional_typescript_malformed",
        `node_modules/typescript/package.json is malformed: ${tsInstalled.error}`,
      ),
    );
  }
  if (checkSpec && !checkInstalled.present) {
    warnings.push({
      code: "optional_check_declared_uninstalled",
      message: `@astrojs/check is declared (${checkSpec}) but not installed`,
    });
  }
  if (tsSpec && !tsInstalled.present) {
    warnings.push({
      code: "optional_typescript_declared_uninstalled",
      message: `typescript is declared (${tsSpec}) but not installed`,
    });
  }
  const usable = Boolean(
    checkInstalled.present &&
      !checkInstalled.malformed &&
      tsInstalled.present &&
      !tsInstalled.malformed,
  );
  if (checkInstalled.present && !tsInstalled.present) {
    warnings.push({
      code: "optional_check_without_typescript",
      message: "@astrojs/check is installed but typescript is not; astro check is not usable",
    });
  }
  return {
    check: {
      optional: true,
      packageDeclared: Boolean(checkSpec),
      packageDeclaredSpec: checkSpec ? String(checkSpec) : null,
      packageInstalled: Boolean(checkInstalled.present && !checkInstalled.malformed),
      packageVersion: checkInstalled.version ?? null,
      typescriptDeclared: Boolean(tsSpec),
      typescriptDeclaredSpec: tsSpec ? String(tsSpec) : null,
      typescriptInstalled: Boolean(tsInstalled.present && !tsInstalled.malformed),
      typescriptVersion: tsInstalled.version ?? null,
      usable,
      note: "astro check is optional. Absence is not a residual. Doctor does not run check or install.",
    },
    residuals,
    warnings,
  };
}

export function inspectModuleReadiness(runtime = {}) {
  const nodeInspect = inspectNode(
    runtime.nodeVersion ?? process.versions.node,
    runtime.execPath ?? process.execPath,
    false,
  );
  let snapshot = null;
  const residuals = [...nodeInspect.residuals];
  const warnings = [...nodeInspect.warnings];
  try {
    snapshot = verifyRoot(path.join(moduleRoot, "vendor", "astro-docs"));
  } catch (err) {
    residuals.push(
      residual("vendor_snapshot_invalid", String(err?.message || err)),
    );
  }
  const canaryDir = path.join(moduleRoot, "canary");
  const canaryReport = fs.existsSync(canaryDir)
    ? inspectProject(canaryDir, { ...runtime, strictPin: false })
    : null;
  if (canaryReport && !canaryReport.ok) {
    warnings.push({
      code: "canary_not_ready",
      message: `Canary has residuals: ${canaryReport.residuals.map((r) => r.code).join(", ")}`,
    });
  }
  return {
    ok: residuals.length === 0,
    mode: "module",
    module: "legends-astro",
    moduleRoot,
    expectedAstro: EXPECTED_ASTRO,
    latestReviewedAstro: LATEST_REVIEWED_ASTRO,
    node: nodeInspect.node,
    snapshot,
    canaryOk: canaryReport ? canaryReport.ok : false,
    warnings,
    residuals,
  };
}

export function inspectProject(projectRoot, runtime = {}) {
  try {
    return inspectProjectUnchecked(projectRoot, runtime);
  } catch (error) {
    return {
      ok: false,
      project: projectRoot ? path.resolve(projectRoot) : null,
      expectedAstro: EXPECTED_ASTRO,
      node: { version: null, status: "unknown" },
      astro: {
        expected: EXPECTED_ASTRO,
        declared: null,
        declaredExact: null,
        declaredSpec: null,
        installed: null,
        lock: null,
        bin: { declared: null, exists: false, path: null, selected: EXPECTED_BIN },
      },
      lockfile: { present: false, kind: null, consistent: false },
      config: { status: "absent", path: null, candidates: [], note: "uninspected after internal error" },
      check: { optional: true, usable: false },
      warnings: [],
      residuals: [
        residual("doctor_internal_error", String(error?.message || error)),
      ],
    };
  }
}

function inspectProjectUnchecked(projectRoot, runtime = {}) {
  const strictPin = Boolean(runtime.strictPin);
  const residuals = [];
  const warnings = [];
  const resolved = projectRoot ? path.resolve(projectRoot) : "";
  const nodeInspect = inspectNode(
    runtime.nodeVersion ?? process.versions.node,
    runtime.execPath ?? process.execPath,
    strictPin,
  );
  residuals.push(...nodeInspect.residuals);
  warnings.push(...nodeInspect.warnings);

  const report = {
    ok: false,
    project: resolved || null,
    expectedAstro: EXPECTED_ASTRO,
    latestReviewedAstro: LATEST_REVIEWED_ASTRO,
    node: nodeInspect.node,
    astro: {
      expected: EXPECTED_ASTRO,
      declared: null,
      declaredExact: null,
      declaredSpec: null,
      installed: null,
      lock: null,
      bin: { declared: null, exists: false, path: null, selected: EXPECTED_BIN },
    },
    lockfile: { present: false, kind: null, consistent: false },
    config: {
      status: "absent",
      path: null,
      candidates: [],
      note: "not inspected yet",
    },
    structure: null,
    check: { optional: true, usable: false },
    warnings: [],
    residuals: [],
  };

  if (!projectRoot) {
    residuals.push(residual("project_missing", "missing --project <root>"));
    report.residuals = residuals;
    report.warnings = warnings;
    report.ok = false;
    return report;
  }

  let stat;
  try {
    stat = fs.statSync(resolved);
  } catch {
    residuals.push(residual("project_missing", `project path does not exist: ${resolved}`));
    report.residuals = residuals;
    report.warnings = warnings;
    return report;
  }
  if (!stat.isDirectory()) {
    residuals.push(residual("project_not_directory", `project path is not a directory: ${resolved}`));
    report.residuals = residuals;
    report.warnings = warnings;
    return report;
  }

  const pkgPath = path.join(resolved, "package.json");
  if (!fs.existsSync(pkgPath)) {
    residuals.push(residual("package_json_missing", `package.json is missing under ${resolved}`));
    report.residuals = residuals;
    report.warnings = warnings;
    Object.assign(report, inspectConfig(resolved));
    return report;
  }
  const pkgParsed = readJsonFile(pkgPath);
  if (!pkgParsed.ok) {
    residuals.push(
      residual("package_json_malformed", `package.json is not valid JSON: ${pkgParsed.message}`),
    );
    report.residuals = residuals;
    report.warnings = warnings;
    return report;
  }
  const pkg = pkgParsed.value;
  if (pkg == null || typeof pkg !== "object" || Array.isArray(pkg)) {
    residuals.push(residual("package_json_malformed", "package.json root must be an object"));
    report.residuals = residuals;
    report.warnings = warnings;
    return report;
  }

  const spec = depSpec(pkg, "astro");
  const declared = declaredAstroVersion(spec);
  report.astro.declaredSpec = spec ? String(spec) : null;
  report.astro.declared = declared?.text ?? null;
  report.astro.declaredExact = declared?.exact ?? null;
  if (!spec) {
    residuals.push(
      residual("astro_declared_missing", "package.json does not declare an astro dependency"),
    );
  } else if (!declared?.text || !parseVersion(declared.text)) {
    residuals.push(
      residual("astro_declared_unparsed", `cannot parse astro spec ${spec}`),
    );
  } else {
    if (!declared.exact) {
      warnings.push({
        code: "astro_declared_not_exact",
        message: `package.json astro spec ${spec} is a range rather than an exact version pin`,
      });
    }
    if (!isSupportedAstroVersion(declared.text, strictPin)) {
      residuals.push(
        residual(
          "astro_declared_version_mismatch",
          strictPin
            ? `package.json astro is ${spec}; strict pin is ${EXPECTED_ASTRO}`
            : `package.json astro (${spec}) is outside supported Astro v${SUPPORTED_ASTRO_MAJOR}.x`,
        ),
      );
    } else if (declared.text !== EXPECTED_ASTRO && declared.text !== LATEST_REVIEWED_ASTRO) {
      warnings.push({
        code: "astro_v7_untested_patch",
        message: `package.json declares astro ${declared.text} (compatible Astro v7; reviewed pins are ${EXPECTED_ASTRO} and ${LATEST_REVIEWED_ASTRO}).`,
      });
    }
  }

  const installed = installedPackage(resolved, "astro");
  if (installed.malformed) {
    residuals.push(
      residual("astro_package_malformed", `node_modules/astro/package.json is malformed: ${installed.error}`),
    );
  } else if (!installed.present) {
    residuals.push(
      residual("astro_not_installed", "local node_modules/astro is missing; install is a separate setup step"),
    );
  } else {
    report.astro.installed = installed.version;
    if (!isSupportedAstroVersion(installed.version, strictPin)) {
      residuals.push(
        residual(
          "astro_installed_version_mismatch",
          strictPin
            ? `installed astro@${installed.version} is not strict pin ${EXPECTED_ASTRO}`
            : `installed astro@${installed.version} is outside supported Astro v${SUPPORTED_ASTRO_MAJOR}.x`,
        ),
      );
    }
    const declaredRel = declaredBinRel(installed.bin);
    const selected = resolveExpectedAstroBin(resolved);
    report.astro.bin.declared = declaredRel;
    report.astro.bin.selected = selected.selected;
    report.astro.bin.path = selected.path;
    report.astro.bin.exists = selected.exists;
    if (installed.bin != null && declaredRel !== EXPECTED_BIN) {
      residuals.push(
        residual(
          "astro_bin_nonstandard",
          `local astro package bin metadata is ${JSON.stringify(installed.bin)}; module uses node_modules/astro/${EXPECTED_BIN} only and ignores package-controlled paths`,
        ),
      );
    }
    if (!selected.exists) {
      residuals.push(
        residual(
          "astro_bin_missing",
          `local Astro bin not found at node_modules/astro/${EXPECTED_BIN}`,
        ),
      );
    }
  }

  const lock = inspectLockfile(resolved, declared, installed.version ?? null, strictPin);
  residuals.push(...lock.residuals);
  warnings.push(...lock.warnings);
  report.lockfile = lock.lockfile;
  report.astro.lock = lock.lockfile.astro ?? null;

  Object.assign(report, inspectConfig(resolved));

  const structure = inspectStructureAndTraps(resolved);
  residuals.push(...structure.residuals);
  warnings.push(...structure.warnings);
  report.structure = structure.structure;

  const check = inspectOptionalCheck(resolved, pkg);
  residuals.push(...check.residuals);
  warnings.push(...check.warnings);
  report.check = check.check;

  report.warnings = warnings;
  report.residuals = residuals;
  report.ok = residuals.length === 0;
  return report;
}

export function parseArgs(argv) {
  const residuals = [];
  let project = null;
  let json = false;
  let strictPin = false;
  let moduleMode = false;
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--json") {
      json = true;
      continue;
    }
    if (arg === "--strict-pin") {
      strictPin = true;
      continue;
    }
    if (arg === "--module") {
      moduleMode = true;
      continue;
    }
    if (arg === "--project") {
      project = argv[i + 1];
      i += 1;
      if (project == null || project.startsWith("--")) {
        residuals.push(residual("project_missing", "missing --project <root>"));
        project = null;
      }
      continue;
    }
    if (arg.startsWith("--project=")) {
      project = arg.slice("--project=".length);
      if (!project) residuals.push(residual("project_missing", "missing --project <root>"));
      continue;
    }
    residuals.push(residual("unknown_flag", `unknown argument: ${arg}`));
  }
  return { project, json, strictPin, moduleMode, residuals };
}

export function run(argv = process.argv, runtime = {}) {
  const args = parseArgs(argv);
  if (!args.project && args.residuals.length === 0 && args.moduleMode) {
    const report = inspectModuleReadiness(runtime);
    return { report, json: args.json };
  }
  const report = inspectProject(args.project, { ...runtime, strictPin: args.strictPin || runtime.strictPin });
  if (args.residuals.length) {
    report.residuals = [...args.residuals, ...report.residuals];
    report.ok = false;
  }
  return { report, json: args.json };
}

function isMain(metaUrl) {
  const self = path.normalize(fileURLToPath(metaUrl));
  const invoked = process.argv[1] ? path.normalize(path.resolve(process.argv[1])) : "";
  return Boolean(invoked) && self === invoked;
}

if (isMain(import.meta.url)) {
  try {
    const { report, json } = run(process.argv);
    if (json) {
      process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    } else if (report.mode === "module") {
      const lines = [
        report.ok ? "ok: legends-astro module doctor" : "residual: legends-astro module doctor",
        `node ${report.node.version} ${report.node.status}`,
        `snapshot commit=${report.snapshot?.commit ?? "error"} files=${report.snapshot?.files ?? 0}`,
        `canary ok=${report.canaryOk}`,
      ];
      for (const warning of report.warnings) lines.push(`warning ${warning.code}: ${warning.message}`);
      for (const item of report.residuals) lines.push(`residual ${item.code}: ${item.message}`);
      process.stdout.write(`${lines.join("\n")}\n`);
    } else {
      const lines = [
        report.ok ? "ok: astro doctor" : "residual: astro doctor",
        `project ${report.project ?? "(none)"}`,
        `node ${report.node.version} ${report.node.status}`,
        `astro declared=${report.astro.declared ?? "none"} installed=${report.astro.installed ?? "none"} lock=${report.astro.lock ?? "none"}`,
        `config ${report.config.status}${report.config.path ? ` ${report.config.path}` : ""}`,
        `check usable=${report.check.usable} (optional)`,
      ];
      for (const warning of report.warnings) lines.push(`warning ${warning.code}: ${warning.message}`);
      for (const item of report.residuals) lines.push(`residual ${item.code}: ${item.message}`);
      process.stdout.write(`${lines.join("\n")}\n`);
    }
    process.exitCode = report.ok ? 0 : 1;
  } catch (error) {
    process.stdout.write(
      `${JSON.stringify({
        ok: false,
        residuals: [{ code: "doctor_internal_error", message: String(error?.message || error) }],
      })}\n`,
    );
    process.exitCode = 1;
  }
}
