# Operate `legends-astro`

1. Resolve the selected module root and the user's target project workspace. Never write user site files or migration contracts inside the managed `legends-astro` source tree.
2. Run `python -m legends_astro doctor` (or `python -m legends_astro doctor --project <consumer-root>`). Add `--live` when the user asks about the latest Astro version, companion packages, or documentation freshness.
3. Search local operational runbooks and the 24 vendored official Astro v7.4 MDX pages before browsing the web:
   ```sh
   python -m legends_astro docs-lookup "<topic>"
   python -m legends_astro docs-lookup --live-locations
   ```
4. **For site migrations (HTML, WordPress XML/JSON, or `legends-firecrawl` scrapes):**
   - Extract the baseline contract first:
     ```sh
     python -m legends_astro source-audit --source <legacy-source> --output <project>/migration-contract.json
     ```
   - Preview and scaffold Astro v7 Content Layer collections (`src/content.config.ts` with `glob()` and `astro/zod`, plus `src/lib/published.ts`):
     ```sh
     python -m legends_astro ingest --source <legacy-source> --output <project>
     python -m legends_astro ingest --source <legacy-source> --output <project> --apply
     ```
5. **For building and verifying any Astro v7 site:**
   - Run project-local Astro with telemetry disabled:
     ```sh
     ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs sync
     ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
     ```
   - Run the mechanical post-build proof gate:
     ```sh
     python -m legends_astro verify-build --dist <project>/dist --project <project> [--contract <project>/migration-contract.json]
     ```
6. Read [wiki/index.md](../wiki/index.md) for task-specific runbooks:
   - [wiki/inspect-setup.md](../wiki/inspect-setup.md)
   - [wiki/pages-components-styles-scripts.md](../wiki/pages-components-styles-scripts.md)
   - [wiki/content-collections.md](../wiki/content-collections.md)
   - [wiki/migrate-and-wordpress.md](../wiki/migrate-and-wordpress.md)
   - [wiki/performance-and-caching.md](../wiki/performance-and-caching.md)
   - [wiki/diagnose-and-upgrade.md](../wiki/diagnose-and-upgrade.md)
   - [wiki/latest-docs-and-ecosystem.md](../wiki/latest-docs-and-ecosystem.md)

Do not install a standalone module skill. The central `cto-legends` skill routes requests and loads this recipe on demand.
