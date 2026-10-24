# Astro operating knowledge

Use this knowledge library with the `legends-astro` CLI or as a standalone Markdown reference. Read the dated [source ledger](sources.md) for exact upstream commits and package versions.

| Task | Runbook | CLI command | Evidence to keep in project |
|---|---|---|---|
| Check environment, Node runtime, lockfile, or live version drift | [Inspect and setup](inspect-setup.md) | `legends-astro doctor --project <dir> [--live]` | Doctor JSON receipt |
| Build pages, layouts, CSS, and client scripts without breaking order | [Pages, components, styles, scripts](pages-components-styles-scripts.md) | `legends-astro verify-build --dist <dist>` | `dist/` verification receipt |
| Configure Astro v7 Content Layer collections and prove draft exclusion | [Content collections](content-collections.md) | `legends-astro verify-build --dist <dist> --project <dir>` | Built route and draft-exclusion receipt |
| Migrate WordPress, HTML, or `legends-firecrawl` pages to Astro | [Migrate and WordPress conversion](migrate-and-wordpress.md) | `legends-astro source-audit` -> `ingest` -> `verify-build` | `migration-contract.json` and parity report |
| Optimize assets, images, and origin cache headers | [Performance and caching](performance-and-caching.md) | `legends-astro verify-build --dist <dist>` | Asset inventory and header checks |
| Diagnose compiler errors, `astro check`, or version upgrades | [Diagnose and upgrade](diagnose-and-upgrade.md) | `legends-astro doctor --project <dir> --live` | Upgrade diff and post-upgrade build proof |
| Find latest official Astro docs, changelogs, and ecosystem versions | [Latest docs and ecosystem lookup](latest-docs-and-ecosystem.md) | `legends-astro docs-lookup <query> --live-locations` | Retrieved commit SHA and package version |

## Why `legends-astro` exists beyond a raw agent prompt

Asking a general AI agent to "build an Astro site" or "convert my WordPress page to Astro" frequently produces code that compiles (`astro build` exits 0) while silently introducing production regressions:
- Using deprecated Astro v4/v5 content APIs (`src/content/config.ts`, `type: 'content'`, `entry.render()`) that fail under Astro v6/v7 Content Layer.
- Filtering `draft: true` only on an index listing while `getStaticPaths()` still emits public HTML files for drafts in `dist/`.
- Dropping URL shape parity (`/about.html` vs `/about/`), canonical URLs, `<meta name="description">`, OpenGraph tags, or `application/ld+json` structured data during migration.
- Breaking vanilla client scripts by stripping or renaming DOM `id` attributes, losing `type="module"` or `defer` when adding `is:inline`, or applying scoped CSS to DOM nodes created dynamically via `document.createElement()`.
- Extracting critical first-paint `<style>` blocks into external `/_astro/*.css` stylesheets or hoisting blocking scripts above them.

`legends-astro` replaces guesswork with deterministic pre-migration extraction (`source-audit`), Astro v7 Content Layer scaffolding (`ingest`), and mechanical post-build verification (`verify-build`), backed by 24 cryptographically verified official Astro v7.4 MDX docs on disk.
