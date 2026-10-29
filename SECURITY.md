# Security

Do not include credentials, private domain inventories, or proprietary site exports in public issues. Report suspected vulnerabilities privately through the repository's GitHub security reporting feature when available; otherwise contact the maintainer through a private channel.

## Supported versions

| Version | Supported |
|---|---|
| `0.2.1` | Yes |
| `< 0.2.1` | No |

## Response timeline

- Initial acknowledgment target: within 72 hours of a private report.
- Triage and remediation plan target: within 7 days for confirmed security issues in the supported `0.2.x` release line.

## Execution boundaries

`legends-astro` is an offline-first inspection, migration contract extraction, and build parity verification toolkit. Its default commands (`doctor`, `source-audit`, `ingest` preview, `verify-build`, `docs-lookup`, `knowledge-pack`) do not execute consumer `astro.config.*` files, do not install npm packages, and do not make network requests.

Optional live version drift checks (`doctor --live` and `docs-lookup --check-live`) query only the public npm registry (`registry.npmjs.org`) and GitHub API (`api.github.com/repos/withastro/docs/commits/main`).

File writes in `ingest` require the explicit `--apply` flag. Reported compatibility and test coverage are documented in `docs/RELEASE-READINESS.md`.
