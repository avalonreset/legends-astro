# Contributing

Read `AGENTS.md` and `references/sources.md`. Keep one registered router skill (`skills/cto-legends/SKILL.md`), private evidence outside the repository, and Astro behavior claims tied to official sources in `vendor/astro-docs/`.

Run `python -m unittest discover -s tests -v`, `node ./scripts/verify-sources.mjs`, `node ./scripts/test-doctor.mjs`, and `python scripts/check_release.py`.

Tests must use synthetic HTML, WordPress XML/JSON, or Firecrawl fixtures and require no network credentials. Do not execute consumer `astro.config.*` files inside `doctor`.

Use plain language without em dashes. Keep existing attribution and the approved typography style.
