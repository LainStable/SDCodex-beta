"""Authenticated API for previewing, applying, and undoing media renames."""

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import requests
from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

ai_renamer_bp = Blueprint("beta_renamer_bp", __name__, url_prefix="/betarenamer/api")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
PROVIDERS = {"ollama", "lm-studio", "openai"}
CASE_STYLES = {"kebabCase", "camelCase", "snakeCase", "pascalCase", "noCase", "sentenceCase"}
MAX_FILES_PER_JOB = 250
MAX_ACTIVE_JOBS = 32
JOB_RETENTION_SECONDS = 24 * 60 * 60
MAX_IMAGE_BYTES = 25 * 1024 * 1024
DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")

_jobs = {}
_jobs_lock = threading.Lock()
_last_undo = None
_undo_lock = threading.Lock()


def _latest_job_locked():
    """Newest dry-run job: a running one wins, otherwise the last touched.

    Caller holds ``_jobs_lock``. Used by /jobs/active (page resume after
    browsing away) and /task (core floating progress notification)."""
    running = [job for job in _jobs.values() if job.get("status") == "running"]
    pool = running or list(_jobs.values())
    return max(pool, key=lambda job: job.get("updated_at", 0)) if pool else None


DEFAULT_SETTINGS = {
    "provider": "ollama",
    "model": "",
    "base_url": "http://127.0.0.1:11434",
    "case": "kebabCase",
    "max_chars": 35,
    "frames": 5,
    "language": "English",
    "custom_prompt": "",
    "include_subdirectories": True,
    "api_key": "",
}


@ai_renamer_bp.before_request
def require_login():
    try:
        from sdcodex.auth import current_user
    except Exception:
        # Standalone deployment: there is no core session to enforce.
        current_user = None

    if current_user is not None and current_user() is None:
        return jsonify({"ok": False, "error": "Sign in to use AI Renamer."}), 401
    if request.method == "POST" and not request.is_json:
        return jsonify({"ok": False, "error": "Expected a JSON request."}), 415
    return None


@ai_renamer_bp.errorhandler(Exception)
def handle_plugin_error(error):
    if isinstance(error, HTTPException):
        return jsonify({"ok": False, "error": error.description}), error.code
    current_app.logger.exception("AI Renamer request failed")
    return jsonify({"ok": False, "error": str(error) or "AI Renamer request failed."}), 500


def _settings_file():
    # This beta channel keeps its own settings folder: the stable AI Renamer
    # plugin must not read or overwrite them.
    folder = os.path.join(current_app.instance_path, "plugins", "betarenamer")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return os.path.join(folder, "settings.json")


def _read_settings():
    try:
        with open(_settings_file(), encoding="utf-8") as stream:
            saved = json.load(stream)
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict):
        raise ValueError("Saved AI Renamer settings must be a JSON object.")
    result = {**DEFAULT_SETTINGS, **saved}
    return _validate_settings(result)


def _validate_settings(value, preserve_secret=False):
    provider = str(value.get("provider", "ollama")).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError("Choose Ollama, LM Studio, or OpenAI.")

    base_url = str(value.get("base_url", "")).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Base URL must be a valid http(s) URL without embedded credentials.")

    model = str(value.get("model", "")).strip()
    if len(model) > 200:
        raise ValueError("Model name is too long.")

    case_style = str(value.get("case", "kebabCase"))
    if case_style not in CASE_STYLES:
        raise ValueError("Choose a supported filename case convention.")

    try:
        max_chars = int(value.get("max_chars", 35))
        frames = int(value.get("frames", 5))
    except (TypeError, ValueError) as exc:
        raise ValueError("Maximum characters and video frames must be whole numbers.") from exc
    if not 1 <= max_chars <= 180:
        raise ValueError("Maximum characters must be between 1 and 180.")
    if not 1 <= frames <= 20:
        raise ValueError("Video frame count must be between 1 and 20.")

    api_key = str(value.get("api_key", "")).strip()
    if preserve_secret and not api_key:
        api_key = _read_saved_secret()
    if len(api_key) > 4096:
        raise ValueError("API key is too long.")

    include_subdirectories = value.get("include_subdirectories", True)
    if not isinstance(include_subdirectories, bool):
        raise ValueError("Include Subdirectories must be true or false.")

    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "case": case_style,
        "max_chars": max_chars,
        "frames": frames,
        "language": (str(value.get("language", "English")).strip() or "English")[:80],
        "custom_prompt": str(value.get("custom_prompt", ""))[:2000],
        "include_subdirectories": include_subdirectories,
        "api_key": api_key,
    }


def _read_saved_secret():
    try:
        with open(_settings_file(), encoding="utf-8") as stream:
            saved = json.load(stream)
        return str(saved.get("api_key", ""))
    except FileNotFoundError:
        return ""


def _write_settings(settings):
    target = _settings_file()
    fd, temp_path = tempfile.mkstemp(prefix=".settings-", dir=os.path.dirname(target))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
            stream.write("\n")
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _browse_root():
    workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    configured = (
        os.environ.get("AI_RENAMER_ROOT")
        or os.environ.get("GALLERY_ROOT")
        or os.environ.get("GALLERY")
    )
    if configured:
        configured = os.path.expanduser(configured)
        root = os.path.abspath(configured if os.path.isabs(configured) else os.path.join(workspace, configured))
    else:
        candidates = ["/data/downloads", os.path.join(workspace, "downloads")]
        root = next((os.path.abspath(path) for path in candidates if os.path.isdir(path)), candidates[0])
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Configured media folder does not exist: {root}")
    return os.path.realpath(root)


def _resolve_path(relative_path="", must_exist=True):
    root = _browse_root()
    supplied = os.path.expanduser(str(relative_path or ""))
    if os.path.isabs(supplied):
        target = os.path.realpath(supplied)
        rel = os.path.relpath(target, root)
        if rel == ".":
            rel = ""
        rel = rel.replace(os.sep, "/")
    else:
        rel = supplied.replace("\\", "/").strip("/")
        if rel and any(part in {".", ".."} for part in rel.split("/")):
            raise ValueError("Choose a folder or file inside the configured media root.")
        target = os.path.realpath(os.path.join(root, rel))
    try:
        safe = os.path.commonpath((root, target)) == root
    except ValueError:
        safe = False
    if not safe:
        raise ValueError("The requested path is outside the configured media root.")
    if must_exist and not os.path.exists(target):
        raise FileNotFoundError("The requested file or folder no longer exists.")
    return root, target, rel


def _relative(root, target):
    return os.path.relpath(target, root).replace(os.sep, "/")


def _media_kind(path):
    extension = os.path.splitext(path)[1].lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None


def _video_tools():
    """Return ``(ffmpeg, ffprobe)`` paths, preferring host binaries.

    ``imageio-ffmpeg`` ships with the plugin requirements, so hosts and
    containers without a system ffmpeg still get a bundled binary. ``ffprobe``
    stays optional: video duration falls back to reading ffmpeg's own output.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # optional dependency or no packaged binary
            ffmpeg = None
    return ffmpeg, shutil.which("ffprobe")


def _video_duration(path, ffmpeg, ffprobe):
    """Length of ``path`` in seconds, measured with ffprobe or ffmpeg."""
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=30)
    match = DURATION_PATTERN.search(result.stderr or "")
    if match is None:
        raise RuntimeError("Could not determine video duration.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _file_info(root, path):
    stat = os.stat(path, follow_symlinks=False)
    return {
        "path": _relative(root, path),
        "name": os.path.basename(path),
        "kind": _media_kind(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _list_files(folder, recursive):
    """List supported media under ``folder`` up to the staging cap.

    Media roots in real installs hold hundreds of thousands of files, so a
    hard error here made "Scan Folder" a dead end and every later action
    unreachable. Stop at ``MAX_FILES_PER_JOB`` and report ``truncated``; the
    cap still applies to how many files a single dry-run may stage.
    """
    root, target, _ = _resolve_path(folder)
    if not os.path.isdir(target):
        raise ValueError("Choose a folder to scan.")

    files = []
    truncated = False

    def add(path):
        nonlocal truncated
        if len(files) >= MAX_FILES_PER_JOB:
            truncated = True
            return False
        files.append(_file_info(root, path))
        return True

    if recursive:
        for current, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs
                if not name.startswith(".") and not os.path.islink(os.path.join(current, name))
            )
            for name in sorted(names, key=str.casefold):
                if name.startswith("."):
                    continue
                path = os.path.join(current, name)
                if os.path.islink(path) or _media_kind(path) is None:
                    continue
                if not add(path):
                    return root, files, truncated
    else:
        with os.scandir(target) as entries:
            for entry in sorted(entries, key=lambda item: item.name.casefold()):
                if entry.is_file(follow_symlinks=False) and _media_kind(entry.name):
                    if not add(entry.path):
                        return root, files, truncated
    return root, files, truncated


def _model_list(settings):
    base = settings["base_url"]
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings["api_key"] else {}
    if settings["provider"] == "ollama":
        response = requests.get(f"{base}/api/tags", timeout=12)
        response.raise_for_status()
        items = response.json().get("models", [])
        models = [item.get("name", "") for item in items if item.get("name")]
        return models
    response = requests.get(f"{base}/v1/models", headers=headers, timeout=12)
    response.raise_for_status()
    items = response.json().get("data", [])
    return [item.get("id", "") for item in items if item.get("id")]


def _prompt(settings, relative_path, video_prompt=""):
    parts = []
    if video_prompt:
        parts.append(video_prompt)
    parts.extend([
        f"Generate a filename describing this media: {os.path.basename(relative_path)}",
        "",
        f"Use {settings['case']}.",
        f"Maximum {settings['max_chars']} characters, excluding the file extension.",
        f"Write in {settings['language']} only.",
        "Do not include a file extension or special characters.",
        "Describe only the key visible subject and action. Be concise; one phrase is best.",
        "Respond with only the filename.",
    ])
    if settings["custom_prompt"]:
        parts.extend(["", "Additional instructions:", settings["custom_prompt"]])
    return "\n".join(parts)


def _request_name(settings, prompt, image_paths):
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    if settings["provider"] == "ollama":
        images = []
        for path in image_paths:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                raise ValueError(f"Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
            with open(path, "rb") as image:
                images.append(base64.b64encode(image.read()).decode("ascii"))
        response = requests.post(
            f"{settings['base_url']}/api/generate",
            json={"model": settings["model"], "prompt": prompt, "images": images, "stream": False},
            headers=headers,
            timeout=180,
        )
        response.raise_for_status()
        text = response.json().get("response", "")
    else:
        content = [{"type": "text", "text": prompt}]
        for path in image_paths:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                raise ValueError(f"Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
            mime = mimetypes.guess_type(path)[0] or "image/jpeg"
            with open(path, "rb") as image:
                data = base64.b64encode(image.read()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        response = requests.post(
            f"{settings['base_url']}/v1/chat/completions",
            json={"model": settings["model"], "messages": [{"role": "user", "content": content}], "stream": False},
            headers=headers,
            timeout=180,
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
    return _format_name(text, settings["case"], settings["max_chars"])


def _format_name(text, case_style, max_chars):
    text = str(text or "").strip().splitlines()[0].strip(" `\"'")
    extension = os.path.splitext(text)[1].lower()
    if extension in MEDIA_EXTENSIONS:
        text = text[:-len(extension)].strip()
    words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    if not words:
        raise ValueError("The model did not return a usable filename.")
    if case_style == "camelCase":
        name = words[0].lower() + "".join(word.capitalize() for word in words[1:])
    elif case_style == "snakeCase":
        name = "_".join(word.lower() for word in words)
    elif case_style == "pascalCase":
        name = "".join(word.capitalize() for word in words)
    elif case_style == "noCase":
        name = " ".join(word.lower() for word in words)
    elif case_style == "sentenceCase":
        name = " ".join(word.lower() for word in words)
        name = name[:1].upper() + name[1:]
    else:
        name = "-".join(word.lower() for word in words)
    name = name[:max_chars].rstrip(" ._-")
    if not name:
        raise ValueError("The model response produced an empty filename.")
    return name


def _analyze_one(item, settings):
    _, path, rel = _resolve_path(item["path"])
    if not os.path.isfile(path) or _media_kind(path) is None or os.path.islink(path):
        raise ValueError("The staged file is no longer a supported regular media file.")
    stat = os.stat(path, follow_symlinks=False)
    if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
        raise ValueError("File changed since it was staged. Scan and stage it again.")
    kind = _media_kind(path)
    temp_dir = None
    try:
        if kind == "image":
            image_paths = [path]
            video_prompt = ""
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix="betarenamer-")
            ffmpeg, ffprobe = _video_tools()
            if not ffmpeg:
                raise RuntimeError(
                    "Video renaming needs ffmpeg. Reinstall the plugin requirements (imageio-ffmpeg) "
                    "or install ffmpeg on the SDCodex host."
                )
            duration = _video_duration(path, ffmpeg, ffprobe)
            if duration <= 0:
                raise RuntimeError("Could not determine video duration.")
            frame_count = max(1, min(settings["frames"], int(duration) or 1))
            rate = frame_count / duration
            pattern = os.path.join(temp_dir.name, "frame_%03d.jpg")
            subprocess.run(
                [ffmpeg, "-v", "error", "-i", path, "-vf", f"fps={rate:.8f}", "-frames:v", str(frame_count), "-q:v", "2", pattern],
                check=True,
                capture_output=True,
                timeout=90,
            )
            image_paths = [os.path.join(temp_dir.name, name) for name in sorted(os.listdir(temp_dir.name))]
            if not image_paths:
                raise RuntimeError("ffmpeg did not extract any video frames.")
            interval = duration / len(image_paths)
            video_prompt = f"Analyze these {len(image_paths)} frames from a {duration:.1f}-second video, sampled about every {interval:.1f} seconds."
        suggested = _request_name(settings, _prompt(settings, rel, video_prompt), image_paths)
        return {**item, "status": "ready", "suggested_name": suggested, "error": ""}
    finally:
        if temp_dir:
            temp_dir.cleanup()


def _job_worker(job_id, items, settings):
    for index, item in enumerate(items):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["current"] = item["name"]
            job["updated_at"] = time.time()
        try:
            result = _analyze_one(item, settings)
        except Exception as error:
            result = {**item, "status": "error", "suggested_name": "", "error": str(error)}
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["items"][index] = result
            job["processed"] = index + 1
            job["current"] = ""
            job["updated_at"] = time.time()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "complete"
        job["current"] = ""
        job["finished_at"] = time.time()
        job["updated_at"] = time.time()


def _new_name(path, stem):
    folder = os.path.dirname(path)
    extension = os.path.splitext(path)[1]
    base = os.path.join(folder, stem + extension)
    candidate = base
    counter = 1
    while os.path.lexists(candidate) and os.path.realpath(candidate) != os.path.realpath(path):
        candidate = os.path.join(folder, f"{stem}{counter}{extension}")
        counter += 1
        if counter > 10000:
            raise FileExistsError("Could not find a free filename after 10,000 attempts.")
    return candidate


@ai_renamer_bp.get("/status")
def status():
    ffmpeg, _ = _video_tools()
    settings = _read_settings()
    provider = settings.get("provider", "Ollama")
    return jsonify({"ok": True, "engine": provider, "video_tools": bool(ffmpeg)})


@ai_renamer_bp.get("/settings")
def get_settings():
    settings = _read_settings()
    return jsonify({**settings, "api_key": "", "api_key_set": bool(settings["api_key"])})


@ai_renamer_bp.post("/settings")
def save_settings():
    submitted = request.get_json()
    if not isinstance(submitted, dict):
        return jsonify({"ok": False, "error": "Settings must be a JSON object."}), 400
    secret = "" if submitted.get("clear_api_key") else str(submitted.get("api_key", "")).strip()
    candidate = {**_read_settings(), **submitted}
    candidate["api_key"] = secret
    try:
        settings = _validate_settings(candidate, preserve_secret=not submitted.get("clear_api_key"))
        _write_settings(settings)
    except (ValueError, OSError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    return jsonify({**settings, "api_key": "", "api_key_set": bool(settings["api_key"])})


@ai_renamer_bp.post("/models")
def models():
    try:
        saved = _read_settings()
        submitted = request.get_json()
        if not isinstance(submitted, dict):
            return jsonify({"ok": False, "error": "Model settings must be a JSON object."}), 400
        supplied = {**saved, **submitted}
        if not submitted.get("api_key"):
            supplied["api_key"] = saved["api_key"]
        settings = _validate_settings(supplied, preserve_secret=True)
        return jsonify({"ok": True, "models": _model_list(settings)})
    except (ValueError, requests.RequestException, KeyError) as error:
        detail = getattr(getattr(error, "response", None), "text", "") or str(error)
        return jsonify({"ok": False, "error": detail[:500]}), 502


@ai_renamer_bp.get("/folders")
def folders():
    try:
        root, target, rel = _resolve_path(request.args.get("path", ""))
        if not os.path.isdir(target):
            return jsonify({"ok": False, "error": "Choose a folder."}), 400
        dirs = []
        with os.scandir(target) as entries:
            for entry in sorted(entries, key=lambda item: item.name.casefold()):
                if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                    continue
                dirs.append({"name": entry.name, "path": "/".join(part for part in (rel, entry.name) if part)})
        return jsonify({"ok": True, "root": root, "path": rel, "folders": dirs[:500]})
    except (ValueError, OSError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@ai_renamer_bp.get("/files")
def files():
    try:
        root, items, truncated = _list_files(request.args.get("path", ""), request.args.get("recursive", "true").lower() in {"1", "true", "yes"})
        return jsonify({"ok": True, "root": root, "files": items, "limit": MAX_FILES_PER_JOB, "truncated": truncated})
    except (ValueError, FileNotFoundError, OSError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@ai_renamer_bp.get("/preview")
def preview():
    try:
        _, path, _ = _resolve_path(request.args.get("path", ""))
        kind = _media_kind(path)
        if kind == "image":
            return send_file(path)
        if kind != "video":
            return jsonify({"ok": False, "error": "Unsupported media file."}), 415
        ffmpeg, _ = _video_tools()
        if not ffmpeg:
            return jsonify({"ok": False, "error": "ffmpeg is not installed."}), 501
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", "00:00:01", "-i", path, "-frames:v", "1", "-vf", "scale=640:-1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        from io import BytesIO
        return send_file(BytesIO(result.stdout), mimetype="image/jpeg")
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": "Could not decode a preview frame."}), 422
    except (ValueError, FileNotFoundError, OSError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@ai_renamer_bp.post("/dry-run")
def dry_run():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return jsonify({"ok": False, "error": "Choose one or more files to analyze."}), 400
    if not isinstance(data.get("settings", {}), dict):
        return jsonify({"ok": False, "error": "Analysis settings must be a JSON object."}), 400
    if not data["files"] or len(data["files"]) > MAX_FILES_PER_JOB:
        return jsonify({"ok": False, "error": f"Choose between 1 and {MAX_FILES_PER_JOB} files."}), 400
    try:
        settings = _validate_settings({**_read_settings(), **data.get("settings", {})}, preserve_secret=True)
        if not settings["model"]:
            return jsonify({"ok": False, "error": "Choose or load an AI vision model first."}), 400
        root = _browse_root()
        items = []
        seen = set()
        for supplied in data["files"]:
            path_value = supplied.get("path") if isinstance(supplied, dict) else supplied
            _, path, rel = _resolve_path(path_value)
            if rel in seen:
                continue
            seen.add(rel)
            if not os.path.isfile(path) or os.path.islink(path) or _media_kind(path) is None:
                raise ValueError(f"Not a supported regular media file: {rel}")
            stat = os.stat(path, follow_symlinks=False)
            items.append({
                "path": rel,
                "name": os.path.basename(path),
                "kind": _media_kind(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "status": "pending",
                "suggested_name": "",
                "error": "",
            })
        if not items:
            raise ValueError("No supported media files were selected.")
        job_id = uuid.uuid4().hex
        with _jobs_lock:
            cutoff = time.time() - JOB_RETENTION_SECONDS
            for stale_id in [
                key for key, job in _jobs.items()
                if job["status"] == "complete" and job["updated_at"] < cutoff
            ]:
                _jobs.pop(stale_id, None)
            if len(_jobs) >= MAX_ACTIVE_JOBS:
                completed = sorted(
                    (job for job in _jobs.values() if job["status"] == "complete"),
                    key=lambda job: job["updated_at"],
                )
                while len(_jobs) >= MAX_ACTIVE_JOBS and completed:
                    _jobs.pop(completed.pop(0)["id"], None)
            if len(_jobs) >= MAX_ACTIVE_JOBS:
                return jsonify({"ok": False, "error": "Too many active previews. Wait for a current analysis to finish."}), 429
            _jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "processed": 0,
                "total": len(items),
                "current": "",
                "folder": str(data.get("folder") or "")[:1024],
                "items": items,
                "started_at": time.time(),
                "updated_at": time.time(),
                "_case": settings["case"],
                "_max_chars": settings["max_chars"],
            }
        worker = threading.Thread(target=_job_worker, args=(job_id, items, settings), daemon=True)
        worker.start()
        return jsonify({"ok": True, "job_id": job_id, "total": len(items)}), 202
    except (ValueError, OSError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@ai_renamer_bp.get("/jobs/<job_id>")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Rename preview expired. Run the dry-run again."}), 404
        return jsonify({
            "ok": True,
            "id": job["id"],
            "status": job["status"],
            "processed": job["processed"],
            "total": job["total"],
            "folder": job.get("folder", ""),
            "current": job.get("current", ""),
            "items": job["items"],
            "updated_at": job["updated_at"],
        })


@ai_renamer_bp.get("/jobs/active")
def active_job():
    """Newest dry-run job so the page can pick a run back up after browsing
    away (the analysis itself keeps running in its worker thread)."""
    with _jobs_lock:
        job = _latest_job_locked()
        if not job:
            return jsonify({"ok": True, "job": None})
        return jsonify({
            "ok": True,
            "job": {
                "id": job["id"],
                "status": job["status"],
                "processed": job["processed"],
                "total": job["total"],
                "folder": job.get("folder", ""),
                "current": job.get("current", ""),
                "started_at": job.get("started_at", 0),
                "updated_at": job.get("updated_at", 0),
            },
        })


@ai_renamer_bp.get("/task")
def task_progress():
    """Normalized progress for the core's floating task notification."""
    with _jobs_lock:
        job = _latest_job_locked()
        if not job or job.get("status") != "running":
            return jsonify({"active": False})
        return jsonify({
            "active": True,
            "state": "running",
            "label": "AI Renamer",
            "current": job["processed"],
            "total": job["total"],
            "detail": job.get("current", ""),
            "message": f"Analyzing {job['processed']} / {job['total']}",
            "started_at": job.get("started_at", 0),
        })


@ai_renamer_bp.post("/jobs/<job_id>/rename")
def apply_renames(job_id):
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        return jsonify({"ok": False, "error": "Choose the staged file paths to rename."}), 400
    selected = data["paths"]
    if any(not isinstance(path, str) for path in selected):
        return jsonify({"ok": False, "error": "Rename paths must be strings."}), 400
    names = data.get("names", {})
    if not isinstance(names, dict):
        return jsonify({"ok": False, "error": "Rename suggestions must be a JSON object."}), 400
    global _last_undo
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Rename preview expired. Run the dry-run again."}), 404
        if job["status"] != "complete":
            return jsonify({"ok": False, "error": "Wait for the dry-run to finish before renaming."}), 409
        selected_set = set(selected)
        ready = []
        for item in job["items"]:
            if item["status"] != "ready" or item["path"] not in selected_set:
                continue
            candidate = dict(item)
            if candidate["path"] in names:
                candidate["suggested_name"] = _format_name(
                    names[candidate["path"]],
                    job["_case"],
                    job["_max_chars"],
                )
            ready.append(candidate)
    if not ready:
        return jsonify({"ok": False, "error": "There are no successful staged suggestions to rename."}), 400

    root, _, _ = _resolve_path("")
    moves = []
    results = []
    try:
        for item in ready:
            _, source, _ = _resolve_path(item["path"])
            current_stat = os.stat(source, follow_symlinks=False)
            if current_stat.st_size != item["size"] or current_stat.st_mtime_ns != item["mtime_ns"]:
                raise ValueError(f"{item['name']} changed after analysis. Run the dry-run again.")
            destination = _new_name(source, item["suggested_name"])
            if os.path.normcase(destination) == os.path.normcase(source):
                continue
            os.rename(source, destination)
            moved = {"from": _relative(root, source), "to": _relative(root, destination)}
            moves.append(moved)
            results.append({
                "old_path": moved["from"],
                "new_path": moved["to"],
                "old_name": os.path.basename(source),
                "new_name": os.path.basename(destination),
            })
    except Exception as error:
        rollback_errors = []
        for move in reversed(moves):
            try:
                _, current, _ = _resolve_path(move["to"])
                _, original, _ = _resolve_path(move["from"], must_exist=False)
                if os.path.lexists(original):
                    raise FileExistsError(f"Cannot restore {move['from']}: a file now occupies that name.")
                os.rename(current, original)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = str(error)
        if rollback_errors:
            detail += " Rollback incomplete: " + "; ".join(rollback_errors)
        return jsonify({"ok": False, "error": detail}), 409

    if not moves:
        return jsonify({"ok": False, "error": "The selected suggestions do not change any filenames."}), 400
    token = uuid.uuid4().hex
    with _undo_lock:
        _last_undo = {"token": token, "moves": moves}
    with _jobs_lock:
        for item in job["items"]:
            for result in results:
                if item["path"] == result["old_path"]:
                    item.update({"path": result["new_path"], "name": result["new_name"], "status": "renamed"})
                    break
    return jsonify({"ok": True, "undo_token": token, "renamed": results})


@ai_renamer_bp.post("/undo")
def undo_renames():
    data = request.get_json()
    token = data.get("undo_token") if isinstance(data, dict) else None
    global _last_undo
    with _undo_lock:
        batch = _last_undo
        if not batch or batch["token"] != token:
            return jsonify({"ok": False, "error": "No matching rename batch is available to undo."}), 404
        moves = list(batch["moves"])
    root, _, _ = _resolve_path("")
    reverted = []
    try:
        for move in reversed(moves):
            _, current, _ = _resolve_path(move["to"])
            _, original, _ = _resolve_path(move["from"], must_exist=False)
            if os.path.lexists(original):
                raise FileExistsError(f"Cannot restore {move['from']}: a file now occupies that name.")
            os.rename(current, original)
            reverted.append(move)
    except Exception as error:
        rollback_errors = []
        for move in reversed(reverted):
            try:
                _, original, _ = _resolve_path(move["from"])
                _, current, _ = _resolve_path(move["to"], must_exist=False)
                if os.path.lexists(current):
                    raise FileExistsError(f"Cannot restore {move['to']}: a file now occupies that name.")
                os.rename(original, current)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = str(error)
        if rollback_errors:
            detail += " Rollback incomplete: " + "; ".join(rollback_errors)
        return jsonify({"ok": False, "error": detail}), 409
    with _undo_lock:
        if _last_undo and _last_undo["token"] == token:
            _last_undo = None
    return jsonify({"ok": True, "reverted": [
        {"from": move["to"], "to": move["from"]} for move in reversed(reverted)
    ]})
