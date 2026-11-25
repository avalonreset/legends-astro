"""Hermetic tests for multi-framework static repo audit, WAF/SPA Firecrawl fallback, vendored migration guides, and doctor ecosystem."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from legends_astro.auditor import audit_source, verify_build
from legends_astro.capture import capture_site, detect_empty_spa_shell, detect_waf_challenge
from legends_astro.cli import main as cli_main
from legends_astro.docs_nav import load_vendor_manifest, lookup_docs

MODULE_ROOT = Path(__file__).resolve().parent.parent


def _write(base: Path, rel: str, content: str) -> Path:
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class TestMultiFrameworkAudit(unittest.TestCase):
    def test_nextjs_hybrid_app_and_pages_router_full_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "next-app"
            _write(
                repo,
                "package.json",
                json.dumps(
                    {
                        "name": "acme-next",
                        "dependencies": {"next": "15.0.0", "react": "19.0.0", "react-dom": "19.0.0"},
                        "devDependencies": {"tailwindcss": "^4.0.0"},
                    }
                ),
            )
            _write(
                repo,
                "next.config.mjs",
                """export default {
  async redirects() {
    return [
      { source: '/legacy-blog', destination: '/blog', permanent: true }
    ];
  }
};
""",
            )
            _write(
                repo,
                "vercel.json",
                json.dumps(
                    {
                        "redirects": [
                            {"source": "/old-pricing", "destination": "/pricing", "statusCode": 301}
                        ]
                    }
                ),
            )
            # App Router routes + API route + route group
            _write(
                repo,
                "src/app/(marketing)/page.tsx",
                """import Image from 'next/image';
import Link from 'next/link';
export default function Home() {
  const api = process.env.NEXT_PUBLIC_API_URL;
  return <main><Image src="/hero.png" alt="Hero" /><Link href="/pricing">Pricing</Link>{api}</main>;
}
""",
            )
            _write(repo, "src/app/blog/[slug]/page.tsx", "export default function Post() { return <article />; }\n")
            _write(repo, "src/app/docs/[...path]/page.tsx", "export default function Docs() { return <section />; }\n")
            _write(repo, "src/app/api/health/route.ts", "export async function GET() { return Response.json({ ok: true }); }\n")
            # Pages Router route + API route (makes it hybrid)
            _write(
                repo,
                "src/pages/pricing.tsx",
                "import Head from 'next/head';\nexport default function Pricing() { return <><Head><title>Pricing</title></Head><h1>Pricing</h1></>; }\n",
            )
            _write(repo, "src/pages/api/webhook.ts", "export default function handler(req, res) { res.end('ok'); }\n")
            # Interactive island vs static component
            _write(
                repo,
                "src/components/SearchBar.tsx",
                "'use client';\nimport { useState } from 'react';\nimport { useRouter } from 'next/navigation';\nexport function SearchBar() { const [q, setQ] = useState(''); return <input value={q} onChange={e => setQ(e.target.value)} />; }\n",
            )
            _write(
                repo,
                "src/components/HeroCard.tsx",
                "export function HeroCard({ title }: { title: string }) { return <section><h2>{title}</h2></section>; }\n",
            )
            # Env template vs real secret .env (real .env must NEVER be inspected)
            _write(repo, ".env.example", "NEXT_PUBLIC_SITE_NAME=Acme\n")
            _write(repo, ".env", "NEXT_PUBLIC_SECRET_DO_NOT_READ=supersecret\n")
            # Content collection
            _write(repo, "content/posts/launch.mdx", "---\ntitle: Launch\ndate: 2026-11-24\ntags: [astro]\n---\n# Launch\n")
            # Ignored build artifacts that must be skipped
            _write(repo, ".next/server/app/fake/page.tsx", "export default function Ignored() { return null; }\n")
            _write(repo, "node_modules/pkg/index.tsx", "export const x = process.env.NEXT_PUBLIC_SHOULD_SKIP;\n")

            contract = audit_source(repo)
            self.assertEqual(contract["sourceKind"], "framework-nextjs")
            fm = contract["frameworkMigration"]
            self.assertEqual(fm["framework"], "nextjs")
            self.assertEqual(fm["routerKind"], "hybrid")
            self.assertIn("react", fm["uiFrameworks"])
            self.assertEqual(fm["outputRecommendation"], "server")
            for integ in ("@astrojs/react", "@astrojs/mdx", "@astrojs/sitemap", "@astrojs/node", "@astrojs/cloudflare"):
                self.assertIn(integ, fm["recommendedIntegrations"])

            routes = {r["route"]: r for r in fm["routes"]}
            self.assertIn("/", routes)
            self.assertIn("/pricing", routes)
            self.assertIn("/blog/[slug]", routes)
            self.assertEqual(routes["/blog/[slug]"]["kind"], "dynamic")
            self.assertEqual(routes["/blog/[slug]"]["params"], ["slug"])
            self.assertIn("/docs/[...path]", routes)
            self.assertEqual(routes["/docs/[...path]"]["kind"], "catchall")
            self.assertNotIn("/fake", routes)

            apis = {a["route"] for a in fm["apiRoutes"]}
            self.assertIn("/api/health", apis)
            self.assertIn("/api/webhook", apis)

            redir_pairs = {(r["source"], r["destination"], r["originFile"]) for r in fm["redirects"]}
            self.assertIn(("/old-pricing", "/pricing", "vercel.json"), redir_pairs)
            self.assertIn(("/legacy-blog", "/blog", "next.config.mjs"), redir_pairs)

            interactive_files = {i["file"] for i in fm["islands"]["interactiveIslands"]}
            static_files = {s["file"] for s in fm["islands"]["staticCandidateComponents"]}
            self.assertIn("src/components/SearchBar.tsx", interactive_files)
            self.assertIn("src/components/HeroCard.tsx", static_files)

            env_map = {e["name"]: e["suggestedAstroName"] for e in fm["envVars"]}
            self.assertEqual(env_map.get("NEXT_PUBLIC_API_URL"), "PUBLIC_API_URL")
            self.assertEqual(env_map.get("NEXT_PUBLIC_SITE_NAME"), "PUBLIC_SITE_NAME")
            self.assertNotIn("NEXT_PUBLIC_SECRET_DO_NOT_READ", env_map)
            self.assertNotIn("NEXT_PUBLIC_SHOULD_SKIP", env_map)

            traps = fm["frameworkImportsAndTraps"]
            for expected_trap in ("next/image", "next/link", "next/head", "next/navigation", "tailwind"):
                self.assertIn(expected_trap, traps)

            self.assertTrue(any(c["name"] == "posts" and "title" in c["sampleFrontmatterKeys"] for c in fm["contentCollections"]))
            self.assertIn("migrate-from-nextjs", fm["recommendedDocs"])

            # Ensure verify_build skips dynamic template routes while verifying static routes
            dist = Path(tmp) / "dist"
            _write(dist, "index.html", "<!doctype html><html><head><title>Home</title></head><body><h1>Home</h1></body></html>")
            _write(dist, "pricing/index.html", "<!doctype html><html><head><title>Pricing</title></head><body><h1>Pricing</h1></body></html>")
            cpath = Path(tmp) / "contract.json"
            cpath.write_text(json.dumps(contract), encoding="utf-8")
            ver = verify_build(dist, contract_path=cpath)
            self.assertTrue(ver["ok"], f"Unexpected verify_build failures: {ver['failures']}")

    def test_nuxt_gatsby_vite_cra_docusaurus_hugo_jekyll_eleventy(self) -> None:
        manifest_ids = {f["id"] for f in load_vendor_manifest(MODULE_ROOT)["files"]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Nuxt
            nuxt = root / "nuxt-repo"
            _write(nuxt, "package.json", json.dumps({"dependencies": {"nuxt": "^3.14.0", "vue": "^3.5.0"}}))
            _write(nuxt, "nuxt.config.ts", "export default defineNuxtConfig({ routeRules: { '/old': { redirect: '/new' } } });\n")
            _write(nuxt, "pages/index.vue", "<template><h1>Home</h1></template>\n")
            _write(nuxt, "pages/posts/[id].vue", "<script setup>\nconst { data } = await useFetch('/api/posts');\nconst cfg = process.env.NUXT_PUBLIC_BASE;\n</script>\n<template><div /></template>\n")
            _write(nuxt, "components/Counter.vue", "<script setup>\nimport { ref } from 'vue';\nconst n = ref(0);\n</script>\n<template><button @click='n++'>{{ n }}</button></template>\n")
            _write(nuxt, "components/Badge.vue", "<template><span>Static</span></template>\n")
            _write(nuxt, "server/api/posts.get.ts", "export default defineEventHandler(() => []);\n")

            c_nuxt = audit_source(nuxt)
            fm = c_nuxt["frameworkMigration"]
            self.assertEqual(fm["framework"], "nuxt")
            self.assertIn("vue", fm["uiFrameworks"])
            self.assertIn("@astrojs/vue", fm["recommendedIntegrations"])
            self.assertEqual(fm["outputRecommendation"], "server")
            self.assertTrue(any(a["route"] == "/api/posts" for a in fm["apiRoutes"]))
            self.assertTrue(any(r["source"] == "/old" and r["destination"] == "/new" for r in fm["redirects"]))
            self.assertTrue(any(i["file"] == "components/Counter.vue" for i in fm["islands"]["interactiveIslands"]))
            self.assertTrue(any(s["file"] == "components/Badge.vue" for s in fm["islands"]["staticCandidateComponents"]))
            self.assertTrue(any(e["suggestedAstroName"] == "PUBLIC_BASE" for e in fm["envVars"]))
            for doc_id in fm["recommendedDocs"]:
                self.assertIn(doc_id, manifest_ids)

            # 2. Gatsby
            gatsby = root / "gatsby-repo"
            _write(gatsby, "package.json", json.dumps({"dependencies": {"gatsby": "^5.13.0", "react": "^18.3.0"}}))
            _write(gatsby, "gatsby-config.js", "module.exports = { siteMetadata: { title: 'Gatsby' } };\n")
            _write(gatsby, "src/pages/index.js", "import { GatsbyImage } from 'gatsby-plugin-image';\nconst k = process.env.GATSBY_API_KEY;\nexport default () => <div />;\n")
            _write(gatsby, "content/blog/first.md", "---\ntitle: First Post\nslug: /blog/first/\n---\nHello\n")
            c_gatsby = audit_source(gatsby)
            fm = c_gatsby["frameworkMigration"]
            self.assertEqual(fm["framework"], "gatsby")
            self.assertIn("gatsby-plugin-image", fm["frameworkImportsAndTraps"])
            self.assertTrue(any(e["suggestedAstroName"] == "PUBLIC_API_KEY" for e in fm["envVars"]))
            self.assertIn("migrate-from-gatsby", fm["recommendedDocs"])

            # 3. Vite React SPA
            vite = root / "vite-repo"
            _write(vite, "package.json", json.dumps({"dependencies": {"react": "^19.0.0"}, "devDependencies": {"vite": "^6.0.0"}}))
            _write(vite, "vite.config.ts", "export default {};\n")
            _write(vite, "index.html", '<!doctype html><html><body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>')
            _write(vite, "src/App.tsx", "import { Route } from 'react-router-dom';\nconst url = import.meta.env.VITE_ENDPOINT;\nexport function App() { return <Route path=\"/about\" />; }\n")
            c_vite = audit_source(vite)
            fm = c_vite["frameworkMigration"]
            self.assertEqual(fm["framework"], "vite")
            self.assertIn("react", fm["uiFrameworks"])
            self.assertTrue(any(r["route"] == "/about" for r in fm["routes"]))
            self.assertTrue(any(e["suggestedAstroName"] == "PUBLIC_ENDPOINT" for e in fm["envVars"]))
            self.assertIn("spa-router", fm["frameworkImportsAndTraps"])

            # 4. Create React App
            cra = root / "cra-repo"
            _write(cra, "package.json", json.dumps({"dependencies": {"react": "^18.2.0", "react-scripts": "5.0.1"}}))
            _write(cra, "public/index.html", '<!doctype html><html><body><div id="root"></div></body></html>')
            _write(cra, "src/App.jsx", "export default function App() { return <div>{process.env.REACT_APP_TITLE}</div>; }\n")
            c_cra = audit_source(cra)
            self.assertEqual(c_cra["frameworkMigration"]["framework"], "create-react-app")
            self.assertTrue(any(e["suggestedAstroName"] == "PUBLIC_TITLE" for e in c_cra["frameworkMigration"]["envVars"]))
            self.assertIn("migrate-from-create-react-app", c_cra["frameworkMigration"]["recommendedDocs"])

            # 5. Docusaurus
            doku = root / "docusaurus-repo"
            _write(doku, "package.json", json.dumps({"dependencies": {"@docusaurus/core": "^3.5.0", "react": "^18.0.0"}}))
            _write(doku, "docusaurus.config.ts", "export default { title: 'Docs' };\n")
            _write(doku, "docs/getting-started.mdx", "---\ntitle: Getting Started\nsidebar_position: 1\n---\n# Start\n")
            _write(doku, "blog/2026-10-01-release.md", "---\ntitle: Release\nauthors: [alex]\n---\nHello blog\n")
            c_doku = audit_source(doku)
            fm = c_doku["frameworkMigration"]
            self.assertEqual(fm["framework"], "docusaurus")
            routes = {r["route"] for r in fm["routes"]}
            self.assertIn("/docs/getting-started", routes)
            self.assertIn("/blog/release", routes)
            self.assertIn("migrate-from-docusaurus", fm["recommendedDocs"])

            # 6. Hugo
            hugo = root / "hugo-repo"
            _write(hugo, "hugo.toml", 'baseURL = "https://hugo.example/"\ntitle = "My Hugo"\n')
            _write(hugo, "layouts/_default/single.html", "<html><body>{{ .Content }}</body></html>\n")
            _write(hugo, "content/posts/hello.md", '+++\ntitle = "Hello Hugo"\ndate = 2026-10-01\ndraft = false\n+++\nCheck {{< youtube abc123 >}}.\n')
            _write(hugo, "content/posts/secret.md", '+++\ntitle = "Draft Post"\ndraft = true\n+++\nSecret\n')
            _write(hugo, "static/_redirects", "/old-hugo /posts/hello/ 301\n")
            c_hugo = audit_source(hugo)
            fm = c_hugo["frameworkMigration"]
            self.assertEqual(fm["framework"], "hugo")
            self.assertEqual(c_hugo["summary"]["draftCount"], 1)
            self.assertIn("hugo-shortcodes", fm["frameworkImportsAndTraps"])
            self.assertTrue(any(r["source"] == "/old-hugo" for r in fm["redirects"]))
            self.assertIn("migrate-from-hugo", fm["recommendedDocs"])

            # 7. Jekyll
            jekyll = root / "jekyll-repo"
            _write(jekyll, "_config.yml", "title: Jekyll Blog\n")
            _write(jekyll, "_layouts/post.html", "<html><body>{% include nav.html %}{{ content }}</body></html>\n")
            _write(jekyll, "_posts/2026-11-01-launch-day.md", "---\ntitle: Launch Day\nlayout: post\n---\nHello Jekyll\n")
            c_jekyll = audit_source(jekyll)
            fm = c_jekyll["frameworkMigration"]
            self.assertEqual(fm["framework"], "jekyll")
            self.assertTrue(any(r["route"] == "/2026/11/01/launch-day/" for r in fm["routes"]))
            self.assertIn("jekyll-liquid", fm["frameworkImportsAndTraps"])
            self.assertIn("migrate-from-jekyll", fm["recommendedDocs"])

            # 8. Eleventy + netlify.toml
            el = root / "eleventy-repo"
            _write(el, ".eleventy.js", "module.exports = function() { return {}; };\n")
            _write(el, "index.md", "---\ntitle: 11ty Home\n---\nWelcome\n")
            _write(el, "netlify.toml", '[[redirects]]\nfrom = "/blog/*"\nto = "/posts/:splat"\nstatus = 301\n')
            c_el = audit_source(el)
            fm = c_el["frameworkMigration"]
            self.assertEqual(fm["framework"], "eleventy")
            self.assertTrue(any(r["source"] == "/blog/*" and r["originFile"] == "netlify.toml" for r in fm["redirects"]))
            self.assertIn("migrate-from-eleventy", fm["recommendedDocs"])


class TestFirecrawlFallbackAndEcosystem(unittest.TestCase):
    def test_waf_403_cloudflare_challenge_and_empty_spa_shell_fallback(self) -> None:
        pub_resolver = lambda _host, _port: ["93.184.216.34"]

        # 1. WAF 403 + cf-mitigated challenge triggers firecrawlFallback
        def waf_transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return (
                403,
                {"Content-Type": "text/html", "cf-mitigated": "challenge"},
                b"<!doctype html><html><head><title>Just a moment...</title></head><body>Enable JavaScript and cookies to continue</body></html>",
            )

        with tempfile.TemporaryDirectory() as tmp:
            res = capture_site(
                output_dir=tmp,
                url="https://protected.example/",
                resolver=pub_resolver,
                transport=waf_transport,
            )
            fb = res["firecrawlFallback"]
            self.assertTrue(fb["recommended"])
            self.assertIn("waf_or_bot_challenge", fb["reasons"])
            self.assertTrue(any("legends-firecrawl" in cmd for cmd in fb["suggestedCommands"]))
            self.assertTrue(any("--from-firecrawl" in cmd for cmd in fb["suggestedCommands"]))
            self.assertEqual(fb["affectedUrls"][0]["status"], 403)

        # 2. Empty JS SPA shell triggers firecrawlFallback
        def spa_transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return (
                200,
                {"Content-Type": "text/html"},
                b'<!doctype html><html><head><title>React App</title><script type="module" src="/assets/index-abc.js"></script></head><body><div id="root"></div><noscript>Enable JS</noscript></body></html>',
            )

        with tempfile.TemporaryDirectory() as tmp:
            res = capture_site(
                output_dir=tmp,
                url="https://spa.example/",
                resolver=pub_resolver,
                transport=spa_transport,
            )
            fb = res["firecrawlFallback"]
            self.assertTrue(fb["recommended"])
            self.assertIn("empty_js_spa_shell", fb["reasons"])
            self.assertIn("#root", fb["affectedUrls"][0]["detail"])

        # 3. Clean SSR page does NOT recommend fallback
        def clean_transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return (
                200,
                {"Content-Type": "text/html"},
                b"<!doctype html><html><head><title>SSR Site</title></head><body><main><h1>Welcome</h1><p>"
                + b"Real server rendered content that has plenty of visible words for visitors and search crawlers alike. " * 3
                + b"</p></main></body></html>",
            )

        with tempfile.TemporaryDirectory() as tmp:
            res = capture_site(
                output_dir=tmp,
                url="https://clean.example/",
                resolver=pub_resolver,
                transport=clean_transport,
            )
            self.assertFalse(res["firecrawlFallback"]["recommended"])
            self.assertEqual(res["firecrawlFallback"]["reasons"], [])

        # Unit checks on helper detectors
        self.assertTrue(detect_waf_challenge(503, {}, "Attention Required! | Cloudflare"))
        self.assertIsNotNone(detect_empty_spa_shell('<html><body><div id="__next"></div><script src="/_next/app.js"></script></body></html>'))

    def test_docs_lookup_finds_all_vendored_framework_migration_guides(self) -> None:
        expected_queries = [
            ("gatsby", "migrate-from-gatsby"),
            ("nuxt", "migrate-from-nuxtjs"),
            ("create react app", "migrate-from-create-react-app"),
            ("hugo", "migrate-from-hugo"),
            ("jekyll", "migrate-from-jekyll"),
            ("eleventy", "migrate-from-eleventy"),
            ("docusaurus", "migrate-from-docusaurus"),
            ("sveltekit", "migrate-from-sveltekit"),
            ("vuepress", "migrate-from-vuepress"),
            ("gitbook", "migrate-from-gitbook"),
            ("gridsome", "migrate-from-gridsome"),
            ("pelican", "migrate-from-pelican"),
        ]
        for query, expected_id in expected_queries:
            res = lookup_docs(query=query, module_root=MODULE_ROOT)
            self.assertEqual(res["vendoredPageCount"], 53)
            ids = [m["id"] for m in res["matches"]]
            self.assertIn(expected_id, ids, f"Query {query!r} did not return {expected_id} in {ids}")

    def test_doctor_reports_non_blocking_ecosystem_metadata(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["doctor", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("ecosystem", payload)
        eco = payload["ecosystem"]
        self.assertTrue(eco["optional"])
        self.assertIsInstance(eco["legendsFirecrawlDetected"], bool)
        self.assertIsInstance(eco["playwrightAvailable"], bool)
        self.assertTrue(eco["legendsFirecrawlHint"])
        self.assertTrue(eco["playwrightHint"])


if __name__ == "__main__":
    unittest.main()
