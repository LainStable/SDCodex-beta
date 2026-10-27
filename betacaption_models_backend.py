"""SDCodex ComfyCaption — caption_models_backend.py

HuggingFace model downloader for captioning GGUF models.
Routes under /betacaption/models/:
  GET  /betacaption/models/status    — download job status
  POST /betacaption/models/download  — start background download
  POST /betacaption/models/stop      — cancel download
  POST /betacaption/models/save      — save HF token / repo settings
  GET  /betacaption/models/list      — list files present on disk
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------

def _models_dir():
    """Return the models directory, creating it if writable."""
    for cand in (
        os.environ.get("CAPTION_MODELS"),
        os.environ.get("LMSTUDIO_MODELS"),
        os.path.join(os.environ.get("DATA_DIR", "/data"), "caption_models"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "caption_models"),
    ):
        if cand:
            try:
                os.makedirs(cand, exist_ok=True)
                return cand
            except Exception:
                continue
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "caption_models")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _settings_file():
    return os.path.join(_models_dir(), "models_settings.json")


DEFAULT_REPO = "nakedlittlezombie/Qwen3-VL-8B-Thinking-heretic"
HF_ENDPOINT  = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
CHUNK        = 1024 * 1024  # 1 MiB

MODEL_OPTIONS = [
    {
        "key":                "q4_k_m",
        "label":              "Q4_K_M",
        "size":               "~5.0 GB",
        "gguf":               "Qwen3-VL-8B-Thinking-heretic-Q4_K_M.gguf",
        "mmproj":             "mmproj-Qwen3-VL-8b-Thinking-heretic-Q4_K_M.gguf",
        "gguf_size_approx":   5.1e9,
        "mmproj_size_approx": 0.6e9,
    },
    {
        "key":                "q8_0",
        "label":              "Q8_0",
        "size":               "~8.7 GB",
        "gguf":               "Qwen3-VL-8B-Thinking-heretic-Q8_0.gguf",
        "mmproj":             "mmproj-Qwen3-VL-8b-Thinking-heretic-Q8_0.gguf",
        "gguf_size_approx":   8.7e9,
        "mmproj_size_approx": 0.75e9,
    },
    {
        "key":                "f16",
        "label":              "F16",
        "size":               "~16.4 GB",
        "gguf":               "Qwen3-VL-8B-Thinking-heretic-F16.gguf",
        "mmproj":             "mmproj-Qwen3-VL-8b-Thinking-heretic-F16.gguf",
        "gguf_size_approx":   16.4e9,
        "mmproj_size_approx": 1.16e9,
    },
    {
        "key":                "bf16",
        "label":              "BF16",
        "size":               "~16.4 GB",
        "gguf":               "Qwen3-VL-8B-Thinking-heretic-BF16.gguf",
        "mmproj":             "mmproj-Qwen3-VL-8b-Thinking-heretic-BF16.gguf",
        "gguf_size_approx":   16.4e9,
        "mmproj_size_approx": 1.16e9,
    },
]

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings():
    try:
        with open(_settings_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data.setdefault("hf_token", "")
    data.setdefault("repo", DEFAULT_REPO)
    return data


def save_settings(**updates):
    data = load_settings()
    data.update(updates)
    os.makedirs(os.path.dirname(_settings_file()), exist_ok=True)
    with open(_settings_file(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


# ---------------------------------------------------------------------------
# Download job
# ---------------------------------------------------------------------------

class _DownloadJob:
    def __init__(self):
        self.lock       = threading.Lock()
        self.generation = 0
        self.cancel     = False
        self.file       = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self.speed      = 0.0
        self.state      = "idle"   # idle | downloading | done | error | cancelled | cancelling
        self.message    = ""


_job        = _DownloadJob()
_job_thread = None


def _download_one(dest, url, token, approx_size):
    """Stream-download a single file, resuming from any partial."""
    partial = dest + ".incomplete"
    start = os.path.getsize(partial) if os.path.exists(partial) else 0

    with _job.lock:
        gen = _job.generation
        _job.file        = os.path.basename(dest)
        _job.cancel      = False
        _job.state       = "downloading"
        _job.done_bytes  = start
        _job.total_bytes = approx_size or 0
        _job.speed       = 0.0
        _job.message     = f"Downloading {os.path.basename(dest)}"

    prev       = time.time()
    prev_bytes = start

    headers = {"Range": f"bytes={start}-"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = approx_size
            if resp.status == 200 and start:
                start = 0
                with _job.lock:
                    _job.done_bytes = 0
                if os.path.exists(partial):
                    os.remove(partial)
            clen  = resp.headers.get("Content-Length")
            crng  = resp.headers.get("Content-Range")
            if crng and "/" in crng:
                try:
                    total = int(crng.rsplit("/", 1)[1])
                except Exception:
                    pass
            elif clen:
                try:
                    total = start + int(clen)
                except Exception:
                    pass
            with _job.lock:
                _job.total_bytes = total

            mode = "ab" if start else "wb"
            with open(partial, mode) as fh:
                while True:
                    if _job.cancel or _job.generation != gen:
                        with _job.lock:
                            _job.state   = "cancelled"
                            _job.message = "Cancelled"
                        return False
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    now = time.time()
                    with _job.lock:
                        _job.done_bytes += len(chunk)
                        dt = now - prev
                        if dt >= 1.0:
                            _job.speed = _job.done_bytes / dt if dt else 0
                            prev = now

        os.replace(partial, dest)
        return True

    except urllib.error.HTTPError as e:
        msg = {
            401: "Auth rejected — check HuggingFace token / repo access.",
            403: "Auth rejected — check HuggingFace token / repo access.",
            404: f"File not found in repo (HTTP 404): {os.path.basename(dest)}",
        }.get(e.code, f"HTTP {e.code} downloading {os.path.basename(dest)}")
        with _job.lock:
            _job.state   = "error"
            _job.message = msg
        return False
    except Exception as e:
        with _job.lock:
            _job.state   = "error"
            _job.message = f"{type(e).__name__}: {e}"
        return False


def _run_download(repo, token, option):
    with _job.lock:
        gen = _job.generation

    dest_dir = _models_dir()
    files = [
        (option["gguf"],   option.get("gguf_size_approx",   0)),
        (option["mmproj"], option.get("mmproj_size_approx", 0)),
    ]

    # Try to get exact sizes from HF metadata.
    sizes = {}
    try:
        meta_url = f"{HF_ENDPOINT}/api/models/{repo}?blobs=true"
        req = urllib.request.Request(meta_url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        for sib in meta.get("siblings", []) or []:
            sizes[sib.get("rfilename", "")] = int(sib.get("size") or 0) or None
    except Exception:
        pass

    ok = True
    for name, approx in files:
        if _job.cancel or _job.generation != gen:
            break
        dest = os.path.join(dest_dir, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0 and not os.path.exists(dest + ".incomplete"):
            with _job.lock:
                _job.message = f"Skipping {name} (already present)"
            continue
        url  = f"{HF_ENDPOINT}/{repo}/resolve/main/{name}"
        size = sizes.get(name) or approx
        if not _download_one(dest, url, token, size):
            ok = False
            break

    with _job.lock:
        if _job.generation == gen:
            if _job.cancel:
                _job.state   = "cancelled"
                _job.message = "Cancelled"
            else:
                _job.state   = "done" if ok else "error"
                _job.message = "All files downloaded" if ok else (_job.message or "Download failed")


def _job_status():
    with _job.lock:
        return {
            "state":       _job.state,
            "message":     _job.message,
            "file":        _job.file,
            "done_bytes":  _job.done_bytes,
            "total_bytes": _job.total_bytes,
            "speed":       _job.speed,
        }


def start_download(repo, token, option_key):
    global _job_thread
    option = next((o for o in MODEL_OPTIONS if o["key"] == option_key), None)
    if not option:
        return False, "Unknown option key"
    repo = (repo or DEFAULT_REPO).strip() or DEFAULT_REPO
    with _job.lock:
        _job.cancel      = True
        _job.generation += 1
        _job.state       = "idle"
    _job_thread = threading.Thread(target=_run_download, args=(repo, token, option), daemon=True)
    _job_thread.start()
    return True, ""


def stop_download():
    with _job.lock:
        _job.cancel  = True
        _job.state   = "cancelling"


def _list_models():
    d = _models_dir()
    present = {}
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and not name.endswith((".incomplete", ".json", ".partial")):
                present[name] = os.path.getsize(p)
    return present


# ---------------------------------------------------------------------------
# Blueprint & routes
# ---------------------------------------------------------------------------

models_bp = Blueprint("beta_caption_models_bp", __name__, url_prefix="/betacaption/models")


def _core_session_missing() -> bool:
    try:
        from sdcodex import auth as core_auth
    except Exception:
        return False  # standalone deployment
    try:
        return core_auth.current_user() is None
    except Exception:
        return True


@models_bp.before_request
def _models_auth_guard():
    if _core_session_missing():
        return jsonify({"error": "unauthorized"}), 401
    return None


@models_bp.get("/status")
def status_route():
    return jsonify(_job_status())


@models_bp.get("/list")
def list_models():
    d = _models_dir()
    opts = []
    for o in MODEL_OPTIONS:
        gguf_done = (
            os.path.isfile(os.path.join(d, o["gguf"])) and
            not os.path.exists(os.path.join(d, o["gguf"] + ".incomplete"))
        )
        mm_done = (
            os.path.isfile(os.path.join(d, o["mmproj"])) and
            not os.path.exists(os.path.join(d, o["mmproj"] + ".incomplete"))
        )
        opts.append({**o, "gguf_done": gguf_done, "mmproj_done": mm_done})
    return jsonify({
        "models_dir": d,
        "options":    opts,
        "present":    _list_models(),
        "settings":   {"repo": load_settings().get("repo", DEFAULT_REPO)},
    })


@models_bp.get("/token-status")
def token_status():
    return jsonify(configured=bool(load_settings().get("hf_token")))


@models_bp.post("/token")
def save_token():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    save_settings(hf_token=token)
    return jsonify(ok=True, configured=bool(token))


@models_bp.post("/save")
def save():
    data = request.get_json(silent=True) or {}
    updates = {}
    if "hf_token" in data:
        updates["hf_token"] = (data.get("hf_token") or "").strip()
    if "repo" in data:
        updates["repo"] = (data.get("repo") or "").strip() or DEFAULT_REPO
    return jsonify(ok=True, settings=save_settings(**updates))


@models_bp.post("/download")
def download():
    data      = request.get_json(silent=True) or {}
    option    = data.get("option", "")
    repo      = (data.get("repo") or "").strip()
    saved     = load_settings()
    if repo and repo != saved.get("repo"):
        save_settings(repo=repo)
    token = saved.get("hf_token", "")
    ok, msg = start_download(repo or saved.get("repo", DEFAULT_REPO), token, option)
    return jsonify(ok=ok, message=msg)


@models_bp.post("/stop")
def stop():
    stop_download()
    return jsonify(ok=True)
