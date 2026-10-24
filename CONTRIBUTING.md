# Contributing

Read `AGENTS.md`, `docs/AGENT-RECIPE.md`, and `references/sources.md`. Keep one registered router skill (`skills/cto-legends/SKILL.md`), private evidence outside the repository, and Astro domain claims tied to official `withastro/docs` sources.

## Development setup

Python 3.10 or newer and Node.js 22.12.0 or newer (LTS 24.15.0 tested).

```sh
git clone https://github.com/avalonreset/legends-astro.git
cd legends-astro
python -m pip install -e .
```

## Validation checks

Run all four verification suites before opening a pull request:

```sh
python -m unittest discover -s tests -v
node ./scripts/verify-sources.mjs
node ./scripts/test-doctor.mjs
python scripts/check_release.py
```

Add behavioral tests for new CLI checks, HTML-to-Markdown edge cases, route parity rules, and Astro v7 Content Layer guards. Tests must use synthetic fixtures and require no network credentials.

Use plain language without em dashes. Keep existing attribution and the approved typography style.
