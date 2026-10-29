# Capabilities and verification scope

Module: `legends-astro` **0.2.1** (successor to `legends-astro-kit` 0.1.0), verified **2026-10-24**.

## Executed and verified in `legends-astro v0.2.1`

- **Dual CLI & Router Contract:** Python CLI (`python -m legends_astro` / `legends-astro`) and Node helpers (`scripts/doctor.mjs`, `scripts/verify-sources.mjs`) with zero external Python package dependencies.
- **Project & Module Readiness (`doctor`):**
  - Verifies Node runtime against Astro v7 engine requirements (`>=22.12.0` for `7.3.1`, `>=22.23.4` for `7.4.2`, tested canary baseline `24.15.0`, compatible warning on odd majors such as `v25.9.0`).
  - Supports `npm` (`package-lock.json`), `pnpm` (`pnpm-lock.yaml`), `yarn` (`yarn.lock`), and `bun` (`bun.lock` / `bun.lockb`) without mutating consumer lockfiles.
  - Detects legacy `src/content/config.ts` paths, on-demand routes (`prerender = false`), optional `@astrojs/check` + `typescript` availability, and global `Astronomer.Astro` PATH collisions.
  - Live ecosystem probe (`doctor --live`) queries `registry.npmjs.org` for `astro` (`7.4.2`) and official `@astrojs/*` companions plus `api.github.com/repos/withastro/docs/commits/main`.
- **Pre-Migration Contract Extraction (`source-audit`):**
  - Deterministic extraction of routes, URL shapes (`file`, `directory`, `clean`), `<title>`, `<meta name="description">`, canonical URLs, OpenGraph metadata, `application/ld+json` blocks, DOM `id` values referenced by JS (`getElementById`, `querySelector`), script attributes (`type="module"`, `defer`, `async`), and local assets from HTML trees, WordPress WXR XML exports, WordPress REST JSON dumps, and `legends-firecrawl` JSON files.
- **Content Layer Scaffolding (`ingest`):**
  - Preview-first (`--apply` to write) generation of Astro v7 `src/content.config.ts` (`glob()` + `astro/zod`), shared `src/lib/published.ts` draft predicate, Markdown content files, and `migration-contract.json`.
- **Post-Build Parity & Quality Gate (`verify-build`):**
  - Verifies built `dist/` HTML against source `migration-contract.json` and project `src/content/` drafts: route coverage, zero draft route or body text leaks, `<title>`/description/JSON-LD preservation, required DOM `id` retention, `<style is:inline>` before `<script>` ordering, and internal link/asset resolution.
- **Curated Official Astro v7.4 Documentation Snapshot (`vendor/astro-docs/`):**
  - 24 official MDX files from `withastro/docs@0844aab04ea2f3859132ab97b1e9daa01ade76a1` (`2026-10-22T14:08:43Z`) verified offline via SHA-256 hashes and negative tamper/missing/path-escape self-tests (`scripts/verify-sources.mjs`).
- **Attachable Empire Knowledge Pack (`knowledge/astro/pack.json`):**
  - Schema `legends.knowledge-pack/v1` with SHA-256 verified operational runbooks and templates.
- **Isolated Static Canary (`canary/`):**
  - Locked `astro@7.3.1` 4-page static build (`index.html`, `privacy.html`, `terms.html`, `notes/visible.html` with `notes/secret-draft.md` excluded) plus loopback HTTP stub parity tests (`test-canary-static.mjs`, `test-handler-parity.mjs`).

## Explicit boundaries and exclusions

- Does not claim browser rendering, computed visual layout, or Lighthouse/Core Web Vitals improvements from build-time HTML checks alone.
- Does not replace `legends-firecrawl` for live web scraping, `legends-cloudflare` for DNS/edge rules, or `legends-coolify` / `legends-hetzner` for server deployment.
- Does not silently upgrade a consumer's Astro version or convert non-npm lockfiles.
