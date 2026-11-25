# Changelog

## 0.4.0 - 2026-11-25

- Added Day-1 greenfield agency starter scaffolding via `legends-astro init` and `legends-astro scaffold --mode starter` (`--preset agency|blog|local-business`, `--trailing-slash always|never|ignore`, `--site`, `--with-sitemap` / `--no-sitemap`, `--staging-lock`, preview-first `planSha256`). Generates `src/content.config.ts` (`glob()` + `astro/zod`), `isPublished` draft + scheduled future `pubDate` production filtering, zero-dependency RSS 2.0 `src/pages/rss.xml.ts`, `BaseLayout` + `BlogPostLayout` with `Organization`/`LocalBusiness` + `BlogPosting` + `BreadcrumbList` JSON-LD, and repo-local `ASTRO-BUILD-GUIDELINES.md`.
- Added Day-2 article-to-live publishing via `legends-astro post`: read-only `--list`, preview-first create (`--input` or `--title` + `--body-file`, `--hero` / `--hero-alt`, `--draft` / `--publish`), `--update` preserving `pubDate` and unknown custom frontmatter keys while auto-stamping `updatedDate`, and safe `--rename-from` appending deduplicated, chain-flattened, cycle-checked `301` rules to `public/_redirects`. Enforces strict slugs, magic-byte + SVG XSS image localization into `public/images/blog/<slug>/`, static read-only `src/content.config.*` detection, and editorial SEO preflight lint.
- Added Day-2 regression manifest gating to `legends-astro verify-build` (`--write-manifest`, `--baseline`, `--overwrite`, `--allow-canonical-change`) emitting fail-closed `legends.astro-site-manifest/v1` (never written on failure) with `baseline_route_missing` (honoring valid redirects), canonical, title, description, H1, and JSON-LD type checks plus `baselineDiff`.
- Refreshed `vendor/astro-docs/` to `withastro/docs@7f32508fbe63450b15ff4503a7062c847e1cd0e5` (`2026-11-25T13:19:10Z`) and added 8 official English MDX guides (`recipes/rss.mdx`, `guides/integrations-guide/mdx.mdx`, `guides/markdown-content.mdx`, `guides/endpoints.mdx`, `guides/cms/index.mdx`, `guides/deploy/index.mdx`, `guides/deploy/netlify.mdx`, `guides/deploy/vercel.mdx`; 53 MDX guides / 56 manifest entries total).
- Added agency runbooks `wiki/agency-starter-guidelines.md` and `wiki/publishing-and-content-ops.md` (Git CLI publishing lane plus zero-lock-in Git-backed CMS lane), expanded the attachable Empire knowledge pack to 12 SHA-256 verified files (`knowledge/astro/pack.json` `0.4.0`), and refreshed evergreen README copy and `r1` SVG cards to cover Day-1 starter, migration, Day-2 publishing, and the regression manifest gate.

## 0.3.0 - 2026-11-24

- Upgraded Astro baseline and canary lock to `astro@7.4.13` (`engines.node >=22.23.4`, `sharp ^0.35.5`, `undici ^8.13.1`), refreshed all 24 existing `withastro/docs` MDX guides to commit `87c37b156627418328b8d3d4671320ca20487c79`, and added 21 new official Astro v7.4 MDX guides (45 MDX guides / 48 manifest files total: Fonts API, Font Provider Reference, `@astrojs/sitemap`, `@astrojs/cloudflare`, Cloudflare deploy, `@astrojs/node`, `astro:assets`, Routing Reference, Vite Plugin Astro, plus every official `migrate-to-astro/` guide covering Create React App, Docusaurus, Eleventy, Gatsby, GitBook, Gridsome, Hugo, Jekyll, NuxtJS, Pelican, SvelteKit, and VuePress).
- Added multi-framework static repository auditor (`legends_astro/framework_audit.py` wired into `source-audit`): read-only static auto-detection for Next.js (App Router, Pages Router, hybrid), Nuxt, Gatsby, Vite/CRA (React, Vue, Svelte, Preact, Solid), Docusaurus, Hugo, Jekyll, Eleventy, and SvelteKit emitting `frameworkMigration` (routes, API routes, static redirects from `vercel.json`/`netlify.toml`/`_redirects`/`next.config.*`/`nuxt.config.*`, interactive islands vs static `.astro` candidates, `PUBLIC_*` env var renames, framework import/syntax traps, Markdown/MDX content collections, and exact vendored `docs-lookup` IDs) while preserving precedence for `raw-capture`, Firecrawl, WordPress, and HTML trees.
- Added smart optional `legends-firecrawl` ecosystem integration: `capture` detects WAF/bot challenges (HTTP 403/429/503, `cf-mitigated`, Cloudflare/Turnstile/Akamai/PerimeterX/DataDome) and empty JS SPA shells (`#root`, `#__next`, `#app`, `#__nuxt`) and emits `firecrawlFallback` handoff guidance; `doctor` emits a read-only, non-blocking `ecosystem` block reporting optional `legends-firecrawl` and Playwright availability.
- Refreshed outlined Legends Font `r1` SVG architecture, three-pillars, plain-english chains, and comparison cards (`scripts/render_cards.py`) with evergreen copy (zero hardcoded version numbers or doc counts).
- Added `@astrojs/cloudflare` to live ecosystem probes (`docs_nav.py`) and hardened TLS CA verification via strict `ssl.SSLContext` + optional `certifi` bundle loading without weakening certificate checks.
- Added shared network and filesystem safety module (`legends_astro/net_safety.py`): CWE-918 SSRF and DNS-rebinding IP pinning, explicit `--allow-host` allowlisting, `https://` default (`--allow-http` opt-in), manual redirect hop validation (max 5 hops), bounded streaming reads, CWE-59 symlink refusal, Windows Controlled Folder Access refusal, CWE-22 path containment, `O_CREAT | O_EXCL` writes, and CWE-776 XXE-safe XML parsing.
- Added `capture` command (`legends_astro/capture.py`): live recursive sitemap / bounded BFS crawl and local `legends-firecrawl` archive/directory normalizer (`response.data`, `legends-firecrawl-evidence/v1`, top-level `data`) with `__`-delimited collision-safe slugs and deterministic Migration Triage reporting (platform, page builders, `[shortcodes]`, forms/Turnstile/reCAPTCHA, WooCommerce, Cloudflare email obfuscation, trackers, external CDNs).
- Added `vendor-assets` command (`legends_astro/vendor.py`): two-pass HTML + recursive CSS `@import`/`url(...)` asset localizer handling `srcset`, `data-poster-url`, `data-video-urls`, inline `background-image`, and `dynamic_css.php` -> `.css`, writing `public/` + `asset-map.json`, plus longest-URL-first rewriter and local SRI `integrity=`/`crossorigin=` cleanup.
- Added `scaffold` command (`legends_astro/scaffold.py`, `--mode clone|editorial`): preview-first (`planSha256` -> `--apply --confirm-plan`) Astro v7.4.13 project generator with `<Fragment set:html={...} />` exact-DOM clone layout, Cloudflare XOR hex email decoder (`--decode-cf-email`), live tracker stripping (`--strip-trackers`), and Triple-Lock staging (`--staging-lock`).
- Added `twin` and `twin-diff` commands (`scripts/twin.mjs` + `legends_astro/twin.py`): optional runtime Playwright Visual + Computed-Style Digital Twin capture for live `--url` (SSRF + `page.route` guarded) or local `--dist` (ephemeral in-process `127.0.0.1:0` read-only static server closed in `finally`) plus pure offline `twin-diff` regression gate.
- Upgraded `source-audit` and `verify-build` (`legends_astro/auditor.py`): Yoast/RankMath title precedence, WordPress `attachment` -> `_thumbnail_id` featured images, Custom Post Types/taxonomies/authors, Markdown `<table>`/`<figure>`/`<iframe>` conversion, Firecrawl directory unwrapping, plus `verify-build --forbid-host`, `--env staging|production`, CSS `url()`/`@import` resolution, local SRI check, canonical/OG/JSON-LD/H1/Webflow `data-wf-page` parity, `trailingSlash` shape verification, XXE-safe sitemap verification, `_redirects` loop/target validation, and `/cdn-cgi/` + `[shortcode]` leak checks.

## 0.2.6 - 2026-11-22

- Sync vendored `cto-legends` router skill pin (`69ddc4f68faa`), enforce LF line endings via `.gitattributes`, and standardize release artifact checksums.

## 0.2.5 - 2026-11-21

- Added `scripts/render_cards.py` and the 7 outlined Legends Font `r1` SVG architecture, plain-english chain, comparison, handoff, message-desk, and star-shrine cards under `assets/cards/r1/`, removing the duplicate root `assets/collection-star-shrine.svg`.
- Rewrote `README.md` to the evergreen CTO Legends visual architecture standard with collapsible `<details>` reference drawers and zero hardcoded version or module-count drift.
- Added `VERSION` and expanded `scripts/check_release.py` and packaging manifests (`MANIFEST.in`, `setup.py`) to verify `0.2.5` version synchronization and `assets/cards/r1/*.svg` integrity.

## 0.2.4 - 2026-11-10

- Repaired control-character corruption in `vendor/astro-docs/README.md`, updated `vendor/astro-docs/manifest.json`, and expanded `scripts/check_release.py` to scan all tracked text files (including `vendor/`) for ASCII control characters and `U+FFFD`.
- Removed hidden-window spawn option (`windowsHide`) from `scripts/test-doctor.mjs`, excluded `scripts/test-*.mjs` from wheel `_data` mirroring in `setup.py`, and added `windowsHide` detection to `scripts/check_release.py` (Windows 11 EDR Hard Law 1).
- Hardened `auditor.py` and `cli.py` with CWE-59 symlink chain refusal, Windows Controlled Folder Access directory protection, `O_CREAT | O_EXCL` overwrite guards (`--overwrite`), preview `planSha256` confirmation (`--confirm-plan`), `dist/` path-containment verification, `50 MB` WXR / `10 MB` source file size caps, Expat `<!DOCTYPE` / `<!ENTITY` refusal (CWE-776), and `json.dumps` YAML frontmatter scalar escaping (CWE-74).
- Hardened `probe_live_ecosystem` in `docs_nav.py` with `_NoRedirectHandler`, `ProxyHandler({})`, HTTPS host allowlisting (`registry.npmjs.org`, `api.github.com`), and a `1 MB` response cap (CWE-918).
- Documented `resources` and `knowledge-pack` subcommands and complete Windows 11 EDR Hard Law 6 guardrails across `README.md`, `AGENTS.md`, and `docs/AGENT-RECIPE.md`, and added `.legends-router-pin` to `MANIFEST.in` and `scripts/check_release.py`.

## 0.2.3 - 2026-11-05

- Enforced single-skill router compliance by removing redundant `skills/legends-astro/` directory (`skills/cto-legends/SKILL.md` is the sole shipped skill).
- Added `setup.py` build-time wheel data mirror (`legends_astro/_data/`), `legends-astro resources` subcommand, `.legends-module`, `assets/manifest.json`, `scripts/smoke_installed_resources.py`, and `scripts/verify_knowledge_attachment.py` so standalone wheel installs resolve all knowledge packs, vendored docs, and Node scripts outside a source checkout.
- Upgraded `scripts/check_release.py` and `.github/workflows/checks.yml` with Gold-Standard EDR/metadata/dash gates and multi-OS `empire-attachment` CI verification.

## 0.2.2 - 2026-11-04

- Add explicit agent guardrails in `AGENTS.md`, `docs/AGENT-RECIPE.md`, and `wiki/inspect-setup.md` forbidding detached background `astro dev` / `astro preview` daemons and `--open` browser launches during automated agent runs (preventing Windows EDR local-portal behavioral alerts) in favor of offline `astro build` + `legends-astro verify-build`.

## 0.2.1 - 2026-10-29

- Raise minimum supported Python runtime to Python 3.11 (`requires-python = ">=3.11"`) across package metadata, documentation, and multi-OS CI.
- Upgrade transitive `sharp` (`>=0.35.5`, fixing `CVE-2026-96889`) and `undici` (`>=8.12.2`, fixing `CVE-2026-109383`) in `canary/package-lock.json` via npm `overrides`.
- Harden `.github/workflows/checks.yml` with 40-character commit SHA action pins, `persist-credentials: false`, and `pip>=26.2.2` before package installation.

## 0.2.0 - 2026-10-24

- Renamed public module from `legends-astro-kit` to `legends-astro` (preserving `legends-astro-kit` as a legacy compatibility alias).
- Added Python CLI (`legends-astro`) with `doctor`, `source-audit`, `ingest`, `verify-build`, `docs-lookup`, and `knowledge-pack` subcommands.
- Added deterministic `source-audit` and `ingest` for HTML trees, WordPress WXR XML, WordPress REST JSON, and `legends-firecrawl` JSON, including HTML-to-Markdown body conversion and `public/_redirects` generation.
- Added mechanical `verify-build` post-build parity verifier checking route preservation, SEO/JSON-LD parity, client JS DOM `id` ABI, draft exclusion, first-paint `<style is:inline>` ordering, and internal link/asset resolution.
- Expanded vendored `withastro/docs` snapshot to commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1` (24 official MDX pages + license/notice/readme = 27 verified files) and reviewed upstream `astro@7.4.2` / canary pin `astro@7.3.1`.
- Added attachable `legends-empire` knowledge pack (`knowledge/astro/pack.json`, `legends.knowledge-pack/v1`).
