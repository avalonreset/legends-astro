---
name: legends-astro
description: >
  legends-astro (successor to legends-astro-kit): Astro v7 readiness, migration
  contract extraction (source-audit), WordPress/HTML/Firecrawl Content Layer
  scaffolding (ingest), post-build parity verification (verify-build), and a
  24-page verified Astro v7.4 documentation library.
---

# `legends-astro`

Read [docs/AGENT-RECIPE.md](../../docs/AGENT-RECIPE.md) and [wiki/index.md](../../wiki/index.md).

## Core commands

```sh
# Readiness and optional live npm / GitHub docs freshness check:
python -m legends_astro doctor --project <consumer-root> [--live]

# Search 7 operational runbooks and 24 vendored official Astro v7.4 MDX pages:
python -m legends_astro docs-lookup "<topic>"
python -m legends_astro docs-lookup --live-locations

# Pre-migration contract extraction (HTML, WordPress XML/JSON, Firecrawl JSON):
python -m legends_astro source-audit --source <source-path> --output <project>/migration-contract.json

# Scaffold Astro v7 Content Layer (src/content.config.ts + glob() + astro/zod):
python -m legends_astro ingest --source <source-path> --output <project> [--apply]

# Build with project-local Astro (telemetry disabled) and prove parity:
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
python -m legends_astro verify-build --dist <project>/dist --project <project> [--contract <project>/migration-contract.json]
```

## Runbooks

- [wiki/inspect-setup.md](../../wiki/inspect-setup.md)
- [wiki/pages-components-styles-scripts.md](../../wiki/pages-components-styles-scripts.md)
- [wiki/content-collections.md](../../wiki/content-collections.md)
- [wiki/migrate-and-wordpress.md](../../wiki/migrate-and-wordpress.md)
- [wiki/performance-and-caching.md](../../wiki/performance-and-caching.md)
- [wiki/diagnose-and-upgrade.md](../../wiki/diagnose-and-upgrade.md)
- [wiki/latest-docs-and-ecosystem.md](../../wiki/latest-docs-and-ecosystem.md)
- [wiki/sources.md](../../wiki/sources.md)
