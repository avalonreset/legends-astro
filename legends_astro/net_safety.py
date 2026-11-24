"""Shared network (SSRF/DNS-rebinding/TLS) and filesystem (CWE-22/59/776, Windows CFA) safety primitives."""

from __future__ import annotations

import dataclasses
import http.client
import ipaddress
import os
import re
import secrets
import socket
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__

DEFAULT_MAX_PAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ASSET_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
MAX_XML_BYTES = 50 * 1024 * 1024

_MACOS_SYSTEM_SYMLINKS = frozenset(
    {Path("/var"), Path("/tmp"), Path("/etc"), Path("/private/var"), Path("/private/tmp"), Path("/private/etc")}
)
_CFA_PROTECTED_DIRS = ("Desktop", "Documents", "Pictures", "Videos", "Music", "Favorites")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_THIS_HOST_NET = ipaddress.ip_network("0.0.0.0/8")
_EXTRA_FORBIDDEN_IPV4_NETS = (
    _CGNAT_NET,
    _THIS_HOST_NET,
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)
_EXTRA_FORBIDDEN_IPV6_NETS = (
    ipaddress.ip_network("2001:0000::/32"),  # Teredo
    ipaddress.ip_network("2001:db8::/32"),   # Documentation
    ipaddress.ip_network("64:ff9b::/96"),    # NAT64 well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),  # NAT64 local-use prefix
    ipaddress.ip_network("100::/64"),        # RFC 6666 discard-only
    ipaddress.ip_network("::ffff:0:0:0/96"), # IPv4-translated
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_WINDOWS_BAD_CHARS_RE = re.compile(r"""[<>:"'|?*\x00-\x1f\x7f`(),;]""")
_WINDOWS_RESERVED_STEM_RE = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class FetchResult:
    """Result of a validated, bounded outbound HTTP(S) fetch."""

    url: str
    final_url: str
    status: int
    content_type: str
    headers: dict[str, str]
    body: bytes
    redirects: list[str]


def build_tls_context() -> ssl.SSLContext:
    """Build a strict TLS 1.2+ context using OS trust roots plus optional certifi CAs."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "TLSVersion"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        import certifi  # type: ignore[import-not-found]

        cafile = certifi.where()
        if cafile and os.path.isfile(cafile):
            ctx.load_verify_locations(cafile=cafile)
    except Exception:
        pass
    return ctx


def is_forbidden_ip(ip_str: str) -> bool:
    """Return True if an IP address is loopback, private, link-local, CGNAT, transition/NAT64/6to4/Teredo, benchmark, documentation, unspecified, multicast, or reserved."""
    try:
        addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        return True

    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        else:
            if addr.sixtofour is not None and is_forbidden_ip(str(addr.sixtofour)):
                return True
            if addr.teredo is not None:
                return True
            for net6 in _EXTRA_FORBIDDEN_IPV6_NETS:
                if addr in net6:
                    return True

    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    ):
        return True

    if isinstance(addr, ipaddress.IPv4Address):
        for net4 in _EXTRA_FORBIDDEN_IPV4_NETS:
            if addr in net4:
                return True

    return not addr.is_global


def is_forbidden_hostname(hostname: str) -> bool:
    """Return True if a hostname is empty, localhost, or a forbidden IP literal."""
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return is_forbidden_ip(host.strip("[]"))
    except ValueError:
        return False


def normalize_host(host: str) -> str:
    """Normalize a hostname or URL to a lowercase bare hostname, preserving IPv6 literals."""
    raw = host.strip()
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        raw = parsed.hostname or ""
    else:
        if "/" in raw:
            raw = raw.split("/", 1)[0]
        if raw.startswith("["):
            end = raw.find("]")
            if end != -1:
                raw = raw[1:end]
            else:
                raw = raw.strip("[]")
        elif raw.count(":") >= 2:
            # Unbracketed IPv6 literal (e.g. ::ffff:127.0.0.1 or ::1) - preserve intact
            pass
        elif ":" in raw:
            raw = raw.split(":", 1)[0]
    return raw.strip().lower().rstrip(".")


def format_sanitized_origin(url: str) -> str | None:
    """Build 'scheme://host[:port]' stripping any embedded user:password credentials (CWE-200)."""
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower()
    if not scheme or not host:
        return None
    host_fmt = f"[{host}]" if ":" in host and not host.startswith("[") else host
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        return f"{scheme}://{host_fmt}:{port}"
    return f"{scheme}://{host_fmt}"


def canonicalize_route(raw_route: str) -> str:
    """Canonicalize a route path to a single leading '/' (eliminating protocol-relative '//host' and backslashes)."""
    cleaned = (raw_route or "").strip().replace("\\", "/")
    return "/" + cleaned.lstrip("/")


def host_matches_allowlist(hostname: str, allowed_hosts: Iterable[str]) -> bool:
    """Check whether hostname matches an explicit allowlist entry (exact or *.domain)."""
    host = normalize_host(hostname)
    if not host:
        return False
    for entry in allowed_hosts:
        pattern = entry.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        else:
            norm = normalize_host(pattern)
            if norm and host == norm:
                return True
    return False


def resolve_and_validate_host(
    hostname: str,
    port: int,
    *,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> list[str]:
    """Resolve DNS for hostname and reject if any resolved address is non-global / SSRF-unsafe."""
    host = hostname.strip().lower().rstrip(".")
    if is_forbidden_hostname(host):
        raise ValueError(f"Refusing forbidden hostname (CWE-918): {hostname}")

    ips: list[str] = []
    if resolver is not None:
        ips = list(resolver(host, port))
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"DNS resolution failed for {host}: {exc}") from exc
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0] not in ips:
                ips.append(str(sockaddr[0]))

    if not ips:
        raise ValueError(f"DNS resolution returned no addresses for {host}")

    for ip in ips:
        if is_forbidden_ip(ip):
            raise ValueError(f"Refusing SSRF-unsafe resolved IP {ip} for host {host} (CWE-918)")
    return ips


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_http: bool = False,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[urllib.parse.SplitResult, list[str]]:
    """Validate scheme, credentials, allowlist, hostname, and resolved IPs before outbound connection."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise ValueError(
            f"Refusing URL scheme '{parsed.scheme}' (allowed: {', '.join(allowed_schemes)}; pass --allow-http for HTTP): {url}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Refusing URL with embedded credentials: {url}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"URL is missing a hostname: {url}")

    allow_list = [h for h in allowed_hosts if h and h.strip()]
    if not allow_list:
        raise ValueError("Refusing outbound request with empty host allowlist (--allow-host required).")
    if not host_matches_allowlist(host, allow_list):
        raise ValueError(f"Refusing outbound request to host '{host}' outside --allow-host allowlist.")

    port = parsed.port or (443 if scheme == "https" else 80)
    ips = resolve_and_validate_host(host, port, resolver=resolver)
    return parsed, ips


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a pre-validated IP address to prevent DNS rebinding."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ip: str, timeout: float = 15.0) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials a pre-validated IP while preserving TLS SNI and hostname verification."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        pinned_ip: str,
        context: ssl.SSLContext,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
        except Exception:
            raw_sock.close()
            raise


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects so callers validate every hop manually."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(f"Refusing automatic HTTP redirect ({code}) from {req.full_url}")


def build_safe_opener(*, context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    """Build a urllib opener with strict TLS, no proxies, and no automatic redirects."""
    tls_ctx = context or build_tls_context()
    return urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=tls_ctx),
    )


def safe_fetch_bytes(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_http: bool = False,
    max_bytes: int = DEFAULT_MAX_PAGE_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[[str, list[str], dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
) -> FetchResult:
    """Fetch a URL safely with DNS-rebinding pinning, allowlist, manual redirects, and byte caps."""
    current_url = url
    redirects: list[str] = []
    req_headers: dict[str, str] = {
        "User-Agent": f"legends-astro/{__version__}",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        req_headers.update(headers)

    tls_ctx = build_tls_context()

    for hop in range(max_redirects + 1):
        parsed, ips = validate_outbound_url(
            current_url,
            allowed_hosts=allowed_hosts,
            allow_http=allow_http,
            resolver=resolver,
        )

        if transport is not None:
            status, resp_headers_raw, body = transport(current_url, ips, dict(req_headers), timeout)
            if len(body) > max_bytes:
                raise ValueError(f"Response from {current_url} exceeded max_bytes ({max_bytes})")
            resp_headers = {k.lower(): v for k, v in resp_headers_raw.items()}
        else:
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            path_q = parsed.path or "/"
            if parsed.query:
                path_q = f"{path_q}?{parsed.query}"

            pinned_ip = ips[0]
            conn: http.client.HTTPConnection
            if parsed.scheme.lower() == "https":
                conn = _PinnedHTTPSConnection(host, port, pinned_ip=pinned_ip, context=tls_ctx, timeout=timeout)
            else:
                conn = _PinnedHTTPConnection(host, port, pinned_ip=pinned_ip, timeout=timeout)

            try:
                conn.request("GET", path_q, headers=req_headers)
                resp = conn.getresponse()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                content_len = resp_headers.get("content-length")
                if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                    raise ValueError(
                        f"Response from {current_url} declares Content-Length {content_len} > max_bytes ({max_bytes})"
                    )
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Response from {current_url} exceeded max_bytes ({max_bytes})")
                    chunks.append(chunk)
                body = b"".join(chunks)
            finally:
                conn.close()

        if status in _REDIRECT_STATUSES:
            location = resp_headers.get("location")
            if not location:
                raise ValueError(f"Redirect response ({status}) missing Location header: {current_url}")
            if hop >= max_redirects:
                raise ValueError(f"Exceeded max redirects ({max_redirects}) starting from {url}")
            next_url = urllib.parse.urljoin(current_url, location.strip())
            redirects.append(next_url)
            current_url = next_url
            req_headers = {
                k: v for k, v in req_headers.items() if k.lower() not in _SENSITIVE_REDIRECT_HEADERS
            }
            continue

        content_type = resp_headers.get("content-type", "")
        return FetchResult(
            url=url,
            final_url=current_url,
            status=status,
            content_type=content_type,
            headers=resp_headers,
            body=body,
            redirects=redirects,
        )

    raise ValueError(f"Exceeded max redirects ({max_redirects}) starting from {url}")


# ---------------------------------------------------------------------------
# Filesystem & XML safety primitives (shared across all commands)
# ---------------------------------------------------------------------------


_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def is_reparse_or_symlink(path: str | Path) -> bool:
    """Return True if path is a symlink, Windows NTFS junction, or reparse point (CWE-59)."""
    p = Path(path)
    try:
        if p.is_symlink():
            return True
    except OSError:
        return True
    try:
        if hasattr(p, "is_junction") and p.is_junction():
            return True
    except OSError:
        return True
    isjunction_fn = getattr(os.path, "isjunction", None)
    if callable(isjunction_fn):
        try:
            if isjunction_fn(str(p)):
                return True
        except OSError:
            return True
    if os.name == "nt":
        try:
            st = os.lstat(p)
            if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
    return False


def reject_symlink_chain(path: str | Path) -> None:
    """Refuse symlinks and NTFS junctions/reparse points on the target path or any non-system parent directory (CWE-59)."""
    raw = Path(path).expanduser()
    if is_reparse_or_symlink(raw):
        raise ValueError(f"Refusing symlink or reparse-point path (CWE-59): {raw}")
    for parent in raw.absolute().parents:
        if sys.platform == "darwin" and parent in _MACOS_SYSTEM_SYMLINKS:
            continue
        if is_reparse_or_symlink(parent):
            raise ValueError(f"Refusing path under symlinked or junction parent directory (CWE-59): {parent}")


def reject_controlled_folder_access(path: str | Path) -> None:
    """Refuse writes inside Windows Controlled Folder Access protected user directories (including OneDrive KFM, EDR Law 2)."""
    try:
        home = Path.home().resolve()
        resolved = Path(path).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"Unable to verify Controlled Folder Access path safety: {exc}") from exc

    base_roots: list[Path] = [home, home / "OneDrive"]
    for env_key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        env_val = os.environ.get(env_key)
        if env_val and env_val.strip():
            base_roots.append(Path(env_val.strip()).expanduser())

    seen_bases: set[Path] = set()
    for base in base_roots:
        try:
            base_res = base.resolve()
        except OSError:
            continue
        if base_res in seen_bases:
            continue
        seen_bases.add(base_res)
        for folder in _CFA_PROTECTED_DIRS:
            try:
                protected = (base_res / folder).resolve()
            except OSError as exc:
                raise ValueError(f"Unable to resolve Controlled Folder Access directory ({folder}): {exc}") from exc
            if resolved == protected or resolved.is_relative_to(protected):
                raise ValueError(
                    f"Refusing to write inside Controlled Folder Access protected directory ({folder}): {path}"
                )


def assert_path_within(root: Path, target: Path) -> Path:
    """Ensure target does not contain symlinks/junctions and resolves inside root (CWE-22, CWE-59)."""
    reject_symlink_chain(target)
    root_res = root.resolve()
    target_res = target.resolve()
    if not target_res.is_relative_to(root_res):
        raise ValueError(f"Refusing path escaping root {root_res}: {target}")
    return target_res


def safe_read_bytes(path: Path, max_bytes: int = DEFAULT_MAX_PAGE_BYTES) -> bytes:
    """Read a regular file after rejecting symlinks/junctions and enforcing byte-size bounds."""
    reject_symlink_chain(path)
    if not path.is_file() or is_reparse_or_symlink(path):
        raise ValueError(f"Refusing non-regular, symlink, or reparse-point file: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File exceeds {max_bytes} byte limit ({size} bytes): {path}")
    return path.read_bytes()


def safe_write_bytes(path: str | Path, data: bytes, *, overwrite: bool = False) -> Path:
    """Write raw bytes safely with symlink/junction refusal, CFA check, and O_CREAT | O_EXCL default."""
    raw = Path(path).expanduser()
    reject_symlink_chain(raw)
    target = raw.resolve()
    if is_reparse_or_symlink(target):
        raise ValueError(f"Refusing symlink or reparse-point output path: {target}")
    reject_controlled_folder_access(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_chain(target.parent)
    if overwrite:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        tmp_path: Path | None = None
        for _ in range(16):
            cand_tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
            try:
                fd = os.open(str(cand_tmp), flags, 0o600)
                tmp_path = cand_tmp
                break
            except FileExistsError:
                continue
        if tmp_path is None:
            raise OSError(f"Unable to allocate unique temporary file in {target.parent}")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if is_reparse_or_symlink(target.parent) or is_reparse_or_symlink(target):
                raise ValueError(f"Refusing symlink or reparse-point output path: {target}")
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(str(target), flags, 0o600)
        except FileExistsError:
            raise FileExistsError(
                f"Refusing to overwrite existing file without --overwrite: {target}"
            ) from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    return target


def safe_write_text(path: str | Path, content: str, *, overwrite: bool = False) -> Path:
    """Write UTF-8 text safely with symlink refusal, CFA check, and O_CREAT | O_EXCL default."""
    return safe_write_bytes(path, content.encode("utf-8"), overwrite=overwrite)


def sanitize_rel_path(raw_path: str) -> str:
    """Sanitize a URL or relative path into a deterministic, traversal-free POSIX relative path."""
    cleaned = raw_path.split("#", 1)[0].split("?", 1)[0]
    # Decode up to twice (handles %2520 double-encoding safely)
    for _ in range(2):
        unq = urllib.parse.unquote(cleaned)
        if unq == cleaned:
            break
        cleaned = unq
    cleaned = cleaned.replace("\\", "/")
    if "\x00" in cleaned:
        raise ValueError(f"Refusing NUL byte in path: {raw_path!r}")
    if re.match(r"^[a-zA-Z]:", cleaned):
        raise ValueError(f"Refusing drive-prefixed path: {raw_path!r}")

    segments: list[str] = []
    for seg in cleaned.split("/"):
        s = seg.strip()
        if not s or s == ".":
            continue
        if s == "..":
            raise ValueError(f"Refusing path traversal segment '..': {raw_path!r}")
        s = re.sub(r"\s+", "-", s)
        s = _WINDOWS_BAD_CHARS_RE.sub("-", s)
        s = s.strip(".") or "file"
        if _WINDOWS_RESERVED_STEM_RE.match(s):
            s = f"_{s}"
        segments.append(s)

    return "/".join(segments)


def safe_parse_xml_bytes(
    raw_bytes: bytes,
    *,
    source_label: str = "<xml>",
    max_bytes: int = MAX_XML_BYTES,
) -> ET.Element:
    """Parse XML bytes with size cap and unconditional DOCTYPE/ENTITY refusal (CWE-776)."""
    if len(raw_bytes) > max_bytes:
        raise ValueError(f"XML exceeds {max_bytes} byte limit ({len(raw_bytes)} bytes): {source_label}")
    lowered = raw_bytes.lower()
    if (
        b"<!doctype" in lowered
        or b"<!entity" in lowered
        or b"<\x00!\x00d\x00o\x00c\x00t\x00y\x00p\x00e" in lowered
        or b"<\x00!\x00e\x00n\x00t\x00i\x00t\x00y" in lowered
        or b"\x00<\x00!\x00d\x00o\x00c\x00t\x00y\x00p\x00e" in lowered
        or b"\x00<\x00!\x00e\x00n\x00t\x00i\x00t\x00y" in lowered
    ):
        raise ValueError(f"Refusing XML with DOCTYPE or ENTITY declarations (CWE-776): {source_label}")

    parser = ET.XMLParser()

    def _forbid_doctype(*_args: Any) -> None:
        raise ValueError(f"Refusing XML DOCTYPE declaration (CWE-776): {source_label}")

    def _forbid_entity(*_args: Any) -> None:
        raise ValueError(f"Refusing XML ENTITY declaration (CWE-776): {source_label}")

    expat_parser = getattr(parser, "parser", None) or getattr(parser, "_parser", None)
    if expat_parser is not None:
        expat_parser.StartDoctypeDeclHandler = _forbid_doctype
        expat_parser.EntityDeclHandler = _forbid_entity

    return ET.fromstring(raw_bytes, parser=parser)


# ---------------------------------------------------------------------------
# Linear O(N) HTML scanning & block stripping primitives (CWE-1333)
# ---------------------------------------------------------------------------

_TAG_BOUNDARY_CHARS = frozenset(" \t\r\n\f/>")
_TAG_DELIM_RE = re.compile(r"""[><"']""")


def scan_html_open_tag(text: str, start: int) -> tuple[int, int]:
    """Scan linearly from '<' at `start` for the closing '>' of an HTML opening tag.

    Respects double- and single-quoted attribute values without backtracking (O(N) worst-case).
    Returns `(gt_index, next_pos)`:
      - When closed cleanly: `(gt_index, gt_index + 1)` where `text[gt_index] == '>'`.
      - When unclosed (hit an unquoted '<', unclosed quote, or EOF): `(-1, next_pos)` where
        `next_pos > start` is the next '<' to resume from (or `len(text)` if none).
    """
    n = len(text)
    i = start + 1
    while i < n:
        m = _TAG_DELIM_RE.search(text, i)
        if m is None:
            return -1, n
        ch = m.group(0)
        idx = m.start()
        if ch == ">":
            return idx, idx + 1
        if ch == "<":
            return -1, idx
        close_q = text.find(ch, idx + 1)
        if close_q == -1:
            next_lt = text.find("<", idx + 1)
            return -1, (next_lt if next_lt != -1 else n)
        i = close_q + 1
    return -1, n


def find_html_close_tag(low_text: str, tag_name: str, start: int) -> tuple[int, int]:
    """Find the next valid `</{tag_name}...>` starting at `start` in lowercased HTML.

    Returns `(close_start, close_after_gt)` or `(-1, -1)` if none exists to EOF.
    """
    needle = f"</{tag_name}"
    needle_len = len(needle)
    n = len(low_text)
    search_from = start
    while True:
        idx = low_text.find(needle, search_from)
        if idx == -1:
            return -1, -1
        after = idx + needle_len
        if after < n and low_text[after] in _TAG_BOUNDARY_CHARS:
            gt = low_text.find(">", after)
            if gt == -1:
                return -1, -1
            return idx, gt + 1
        search_from = after


def replace_html_blocks(
    html_text: str,
    *,
    tags: Iterable[str] = (),
    strip_comments: bool = False,
    comment_filter: Callable[[str], bool] | None = None,
    replacer: Callable[[str, str], str] | None = None,
    default_replacement: str = " ",
) -> str:
    """Linear O(N) HTML comment and raw-element (<script>/<style>/<noscript>) block replacer.

    Memoizes missing closing delimiters ('-->' and '</tag>') so repeated unclosed openers
    never re-scan to EOF (eliminating O(N^2) ReDoS, CWE-1333).
    """
    if not html_text or "<" not in html_text:
        return html_text

    tag_tuple = tuple(t.lower() for t in tags)
    n = len(html_text)
    out: list[str] = []

    # Fast path when stripping/replacing HTML comments only
    if strip_comments and not tag_tuple:
        pos = 0
        while pos < n:
            start = html_text.find("<!--", pos)
            if start == -1:
                out.append(html_text[pos:])
                break
            end = html_text.find("-->", start + 4)
            if end == -1:
                out.append(html_text[pos:])
                break
            if start > pos:
                out.append(html_text[pos:start])
            block = html_text[start : end + 3]
            if comment_filter is None or comment_filter(block):
                out.append(replacer("comment", block) if replacer is not None else default_replacement)
            else:
                out.append(block)
            pos = end + 3
        return "".join(out)

    if not tag_tuple and not strip_comments:
        return html_text

    low = html_text.lower()
    no_comment_end = False
    no_tag_end = {t: False for t in tag_tuple}
    pos = 0

    while pos < n:
        lt = html_text.find("<", pos)
        if lt == -1:
            out.append(html_text[pos:])
            break
        if lt > pos:
            out.append(html_text[pos:lt])
            pos = lt

        if strip_comments and html_text.startswith("<!--", pos):
            if not no_comment_end:
                end = html_text.find("-->", pos + 4)
                if end != -1:
                    block = html_text[pos : end + 3]
                    if comment_filter is None or comment_filter(block):
                        out.append(replacer("comment", block) if replacer is not None else default_replacement)
                    else:
                        out.append(block)
                    pos = end + 3
                    continue
                no_comment_end = True
            out.append("<!--")
            pos += 4
            continue

        matched_tag: str | None = None
        for t in tag_tuple:
            t_end = pos + 1 + len(t)
            if low.startswith(t, pos + 1) and t_end < n and low[t_end] in _TAG_BOUNDARY_CHARS:
                matched_tag = t
                break

        if matched_tag is not None:
            gt, next_pos = scan_html_open_tag(html_text, pos)
            if gt == -1:
                out.append(html_text[pos:next_pos])
                pos = next_pos
                continue
            if not no_tag_end[matched_tag]:
                close_start, close_after = find_html_close_tag(low, matched_tag, gt + 1)
                if close_start != -1:
                    block = html_text[pos:close_after]
                    out.append(replacer(matched_tag, block) if replacer is not None else default_replacement)
                    pos = close_after
                    continue
                no_tag_end[matched_tag] = True
            out.append(html_text[pos : gt + 1])
            pos = gt + 1
            continue

        out.append("<")
        pos += 1

    return "".join(out)


def extract_html_tag_bodies(html_text: str, tag_name: str) -> list[str]:
    """Extract inner bodies of `<tag_name ...>...</tag_name>` blocks in strict O(N) time."""
    if not html_text or "<" not in html_text:
        return []
    target = tag_name.lower()
    t_len = len(target)
    low = html_text.lower()
    n = len(html_text)
    pos = 0
    bodies: list[str] = []

    while pos < n:
        lt = html_text.find("<", pos)
        if lt == -1:
            break
        t_end = lt + 1 + t_len
        if low.startswith(target, lt + 1) and t_end < n and low[t_end] in _TAG_BOUNDARY_CHARS:
            gt, next_pos = scan_html_open_tag(html_text, lt)
            if gt == -1:
                pos = next_pos
                continue
            close_start, close_after = find_html_close_tag(low, target, gt + 1)
            if close_start == -1:
                break
            bodies.append(html_text[gt + 1 : close_start])
            pos = close_after
            continue
        pos = lt + 1
    return bodies
