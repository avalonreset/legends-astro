"""Offline documentation search, verified upstream live-doc locations, live registry/docs freshness checks, and knowledge pack verification."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path
from typing import Any


MODULE_ROOT = Path(__file__).resolve().parent.parent

OFFICIAL_PACKAGES = (
    "astro",
    "create-astro",
    "@astrojs/check",
    "@astrojs/mdx",
    "@astrojs/sitemap",
    "@astrojs/rss",
    "@astrojs/node",
    "@astrojs/upgrade",
)

UPSTREAM_LOCATIONS: list[dict[str, Any]] = [
    {
        "id": "npm-registry-astro",
        "kind": "registry-json",
        "title": "Official npm registry metadata for astro and @astrojs/* companions",
        "url": "https://registry.npmjs.org/astro/latest",
        "command": "npm view astro version dist-tags.latest engines",
        "useWhen": "Checking the exact current latest Astro version, Node engine floor, and companion package versions (@astrojs/check, @astrojs/mdx, @astrojs/node, @astrojs/sitemap, @astrojs/upgrade).",
        "verifiedStatus": "200 OK (7.4.2 on 2026-10-24; engines.node >=22.23.4)",
    },
    {
        "id": "github-withastro-docs-raw",
        "kind": "github-source-mdx",
        "title": "Official withastro/docs English MDX source repository",
        "url": "https://api.github.com/repos/withastro/docs/commits/main",
        "rawTemplate": "https://raw.githubusercontent.com/withastro/docs/<commit>/src/content/docs/en/<topic>.mdx",
        "useWhen": "Fetching clean Markdown/MDX documentation for any Astro guide, CMS integration, or adapter without HTML noise.",
        "verifiedStatus": "200 OK (commit 0844aab04ea2f3859132ab97b1e9daa01ade76a1 on 2026-10-24)",
    },
    {
        "id": "github-withastro-astro-changelog",
        "kind": "github-changelog",
        "title": "Official Astro package CHANGELOG.md",
        "url": "https://raw.githubusercontent.com/withastro/astro/main/packages/astro/CHANGELOG.md",
        "useWhen": "Inspecting breaking changes, patch fixes, and minor features between 7.3.x and 7.4.x+.",
        "verifiedStatus": "200 OK",
    },
    {
        "id": "astro-docs-mcp",
        "kind": "design-time-mcp",
        "title": "Official Astro Docs MCP endpoint (search_astro_docs)",
        "url": "https://mcp.docs.astro.build/mcp",
        "useWhen": "Design-time semantic search over rolling latest docs when an unvendored third-party integration or adapter is requested. Do not require as a runtime dependency.",
        "verifiedStatus": "Streamable HTTP MCP; exposes single tool `search_astro_docs`",
    },
    {
        "id": "dead-ends-to-avoid",
        "kind": "anti-pattern",
        "title": "Known dead URLs and CLI traps agents must avoid",
        "traps": [
            "https://docs.astro.build/llms.txt and /llms-full.txt return HTTP 404 (verified 2026-10-24). Do not rely on community skills that point there.",
            "Appending .md to https://docs.astro.build/en/... returns HTTP 404. Read local vendor/astro-docs/ MDX or raw.githubusercontent.com/withastro/docs instead.",
            "Windows winget `Astronomer.Astro` ('Astro CLI') is Apache Airflow, not withastro. Always invoke `node ./node_modules/astro/bin/astro.mjs`.",
            "Astro v6/v7 Content Layer uses `src/content.config.ts` with `glob()` from `astro/loaders` and `z` from `astro/zod`. Legacy `src/content/config.ts`, `type: 'content'`, and `entry.render()` fail.",
        ],
    },
]


def load_vendor_manifest(module_root: Path = MODULE_ROOT) -> dict[str, Any]:
    mpath = module_root / "vendor" / "astro-docs" / "manifest.json"
    return json.loads(mpath.read_text(encoding="utf-8"))


def lookup_docs(
    query: str | None = None,
    list_all: bool = False,
    include_live_locations: bool = False,
    module_root: Path = MODULE_ROOT,
) -> dict[str, Any]:
    manifest = load_vendor_manifest(module_root)
    vendor_root = module_root / "vendor" / "astro-docs"
    wiki_root = module_root / "wiki"

    wiki_entries: list[dict[str, Any]] = []
    if wiki_root.is_dir():
        for wf in sorted(wiki_root.glob("*.md")):
            text = wf.read_text(encoding="utf-8", errors="replace")
            first_heading = next(
                (line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")),
                wf.stem,
            )
            wiki_entries.append(
                {
                    "id": f"wiki:{wf.stem}",
                    "title": first_heading,
                    "path": wf.relative_to(module_root).as_posix(),
                    "kind": "operational-runbook",
                }
            )

    vendor_entries = [
        {
            "id": item["id"],
            "title": item["title"],
            "topic": item.get("topic"),
            "path": f"vendor/astro-docs/{item['localPath']}",
            "docsUrl": item.get("docsUrl"),
            "immutableUrl": item.get("immutableUrl"),
            "kind": "official-mdx-snapshot",
        }
        for item in manifest.get("files", [])
        if item.get("origin") == "upstream" and item.get("localPath", "").endswith(".mdx")
    ]

    matches: list[dict[str, Any]] = []
    if query:
        q_terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
        for item in wiki_entries + vendor_entries:
            abs_p = module_root / item["path"]
            content = abs_p.read_text(encoding="utf-8", errors="ignore") if abs_p.is_file() else ""
            haystack_meta = f"{item['id']} {item['title']} {item.get('topic', '')}".lower()
            haystack_full = f"{haystack_meta}\n{content.lower()}"
            score = 0
            for term in q_terms:
                if term in haystack_meta:
                    score += 10
                count = haystack_full.count(term)
                if count > 0:
                    score += min(count, 15)
            if score > 0:
                headings = [
                    line.strip()
                    for line in content.splitlines()
                    if line.startswith("## ") and any(t in line.lower() for t in q_terms)
                ][:5]
                matches.append(
                    {
                        **item,
                        "score": score,
                        "matchingHeadings": headings,
                    }
                )
        matches.sort(key=lambda x: (-x["score"], x["id"]))

    result: dict[str, Any] = {
        "ok": True,
        "snapshotCommit": manifest.get("commit"),
        "snapshotCommitDate": manifest.get("commitDate"),
        "vendoredPageCount": len(vendor_entries),
        "wikiRunbookCount": len(wiki_entries),
    }
    if query:
        result["query"] = query
        result["matches"] = matches[:12]
    if list_all or not query:
        result["wikiRunbooks"] = wiki_entries
        result["vendoredOfficialDocs"] = vendor_entries
    if include_live_locations or not query:
        result["upstreamLocations"] = UPSTREAM_LOCATIONS
    return result


def probe_live_ecosystem(module_root: Path = MODULE_ROOT, timeout: float = 8.0) -> dict[str, Any]:
    """Query npm registry and GitHub withastro/docs for live version and commit freshness."""
    manifest = load_vendor_manifest(module_root)
    packages_report: dict[str, Any] = {}
    errors: list[str] = []

    for pkg in OFFICIAL_PACKAGES:
        url = f"https://registry.npmjs.org/{pkg}/latest"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "legends-astro/0.2.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                packages_report[pkg] = {
                    "latest": data.get("version"),
                    "engines": data.get("engines"),
                }
        except Exception as exc:
            errors.append(f"npm {pkg}: {exc}")

    docs_head: dict[str, Any] | None = None
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/withastro/docs/commits/main",
            headers={"User-Agent": "legends-astro/0.2.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            c_data = json.loads(resp.read().decode("utf-8"))
            docs_head = {
                "latestCommit": c_data.get("sha"),
                "date": c_data.get("commit", {}).get("committer", {}).get("date"),
                "vendoredCommit": manifest.get("commit"),
                "matchesVendored": c_data.get("sha") == manifest.get("commit"),
            }
    except Exception as exc:
        errors.append(f"github withastro/docs: {exc}")

    return {
        "ok": len(errors) == 0,
        "packages": packages_report,
        "docs": docs_head,
        "errors": errors,
    }


def verify_knowledge_pack(module_root: Path = MODULE_ROOT) -> dict[str, Any]:
    pack_path = module_root / "knowledge" / "astro" / "pack.json"
    vault_root = module_root / "knowledge" / "astro" / "vault"
    if not pack_path.is_file():
        return {"ok": False, "error": f"pack.json missing at {pack_path}"}
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if pack.get("schema") != "legends.knowledge-pack/v1":
        errors.append(f"Unexpected schema: {pack.get('schema')}")
    if pack.get("module") != "legends-astro":
        errors.append(f"Unexpected module: {pack.get('module')}")
    files = pack.get("files", [])
    for entry in files:
        rel = entry.get("path", "")
        target = (vault_root / rel).resolve()
        if not target.is_file():
            errors.append(f"Missing pack file: {rel}")
            continue
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_sha != entry.get("sha256"):
            errors.append(f"SHA-256 mismatch for {rel}: expected {entry.get('sha256')}, got {actual_sha}")
    return {
        "ok": len(errors) == 0,
        "packPath": str(pack_path),
        "id": pack.get("id"),
        "module": pack.get("module"),
        "version": pack.get("version"),
        "suggestedHome": pack.get("ontology", {}).get("suggested_home"),
        "fileCount": len(files),
        "errors": errors,
    }
