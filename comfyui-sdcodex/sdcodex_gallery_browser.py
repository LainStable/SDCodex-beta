import os
import mimetypes
from aiohttp import web
from server import PromptServer

# The gallery is fully disk-based and folder-only: images live in real folders
# and captions are read from "<image>.txt" sidecar files (as written by the
# captioning plugin). SD prompts / negative prompts are read from image
# metadata. No SDCodex database is used and there is no "Saved Gallery" mode.

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if os.path.exists(os.path.join(parent_dir, "run.py")):
    default_sdcodex_root = parent_dir
else:
    default_sdcodex_root = "/home/naked/dev/SDCodex"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}


def _read_sidecar_caption(image_path):
    """Read the .txt sidecar caption beside an image, if present."""
    base, _ = os.path.splitext(image_path)
    txt_path = base + ".txt"
    if os.path.isfile(txt_path):
        try:
            with open(txt_path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read().strip()
        except Exception:
            pass
    return ""


def _read_sd_prompt(image_path):
    """Best-effort SD positive/negative from image metadata (A1111-style)."""
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            meta = img.info.get("prompt") or img.info.get("parameters")
            if meta and isinstance(meta, str) and "workflow" not in meta:
                lines = [ln.strip() for ln in meta.split("\n") if ln.strip()]
                if lines:
                    positive = lines[0]
                    negative = ""
                    for ln in lines[1:]:
                        if ln.lower().startswith("negative prompt:"):
                            negative = ln[len("negative prompt:"):].strip()
                            break
                    return positive, negative
    except Exception:
        pass
    return "", ""


def _list_images(folder_path):
    """Return sorted image file names in a directory (case-insensitive ext)."""
    try:
        names = os.listdir(folder_path)
    except Exception:
        return []
    return sorted(
        n for n in names
        if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
    )


def _resolve_folder(params):
    """Resolve the folder to browse from query params (folder-only)."""
    folder_path = params.get("folder_path", "").strip()
    if not folder_path or not os.path.isdir(folder_path):
        return None
    return folder_path


@PromptServer.instance.routes.get("/sdcodex/images")
async def get_sdcodex_images(request):
    params = request.query
    folder_path = _resolve_folder(params)
    page = int(params.get("page", 1))
    limit = int(params.get("limit", 24))

    page = max(1, page)
    limit = max(1, limit)

    if not folder_path:
        return web.json_response(
            {"error": "folder_path must be a real directory"},
            status=400,
        )

    try:
        image_files = _list_images(folder_path)
        total = len(image_files)
        total_pages = (total + limit - 1) // limit if total > 0 else 1

        start_index = (page - 1) * limit
        page_files = image_files[start_index: start_index + limit]

        results = []
        for file in page_files:
            img_abs_path = os.path.join(folder_path, file)
            caption = _read_sidecar_caption(img_abs_path)
            sd_pos, sd_neg = _read_sd_prompt(img_abs_path)
            results.append({
                "file_name": file,
                "image_path": img_abs_path,
                "caption": caption,
                "sd_prompt": sd_pos,
                "sd_negative": sd_neg,
            })

        return web.json_response({
            "images": results,
            "total": total,
            "page": page,
            "totalPages": total_pages,
            "limit": limit,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.get("/sdcodex/image")
async def get_sdcodex_image_content(request):
    params = request.query
    path = params.get("path", "").strip()
    if not path:
        return web.Response(status=400, text="Path parameter is required")

    if not os.path.exists(path):
        return web.Response(status=404, text="File not found")

    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return web.Response(status=400, text="File requested is not a valid image format")

    try:
        with open(path, "rb") as f:
            content = f.read()
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "image/jpeg"
        return web.Response(body=content, content_type=mime)
    except Exception as e:
        return web.Response(status=500, text=str(e))


class SDCodexGalleryBrowser:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "folder_path": ("STRING", {"default": ""}),
                "selected_image": ("STRING", {"default": ""}),
                "caption": ("STRING", {"default": "", "multiline": True}),
                "sd_prompt": ("STRING", {"default": "", "multiline": True}),
                "sd_negative": ("STRING", {"default": "", "multiline": True}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("selected_image", "caption", "sd_prompt", "sd_negative", "image")
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "SDCodex"

    def execute(self, folder_path="", selected_image="", caption="", sd_prompt="", sd_negative=""):
        import torch
        import numpy as np
        from PIL import Image, ImageOps

        out_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
        out_sd_prompt = sd_prompt or ""
        out_sd_negative = sd_negative or ""

        img_path = None
        if selected_image:
            if os.path.isabs(selected_image) and os.path.isfile(selected_image):
                img_path = selected_image
            elif folder_path and os.path.isdir(folder_path) and os.path.isfile(
                os.path.join(folder_path, selected_image)
            ):
                img_path = os.path.join(folder_path, selected_image)

        if img_path and os.path.isfile(img_path):
            try:
                img = Image.open(img_path)
                img = ImageOps.exif_transpose(img)
                img_rgb = img.convert("RGB")

                np_img = np.array(img_rgb).astype(np.float32) / 255.0
                out_image = torch.from_numpy(np_img)[None,]

                if not caption.strip():
                    caption = _read_sidecar_caption(img_path)
                if not out_sd_prompt or not out_sd_negative:
                    sd_pos, sd_neg = _read_sd_prompt(img_path)
                    if not out_sd_prompt:
                        out_sd_prompt = sd_pos
                    if not out_sd_negative:
                        out_sd_negative = sd_neg
            except Exception as e:
                print(f"Error loading image file {img_path}: {e}")

        return (selected_image or "", caption or "", out_sd_prompt, out_sd_negative, out_image)