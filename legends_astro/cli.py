"""Command-line interface for legends-astro."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import LEGACY_ALIAS, MODULE_NAME, __version__
from .auditor import audit_source, ingest_source_to_astro, verify_build
from .capture import capture_site
from .docs_nav import lookup_docs, probe_live_ecosystem, verify_knowledge_pack
from .net_safety import DEFAULT_MAX_ASSET_BYTES, DEFAULT_MAX_PAGE_BYTES, DEFAULT_MAX_TOTAL_BYTES, safe_write_text
from .publisher import publish_post
from .scaffold import scaffold_astro_project
from .twin import diff_twins, run_twin
from .vendor import vendor_assets


def _resolve_module_root(anchor: Path | None = None) -> Path:
    env_override = os.environ.get("LEGENDS_ASTRO_ROOT")
    if env_override:
        cand = Path(env_override).resolve()
        if (cand / "scripts" / "doctor.mjs").is_file():
            return cand
    here = (anchor or Path(__file__)).resolve().parent
    data_dir = here / "_data"
    if (data_dir / "scripts" / "doctor.mjs").is_file() and (data_dir / "knowledge" / "astro" / "pack.json").is_file():
        return data_dir
    direct = here.parent
    if (direct / "scripts" / "doctor.mjs").is_file() and (direct / "knowledge" / "astro" / "pack.json").is_file():
        return direct
    for parent in here.parents:
        candidate = parent / "source"
        if (candidate / "scripts" / "doctor.mjs").is_file() and (candidate / "knowledge" / "astro" / "pack.json").is_file():
            return candidate
    return direct


MODULE_ROOT = _resolve_module_root()


def resolve_resources(anchor: Path | None = None) -> dict[str, Any]:
    root = _resolve_module_root(anchor=anchor)
    here = (anchor or Path(__file__)).resolve().parent
    source = "package_data" if root == (here / "_data").resolve() else "checkout"
    recipe = root / "docs" / "AGENT-RECIPE.md"
    pack_manifest = root / "knowledge" / "astro" / "pack.json"
    complete = (
        recipe.is_file()
        and pack_manifest.is_file()
        and (root / "scripts" / "doctor.mjs").is_file()
        and (root / "scripts" / "twin.mjs").is_file()
    )
    return {
        "ok": complete,
        "module": MODULE_NAME,
        "version": __version__,
        "source": source,
        "root": str(root),
        "recipe": str(recipe),
        "pack_manifest": str(pack_manifest),
        "complete": complete,
    }


def _run_node_doctor(project: str | None, strict_pin: bool = False) -> dict[str, Any]:
    node_bin = shutil.which("node")
    doctor_script = MODULE_ROOT / "scripts" / "doctor.mjs"
    if not node_bin:
        return {
            "ok": False,
            "residuals": [{"code": "node_missing", "message": "Node.js binary not found on PATH."}],
        }
    cmd = [node_bin, str(doctor_script), "--json"]
    if project:
        cmd.extend(["--project", str(Path(project).resolve())])
    else:
        cmd.append("--module")
    if strict_pin:
        cmd.append("--strict-pin")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    try:
        return json.loads(stdout_text)
    except Exception:
        return {
            "ok": False,
            "residuals": [
                {
                    "code": "doctor_parse_error",
                    "message": (stderr_text or stdout_text or "Failed to parse doctor output").strip(),
                }
            ],
        }


def _check_astronomer_collision() -> dict[str, Any]:
    astro_on_path = shutil.which("astro")
    if not astro_on_path:
        return {"globalAstroOnPath": False, "path": None, "warning": None}
    return {
        "globalAstroOnPath": True,
        "path": astro_on_path,
        "warning": (
            f"Global 'astro' binary found on PATH ({astro_on_path}). "
            "On Windows winget this is often Astronomer Airflow CLI, and official Astro forbids global installs. "
            "Always run `node ./node_modules/astro/bin/astro.mjs` inside the project."
        ),
    }


def _flatten_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in values or []:
        for part in str(item).split(","):
            clean = part.strip()
            if clean and clean not in out:
                out.append(clean)
    return out


def _detect_ecosystem(project: str | None = None) -> dict[str, Any]:
    """Read-only, non-blocking detection of optional ecosystem tools (legends-firecrawl, Playwright)."""
    firecrawl_bin = shutil.which("legends-firecrawl")
    firecrawl_spec = importlib.util.find_spec("legends_firecrawl") is not None
    firecrawl_sibling = (MODULE_ROOT.parent / "legends-firecrawl").is_dir()
    firecrawl_detected = bool(firecrawl_bin or firecrawl_spec or firecrawl_sibling)

    pw_candidates: list[Path] = []
    env_pw = os.environ.get("PLAYWRIGHT_PACKAGE_JSON")
    if env_pw:
        pw_candidates.append(Path(env_pw).expanduser())
    if project:
        pw_candidates.append(Path(project).expanduser() / "node_modules" / "playwright" / "package.json")
    pw_candidates.append(MODULE_ROOT / "node_modules" / "playwright" / "package.json")
    pw_candidates.append(Path.cwd() / "node_modules" / "playwright" / "package.json")
    pw_found = next((p for p in pw_candidates if p.is_file()), None)

    return {
        "optional": True,
        "legendsFirecrawlDetected": firecrawl_detected,
        "legendsFirecrawlHint": (
            "Optional companion detected; use `cto-legends run legends-firecrawl` + `--from-firecrawl` when live HTTP capture hits WAF/bot challenges or empty JS SPA shells."
            if firecrawl_detected
            else "Optional: install via `cto-legends install legends-firecrawl` if migrating WAF-protected or client-rendered JS SPA sites."
        ),
        "playwrightAvailable": pw_found is not None,
        "playwrightHint": (
            f"Optional Playwright package detected ({pw_found}); ready for `legends-astro twin`."
            if pw_found is not None
            else "Optional: install `playwright` locally or pass `--playwright-from <dir>` when running Visual Digital Twin (`twin`) captures."
        ),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    node_report = _run_node_doctor(args.project, strict_pin=args.strict_pin)
    pack_report = verify_knowledge_pack(MODULE_ROOT)
    collision = _check_astronomer_collision()
    ecosystem = _detect_ecosystem(args.project)

    live_report = probe_live_ecosystem(MODULE_ROOT) if args.live else None
    ok = bool(node_report.get("ok")) and bool(pack_report.get("ok"))
    if args.live and live_report and not live_report.get("ok"):
        ok = False

    payload: dict[str, Any] = {
        "ok": ok,
        "module": MODULE_NAME,
        "legacyAlias": LEGACY_ALIAS,
        "version": __version__,
        "runtime": node_report,
        "pathCollisionCheck": collision,
        "knowledgePack": pack_report,
        "ecosystem": ecosystem,
    }
    if live_report is not None:
        payload["liveEcosystem"] = live_report

    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if ok else 1


def cmd_capture(args: argparse.Namespace) -> int:
    report = capture_site(
        output_dir=args.output,
        url=args.url,
        from_firecrawl=args.from_firecrawl,
        from_sitemap=args.from_sitemap,
        sitemap_url=args.sitemap_url,
        crawl=args.crawl,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        max_page_bytes=args.max_page_bytes,
        max_total_bytes=args.max_total_bytes,
        allow_hosts=_flatten_csv(args.allow_host),
        allow_http=args.allow_http,
        overwrite=args.overwrite,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_vendor_assets(args: argparse.Namespace) -> int:
    report = vendor_assets(
        source_dir=args.source,
        output_dir=args.output,
        allow_hosts=_flatten_csv(args.allow_host),
        origin_url=args.origin,
        asset_map_path=args.asset_map,
        allow_http=args.allow_http,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        max_asset_bytes=args.max_asset_bytes,
        max_total_bytes=args.max_total_bytes,
        concurrency=args.concurrency,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_scaffold(args: argparse.Namespace) -> int:
    report = scaffold_astro_project(
        source_path=args.source,
        output_dir=args.output,
        mode=args.mode,
        asset_map_path=args.asset_map,
        decode_cf_email=args.decode_cf_email,
        staging_lock=args.staging_lock,
        strip_trackers_flag=args.strip_trackers,
        site_url=args.site,
        name=args.name,
        preset=args.preset,
        trailing_slash=args.trailing_slash,
        with_sitemap=args.with_sitemap,
        apply=args.apply,
        confirm_plan=args.confirm_plan,
        overwrite=args.overwrite,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_init(args: argparse.Namespace) -> int:
    report = scaffold_astro_project(
        source_path=None,
        output_dir=args.output,
        mode="starter",
        staging_lock=args.staging_lock,
        site_url=args.site,
        name=args.name,
        preset=args.preset,
        trailing_slash=args.trailing_slash,
        with_sitemap=args.with_sitemap,
        apply=args.apply,
        confirm_plan=args.confirm_plan,
        overwrite=args.overwrite,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_post(args: argparse.Namespace) -> int:
    tags = _flatten_csv(args.tags) if args.tags is not None else None
    categories = _flatten_csv(args.categories) if args.categories is not None else None
    report = publish_post(
        project_dir=args.project,
        collection=args.collection,
        content_dir_override=args.content_dir,
        list_only=args.list_entries,
        update=args.update,
        rename_from=args.rename_from,
        input_path=args.input,
        body_file=args.body_file,
        slug=args.slug,
        title=args.title,
        description=args.description,
        author=args.author,
        tags=tags,
        categories=categories,
        pub_date=args.pub_date,
        updated_date=args.updated_date,
        no_updated_date=args.no_updated_date,
        hero=args.hero,
        hero_alt=args.hero_alt,
        draft_flag=args.draft,
        publish_flag=args.publish,
        strip_body_h1=args.strip_body_h1,
        strict_typography=args.strict_typography,
        field_map_overrides=args.field_map,
        apply=args.apply,
        confirm_plan=args.confirm_plan,
        overwrite=args.overwrite,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_twin(args: argparse.Namespace) -> int:
    rc, report = run_twin(
        module_root=MODULE_ROOT,
        output_dir=args.output,
        url=args.url,
        dist_dir=args.dist,
        routes=_flatten_csv(args.route),
        allow_hosts=_flatten_csv(args.allow_host),
        crops=args.crop or [],
        timeout_ms=args.timeout_ms,
        no_mobile=args.no_mobile,
        no_full_page=args.no_full_page,
        allow_http=args.allow_http,
        overwrite=args.overwrite,
        playwright_from=args.playwright_from,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if rc == 0 and report.get("ok") else (rc or 1)


def cmd_twin_diff(args: argparse.Namespace) -> int:
    report = diff_twins(
        baseline_path=args.baseline,
        candidate_path=args.candidate,
        max_section_y_delta=args.max_section_y_delta,
        max_section_height_delta=args.max_section_height_delta,
        output_path=args.output,
        overwrite=args.overwrite,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_source_audit(args: argparse.Namespace) -> int:
    contract = audit_source(args.source, base_url=args.base_url)
    if args.output:
        out_path = safe_write_text(
            args.output,
            json.dumps(contract, indent=2) + "\n",
            overwrite=getattr(args, "overwrite", False),
        )
        contract["writtenTo"] = str(out_path)
    sys.stdout.write(json.dumps(contract, indent=2) + "\n")
    return 0


def cmd_verify_build(args: argparse.Namespace) -> int:
    report = verify_build(
        dist_dir=args.dist,
        project_dir=args.project,
        contract_path=args.contract,
        forbid_hosts=_flatten_csv(args.forbid_host),
        forbid_host_strict=args.forbid_host_strict,
        env=args.env,
        baseline_path=args.baseline,
        write_manifest_path=args.write_manifest,
        overwrite=args.overwrite,
        allow_canonical_change=args.allow_canonical_change,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    report = ingest_source_to_astro(
        source_path=args.source,
        output_dir=args.output,
        apply=args.apply,
        confirm_plan=getattr(args, "confirm_plan", None),
        overwrite=getattr(args, "overwrite", False),
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


def cmd_docs_lookup(args: argparse.Namespace) -> int:
    report = lookup_docs(
        query=args.query,
        list_all=args.list_all,
        include_live_locations=args.live_locations,
        module_root=MODULE_ROOT,
    )
    if args.check_live:
        report["liveProbe"] = probe_live_ecosystem(MODULE_ROOT)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


def cmd_knowledge_pack(args: argparse.Namespace) -> int:
    report = verify_knowledge_pack(MODULE_ROOT)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_resources(_args: argparse.Namespace) -> int:
    report = resolve_resources()
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legends-astro",
        description="End-to-end multi-framework, WordPress, Webflow, and HTML to Astro v7.4 migration, Visual Digital Twin, build parity verification, and curated docs CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_doc = sub.add_parser("doctor", help="Inspect module readiness, Node runtime, Astro project pin/lock, and optional live upstream drift.")
    p_doc.add_argument("--project", help="Path to an Astro consumer project to inspect.")
    p_doc.add_argument("--strict-pin", action="store_true", help="Enforce exact canary pin astro@7.4.13 and Node 24.15.0.")
    p_doc.add_argument("--live", action="store_true", help="Query live npm registry and GitHub withastro/docs for latest version drift.")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    p_doc.set_defaults(func=cmd_doctor)

    p_cap = sub.add_parser("capture", help="Capture live HTML pages via sitemap/BFS crawl or normalize local legends-firecrawl archives with Migration Triage.")
    p_cap.add_argument("--url", help="Live origin or seed URL to capture (https:// default).")
    p_cap.add_argument("--from-firecrawl", help="Local Firecrawl JSON file or capture directory to normalize.")
    p_cap.add_argument("--output", required=True, help="Target output directory for raw-capture/ and migration-contract.json.")
    p_cap.add_argument("--from-sitemap", action="store_true", help="Discover URLs recursively from sitemap_index.xml / sitemap.xml.")
    p_cap.add_argument("--sitemap-url", help="Explicit XML sitemap URL to parse.")
    p_cap.add_argument("--crawl", action="store_true", help="Enable bounded BFS HTML link crawling.")
    p_cap.add_argument("--max-pages", type=int, default=50, help="Maximum HTML pages to capture (default: 50).")
    p_cap.add_argument("--max-depth", type=int, default=2, help="Maximum BFS crawl depth (default: 2).")
    p_cap.add_argument("--max-page-bytes", type=int, default=DEFAULT_MAX_PAGE_BYTES, help="Max bytes per HTML/XML response.")
    p_cap.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES, help="Max total bytes across capture.")
    p_cap.add_argument("--allow-host", action="append", default=[], help="Additional allowed hostname(s) (repeatable or comma-separated).")
    p_cap.add_argument("--allow-http", action="store_true", help="Allow plain http:// URLs (https:// is enforced by default).")
    p_cap.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")
    p_cap.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_cap.set_defaults(func=cmd_capture)

    p_ven = sub.add_parser("vendor-assets", help="Two-pass HTML + recursive CSS @import/url() asset localizer and asset-map.json generator.")
    p_ven.add_argument("--source", required=True, help="Directory (e.g. raw-capture/) or HTML file to scan for assets.")
    p_ven.add_argument("--output", required=True, help="Target Astro project directory (writes public/ and asset-map.json).")
    p_ven.add_argument("--allow-host", action="append", required=True, help="Explicit allowed asset hostname(s) (origin, WP, or Webflow CDNs).")
    p_ven.add_argument("--origin", help="Optional source site origin URL to resolve relative asset paths.")
    p_ven.add_argument("--asset-map", help="Optional custom output path for asset-map.json (default: <output>/asset-map.json).")
    p_ven.add_argument("--concurrency", type=int, default=6, help="Bounded concurrent asset downloads (default: 6, max: 16).")
    p_ven.add_argument("--allow-http", action="store_true", help="Allow plain http:// asset fetches.")
    p_ven.add_argument("--dry-run", action="store_true", help="Preview discovered assets and local destinations without downloading.")
    p_ven.add_argument("--overwrite", action="store_true", help="Overwrite existing vendored assets and asset-map.json.")
    p_ven.add_argument("--max-asset-bytes", type=int, default=DEFAULT_MAX_ASSET_BYTES, help="Max bytes per asset response.")
    p_ven.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES, help="Max total bytes across all assets.")
    p_ven.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ven.set_defaults(func=cmd_vendor_assets)

    p_init = sub.add_parser("init", help="Initialize a greenfield Astro v7.4.13 agency, blog, or local-business starter project (preview-first).")
    p_init.add_argument("--output", required=True, help="Target Astro project directory.")
    p_init.add_argument("--name", help="Human-readable site or brand name (default: derived from --output directory name).")
    p_init.add_argument("--site", help="Optional canonical site URL for astro.config.mjs, canonicals, RSS, and sitemap.")
    p_init.add_argument("--preset", choices=["agency", "blog", "local-business"], default="agency", help="Starter preset: agency, blog, or local-business (default: agency).")
    p_init.add_argument("--trailing-slash", choices=["always", "never", "ignore"], default="always", help="Astro trailingSlash & build.format alignment (default: always).")
    p_init_sm = p_init.add_mutually_exclusive_group()
    p_init_sm.add_argument("--with-sitemap", dest="with_sitemap", action="store_true", default=None, help="Wire @astrojs/sitemap in package.json and astro.config.mjs.")
    p_init_sm.add_argument("--no-sitemap", dest="with_sitemap", action="store_false", help="Omit @astrojs/sitemap from starter project.")
    p_init.add_argument("--staging-lock", action="store_true", help="Inject Triple-Lock staging (robots.txt Disallow, meta noindex, public/_headers X-Robots-Tag).")
    p_init.add_argument("--apply", action="store_true", help="Write planned starter files to disk (requires --confirm-plan).")
    p_init.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview output when --apply is set.")
    p_init.add_argument("--overwrite", action="store_true", help="Overwrite existing files when --apply is set.")
    p_init.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_init.set_defaults(func=cmd_init)

    p_scaf = sub.add_parser("scaffold", help="Preview-first Astro v7.4.13 project scaffolder (--mode clone | editorial | starter).")
    p_scaf.add_argument("--source", required=False, help="Source raw-capture/, HTML directory, WordPress export, or Firecrawl source (required for clone/editorial).")
    p_scaf.add_argument("--output", required=True, help="Target Astro project directory.")
    p_scaf.add_argument("--mode", choices=["clone", "editorial", "starter"], default="clone", help="Scaffolding mode: clone, editorial, or starter.")
    p_scaf.add_argument("--name", help="Starter mode: site or brand name (default: derived from --output directory name).")
    p_scaf.add_argument("--preset", choices=["agency", "blog", "local-business"], default="agency", help="Starter mode preset: agency, blog, or local-business.")
    p_scaf.add_argument("--trailing-slash", choices=["always", "never", "ignore"], default="always", help="Starter mode trailingSlash alignment (default: always).")
    p_scaf_sm = p_scaf.add_mutually_exclusive_group()
    p_scaf_sm.add_argument("--with-sitemap", dest="with_sitemap", action="store_true", default=None, help="Starter mode: wire @astrojs/sitemap.")
    p_scaf_sm.add_argument("--no-sitemap", dest="with_sitemap", action="store_false", help="Starter mode: omit @astrojs/sitemap.")
    p_scaf.add_argument("--asset-map", help="Optional asset-map.json from vendor-assets to rewrite URLs longest-first and strip local SRI.")
    p_scaf.add_argument("--decode-cf-email", action="store_true", help="Decode Cloudflare email-protection XOR hex spans/links.")
    p_scaf.add_argument("--staging-lock", action="store_true", help="Inject Triple-Lock staging (robots.txt Disallow, meta noindex, public/_headers X-Robots-Tag).")
    p_scaf.add_argument("--strip-trackers", action="store_true", help="Strip live GA/GTM/Hotjar/Wordfence/reCAPTCHA/Turnstile/Cloudflare scripts.")
    p_scaf.add_argument("--site", help="Optional canonical site URL for astro.config.mjs.")
    p_scaf.add_argument("--apply", action="store_true", help="Write planned project files to disk (requires --confirm-plan).")
    p_scaf.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview output when --apply is set.")
    p_scaf.add_argument("--overwrite", action="store_true", help="Overwrite existing files when --apply is set.")
    p_scaf.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_scaf.set_defaults(func=cmd_scaffold)

    p_post = sub.add_parser("post", help="Day-2 article-to-live publisher, updater, safe slug renamer, image localizer, and editorial SEO linter.")
    p_post.add_argument("--project", required=True, help="Target Astro project root directory.")
    p_post.add_argument("--collection", default="blog", help="Target Content Layer collection name (default: blog).")
    p_post.add_argument("--content-dir", help="Optional relative content directory override (e.g. src/content/articles).")
    p_post.add_argument("--list", dest="list_entries", action="store_true", help="Read-only inventory of entries in the resolved collection.")
    p_post.add_argument("--update", action="store_true", help="Update an existing post (preserves pubDate and unknown custom frontmatter keys, stamps updatedDate).")
    p_post.add_argument("--rename-from", help="Safely rename an existing slug to --slug and append a 301 rule in public/_redirects.")
    p_post.add_argument("--input", help="Source Markdown file (with optional YAML frontmatter).")
    p_post.add_argument("--body-file", help="Source Markdown body file.")
    p_post.add_argument("--slug", help="Target post slug (strictly validated lowercase ASCII + hyphens).")
    p_post.add_argument("--title", help="Post title.")
    p_post.add_argument("--description", help="Post meta description.")
    p_post.add_argument("--author", help="Post author name.")
    p_post.add_argument("--tags", action="append", default=None, help="Comma-separated or repeatable post tags.")
    p_post.add_argument("--categories", action="append", default=None, help="Comma-separated or repeatable post categories.")
    p_post.add_argument("--pub-date", help="Publication date (YYYY-MM-DD or ISO-8601; defaults to today UTC on create, preserved on --update).")
    p_post.add_argument("--updated-date", help="Explicit updatedDate (YYYY-MM-DD or ISO-8601).")
    p_post.add_argument("--no-updated-date", action="store_true", help="Do not auto-stamp updatedDate during --update or --rename-from.")
    p_post.add_argument("--hero", help="Local image file path or public URL for hero/featured image.")
    p_post.add_argument("--hero-alt", help="Accessible alt text for hero image.")
    p_post.add_argument("--draft", action="store_true", help="Mark post as draft: true (create mode also defaults to draft: true when --publish, --draft, and input frontmatter draft are all omitted).")
    p_post.add_argument("--publish", action="store_true", help="Mark post as draft: false (ready for production build; overrides the create-mode draft: true default).")
    p_post.add_argument("--strip-body-h1", action="store_true", help="Strip a leading '# Heading' from Markdown body when layout already renders <h1>.")
    p_post.add_argument("--strict-typography", action="store_true", help="Warn on em-dash or en-dash characters in title, description, or body.")
    p_post.add_argument("--field-map", action="append", default=[], help="Override detected schema field mapping as role=customKey (repeatable).")
    p_post.add_argument("--apply", action="store_true", help="Write planned post, localized assets, and redirects (requires --confirm-plan).")
    p_post.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview output when --apply is set.")
    p_post.add_argument("--overwrite", action="store_true", help="Overwrite existing files when --apply is set.")
    p_post.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_post.set_defaults(func=cmd_post)

    p_twin = sub.add_parser("twin", help="Capture Playwright Visual + Computed-Style Digital Twin from live --url or local --dist.")
    p_twin.add_argument("--url", help="Live target URL (https:// default, validated with SSRF guard).")
    p_twin.add_argument("--dist", help="Local built Astro dist/ directory (served via ephemeral in-process 127.0.0.1:0 static server).")
    p_twin.add_argument("--output", required=True, help="Output directory for PNG screenshots, twin.json, and twin-audit-summary.md.")
    p_twin.add_argument("--route", action="append", default=[], help="Specific route(s) to audit (repeatable or comma-separated).")
    p_twin.add_argument("--allow-host", action="append", default=[], help="Allowed host(s) when using --url.")
    p_twin.add_argument("--crop", action="append", default=[], help="Optional CSS selector(s) to capture element crop screenshots on desktop.")
    p_twin.add_argument("--timeout-ms", type=int, default=45000, help="Navigation timeout in milliseconds (default: 45000).")
    p_twin.add_argument("--no-mobile", action="store_true", help="Skip 390x844 mobile viewport capture.")
    p_twin.add_argument("--no-full-page", action="store_true", help="Skip desktop full-page screenshot.")
    p_twin.add_argument("--allow-http", action="store_true", help="Allow plain http:// when using --url.")
    p_twin.add_argument("--overwrite", action="store_true", help="Overwrite existing screenshots, twin.json, and twin-audit-summary.md in --output.")
    p_twin.add_argument("--playwright-from", help="Optional path to a directory or package.json containing playwright.")
    p_twin.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_twin.set_defaults(func=cmd_twin)

    p_tdiff = sub.add_parser("twin-diff", help="Offline parity diff between baseline twin.json and candidate twin.json.")
    p_tdiff.add_argument("--baseline", required=True, help="Baseline twin.json path.")
    p_tdiff.add_argument("--candidate", required=True, help="Candidate twin.json path.")
    p_tdiff.add_argument("--max-section-y-delta", type=int, default=80, help="Threshold in px for section Y shift drift (default: 80).")
    p_tdiff.add_argument("--max-section-height-delta", type=int, default=80, help="Threshold in px for section height drift (default: 80).")
    p_tdiff.add_argument("--output", help="Optional path to save diff JSON report.")
    p_tdiff.add_argument("--overwrite", action="store_true", help="Overwrite --output if it exists.")
    p_tdiff.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_tdiff.set_defaults(func=cmd_twin_diff)

    p_audit = sub.add_parser("source-audit", help="Extract a deterministic migration-contract.json from Next.js/Nuxt/Gatsby/Vite/CRA/Docusaurus/Hugo/Jekyll/Eleventy repos, HTML, raw-capture, WordPress XML/JSON, or Firecrawl sources.")
    p_audit.add_argument("--source", required=True, help="Source framework repo, HTML dir/file, raw-capture/, WordPress .xml/.json, or Firecrawl .json/dir.")
    p_audit.add_argument("--output", help="Optional path to save the extracted migration-contract.json.")
    p_audit.add_argument("--overwrite", action="store_true", help="Overwrite --output file if it already exists.")
    p_audit.add_argument("--base-url", help="Optional canonical base URL of the source site.")
    p_audit.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_audit.set_defaults(func=cmd_source_audit)

    p_ver = sub.add_parser("verify-build", help="Verify an Astro dist/ build for route parity, SEO/schema, DOM ID ABI, drafts, CSS assets, SRI, sitemaps, redirects, env locks, and baseline manifest regressions.")
    p_ver.add_argument("--dist", required=True, help="Path to built Astro dist/ directory.")
    p_ver.add_argument("--project", help="Optional Astro project root to verify draft content entries in src/content/ were excluded.")
    p_ver.add_argument("--contract", help="Optional migration-contract.json from source-audit to verify parity against.")
    p_ver.add_argument("--baseline", help="Optional prior legends.astro-site-manifest/v1 JSON to enforce fail-closed Day-2 regression checks.")
    p_ver.add_argument("--write-manifest", help="Write legends.astro-site-manifest/v1 JSON to this path when verification passes (fail-closed).")
    p_ver.add_argument("--overwrite", action="store_true", help="Allow overwriting an existing --write-manifest file.")
    p_ver.add_argument("--allow-canonical-change", action="store_true", help="Authorize canonical URL changes on surviving routes compared to --baseline.")
    p_ver.add_argument("--forbid-host", action="append", default=[], help="Forbid leftover active asset/embed/CSS references to origin/CDN host(s) in dist/ HTML and CSS.")
    p_ver.add_argument("--forbid-host-strict", action="store_true", help="Scan raw dist/ HTML text (including canonical, JSON-LD, and <a href>) for --forbid-host.")
    p_ver.add_argument("--env", choices=["staging", "production"], help="Verify staging Triple-Lock + zero trackers, or production indexability.")
    p_ver.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ver.set_defaults(func=cmd_verify_build)

    p_ing = sub.add_parser("ingest", help="Preview or scaffold an Astro v7 Content Layer collection and migration contract from legacy sources.")
    p_ing.add_argument("--source", required=True, help="Source HTML directory, WordPress export, or Firecrawl JSON/directory.")
    p_ing.add_argument("--output", required=True, help="Target Astro project directory.")
    p_ing.add_argument("--apply", action="store_true", help="Write generated Content Layer config, Markdown entries, and migration-contract.json.")
    p_ing.add_argument("--confirm-plan", help="Required SHA-256 plan digest from preview output when --apply is set.")
    p_ing.add_argument("--overwrite", action="store_true", help="Allow overwriting existing scaffolded files when --apply is set.")
    p_ing.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ing.set_defaults(func=cmd_ingest)

    p_lookup = sub.add_parser("docs-lookup", help="Search local Astro v7.4 MDX snapshot and operational runbooks, or list verified upstream live doc endpoints.")
    p_lookup.add_argument("query", nargs="?", help="Search query (e.g. 'wordpress', 'fonts', 'content collections', 'is:inline', 'trailingSlash').")
    p_lookup.add_argument("--list", dest="list_all", action="store_true", help="List all vendored official MDX pages and operational runbooks.")
    p_lookup.add_argument("--live-locations", action="store_true", help="Show verified upstream URLs and commands for checking the latest Astro docs and releases.")
    p_lookup.add_argument("--check-live", action="store_true", help="Query npm and GitHub live to compare vendored snapshot against current upstream latest.")
    p_lookup.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_lookup.set_defaults(func=cmd_docs_lookup)

    p_pack = sub.add_parser("knowledge-pack", help="Verify the attachable Empire knowledge pack manifest and SHA-256 digests.")
    p_pack.add_argument("--check", action="store_true", help="Verify pack.json integrity.")
    p_pack.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_pack.set_defaults(func=cmd_knowledge_pack)

    p_res = sub.add_parser("resources", help="Show resolved module root, recipe, and knowledge pack paths.")
    p_res.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_res.set_defaults(func=cmd_resources)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except (ValueError, FileExistsError, FileNotFoundError, PermissionError, OSError, RuntimeError) as exc:
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                        "errorType": type(exc).__name__,
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stderr.write(f"error ({type(exc).__name__}): {exc}\n")
        return 1
