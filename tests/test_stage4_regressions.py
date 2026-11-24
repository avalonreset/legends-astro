"""Hermetic Stage 4a regression tests covering every live battle-test finding (BT-01 through BT-20 + self-redirect dedupe)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from legends_astro.auditor import audit_source, verify_build
from legends_astro.capture import (
    analyze_migration_triage,
    capture_site,
    detect_waf_challenge,
    extract_shortcodes,
    unwrap_firecrawl_records,
)
from legends_astro.cli import main as cli_main
from legends_astro.net_safety import (
    is_reparse_or_symlink,
    normalize_host,
    reject_symlink_chain,
    validate_outbound_url,
)
from legends_astro.scaffold import scaffold_astro_project, strip_trackers
from legends_astro.vendor import extract_html_asset_urls, rewrite_html_assets, vendor_assets


PUB_RESOLVER = lambda _host, _port: ["93.184.216.34"]


def _write(base: Path, rel: str, content: str) -> Path:
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class TestStage4BattleTestRegressions(unittest.TestCase):
    def test_bt01_capture_prioritizes_seed_url_before_sitemap(self) -> None:
        sitemap_xml = (
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://blog.example/post-1/</loc></url>"
            "<url><loc>https://blog.example/post-2/</loc></url>"
            "<url><loc>https://blog.example/post-3/</loc></url>"
            "<url><loc>https://blog.example/post-4/</loc></url>"
            "</urlset>"
        ).encode("utf-8")

        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            if url.endswith("/sitemap.xml"):
                return 200, {"Content-Type": "application/xml"}, sitemap_xml
            if url.endswith("/sitemap_index.xml"):
                return 404, {"Content-Type": "text/plain"}, b"missing"
            return (
                200,
                {"Content-Type": "text/html"},
                f"<!doctype html><html><head><title>{url}</title></head><body><h1>{url}</h1></body></html>".encode("utf-8"),
            )

        with tempfile.TemporaryDirectory() as tmp:
            res = capture_site(
                output_dir=tmp,
                url="https://blog.example/",
                from_sitemap=True,
                max_pages=2,
                resolver=PUB_RESOLVER,
                transport=transport,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["pageCount"], 2)
            manifest = json.loads((Path(tmp) / "raw-capture" / "manifest.json").read_text(encoding="utf-8"))
            routes = [p["route"] for p in manifest["pages"]]
            self.assertEqual(routes[0], "/", f"Expected seed '/' first, got {routes}")

    def test_bt02_source_audit_framework_precedence_and_firecrawl_tightening(self) -> None:
        # Bare dict with only "url" or "content" must NOT unwrap as Firecrawl
        self.assertEqual(unwrap_firecrawl_records({"url": "https://app.example.com", "rows": [1, 2]}), [])
        self.assertEqual(unwrap_firecrawl_records({"content": "generic fixture", "id": 1}), [])
        self.assertEqual(unwrap_firecrawl_records({"data": {"rows": [{"a": 1}]}}), [])
        # Real Firecrawl record with html/markdown DOES unwrap
        self.assertEqual(
            len(unwrap_firecrawl_records({"response": {"data": {"html": "<h1>Hi</h1>", "metadata": {"sourceURL": "https://a.example/"}}}})),
            1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "vite-react-app"
            _write(
                repo,
                "package.json",
                json.dumps({"dependencies": {"react": "^19.0.0"}, "devDependencies": {"vite": "^6.0.0"}}),
            )
            _write(repo, "vite.config.ts", "export default {};\n")
            _write(repo, "index.html", '<!doctype html><html><body><div id="root"></div></body></html>')
            _write(repo, "src/App.tsx", 'export function App() { return <div>Hi</div>; }\n')
            # Fixture JSON containing "url" AND even a fake firecrawl-like file inside node_modules
            _write(
                repo,
                "ops/fixtures/observed-user-rows.json",
                json.dumps({"url": "https://app.gohighlevel.com", "rows": []}),
            )
            _write(
                repo,
                "node_modules/some-pkg/sample.json",
                json.dumps({"html": "<h1>Should Be Pruned</h1>", "metadata": {"sourceURL": "https://ignore.example/"}}),
            )

            contract = audit_source(repo)
            self.assertEqual(contract["sourceKind"], "framework-vite")
            self.assertIn("frameworkMigration", contract)
            self.assertEqual(contract["frameworkMigration"]["framework"], "vite")

    def test_bt03_and_bt11_vendor_assets_asset_map_flag_and_bounded_concurrency(self) -> None:
        store = {
            f"https://cdn.example/img-{i}.png": (200, {"Content-Type": "image/png"}, f"PNG{i}".encode("utf-8"))
            for i in range(1, 13)
        }

        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return store.get(url, (404, {"Content-Type": "text/plain"}, b"missing"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "raw"
            imgs = "".join(f'<img src="https://cdn.example/img-{i}.png">' for i in range(1, 13))
            _write(src, "index.html", f"<!doctype html><html><body>{imgs}</body></html>")
            out = root / "astro-out"
            custom_map = root / "maps" / "custom-asset-map.json"

            res = vendor_assets(
                source_dir=src,
                output_dir=out,
                allow_hosts=["cdn.example"],
                asset_map_path=custom_map,
                concurrency=6,
                resolver=PUB_RESOLVER,
                transport=transport,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["assetCount"], 12)
            self.assertEqual(Path(res["assetMapPath"]).resolve(), custom_map.resolve())
            self.assertTrue(custom_map.is_file())
            self.assertFalse((out / "asset-map.json").exists())

            # Verify CLI accepts --asset-map and --concurrency in dry-run mode
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(
                    [
                        "vendor-assets",
                        "--source",
                        str(src),
                        "--output",
                        str(out),
                        "--allow-host",
                        "cdn.example",
                        "--asset-map",
                        str(custom_map),
                        "--concurrency",
                        "4",
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["plannedAssetCount"], 12)

    def test_bt04_cloudflare_passive_js_beacon_on_200_ok_not_waf_fallback(self) -> None:
        real_body = "<p>" + ("Full WordPress article body content with substantial visible prose. " * 12) + "</p>"
        passive_cf_html = (
            "<!doctype html><html><head><title>Dirigo Creative</title></head><body>"
            f"{real_body}"
            "<script>(function(){var a=document.createElement('script');a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';})();</script>"
            "</body></html>"
        )
        # 200 OK full page with passive CF beacon must NOT trigger WAF challenge
        self.assertEqual(detect_waf_challenge(200, {"content-type": "text/html"}, passive_cf_html), [])
        # Actual CF interstitial with Just a moment... or cf-mitigated or HTTP 403 MUST trigger
        self.assertTrue(
            detect_waf_challenge(
                200,
                {"content-type": "text/html"},
                "<html><head><title>Just a moment...</title></head><body><script src='/cdn-cgi/challenge-platform/h/b/main.js'></script></body></html>",
            )
        )
        self.assertTrue(
            detect_waf_challenge(403, {"content-type": "text/html"}, passive_cf_html)
        )
        self.assertTrue(
            detect_waf_challenge(200, {"cf-mitigated": "challenge"}, passive_cf_html)
        )

    def test_bt05_triage_external_cdns_ignores_anchor_hrefs(self) -> None:
        html_doc = (
            '<!doctype html><html><head>'
            '<link rel="canonical" href="https://origin.example/">'
            '<link rel="stylesheet" href="https://cdn.assets.example/site.css">'
            '<script src="https://scripts.assets.example/app.js"></script>'
            '<style>body{background:url("https://fonts.assets.example/bg.svg");}</style>'
            '</head><body>'
            '<a href="https://twitter.com/acme">Twitter</a>'
            '<a href="https://www.instagram.com/acme">Instagram</a>'
            '<a href="https://goals.sos.ga.gov/verify">Verify</a>'
            '<img src="https://images.assets.example/hero.webp">'
            '</body></html>'
        )
        triage = analyze_migration_triage([{"route": "/", "html": html_doc}], origin_host="origin.example")
        cdns = set(triage["externalCdns"])
        self.assertIn("cdn.assets.example", cdns)
        self.assertIn("scripts.assets.example", cdns)
        self.assertIn("fonts.assets.example", cdns)
        self.assertIn("images.assets.example", cdns)
        self.assertNotIn("twitter.com", cdns)
        self.assertNotIn("www.instagram.com", cdns)
        self.assertNotIn("goals.sos.ga.gov", cdns)

    def test_bt06_ssrf_upfront_seed_validation_and_ipv6_normalize_host(self) -> None:
        # IPv6 normalize_host preservation
        self.assertEqual(normalize_host("::ffff:127.0.0.1"), "::ffff:127.0.0.1")
        self.assertEqual(normalize_host("[::ffff:127.0.0.1]:8080"), "::ffff:127.0.0.1")
        self.assertEqual(normalize_host("http://[::ffff:127.0.0.1]/"), "::ffff:127.0.0.1")
        self.assertEqual(normalize_host("example.com:443"), "example.com")

        # IPv4-mapped IPv6 SSRF rejected by validate_outbound_url
        with self.assertRaises(ValueError):
            validate_outbound_url(
                "http://[::ffff:127.0.0.1]/",
                allowed_hosts=["::ffff:127.0.0.1"],
                allow_http=True,
            )

        # Upfront seed validation in capture_site creates ZERO output dirs on SSRF or disallowed HTTP
        probes = [
            ("http://127.0.0.1:80/", True),
            ("http://169.254.169.254/latest/meta-data/", True),
            ("http://[::ffff:127.0.0.1]/", True),
            ("http://example.com/", False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for idx, (bad_url, allow_http) in enumerate(probes):
                out_dir = Path(tmp) / f"ssrf-{idx}"
                with self.assertRaises(ValueError):
                    capture_site(output_dir=out_dir, url=bad_url, allow_http=allow_http)
                self.assertFalse(out_dir.exists(), f"Output dir must NOT be created for blocked seed {bad_url}")

                # Also verify via CLI --json returns exit 1, ok:false, and zero dirs created
                cli_out = Path(tmp) / f"ssrf-cli-{idx}"
                argv = ["capture", "--url", bad_url, "--output", str(cli_out), "--json"]
                if allow_http:
                    argv.append("--allow-http")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_main(argv)
                self.assertEqual(rc, 1)
                payload = json.loads(buf.getvalue())
                self.assertFalse(payload["ok"])
                self.assertFalse(cli_out.exists())

    def test_bt07_windows_ntfs_junction_and_symlink_chain_blocked(self) -> None:
        # 1. Belt-and-suspenders mocked Windows FILE_ATTRIBUTE_REPARSE_POINT check
        fake_stat = mock.MagicMock()
        fake_stat.st_file_attributes = 0x0400  # FILE_ATTRIBUTE_REPARSE_POINT
        with mock.patch("legends_astro.net_safety.os.name", "nt"), mock.patch(
            "legends_astro.net_safety.os.lstat", return_value=fake_stat
        ):
            self.assertTrue(is_reparse_or_symlink(Path("C:/fake/junction")))

        # 2. Real Windows NTFS Directory Junction end-to-end test (unprivileged on Windows!)
        if os.name == "nt":
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                src = root / "src-html"
                _write(src, "index.html", "<!doctype html><html><head><title>Hi</title></head><body><h1>Hi</h1></body></html>")
                real_target = root / "real-target"
                real_target.mkdir()
                junc = root / "ntfs-junction"
                proc = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junc), str(real_target)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, f"mklink /J failed: {proc.stderr} {proc.stdout}")
                try:
                    self.assertTrue(is_reparse_or_symlink(junc))
                    with self.assertRaises(ValueError):
                        reject_symlink_chain(junc)
                    with self.assertRaises(ValueError):
                        reject_symlink_chain(junc / "nested" / "file.txt")
                    with self.assertRaises(ValueError):
                        scaffold_astro_project(source_path=src, output_dir=junc, mode="clone", apply=False)
                    self.assertEqual(list(real_target.iterdir()), [], "Nothing may be written through an NTFS junction")
                finally:
                    subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True, check=False)

    def test_bt08_firecrawl_skips_empty_records_no_zero_byte_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fc_dir = root / "fc-census"
            # Non-HTML API response + empty html/markdown record
            _write(
                fc_dir,
                "census.raw.json",
                json.dumps(
                    {
                        "response": {
                            "data": {
                                "metadata": {"sourceURL": "https://api.census.gov/data"},
                                "html": "",
                                "markdown": "",
                            }
                        }
                    }
                ),
            )
            out_dir = root / "cap-out"
            res = capture_site(output_dir=out_dir, from_firecrawl=fc_dir)
            self.assertTrue(res["ok"])
            self.assertEqual(res["pageCount"], 0)
            self.assertEqual(res["totalBytes"], 0)
            self.assertFalse((out_dir / "raw-capture" / "pages" / "index.html").exists())

    def test_bt09_shortcode_detector_ignores_non_wp_route_placeholders(self) -> None:
        docs_text = "In Next.js dynamic routes, create pages/blog/[slug].tsx or [id] or [image] for dynamic params."
        self.assertEqual(extract_shortcodes(docs_text, is_wordpress=False), [])
        # Real WP shortcodes still caught
        wp_text = 'Fill out [contact-form-7 id="42" title="Contact"] and [vc_row]Hello[/vc_row] and [gallery].'
        found = extract_shortcodes(wp_text, is_wordpress=False)
        self.assertIn("[contact-form-7]", found)
        self.assertIn("[vc_row]", found)
        self.assertIn("[gallery]", found)

    def test_bt10_cli_json_clean_structured_errors_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xxe = _write(
                Path(tmp),
                "xxe.xml",
                '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b">]><rss><channel></channel></rss>',
            )
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = cli_main(["source-audit", "--source", str(xxe), "--json"])
            self.assertEqual(rc, 1)
            self.assertEqual(err_buf.getvalue(), "")
            payload = json.loads(out_buf.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["errorType"], "ValueError")
            self.assertIn("CWE-776", payload["error"])

            # Non-json mode emits concise stderr message, no traceback
            out_buf2 = io.StringIO()
            err_buf2 = io.StringIO()
            with redirect_stdout(out_buf2), redirect_stderr(err_buf2):
                rc2 = cli_main(["source-audit", "--source", str(xxe)])
            self.assertEqual(rc2, 1)
            self.assertNotIn("Traceback", err_buf2.getvalue())
            self.assertIn("ValueError", err_buf2.getvalue())

    def test_bt12_and_bt17_vendor_assets_script_exclusion_html_rejection_and_final_url_dedupe(self) -> None:
        # 1. BT-17: JS new URL(r), new URL(l.origin) inside <script> must NOT be extracted
        html_with_js = (
            '<!doctype html><html><head>'
            '<style>.bg{background:url("https://cdn.example/bg.png");}</style>'
            '<script type="module">const a = new URL(l.origin); const b = new URL(r); const c = new URL(u,o);</script>'
            '<!-- background:url(https://cdn.example/commented.png) -->'
            '</head><body>'
            '<div style="background-image:url(&quot;https://cdn.example/inline.webp&quot;)"></div>'
            '<img src="https://cdn.example/a-redirect.jpg">'
            '<img src="https://cdn.example/b-redirect.jpg">'
            '<img src="https://cdn.example/c-redirect.jpg">'
            '<img src="https://cdn.example/soft-404-html.jpg">'
            '</body></html>'
        )
        extracted = extract_html_asset_urls(html_with_js)
        self.assertIn("https://cdn.example/bg.png", extracted)
        self.assertIn("https://cdn.example/inline.webp", extracted)
        self.assertNotIn("l.origin", extracted)
        self.assertNotIn("r", extracted)
        self.assertNotIn("u,o", extracted)
        self.assertNotIn("https://cdn.example/commented.png", extracted)

        # 2. BT-12: 3 URLs redirecting to same final_url + 1 text/html soft-404 must NOT crash with FileExistsError
        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            if url in (
                "https://cdn.example/a-redirect.jpg",
                "https://cdn.example/b-redirect.jpg",
                "https://cdn.example/c-redirect.jpg",
            ):
                return 301, {"Location": "https://cdn.example/canonical-photo.jpg"}, b""
            if url == "https://cdn.example/canonical-photo.jpg":
                return 200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xffJPG"
            if url == "https://cdn.example/soft-404-html.jpg":
                return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<!doctype html><html><body>Not found page</body></html>"
            if url == "https://cdn.example/bg.png":
                return 200, {"Content-Type": "image/png"}, b"PNGDATA"
            if url == "https://cdn.example/inline.webp":
                return 200, {"Content-Type": "image/webp"}, b"WEBPDATA"
            return 404, {"Content-Type": "text/plain"}, b"missing"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "raw"
            _write(src, "index.html", html_with_js)
            out = root / "astro-out"

            res = vendor_assets(
                source_dir=src,
                output_dir=out,
                allow_hosts=["cdn.example"],
                overwrite=False,
                resolver=PUB_RESOLVER,
                transport=transport,
            )
            self.assertTrue(res["ok"])
            # 3 real unique assets: bg.png, inline.webp, canonical-photo.jpg
            self.assertEqual(res["assetCount"], 3)
            # Soft-404 text/html rejected and logged, never saved to public/
            self.assertTrue(any(e.get("reason") == "skipped_html_response" for e in res["errors"]))
            self.assertFalse(any("soft-404-html" in p.name for p in (out / "public").rglob("*")))
            # All 3 redirecting URLs map to the same deduplicated local path
            amap = json.loads((out / "asset-map.json").read_text(encoding="utf-8"))["map"]
            self.assertEqual(
                amap["https://cdn.example/a-redirect.jpg"],
                amap["https://cdn.example/b-redirect.jpg"],
            )
            self.assertEqual(
                amap["https://cdn.example/b-redirect.jpg"],
                amap["https://cdn.example/c-redirect.jpg"],
            )

    def test_bt13_editorial_scaffold_generates_root_index_when_missing(self) -> None:
        wxr = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
    <item>
        <title>Cloud Post</title>
        <link>https://acme.example/blog/cloud-post/</link>
        <wp:post_name>cloud-post</wp:post_name>
        <wp:post_type>post</wp:post_type>
        <wp:status>publish</wp:status>
        <content:encoded><![CDATA[<p>Body text.</p>]]></content:encoded>
    </item>
</channel>
</rss>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_p = _write(root, "export.xml", wxr)
            out = root / "ed-out"
            prev = scaffold_astro_project(source_path=xml_p, output_dir=out, mode="editorial", apply=False)
            planned_paths = {f["path"] for f in prev["plannedFiles"]}
            self.assertIn("src/pages/index.astro", planned_paths)
            scaffold_astro_project(
                source_path=xml_p, output_dir=out, mode="editorial", apply=True, confirm_plan=prev["planSha256"]
            )
            idx_txt = (out / "src" / "pages" / "index.astro").read_text(encoding="utf-8")
            self.assertIn("getCollection", idx_txt)
            self.assertIn("isPublished", idx_txt)

    def test_bt14_ga4_tracker_regex_case_sensitive_ignores_padding_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            _write(dist, "robots.txt", "User-agent: *\nDisallow: /\n")
            _write(dist, "_headers", "/*\n  X-Robots-Tag: noindex, nofollow\n")
            _write(
                dist,
                "index.html",
                '<!doctype html><html><head><title>Clean Staging</title>'
                '<meta name="robots" content="noindex, nofollow">'
                '<style>.card{padding-bottom:24px;padding-horizontal:16px;padding-vertical:12px;}</style>'
                "</head><body><h1>Hello</h1></body></html>",
            )
            res = verify_build(dist, env="staging")
            self.assertTrue(res["ok"], f"False-positive staging failures: {res['failures']}")

            # Real GA4 measurement ID MUST still be caught
            _write(
                dist,
                "leaked.html",
                '<!doctype html><html><head><title>Leaked</title><meta name="robots" content="noindex">'
                "<script>const id = 'G-A1B2C3D4E5';</script></head><body><h1>Leak</h1></body></html>",
            )
            res_leak = verify_build(dist, env="staging")
            self.assertFalse(res_leak["ok"])
            self.assertTrue(
                any(f["code"] == "staging_live_tracker_leaked" and f["tracker"] == "google-analytics" for f in res_leak["failures"])
            )

    def test_bt15_raw_capture_does_not_synthesize_missing_canonical(self) -> None:
        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return (
                200,
                {"Content-Type": "text/html"},
                b"<!doctype html><html><head><title>No Canonical</title></head><body><h1>Home</h1></body></html>",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cap = root / "cap"
            capture_site(
                output_dir=cap,
                url="https://nocanon.example/",
                resolver=PUB_RESOLVER,
                transport=transport,
            )
            contract = json.loads((cap / "migration-contract.json").read_text(encoding="utf-8"))
            self.assertIsNone(contract["pages"][0]["canonical"])
            self.assertEqual(contract["pages"][0]["finalUrl"], "https://nocanon.example/")

            dist = root / "dist"
            _write(dist, "index.html", "<!doctype html><html><head><title>No Canonical</title></head><body><h1>Home</h1></body></html>")
            ver = verify_build(dist, contract_path=cap / "migration-contract.json")
            self.assertTrue(ver["ok"], f"Unexpected canonical failure: {ver['failures']}")

    def test_bt16_vendor_og_image_strip_preconnect_and_deduped_forbid_host(self) -> None:
        html_src = (
            '<!doctype html><html><head><title>Webflow</title>'
            '<link rel="preconnect" href="https://cdn.wf.example">'
            '<link rel="dns-prefetch" href="https://cdn.wf.example">'
            '<link rel="preconnect" href="https://keep-me.example">'
            '<meta property="og:image" content="https://cdn.wf.example/og.jpg">'
            '<meta name="twitter:image" content="https://cdn.wf.example/tw.jpg">'
            "</head><body><h1>Site</h1></body></html>"
        )
        urls = extract_html_asset_urls(html_src)
        self.assertIn("https://cdn.wf.example/og.jpg", urls)
        self.assertIn("https://cdn.wf.example/tw.jpg", urls)

        rewritten = rewrite_html_assets(
            html_src,
            {
                "https://cdn.wf.example/og.jpg": "/assets/cdn-wf-example/og.jpg",
                "https://cdn.wf.example/tw.jpg": "/assets/cdn-wf-example/tw.jpg",
            },
        )
        self.assertNotIn("cdn.wf.example", rewritten)
        self.assertIn("https://keep-me.example", rewritten)

        # verify-build --forbid-host deduplicates per (file, host) and ignores passive canonical/JSON-LD/<a href>
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            _write(
                dist,
                "index.html",
                '<!doctype html><html><head><title>Test</title>'
                '<link rel="canonical" href="https://old.example/">'
                '<script type="application/ld+json">{"@id":"https://old.example/#org","url":"https://old.example/"}</script>'
                "</head><body>"
                '<a href="https://old.example/about">Outbound link</a>'
                "<h1>Passive Only</h1></body></html>",
            )
            res_passive = verify_build(dist, forbid_hosts=["old.example"])
            self.assertTrue(res_passive["ok"], f"Passive refs should not fail default --forbid-host: {res_passive['failures']}")

            # Strict mode catches raw passive refs and deduplicates them
            res_strict = verify_build(dist, forbid_hosts=["old.example"], forbid_host_strict=True)
            forbid_strict = [f for f in res_strict["failures"] if f["code"] == "forbidden_host_referenced"]
            self.assertEqual(len(forbid_strict), 1)
            self.assertGreaterEqual(forbid_strict[0]["count"], 3)

            # Active hotlinks are caught and deduplicated per (file, host) with max 5 samples
            _write(
                dist,
                "hotlinks.html",
                "<!doctype html><html><head><title>Hot</title></head><body>"
                + "".join(f'<img src="https://old.example/p{i}.jpg">' for i in range(8))
                + "</body></html>",
            )
            res_active = verify_build(dist, forbid_hosts=["old.example"])
            active_forbid = [f for f in res_active["failures"] if f["code"] == "forbidden_host_referenced"]
            self.assertEqual(len(active_forbid), 1)
            self.assertEqual(active_forbid[0]["count"], 8)
            self.assertEqual(len(active_forbid[0]["samples"]), 5)

    def test_bt18_framework_audit_gates_nuxt_composables_filters_content_and_finds_server_apis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "vite-express"
            _write(
                repo,
                "package.json",
                json.dumps({"dependencies": {"react": "^19.0.0", "express": "^4.19.0"}, "devDependencies": {"vite": "^6.0.0"}}),
            )
            _write(repo, "vite.config.ts", "export default {};\n")
            _write(repo, "index.html", '<!doctype html><html><body><div id="root"></div></body></html>')
            _write(
                repo,
                "src/features/nav.tsx",
                "export function Nav() { navigateTo('/home'); useFetch('/api/ping'); return <nav />; }\n",
            )
            _write(
                repo,
                "server/index.mjs",
                "app.get('/api/health', handler);\nrouter.post('/api/checkout', pay);\nif (url.pathname === '/api/session') {}\n",
            )
            _write(repo, "server/index.node-test.mjs", "app.get('/api/test-only', () => {});\n")
            _write(repo, ".github/PULL_REQUEST_TEMPLATE.md", "# PR\n")
            _write(repo, "ops/runbook.md", "# Ops\n")
            _write(repo, "scripts/notes.md", "# Script\n")
            _write(repo, "tools/readme.md", "# Tool\n")
            _write(repo, "sounds/credits.md", "# Sound\n")
            _write(repo, "public/info.md", "# Public\n")
            _write(repo, "CREDITS.md", "# Credits\n")
            _write(repo, "content/blog/hello.md", "---\ntitle: Hello\n---\nReal post\n")

            contract = audit_source(repo)
            fm = contract["frameworkMigration"]
            # nuxt-composables NOT flagged on React/Vite .tsx
            self.assertNotIn("nuxt-composables", fm["frameworkImportsAndTraps"])
            # Express & Node HTTP server/ routes discovered (and test file skipped)
            api_routes = {a["route"] for a in fm["apiRoutes"]}
            self.assertIn("/api/health", api_routes)
            self.assertIn("/api/checkout", api_routes)
            self.assertIn("/api/session", api_routes)
            self.assertNotIn("/api/test-only", api_routes)
            # Only genuine content/blog collection included
            col_roots = {c["rootDir"] for c in fm["contentCollections"]}
            self.assertEqual(col_roots, {"content/blog"})

    def test_bt19_and_bt20_strip_trackers_completeness_and_exclude_resource_tag_ids(self) -> None:
        wp_html = (
            '<!doctype html><html><head><title>WP</title>'
            '<link id="wp-block-library-css" rel="stylesheet" href="/style.css">'
            '<style id="global-styles-inline-css">body{margin:0}</style>'
            '<script id="google-recaptcha-js" src="https://www.google.com/recaptcha/api.js"></script>'
            '<script id="wpcf7-recaptcha-js" src="/wp-content/plugins/contact-form-7/modules/recaptcha/index.js"></script>'
            '<script id="cleantalk-js" src="/wp-content/plugins/cleantalk-spam-protect/js/apbct-public.min.js"></script>'
            "<script>(function(){var s='/cdn-cgi/challenge-platform/scripts/jsd/main.js';})();</script>"
            '<script id="app-main-js" src="/app.js"></script>'
            '</head><body><main id="primary-content"><h1>Hello</h1></main></body></html>'
        )
        stripped = strip_trackers(wp_html)
        self.assertNotIn("google.com/recaptcha", stripped)
        self.assertNotIn("wpcf7-recaptcha", stripped)
        self.assertNotIn("cleantalk-spam-protect", stripped)
        self.assertNotIn("/cdn-cgi/challenge-platform/", stripped)
        self.assertIn('id="app-main-js"', stripped)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            _write(src, "index.html", wp_html)
            contract = audit_source(src)
            req_ids = contract["pages"][0]["requiredDomIds"]
            self.assertIn("primary-content", req_ids)
            self.assertNotIn("google-recaptcha-js", req_ids)
            self.assertNotIn("wp-block-library-css", req_ids)
            self.assertNotIn("global-styles-inline-css", req_ids)
            self.assertNotIn("app-main-js", req_ids)

    def test_self_redirect_loop_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            _write(dist, "index.html", "<!doctype html><html><head><title>Home</title></head><body><h1>Home</h1></body></html>")
            _write(dist, "_redirects", "/self /self 301\n")
            res = verify_build(dist)
            self_loops = [f for f in res["failures"] if f["code"] == "redirect_loop" and f.get("source") == "/self"]
            self.assertEqual(len(self_loops), 1, f"Expected exactly 1 self-redirect loop failure, got {self_loops}")


if __name__ == "__main__":
    unittest.main()
