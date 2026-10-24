"""Command-line interface for legends-astro."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import LEGACY_ALIAS, MODULE_NAME, __version__
from .auditor import audit_source, ingest_source_to_astro, verify_build
from .docs_nav import lookup_docs, probe_live_ecosystem, verify_knowledge_pack


MODULE_ROOT = Path(__file__).resolve().parent.parent


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
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {
            "ok": False,
            "residuals": [
                {
                    "code": "doctor_parse_error",
                    "message": (proc.stderr or proc.stdout or "Failed to parse doctor output").strip(),
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


def cmd_doctor(args: argparse.Namespace) -> int:
    node_report = _run_node_doctor(args.project, strict_pin=args.strict_pin)
    pack_report = verify_knowledge_pack(MODULE_ROOT)
    collision = _check_astronomer_collision()

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
    }
    if live_report is not None:
        payload["liveEcosystem"] = live_report

    if args.json or True:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if ok else 1


def cmd_source_audit(args: argparse.Namespace) -> int:
    contract = audit_source(args.source, base_url=args.base_url)
    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        contract["writtenTo"] = str(out_path)
    sys.stdout.write(json.dumps(contract, indent=2) + "\n")
    return 0


def cmd_verify_build(args: argparse.Namespace) -> int:
    report = verify_build(
        dist_dir=args.dist,
        project_dir=args.project,
        contract_path=args.contract,
    )
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report.get("ok") else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    report = ingest_source_to_astro(
        source_path=args.source,
        output_dir=args.output,
        apply=args.apply,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legends-astro",
        description="Astro v7 project readiness, source migration contract extraction, build parity verification, and curated documentation workflows.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_doc = sub.add_parser("doctor", help="Inspect module readiness, Node runtime, Astro project pin/lock, and optional live upstream drift.")
    p_doc.add_argument("--project", help="Path to an Astro consumer project to inspect.")
    p_doc.add_argument("--strict-pin", action="store_true", help="Enforce exact canary pin astro@7.3.1 and Node 24.15.0.")
    p_doc.add_argument("--live", action="store_true", help="Query live npm registry and GitHub withastro/docs for latest version drift.")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output (default).")
    p_doc.set_defaults(func=cmd_doctor)

    p_audit = sub.add_parser("source-audit", help="Extract a deterministic migration-contract.json from HTML, WordPress XML/JSON, or Firecrawl sources.")
    p_audit.add_argument("--source", required=True, help="Source directory or file (.html, WordPress .xml/.json, Firecrawl .json).")
    p_audit.add_argument("--output", help="Optional path to save the extracted migration-contract.json.")
    p_audit.add_argument("--base-url", help="Optional canonical base URL of the source site.")
    p_audit.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_audit.set_defaults(func=cmd_source_audit)

    p_ver = sub.add_parser("verify-build", help="Verify an Astro dist/ build for route parity, SEO/schema preservation, DOM ID ABI, draft exclusion, and asset links.")
    p_ver.add_argument("--dist", required=True, help="Path to built Astro dist/ directory.")
    p_ver.add_argument("--project", help="Optional Astro project root to verify draft content entries in src/content/ were excluded.")
    p_ver.add_argument("--contract", help="Optional migration-contract.json from source-audit to verify parity against.")
    p_ver.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ver.set_defaults(func=cmd_verify_build)

    p_ing = sub.add_parser("ingest", help="Preview or scaffold an Astro v7 Content Layer collection and migration contract from legacy sources.")
    p_ing.add_argument("--source", required=True, help="Source HTML directory, WordPress export, or Firecrawl JSON.")
    p_ing.add_argument("--output", required=True, help="Target Astro project directory.")
    p_ing.add_argument("--apply", action="store_true", help="Write generated Content Layer config, Markdown entries, and migration-contract.json.")
    p_ing.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ing.set_defaults(func=cmd_ingest)

    p_lookup = sub.add_parser("docs-lookup", help="Search local Astro v7 MDX snapshot and operational runbooks, or list verified upstream live doc endpoints.")
    p_lookup.add_argument("query", nargs="?", help="Search query (e.g. 'wordpress', 'content collections', 'is:inline', 'trailingSlash').")
    p_lookup.add_argument("--list", dest="list_all", action="store_true", help="List all vendored official MDX pages and operational runbooks.")
    p_lookup.add_argument("--live-locations", action="store_true", help="Show verified upstream URLs and commands for checking the latest Astro docs and releases.")
    p_lookup.add_argument("--check-live", action="store_true", help="Query npm and GitHub live to compare vendored snapshot against current upstream latest.")
    p_lookup.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_lookup.set_defaults(func=cmd_docs_lookup)

    p_pack = sub.add_parser("knowledge-pack", help="Verify the attachable Empire knowledge pack manifest and SHA-256 digests.")
    p_pack.add_argument("--check", action="store_true", help="Verify pack.json integrity.")
    p_pack.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_pack.set_defaults(func=cmd_knowledge_pack)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))
