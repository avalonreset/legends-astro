# Publishing and content ops (Day-2 article to live page)

Cross-read: [agency-starter-guidelines.md](agency-starter-guidelines.md), [content-collections.md](content-collections.md), [migrate-and-wordpress.md](migrate-and-wordpress.md).
Vendored official docs: `vendor/astro-docs/src/content/docs/en/guides/cms/index.mdx`, `vendor/astro-docs/src/content/docs/en/guides/deploy/index.mdx`, `vendor/astro-docs/src/content/docs/en/guides/markdown-content.mdx`, `vendor/astro-docs/src/content/docs/en/guides/endpoints.mdx`.

Day-1 gets a site launched. Day-2 is where agencies lose search equity: a writer renames a slug without a redirect, an update resets `pubDate`, a draft leaks, or a template edit drops a canonical. This runbook gives two lanes that share one safety gate. Lane 1 is for AI agents and Git-native operators. Lane 2 is for non-technical clients and writers. Both end at the same `verify-build --baseline` check.

## Lane 1: AI-agent and Git CLI publishing workflow

Flow: raw Markdown article to `legends-astro post` to `astro build` to `legends-astro verify-build --baseline` to Git commit and push to auto-deploy.

### 1.1 Inventory what exists (--list)

Start read-only. `--list` writes nothing and needs no plan confirmation.

```sh
legends-astro post --project ./client-site --collection blog --list
```

Output includes `id`, `slug`, `file`, `title`, `description`, `pubDate`, `updatedDate`, `draft`, `heroImage`, and `wordCount` per entry. Use it to confirm slugs before an update or rename and to spot drafts or future-dated scheduled posts. `--collection` defaults to `blog`. `--content-dir <relpath>` overrides the folder when a project stores posts outside `src/content/<collection>/`. The publisher resolves custom `glob({ base: ... })` folders and field aliases by reading `src/content.config.ts` (or `.mjs` / `.js`) statically. It never executes JS or TS. Use repeatable `--field-map role=customKey` only when auto-detection picks the wrong field.

### 1.2 Create a post (--input or --title + --body-file)

Preview first, then apply with the returned `planSha256`. Nothing is written until `--apply --confirm-plan <planSha256>` matches.

```sh
legends-astro post --project ./client-site --input ./draft-article.md --hero ./cover.png --hero-alt "Front desk team reviewing intake forms" --publish
legends-astro post --project ./client-site --input ./draft-article.md --hero ./cover.png --hero-alt "Front desk team reviewing intake forms" --publish --apply --confirm-plan <planSha256>
```

- Input: `--input <file.md>` (Markdown with optional YAML frontmatter) or `--title <str>` plus `--body-file <file.md>`. Explicit CLI flags override or fill frontmatter fields.
- Metadata flags: `--slug`, `--description`, `--author`, `--tags` and `--categories` (comma-separated or repeatable), `--pub-date <YYYY-MM-DD|ISO-8601>` (defaults to today in UTC on create), `--hero`, `--hero-alt`.
- Visibility: `--draft` or `--publish`, never both. Without an explicit flag the input frontmatter `draft` value is respected; in create mode, if `--publish`, `--draft`, and input frontmatter `draft` are all omitted, `post` defaults safely to `draft: true`. Drafts and future `pubDate` posts stay out of production via `isPublished`.
- `--strip-body-h1` removes a leading `# Heading` when the layout already renders `<h1>`.
- Slugs are strictly validated: lowercase ASCII letters, digits, and single hyphens only, with traversal and Windows reserved names refused.
- If the target slug file already exists, create mode refuses unless `--overwrite` is passed with `--apply`.

### 1.3 Image safety (hero and inline)

`--hero` and local inline Markdown images `![alt](path)` are localized into `public/images/<collection>/<slug>/` (e.g. `public/images/blog/<slug>/`) and frontmatter plus body links are rewritten. Remote `https://` images and paths already under `public/` are left alone. Every local image must pass all of these checks or the plan fails:

- Symlink-chain and path-containment refusal plus a 25 MB size cap.
- Extension allowlist: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.avif`, `.svg`.
- Magic-byte verification matching the extension, so renamed HTML or executables cannot land in `public/`.
- SVG XSS refusal: script elements, inline event handlers, `javascript:` URLs, and entity or doctype tricks are rejected.
- Filenames are sanitized before writing. Always supply meaningful `--hero-alt` and inline alt text. Empty alt text on a hero is a blocking lint error.

### 1.4 Editorial SEO preflight lint

Every preview returns `lint: { ok, errors, warnings }`. Blocking errors include a missing title, missing hero alt text, empty inline image alt text, broken local image references, leftover placeholders (`TBD`, `TODO`, `FIXME`, `[placeholder]`, lorem ipsum, `INSERT_...`), and frontmatter control characters. Advisory warnings include title length outside 30 to 65 characters, description missing or outside 110 to 160 characters, and a duplicate body H1 when the layout renders one. `--strict-typography` adds warnings for em-dash and en-dash characters in title, description, and body. Fix blocking errors before applying. Do not train writers to ignore the lint.

### 1.5 Update without breaking SEO (--update)

```sh
legends-astro post --project ./client-site --slug launch-notes --update --input ./revised-article.md
legends-astro post --project ./client-site --slug launch-notes --update --input ./revised-article.md --apply --confirm-plan <planSha256>
```

- The target post must exist or the command fails closed.
- Original `pubDate` is preserved unless `--pub-date` is explicitly supplied. Search engines keep the first-publish date.
- `updatedDate` is auto-stamped to today in UTC unless `--updated-date` is given or `--no-updated-date` is passed.
- Unknown custom frontmatter keys on consumer projects are preserved. Nothing outside the planned merge is deleted.
- Frontmatter is serialized with safe JSON-quoted scalars so quotes and colons in titles cannot inject YAML structure.

### 1.6 Rename a slug safely (--rename-from)

```sh
legends-astro post --project ./client-site --rename-from welcome --slug launch-notes
legends-astro post --project ./client-site --rename-from welcome --slug launch-notes --apply --confirm-plan <planSha256>
```

The rename creates the new file, removes the old file, stamps `updatedDate`, and appends a deduplicated `301` rule to `public/_redirects` in one confirmed plan. Route shape respects the project `trailingSlash` setting read statically from `astro.config.*`. Existing redirect chains are flattened so an older rule pointing at the old route is repointed at the new route (no 2-hop chains), and cycles or self-loops are refused. The old slug must exist and the new slug must not, unless `--overwrite` is explicitly authorized. Never rename a file by hand without the redirect. That is the fastest way to burn inbound links and rankings.

### 1.7 Build and regression-gate every change

```sh
set ASTRO_TELEMETRY_DISABLED=1
node ./node_modules/astro/bin/astro.mjs build
legends-astro verify-build --dist ./dist --project . --baseline site-manifest.json --write-manifest site-manifest.json --overwrite --env production
```

- First production build: run with `--write-manifest site-manifest.json --env production` (plus `--overwrite` only when replacing) and commit the manifest. The manifest schema is `legends.astro-site-manifest/v1` and records routes, titles, descriptions, canonicals, H1 counts, JSON-LD types, sitemaps, redirects, and asset counts.
- Every later build: pass `--baseline site-manifest.json`. The gate fails closed on `baseline_route_missing` (unless a valid `301` / `302` / `308` in `_redirects` resolves to a live route or an external `https://` target), `baseline_canonical_lost`, `baseline_canonical_changed` (unless `--allow-canonical-change` is explicitly authorized), `baseline_title_lost`, `baseline_description_lost`, `baseline_h1_lost`, and `baseline_jsonld_type_lost`. It also emits `baselineDiff` with added, removed, and redirected routes plus asset count delta.
- A manifest is never written or overwritten when verification fails (`ok == False`). A broken build cannot bless itself as the new baseline.
- All standard checks still run: draft exclusion, broken links and CSS assets, SRI, sitemaps, `_redirects` targets and loops, `--forbid-host`, and `--env staging|production` locks.

Commit the post, localized images, `public/_redirects`, and the refreshed `site-manifest.json` together, then push.

### 1.8 Deploy and scheduled posts

Push-to-deploy works on any static host. Cloudflare Pages and Netlify honor `public/_redirects` and `public/_headers` natively. Vercel needs redirect rules translated to its platform config if you rely on renames. Coolify on Hetzner (or any Nginx or Caddy static origin) must be configured to honor `_redirects` semantics or equivalent rewrite rules. Verify redirect behavior on the real host after the first rename, not just in `dist/`.

Future-dated posts (`pubDate` ahead of build time) are excluded by `isPublished` until a rebuild runs on or after that date. Schedule a daily rebuild trigger (host build hook, scheduled CI workflow, or cron-driven deploy) so scheduled posts go live without a code change. Confirm the trigger rebuilds production, not a staging-locked branch.

## Lane 2: Client-editable Git-backed CMS

Not every writer should touch a terminal. Git-backed CMS editors keep zero lock-in: content stays as Markdown files in `src/content/blog/` inside the same Git repo, and the exact same build plus baseline gate protects production.

| Editor | How it writes | Best fit |
|---|---|---|
| Sveltia CMS | Browser admin UI commits Markdown and media to the repo via the Git host API | Drop-in Git CMS for blogs with collections, media library, and editorial workflow |
| Keystatic | Local or GitHub-backed admin UI writes structured Markdown or MDX files | Schema-driven editing when developers define fields in code |
| Pages CMS | Hosted editor commits content files to GitHub | Lightweight client editing without self-hosting an admin app |

Operating rules for Lane 2:

- Map CMS fields one-to-one to the `src/content.config.ts` schema (`title`, `description`, `pubDate`, `updatedDate`, `author`, `categories`, `tags`, `heroImage`, `heroAlt`, `canonicalUrl`, `draft`). Never let the CMS invent a second schema.
- Configure media uploads to land under `public/images/blog/` (or a documented `src/assets` convention) and require alt text in the CMS UI where supported.
- Writers get `draft` by default. Publishing is a deliberate field flip plus a production rebuild.
- Protect the production branch: require the CI job to run `astro build` then `legends-astro verify-build --dist ./dist --project . --baseline site-manifest.json --env production` before merge or deploy. A client typo that deletes a page or drops a canonical fails CI, not Google rankings.
- Slug renames inside a CMS do not create redirects automatically. Route renames through `legends-astro post --rename-from` (Lane 1) or add and review the `public/_redirects` rule manually in the same pull request.
- Because content is plain Markdown in Git, leaving the CMS is trivial. There is no database export and no proprietary content API to escape.

See the vendored overview at `vendor/astro-docs/src/content/docs/en/guides/cms/index.mdx` before choosing an editor.

## Cross-module synergies (optional pairings)

Each pairing is optional. `legends-astro` works standalone.

- `legends-dataforseo` for keyword and SERP research before writing: pick topics and search intent first, then publish via Lane 1 and gate with `--baseline`. Research informs the brief. It never edits the Astro repo.
- `legends-firecrawl` for migration rescue when a source site blocks plain HTTP or renders only via JavaScript: capture with Firecrawl, then `legends-astro capture --from-firecrawl <dir> --output <work-dir>` and proceed through `vendor-assets`, `scaffold`, and `verify-build --write-manifest`. Day-2 updates then use `post` plus `--baseline` as normal.
- `legends-coolify` and `legends-hetzner` for self-hosted deployment: provision the server, deploy the static build, and confirm `_redirects` and `_headers` behavior at the proxy before DNS cutover.
- `legends-cloudflare` for DNS cutover plus edge behavior: move DNS only after `verify-build --baseline --env production` passes on the candidate build, and confirm Cloudflare Pages (or the proxied origin) serves `public/_redirects` and `public/_headers` as expected.

## Day-2 acceptance checklist

- `legends-astro post --list` reviewed before any update or rename.
- Create, update, or rename applied only with a matching `--confirm-plan <planSha256>`.
- `pubDate` unchanged on updates, `updatedDate` stamped, redirect present for every rename.
- `astro build` passes and `verify-build --baseline site-manifest.json --env production` passes.
- Refreshed `site-manifest.json` committed with the content change.
- One live redirect and one canonical spot-checked on the deployed host.
