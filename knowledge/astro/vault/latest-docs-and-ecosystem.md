# Latest Astro documentation and ecosystem lookup

Cross-read: [index.md](index.md), [inspect-setup.md](inspect-setup.md), [diagnose-and-upgrade.md](diagnose-and-upgrade.md).

## 1. Local snapshot first (24 official Astro v7.4 MDX pages on disk)

Before browsing the web, search the local verified snapshot (`withastro/docs` commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1`, retrieved `2026-10-24`, covering `astro@7.4.2` and `astro@7.3.1`):

```sh
python -m legends_astro docs-lookup "wordpress"
python -m legends_astro docs-lookup "content collections"
python -m legends_astro docs-lookup --list
```

Verify snapshot hashes offline at any time:

```sh
node <legends-astro-root>/scripts/verify-sources.mjs --skip-self-test
```

## 2. Where agents should check for live / post-snapshot Astro updates

When a user asks about a brand-new release, an unvendored third-party CMS/adapter, or whether a newer Astro version is out, use these **verified upstream endpoints**:

### A. Live npm Registry JSON API (fastest, zero HTML noise)

Run `legends-astro doctor --live` or `legends-astro docs-lookup --check-live`, or query the npm registry directly:

```sh
npm view astro version dist-tags.latest engines
npm view create-astro version
npm view @astrojs/check version
npm view @astrojs/mdx version
npm view @astrojs/node version
npm view @astrojs/sitemap version
npm view @astrojs/rss version
npm view @astrojs/upgrade version
```

Direct HTTPS JSON endpoint (no npm CLI needed):
- `https://registry.npmjs.org/astro/latest` (returns exact `version`, `engines`, `dist.shasum`, `dist.integrity`)

### B. Official `withastro/docs` GitHub repository (raw MDX source)

The rendered website `https://docs.astro.build/` always tracks the latest major version, but **does not serve `/llms.txt`, `/llms-full.txt`, or `.md` URL suffixes** (all return HTTP 404 as of `2026-10-24`).
Instead, fetch raw English MDX directly from `withastro/docs`:

1. Check latest docs commit:
   `https://api.github.com/repos/withastro/docs/commits/main`
2. List the full documentation file tree recursively:
   `https://api.github.com/repos/withastro/docs/git/trees/main?recursive=1`
3. Read any specific guide as clean raw MDX:
   `https://raw.githubusercontent.com/withastro/docs/main/src/content/docs/en/<path>.mdx`
   Examples of high-value paths in `withastro/docs`:
   - `src/content/docs/en/guides/migrate-to-astro/from-wordpress.mdx`
   - `src/content/docs/en/guides/cms/wordpress.mdx`
   - `src/content/docs/en/guides/migrate-to-astro/from-nextjs.mdx`
   - `src/content/docs/en/guides/integrations-guide/node.mdx`
   - `src/content/docs/en/guides/integrations-guide/cloudflare.mdx`
   - `src/content/docs/en/guides/server-islands.mdx`
   - `src/content/docs/en/guides/actions.mdx`
   - `src/content/docs/en/guides/environment-variables.mdx`

### C. Official `withastro/astro` release changelog

To inspect exact patch/minor changes between `7.3.1`, `7.4.2`, and newer releases:
- `https://raw.githubusercontent.com/withastro/astro/main/packages/astro/CHANGELOG.md`
- `https://api.github.com/repos/withastro/astro/releases/latest`

### D. Official Astro Docs MCP server (design-time lookup only)

Astro operates a public streamable HTTP MCP endpoint backed by kapa.ai over current documentation:
- Endpoint: `https://mcp.docs.astro.build/mcp`
- Tool exposed: `search_astro_docs` (`{ "query": "<search string>" }`)
- Rule: Use only for design-time research on unvendored topics. Never make a project build or daily workflow depend on live MCP availability.

## 3. Ecosystem handoffs within `cto-legends`

- **Scraping a live website before migrating to Astro:** Use `legends-firecrawl` to capture pages, HTML, and metadata with saved evidence, then pass the saved JSON to `legends-astro source-audit` and `legends-astro ingest`.
- **DNS, redirects, and edge caching for an Astro site:** Use `legends-cloudflare`.
- **Container deployment and PaaS operations:** Use `legends-coolify` and `legends-hetzner`.
