# Primary sources and version ledger

Observed and verified on **2026-10-24**.

## 1. Upstream package versions (npm registry)

| Package | Latest Version (2026-10-24) | Canary Baseline | Node Engines | License |
|---|---|---|---|---|
| `astro` | `7.4.2` | `7.3.1` (`92f23cc121d3a1a03c6fb901a519309662b333de`) | `>=22.23.4` (`7.4.2`) / `>=22.12.0` (`7.3.1`) | MIT (Fred K. Schott 2021) |
| `create-astro` | `5.3.1` | `5.2.4` | `>=22.12.0` | MIT |
| `@astrojs/check` | `0.9.12` | `0.9.10` | Peer `typescript ^5 \|\| ^6` | MIT |
| `@astrojs/mdx` | `8.1.1` | `8.x` | Astro v7 peer | MIT |
| `@astrojs/sitemap` | `3.7.4` | `3.7.x` | Astro v7 peer | MIT |
| `@astrojs/rss` | `4.0.19` | `4.0.x` | Astro v7 peer | MIT |
| `@astrojs/node` | `11.1.9` | `11.1.5` | Astro v7 peer | MIT |
| `@astrojs/upgrade` | `0.8.1` | `0.7.4` | `>=22.12.0` | MIT |

## 2. Vendored official documentation snapshot

- **Repository:** `https://github.com/withastro/docs`
- **Commit:** `0844aab04ea2f3859132ab97b1e9daa01ade76a1` (`2026-10-22T14:08:43Z`)
- **Prior foundation commit:** `43b55a036ed8942c6b76679c628c859d655c4293` (`2026-09-04T02:19:34Z`)
- **License:** MIT (`Copyright (c) 2022 withastro`), preserved in `vendor/astro-docs/LICENSE` and `vendor/astro-docs/NOTICE`.
- **Integrity manifest:** `vendor/astro-docs/manifest.json` (24 upstream MDX files + `LICENSE`, `NOTICE`, `README.md`, verified via `node scripts/verify-sources.mjs`).

## 3. Verified upstream live endpoints and negative probes (2026-10-24)

- `https://registry.npmjs.org/astro/latest` -> HTTP 200 (`version: "7.4.2"`).
- `https://api.github.com/repos/withastro/docs/commits/main` -> HTTP 200 (`0844aab04ea2f3859132ab97b1e9daa01ade76a1`).
- `https://mcp.docs.astro.build/mcp` -> Streamable HTTP MCP server (`Astro Docs server` `1.0.0`, single tool `search_astro_docs`).
- `https://docs.astro.build/llms.txt` and `https://docs.astro.build/llms-full.txt` -> HTTP 404 NotFound (verified `2026-10-24`; do not use in agent instructions).
