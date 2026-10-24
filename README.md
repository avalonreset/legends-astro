# `legends-astro`

Astro v7 readiness, deterministic migration contract extraction (`source-audit`), WordPress/HTML/`legends-firecrawl` Content Layer scaffolding (`ingest`), post-build parity verification (`verify-build`), and a 24-page cryptographically verified Astro v7.4 documentation library for coding agents.

Version **0.2.0** (canonical successor to `legends-astro-kit`). Reviewed against **`astro@7.4.2`** (current upstream latest) and **`astro@7.3.1`** (isolated canary baseline) on Node `>=22.12.0` (`>=22.23.4` for `7.4.2`; tested baseline `v24.15.0`).

## Why `legends-astro` validates its own existence

Prompting a general frontier AI agent to "make an Astro site" or "convert my WordPress/HTML page to Astro" frequently yields a project where `astro build` exits `0` while silently dropping production requirements. `legends-astro` closes those failure modes with executable checks and verified Astro v7 references:

1. **Pre-migration Contract Extraction (`source-audit`):** Parses legacy HTML directories, WordPress WXR XML exports, WordPress REST JSON dumps, or `legends-firecrawl` scrapes into a machine-readable `migration-contract.json` (routes, URL shapes, `<title>`, `<meta name="description">`, canonicals, OpenGraph, JSON-LD schema blocks, client JS `getElementById` DOM IDs, script attributes, and first-paint inline styles).
2. **Deterministic Content Layer Scaffolding (`ingest`):** Preview-first generator that scaffolds valid Astro v7 Content Layer collections (`src/content.config.ts` using `glob()` from `astro/loaders` and `z` from `astro/zod`), shared `isPublished` draft filtering (`src/lib/published.ts`), and Markdown content entries instead of hallucinated Astro v4/v5 `src/content/config.ts` or `entry.render()` code.
3. **Post-Build Parity & Quality Proof (`verify-build`):** Inspects `dist/` after `astro build` and fails closed if any source route is missing, if any `draft: true` entry leaked a public HTML route or body text into `dist/`, if any page lost its `<title>`, meta description, or JSON-LD schema, if any required DOM `id` was stripped, if critical `<style is:inline>` order broke, or if any internal link or asset 404s.
4. **Un-handcuffed Project Doctor (`doctor`):** Inspects Node engines, `npm`/`pnpm`/`yarn`/`bun` lockfiles without mutating them, legacy `src/content/config.ts` traps, optional `@astrojs/check` readiness, and Windows `Astronomer.Astro` PATH collisions. Supports `--live` to check live npm registry versions and `withastro/docs` commits.
5. **Offline + Live Documentation Navigator (`docs-lookup`):** Instant search across 7 operational runbooks and 24 vendored official `withastro/docs` MDX pages (`0844aab04ea2f3859132ab97b1e9daa01ade76a1`), plus verified upstream endpoints for live lookups (and explicit guardrails against dead 404 URLs like `docs.astro.build/llms.txt`).

## Quick start

```sh
# 1. Check module readiness and live upstream Astro ecosystem versions:
python -m legends_astro doctor --live

# 2. Inspect a consumer Astro project:
python -m legends_astro doctor --project /path/to/astro-project

# 3. Search local Astro v7.4 docs and runbooks (or list live doc locations):
python -m legends_astro docs-lookup "wordpress"
python -m legends_astro docs-lookup --live-locations

# 4. Audit a legacy HTML directory, WordPress XML/JSON export, or Firecrawl scrape:
python -m legends_astro source-audit --source /path/to/legacy-site --output /path/to/astro-project/migration-contract.json

# 5. Preview and scaffold Astro v7 Content Layer collections from source:
python -m legends_astro ingest --source /path/to/legacy-site --output /path/to/astro-project
python -m legends_astro ingest --source /path/to/legacy-site --output /path/to/astro-project --apply

# 6. Build with project-local Astro (telemetry disabled) and prove parity:
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
python -m legends_astro verify-build --dist /path/to/astro-project/dist --project /path/to/astro-project --contract /path/to/astro-project/migration-contract.json
```

## Operational knowledge & Empire integration

Read [wiki/index.md](wiki/index.md) and [docs/AGENT-RECIPE.md](docs/AGENT-RECIPE.md).
An attachable `legends.knowledge-pack/v1` library is included at `knowledge/astro/pack.json` (suggested vault shelf: `wiki/library/legends-astro/astro`). Verify it with:

```sh
python -m legends_astro knowledge-pack --check
```

## License and attribution

Module code and operational runbooks are licensed under MIT ([LICENSE](LICENSE)). Vendored official Astro documentation under `vendor/astro-docs/` retains upstream MIT licensing from `withastro/docs` (`vendor/astro-docs/LICENSE` and [NOTICE](NOTICE)).
