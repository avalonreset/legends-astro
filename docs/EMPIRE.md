# Empire knowledge-pack integration

`legends-astro` ships an attachable `legends.knowledge-pack/v1` knowledge pack at `knowledge/astro/pack.json` containing the Astro v7 operational runbooks, source ledger, and migration receipt template.

## Attach to an Empire vault

Preview attachment first:

```sh
cto-legends run legends-empire -- knowledge attach /path/to/legends-astro/knowledge/astro/pack.json --vault /path/to/vault --operation-id op-astro-001 --generated-at 2026-10-24T00:00:00Z
```

Apply when authorized using the `approved_plan_sha256` from the dry-run output:

```sh
cto-legends run legends-empire -- knowledge attach /path/to/legends-astro/knowledge/astro/pack.json --vault /path/to/vault --operation-id op-astro-001 --generated-at 2026-10-24T00:00:00Z --approved-plan-sha256 <SHA256> --apply
```

By default, the pack attaches under `wiki/library/legends-astro/astro` inside the target vault and registers in `.legends-empire/knowledge-packs.json` without overwriting user notes.

## Verify pack integrity standalone

```sh
legends-astro knowledge-pack --check
```
