"""DEMO GLUE (Host side): download one file into the shared inbox for the Agent.

Why a tool and not the Agent's own shell: inside Codex's Windows sandbox the shell cannot make HTTPS
requests (curl: SEC_E_NO_CREDENTIALS, Invoke-WebRequest: connection error; 2026-10-08), and giving the
Agent write + network access on the Host is what we want to avoid anyway. The download is a Host
function (spec: Runtime Manager only takes the finished file); the Host owner may adopt this.

Limits, all checked here, not left to the Agent:
- https only, every redirect too (at most 5); the host name must resolve only to public addresses
  (no loopback, private LAN, link-local, ...), checked again for each redirect
- at most config.INPUT_FILE_MAX bytes; streamed to a private staging folder, never into the inbox
  half-written; then copied into the inbox (the Guest lists it once inbox.InboxWatcher sees it finished)
- the file name comes from the URL path, reduced to [A-Za-z0-9._-]; an existing name is never overwritten
- a .zip may be extracted (into inbox\\<name>\\): no absolute paths, no "..", no links, size and count limits
Nothing downloaded is ever run on the Host.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import shutil
import socket
import ssl
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PureWindowsPath

from sandbox_manager import config

MAX_REDIRECTS = 5
TIMEOUT_S = 60
ZIP_MAX_FILES = 200

TOOL = {
    "name": "inbox_download",
    "description": ("Download one file from an https URL into the shared inbox on this computer. The inbox is "
                    "visible read-only inside the Sandbox as C:\\Inbox, and a few seconds after the download each "
                    "file is copied to the Sandbox desktop folder Input and checked (Desktop\\input-check.html). "
                    "Nothing is run on this computer. With extract=true a .zip is unpacked into a folder of the "
                    "same name. Only public https addresses; at most 50 MiB."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["url"],
        "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 2048, "description": "https URL of the file"},
            "extract": {"type": "boolean", "default": False, "description": "unpack a .zip after downloading"},
        },
    },
    "annotations": {"title": "Download into the Sandbox inbox", "readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": True},
}


class DownloadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _check_url(url: str) -> urllib.parse.SplitResult:
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise DownloadError("INVALID_ARGUMENT", "only https URLs are allowed")
    if u.username or u.password:
        raise DownloadError("INVALID_ARGUMENT", "URLs with credentials are not allowed")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise DownloadError("INVALID_ARGUMENT", f"cannot resolve {u.hostname}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise DownloadError("POLICY_DENIED", f"{u.hostname} resolves to a non-public address ({ip})")
    return u


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                   # handled by hand so every hop is checked


def safe_name(name: str, fallback: str = "download.bin") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or fallback
    return name[:100]


def _unique(folder: Path, name: str) -> Path:
    p = folder / name
    stem, suffix, n = p.stem, p.suffix, 1
    while p.exists():
        n += 1
        p = folder / f"{stem}-{n}{suffix}"
    return p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, staging: Path) -> tuple[Path, str]:
    """Download to `staging`; returns (file, final URL)."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(current)
        req = urllib.request.Request(current, headers={"User-Agent": "SecureCUA-inbox/1.0"})
        try:
            resp = opener.open(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urllib.parse.urljoin(current, e.headers["Location"])
                continue
            raise DownloadError("INVALID_ARGUMENT", f"HTTP {e.code} from {urllib.parse.urlsplit(current).hostname}") from None
        except (urllib.error.URLError, OSError) as e:
            raise DownloadError("RUNTIME_UNAVAILABLE", f"download failed: {e}") from None
        with resp:
            length = resp.headers.get("Content-Length")
            if length and int(length) > config.INPUT_FILE_MAX:
                raise DownloadError("INVALID_ARGUMENT", f"file is {length} bytes (limit {config.INPUT_FILE_MAX})")
            name = safe_name(PureWindowsPath(urllib.parse.unquote(urllib.parse.urlsplit(current).path)).name)
            out = staging / name
            total = 0
            with out.open("wb") as f:
                while chunk := resp.read(1 << 16):
                    total += len(chunk)
                    if total > config.INPUT_FILE_MAX:
                        raise DownloadError("INVALID_ARGUMENT", f"file is over {config.INPUT_FILE_MAX} bytes")
                    f.write(chunk)
            if length and total != int(length):
                raise DownloadError("RUNTIME_UNAVAILABLE", f"download incomplete ({total} of {length} bytes)")
            return out, current
    raise DownloadError("INVALID_ARGUMENT", f"more than {MAX_REDIRECTS} redirects")


def _extract(archive: Path, dest: Path) -> list[Path]:
    out = []
    with zipfile.ZipFile(archive) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) > ZIP_MAX_FILES:
            raise DownloadError("INVALID_ARGUMENT", f"zip has more than {ZIP_MAX_FILES} files")
        if sum(m.file_size for m in members) > config.INPUT_TOTAL_MAX:
            raise DownloadError("INVALID_ARGUMENT", "zip contents are too large")
        for m in members:
            parts = PureWindowsPath(m.filename.replace("/", "\\")).parts
            if not parts or PureWindowsPath(m.filename).is_absolute() or any(p in ("..", "") or ":" in p for p in parts):
                raise DownloadError("POLICY_DENIED", f"unsafe path in zip: {m.filename!r}")
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                raise DownloadError("POLICY_DENIED", f"link in zip: {m.filename!r}")
            target = dest.joinpath(*[safe_name(p) for p in parts])
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def download_into_inbox(url: str, inbox: Path, extract: bool = False) -> dict:
    inbox = Path(inbox)
    with tempfile.TemporaryDirectory(prefix="securecua-dl-") as tmp:
        staging = Path(tmp)
        got, final_url = fetch(url, staging)
        files = [got]
        if extract:
            if got.suffix.lower() != ".zip":
                raise DownloadError("INVALID_ARGUMENT", "extract=true needs a .zip file")
            unpack = staging / "unpacked"
            files = _extract(got, unpack)
        # Copy into the inbox (new files only; the watcher lists each once it is complete).
        if extract:
            root = _unique(inbox, got.stem)
            placed = []
            for f in files:
                dst = root / f.relative_to(staging / "unpacked")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                placed.append(dst)
        else:
            dst = _unique(inbox, got.name)
            shutil.copyfile(got, dst)
            placed = [dst]
        result = [{"name": str(p.relative_to(inbox)), "size": p.stat().st_size, "sha256": _sha256(p)} for p in placed]
    return {"source": url, "final_url": final_url, "files": result,
            "sandbox_paths": [f"Desktop\\Input\\{r['name']}" for r in result],
            "next": "task_submit (if not yet), wait for the Sandbox, open Desktop\\input-check.html, "
                    "check the line is OK, then run the file from Desktop\\Input inside the Sandbox"}
