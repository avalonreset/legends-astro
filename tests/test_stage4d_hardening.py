"""Hermetic Stage 4d hardening regression tests covering all 16 audit + variety battle-test findings."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from legends_astro.auditor import (
    audit_source,
    html_to_markdown,
    ingest_source_to_astro,
    inspect_html_text,
    verify_build,
)
from legends_astro.capture import (
    _TRACKER_PATTERNS,
    _parse_sitemap_urls,
    capture_site,
    collect_firecrawl_sources,
    extract_head_and_body,
)
from legends_astro.cli import _run_node_doctor
from legends_astro.docs_nav import ALLOWED_LIVE_HOSTS, _fetch_bounded_json
from legends_astro.net_safety import (
    FetchResult,
    is_forbidden_ip,
    reject_controlled_folder_access,
    safe_write_bytes,
    sanitize_rel_path,
)
from legends_astro.scaffold import scaffold_astro_project, strip_trackers
from legends_astro.twin import run_twin, validate_twin_route
from legends_astro.vendor import (
    extract_css_urls,
    rewrite_css_assets,
    rewrite_html_assets,
    rewrite_urls_longest_first,
    vendor_assets,
)

MODULE_ROOT = Path(__file__).resolve().parent.parent
PUB_RESOLVER = lambda _host, _port: ["93.184.216.34"]


def _write(base: Path, rel: str, content: str) -> Path:
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class TestStage4dHardening(unittest.TestCase):
    # ------------------------------------------------------------------
    # C-01: loadPlaywright() RCE via cwd fallback & package self-ref
    # ------------------------------------------------------------------
    def test_c01_twin_blocks_cwd_fallback_and_package_self_referencing_rce(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required for twin tests")
        twin_mjs = MODULE_ROOT / "scripts" / "twin.mjs"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evil_dir = root / "evil-repo"
            evil_dir.mkdir()
            marker = evil_dir / "pwned.txt"
            _write(
                evil_dir,
                "package.json",
                json.dumps({"name": "playwright", "version": "1.0.0", "exports": "./pwn.cjs"}),
            )
            _write(
                evil_dir,
                "pwn.cjs",
                f'require("fs").writeFileSync({json.dumps(str(marker))}, "PWNED");\nmodule.exports = {{}};\n',
            )
            dist = root / "dist"
            _write(dist, "index.html", "<!doctype html><html><head><title>Hi</title></head><body><h1>Hi</h1></body></html>")

            env = dict(os.environ)
            env.pop("PLAYWRIGHT_PACKAGE_JSON", None)

            # 1. Implicit cwd fallback must NOT execute pwn.cjs
            proc1 = subprocess.run(
                [node_bin, str(twin_mjs), "--dist", str(dist), "--out", str(root / "out1")],
                cwd=str(evil_dir),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertFalse(marker.exists(), "Implicit process.cwd() executed attacker pwn.cjs!")
            self.assertEqual(proc1.returncode, 2, f"Expected exit 2 playwright_not_installed, got {proc1.returncode}: {proc1.stdout}")

            # 2. Explicit --playwright-from pointing at evil_dir or evil_dir/package.json must NOT trigger Package Self-Referencing
            for target_arg in (str(evil_dir), str(evil_dir / "package.json")):
                proc2 = subprocess.run(
                    [node_bin, str(twin_mjs), "--dist", str(dist), "--out", str(root / "out2"), "--playwright-from", target_arg],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertFalse(marker.exists(), f"--playwright-from {target_arg} executed Package Self-Referencing RCE!")
                self.assertEqual(proc2.returncode, 2)

            # 3. Wrong package name inside node_modules/playwright/package.json must be rejected
            _write(
                evil_dir,
                "node_modules/playwright/package.json",
                json.dumps({"name": "evil-impostor", "main": "./index.cjs"}),
            )
            _write(
                evil_dir,
                "node_modules/playwright/index.cjs",
                f'require("fs").writeFileSync({json.dumps(str(marker))}, "PWNED2");\n',
            )
            proc3 = subprocess.run(
                [node_bin, str(twin_mjs), "--dist", str(dist), "--out", str(root / "out3"), "--playwright-from", str(evil_dir)],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertFalse(marker.exists(), "Impostor node_modules/playwright package executed!")
            self.assertEqual(proc3.returncode, 2)

            # 4. twin.py rejects symlink/junction on --playwright-from, and twin.mjs blocks escaping node_modules/playwright junctions while allowing contained pnpm .pnpm/ links
            if os.name == "nt":
                real_pw = root / "real-pw"
                real_pw.mkdir()
                junc_pw = root / "junc-pw"
                mk = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc_pw), str(real_pw)], capture_output=True, text=True, check=False)
                self.assertEqual(mk.returncode, 0)
                try:
                    with self.assertRaises(ValueError):
                        run_twin(
                            module_root=MODULE_ROOT,
                            output_dir=root / "out4",
                            dist_dir=dist,
                            playwright_from=junc_pw,
                        )
                finally:
                    subprocess.run(["cmd", "/c", "rmdir", str(junc_pw)], capture_output=True, check=False)

                # Escaping junction inside node_modules/playwright -> outside dir MUST be rejected
                esc_proj = root / "esc-proj"
                (esc_proj / "node_modules").mkdir(parents=True)
                outside_pw = root / "outside-pw"
                _write(outside_pw, "package.json", json.dumps({"name": "playwright", "main": "./index.cjs"}))
                _write(outside_pw, "index.cjs", f'require("fs").writeFileSync({json.dumps(str(marker))}, "ESC");\n')
                esc_junc = esc_proj / "node_modules" / "playwright"
                mk2 = subprocess.run(["cmd", "/c", "mklink", "/J", str(esc_junc), str(outside_pw)], capture_output=True, text=True, check=False)
                self.assertEqual(mk2.returncode, 0)
                try:
                    proc_esc = subprocess.run(
                        [node_bin, str(twin_mjs), "--dist", str(dist), "--out", str(root / "out5"), "--playwright-from", str(esc_proj)],
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    self.assertFalse(marker.exists(), "Escaping node_modules/playwright junction executed!")
                    self.assertEqual(proc_esc.returncode, 2)
                finally:
                    subprocess.run(["cmd", "/c", "rmdir", str(esc_junc)], capture_output=True, check=False)

    # ------------------------------------------------------------------
    # H-01: Multi-vector SSRF (net_safety.py + twin.py + scripts/twin.mjs)
    # ------------------------------------------------------------------
    def test_h01_ssrf_ipv6_transition_routes_and_twin_guards(self) -> None:
        # Python net_safety.py checks
        for bad_ip in (
            "2002:7f00:1::1",
            "2002:a9fe:a9fe::1",
            "2002:0a00:0001::1",
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
            "64:ff9b::127.0.0.1",
            "64:ff9b::a9fe:a9fe",
            "64:ff9b:1::1",
            "100::1",
            "2001:db8::1",
            "192.0.0.1",
            "192.0.2.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
        ):
            self.assertTrue(is_forbidden_ip(bad_ip), f"net_safety must forbid {bad_ip}")
        self.assertFalse(is_forbidden_ip("93.184.216.34"))
        self.assertFalse(is_forbidden_ip("2606:2800:220:1:248:1893:25c8:1946"))

        # Python twin.py route validation
        for bad_route in (
            "//169.254.169.254/latest/meta-data/",
            "/\\evil.example/pwn",
            "https://evil.example/pwn",
            "/bad\r\nroute",
        ):
            with self.assertRaises(ValueError, msg=f"validate_twin_route must reject {bad_route!r}"):
                validate_twin_route(bad_route, base_url="https://site.example")
        self.assertEqual(validate_twin_route("about/", base_url="https://site.example"), "/about/")

        # Node scripts/twin.mjs IPv6 + route + isolation checks
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin)
        twin_url = (MODULE_ROOT / "scripts" / "twin.mjs").as_uri()
        check_js = f"""
import {{ isForbiddenIp, validateRoute, hostMatchesAllowlist }} from {json.dumps(twin_url)};
const mustForbid = [
  "::ffff:7f00:1", "[::ffff:7f00:1]", "[::ffff:a9fe:a9fe]", "::ffff:127.0.0.1",
  "0:0:0:0:0:0:0:1", "0000:0000:0000:0000:0000:0000:0000:0000",
  "2002:7f00:1::1", "2002:a9fe:a9fe::1", "2001:0000::1",
  "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "100::1", "2001:db8::1",
  "fc00::1", "fd12::1", "fe80::1", "ff02::1",
  "192.0.0.1", "192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1"
];
for (const ip of mustForbid) {{
  if (!isForbiddenIp(ip)) throw new Error("Expected forbidden IP: " + ip);
}}
for (const okIp of ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]) {{
  if (isForbiddenIp(okIp)) throw new Error("Expected allowed IP: " + okIp);
}}
for (const badRoute of ["//169.254.169.254/latest/meta-data/", "/\\\\evil.example/pwn", "http://evil.example/", "/a\\nb"]) {{
  let threw = false;
  try {{ validateRoute(badRoute, "https://site.example"); }} catch {{ threw = true; }}
  if (!threw) throw new Error("Expected validateRoute to reject: " + badRoute);
}}
if (validateRoute("/docs/", "https://site.example") !== "/docs/") throw new Error("Valid route rejected");
if (!hostMatchesAllowlist("cdn.example.com", ["*.example.com"])) throw new Error("Allowlist wildcard failed");
if (hostMatchesAllowlist("evil.com", ["*.example.com"])) throw new Error("Allowlist leaked");
console.log("TWIN_SSRF_OK");
"""
        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", check_js],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"twin.mjs SSRF check failed: {proc.stderr} {proc.stdout}")
        self.assertIn("TWIN_SSRF_OK", proc.stdout)

        # Verify scripts/twin.mjs installs context.route, context.routeWebSocket, and --host-resolver-rules
        twin_src = (MODULE_ROOT / "scripts" / "twin.mjs").read_text(encoding="utf-8")
        self.assertIn('context.route("**/*"', twin_src)
        self.assertIn('context.routeWebSocket("**/*"', twin_src)
        self.assertIn("--host-resolver-rules=", twin_src)

    # ------------------------------------------------------------------
    # H-02: Stored XSS in html_to_markdown()
    # ------------------------------------------------------------------
    def test_h02_html_to_markdown_prevents_xss_and_unsafe_urls(self) -> None:
        html_in = (
            "<p>Code example: &lt;script&gt;window.__XSS=1&lt;/script&gt;</p>"
            "<p>---</p>"
            '<p><a href="javascript:alert(1)">Click [me]</a></p>'
            '<p><a href="vbscript:msgbox(1)">VB</a></p>'
            '<p><img src="data:text/html,&lt;script&gt;1&lt;/script&gt;" alt="bad [img]"></p>'
            '<p><a href="https://example.com/path(1)">Safe [link]</a></p>'
            "<pre><code>&lt;div&gt;fenced&lt;/div&gt;</code></pre>"
        )
        md = html_to_markdown(html_in)
        self.assertNotIn("<script>", md)
        self.assertIn("&lt;script&gt;window.__XSS=1&lt;/script&gt;", md)
        self.assertNotIn("javascript:", md.lower())
        self.assertNotIn("vbscript:", md.lower())
        self.assertNotIn("data:text/html", md.lower())
        self.assertIn(r"[Safe \[link\]](https://example.com/path%281%29)", md)
        self.assertNotIn("\n---\n", f"\n{md}\n")
        self.assertIn("```\n<div>fenced</div>\n```", md)

    # ------------------------------------------------------------------
    # M-01: ReDoS in Facebook tracker regex & premature </body> truncation
    # ------------------------------------------------------------------
    def test_m01_tracker_regex_no_redos_and_script_body_close_no_truncation(self) -> None:
        adversarial = "connect.facebook.net/" * 6000
        t0 = time.monotonic()
        for _name, pat in _TRACKER_PATTERNS:
            pat.search(adversarial)
        strip_trackers(f"<script>{adversarial}</script>")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, f"Tracker regex showed polynomial slowdown: {elapsed:.3f}s")

        tricky_html = (
            '<!doctype html><html><head><title>Script Body Test</title></head>'
            '<body><header>Top</header>'
            '<script>function popup(){ doc.write("<html><body>Hi</body></html>"); }</script>'
            '<!-- </body> -->'
            '<main id="preserved"><h1>After Script</h1></main>'
            "</body></html>"
        )
        _head, body = extract_head_and_body(tricky_html)
        self.assertIn('<main id="preserved"><h1>After Script</h1></main>', body)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            _write(src, "index.html", tricky_html)
            out = root / "clone"
            prev = scaffold_astro_project(source_path=src, output_dir=out, mode="clone", apply=False)
            scaffold_astro_project(source_path=src, output_dir=out, mode="clone", apply=True, confirm_plan=prev["planSha256"])
            md = (out / "src" / "content" / "pages" / "index.md").read_text(encoding="utf-8")
            self.assertIn("After Script", md)

    # ------------------------------------------------------------------
    # M-02: OneDrive CFA, atomic safe_write_bytes, twin --overwrite
    # ------------------------------------------------------------------
    def test_m02_onedrive_cfa_atomic_overwrite_and_twin_overwrite_guard(self) -> None:
        # OneDrive KFM + Favorites blocked
        with self.assertRaises(ValueError):
            reject_controlled_folder_access(Path.home() / "OneDrive" / "Documents" / "evil.txt")
        with self.assertRaises(ValueError):
            reject_controlled_folder_access(Path.home() / "Favorites" / "evil.txt")

        with tempfile.TemporaryDirectory() as tmp:
            fake_od = Path(tmp) / "CorpOneDrive"
            (fake_od / "Desktop").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"OneDriveCommercial": str(fake_od)}):
                with self.assertRaises(ValueError):
                    reject_controlled_folder_access(fake_od / "Desktop" / "out.txt")

            # Fail closed on OSError
            with mock.patch.object(Path, "home", side_effect=OSError("no home")):
                with self.assertRaises(ValueError):
                    reject_controlled_folder_access(Path(tmp) / "ok.txt")

            # Atomic safe_write_bytes(..., overwrite=True) leaves no temp files
            target = Path(tmp) / "atomic.txt"
            safe_write_bytes(target, b"v1", overwrite=False)
            safe_write_bytes(target, b"v2-updated", overwrite=True)
            self.assertEqual(target.read_bytes(), b"v2-updated")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

            # scripts/twin.mjs refuses clobbering existing twin.json without --overwrite
            node_bin = shutil.which("node")
            self.assertIsNotNone(node_bin)
            dist = Path(tmp) / "dist"
            _write(dist, "index.html", "<!doctype html><html><head><title>Hi</title></head><body><h1>Hi</h1></body></html>")
            out_dir = Path(tmp) / "twin-out"
            _write(out_dir, "twin.json", '{"existing": true}\n')
            # Provide a fake verified node_modules/playwright stub to reach assertSafeOutputTarget
            pw_root = Path(tmp) / "pw-stub"
            _write(pw_root, "node_modules/playwright/package.json", json.dumps({"name": "playwright", "main": "./index.cjs"}))
            _write(pw_root, "node_modules/playwright/index.cjs", "module.exports = { chromium: { launch: async () => { throw new Error('should not launch'); } } };\n")
            proc = subprocess.run(
                [node_bin, str(MODULE_ROOT / "scripts" / "twin.mjs"), "--dist", str(dist), "--out", str(out_dir), "--playwright-from", str(pw_root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            payload = json.loads(proc.stdout)
            self.assertIn("--overwrite", payload["message"])
            self.assertEqual((out_dir / "twin.json").read_text(encoding="utf-8"), '{"existing": true}\n')

    # ------------------------------------------------------------------
    # M-03: Sitemap cap, vendor_assets bounded cancel, Firecrawl bounds
    # ------------------------------------------------------------------
    def test_m03_sitemap_cap_budget_vendor_cancel_and_firecrawl_bounds(self) -> None:
        # 1. Sitemap index with 80 empty child sitemaps capped at max_sitemaps=32
        children = "".join(f"<sitemap><loc>https://site.example/sm-{i}.xml</loc></sitemap>" for i in range(80))
        idx_xml = f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{children}</sitemapindex>'.encode("utf-8")
        empty_urlset = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        fetch_count = 0

        def sm_transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            nonlocal fetch_count
            fetch_count += 1
            if url.endswith("sitemap_index.xml"):
                return 200, {"Content-Type": "application/xml"}, idx_xml
            return 200, {"Content-Type": "application/xml"}, empty_urlset

        _urls, visited = _parse_sitemap_urls(
            "https://site.example/sitemap_index.xml",
            allowed_hosts=["site.example"],
            allow_http=False,
            max_page_bytes=1024 * 1024,
            max_urls=10,
            resolver=PUB_RESOLVER,
            transport=sm_transport,
        )
        self.assertLessEqual(fetch_count, 32, f"Sitemap amplification exceeded cap: {fetch_count}")
        self.assertLessEqual(len(visited), 32)

        # Sitemap XML bytes deducted from max_total_bytes
        with self.assertRaises(ValueError):
            _parse_sitemap_urls(
                "https://site.example/sitemap_index.xml",
                allowed_hosts=["site.example"],
                allow_http=False,
                max_page_bytes=1024 * 1024,
                max_urls=10,
                byte_budget={"used": 0, "limit": 200},
                resolver=PUB_RESOLVER,
                transport=sm_transport,
            )

        # 2. vendor_assets raises cleanly on budget breach with bounded window
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            imgs = "".join(f'<img src="https://cdn.example/f{i}.bin">' for i in range(20))
            _write(src, "index.html", f"<!doctype html><html><body>{imgs}</body></html>")

            def big_transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
                return 200, {"Content-Type": "application/octet-stream"}, b"X" * 1000

            with self.assertRaises(ValueError):
                vendor_assets(
                    source_dir=src,
                    output_dir=root / "out",
                    allow_hosts=["cdn.example"],
                    max_total_bytes=2500,
                    concurrency=2,
                    resolver=PUB_RESOLVER,
                    transport=big_transport,
                )

            # 3. collect_firecrawl_sources respects max_files and max_total_bytes
            fc = root / "fc"
            for i in range(6):
                _write(
                    fc,
                    f"p{i}.json",
                    json.dumps({"metadata": {"sourceURL": f"https://fc.example/p{i}/"}, "html": f"<h1>Page {i}</h1>"}),
                )
            bounded = collect_firecrawl_sources(fc, max_files=2)
            self.assertEqual(len(bounded), 2)
            with self.assertRaises(ValueError):
                collect_firecrawl_sources(fc, max_total_bytes=50)

    # ------------------------------------------------------------------
    # M-04: CSS & query, single-pass rewrite, refreshed CSS sha256/bytes
    # ------------------------------------------------------------------
    def test_m04_css_ampersand_single_pass_and_digest_refresh(self) -> None:
        css_in = 'body{background:url("https://cdn.example/hero.jpg?w=1200&q=80");}\n.b{background:url(&quot;https://cdn.example/pic.jpg?a=1&amp;b=2&quot;);}'
        found = extract_css_urls(css_in)
        self.assertIn("https://cdn.example/hero.jpg?w=1200&q=80", found)
        self.assertIn("https://cdn.example/pic.jpg?a=1&b=2", found)

        # Single-pass prevents /assets/assets/... clobbering
        res_single = rewrite_urls_longest_first(
            "https://site.example/fonts/inter.woff2",
            {
                "https://site.example/fonts/inter.woff2": "/assets/fonts/inter.woff2",
                "/fonts/inter.woff2": "/assets/fonts/inter.woff2",
            },
        )
        self.assertEqual(res_single, "/assets/fonts/inter.woff2")

        # Relative CSS rewriting restricted strictly to url(...) / @import (never raw text/comments)
        css_rel = '/* inter.woff2 comment */ .inter-woff2{src:url("./inter.woff2");}'
        rewritten_css = rewrite_css_assets(
            css_rel,
            {"https://site.example/fonts/inter.woff2": "/assets/fonts/inter.woff2"},
            css_remote_url="https://site.example/fonts/style.css",
        )
        self.assertIn("/* inter.woff2 comment */", rewritten_css)
        self.assertIn('url("/assets/fonts/inter.woff2")', rewritten_css)
        self.assertNotIn("/assets/assets/", rewritten_css)

        # vendor_assets refreshes CSS bytes + sha256 + totalBytes after rewriting
        store = {
            "https://cdn.example/app.css": (
                200,
                {"Content-Type": "text/css"},
                b'body{background:url("https://cdn.example/hero.jpg?w=1200&q=80");}',
            ),
            "https://cdn.example/hero.jpg?w=1200&q=80": (
                200,
                {"Content-Type": "image/jpeg"},
                b"JPGDATA",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            _write(src, "index.html", '<link rel="stylesheet" href="https://cdn.example/app.css">')
            out = root / "out"
            vendor_assets(
                source_dir=src,
                output_dir=out,
                allow_hosts=["cdn.example"],
                resolver=PUB_RESOLVER,
                transport=lambda u, _ips, _h, _t: store.get(u, (404, {}, b"")),
            )
            amap_data = json.loads((out / "asset-map.json").read_text(encoding="utf-8"))
            css_rec = next(r for r in amap_data["assets"] if r["localPublicPath"].endswith(".css"))
            disk_bytes = (out / "public" / css_rec["localPublicPath"].lstrip("/")).read_bytes()
            self.assertEqual(css_rec["bytes"], len(disk_bytes))
            self.assertEqual(css_rec["sha256"], hashlib.sha256(disk_bytes).hexdigest())
            self.assertEqual(amap_data["totalBytes"], sum(r["bytes"] for r in amap_data["assets"]))

    # ------------------------------------------------------------------
    # M-05: Windows cp1252 UTF-8 subprocess handling
    # ------------------------------------------------------------------
    def test_m05_utf8_subprocess_encoding_and_none_stdout_guard(self) -> None:
        fake_proc = mock.MagicMock(returncode=1, stdout=None, stderr=None)
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            with mock.patch("legends_astro.twin.subprocess.run", return_value=fake_proc) as m_run:
                rc, payload = run_twin(module_root=MODULE_ROOT, output_dir=Path(tmp) / "out", dist_dir=dist, overwrite=True)
                self.assertEqual(rc, 1)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["stdout"], "")
                self.assertEqual(m_run.call_args.kwargs.get("encoding"), "utf-8")
                self.assertEqual(m_run.call_args.kwargs.get("errors"), "replace")
                self.assertIn("--overwrite", m_run.call_args.args[0])

        with mock.patch("legends_astro.cli.subprocess.run", return_value=fake_proc) as m_doc:
            rep = _run_node_doctor(None)
            self.assertFalse(rep["ok"])
            self.assertEqual(m_doc.call_args.kwargs.get("encoding"), "utf-8")
            self.assertEqual(m_doc.call_args.kwargs.get("errors"), "replace")

    # ------------------------------------------------------------------
    # M-06: Duplicate slugs, editorial 404/OG/JSON-LD, sitemap draft precision
    # ------------------------------------------------------------------
    def test_m06_duplicate_slugs_editorial_404_og_jsonld_and_draft_precision(self) -> None:
        wxr = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
    <item><title>Launch 2023</title><link>https://ex.com/2023/launch/</link><wp:post_name>launch</wp:post_name><wp:post_type>post</wp:post_type><wp:status>publish</wp:status><content:encoded><![CDATA[<p>A</p>]]></content:encoded></item>
    <item><title>Launch 2024</title><link>https://ex.com/2024/launch/</link><wp:post_name>launch</wp:post_name><wp:post_type>post</wp:post_type><wp:status>publish</wp:status><content:encoded><![CDATA[<p>B</p>]]></content:encoded></item>
    <item><title>Launch Explicit 2</title><link>https://ex.com/launch-2/</link><wp:post_name>launch-2</wp:post_name><wp:post_type>post</wp:post_type><wp:status>publish</wp:status><content:encoded><![CDATA[<p>C</p>]]></content:encoded></item>
    <item><title>Launch 2025</title><link>https://ex.com/2025/launch/</link><wp:post_name>launch</wp:post_name><wp:post_type>post</wp:post_type><wp:status>publish</wp:status><content:encoded><![CDATA[<p>D</p>]]></content:encoded></item>
</channel>
</rss>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_p = _write(root, "dup.xml", wxr)

            # ingest deduplicates all 4 without FileExistsError
            ing_out = root / "ing"
            prev = ingest_source_to_astro(xml_p, ing_out, apply=False)
            ingest_source_to_astro(xml_p, ing_out, apply=True, confirm_plan=prev["planSha256"])
            md_files = sorted(p.name for p in (ing_out / "src" / "content" / "pages").glob("*.md"))
            self.assertEqual(len(md_files), 4)
            self.assertEqual(len(set(md_files)), 4)

            # Editorial mode handles 404 without [..slug] conflict and preserves OG + JSON-LD
            src_html = root / "html-site"
            _write(
                src_html,
                "index.html",
                '<!doctype html><html><head><title>Home</title>'
                '<meta property="og:title" content="Home OG">'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>'
                "</head><body><h1>Home</h1></body></html>",
            )
            _write(
                src_html,
                "404.html",
                '<!doctype html><html><head><title>Custom Missing Page</title>'
                '<meta name="description" content="Gone"></head><body><h1>Missing</h1></body></html>',
            )
            ed_out = root / "ed"
            ed_prev = scaffold_astro_project(source_path=src_html, output_dir=ed_out, mode="editorial", apply=False)
            scaffold_astro_project(source_path=src_html, output_dir=ed_out, mode="editorial", apply=True, confirm_plan=ed_prev["planSha256"])
            self.assertFalse((ed_out / "src" / "content" / "pages" / "404.md").exists())
            page_404 = (ed_out / "src" / "pages" / "404.astro").read_text(encoding="utf-8")
            self.assertIn("Custom Missing Page", page_404)
            base_layout = (ed_out / "src" / "layouts" / "BaseLayout.astro").read_text(encoding="utf-8")
            self.assertIn("application/ld+json", base_layout)
            self.assertIn("openGraph", base_layout)
            idx_md = (ed_out / "src" / "content" / "pages" / "index.md").read_text(encoding="utf-8")
            self.assertIn('"og:title": "Home OG"', idx_md)
            self.assertIn('"Organization"', idx_md)

            # Draft sitemap precision: nested draft docs/v1/overview.md must NOT false-positive on /guides/v2/overview/
            proj = root / "proj"
            _write(
                proj,
                "src/content/docs/v1/overview.md",
                '---\ntitle: "Draft V1"\nlegacyRoute: "/docs/v1/overview/"\ndraft: true\n---\nUnique secret draft body line.\n',
            )
            dist = root / "dist"
            _write(dist, "guides/v2/overview/index.html", "<!doctype html><html><head><title>V2</title></head><body><h1>V2</h1></body></html>")
            _write(
                dist,
                "sitemap-0.xml",
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://site.example/guides/v2/overview/</loc></url></urlset>",
            )
            ver_ok = verify_build(dist, project_dir=proj)
            self.assertTrue(ver_ok["ok"], f"False positive draft failure on unrelated route: {ver_ok['failures']}")

            # Real leak of /docs/v1/overview/ in sitemap + dist MUST fail
            _write(dist, "docs/v1/overview/index.html", "<!doctype html><html><head><title>Leaked</title></head><body><h1>Leaked</h1></body></html>")
            _write(
                dist,
                "sitemap-0.xml",
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://site.example/docs/v1/overview/</loc></url></urlset>",
            )
            ver_bad = verify_build(dist, project_dir=proj)
            codes = {f["code"] for f in ver_bad["failures"]}
            self.assertIn("draft_route_leaked", codes)
            self.assertIn("sitemap_draft_leaked", codes)

    # ------------------------------------------------------------------
    # L-01, L-02, L-03, N-01
    # ------------------------------------------------------------------
    def test_l01_l02_l03_n01_hardening(self) -> None:
        # L-01: _fetch_bounded_json uses safe_fetch_bytes when opener is None
        fake_res = FetchResult(
            url="https://registry.npmjs.org/astro/latest",
            final_url="https://registry.npmjs.org/astro/latest",
            status=200,
            content_type="application/json",
            headers={},
            body=b'{"version": "7.4.13"}',
            redirects=[],
        )
        with mock.patch("legends_astro.docs_nav.safe_fetch_bytes", return_value=fake_res) as m_fetch:
            data = _fetch_bounded_json("https://registry.npmjs.org/astro/latest")
            self.assertEqual(data["version"], "7.4.13")
            self.assertEqual(m_fetch.call_args.kwargs.get("allowed_hosts"), ALLOWED_LIVE_HOSTS)
            self.assertIs(m_fetch.call_args.kwargs.get("allow_http"), False)

        # L-02: MANIFEST.in, setup.py, check_release.py include CAPABILITIES.md & canary
        manifest_txt = (MODULE_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("include CAPABILITIES.md", manifest_txt)
        self.assertIn("graft canary", manifest_txt)
        self.assertIn("prune canary/node_modules", manifest_txt)
        setup_txt = (MODULE_ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"CAPABILITIES.md"', setup_txt)
        chk_txt = (MODULE_ROOT / "scripts" / "check_release.py").read_text(encoding="utf-8")
        self.assertIn('"CAPABILITIES.md"', chk_txt)

        # L-03: strip embedded credentials from originUrl/baseUrl & canonicalize //host routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fc = _write(
                root,
                "fc.json",
                json.dumps(
                    {
                        "metadata": {"sourceURL": "https://admin:topsecret@client.example:8443/home/"},
                        "html": "<!doctype html><html><head><title>Home</title></head><body><h1>Hi</h1></body></html>",
                    }
                ),
            )
            cap = capture_site(output_dir=root / "cap", from_firecrawl=fc)
            manifest = json.loads(Path(cap["manifestPath"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["originUrl"], "https://client.example:8443")
            self.assertNotIn("topsecret", manifest["originUrl"])

            contract_fc = audit_source(fc)
            self.assertEqual(contract_fc["baseUrl"], "https://client.example:8443")
            self.assertNotIn("topsecret", contract_fc["baseUrl"])

            # Protocol-relative Hugo url: //evil.example/pwn canonicalized to single /
            hugo = root / "hugo-sec"
            _write(hugo, "hugo.toml", 'title = "Sec"\n')
            _write(hugo, "content/posts/p.md", '---\ntitle: "Test"\nurl: "//evil.example/pwn"\n---\nBody\n')
            c_hugo = audit_source(hugo)
            for r in c_hugo["frameworkMigration"]["routes"]:
                self.assertFalse(r["route"].startswith("//"), f"Protocol-relative route leaked: {r['route']}")

        # N-01: Windows reserved device stems prefixed + metadata updated
        self.assertEqual(sanitize_rel_path("/assets/aux.js"), "assets/_aux.js")
        self.assertEqual(sanitize_rel_path("/con/nul.png"), "_con/_nul.png")
        self.assertEqual(sanitize_rel_path("/com1.css"), "_com1.css")
        self.assertEqual(sanitize_rel_path("/lpt9"), "_lpt9")
        self.assertEqual(sanitize_rel_path("/assets/console.js"), "assets/console.js")

        for meta_rel in ("pyproject.toml", "package.json", "docs/catalog-entry.candidate.json"):
            txt = (MODULE_ROOT / meta_rel).read_text(encoding="utf-8").lower()
            self.assertIn("nextjs", txt.replace("next.js", "nextjs"))
            self.assertIn("visual digital twin", txt)

    # ------------------------------------------------------------------
    # VB-01, VB-02, VB-03: Variety Battle-Test Fixes
    # ------------------------------------------------------------------
    def test_vb01_root_relative_same_origin_asset_rewriting_and_sri_cleanup(self) -> None:
        html_doc = (
            '<!doctype html><html><head><title>Modern Site</title>'
            '<link rel="stylesheet" href="/css/styles.min.abc.css" integrity="sha256-deadbeef" crossorigin="anonymous">'
            '<script src="/_next/static/chunks/app.js" integrity="sha384-cafebabe" crossorigin="anonymous"></script>'
            '<style>.hero{background-image:url("/img/bg.png");}</style>'
            "</head><body>"
            '<a href="/css/styles.min.abc.css">Download stylesheet</a>'
            "<p>Source path is /css/styles.min.abc.css in prose.</p>"
            '<img src="/_next/image?url=%2Fstatic%2Fhero.png&amp;w=750&amp;q=75" '
            'srcset="/_next/image?url=%2Fstatic%2Fhero.png&amp;w=384&amp;q=75 1x, /_next/image?url=%2Fstatic%2Fhero.png&amp;w=750&amp;q=75 2x">'
            "</body></html>"
        )
        amap = {
            "https://gohugo.io/css/styles.min.abc.css": "/assets/css/styles.min.abc.css",
            "https://gohugo.io/_next/static/chunks/app.js": "/assets/_next/static/chunks/app.js",
            "https://gohugo.io/img/bg.png": "/assets/img/bg.png",
            "https://gohugo.io/_next/image?url=%2Fstatic%2Fhero.png&w=384&q=75": "/assets/_next/image-11111111.png",
            "https://gohugo.io/_next/image?url=%2Fstatic%2Fhero.png&w=750&q=75": "/assets/_next/image-22222222.png",
        }
        rewritten = rewrite_html_assets(html_doc, amap, origin_url="https://gohugo.io")
        self.assertIn('href="/assets/css/styles.min.abc.css"', rewritten)
        self.assertIn('src="/assets/_next/static/chunks/app.js"', rewritten)
        self.assertIn('url("/assets/img/bg.png")', rewritten)
        self.assertIn('src="/assets/_next/image-22222222.png"', rewritten)
        self.assertIn('srcset="/assets/_next/image-11111111.png 1x, /assets/_next/image-22222222.png 2x"', rewritten)
        # Local SRI + crossorigin stripped
        self.assertNotIn("sha256-deadbeef", rewritten)
        self.assertNotIn("sha384-cafebabe", rewritten)
        self.assertNotIn("crossorigin", rewritten)
        # <a href> navigation link and bare prose MUST NOT be clobbered
        self.assertIn('<a href="/css/styles.min.abc.css">Download stylesheet</a>', rewritten)
        self.assertIn("<p>Source path is /css/styles.min.abc.css in prose.</p>", rewritten)

    def test_vb02_page_html_inspector_ignores_svg_title_and_duplicate_titles(self) -> None:
        html_doc = (
            "<!doctype html><html><head>"
            "<title>WordPress News | Official</title>"
            "<title>Duplicate Head Title</title>"
            "</head><body>"
            '<svg viewBox="0 0 24 24"><title>WordPress.org</title><path d="M0 0h24v24H0z"/></svg>'
            "<svg><title>Code is Poetry</title></svg>"
            "<title>Stray Body Title</title>"
            "<h1>Hello</h1></body></html>"
        )
        inspected = inspect_html_text(html_doc)
        self.assertEqual(inspected.title, "WordPress News | Official")

    def test_vb03_extensionless_dynamic_image_endpoint_infers_content_type_ext(self) -> None:
        store = {
            "https://nextjs.org/_next/image?url=%2Fstatic%2Fhero.png&w=384&q=75": (
                200,
                {"Content-Type": "image/png"},
                b"\x89PNG\r\n\x1a\nfake",
            ),
            "https://nextjs.org/_next/image?url=%2Fstatic%2Fhero.webp&w=750&q=75": (
                200,
                {"Content-Type": "image/webp; charset=binary"},
                b"RIFFxxxxWEBP",
            ),
            "https://nextjs.org/api/font": (
                200,
                {"Content-Type": "font/woff2"},
                b"wOF2font",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "raw"
            _write(
                src,
                "index.html",
                '<!doctype html><html><head>'
                '<link rel="preload" as="font" href="/api/font">'
                "</head><body>"
                '<img src="/_next/image?url=%2Fstatic%2Fhero.png&amp;w=384&amp;q=75">'
                '<img src="/_next/image?url=%2Fstatic%2Fhero.webp&amp;w=750&amp;q=75">'
                "</body></html>",
            )
            out = root / "astro-out"
            res = vendor_assets(
                source_dir=src,
                output_dir=out,
                allow_hosts=["nextjs.org"],
                origin_url="https://nextjs.org",
                resolver=PUB_RESOLVER,
                transport=lambda u, _ips, _h, _t: store.get(u, (404, {}, b"")),
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["assetCount"], 3)
            amap_data = json.loads((out / "asset-map.json").read_text(encoding="utf-8"))
            amap = amap_data["map"]
            png_path = amap["https://nextjs.org/_next/image?url=%2Fstatic%2Fhero.png&w=384&q=75"]
            webp_path = amap["https://nextjs.org/_next/image?url=%2Fstatic%2Fhero.webp&w=750&q=75"]
            font_path = amap["https://nextjs.org/api/font"]
            self.assertTrue(png_path.endswith(".png"), f"Expected .png suffix, got {png_path}")
            self.assertTrue(webp_path.endswith(".webp"), f"Expected .webp suffix, got {webp_path}")
            self.assertTrue(font_path.endswith(".woff2"), f"Expected .woff2 suffix, got {font_path}")
            self.assertTrue((out / "public" / png_path.lstrip("/")).is_file())
            self.assertTrue((out / "public" / webp_path.lstrip("/")).is_file())
            self.assertTrue((out / "public" / font_path.lstrip("/")).is_file())
            # Also verify rootRelativeMap populated for VB-01
            self.assertIn("/_next/image?url=%2Fstatic%2Fhero.png&w=384&q=75", amap_data["rootRelativeMap"])


if __name__ == "__main__":
    unittest.main()
