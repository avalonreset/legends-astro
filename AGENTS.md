# Agent instructions

Read `docs/AGENT-RECIPE.md`, then `wiki/index.md` and the smallest matching runbook.
Run offline `doctor` before commands (`legends-astro resources --json` and `legends-astro knowledge-pack --check --json` are also available offline) and `doctor --live` when checking upstream Astro version or documentation freshness.

- This module is `legends-astro`. The earlier `legends-astro-kit` directory is preserved as a legacy compatibility alias.
- Preserve the one-skill router (`cto-legends`). Load module instructions on demand as Markdown; do not register a new per-module host skill.
- Always invoke project-local Astro via `node ./node_modules/astro/bin/astro.mjs` with `ASTRO_TELEMETRY_DISABLED=1`. Never run global `astro` (which collides with Windows `winget` `Astronomer.Astro` Airflow CLI).
- Before migrating any existing HTML, WordPress, or `legends-firecrawl` site to Astro, run `legends-astro source-audit` to record `migration-contract.json`.
- Preview Content Layer scaffolding with `legends-astro ingest --source <path> --output <dir>`, then pass `--apply --confirm-plan <planSha256>` (and `--overwrite` only when replacing existing files is authorized).
- After every `astro build`, run `legends-astro verify-build --dist <dist> --project <dir> [--contract migration-contract.json]` to prove route parity, SEO/JSON-LD preservation, DOM `id` ABI integrity, draft exclusion, and first-paint `<style is:inline>` order.
- Do not spawn detached background `astro dev` or `astro preview` daemons or pass `--open` from an AI coding agent (which can trigger Windows EDR local-portal heuristics); verify builds offline via `astro build` + `legends-astro verify-build`, and set `ASTRO_DEV_BACKGROUND=0` without `--open` only when a foreground dev server is explicitly requested.
- Keep user project outputs and receipts outside this managed module checkout.

## EDR and Security Guardrails

- Never spawn hidden consoles, execution-policy bypass shell wrappers, or detached background localhost daemons.
- Never read local browser profile databases, cookie stores, `Login Data`, or OS credential stores.
- Never print raw API keys, tokens, or secrets to stdout, stderr, or Markdown receipts.
