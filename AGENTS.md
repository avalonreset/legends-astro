# Agent instructions

Read `docs/AGENT-RECIPE.md`, then `wiki/index.md` and the smallest matching runbook.
Run offline `doctor` before commands and `doctor --live` when checking upstream Astro version or documentation freshness.

- This module is `legends-astro`. The earlier `legends-astro-kit` directory is preserved as a legacy compatibility alias.
- Preserve the one-skill router (`cto-legends`). Load module instructions on demand as Markdown; do not register a new per-module host skill.
- Always invoke project-local Astro via `node ./node_modules/astro/bin/astro.mjs` with `ASTRO_TELEMETRY_DISABLED=1`. Never run global `astro` (which collides with Windows `winget` `Astronomer.Astro` Airflow CLI).
- Before migrating any existing HTML, WordPress, or `legends-firecrawl` site to Astro, run `legends-astro source-audit` to record `migration-contract.json`.
- After every `astro build`, run `legends-astro verify-build --dist <dist> --project <dir> [--contract migration-contract.json]` to prove route parity, SEO/JSON-LD preservation, DOM `id` ABI integrity, draft exclusion, and first-paint `<style is:inline>` order.
- Keep user project outputs and receipts outside this managed module checkout.
