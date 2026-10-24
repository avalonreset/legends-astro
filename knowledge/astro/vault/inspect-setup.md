# Inspect and setup

Cross-read: [index.md](index.md), [diagnose-and-upgrade.md](diagnose-and-upgrade.md), [latest-docs-and-ecosystem.md](latest-docs-and-ecosystem.md).
Vendored official docs: `vendor/astro-docs/src/content/docs/en/install-and-setup.mdx`, `vendor/astro-docs/src/content/docs/en/reference/cli-reference.mdx`.

## 1. Run `doctor` first

```sh
python -m legends_astro doctor --project <consumer-root>
# Or directly via Node:
node <legends-astro-root>/scripts/doctor.mjs --project <consumer-root> --json
```

`doctor` is read-only. It never imports `astro.config.*`, loads `.env` secrets, installs packages, or runs a build. Add `--live` to query the npm registry and `withastro/docs` GitHub repository for latest upstream versions.

| Field | Meaning |
|---|---|
| `node.status` | `tested` (`v24.15.0`), `engine_compatible` (even LTS meeting `>=22.12.0`), `odd_major_compatible` (odd major such as `v25.x` meeting engine floor with warning), or `engine_unsatisfied` |
| `astro.declared` / `installed` / `lock` | Version pin and consistency across `package.json`, `node_modules/astro`, and lockfile |
| `lockfile.kind` | Supports `npm` (`package-lock.json`), `pnpm` (`pnpm-lock.yaml`), `yarn` (`yarn.lock`), and `bun` (`bun.lock` / `bun.lockb`). Never replace a user's lockfile to satisfy a tool |
| `structure` | Detects `src/pages/` count, Astro v6/v7 `src/content.config.ts` vs legacy `src/content/config.ts`, and routes declaring `prerender = false` |
| `check.usable` | Reports whether optional `@astrojs/check` and `typescript` are both installed |

## 2. Reviewed Astro and Node versions (2026-10-24)

| Item | Current Latest | Canary Baseline | Notes |
|---|---|---|---|
| `astro` | `7.4.2` | `7.3.1` | Always pin exact version in `package.json` (`--save-exact`) for repeatable builds |
| Node engines | `>=22.23.4` (`7.4.2`) / `>=22.12.0` (`7.3.1`) | `v24.15.0` | Even LTS (`22.x`, `24.x`) preferred for release receipts; Node `25.x` runs CLI builds with a warning |
| `create-astro` | `5.3.1` | `5.2.4` | Use `--yes --no-git --no-ai --template minimal` when scaffolding |
| `@astrojs/check` | `0.9.12` | `0.9.10` | Optional; requires `typescript` (`^5 \|\| ^6`) |

## 3. Avoid the Windows PATH collision (`Astronomer.Astro`)

On Windows, `winget` distributes `Astronomer.Astro` ("Astro CLI" for Apache Airflow), which places an `astro` executable on `PATH`. Official Astro documentation also forbids `npm install -g astro`.

Always invoke the project-local CLI binary with telemetry disabled:

```sh
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs --version
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
```

In PowerShell:

```powershell
$env:ASTRO_TELEMETRY_DISABLED = "1"
node ./node_modules/astro/bin/astro.mjs build
```

## 4. Bootstrap a new or missing Astro v7 project

When `doctor` reports `package_json_missing` or `astro_not_installed` on an authorized setup task:

```sh
npm init --yes
npm install astro@7.4.2 --save-exact
```

(Or `npm install astro@7.3.1 --save-exact` when matching the locked canary baseline.)

Minimum runnable project layout:
- `package.json` with `"astro"` dependency
- `src/pages/index.astro`
- Optional `astro.config.mjs` (defaults to `output: 'static'`, `outDir: './dist'`, `build.format: 'directory'`)
- Optional `src/content.config.ts` when using Content Layer collections

## 5. CLI commands in Astro v7

- `build`: Build project to `dist/` (`--root`, `--outDir`, `--force` clears Content Layer cache).
- `sync`: Generate TypeScript types for Content Layer collections, `astro:env`, and `astro:actions`. Does not require `@astrojs/check`.
- `info`: Print environment receipt.
- `check`: Run diagnostics. **Only run if `doctor` reports `check.usable: true`**; otherwise `astro check` attempts an interactive package install.
- `dev` / `preview`: Local development servers. Astro v7 auto-backgrounds `dev`/`preview` when it detects an AI coding agent (`am-i-vibing`); set `ASTRO_DEV_BACKGROUND=0` if foreground execution is needed.
