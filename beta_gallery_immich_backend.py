"""Immich integration for the SDCodex Beta Gallery plugin.

Beta channel copy of the Gallery Immich bridge: it keeps its own module name,
blueprint and ``/beta-gallery/api/immich`` prefix, and stores server rows in
``beta-gallery-immich.json`` so the stable Gallery plugin's configuration is
never touched.

Remote Immich servers are browsed through this module so the API key never
reaches the browser and so Immich does not need CORS headers (an ``<img>`` tag
cannot send an ``x-api-key`` header at all).

The FolderFrame UI only understands directory listings, so every configured
server is exposed as an ordinary web directory::

    /beta-gallery/api/immich/<sid>/browse/            albums  (folders)
    /beta-gallery/api/immich/<sid>/browse/<album>/    assets  (files)
    /beta-gallery/api/immich/<sid>/browse/<album>/<f> preview bytes for the viewer
    /beta-gallery/api/immich/<sid>/original/<album>/<f>  the untouched original
    /beta-gallery/api/immich/<sid>/thumb/<album>/<f>.webp  small grid thumbnail
    /beta-gallery/api/immich/<sid>/meta?album=&file=  details panel metadata

``folderframe.config.json`` gains one source per enabled server, so the
gallery's source switcher, breadcrumbs, grid and viewer work unchanged.

Server rows (name, URL, API key, TLS preference) live in
``<CONFIG>/beta-gallery-immich.json`` with mode 0600. The API key is never returned
by any endpoint.
"""

import html
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

from flask import Blueprint, Response, jsonify, request

immich_bp = Blueprint("beta_gallery_immich", __name__, url_prefix="/beta-gallery/api/immich")

SETTINGS_FILENAME = "beta-gallery-immich.json"
CONFIG_ENV_VARS = ("BETA_GALLERY_IMMICH_CONFIG", "GALLERY_IMMICH_CONFIG")
DIR_ENV_VARS = ("CONFIG", "CONFIG_DIR")

REQUEST_TIMEOUT = 20
LISTING_TTL = 20.0        # Albums/assets are re-read at most this often.
PAGE_SIZE = 1000
MAX_PAGES = 8
CHUNK = 64 * 1024

# Only formats the gallery UI can actually display are listed.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}

_settings_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache: dict = {}


class ImmichError(Exception):
    """Raised for any Immich call that could not be completed."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------- storage ---

def _plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _workspace_root() -> str:
    cur = _plugin_dir()
    for _ in range(5):
        if os.path.isdir(os.path.join(cur, "plugins")) and os.path.isdir(os.path.join(cur, "backend")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(_plugin_dir(), "..", ".."))


def config_dir() -> str:
    """Where plugin settings are stored (volume-mounted when available)."""
    for var in CONFIG_ENV_VARS:
        explicit = os.environ.get(var)
        if explicit:
            d = os.path.dirname(os.path.abspath(explicit)) or _workspace_root()
            os.makedirs(d, exist_ok=True)
            return d
    for var in DIR_ENV_VARS:
        explicit = os.environ.get(var)
        if explicit:
            os.makedirs(explicit, exist_ok=True)
            return explicit
    d = os.path.join(_workspace_root(), "config")
    os.makedirs(d, exist_ok=True)
    return d


def settings_path() -> str:
    for var in CONFIG_ENV_VARS:
        explicit = os.environ.get(var)
        if explicit:
            return os.path.abspath(explicit)
    return os.path.join(config_dir(), SETTINGS_FILENAME)


def _load_settings() -> dict:
    path = settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    servers = data.get("servers")
    if not isinstance(servers, list):
        servers = []
    clean = []
    for row in servers:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        url = str(row.get("url") or "").strip()
        if not sid or not url:
            continue
        clean.append({
            "id": sid,
            "name": str(row.get("name") or sid).strip() or sid,
            "url": url.rstrip("/"),
            "api_key": str(row.get("api_key") or ""),
            "enabled": row.get("enabled") is not False,
            "verify_tls": row.get("verify_tls") is not False,
            "last_check": row.get("last_check") if isinstance(row.get("last_check"), dict) else None,
        })
    return {"servers": clean}


def _save_settings(data: dict) -> None:
    path = settings_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _public(row: dict) -> dict:
    """A server row without the secret."""
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "enabled": bool(row.get("enabled", True)),
        "verifyTls": bool(row.get("verify_tls", True)),
        "hasKey": bool(row.get("api_key")),
        "lastCheck": row.get("last_check"),
    }


def find_server(sid: str):
    for row in _load_settings()["servers"]:
        if row["id"] == sid:
            return row
    return None


def enabled_servers() -> list:
    return [row for row in _load_settings()["servers"] if row.get("enabled", True)]


def invalidate_cache(sid: str | None = None) -> None:
    with _cache_lock:
        if sid is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == sid]:
            _cache.pop(key, None)


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    return None


def _cache_put(key, value, ttl=LISTING_TTL):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)
    return value


def _write_blocked() -> bool:
    """Block mutations when the core reports no session (deny only then).

    Standalone plugin deployments have no core auth module to consult, so the
    check is a no-op there — matching how the rest of this plugin's API works.
    """
    try:
        from sdcodex import auth as core_auth
    except Exception:
        return False
    try:
        return core_auth.current_user() is None
    except Exception:
        return True


@immich_bp.before_request
def _immich_auth_guard():
    """Every Immich route (including reads and proxied originals) needs a session."""
    if _write_blocked():
        return jsonify({"error": "unauthorized"}), 401
    return None


# ---------------------------------------------------------------- client ---

def _base_url(server: dict) -> str:
    return server["url"].rstrip("/")


def _open(server: dict, path: str, method: str = "GET", body=None, timeout: int = REQUEST_TIMEOUT,
          headers: dict | None = None):
    url = f"{_base_url(server)}/api{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    # Some reverse proxies / WAFs (Cloudflare-style bot checks, allowlists)
    # reject urllib's default "Python-urllib/…" UA with a bare 403, which then
    # looks exactly like a rejected API key. Identify as SDCodex instead.
    req.add_header("User-Agent", "SDCodex/1.0 (+https://github.com/LainStable/SDCodex)")
    req.add_header("x-api-key", server.get("api_key") or "")
    req.add_header("Accept", "*/*")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    context = None
    if not server.get("verify_tls", True):
        context = ssl._create_unverified_context()
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=context)
    except urllib.error.HTTPError as exc:
        raise ImmichError(f"{method} {path} failed with HTTP {exc.code}", exc.code) from None
    except urllib.error.URLError as exc:
        raise ImmichError(f"Cannot reach {_base_url(server)}: {exc.reason}") from None
    except Exception as exc:  # pragma: no cover - defensive
        raise ImmichError(f"{method} {path} failed: {exc}") from None


def _json_call(server: dict, path: str, method: str = "GET", body=None, timeout: int = REQUEST_TIMEOUT):
    with closing(_open(server, path, method=method, body=body, timeout=timeout)) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ImmichError(f"{path} did not return JSON", 502) from None


def _fetch_albums(server: dict) -> list:
    data = _json_call(server, "/albums")
    albums = []
    for row in data or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        albums.append({
            "id": str(row["id"]),
            "name": str(row.get("albumName") or "").strip() or "Untitled album",
            "count": row.get("assetCount"),
            "cover": row.get("albumThumbnailAssetId"),
        })
    albums.sort(key=lambda a: a["name"].casefold())
    return albums


def _ext_for_mime(mime: str, kind: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    known = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/avif": ".avif", "image/bmp": ".bmp", "image/heic": ".heic", "image/heif": ".heif",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    }
    return known.get(mime, ".mp4" if kind == "video" else ".jpg")


def _normalize_asset(row: dict) -> dict:
    exif = row.get("exifInfo") if isinstance(row.get("exifInfo"), dict) else {}
    mime = str(row.get("originalMimeType") or row.get("mimeType") or exif.get("mimeType") or "")
    kind = "video" if str(row.get("type") or "").upper() == "VIDEO" or mime.startswith("video/") else "image"
    asset_id = str(row.get("id") or "")
    name = str(row.get("originalFileName") or "").strip()
    if not name:
        name = f"{asset_id or 'asset'}{_ext_for_mime(mime, kind)}"
    return {
        "id": asset_id,
        "fileName": name,
        "kind": kind,
        "mime": mime,
        "width": exif.get("exifImageWidth") or row.get("width"),
        "height": exif.get("exifImageHeight") or row.get("height"),
        "takenAt": row.get("fileCreatedAt") or exif.get("dateTimeOriginal"),
        "size": exif.get("fileSizeInByte") or row.get("fileSizeInByte"),
        "description": str(row.get("description") or exif.get("description") or ""),
        "make": exif.get("make") or "",
        "model": exif.get("model") or "",
        "lens": exif.get("lensModel") or "",
        "iso": exif.get("iso"),
        "exposure": exif.get("exposureTime"),
        "aperture": exif.get("fNumber"),
        "focalLength": exif.get("focalLength"),
        "rating": row.get("rating"),
        "trashed": bool(row.get("isTrashed")),
    }


def _fetch_album_assets(server: dict, album_id: str) -> list:
    """Assets of one album, newest API first with a legacy fallback."""
    if not album_id:
        return []
    items = []
    try:
        page = 1
        while page <= MAX_PAGES:
            result = _json_call(server, "/search/metadata", method="POST",
                                body={"albumIds": [album_id], "page": page, "size": PAGE_SIZE})
            block = (result or {}).get("assets") if isinstance(result, dict) else None
            chunk = (block or {}).get("items") if isinstance(block, dict) else None
            chunk = chunk if isinstance(chunk, list) else []
            items.extend(row for row in chunk if isinstance(row, dict))
            total = (block or {}).get("total") if isinstance(block, dict) else None
            if len(chunk) < PAGE_SIZE or (isinstance(total, int) and len(items) >= total):
                break
            page += 1
        return [_normalize_asset(row) for row in items]
    except ImmichError as exc:
        if exc.status not in (400, 404, 405, 500, 501):
            raise
    # Older servers return the album's assets inline.
    album = _json_call(server, f"/albums/{urllib.parse.quote(album_id, safe='')}") or {}
    assets = album.get("assets") if isinstance(album, dict) else None
    if not isinstance(assets, list):
        return []
    return [_normalize_asset(row) for row in assets if isinstance(row, dict)]


# --------------------------------------------------------------- listing ---

_ILLEGAL = dict.fromkeys(map(ord, '/\\\x00\r\n\t'), "_")


def _clean_name(value: str, fallback: str) -> str:
    name = str(value or "").translate(_ILLEGAL).strip()
    name = re.sub(r"\s+", " ", name).lstrip(". ").strip()
    return name or fallback


def _display_name_map(items: list, name_of) -> dict:
    """Map display name -> item, de-duplicating names the UI would collide on."""
    mapping = {}
    for index, item in enumerate(items, start=1):
        base = _clean_name(name_of(item), f"item-{index}")
        name = base
        suffix = 2
        while name.casefold() in mapping:
            stem, ext = os.path.splitext(base)
            name = f"{stem} ({suffix}){ext}"
            suffix += 1
        mapping[name] = item
    return mapping


def _albums_index(server: dict) -> dict:
    key = (server["id"], "albums")

    def build():
        albums = _fetch_albums(server)
        return {"albums": albums, "names": _display_name_map(albums, lambda a: a["name"])}

    return _cache_get(key) or _cache_put(key, build())


def _assets_index(server: dict, album_id: str) -> dict:
    key = (server["id"], "assets", album_id)

    def build():
        assets = [a for a in _fetch_album_assets(server, album_id) if _is_displayable(a)]
        return {"assets": assets, "names": _display_name_map(assets, lambda a: a["fileName"])}

    return _cache_get(key) or _cache_put(key, build())


def _is_displayable(asset: dict) -> bool:
    ext = os.path.splitext(asset.get("fileName") or "")[1].lower()
    return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS


def _index_for(sid: str):
    server = find_server(sid)
    if not server:
        raise ImmichError("Unknown Immich server", 404)
    return server


def _resolve_album(server: dict, name: str):
    index = _albums_index(server)
    wanted = _clean_name(urllib.parse.unquote(name or ""), "")
    if not wanted:
        return None
    for display, album in index["names"].items():
        if display.casefold() == wanted.casefold():
            return album
    for album in index["albums"]:
        if album["name"].casefold() == wanted.casefold() or album["id"] == wanted:
            return album
    return None


def _resolve_asset(server: dict, album_name: str, file_name: str):
    album = _resolve_album(server, album_name)
    if not album:
        return None, None
    index = _assets_index(server, album["id"])
    wanted = _clean_name(urllib.parse.unquote(file_name or ""), "")
    if not wanted:
        return album, None
    for display, asset in index["names"].items():
        if display.casefold() == wanted.casefold():
            return album, asset
    return album, None


def _listing_html(title: str, entries: list, directories: bool = False) -> str:
    """Directory-listing HTML shaped like the local one the gallery scrapes.

    Folder hrefs carry a trailing slash: the gallery only treats an entry as a
    directory when the URL ends with one.
    """
    rows = []
    suffix = "/" if directories else ""
    for name, label in entries:
        href = urllib.parse.quote(name, safe="") + suffix
        rows.append(f'<li><a href="{href}">{html.escape(label)}</a></li>')
    return (
        '<!doctype html><meta charset="utf-8"><title>Immich gallery</title>'
        f"<h1>{html.escape(title)}</h1><ul>{''.join(rows)}</ul>"
    )


# ---------------------------------------------------------------- routes ---

@immich_bp.get("/servers")
def list_servers():
    return jsonify({"servers": [_public(row) for row in _load_settings()["servers"]]})


@immich_bp.post("/servers")
def save_server():
    """Create or update a server. Omit ``apiKey`` to keep the stored one."""
    if _write_blocked():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip().rstrip("/")
    if not name:
        return jsonify({"error": "A name is required."}), 400
    if not re.match(r"^https?://[^\s]+$", url):
        return jsonify({"error": "The server URL must start with http:// or https://"}), 400
    if "/api" in urllib.parse.urlsplit(url).path.rstrip("/"):
        url = re.sub(r"/api/?$", "", url)
    with _settings_lock:
        store = _load_settings()
        sid = str(data.get("id") or "").strip()
        row = next((r for r in store["servers"] if r["id"] == sid), None)
        if row is None:
            sid = f"immich-{secrets.token_hex(4)}"
            row = {"id": sid, "api_key": "", "last_check": None}
            store["servers"].append(row)
        api_key = data.get("apiKey")
        if isinstance(api_key, str) and api_key.strip():
            row["api_key"] = api_key.strip()
        elif not row.get("api_key"):
            return jsonify({"error": "An API key is required for a new server."}), 400
        row["name"] = name
        row["url"] = url
        row["enabled"] = data.get("enabled") is not False
        if isinstance(data.get("verifyTls"), bool):
            row["verify_tls"] = data["verifyTls"]
        row.setdefault("verify_tls", True)
        _save_settings(store)
        invalidate_cache(row["id"])
        return jsonify({"ok": True, "server": _public(row)})


@immich_bp.delete("/servers/<sid>")
def delete_server(sid: str):
    if _write_blocked():
        return jsonify({"error": "unauthorized"}), 401
    with _settings_lock:
        store = _load_settings()
        keep = [row for row in store["servers"] if row["id"] != sid]
        if len(keep) == len(store["servers"]):
            return jsonify({"error": "Unknown server"}), 404
        store["servers"] = keep
        _save_settings(store)
    invalidate_cache(sid)
    return jsonify({"ok": True})


def _probe_identity(row: dict) -> str:
    """Confirm the API key is accepted and return the account name.

    ``/users/me`` is the modern endpoint; older servers may not expose it, so a
    missing route falls through to a call every Immich version honours instead
    of reporting a false failure. A 401/403 anywhere means the key is wrong.
    """
    for path in ("/users/me", "/albums"):
        try:
            data = _json_call(row, path)
        except ImmichError as exc:
            if exc.status in (404, 405):
                continue
            raise
        if isinstance(data, dict):
            return str(data.get("name") or data.get("email") or "")
        return ""
    return ""


@immich_bp.post("/servers/<sid>/test")
def test_server(sid: str):
    if _write_blocked():
        return jsonify({"error": "unauthorized"}), 401
    row = find_server(sid)
    if not row:
        return jsonify({"error": "Unknown server"}), 404
    result = {"ok": False, "version": "", "user": "", "error": ""}
    try:
        try:
            version = _json_call(row, "/server/version")
        except ImmichError as exc:
            if exc.status in (404, 405):
                # Older servers only expose the legacy version route.
                version = _json_call(row, "/server-info/version")
            else:
                raise
        result["version"] = version.get("major", "") if isinstance(version, dict) else str(version or "")
        if isinstance(version, dict) and version.get("major"):
            result["version"] = f"v{version.get('major')}.{version.get('minor')}.{version.get('patch')}"
        try:
            result["user"] = _probe_identity(row)
        except ImmichError as exc:
            if exc.status in (401, 403):
                raise ImmichError(
                    "API key rejected \u2014 check it in Immich \u2192 User settings \u2192 API Keys. "
                    "If the key is correct, a reverse proxy in front of Immich may be "
                    "stripping the x-api-key header.", exc.status)
            raise
        result["ok"] = True
    except ImmichError as exc:
        if exc.status in (401, 403):
            # /server/version needs no key: a refusal here means something in
            # front of Immich (proxy / WAF / allowlist) blocked the API call,
            # not that the key is wrong.
            result["error"] = (f"Immich refused the request (HTTP {exc.status}). "
                               "Check any reverse proxy, firewall or bot protection "
                               "in front of Immich \u2014 it may be blocking API clients.")
        else:
            result["error"] = str(exc)
    with _settings_lock:
        store = _load_settings()
        for entry in store["servers"]:
            if entry["id"] == sid:
                entry["last_check"] = {
                    "ok": bool(result["ok"]),
                    "version": result["version"],
                    "error": result["error"],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
        _save_settings(store)
    return jsonify(result)


@immich_bp.post("/cache/clear")
def clear_cache():
    invalidate_cache(request.args.get("sid") or None)
    return jsonify({"ok": True})


@immich_bp.get("/<sid>/albums")
def albums(sid: str):
    try:
        server = _index_for(sid)
        index = _albums_index(server)
    except ImmichError as exc:
        return jsonify({"error": str(exc)}), exc.status or 502
    return jsonify({
        "server": {"id": server["id"], "name": server["name"]},
        "albums": [{"id": a["id"], "name": a["name"], "assetCount": a.get("count"), "coverAssetId": a.get("cover")}
                   for a in index["albums"]],
    })


@immich_bp.get("/<sid>/assets")
def assets(sid: str):
    """JSON listing for one album (used for diagnostics and tests)."""
    album_name = request.args.get("album", "")
    try:
        server = _index_for(sid)
        album = _resolve_album(server, album_name) if album_name else None
        if album_name and not album:
            return jsonify({"error": "Unknown album"}), 404
        if album is None:
            index = _albums_index(server)
            return jsonify({
                "server": {"id": server["id"], "name": server["name"]},
                "album": None,
                "albums": [{"id": a["id"], "name": a["name"], "assetCount": a.get("count")} for a in index["albums"]],
                "assets": [],
            })
        index = _assets_index(server, album["id"])
    except ImmichError as exc:
        return jsonify({"error": str(exc)}), exc.status or 502
    return jsonify({
        "album": {"id": album["id"], "name": album["name"]},
        "assets": [{"id": a["id"], "fileName": a["fileName"], "kind": a["kind"], "mime": a["mime"],
                    "width": a["width"], "height": a["height"], "takenAt": a["takenAt"], "size": a["size"]}
                   for a in index["assets"]],
    })


@immich_bp.get("/<sid>/browse/")
@immich_bp.get("/<sid>/browse/<path:relpath>")
def browse(sid: str, relpath: str = ""):
    relpath = relpath or ""
    try:
        server = _index_for(sid)
        if relpath.endswith("/") or not relpath:
            album_part = relpath.rstrip("/")
            if not album_part:
                index = _albums_index(server)
                entries = [(name, name) for name in index["names"]]
                return Response(_listing_html(server["name"], entries, directories=True), mimetype="text/html")
            album = _resolve_album(server, album_part)
            if not album:
                return Response(_listing_html(f"{server['name']} / {album_part}", []), mimetype="text/html")
            index = _assets_index(server, album["id"])
            entries = [(name, name) for name in index["names"]]
            return Response(_listing_html(album["name"], entries), mimetype="text/html")

        album_part, _, file_part = relpath.rpartition("/")
        if not album_part:
            # "Albums" without the trailing slash still means the album.
            album = _resolve_album(server, relpath)
            if album:
                index = _assets_index(server, album["id"])
                return Response(_listing_html(album["name"], [(name, name) for name in index["names"]]),
                                mimetype="text/html")
            return Response("Not found", status=404, mimetype="text/plain")
        album, asset = _resolve_asset(server, album_part, file_part)
        if album and not asset:
            # Folder URL without the trailing slash.
            index = _assets_index(server, album["id"])
            return Response(_listing_html(album["name"], [(name, name) for name in index["names"]]), mimetype="text/html")
        if not asset:
            return Response("Not found", status=404, mimetype="text/plain")
    except ImmichError as exc:
        return Response(str(exc), status=exc.status or 502, mimetype="text/plain")

    if asset["kind"] == "video":
        return _stream_asset(server, asset, size=None, attachment=False)
    return _stream_asset(server, asset, size="preview", attachment=False)


@immich_bp.get("/<sid>/original/<path:relpath>")
def original(sid: str, relpath: str):
    album_part, _, file_part = (relpath or "").rpartition("/")
    try:
        server = _index_for(sid)
        album, asset = _resolve_asset(server, album_part, file_part)
    except ImmichError as exc:
        return Response(str(exc), status=exc.status or 502, mimetype="text/plain")
    if not asset:
        return Response("Not found", status=404, mimetype="text/plain")
    return _stream_asset(server, asset, size=None, attachment=True)


@immich_bp.get("/<sid>/thumb/<path:relpath>")
def thumb(sid: str, relpath: str):
    relpath = relpath or ""
    if relpath.lower().endswith(".webp"):
        relpath = relpath[: -len(".webp")]
    album_part, _, file_part = relpath.rpartition("/")
    try:
        server = _index_for(sid)
        album, asset = _resolve_asset(server, album_part, file_part)
    except ImmichError as exc:
        return Response(str(exc), status=exc.status or 502, mimetype="text/plain")
    if not asset:
        return Response("Not found", status=404, mimetype="text/plain")
    return _stream_asset(server, asset, size="thumbnail", attachment=False)


@immich_bp.get("/<sid>/meta")
def meta(sid: str):
    # The gallery's inspector sends ``folder`` (matching the local /meta route);
    # ``album`` is accepted too so a hand-typed URL works.
    album_name = request.args.get("folder") or request.args.get("album", "")
    file_name = request.args.get("file", "")
    try:
        server = _index_for(sid)
        album, asset = _resolve_asset(server, album_name, file_name)
    except ImmichError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status or 502
    if not asset:
        return jsonify({"ok": False, "error": "Asset not found on the Immich server"}), 404
    ext = os.path.splitext(asset["fileName"])[1].lstrip(".").upper()
    details = []
    if asset.get("takenAt"):
        details.append(f"Taken: {asset['takenAt']}")
    camera = " ".join(part for part in (asset.get("make"), asset.get("model")) if part)
    if camera:
        details.append(f"Camera: {camera}" + (f" · {asset['lens']}" if asset.get("lens") else ""))
    if asset.get("width") and asset.get("height"):
        details.append(f"Dimensions: {asset['width']} × {asset['height']}")
    if asset.get("size"):
        details.append(f"Size: {round(asset['size'] / 1024)} KB")
    exposure = " · ".join(str(part) for part in (
        (f"ISO {asset['iso']}" if asset.get("iso") else ""),
        (f"{asset['exposure']}s" if asset.get("exposure") else ""),
        (f"f/{asset['aperture']}" if asset.get("aperture") else ""),
        (f"{asset['focalLength']}mm" if asset.get("focalLength") else ""),
    ) if part)
    if exposure:
        details.append(f"Exposure: {exposure}")
    details.append(f"Immich server: {server['name']}")
    caption = asset.get("description") or "(no description on this Immich asset)"
    return jsonify({
        "ok": True,
        "fileName": asset["fileName"],
        "relpath": f"{album_name}/{file_name}".strip("/"),
        "size": asset.get("size") or 0,
        "size_fmt": "",
        "width": asset.get("width") or 0,
        "height": asset.get("height") or 0,
        "format": ext,
        "kind": asset["kind"],
        "caption": caption,
        "hasCaption": bool(asset.get("description")),
        "hasSidecar": False,
        "sdPrompt": "",
        "sdNegative": "",
        "sdSetting": "\n".join(details),
        "params": {},
        "hasWorkflow": False,
        "workflowJson": None,
        "immich": {"serverId": server["id"], "serverName": server["name"], "assetId": asset["id"]},
    })


# -------------------------------------------------------------- streaming ---

def _stream_asset(server: dict, asset: dict, size: str | None, attachment: bool):
    """Proxy an asset's bytes, preferring the requested Immich size."""
    attempts = []
    if size:
        attempts.append((f"/assets/{urllib.parse.quote(asset['id'], safe='')}/thumbnail"
                         f"?size={urllib.parse.quote(size, safe='')}", True))
    attempts.append((f"/assets/{urllib.parse.quote(asset['id'], safe='')}/original", False))

    headers = {}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    last_error = None
    for path, _sized in attempts:
        try:
            upstream = _open(server, path, timeout=REQUEST_TIMEOUT * 3, headers=headers)
        except ImmichError as exc:
            last_error = exc
            if exc.status in (400, 404):  # size not supported on this server
                continue
            raise
        return _passthrough(upstream, asset, attachment)
    raise last_error or ImmichError("Asset could not be fetched")


def _passthrough(upstream, asset: dict, attachment: bool):
    status = getattr(upstream, "status", 200) or 200
    content_type = upstream.headers.get("Content-Type") or ("video/mp4" if asset["kind"] == "video" else "image/jpeg")

    def generate():
        try:
            while True:
                chunk = upstream.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    response = Response(generate(), status=status, content_type=content_type)
    for header in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
        value = upstream.headers.get(header)
        if value:
            response.headers[header] = value
    response.headers["Cache-Control"] = "private, max-age=120"
    if attachment:
        quoted = urllib.parse.quote(asset["fileName"])
        response.headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quoted}"
    return response
