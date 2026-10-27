"""SDCodex ComfyCaption — caption_backend.py

All API routes for the ComfyCaption plugin:
  - /betacaption/api/check-connection   POST  — probe LM Studio / vision model server
  - /betacaption/api/images             GET   — list images in a folder with captions
  - /betacaption/api/caption-single     POST  — caption one image (writes .txt sidecar)
  - /betacaption/api/caption            POST  — batch caption via SSE stream
  - /betacaption/api/workflow           GET   — download embedded ComfyUI workflow JSON
  - /betacaption/api/browse-folders     GET   — web-based directory browser
  - /betacaption/api/browse-roots       GET   — quick-pick roots for the folder browser
  - /betacaption/models/*               delegated to caption_models_backend blueprint
"""

import base64
import json
import mimetypes
import os
import re
import socket
import struct
import threading
import time
from io import BytesIO
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request, send_file

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    from openai import OpenAI
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False

try:
    from sd_prompt_reader.image_data_reader import ImageDataReader
except ImportError:
    ImageDataReader = None

try:
    import piexif, piexif.helper
    _PIEXIF_OK = True
except ImportError:
    _PIEXIF_OK = False
    piexif = None

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

caption_bp = Blueprint("beta_caption_bp", __name__, url_prefix="/betacaption/api")


def _core_session_missing() -> bool:
    try:
        from sdcodex import auth as core_auth
    except Exception:
        return False  # standalone deployment
    try:
        return core_auth.current_user() is None
    except Exception:
        return True


@caption_bp.before_request
def _caption_auth_guard():
    if request.path.endswith("/status"):
        return None
    if _core_session_missing():
        return jsonify({"error": "unauthorized"}), 401
    return None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_batch_lock = threading.RLock()
_batch = {
    "job_id": 0, "state": "idle", "folder_path": "", "current": 0,
    "total": 0, "file": "", "event_id": 0, "events": [], "cancel": False,
    "pause": False,
}


def _batch_emit(job_id, event):
    with _batch_lock:
        if job_id != _batch["job_id"]:
            return
        _batch["event_id"] += 1
        _batch["events"].append({**event, "eventId": _batch["event_id"]})
        _batch["events"] = _batch["events"][-300:]
        for key in ("current", "total", "file"):
            if key in event:
                _batch[key] = event[key]
        if event.get("status") in ("complete", "cancelled"):
            _batch["state"] = event["status"]
        elif event.get("status") == "error":
            _batch["last_error"] = event.get("error", "")


def _run_caption_batch(job_id, image_files, folder_path, prompt, url, model, mode, trigger_tag):
    total = len(image_files)
    try:
        client = OpenAI(api_key="not-needed", base_url=url)
        for idx, fname in enumerate(image_files, start=1):
            with _batch_lock:
                if job_id != _batch["job_id"] or _batch["cancel"]:
                    break
            # A paused batch waits here until it is resumed or stopped.
            while True:
                with _batch_lock:
                    if job_id != _batch["job_id"] or _batch["cancel"]:
                        break
                    if not _batch["pause"]:
                        break
                time.sleep(0.25)
            img_path = os.path.join(folder_path, fname)
            txt_path = os.path.join(folder_path, os.path.splitext(fname)[0] + ".txt")

            if mode == "missing" and os.path.isfile(txt_path):
                try:
                    with open(txt_path, "r", encoding="utf-8") as fh:
                        existing = fh.read()
                    sd = _read_sd_info(img_path)
                    _batch_emit(job_id, {
                        "status": "success", "current": idx, "total": total,
                        "file": fname, "caption": existing,
                        "sdPrompt": sd["sdPrompt"], "sdNegative": sd["sdNegative"],
                        "sdSetting": sd["sdSetting"], "skipped": True,
                        "hasWorkflow": _extract_comfy_workflow(img_path) is not None,
                    })
                except Exception as e:
                    _batch_emit(job_id, {"status": "error", "current": idx, "total": total, "file": fname, "error": str(e)})
                continue

            _batch_emit(job_id, {"status": "started", "current": idx, "total": total, "file": fname})
            try:
                llm_b64 = _process_image_for_llm(img_path)
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": llm_b64}},
                        ],
                    }],
                )
                raw = resp.choices[0].message.content
                caption = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                if trigger_tag:
                    caption = f"{trigger_tag}, {caption}"
                caption, sd = _build_caption_with_sd(caption, img_path)
                with open(txt_path, "w", encoding="utf-8") as fh:
                    fh.write(caption)
                _batch_emit(job_id, {
                    "status": "success", "current": idx, "total": total,
                    "file": fname, "caption": caption,
                    "sdPrompt": sd["sdPrompt"], "sdNegative": sd["sdNegative"],
                    "sdSetting": sd["sdSetting"],
                    "hasWorkflow": _extract_comfy_workflow(img_path) is not None,
                })
            except Exception as e:
                _batch_emit(job_id, {"status": "error", "current": idx, "total": total, "file": fname, "error": str(e)})

        with _batch_lock:
            cancelled = job_id == _batch["job_id"] and _batch["cancel"]
            current = _batch["current"]
        _batch_emit(job_id, {"status": "cancelled" if cancelled else "complete", "current": current, "total": total})
    except Exception as e:
        _batch_emit(job_id, {"status": "error", "file": "", "error": str(e)})
        _batch_emit(job_id, {"status": "complete", "current": _batch["current"], "total": total})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_docker():
    return os.path.exists("/.dockerenv") or os.environ.get("REMBG_OUTPUT", "").startswith("/data")


def _docker_default_gateway():
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
                    return socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
    except Exception:
        pass
    return "172.17.0.1"


def _reachable_url(url):
    """Rewrite localhost/127.0.0.1 to the Docker gateway IP when inside a container."""
    if not url:
        return "http://localhost:1234/v1"
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        if not parts.hostname or parts.hostname.lower() not in ("localhost", "127.0.0.1"):
            return url
        if not _in_docker():
            return url
        gw = _docker_default_gateway()
        netloc = parts.netloc.replace(parts.hostname, gw, 1)
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


def _is_safe_path(base, target):
    resolved_base = os.path.realpath(base)
    resolved_target = os.path.realpath(target)
    try:
        return os.path.commonpath((resolved_base, resolved_target)) == resolved_base
    except ValueError:
        return False


def _process_image_for_llm(image_path):
    """Resize to max 1024 px, convert to JPEG, return data-URI."""
    if not _PIL_OK:
        raise RuntimeError("Pillow not installed")
    with Image.open(image_path) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _extract_comfy_workflow(image_path):
    """Extract ComfyUI workflow JSON from PNG/WebP/JPEG metadata."""
    if not _PIL_OK:
        return None

    def _valid(val):
        if not isinstance(val, str):
            val = str(val)
        val = val.strip()
        if val.startswith(("{", "[")):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, (dict, list)):
                    return val
            except Exception:
                pass
        return None

    try:
        with Image.open(image_path) as img:
            img.load()
            for key in ("workflow", "prompt"):
                if key in img.info:
                    v = _valid(img.info[key])
                    if v:
                        return v
            if "exif" in img.info and piexif:
                try:
                    ed = piexif.load(img.info["exif"])
                    if "Exif" in ed and piexif.ExifIFD.UserComment in ed["Exif"]:
                        uc = ed["Exif"][piexif.ExifIFD.UserComment]
                        try:
                            s = piexif.helper.UserComment.load(uc)
                        except Exception:
                            s = uc.decode("utf-8", errors="ignore") if isinstance(uc, bytes) else str(uc)
                            if s.startswith(("ASCII\0\0\0", "UNICODE\0")):
                                s = s[8:]
                        v = _valid(s)
                        if v:
                            return v
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _read_sd_info(image_path):
    """Read SD prompt data; returns dict with sdPrompt/sdNegative/sdSetting."""
    if ImageDataReader:
        try:
            r = ImageDataReader(image_path)
            return {
                "sdPrompt":   str(r.positive).strip() if getattr(r, "positive", None) else "",
                "sdNegative": str(r.negative).strip() if getattr(r, "negative",  None) else "",
                "sdSetting":  str(r.setting).strip()  if getattr(r, "setting",   None) else "",
            }
        except Exception:
            pass
    return {"sdPrompt": "", "sdNegative": "", "sdSetting": ""}


def _build_caption_with_sd(caption, image_path):
    """Optionally append SD prompt data behind the LLM caption (legacy behaviour)."""
    sd = _read_sd_info(image_path)
    extra = ""
    if sd["sdPrompt"] or sd["sdNegative"] or sd["sdSetting"]:
        extra += "\n\n"
        if sd["sdPrompt"]:
            extra += sd["sdPrompt"] + "\n"
        if sd["sdNegative"]:
            extra += f"Negative prompt: {sd['sdNegative']}\n"
        if sd["sdSetting"]:
            extra += sd["sdSetting"]
    return caption + extra, sd


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@caption_bp.post("/check-connection")
def check_connection():
    """Probe the LM Studio / vision-model server."""
    data = request.get_json(silent=True) or {}
    url = _reachable_url(data.get("lmStudioUrl", "http://localhost:1234/v1"))
    if not _OPENAI_OK:
        return jsonify({"connected": False, "error": "openai package not installed"}), 500
    try:
        client = OpenAI(api_key="not-needed", base_url=url, timeout=5.0)
        models = client.models.list()
        model_list = [{"id": m.id} for m in models.data]
        return jsonify({"connected": True, "models": model_list})
    except Exception as e:
        import requests as _req
        try:
            r = _req.get(url, timeout=5.0)
            return jsonify({"connected": True, "warning": "Server responded but models.list() failed"})
        except Exception as fe:
            return jsonify({"connected": False, "error": str(fe)})


@caption_bp.get("/file")
def get_file():
    """Stream one image straight from disk (the Gallery plugin's approach).

    The grid used to receive every image as a base64 data URL inside the
    /images JSON: 40 files per page meant tens of megabytes per request and
    pinned server threads. Browsers cache and lazily load plain URLs instead.
    """
    folder_path = (request.args.get("folderPath") or "").strip("\"'")
    name = os.path.basename((request.args.get("file") or "").strip("\"'"))
    if not folder_path or not name or os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:
        return jsonify({"error": "folderPath and a valid image file are required"}), 400
    if not os.path.isdir(folder_path):
        return jsonify({"error": f"Folder does not exist: {folder_path}"}), 400
    image_path = os.path.join(folder_path, name)
    if not _is_safe_path(folder_path, image_path) or not os.path.isfile(image_path):
        return jsonify({"error": "Image file not found"}), 404
    mime, _ = mimetypes.guess_type(image_path)
    response = send_file(image_path, mimetype=mime or "application/octet-stream", conditional=True)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@caption_bp.get("/images")
def get_images():
    """List images in a folder with captions + SD metadata (paginated)."""
    folder_path = (request.args.get("folderPath") or "").strip("\"'")
    page  = max(1, int(request.args.get("page",  1)))
    limit = max(1, int(request.args.get("limit", 20)))

    if not folder_path:
        return jsonify({"error": "folderPath is required"}), 400
    if not os.path.isdir(folder_path):
        return jsonify({"error": f"Folder does not exist: {folder_path}"}), 400

    image_files = sorted(
        f for f in os.listdir(folder_path)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )

    total = len(image_files)
    total_pages = max(1, (total + limit - 1) // limit)
    page_files = image_files[(page - 1) * limit : (page - 1) * limit + limit]

    results = []
    for fname in page_files:
        img_path = os.path.join(folder_path, fname)
        txt_path = os.path.join(folder_path, os.path.splitext(fname)[0] + ".txt")

        caption = ""
        if os.path.isfile(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as fh:
                    caption = fh.read()
            except Exception:
                pass

        sd = _read_sd_info(img_path)

        width = height = 0
        if _PIL_OK:
            try:
                with Image.open(img_path) as _im:
                    width, height = _im.size
            except Exception:
                pass

        results.append({
            "file": fname,
            "caption": caption,
            "sdPrompt": sd["sdPrompt"],
            "sdNegative": sd["sdNegative"],
            "sdSetting": sd["sdSetting"],
            # Live disk URL (cached, lazy-loaded) instead of an inline data URL.
            "url": f"/betacaption/api/file?folderPath={quote(folder_path)}&file={quote(fname)}",
            "hasCaption": bool(caption),
            "hasWorkflow": _extract_comfy_workflow(img_path) is not None,
            "width": width,
            "height": height,
            "size": os.path.getsize(img_path),
        })

    return jsonify({
        "images": results,
        "total": total,
        "page": page,
        "totalPages": total_pages,
        "limit": limit,
    })


@caption_bp.post("/save-caption")
def save_caption():
    """Write a caption sidecar beside an image in the selected folder."""
    data = request.get_json(silent=True) or {}
    folder = (data.get("folderPath") or "").strip().strip("\"'")
    filename = os.path.basename(data.get("fileName") or "")
    if not folder or not filename or os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
        return jsonify({"ok": False, "error": "A valid image folder and filename are required"}), 400
    image_path = os.path.join(folder, filename)
    if not _is_safe_path(folder, image_path) or not os.path.isfile(image_path):
        return jsonify({"ok": False, "error": "Image file not found"}), 404
    caption = data.get("caption", "")
    if not isinstance(caption, str):
        return jsonify({"ok": False, "error": "Caption must be text"}), 400
    try:
        with open(os.path.splitext(image_path)[0] + ".txt", "w", encoding="utf-8") as fh:
            fh.write(caption.strip())
        return jsonify({"ok": True, "caption": caption.strip()})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@caption_bp.post("/caption-single")
def caption_single():
    """Caption one image and write a .txt sidecar."""
    data = request.get_json(silent=True) or {}
    folder_path = (data.get("folderPath") or "").strip("\"'")
    file_name   = data.get("fileName", "")
    prompt      = data.get("prompt", "Caption this image")
    url         = _reachable_url(data.get("lmStudioUrl", "http://localhost:1234/v1"))
    model       = data.get("model", "model-identifier")
    trigger_tag = (data.get("triggerTag") or "").strip()

    if not folder_path or not file_name:
        return jsonify({"error": "folderPath and fileName are required"}), 400

    image_path = os.path.join(folder_path, file_name)
    txt_path   = os.path.join(folder_path, os.path.splitext(file_name)[0] + ".txt")

    if not _is_safe_path(folder_path, image_path):
        return jsonify({"error": "Invalid fileName"}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "Image file does not exist"}), 404
    if not _OPENAI_OK:
        return jsonify({"error": "openai package not installed"}), 500

    try:
        # Read full-res image for the response display
        mime, _ = mimetypes.guess_type(image_path)
        with open(image_path, "rb") as fh:
            display_b64 = f"data:{mime or 'image/jpeg'};base64," + base64.b64encode(fh.read()).decode()

        llm_b64 = _process_image_for_llm(image_path)

        client = OpenAI(api_key="not-needed", base_url=url)
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": llm_b64}},
                ],
            }],
        )

        raw = resp.choices[0].message.content
        caption = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if trigger_tag:
            caption = f"{trigger_tag}, {caption}"

        caption, sd = _build_caption_with_sd(caption, image_path)

        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(caption)

        return jsonify({
            "status": "success",
            "file": file_name,
            "caption": caption,
            "sdPrompt":   sd["sdPrompt"],
            "sdNegative": sd["sdNegative"],
            "sdSetting":  sd["sdSetting"],
            "image": display_b64,
            "hasWorkflow": _extract_comfy_workflow(image_path) is not None,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _start_caption_batch(data):
    """Validate and launch a background batch, returning its id and count."""
    folder_path = (data.get("folderPath") or "").strip("\"'")
    prompt = data.get("prompt", "Caption this image")
    url = _reachable_url(data.get("lmStudioUrl", "http://localhost:1234/v1"))
    model = data.get("model", "model-identifier")
    mode = data.get("mode")
    trigger_tag = (data.get("triggerTag") or "").strip()

    if not folder_path or not os.path.isdir(folder_path):
        return None, None, (jsonify({"error": "folderPath is required and must exist"}), 400)
    if not _OPENAI_OK:
        return None, None, (jsonify({"error": "openai package not installed"}), 500)
    try:
        client = OpenAI(api_key="not-needed", base_url=url)
        client.models.list(timeout=5.0)
    except Exception as e:
        return None, None, (jsonify({"error": f"LLM instance not reachable: {e}"}), 500)

    image_files = sorted(
        f for f in os.listdir(folder_path)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )
    if not image_files:
        return None, None, (jsonify({"error": "No images found in folder"}), 400)

    with _batch_lock:
        if _batch["state"] in ("running", "cancelling"):
            return None, None, (jsonify({"error": "A caption batch is already running", "jobId": _batch["job_id"]}), 409)
        _batch.update({
            "job_id": _batch["job_id"] + 1, "state": "running", "folder_path": folder_path,
            "current": 0, "total": len(image_files), "file": "", "event_id": 0,
            "events": [], "cancel": False, "pause": False, "last_error": "", "started_at": time.time(),
        })
        job_id = _batch["job_id"]
    worker = threading.Thread(
        target=_run_caption_batch,
        args=(job_id, image_files, folder_path, prompt, url, model, mode, trigger_tag),
        daemon=True,
    )
    worker.start()
    return job_id, len(image_files), None


@caption_bp.post("/batch/start")
def start_caption_batch():
    job_id, total, error = _start_caption_batch(request.get_json(silent=True) or {})
    if error:
        return error
    return jsonify({"ok": True, "jobId": job_id, "total": total, "state": "running"}), 202


@caption_bp.post("/caption")
def caption_batch():
    """Backward-compatible SSE stream; disconnecting does not stop the worker."""
    job_id, total, error = _start_caption_batch(request.get_json(silent=True) or {})
    if error:
        return error

    def generate():
        since = 0
        while True:
            with _batch_lock:
                if job_id != _batch["job_id"]:
                    break
                events = [event for event in _batch["events"] if event["eventId"] > since]
                state = _batch["state"]
            for event in events:
                since = max(since, event["eventId"])
                yield f"data: {json.dumps(event)}\n\n"
            if state in ("complete", "cancelled"):
                break
            time.sleep(0.25)

    return Response(generate(), mimetype="text/event-stream")


@caption_bp.get("/batch/status")
def caption_batch_status():
    """Return batch state and events after the supplied event id."""
    try:
        since = max(0, int(request.args.get("since", "0")))
    except ValueError:
        since = 0
    with _batch_lock:
        return jsonify({
            "jobId": _batch["job_id"], "state": _batch["state"],
            "folderPath": _batch["folder_path"], "current": _batch["current"],
            "total": _batch["total"], "file": _batch["file"],
            "error": _batch.get("last_error", ""),
            "events": [event for event in _batch["events"] if event["eventId"] > since],
        })


@caption_bp.get("/task")
def caption_task():
    """Normalized progress for the core's floating task notification."""
    with _batch_lock:
        if _batch["state"] not in ("running", "cancelling", "paused"):
            return jsonify({"active": False})
        return jsonify({
            "active": True,
            "state": _batch["state"],
            "label": "ComfyCaption",
            "current": _batch["current"],
            "total": _batch["total"],
            "detail": _batch["file"],
            "message": "Captioning images",
            "started_at": _batch.get("started_at", 0),
        })


@caption_bp.post("/batch/pause")
def pause_caption_batch():
    """Pause or resume a running batch (the task float's pause button)."""
    with _batch_lock:
        if _batch["state"] not in ("running", "cancelling"):
            return jsonify({"ok": True, "state": _batch["state"], "paused": False})
        if _batch["pause"]:
            _batch["pause"] = False
            _batch["state"] = "running"
            return jsonify({"ok": True, "state": "running", "paused": False})
        _batch["pause"] = True
        _batch["state"] = "paused"
        return jsonify({"ok": True, "state": "paused", "paused": True})


@caption_bp.post("/batch/stop")
def stop_caption_batch():
    with _batch_lock:
        if _batch["state"] != "running":
            return jsonify({"ok": True, "state": _batch["state"]})
        _batch["cancel"] = True
        _batch["pause"] = False
        _batch["state"] = "cancelling"
        return jsonify({"ok": True, "state": "cancelling"})


@caption_bp.get("/workflow")
def download_workflow():
    """Download the embedded ComfyUI workflow JSON from an image."""
    folder_path = (request.args.get("folderPath") or "").strip("\"'")
    file_name   = request.args.get("file", "")

    if not folder_path or not file_name:
        return jsonify({"error": "folderPath and file are required"}), 400

    image_path = os.path.join(folder_path, file_name)
    if not _is_safe_path(folder_path, image_path) or not os.path.isfile(image_path):
        return jsonify({"error": "File not found or unsafe path"}), 404

    wf = _extract_comfy_workflow(image_path)
    if not wf:
        return jsonify({"error": "No ComfyUI workflow found in image metadata"}), 404

    try:
        formatted = json.dumps(json.loads(wf), indent=2)
    except Exception:
        formatted = wf

    stem = os.path.splitext(file_name)[0]
    return Response(
        formatted,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}_workflow.json"'},
    )


@caption_bp.get("/browse-folders")
def browse_folders():
    """Web-based directory browser (works in Docker/headless)."""
    req_path = os.path.abspath(
        os.path.expanduser((request.args.get("path") or "/data").strip("\"'"))
    )
    if not os.path.exists(req_path):
        return jsonify({"error": f"Folder does not exist: {req_path}"}), 400
    if not os.path.isdir(req_path):
        req_path = os.path.dirname(req_path)
    try:
        entries = sorted(os.listdir(req_path))
    except PermissionError:
        return jsonify({"error": f"Permission denied: {req_path}"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    dirs = []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(req_path, name)
        try:
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
        except Exception:
            continue
        if len(dirs) >= 500:
            break

    parent = os.path.dirname(req_path.rstrip(os.sep)) or os.sep
    return jsonify({
        "cwd": req_path, "parent": parent,
        "dirs": dirs, "isDocker": _in_docker(),
    })


@caption_bp.get("/browse-roots")
def browse_roots():
    """Quick-pick roots for the folder browser."""
    candidates = [
        "/data", "/data/downloads", "/data/tasks", os.path.expanduser("~"), "/app", "/",
    ]
    seen = []
    for p in candidates:
        try:
            ap = os.path.abspath(p)
            if ap not in seen and os.path.isdir(ap):
                seen.append(ap)
        except Exception:
            continue
    return jsonify({"roots": seen, "isDocker": _in_docker()})


@caption_bp.get("/folders")
def list_folders():
    """Live on-disk folder listing with media counts (Gallery-style explorer)."""
    req_path = os.path.abspath(
        os.path.expanduser((request.args.get("path") or "/data").strip("\"'"))
    )
    if not os.path.exists(req_path):
        return jsonify({"error": f"Folder does not exist: {req_path}"}), 400
    if not os.path.isdir(req_path):
        req_path = os.path.dirname(req_path)

    folders = []
    try:
        for entry in sorted(os.listdir(req_path)):
            full = os.path.join(req_path, entry)
            if (
                entry.startswith(".")
                or entry in ("__pycache__", "node_modules")
                or not os.path.isdir(full)
            ):
                continue
            count = 0
            try:
                for _root, dirs, files in os.walk(full):
                    dirs[:] = [
                        d for d in dirs
                        if not d.startswith(".") and d not in ("__pycache__", "node_modules")
                    ]
                    for name in files:
                        if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                            count += 1
                    if count >= 9999:
                        break
            except OSError:
                pass
            folders.append({"name": entry, "path": full, "count": count})
    except PermissionError:
        return jsonify({"error": f"Permission denied: {req_path}"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    parent = os.path.dirname(req_path.rstrip(os.sep)) or os.sep
    return jsonify({
        "cwd": req_path,
        "parent": parent,
        "folders": folders,
        "isDocker": _in_docker(),
    })


@caption_bp.get("/status")
def status():
    return jsonify({
        "ok": True,
        "openai_available": _OPENAI_OK,
        "pillow_available": _PIL_OK,
    })
