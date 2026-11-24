"""Playwright Visual Digital Twin runner (foreground subprocess) and offline twin-diff parity comparator."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from .net_safety import (
    DEFAULT_MAX_PAGE_BYTES,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    safe_read_bytes,
    safe_write_text,
    validate_outbound_url,
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SCHEME_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def validate_twin_route(raw_route: str, *, base_url: str | None = None) -> str:
    """Validate --route argument against protocol-relative, backslash, scheme, or control-char origin override (CWE-918)."""
    r = raw_route.strip()
    if not r:
        return "/"
    if _CONTROL_CHAR_RE.search(r):
        raise ValueError(f"Refusing --route with control characters: {raw_route!r}")
    if "\\" in r:
        raise ValueError(f"Refusing --route with backslash character: {raw_route!r}")
    if r.startswith("//"):
        raise ValueError(f"Refusing protocol-relative --route (CWE-918): {raw_route!r}")
    if _SCHEME_PREFIX_RE.match(r):
        raise ValueError(f"Refusing absolute/scheme --route (CWE-918): {raw_route!r}")
    normalized = r if r.startswith("/") else f"/{r}"
    if normalized.startswith("//"):
        raise ValueError(f"Refusing protocol-relative --route (CWE-918): {raw_route!r}")
    check_base = base_url or "https://origin.invalid"
    base_p = urllib.parse.urlsplit(check_base)
    joined_p = urllib.parse.urlsplit(urllib.parse.urljoin(check_base, normalized))
    if (joined_p.scheme.lower(), joined_p.netloc.lower()) != (base_p.scheme.lower(), base_p.netloc.lower()):
        raise ValueError(f"Refusing --route escaping target origin (CWE-918): {raw_route!r}")
    return normalized


def run_twin(
    *,
    module_root: Path,
    output_dir: str | Path,
    url: str | None = None,
    dist_dir: str | Path | None = None,
    routes: Iterable[str] = (),
    allow_hosts: Iterable[str] = (),
    crops: Iterable[str] = (),
    timeout_ms: int = 45000,
    no_mobile: bool = False,
    no_full_page: bool = False,
    allow_http: bool = False,
    overwrite: bool = False,
    playwright_from: str | Path | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run scripts/twin.mjs synchronously in foreground (never detached, never windowsHide)."""
    if (not url and not dist_dir) or (url and dist_dir):
        raise ValueError("twin requires exactly one of --url <https://...> or --dist <dist-dir>.")

    raw_out = Path(output_dir).expanduser()
    reject_symlink_chain(raw_out)
    out = raw_out.resolve()
    reject_controlled_folder_access(out)

    allow_list = [normalize_host(h) if not h.strip().startswith("*.") else h.strip().lower() for h in allow_hosts if h.strip()]

    if url:
        eff_allow = allow_list or [normalize_host(url)]
        validate_outbound_url(url, allowed_hosts=eff_allow, allow_http=allow_http, resolver=resolver)
        allow_list = eff_allow
        route_base = url
    else:
        assert dist_dir is not None
        raw_dist = Path(dist_dir).expanduser()
        reject_symlink_chain(raw_dist)
        resolved_dist = raw_dist.resolve()
        if not resolved_dist.is_dir():
            raise FileNotFoundError(f"--dist directory does not exist: {resolved_dist}")
        dist_dir = resolved_dist
        route_base = "http://127.0.0.1:8080"

    validated_routes: list[str] = []
    for r in routes:
        if r.strip():
            validated_routes.append(validate_twin_route(r, base_url=route_base))

    resolved_pw: str | None = None
    if playwright_from:
        raw_pw = Path(playwright_from).expanduser()
        reject_symlink_chain(raw_pw)
        resolved_pw = str(raw_pw.resolve())

    node_bin = shutil.which("node")
    if not node_bin:
        return 1, {"ok": False, "code": "node_missing", "message": "Node.js binary not found on PATH."}

    twin_script = module_root / "scripts" / "twin.mjs"
    if not twin_script.is_file():
        return 1, {"ok": False, "code": "twin_script_missing", "message": f"Missing {twin_script}"}

    cmd: list[str] = [node_bin, str(twin_script), "--out", str(out), "--timeout", str(int(timeout_ms))]
    if url:
        cmd.extend(["--url", url])
        for h in allow_list:
            cmd.extend(["--allow-host", h])
    else:
        cmd.extend(["--dist", str(dist_dir)])

    for r in validated_routes:
        cmd.extend(["--route", r])
    for c in crops:
        if c.strip():
            cmd.extend(["--crop", c.strip()])
    if no_mobile:
        cmd.append("--no-mobile")
    if no_full_page:
        cmd.append("--no-full-page")
    if allow_http:
        cmd.append("--allow-http")
    if overwrite:
        cmd.append("--overwrite")
    if resolved_pw:
        cmd.extend(["--playwright-from", resolved_pw])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    try:
        payload = json.loads(stdout_text)
    except Exception:
        payload = {
            "ok": False,
            "code": "twin_output_parse_error",
            "returncode": proc.returncode,
            "stdout": stdout_text.strip(),
            "stderr": stderr_text.strip(),
        }
    return proc.returncode, payload


def _normalize_route_key(route: str) -> str:
    r = (route or "/").strip()
    if r in ("/", "/index.html", "/index.htm"):
        return "/"
    r = r.rstrip("/")
    if r.lower().endswith(".html"):
        r = r[:-5]
    return r or "/"


def _screenshots_for_route(twin: dict[str, Any], page_id: str) -> dict[str, str]:
    out_dir = Path(twin.get("outDir", ""))
    mapping: dict[str, str] = {}
    for sc in twin.get("screenshots", []):
        if sc.get("page") == page_id and sc.get("kind") and sc.get("file"):
            p = out_dir / sc["file"] if str(out_dir) else Path(sc["file"])
            mapping[sc["kind"]] = str(p)
    return mapping


def diff_twins(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    max_section_y_delta: int = 80,
    max_section_height_delta: int = 80,
    output_path: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Pure offline parity diff between baseline twin.json and candidate twin.json."""
    b_raw = Path(baseline_path).expanduser()
    c_raw = Path(candidate_path).expanduser()
    reject_symlink_chain(b_raw)
    reject_symlink_chain(c_raw)

    baseline = json.loads(safe_read_bytes(b_raw.resolve(), max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8"))
    candidate = json.loads(safe_read_bytes(c_raw.resolve(), max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8"))

    b_pages = {_normalize_route_key(p.get("route", "/")): p for p in baseline.get("pages", [])}
    c_pages = {_normalize_route_key(p.get("route", "/")): p for p in candidate.get("pages", [])}

    regressions: list[dict[str, Any]] = []
    drifts: list[dict[str, Any]] = []
    route_diffs: list[dict[str, Any]] = []

    for route_key, bp in b_pages.items():
        cp = c_pages.get(route_key)
        if cp is None:
            regressions.append(
                {
                    "code": "route_missing",
                    "route": route_key,
                    "message": f"Baseline route {route_key} is missing from candidate twin.",
                }
            )
            continue

        b_shots = _screenshots_for_route(baseline, bp.get("id", ""))
        c_shots = _screenshots_for_route(candidate, cp.get("id", ""))
        side_by_side = {
            kind: {"baseline": b_shots.get(kind), "candidate": c_shots.get(kind)}
            for kind in sorted(set(b_shots.keys()) | set(c_shots.keys()))
        }

        route_record: dict[str, Any] = {
            "route": route_key,
            "screenshots": side_by_side,
            "viewports": {},
        }

        for vp in ("desktop", "mobile"):
            b_run = bp.get(vp)
            c_run = cp.get(vp)
            if not b_run or not c_run:
                continue

            b_status = b_run.get("status")
            c_status = c_run.get("status")
            if b_status == 200 and c_status != 200:
                regressions.append(
                    {
                        "code": "status_regression",
                        "route": route_key,
                        "viewport": vp,
                        "baseline": b_status,
                        "candidate": c_status,
                        "message": f"Route {route_key} ({vp}) HTTP status regressed from {b_status} to {c_status}.",
                    }
                )

            # Runtime error regressions (new errors not present in baseline)
            b_errs = b_run.get("errors", {})
            c_errs = c_run.get("errors", {})
            for err_key, code in (
                ("pageErrors", "page_error_regression"),
                ("consoleErrors", "console_error_regression"),
                ("requestFailures", "request_failure_regression"),
            ):
                b_cnt = len(b_errs.get(err_key, []))
                c_list = c_errs.get(err_key, [])
                if len(c_list) > b_cnt:
                    regressions.append(
                        {
                            "code": code,
                            "route": route_key,
                            "viewport": vp,
                            "baselineCount": b_cnt,
                            "candidateCount": len(c_list),
                            "sample": c_list[:5],
                            "message": f"Route {route_key} ({vp}) increased {err_key} from {b_cnt} to {len(c_list)}.",
                        }
                    )

            b_ex = b_run.get("extraction") or {}
            c_ex = c_run.get("extraction") or {}

            # Horizontal overflow regression
            b_ov = (b_ex.get("horizontalOverflow") or {}).get("overflowPx", 0)
            c_ov = (c_ex.get("horizontalOverflow") or {}).get("overflowPx", 0)
            if c_ov > b_ov and c_ov > 0:
                regressions.append(
                    {
                        "code": "horizontal_overflow_regression",
                        "route": route_key,
                        "viewport": vp,
                        "baselineOverflowPx": b_ov,
                        "candidateOverflowPx": c_ov,
                        "offenders": (c_ex.get("horizontalOverflow") or {}).get("offenders", []),
                        "message": f"Route {route_key} ({vp}) introduced horizontal overflow (+{c_ov}px vs baseline +{b_ov}px).",
                    }
                )

            # Missing loaded fonts regression
            b_fonts = set(b_ex.get("loadedFonts", []))
            c_fonts = set(c_ex.get("loadedFonts", []))
            missing_fonts = sorted(b_fonts - c_fonts)
            if missing_fonts:
                regressions.append(
                    {
                        "code": "missing_loaded_font",
                        "route": route_key,
                        "viewport": vp,
                        "missingFonts": missing_fonts,
                        "message": f"Route {route_key} ({vp}) lost loaded font(s): {', '.join(missing_fonts)}.",
                    }
                )

            # Desktop computed-style & section stack comparisons
            vp_summary: dict[str, Any] = {
                "status": {"baseline": b_status, "candidate": c_status},
                "overflowPx": {"baseline": b_ov, "candidate": c_ov},
                "missingFonts": missing_fonts,
            }

            if vp == "desktop":
                b_secs = b_ex.get("sections", [])
                c_secs = c_ex.get("sections", [])
                section_deltas: list[dict[str, Any]] = []
                for idx in range(min(len(b_secs), len(c_secs))):
                    bs, cs = b_secs[idx], c_secs[idx]
                    dy = abs(int(cs.get("y", 0)) - int(bs.get("y", 0)))
                    dh = abs(int(cs.get("height", 0)) - int(bs.get("height", 0)))
                    if dy > max_section_y_delta or dh > max_section_height_delta:
                        delta_entry = {
                            "code": "section_layout_drift",
                            "route": route_key,
                            "index": idx,
                            "baselineSelector": bs.get("selector"),
                            "candidateSelector": cs.get("selector"),
                            "yDeltaPx": dy,
                            "heightDeltaPx": dh,
                            "message": f"Section #{idx} on {route_key} shifted (dY={dy}px, dH={dh}px).",
                        }
                        section_deltas.append(delta_entry)
                        drifts.append(delta_entry)
                vp_summary["sectionDriftCount"] = len(section_deltas)

                # Headings typography / H1 drift
                b_h1 = [h for h in b_ex.get("headings", []) if h.get("tag") == "h1"]
                c_h1 = [h for h in c_ex.get("headings", []) if h.get("tag") == "h1"]
                if b_h1 and not c_h1:
                    regressions.append(
                        {
                            "code": "heading_h1_missing",
                            "route": route_key,
                            "message": f"Route {route_key} lost its visible H1 heading.",
                        }
                    )
                elif b_h1 and c_h1:
                    if b_h1[0].get("fontSize") != c_h1[0].get("fontSize") or b_h1[0].get("fontFamily") != c_h1[0].get("fontFamily"):
                        drifts.append(
                            {
                                "code": "heading_typography_drift",
                                "route": route_key,
                                "baseline": {"fontSize": b_h1[0].get("fontSize"), "fontFamily": b_h1[0].get("fontFamily")},
                                "candidate": {"fontSize": c_h1[0].get("fontSize"), "fontFamily": c_h1[0].get("fontFamily")},
                                "message": f"H1 typography drifted on {route_key}.",
                            }
                        )

                # Button count & radius token drift
                b_radii = {r.get("value") for r in b_ex.get("borderRadii", [])}
                c_radii = {r.get("value") for r in c_ex.get("borderRadii", [])}
                if b_radii != c_radii:
                    drifts.append(
                        {
                            "code": "border_radius_drift",
                            "route": route_key,
                            "added": sorted(c_radii - b_radii),
                            "removed": sorted(b_radii - c_radii),
                            "message": f"Border-radius tokens changed on {route_key}.",
                        }
                    )

                b_btns = len(b_ex.get("buttons", []))
                c_btns = len(c_ex.get("buttons", []))
                if b_btns != c_btns:
                    drifts.append(
                        {
                            "code": "button_count_drift",
                            "route": route_key,
                            "baseline": b_btns,
                            "candidate": c_btns,
                            "message": f"Visible CTA/button count changed on {route_key} ({b_btns} -> {c_btns}).",
                        }
                    )

                # Footer materiality drift
                b_footers = b_ex.get("footers", [])
                c_footers = c_ex.get("footers", [])
                if bool(b_footers) != bool(c_footers):
                    drifts.append(
                        {
                            "code": "footer_presence_drift",
                            "route": route_key,
                            "baselineCount": len(b_footers),
                            "candidateCount": len(c_footers),
                            "message": f"Footer presence changed on {route_key}.",
                        }
                    )
                elif b_footers and c_footers:
                    if b_footers[0].get("backgroundColor") != c_footers[0].get("backgroundColor"):
                        drifts.append(
                            {
                                "code": "footer_materiality_drift",
                                "route": route_key,
                                "baselineBg": b_footers[0].get("backgroundColor"),
                                "candidateBg": c_footers[0].get("backgroundColor"),
                                "message": f"Footer background changed on {route_key}.",
                            }
                        )

            route_record["viewports"][vp] = vp_summary

        route_diffs.append(route_record)

    result: dict[str, Any] = {
        "ok": len(regressions) == 0,
        "schema": "legends.astro-twin-diff/v1",
        "baseline": str(b_raw.resolve()),
        "candidate": str(c_raw.resolve()),
        "routesCompared": len(route_diffs),
        "regressionCount": len(regressions),
        "driftCount": len(drifts),
        "regressions": regressions,
        "drifts": drifts,
        "routeDiffs": route_diffs,
    }

    if output_path:
        written = safe_write_text(output_path, json.dumps(result, indent=2) + "\n", overwrite=overwrite)
        result["writtenTo"] = str(written)

    return result
