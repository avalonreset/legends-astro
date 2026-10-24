<a name="banner"></a>
<a name="legends-astro"></a>

# ![legends-astro](assets/banner.webp)

[![release](https://img.shields.io/github/v/release/avalonreset/legends-astro?style=flat-square&label=release&labelColor=000000&color=ff0000)](https://github.com/avalonreset/legends-astro/releases/latest)
[![checks](https://img.shields.io/github/actions/workflow/status/avalonreset/legends-astro/checks.yml?branch=main&style=flat-square&label=checks&labelColor=000000)](https://github.com/avalonreset/legends-astro/actions/workflows/checks.yml)
[![license](https://img.shields.io/github/license/avalonreset/legends-astro?style=flat-square&label=license&labelColor=000000&color=666666)](LICENSE)

**Astro v7 readiness, WordPress to Astro and HTML migration contracts, Content Layer collections, and build parity verification for coding agents.**

Use `legends-astro` with your coding agent to inspect **astrojs** (`astro static site generator`) workspaces, extract deterministic pre-migration contracts for **WordPress to Astro**, **Next.js to Astro**, and legacy HTML to **Astro static site** rebuilds, scaffold Astro v7 **Astro content collections** (`src/content.config.ts`), verify **Astro SEO** and DOM parity in `dist/`, and search a checksum-pinned snapshot of official `withastro/docs` guides offline. Keep migration receipts and operating knowledge in Markdown that works with Obsidian and [legends-empire](https://github.com/avalonreset/legends-empire).

`legends-astro` replaces the legacy `legends-astro-kit` identifier while retaining `legends-astro-kit` as a compatibility alias in [cto-legends](https://github.com/avalonreset/cto-legends). This is an independent community project built around official Astro documentation and CLI workflows. It is not affiliated with or endorsed by the Astro project.

## Table of contents

- [Why use this with an agent?](#why-use-this-with-an-agent)
- [Install](#install)
- [Quick start: WordPress to Astro and Next.js to Astro usage](#quick-start-wordpress-to-astro-and-nextjs-to-astro-usage)
- [Astro content collections and Astro SEO CLI reference](#astro-content-collections-and-astro-seo-cli-reference)
- [Astro static site generator architecture and how it works](#astro-static-site-generator-architecture-and-how-it-works)
- [CTO Legends router](#cto-legends-router)
- [Knowledge vault and Empire](#knowledge-vault-and-empire)
- [FAQ](#faq)
- [Boundaries and verification](#boundaries-and-verification)
- [Contributing and license](#contributing-and-license)

## Why use this with an agent?

Prompting a generic frontier LLM to "convert my WordPress or HTML site to Astro" routinely fails in six predictable ways: outdated Astro v4/v5 Content Collections syntax (`src/content/config.ts`, `type: 'content'`, and `entry.slug`), dropped DOM `id` attributes referenced by client JavaScript, bundled `<head>` CSS that flashes on first paint, silent route-shape changes (`/page.html` vs `/page/index.html`) that break URLs and canonicals, leaked `draft: true` content in production builds, and global `astro` PATH collisions with the Astronomer Airflow CLI.

| Need | What this module supplies |
|---|---|
| Know runtime & Astro v7 readiness | `doctor` checks Node (`>=22.12.0` / `>=22.23.4`, LTS `24.15.0`), global `astro` PATH collisions, lockfiles, and Astro v7 Content Layer traps without executing `astro.config.*` |
| Extract a pre-migration contract | `source-audit` parses HTML trees, WordPress WXR `.xml`, WP REST `.json`, or `legends-firecrawl` `.json` into a deterministic `migration-contract.json` |
| Scaffold Astro content collections | `ingest` previews and generates `src/content.config.ts` (`glob()` + `astro/zod`), `src/lib/published.ts` (`draft !== true`), clean Markdown entries, and `public/_redirects` |
| Preserve Astro SEO & client JS | `verify-build` checks built `dist/` HTML against `migration-contract.json` for routes, `<title>`, meta descriptions, JSON-LD, client-JS DOM IDs, first-paint CSS order, and internal links |
| Prevent accidental draft leaks | `verify-build` inspects `src/content/` for `draft: true` entries and fails if any draft route or body text appears in `dist/` |
| Query verified Astro v7 docs | `docs-lookup` searches 24 SHA-256-verified `withastro/docs` MDX guides and 9 `wiki/` runbooks offline, plus verified upstream endpoints when live checks are requested |
| Fit the existing ecosystem | One central `cto-legends` router skill (`skills/cto-legends/SKILL.md`); module recipe (`docs/AGENT-RECIPE.md`) loaded only when needed |

## Install

Python 3.10 or newer for the `legends-astro` CLI; Node.js 22.12.0 or newer (LTS 24.15.0 tested) for Astro project builds and `scripts/doctor.mjs`. Zero mandatory Python runtime dependencies.

Through `cto-legends`:

```sh
cto-legends sync
cto-legends sync --apply
cto-legends install legends-astro
cto-legends install legends-astro --apply
cto-legends handoff legends-astro
```

Direct from source:

```sh
git clone https://github.com/avalonreset/legends-astro.git
cd legends-astro
python -m pip install .
legends-astro doctor --json
```

## Quick start: WordPress to Astro and Next.js to Astro usage

Run an end-to-end **WordPress to Astro** or HTML to **Astro static site** migration audit and verification loop:

```sh
# 1. Check local Node runtime, vendored docs checksums, and target Astro project readiness
legends-astro doctor --project /path/to/astro-site --json

# 2. Extract routes, SEO metadata, JSON-LD, client-JS DOM IDs, and Markdown from legacy source
legends-astro source-audit --source /path/to/wp-export.xml --output migration-contract.json

# 3. Preview Astro v7 Content Layer scaffolding (read-only by default; add --apply to write)
legends-astro ingest --source /path/to/wp-export.xml --output /path/to/astro-site

# 4. After running local astro check && astro build, verify dist/ against the contract
legends-astro verify-build --dist /path/to/astro-site/dist --project /path/to/astro-site --contract migration-contract.json
```

Look up Astro v7 Content Layer syntax or migration rules offline:

```sh
legends-astro docs-lookup "astro content collections"
legends-astro docs-lookup "wordpress to astro" --live-locations
```

## Astro content collections and Astro SEO CLI reference

### 1. Inspect runtime and Astro project readiness (`doctor`)

```sh
legends-astro doctor --json
legends-astro doctor --project /path/to/astro-site --json
legends-astro doctor --project /path/to/astro-site --strict-pin --json
legends-astro doctor --live --json
```

Inspects Node runtime (`>=22.12.0` for `astro@7.3.1`, `>=22.23.4` for `astro@7.4.2`, tested LTS baseline `24.15.0`), checks for global `astro` PATH collisions, verifies the 27-file `vendor/astro-docs/manifest.json` SHA-256 snapshot and `knowledge/astro/pack.json`, validates local `node_modules/astro/bin/astro.mjs` and lockfile consistency (`npm`, `pnpm`, `yarn`, `bun`), and flags Astro v7 traps (`src/content/config.ts` vs `src/content.config.ts`, `prerender = false` routes). Never imports or executes consumer `astro.config.*` files.

### 2. Extract a pre-migration contract (`source-audit`)

```sh
legends-astro source-audit --source /path/to/legacy-html-or-wp.xml --output migration-contract.json
```

Audits a legacy HTML directory/file, WordPress WXR `.xml` export, WordPress REST `.json` dump, or `legends-firecrawl` `.json` scrape and produces a deterministic `migration-contract.json` recording:
- Every route and URL shape (`file`, `directory`, `clean`, `root`) plus recommended `build.format` and `trailingSlash` settings
- `<title>`, `<meta name="description">`, canonical link, OpenGraph, Twitter Card, and JSON-LD `@type` blocks
- Every DOM `id` and `data-*` attribute, cross-referenced against `getElementById` / `querySelector('#...')` in `.js` files
- Inline `<head>` styles, scripts, forms, internal links, local assets, and clean converted Markdown body content

### 3. Scaffold an Astro v7 Content Layer collection (`ingest`)

```sh
# Preview planned files (read-only)
legends-astro ingest --source /path/to/wp-export.xml --output /path/to/astro-site

# Write files when authorized
legends-astro ingest --source /path/to/wp-export.xml --output /path/to/astro-site --apply
```

Generates `src/content.config.ts` (using `glob()` from `astro/loaders` and `z` from `astro/zod`), `src/lib/published.ts` (`draft !== true` filter), `src/content/pages/*.md` with converted Markdown body content, `public/_redirects` for legacy route aliases, and `migration-contract.json`.

### 4. Verify built `dist/` parity (`verify-build`)

```sh
legends-astro verify-build --dist /path/to/astro-site/dist --project /path/to/astro-site --contract migration-contract.json
```

Mechanically verifies that:
- Every published route in `migration-contract.json` exists in `dist/`
- No `draft: true` entry in `src/content/` leaked a route or body text into `dist/`
- No `<title>`, `<meta name="description">`, or JSON-LD block was lost
- Every required client-JS DOM `id` is preserved in the built HTML
- First-paint `<style data-first-paint>` blocks remain inline in `<head>` before any `<script>`
- All internal `<a href="/...">` links and local asset references resolve inside `dist/`

### 5. Search vendored Astro v7 docs and live upstream locations (`docs-lookup`)

```sh
legends-astro docs-lookup "wordpress" --live-locations
legends-astro docs-lookup "content collections"
legends-astro docs-lookup --list
legends-astro docs-lookup --check-live
```

Searches the 24 vendored `withastro/docs` MDX pages and 9 operational runbooks in `wiki/`, and reports verified upstream endpoints (`registry.npmjs.org/astro/latest`, `api.github.com/repos/withastro/docs/commits/main`) when live checks are needed. Note that `https://docs.astro.build/llms.txt` and `/llms-full.txt` return HTTP 404 upstream; always use `legends-astro docs-lookup` or the GitHub raw MDX tree.

## Astro static site generator architecture and how it works

`legends-astro` separates static inspection (`legends_astro/cli.py`, `scripts/doctor.mjs`), source-to-contract extraction (`legends_astro/auditor.py`), documentation indexing (`legends_astro/docs_nav.py`), and knowledge pack attachment (`knowledge/astro/pack.json`).

1. **Zero arbitrary config execution:** Consumer `astro.config.*` and project scripts are inspected as text and AST-free regex patterns so untrusted repositories cannot execute code during `doctor` or `verify-build`.
2. **Deterministic HTML-to-Markdown & redirect generation:** `_HTMLToMarkdownParser` converts legacy HTML and WordPress content (`<h1>`-`<h6>`, `<p>`, `<ul>`/`<ol>`, `<pre><code>`, `<blockquote>`, `<a>`, `<img>`) into clean Markdown while recording legacy route aliases in `public/_redirects`.
3. **Pinned documentation provenance:** Every file in `vendor/astro-docs/` is verified by SHA-256 against `vendor/astro-docs/manifest.json` (`withastro/docs` commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1`).

## CTO Legends router

The module recipe is [docs/AGENT-RECIPE.md](docs/AGENT-RECIPE.md). The vendored `skills/cto-legends/SKILL.md` is an unchanged copy of the central router. Do not register a separate `legends-astro` skill when using the central router.

```sh
cto-legends handoff legends-astro
cto-legends run legends-astro -- doctor --json
```

## Knowledge vault and Empire

Start at [wiki/index.md](wiki/index.md) or attach the versioned knowledge pack at [knowledge/astro/pack.json](knowledge/astro/pack.json) (`legends.knowledge-pack/v1`):

- [Wiki index](wiki/index.md)
- [Inspect setup and runtime](wiki/inspect-setup.md)
- [Pages, components, styles, and scripts](wiki/pages-components-styles-scripts.md)
- [Content collections (`src/content.config.ts`)](wiki/content-collections.md)
- [Migrate HTML, WordPress, and Next.js](wiki/migrate-and-wordpress.md)
- [Performance, headers, and caching](wiki/performance-and-caching.md)
- [Diagnose and upgrade Astro v7](wiki/diagnose-and-upgrade.md)
- [Latest docs and ecosystem lookup](wiki/latest-docs-and-ecosystem.md)
- [Source ledger](references/sources.md)
- [Migration receipt template](templates/migration-receipt.md)
- [Empire attachment guide](docs/EMPIRE.md)

Attach the knowledge pack to an Empire vault via `legends-empire`:

```sh
cto-legends run legends-empire -- knowledge attach /path/to/legends-astro/knowledge/astro/pack.json --vault /path/to/vault --operation-id op-astro-001 --generated-at 2026-10-24T00:00:00Z
```

## FAQ

### How does `legends-astro` handle WordPress to Astro migrations?
Run `legends-astro source-audit --source wp-export.xml --output migration-contract.json` on a WordPress WXR `.xml` or REST `.json` export, then run `legends-astro ingest --source wp-export.xml --output ./site --apply` to generate Astro v7 Content Layer collections (`src/content.config.ts`), converted Markdown entries, draft filters (`src/lib/published.ts`), and `public/_redirects`.

### Why does `legends-astro` enforce `src/content.config.ts` instead of `src/content/config.ts`?
Astro v7 uses the Content Layer API (`src/content.config.ts`, `glob()` from `astro/loaders`, `z` from `astro/zod`, and `entry.id`). Legacy v4/v5 `src/content/config.ts` patterns written from stale model training data fail in Astro v7 builds.

### Does `legends-astro` replace the `astro` CLI?
No. Astro builds and typechecks still run through project-local `node ./node_modules/astro/bin/astro.mjs`. `legends-astro` provides the pre-build readiness audit, migration contract extraction, Content Layer scaffolding, offline documentation search, and post-build `dist/` parity verification that coding agents miss.

## Boundaries and verification

This release does not deploy sites to hosting providers, run a live browser crawler, or execute untrusted consumer `astro.config.*` files. Pair with `legends-firecrawl` for live site scraping and `legends-cloudflare`, `legends-coolify`, or `legends-hetzner` for infrastructure and DNS cutover.

```sh
python -m unittest discover -s tests -v
node ./scripts/verify-sources.mjs
node ./scripts/test-doctor.mjs
python scripts/check_release.py
```

## Contributing and license

See [compatibility and verification](docs/RELEASE-READINESS.md), [contributing](CONTRIBUTING.md), [code of conduct](CODE_OF_CONDUCT.md), [security](SECURITY.md), [support](SUPPORT.md), and [attribution](THIRD_PARTY_NOTICES.md). MIT licensed ([LICENSE](LICENSE)). Vendored `withastro/docs` files in `vendor/astro-docs/` are MIT licensed (Copyright (c) 2021 Fred K. Schott; see `vendor/astro-docs/LICENSE` and `THIRD_PARTY_NOTICES.md`).
