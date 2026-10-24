"""Unit and integration tests for legends-astro."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from legends_astro import LEGACY_ALIAS, MODULE_NAME, __version__
from legends_astro.auditor import audit_source, ingest_source_to_astro, verify_build
from legends_astro.cli import main as cli_main
from legends_astro.docs_nav import lookup_docs, verify_knowledge_pack


MODULE_ROOT = Path(__file__).resolve().parent.parent


class TestLegendsAstro(unittest.TestCase):
    def test_identity_and_knowledge_pack(self) -> None:
        self.assertEqual(MODULE_NAME, "legends-astro")
        self.assertEqual(LEGACY_ALIAS, "legends-astro-kit")
        self.assertEqual(__version__, "0.2.0")
        pack = verify_knowledge_pack(MODULE_ROOT)
        self.assertTrue(pack["ok"], f"Knowledge pack errors: {pack.get('errors')}")
        self.assertGreaterEqual(pack["fileCount"], 10)

    def test_docs_lookup_and_live_locations(self) -> None:
        wp = lookup_docs(query="wordpress", include_live_locations=True, module_root=MODULE_ROOT)
        self.assertTrue(wp["ok"])
        self.assertEqual(wp["vendoredPageCount"], 24)
        ids = [m["id"] for m in wp["matches"]]
        self.assertIn("migrate-from-wordpress", ids)
        self.assertIn("cms-wordpress", ids)
        self.assertTrue(any(loc["id"] == "npm-registry-astro" for loc in wp["upstreamLocations"]))

    def test_html_source_audit_and_verify_build_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "app.js").write_text(
                'const btn = document.getElementById("buy-now");\n'
                'const box = document.querySelector("#hero-box");\n',
                encoding="utf-8",
            )
            (legacy / "index.html").write_text(
                '<!doctype html><html><head>'
                '<title>Acme Home</title>'
                '<meta name="description" content="Fast widgets">'
                '<link rel="canonical" href="https://acme.example/">'
                '<style data-first-paint>body{background:#000}</style>'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>'
                '</head><body>'
                '<div id="hero-box"><h1>Welcome</h1><button id="buy-now">Buy</button></div>'
                '<a href="/about.html">About</a>'
                '<script type="module" src="/app.js"></script>'
                '</body></html>',
                encoding="utf-8",
            )
            (legacy / "about.html").write_text(
                '<!doctype html><html><head><title>About Acme</title></head>'
                '<body><h1>About</h1></body></html>',
                encoding="utf-8",
            )

            contract = audit_source(legacy, base_url="https://acme.example")
            self.assertEqual(contract["summary"]["pageCount"], 2)
            self.assertIn("buy-now", contract["jsReferencedIds"])
            self.assertIn("hero-box", contract["jsReferencedIds"])
            self.assertEqual(contract["recommendedAstroConfig"]["build.format"], "file")

            contract_path = root / "migration-contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")

            # Passing dist build
            dist_ok = root / "dist_ok"
            dist_ok.mkdir()
            (dist_ok / "app.js").write_text("console.log(1);", encoding="utf-8")
            (dist_ok / "index.html").write_text(
                (legacy / "index.html").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (dist_ok / "about.html").write_text(
                (legacy / "about.html").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            res_ok = verify_build(dist_ok, contract_path=contract_path)
            self.assertTrue(res_ok["ok"], f"Unexpected failures: {res_ok['failures']}")

            # Regressed dist build: missing about.html, dropped #buy-now, lost JSON-LD, broken link
            dist_bad = root / "dist_bad"
            dist_bad.mkdir()
            (dist_bad / "index.html").write_text(
                '<!doctype html><html><head><title>Acme Home</title></head>'
                '<body><div id="hero-box"><a href="/missing-page.html">Dead</a></div></body></html>',
                encoding="utf-8",
            )
            res_bad = verify_build(dist_bad, contract_path=contract_path)
            self.assertFalse(res_bad["ok"])
            codes = {f["code"] for f in res_bad["failures"]}
            self.assertIn("contract_route_missing", codes)
            self.assertIn("contract_dom_id_missing", codes)
            self.assertIn("contract_jsonld_lost", codes)
            self.assertIn("contract_meta_description_lost", codes)
            self.assertIn("broken_internal_link", codes)

    def test_wordpress_xml_ingest_and_draft_leak_detection(self) -> None:
        wxr = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
    <item>
        <title>Launch Post</title>
        <link>https://blog.example.com/launch-post/</link>
        <wp:post_name>launch-post</wp:post_name>
        <wp:post_type>post</wp:post_type>
        <wp:status>publish</wp:status>
        <content:encoded><![CDATA[<h1>Launch Post</h1><p>Public launch content here.</p>]]></content:encoded>
        <wp:postmeta>
            <wp:meta_key>_yoast_wpseo_metadesc</wp:meta_key>
            <wp:meta_value>Official launch SEO description</wp:meta_value>
        </wp:postmeta>
    </item>
    <item>
        <title>Secret Draft Roadmap</title>
        <link>https://blog.example.com/secret-roadmap/</link>
        <wp:post_name>secret-roadmap</wp:post_name>
        <wp:post_type>post</wp:post_type>
        <wp:status>draft</wp:status>
        <content:encoded><![CDATA[<p>Confidential unreleased Q4 pricing phrase.</p>]]></content:encoded>
    </item>
</channel>
</rss>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_path = root / "wp.xml"
            xml_path.write_text(wxr, encoding="utf-8")
            project_dir = root / "astro-wp"

            preview = ingest_source_to_astro(xml_path, project_dir, apply=False)
            self.assertTrue(preview["ok"])
            self.assertFalse(preview["applied"])
            self.assertFalse((project_dir / "src" / "content.config.ts").exists())

            applied = ingest_source_to_astro(xml_path, project_dir, apply=True)
            self.assertTrue(applied["applied"])
            self.assertTrue((project_dir / "src" / "content.config.ts").is_file())
            self.assertTrue((project_dir / "src" / "lib" / "published.ts").is_file())
            self.assertTrue((project_dir / "src" / "content" / "pages" / "secret-roadmap.md").is_file())

            # Simulate a leaky build that accidentally emitted the draft route
            leaky_dist = project_dir / "dist"
            (leaky_dist / "launch-post").mkdir(parents=True)
            (leaky_dist / "launch-post" / "index.html").write_text(
                "<!doctype html><html><head><title>Launch Post</title>"
                '<meta name="description" content="Official launch SEO description"></head>'
                "<body><h1>Launch Post</h1></body></html>",
                encoding="utf-8",
            )
            (leaky_dist / "secret-roadmap").mkdir(parents=True)
            (leaky_dist / "secret-roadmap" / "index.html").write_text(
                "<!doctype html><html><head><title>Secret</title></head><body>Leaked</body></html>",
                encoding="utf-8",
            )
            check_leaky = verify_build(
                leaky_dist,
                project_dir=project_dir,
                contract_path=project_dir / "migration-contract.json",
            )
            self.assertFalse(check_leaky["ok"])
            codes = {f["code"] for f in check_leaky["failures"]}
            self.assertIn("draft_route_leaked", codes)

    def test_cli_doctor(self) -> None:
        rc = cli_main(["doctor", "--project", str(MODULE_ROOT / "canary")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
