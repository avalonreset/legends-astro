"""Read-only static multi-framework repository auditor for migrating to Astro v7.4."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .net_safety import (
    assert_path_within,
    is_reparse_or_symlink,
    reject_symlink_chain,
    safe_read_bytes,
)

MAX_REPO_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_FILES = 4000

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "out",
        ".cache",
        ".astro",
        ".venv",
        "__pycache__",
        ".svelte-kit",
        ".docusaurus",
        "_site",
        "coverage",
        ".turbo",
        ".vercel",
        ".netlify",
        "public",
        "static",
    }
)

_SKIP_WALK_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "out",
        ".cache",
        ".astro",
        ".venv",
        "__pycache__",
        ".svelte-kit",
        ".docusaurus",
        "_site",
        "coverage",
        ".turbo",
    }
)

_CONFIG_EXTS = ("js", "mjs", "cjs", "ts", "mts", "cts")
_PAGE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".mdx", ".md", ".vue", ".svelte")
_COMPONENT_EXTS = (".tsx", ".jsx", ".vue", ".svelte", ".ts", ".js")
_MD_EXTS = (".md", ".mdx", ".markdown")

_NEXT_SPECIAL_PAGES = frozenset({"_app", "_document", "_error", "_middleware"})
_IGNORED_ROOT_MD = frozenset(
    {
        "readme.md",
        "changelog.md",
        "contributing.md",
        "code_of_conduct.md",
        "security.md",
        "support.md",
        "license.md",
        "third_party_notices.md",
        "agents.md",
        "claude.md",
        "credits.md",
        "notice.md",
        "authors.md",
        "todo.md",
        "roadmap.md",
    }
)

_NON_CONTENT_DIRS = frozenset(
    {
        "ops",
        "scripts",
        "tools",
        "sounds",
        "public",
        "static",
        "fixtures",
        "test",
        "tests",
        "__tests__",
        "e2e",
        "playwright",
        "cypress",
        "bin",
        "config",
        "configs",
        "infra",
        "deploy",
        "ci",
    }
)

# Interactivity detection patterns
_USE_CLIENT_RE = re.compile(r"""^\s*["']use client["']\s*;?""", re.MULTILINE)
_REACT_HOOKS_RE = re.compile(
    r"\b(useState|useEffect|useRef|useContext|useReducer|useCallback|useMemo|useLayoutEffect|useTransition|useOptimistic)\s*(?:<[^>]*>)?\s*\("
)
_VUE_REACTIVITY_RE = re.compile(
    r"\b(ref|reactive|computed|watch|watchEffect|onMounted|onUnmounted|onUpdated|defineEmits|defineModel)\s*\(|@click\b|v-model\b"
)
_SVELTE_REACTIVITY_RE = re.compile(
    r"\b(onMount|afterUpdate|beforeUpdate|onDestroy|writable|readable|derived)\s*\(|\$(?:state|derived|effect)\s*\(|on:click\b|bind:"
)
_BROWSER_GLOBALS_RE = re.compile(r"\b(window|document|localStorage|sessionStorage|navigator)\.")
_JSX_EVENTS_RE = re.compile(r"\b(onClick|onChange|onSubmit|onInput|onKeyDown|onMouseEnter)\s*=")

# Env var patterns (names only; never read or emit values)
_ENV_PREFIXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NEXT_PUBLIC_", re.compile(r"\b(NEXT_PUBLIC_[A-Z0-9_]+)\b")),
    ("NUXT_PUBLIC_", re.compile(r"\b(NUXT_PUBLIC_[A-Z0-9_]+)\b")),
    ("GATSBY_", re.compile(r"\b(GATSBY_[A-Z0-9_]+)\b")),
    ("VITE_", re.compile(r"\b(VITE_[A-Z0-9_]+)\b")),
    ("REACT_APP_", re.compile(r"\b(REACT_APP_[A-Z0-9_]+)\b")),
)

# Framework import / syntax trap patterns
_TRAP_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("next/image", re.compile(r"""from\s+["']next/image["']|require\(["']next/image["']\)"""), "Replace with astro:assets <Image /> or <Picture />."),
    ("next/link", re.compile(r"""from\s+["']next/link["']|require\(["']next/link["']\)"""), "Replace with standard <a href> elements (Astro supports native prefetch and View Transitions)."),
    ("next/head", re.compile(r"""from\s+["']next/head["']|require\(["']next/head["']\)"""), "Move metadata into your Astro Layout <head> or named <slot name=\"head\" />."),
    ("next/router", re.compile(r"""from\s+["']next/router["']|require\(["']next/router["']\)"""), "Replace with Astro.url, Astro.params, and Astro.redirect in server/frontmatter code."),
    ("next/navigation", re.compile(r"""from\s+["']next/navigation["']|require\(["']next/navigation["']\)"""), "Replace with Astro.url, Astro.params, and Astro.redirect or client-side URL APIs inside islands."),
    ("next/font", re.compile(r"""from\s+["']next/font(?:/[^"']*)?["']"""), "Replace with Astro Fonts API in astro.config.mjs."),
    ("next/script", re.compile(r"""from\s+["']next/script["']"""), "Replace with standard <script> or <script is:inline> tags."),
    ("gatsby-plugin-image", re.compile(r"""from\s+["']gatsby-plugin-image["']|GatsbyImage|StaticImage"""), "Replace with astro:assets <Image />."),
    ("gatsby-graphql", re.compile(r"""from\s+["']gatsby["']|\bgraphql\s*`"""), "Replace Gatsby GraphQL page queries with Astro Content Layer getCollection()."),
    ("nuxt-composables", re.compile(r"\b(useFetch|useAsyncData|useRuntimeConfig|navigateTo)\s*\(|<NuxtLink\b|<NuxtImg\b"), "Replace Nuxt composables with Astro frontmatter fetch/env and standard <a>/<Image />."),
    ("spa-router", re.compile(r"""from\s+["'](?:react-router-dom|react-router|vue-router|@tanstack/react-router)["']"""), "Migrate client SPA routes to Astro file-based routing under src/pages/."),
    ("hugo-shortcodes", re.compile(r"\{\{[<%]\s*([a-zA-Z0-9_-]+)"), "Convert Hugo {{< shortcode >}} calls to MDX components in Astro."),
    ("jekyll-liquid", re.compile(r"\{%\s*(?:include|assign|for|if|highlight|post_url|link)\b"), "Convert Jekyll Liquid {% ... %} tags to Astro component frontmatter and templates."),
    ("css-modules", re.compile(r"""from\s+["'][^"']+\.module\.(?:css|scss|sass|less)["']"""), "CSS Modules work in Astro, or simplify to component-scoped <style> blocks."),
    ("tailwind", re.compile(r"""@tailwind\s+(?:base|components|utilities)|@import\s+["']tailwindcss["']"""), "Configure Tailwind via @tailwindcss/vite in astro.config.mjs."),
)

_FRAMEWORK_DOCS: dict[str, list[str]] = {
    "nextjs": ["migrate-from-nextjs", "migration", "routing", "content", "images", "environment-variables"],
    "nuxt": ["migrate-from-nuxtjs", "migration", "routing", "content", "environment-variables"],
    "gatsby": ["migrate-from-gatsby", "migration", "images", "content", "routing"],
    "create-react-app": ["migrate-from-create-react-app", "migration", "components", "routing", "environment-variables"],
    "vite": ["migrate-from-create-react-app", "vite-plugin-astro", "migration", "components", "routing"],
    "docusaurus": ["migrate-from-docusaurus", "migration", "content", "routing"],
    "hugo": ["migrate-from-hugo", "migration", "content", "layouts", "routing"],
    "jekyll": ["migrate-from-jekyll", "migration", "content", "layouts", "routing"],
    "eleventy": ["migrate-from-eleventy", "migration", "content", "layouts", "routing"],
    "sveltekit": ["migrate-from-sveltekit", "migration", "routing", "actions", "environment-variables"],
}


def _safe_read_text(root: Path, path: Path) -> str | None:
    try:
        if is_reparse_or_symlink(path) or not path.is_file():
            return None
        assert_path_within(root, path)
        raw = safe_read_bytes(path, max_bytes=MAX_REPO_FILE_BYTES)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def _has_config_file(root: Path, base_name: str, exts: tuple[str, ...] = _CONFIG_EXTS) -> Path | None:
    for ext in exts:
        cand = root / f"{base_name}.{ext}"
        if cand.is_file() and not is_reparse_or_symlink(cand):
            return cand
    return None


def _read_package_json(root: Path) -> tuple[dict[str, Any], set[str]]:
    pkg_path = root / "package.json"
    text = _safe_read_text(root, pkg_path)
    if not text:
        return {}, set()
    try:
        data = json.loads(text)
    except Exception:
        return {}, set()
    if not isinstance(data, dict):
        return {}, set()
    deps: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(field)
        if isinstance(block, dict):
            deps.update(str(k) for k in block.keys())
    return data, deps


def _iter_repo_files(root: Path, *, skip_dirs: frozenset[str] = _SKIP_WALK_DIRS) -> list[Path]:
    """Walk repo files safely without following symlinks and skipping build/dependency dirs."""
    files: list[Path] = []
    root_res = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root_res, followlinks=False):
        cur = Path(dirpath)
        # Prune skipped and symlinked directories in-place
        kept: list[str] = []
        for d in sorted(dirnames):
            if d in skip_dirs:
                continue
            cand_dir = cur / d
            if is_reparse_or_symlink(cand_dir):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fname in sorted(filenames):
            fp = cur / fname
            if is_reparse_or_symlink(fp):
                continue
            try:
                if not fp.resolve().is_relative_to(root_res):
                    continue
            except OSError:
                continue
            files.append(fp)
            if len(files) >= MAX_SCAN_FILES:
                return files
    return files


def detect_framework_repo(src: Path) -> dict[str, Any] | None:
    """Detect whether src is a known JS/SSG framework repository (read-only static check)."""
    if not src.is_dir():
        return None
    pkg, deps = _read_package_json(src)

    # Do not misclassify an Astro project itself as a foreign framework repo
    is_astro = bool(_has_config_file(src, "astro.config")) or ("astro" in deps)

    # 1. Next.js
    next_cfg = _has_config_file(src, "next.config")
    if next_cfg or "next" in deps:
        has_app = (src / "app").is_dir() or (src / "src" / "app").is_dir()
        has_pages = (src / "pages").is_dir() or (src / "src" / "pages").is_dir()
        if has_app and has_pages:
            router_kind = "hybrid"
        elif has_app:
            router_kind = "app"
        elif has_pages:
            router_kind = "pages"
        else:
            router_kind = "unknown"
        return {"framework": "nextjs", "routerKind": router_kind, "configFile": next_cfg.name if next_cfg else None, "deps": deps, "pkg": pkg}

    # 2. Nuxt
    nuxt_cfg = _has_config_file(src, "nuxt.config")
    if nuxt_cfg or "nuxt" in deps or "nuxt3" in deps:
        has_pages = (src / "pages").is_dir() or (src / "app" / "pages").is_dir()
        return {"framework": "nuxt", "routerKind": "pages" if has_pages else "single-page", "configFile": nuxt_cfg.name if nuxt_cfg else None, "deps": deps, "pkg": pkg}

    # 3. Gatsby
    gatsby_cfg = _has_config_file(src, "gatsby-config")
    if gatsby_cfg or "gatsby" in deps:
        return {"framework": "gatsby", "routerKind": "src-pages", "configFile": gatsby_cfg.name if gatsby_cfg else None, "deps": deps, "pkg": pkg}

    # 4. Docusaurus
    doku_cfg = _has_config_file(src, "docusaurus.config")
    if doku_cfg or "@docusaurus/core" in deps:
        return {"framework": "docusaurus", "routerKind": "docs-blog-pages", "configFile": doku_cfg.name if doku_cfg else None, "deps": deps, "pkg": pkg}

    # 5. SvelteKit
    svelte_cfg = _has_config_file(src, "svelte.config")
    if "@sveltejs/kit" in deps or (svelte_cfg and (src / "src" / "routes").is_dir()):
        return {"framework": "sveltekit", "routerKind": "file-based-routes", "configFile": svelte_cfg.name if svelte_cfg else None, "deps": deps, "pkg": pkg}

    # 6. Eleventy / 11ty
    eleventy_cfg = None
    for cand_name in (".eleventy.js", ".eleventy.cjs", "eleventy.config.js", "eleventy.config.mjs", "eleventy.config.cjs", "eleventy.config.ts"):
        cp = src / cand_name
        if cp.is_file() and not is_reparse_or_symlink(cp):
            eleventy_cfg = cp
            break
    if eleventy_cfg or "@11ty/eleventy" in deps:
        return {"framework": "eleventy", "routerKind": "file-based", "configFile": eleventy_cfg.name if eleventy_cfg else None, "deps": deps, "pkg": pkg}

    # 7. Hugo
    hugo_cfg = None
    for cand_name in ("hugo.toml", "hugo.yaml", "hugo.yml", "hugo.json"):
        cp = src / cand_name
        if cp.is_file() and not is_reparse_or_symlink(cp):
            hugo_cfg = cp
            break
    if not hugo_cfg:
        for cand_name in ("config.toml", "config.yaml", "config.yml"):
            cp = src / cand_name
            if cp.is_file() and not is_reparse_or_symlink(cp) and ((src / "content").is_dir() or (src / "layouts").is_dir()):
                hugo_cfg = cp
                break
    if hugo_cfg or ((src / "config" / "_default").is_dir() and (src / "content").is_dir()):
        return {"framework": "hugo", "routerKind": "content-sections", "configFile": hugo_cfg.name if hugo_cfg else "config/_default", "deps": deps, "pkg": pkg}

    # 8. Jekyll
    jekyll_cfg = None
    for cand_name in ("_config.yml", "_config.yaml"):
        cp = src / cand_name
        if cp.is_file() and not is_reparse_or_symlink(cp):
            jekyll_cfg = cp
            break
    if jekyll_cfg and (
        (src / "_posts").is_dir()
        or (src / "_layouts").is_dir()
        or (src / "_includes").is_dir()
        or (src / "Gemfile").is_file()
    ):
        return {"framework": "jekyll", "routerKind": "posts-pages", "configFile": jekyll_cfg.name, "deps": deps, "pkg": pkg}

    # 9. Create React App
    if "react-scripts" in deps:
        return {"framework": "create-react-app", "routerKind": "spa", "configFile": "package.json", "deps": deps, "pkg": pkg}

    # 10. Vite SPA (React / Vue / Svelte / Preact / Solid) - never hijack an Astro repo
    vite_cfg = _has_config_file(src, "vite.config")
    if (vite_cfg or "vite" in deps) and not is_astro:
        return {"framework": "vite", "routerKind": "spa", "configFile": vite_cfg.name if vite_cfg else "package.json", "deps": deps, "pkg": pkg}

    return None


def _extract_frontmatter_and_body(text: str) -> tuple[dict[str, Any], list[str], str]:
    """Safely extract YAML (---) or Hugo TOML (+++) frontmatter keys and basic scalar metadata."""
    meta: dict[str, Any] = {}
    keys: list[str] = []
    body = text

    yaml_m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    toml_m = re.match(r"^\+\+\+\r?\n(.*?)\r?\n\+\+\+\r?\n?(.*)$", text, re.DOTALL) if not yaml_m else None

    if yaml_m:
        fm_raw, body = yaml_m.group(1), yaml_m.group(2)
        for line in fm_raw.splitlines():
            km = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
            if km:
                k = km.group(1)
                v = km.group(2).strip().strip('"').strip("'")
                if k not in keys:
                    keys.append(k)
                meta[k] = v
    elif toml_m:
        fm_raw, body = toml_m.group(1), toml_m.group(2)
        for line in fm_raw.splitlines():
            km = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$", line)
            if km:
                k = km.group(1)
                v = km.group(2).strip().strip('"').strip("'")
                if k not in keys:
                    keys.append(k)
                meta[k] = v

    return meta, keys, body.strip()


def _route_kind_and_params(route: str) -> tuple[str, list[str]]:
    params: list[str] = []
    is_catchall = False
    for m in re.finditer(r"\[\[\.\.\.([^\]]+)\]\]|\[\.\.\.([^\]]+)\]|\[([^\]]+)\]", route):
        if m.group(1):
            params.append(m.group(1))
            is_catchall = True
        elif m.group(2):
            params.append(m.group(2))
            is_catchall = True
        elif m.group(3):
            params.append(m.group(3))
    for m in re.finditer(r"\{([^}]+)\}", route):
        params.append(m.group(1))
    for m in re.finditer(r"/:([A-Za-z0-9_]+)", route):
        params.append(m.group(1))
    if "*" in route or is_catchall:
        return "catchall", params
    if params:
        return "dynamic", params
    return "static", []


def _normalize_url_path(segments: list[str], *, trailing_slash: bool = False) -> str:
    clean = [s for s in segments if s]
    if not clean:
        return "/"
    joined = "/" + "/".join(clean)
    if trailing_slash and not joined.endswith("/"):
        joined += "/"
    return joined


def _discover_routes(
    root: Path,
    framework: str,
    router_kind: str,
    all_files: list[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    routes: list[dict[str, Any]] = []
    api_routes: list[dict[str, Any]] = []
    seen_routes: set[tuple[str, str]] = set()
    seen_apis: set[tuple[str, str]] = set()

    def add_route(
        route: str,
        rel_file: str,
        router: str,
        *,
        kind_override: str | None = None,
        meta: dict[str, Any] | None = None,
        fm_keys: list[str] | None = None,
        body: str = "",
    ) -> None:
        route = "/" + str(route or "").strip().replace("\\", "/").lstrip("/")
        key = (route, rel_file)
        if key in seen_routes:
            return
        seen_routes.add(key)
        r_kind, params = _route_kind_and_params(route)
        if kind_override and r_kind == "static":
            r_kind = kind_override
        entry: dict[str, Any] = {
            "route": route,
            "sourceFile": rel_file,
            "kind": r_kind,
            "params": params,
            "router": router,
        }
        if meta:
            if meta.get("title"):
                entry["title"] = meta["title"]
            if meta.get("description"):
                entry["metaDescription"] = meta["description"]
            draft_val = str(meta.get("draft", "")).lower()
            pub_val = str(meta.get("published", "")).lower()
            if draft_val == "true" or pub_val == "false":
                entry["draft"] = True
        if fm_keys:
            entry["frontmatterKeys"] = fm_keys
        if body:
            entry["markdownBody"] = body
        routes.append(entry)

    def add_api(route: str, rel_file: str, router: str) -> None:
        key = (route, rel_file)
        if key in seen_apis:
            return
        seen_apis.add(key)
        api_routes.append({"route": route, "sourceFile": rel_file, "router": router})

    if framework == "nextjs":
        # App Router: app/ or src/app/
        for app_prefix in ("app", "src/app"):
            app_dir = root / app_prefix
            if not app_dir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_app = fp.relative_to(app_dir)
                except ValueError:
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                stem = fp.stem
                if fp.suffix.lower() not in (".tsx", ".ts", ".jsx", ".js", ".mdx"):
                    continue
                parent_segs = [
                    seg
                    for seg in rel_to_app.parent.parts
                    if seg and not (seg.startswith("(") and seg.endswith(")")) and not seg.startswith("@")
                ]
                if stem == "route":
                    add_api(_normalize_url_path(parent_segs), rel_posix, "next-app-router")
                elif stem == "page":
                    add_route(_normalize_url_path(parent_segs), rel_posix, "next-app-router")

        # Pages Router: pages/ or src/pages/
        for pages_prefix in ("pages", "src/pages"):
            pages_dir = root / pages_prefix
            if not pages_dir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_pages = fp.relative_to(pages_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".tsx", ".ts", ".jsx", ".js", ".mdx", ".md"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parts = list(rel_to_pages.parts)
                stem = fp.stem
                if parts[0] == "api":
                    segs = parts[:-1] + ([] if stem == "index" else [stem])
                    add_api(_normalize_url_path(segs), rel_posix, "next-pages-router")
                    continue
                if stem in _NEXT_SPECIAL_PAGES or stem.startswith("_"):
                    continue
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                add_route(_normalize_url_path(segs), rel_posix, "next-pages-router")

    elif framework == "nuxt":
        for pages_prefix in ("pages", "app/pages"):
            pages_dir = root / pages_prefix
            if not pages_dir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_pages = fp.relative_to(pages_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".vue", ".tsx", ".ts", ".js", ".md"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parts = list(rel_to_pages.parts)
                stem = fp.stem
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                add_route(_normalize_url_path(segs), rel_posix, "nuxt-pages")

        for srv_prefix, url_prefix in (("server/api", ["api"]), ("server/routes", [])):
            srv_dir = root / srv_prefix
            if not srv_dir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_srv = fp.relative_to(srv_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".ts", ".js", ".mjs"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parts = list(rel_to_srv.parts)
                # Strip Nuxt method suffix like users.get.ts -> users
                stem = re.sub(r"\.(get|post|put|patch|delete|head|options)$", "", fp.stem, flags=re.IGNORECASE)
                segs = url_prefix + parts[:-1] + ([] if stem == "index" else [stem])
                add_api(_normalize_url_path(segs), rel_posix, "nuxt-server")

    elif framework == "gatsby":
        pages_dir = root / "src" / "pages"
        if pages_dir.is_dir():
            for fp in all_files:
                try:
                    rel_to_pages = fp.relative_to(pages_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".tsx", ".ts", ".jsx", ".js", ".mdx", ".md"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parts = list(rel_to_pages.parts)
                stem = fp.stem
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                add_route(_normalize_url_path(segs, trailing_slash=True), rel_posix, "gatsby-pages")
        # Also discover Markdown/MDX posts under content/ or blog/
        for content_root in ("content", "src/content", "blog"):
            cdir = root / content_root
            if not cdir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_c = fp.relative_to(cdir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in _MD_EXTS:
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                txt = _safe_read_text(root, fp) or ""
                meta, fm_keys, body = _extract_frontmatter_and_body(txt)
                parts = list(rel_to_c.parts)
                stem = fp.stem
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                route = meta.get("slug") or meta.get("path") or _normalize_url_path(segs, trailing_slash=True)
                if not route.startswith("/"):
                    route = "/" + route
                add_route(route, rel_posix, "gatsby-content", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

    elif framework == "docusaurus":
        pages_dir = root / "src" / "pages"
        if pages_dir.is_dir():
            for fp in all_files:
                try:
                    rel_to_pages = fp.relative_to(pages_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".tsx", ".ts", ".jsx", ".js", ".mdx", ".md"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parts = list(rel_to_pages.parts)
                stem = fp.stem
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                add_route(_normalize_url_path(segs), rel_posix, "docusaurus-pages")

        for section in ("docs", "blog"):
            sdir = root / section
            if not sdir.is_dir():
                continue
            for fp in all_files:
                try:
                    rel_to_s = fp.relative_to(sdir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in _MD_EXTS:
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                txt = _safe_read_text(root, fp) or ""
                meta, fm_keys, body = _extract_frontmatter_and_body(txt)
                parts = list(rel_to_s.parts)
                stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", fp.stem)
                segs = [section] + parts[:-1] + ([] if stem in ("index", "intro") and not parts[:-1] and section == "docs" else [stem])
                slug_override = meta.get("slug")
                if slug_override:
                    route = slug_override if slug_override.startswith("/") else f"/{section}/{slug_override.lstrip('/')}"
                else:
                    route = _normalize_url_path(segs)
                add_route(route, rel_posix, f"docusaurus-{section}", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

    elif framework == "hugo":
        cdir = root / "content"
        if cdir.is_dir():
            for fp in all_files:
                try:
                    rel_to_c = fp.relative_to(cdir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".md", ".mdx", ".markdown", ".html"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                txt = _safe_read_text(root, fp) or ""
                meta, fm_keys, body = _extract_frontmatter_and_body(txt)
                parts = list(rel_to_c.parts)
                stem = fp.stem
                if meta.get("url"):
                    route = str(meta["url"])
                    if not route.startswith("/"):
                        route = "/" + route
                else:
                    if stem in ("_index", "index"):
                        segs = parts[:-1]
                    else:
                        segs = parts[:-1] + [str(meta.get("slug") or stem)]
                    route = _normalize_url_path(segs, trailing_slash=True)
                add_route(route, rel_posix, "hugo-content", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

    elif framework == "jekyll":
        posts_dir = root / "_posts"
        if posts_dir.is_dir():
            for fp in all_files:
                try:
                    fp.relative_to(posts_dir)
                except ValueError:
                    continue
                if fp.suffix.lower() not in (".md", ".markdown", ".html"):
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                txt = _safe_read_text(root, fp) or ""
                meta, fm_keys, body = _extract_frontmatter_and_body(txt)
                if meta.get("permalink"):
                    route = str(meta["permalink"])
                    if not route.startswith("/"):
                        route = "/" + route
                else:
                    dm = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(.+)$", fp.stem)
                    if dm:
                        route = f"/{dm.group(1)}/{dm.group(2)}/{dm.group(3)}/{dm.group(4)}/"
                    else:
                        route = f"/{fp.stem}/"
                add_route(route, rel_posix, "jekyll-posts", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

        for fp in all_files:
            rel = fp.relative_to(root)
            rel_posix = rel.as_posix()
            if any(part.startswith("_") for part in rel.parts) and rel.parts[0] != "_pages":
                continue
            if fp.name.lower() in _IGNORED_ROOT_MD:
                continue
            if fp.suffix.lower() not in (".md", ".markdown", ".html"):
                continue
            txt = _safe_read_text(root, fp) or ""
            meta, fm_keys, body = _extract_frontmatter_and_body(txt)
            parts = list(rel.parts[1:] if rel.parts[0] == "_pages" else rel.parts)
            stem = fp.stem
            if meta.get("permalink"):
                route = str(meta["permalink"])
                if not route.startswith("/"):
                    route = "/" + route
            else:
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                route = _normalize_url_path(segs, trailing_slash=True)
            add_route(route, rel_posix, "jekyll-pages", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

    elif framework == "eleventy":
        for fp in all_files:
            rel = fp.relative_to(root)
            rel_posix = rel.as_posix()
            if any(part in ("_includes", "_layouts", "_data", "_site") for part in rel.parts):
                continue
            if fp.name.lower() in _IGNORED_ROOT_MD:
                continue
            if fp.suffix.lower() not in (".md", ".markdown", ".njk", ".liquid", ".html", ".hbs"):
                continue
            txt = _safe_read_text(root, fp) or ""
            meta, fm_keys, body = _extract_frontmatter_and_body(txt)
            parts = list(rel.parts)
            if parts and parts[0] in ("src", "content"):
                parts = parts[1:]
            stem = fp.stem
            if meta.get("permalink"):
                route = str(meta["permalink"])
                if not route.startswith("/"):
                    route = "/" + route
            else:
                segs = parts[:-1] + ([] if stem == "index" else [stem])
                route = _normalize_url_path(segs, trailing_slash=True)
            add_route(route, rel_posix, "eleventy-templates", kind_override="content", meta=meta, fm_keys=fm_keys, body=body)

    elif framework == "sveltekit":
        rdir = root / "src" / "routes"
        if rdir.is_dir():
            for fp in all_files:
                try:
                    rel_to_r = fp.relative_to(rdir)
                except ValueError:
                    continue
                rel_posix = fp.relative_to(root).as_posix()
                parent_segs = [
                    seg
                    for seg in rel_to_r.parent.parts
                    if seg and not (seg.startswith("(") and seg.endswith(")"))
                ]
                if fp.stem == "+server" and fp.suffix.lower() in (".ts", ".js"):
                    add_api(_normalize_url_path(parent_segs), rel_posix, "sveltekit-server")
                elif fp.stem == "+page" and fp.suffix.lower() in (".svelte", ".md", ".svx"):
                    add_route(_normalize_url_path(parent_segs), rel_posix, "sveltekit-routes")

    elif framework in ("vite", "create-react-app"):
        entry_html = "index.html" if (root / "index.html").is_file() else ("public/index.html" if (root / "public" / "index.html").is_file() else "src/App.tsx")
        add_route("/", entry_html, f"{framework}-spa")
        # Statically scan SPA router paths in src/
        spa_path_re = re.compile(r"""(?:<Route\b[^>]*\bpath\s*=\s*|path\s*:\s*)["'](/[^"'\s]*)["']""")
        # Statically scan Express / Fastify / Hono / Node HTTP handlers under server/
        express_route_re = re.compile(
            r"""\b(?:app|router|server|api|fastify|hono)\.(?:get|post|put|patch|delete|options|head|all)\s*\(\s*["'](/[^"'\s]*)["']"""
        )
        node_http_api_re = re.compile(r"""\bpathname\s*===?\s*["'](/api(?:/[^"'\s]*)?)["']""")
        for fp in all_files:
            rel_posix = fp.relative_to(root).as_posix()
            name_lower = fp.name.lower()
            if rel_posix.startswith("src/") and fp.suffix.lower() in (".tsx", ".ts", ".jsx", ".js", ".vue", ".svelte"):
                txt = _safe_read_text(root, fp)
                if not txt:
                    continue
                for m in spa_path_re.finditer(txt):
                    rpath = m.group(1)
                    if rpath != "/" and not any(r["route"] == rpath for r in routes):
                        add_route(rpath, rel_posix, f"{framework}-client-router")
            elif (
                rel_posix.startswith("server/")
                and fp.suffix.lower() in (".js", ".mjs", ".cjs", ".ts", ".mts")
                and not any(tok in name_lower for tok in (".test.", ".node-test.", ".spec."))
            ):
                txt = _safe_read_text(root, fp)
                if not txt:
                    continue
                for m in express_route_re.finditer(txt):
                    add_api(m.group(1), rel_posix, "server-http")
                for m in node_http_api_re.finditer(txt):
                    add_api(m.group(1), rel_posix, "server-http")

    return routes, api_routes


def _parse_redirects(root: Path) -> list[dict[str, Any]]:
    """Statically parse redirects from vercel.json, netlify.toml, _redirects, next.config.*, nuxt.config.*."""
    redirects: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    def add(source: str, dest: str, status: int, origin: str) -> None:
        s, d = source.strip(), dest.strip()
        if not s or not d:
            return
        key = (s, d, status)
        if key in seen:
            return
        seen.add(key)
        redirects.append({"source": s, "destination": d, "status": status, "originFile": origin})

    # 1. vercel.json
    vercel_txt = _safe_read_text(root, root / "vercel.json")
    if vercel_txt:
        try:
            vdata = json.loads(vercel_txt)
            if isinstance(vdata, dict) and isinstance(vdata.get("redirects"), list):
                for item in vdata["redirects"]:
                    if isinstance(item, dict) and item.get("source") and item.get("destination"):
                        status = int(item.get("statusCode") or (308 if item.get("permanent", True) else 307))
                        add(str(item["source"]), str(item["destination"]), status, "vercel.json")
        except Exception:
            pass

    # 2. netlify.toml
    netlify_txt = _safe_read_text(root, root / "netlify.toml")
    if netlify_txt:
        for block in re.split(r"(?m)^\s*\[\[redirects\]\]\s*$", netlify_txt)[1:]:
            # Stop at next TOML table header
            section = re.split(r"(?m)^\s*\[", block, maxsplit=1)[0]
            fm = re.search(r"""(?m)^\s*from\s*=\s*["']([^"']+)["']""", section)
            tm = re.search(r"""(?m)^\s*to\s*=\s*["']([^"']+)["']""", section)
            sm = re.search(r"""(?m)^\s*status\s*=\s*(\d{3})""", section)
            if fm and tm:
                add(fm.group(1), tm.group(1), int(sm.group(1)) if sm else 301, "netlify.toml")

    # 3. _redirects files
    for rel_name in ("public/_redirects", "static/_redirects", "_redirects"):
        rtxt = _safe_read_text(root, root / rel_name)
        if not rtxt:
            continue
        for line in rtxt.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            parts = clean.split()
            if len(parts) >= 2:
                st = 301
                if len(parts) >= 3:
                    sm = re.match(r"^(\d{3})", parts[2])
                    if sm:
                        st = int(sm.group(1))
                add(parts[0], parts[1], st, rel_name)

    # 4. next.config.* static object literals { source: '...', destination: '...' }
    next_cfg = _has_config_file(root, "next.config")
    if next_cfg:
        ntxt = _safe_read_text(root, next_cfg)
        if ntxt:
            for m in re.finditer(r"\{[^{}]{0,500}\}", ntxt):
                obj = m.group(0)
                sm = re.search(r"""\bsource\s*:\s*["']([^"']+)["']""", obj)
                dm = re.search(r"""\bdestination\s*:\s*["']([^"']+)["']""", obj)
                if sm and dm:
                    code_m = re.search(r"\bstatusCode\s*:\s*(\d{3})", obj)
                    perm_m = re.search(r"\bpermanent\s*:\s*(true|false)", obj)
                    status = int(code_m.group(1)) if code_m else (308 if (not perm_m or perm_m.group(1) == "true") else 307)
                    add(sm.group(1), dm.group(1), status, next_cfg.name)

    # 5. nuxt.config.* routeRules redirect entries
    nuxt_cfg = _has_config_file(root, "nuxt.config")
    if nuxt_cfg:
        nutxt = _safe_read_text(root, nuxt_cfg)
        if nutxt:
            for m in re.finditer(
                r"""["'](/[^"']+)["']\s*:\s*\{\s*redirect\s*:\s*(?:["']([^"']+)["']|\{\s*to\s*:\s*["']([^"']+)["'](?:[^}]*statusCode\s*:\s*(\d{3}))?[^}]*\})""",
                nutxt,
            ):
                dest = m.group(2) or m.group(3) or ""
                status = int(m.group(4)) if m.group(4) else 301
                add(m.group(1), dest, status, nuxt_cfg.name)

    return redirects


def _infer_component_framework(ext: str, ui_frameworks: list[str]) -> str:
    if ext == ".vue":
        return "vue"
    if ext == ".svelte":
        return "svelte"
    if ext in (".tsx", ".jsx"):
        if "preact" in ui_frameworks and "react" not in ui_frameworks:
            return "preact"
        if "solid" in ui_frameworks and "react" not in ui_frameworks:
            return "solid"
        return "react"
    return ui_frameworks[0] if ui_frameworks else "js"


def _suggest_hydration_directive(rel_posix: str, reasons: list[str]) -> str:
    lower = rel_posix.lower()
    if any(tok in lower for tok in ("nav", "header", "menu", "modal", "auth", "login", "search", "cart", "form")):
        return "client:load"
    if any(tok in lower for tok in ("chart", "carousel", "gallery", "map", "footer", "comments", "pricing", "faq")):
        return "client:visible"
    if "use_client_directive" in reasons and "react_hooks" in reasons:
        return "client:load"
    return "client:idle"


def _analyze_islands_env_traps(
    root: Path,
    all_files: list[Path],
    ui_frameworks: list[str],
    deps: set[str],
    *,
    framework: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    interactive: list[dict[str, Any]] = []
    static_candidates: list[dict[str, Any]] = []
    env_map: dict[str, dict[str, Any]] = {}
    traps: dict[str, dict[str, Any]] = {}

    if "tailwindcss" in deps:
        traps["tailwind"] = {
            "count": 1,
            "files": ["package.json"],
            "guidance": "Configure Tailwind via @tailwindcss/vite in astro.config.mjs.",
        }

    for fp in all_files:
        rel = fp.relative_to(root)
        rel_posix = rel.as_posix()
        ext = fp.suffix.lower()
        name_lower = fp.name.lower()

        # Skip non-text binary assets quickly
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".pdf", ".zip", ".lock", ".map"):
            continue

        # Never read raw .env / .env.local secrets; only inspect safe templates like .env.example
        if name_lower == ".env" or (name_lower.startswith(".env.") and not any(name_lower.endswith(s) for s in (".example", ".sample", ".template"))):
            continue

        text = _safe_read_text(root, fp)
        if not text:
            continue

        # 1. Env vars (names only)
        if ext in (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".vue", ".svelte", ".json", ".toml", ".yml", ".yaml") or name_lower.startswith(".env."):
            for prefix, rx in _ENV_PREFIXES:
                for m in rx.finditer(text):
                    vname = m.group(1)
                    suffix = vname[len(prefix) :]
                    if not suffix:
                        continue
                    rec = env_map.setdefault(
                        vname,
                        {
                            "name": vname,
                            "prefix": prefix,
                            "suggestedAstroName": f"PUBLIC_{suffix}",
                            "files": [],
                        },
                    )
                    if rel_posix not in rec["files"]:
                        rec["files"].append(rel_posix)

        # 2. Framework imports & syntax traps
        if ext in (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".vue", ".svelte", ".md", ".mdx", ".markdown", ".html", ".css", ".scss"):
            for trap_name, rx, guidance in _TRAP_PATTERNS:
                if trap_name == "nuxt-composables" and framework != "nuxt" and ext != ".vue":
                    continue
                matches = rx.findall(text)
                if matches:
                    trec = traps.setdefault(
                        trap_name,
                        {"count": 0, "files": [], "guidance": guidance},
                    )
                    trec["count"] += len(matches)
                    if rel_posix not in trec["files"]:
                        trec["files"].append(rel_posix)

        # 3. Islands vs static component candidates
        is_test_or_config = (
            ".config." in name_lower
            or name_lower.endswith((".d.ts", ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx"))
            or name_lower in ("next-env.d.ts", "vite-env.d.ts", "main.ts", "main.tsx", "main.js", "main.jsx", "index.ts", "index.js", "_app.tsx", "_app.jsx", "_document.tsx", "_document.jsx")
            or "/api/" in f"/{rel_posix}"
            or rel_posix.startswith(("server/api/", "server/routes/"))
        )
        if is_test_or_config:
            continue

        is_ui_file = ext in (".tsx", ".jsx", ".vue", ".svelte") or (
            ext in (".ts", ".js") and any(part in ("components", "widgets", "islands") for part in rel.parts)
        )
        if not is_ui_file:
            continue

        reasons: list[str] = []
        if _USE_CLIENT_RE.search(text):
            reasons.append("use_client_directive")
        if _REACT_HOOKS_RE.search(text):
            reasons.append("react_hooks")
        if ext == ".vue" and _VUE_REACTIVITY_RE.search(text):
            reasons.append("vue_reactivity")
        if ext == ".svelte" and _SVELTE_REACTIVITY_RE.search(text):
            reasons.append("svelte_reactivity")
        if _BROWSER_GLOBALS_RE.search(text):
            reasons.append("browser_globals")
        if ext in (".tsx", ".jsx") and _JSX_EVENTS_RE.search(text):
            reasons.append("dom_event_handlers")

        comp_fw = _infer_component_framework(ext, ui_frameworks)
        if reasons:
            interactive.append(
                {
                    "file": rel_posix,
                    "framework": comp_fw,
                    "reasons": reasons,
                    "suggestedDirective": _suggest_hydration_directive(rel_posix, reasons),
                }
            )
        else:
            static_candidates.append(
                {
                    "file": rel_posix,
                    "framework": comp_fw,
                }
            )

    islands_payload = {
        "interactiveIslands": interactive,
        "staticCandidateComponents": static_candidates,
        "summary": {
            "interactiveCount": len(interactive),
            "staticCandidateCount": len(static_candidates),
        },
    }
    env_list = sorted(env_map.values(), key=lambda x: x["name"])
    return islands_payload, env_list, traps


def _discover_content_collections(root: Path, all_files: list[Path]) -> list[dict[str, Any]]:
    """Discover Markdown/MDX content roots and sample frontmatter keys for src/content.config.ts."""
    groups: dict[str, dict[str, Any]] = {}

    for fp in all_files:
        if fp.suffix.lower() not in _MD_EXTS:
            continue
        rel = fp.relative_to(root)
        name_lower = fp.name.lower()
        if name_lower in _IGNORED_ROOT_MD and len(rel.parts) == 1:
            continue
        if name_lower in ("credits.md", "changelog.md", "license.md", "third_party_notices.md"):
            continue
        dir_parts = rel.parts[:-1]
        if any(part.startswith(".") or part.lower() in _NON_CONTENT_DIRS for part in dir_parts):
            continue
        if any(part in ("_includes", "_layouts", "_data", "layouts") for part in rel.parts):
            continue

        parts = list(rel.parts)
        if len(parts) == 1:
            col_dir = "."
            col_name = "pages"
        elif parts[0] in ("content", "src") and len(parts) >= 3:
            if parts[0] == "src" and parts[1] == "content" and len(parts) >= 4:
                col_dir = "/".join(parts[:3])
                col_name = parts[2]
            else:
                col_dir = "/".join(parts[:2])
                col_name = parts[1]
        else:
            col_dir = parts[0]
            col_name = parts[0].lstrip("_") or "content"

        rec = groups.setdefault(
            col_dir,
            {
                "name": re.sub(r"[^a-zA-Z0-9_-]+", "-", col_name).strip("-") or "content",
                "rootDir": col_dir,
                "fileCount": 0,
                "extensions": set(),
                "sampleFrontmatterKeys": set(),
                "sampleFiles": [],
            },
        )
        rec["fileCount"] += 1
        rec["extensions"].add(fp.suffix.lower())
        if len(rec["sampleFiles"]) < 5:
            rec["sampleFiles"].append(rel.as_posix())
        txt = _safe_read_text(root, fp)
        if txt:
            _meta, fm_keys, _body = _extract_frontmatter_and_body(txt)
            rec["sampleFrontmatterKeys"].update(fm_keys)

    out: list[dict[str, Any]] = []
    for col_dir in sorted(groups.keys()):
        g = groups[col_dir]
        out.append(
            {
                "name": g["name"],
                "rootDir": g["rootDir"],
                "fileCount": g["fileCount"],
                "extensions": sorted(g["extensions"]),
                "sampleFrontmatterKeys": sorted(g["sampleFrontmatterKeys"]),
                "sampleFiles": g["sampleFiles"],
            }
        )
    return out


def _detect_ui_frameworks(framework: str, deps: set[str], all_files: list[Path]) -> list[str]:
    found: list[str] = []
    exts = {fp.suffix.lower() for fp in all_files}
    if "react" in deps or "react-dom" in deps or framework in ("nextjs", "gatsby", "create-react-app", "docusaurus") or ".tsx" in exts or ".jsx" in exts:
        found.append("react")
    if "vue" in deps or framework == "nuxt" or ".vue" in exts:
        found.append("vue")
    if "svelte" in deps or framework == "sveltekit" or ".svelte" in exts:
        found.append("svelte")
    if "preact" in deps:
        found.append("preact")
    if "solid-js" in deps:
        found.append("solid")
    return found


def _recommend_integrations(
    ui_frameworks: list[str],
    content_collections: list[dict[str, Any]],
    output_rec: str,
    framework: str,
) -> list[str]:
    integrations: list[str] = []
    ui_map = {
        "react": "@astrojs/react",
        "vue": "@astrojs/vue",
        "svelte": "@astrojs/svelte",
        "preact": "@astrojs/preact",
        "solid": "@astrojs/solid-js",
    }
    for uif in ui_frameworks:
        pkg = ui_map.get(uif)
        if pkg and pkg not in integrations:
            integrations.append(pkg)

    has_mdx = any(".mdx" in c.get("extensions", []) for c in content_collections) or framework in ("docusaurus", "gatsby")
    if has_mdx:
        integrations.append("@astrojs/mdx")

    integrations.append("@astrojs/sitemap")

    if output_rec == "server":
        integrations.append("@astrojs/node")
        integrations.append("@astrojs/cloudflare")

    return integrations


def audit_framework_repo(source_path: str | Path, base_url: str | None = None) -> dict[str, Any]:
    """Perform strict read-only static migration audit of a Next.js, Nuxt, Gatsby, Vite/CRA, Docusaurus, Hugo, Jekyll, Eleventy, or SvelteKit repo."""
    raw_src = Path(source_path).expanduser()
    reject_symlink_chain(raw_src)
    src = raw_src.resolve()
    if not src.is_dir():
        raise ValueError(f"Framework source must be an existing directory: {src}")

    detected = detect_framework_repo(src)
    if not detected:
        raise ValueError(f"Directory does not match a supported framework repository: {src}")

    framework: str = detected["framework"]
    router_kind: str = detected["routerKind"]
    deps: set[str] = detected["deps"]

    all_files = _iter_repo_files(src)
    ui_frameworks = _detect_ui_frameworks(framework, deps, all_files)
    routes, api_routes = _discover_routes(src, framework, router_kind, all_files)
    redirects = _parse_redirects(src)
    islands, env_vars, traps = _analyze_islands_env_traps(src, all_files, ui_frameworks, deps, framework=framework)
    content_collections = _discover_content_collections(src, all_files)

    output_rec = "server" if api_routes else "static"
    rec_integrations = _recommend_integrations(ui_frameworks, content_collections, output_rec, framework)

    rec_docs = list(_FRAMEWORK_DOCS.get(framework, ["migration", "routing", "content"]))
    if output_rec == "server":
        for d in ("integration-node", "integration-cloudflare"):
            if d not in rec_docs:
                rec_docs.append(d)

    # Populate contract pages from discovered routes so downstream tooling works seamlessly
    pages: list[dict[str, Any]] = []
    pub_count = 0
    draft_count = 0
    for r in routes:
        route_str = r["route"]
        is_dynamic = r["kind"] in ("dynamic", "catchall")
        is_draft = bool(r.get("draft"))
        if is_dynamic:
            status = "dynamic"
        elif is_draft:
            status = "draft"
            draft_count += 1
        else:
            status = "publish"
            pub_count += 1

        shape = "root" if route_str == "/" else ("directory" if route_str.endswith("/") else ("file" if route_str.endswith(".html") else "clean"))
        md_body = str(r.get("markdownBody") or "")
        pages.append(
            {
                "route": route_str,
                "alternateRoutes": [route_str.rstrip("/")] if route_str.endswith("/") and route_str != "/" else [],
                "urlShape": shape,
                "sourceFile": r["sourceFile"],
                "routeKind": r["kind"],
                "dynamicParams": r["params"],
                "router": r["router"],
                "status": status,
                "sha256": hashlib.sha256(f"{r['sourceFile']}:{route_str}".encode("utf-8")).hexdigest(),
                "title": r.get("title"),
                "metaDescription": r.get("metaDescription"),
                "canonical": None,
                "robots": None,
                "openGraph": {},
                "twitterCard": {},
                "jsonLdCount": 0,
                "jsonLdTypes": [],
                "headings": {"h1": [], "h2": [], "h3": []},
                "domIds": [],
                "requiredDomIds": [],
                "dataAttributes": [],
                "scripts": [],
                "hasFirstPaintInlineStyle": False,
                "forms": [],
                "internalLinks": [],
                "assets": [],
                "bodyExcerpt": md_body[:240],
                "markdownBody": md_body,
                "frontmatterKeys": r.get("frontmatterKeys", []),
            }
        )

    uses_trailing_slash = framework in ("hugo", "jekyll", "eleventy", "gatsby")
    framework_migration = {
        "framework": framework,
        "routerKind": router_kind,
        "configFile": detected.get("configFile"),
        "uiFrameworks": ui_frameworks,
        "recommendedIntegrations": rec_integrations,
        "outputRecommendation": output_rec,
        "routes": [
            {
                "route": r["route"],
                "sourceFile": r["sourceFile"],
                "kind": r["kind"],
                "params": r["params"],
                "router": r["router"],
            }
            for r in routes
        ],
        "apiRoutes": api_routes,
        "redirects": redirects,
        "islands": islands,
        "envVars": env_vars,
        "frameworkImportsAndTraps": traps,
        "contentCollections": content_collections,
        "recommendedDocs": rec_docs,
    }

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceKind": f"framework-{framework}",
        "sourcePath": str(src),
        "baseUrl": base_url,
        "recommendedAstroConfig": {
            "output": output_rec,
            "build.format": "directory",
            "trailingSlash": "always" if uses_trailing_slash else "ignore",
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": pub_count,
            "draftCount": draft_count,
            "framework": framework,
            "routerKind": router_kind,
            "routeCount": len(routes),
            "apiRouteCount": len(api_routes),
            "redirectCount": len(redirects),
            "interactiveIslandCount": islands["summary"]["interactiveCount"],
            "staticCandidateCount": islands["summary"]["staticCandidateCount"],
            "envVarCount": len(env_vars),
            "contentCollectionCount": len(content_collections),
            "jsReferencedIdCount": 0,
            "totalDomIds": 0,
            "totalScripts": 0,
        },
        "triage": {
            "detectedPlatform": framework,
            "pageBuilders": [],
            "shortcodes": [],
            "forms": {"formTagCount": 0, "detectedSystems": []},
            "ecommerce": [],
            "cloudflareEmailObfuscationCount": 0,
            "trackers": [],
            "externalCdns": [],
            "webflow": None,
        },
        "frameworkMigration": framework_migration,
        "jsReferencedIds": [],
        "pages": pages,
    }
