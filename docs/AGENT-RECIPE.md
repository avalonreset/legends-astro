# Operate legends-astro

1. Resolve the selected module source and user workspace. Never save migration contracts, audit JSON, or site outputs into a managed module source.
2. Run `legends-astro doctor --json` (and `--project <astro-dir>` when inspecting an existing Astro site). This is offline, checks Node and local `astro` versions, checks for global `astro` PATH collisions, verifies the vendored 27-file `withastro/docs` snapshot, and flags Astro v7 Content Layer traps (`src/content/config.ts` vs `src/content.config.ts`). It never executes consumer `astro.config.*` files.
3. For legacy HTML, WordPress WXR `.xml`, WordPress REST `.json`, or `legends-firecrawl` `.json` migrations, run `legends-astro source-audit --source <path> --output <contract.json>` before writing Astro code. Review routes, URL shapes, metadata, JSON-LD, DOM IDs required by client JS, and converted Markdown.
4. Preview Content Layer scaffolding with `legends-astro ingest --source <path> --output <astro-dir>`. Only pass `--apply` when file writes are authorized.
5. In Astro v7 projects, define collections at `src/content.config.ts` using `glob()` from `astro/loaders` and `z` from `astro/zod`, use `entry.id` instead of `entry.slug`, and filter `draft !== true` in both index listings and `getStaticPaths()`.
6. After `node ./node_modules/astro/bin/astro.mjs check` and `build`, run `legends-astro verify-build --dist <dist-dir> --project <astro-dir> --contract <contract.json>` to verify route parity, zero draft leakage, SEO/JSON-LD preservation, client-JS DOM IDs, and first-paint `<head>` CSS order.
7. Use `legends-astro docs-lookup "<query>" --live-locations` to search the 24 vendored `withastro/docs` MDX guides and 9 `wiki/` runbooks. Note that `https://docs.astro.build/llms.txt` and `/llms-full.txt` return HTTP 404 upstream; use `docs-lookup` or raw GitHub MDX files.
8. Save a private migration receipt only when requested or part of an authorized Empire workflow (`templates/migration-receipt.md`).

Do not install a standalone module skill. The central `cto-legends` skill loads this recipe on demand.
