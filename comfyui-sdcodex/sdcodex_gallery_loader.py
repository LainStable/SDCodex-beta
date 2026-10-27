import os

# Disk-based gallery loader: images live in real folders and captions are read
# from "<image>.txt" sidecar files (as written by the captioning plugin); SD
# prompts / negative prompts are read from image metadata. No SDCodex database
# is used.

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if os.path.exists(os.path.join(parent_dir, "run.py")):
    default_sdcodex_root = parent_dir
else:
    default_sdcodex_root = "/home/naked/dev/SDCodex"


def _read_sidecar_caption(image_path):
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


class SDCodexGalleryLoader:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "selected_image": ("STRING", {"default": ""}),
                "caption": ("STRING", {"default": "", "multiline": True}),
                "sd_prompt": ("STRING", {"default": "", "multiline": True}),
                "sd_negative": ("STRING", {"default": "", "multiline": True}),
                "prefix": ("STRING", {"default": "", "multiline": True}),
                "suffix": ("STRING", {"default": "", "multiline": True}),
            }
        }

    RETURN_TYPES = ("STRING", "IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("caption", "image", "sd_prompt", "sd_negative")
    FUNCTION = "execute"
    CATEGORY = "SDCodex"

    def execute(self, selected_image, caption, sd_prompt, sd_negative, prefix="", suffix=""):
        import torch
        import numpy as np
        from PIL import Image, ImageOps

        out_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)

        final_caption = caption or ""
        out_sd_prompt = sd_prompt or ""
        out_sd_negative = sd_negative or ""

        img_path = None
        if selected_image:
            if os.path.isabs(selected_image) and os.path.exists(selected_image):
                img_path = selected_image
            else:
                candidate = os.path.join(default_sdcodex_root, "downloads", selected_image)
                if os.path.isfile(candidate):
                    img_path = candidate

        if img_path and os.path.isfile(img_path):
            try:
                img = Image.open(img_path)
                img = ImageOps.exif_transpose(img)
                img_rgb = img.convert("RGB")

                np_img = np.array(img_rgb).astype(np.float32) / 255.0
                out_image = torch.from_numpy(np_img)[None,]

                if not final_caption:
                    final_caption = _read_sidecar_caption(img_path)
                if not out_sd_prompt or not out_sd_negative:
                    sd_pos, sd_neg = _read_sd_prompt(img_path)
                    if not out_sd_prompt:
                        out_sd_prompt = sd_pos
                    if not out_sd_negative:
                        out_sd_negative = sd_neg
            except Exception as e:
                print(f"Error loading image file {img_path}: {e}")

        if prefix and prefix.strip():
            p = prefix.strip()
            if final_caption:
                sep = " " if not (p.endswith(",") or p.endswith(" ")) else ""
                final_caption = f"{p}{sep}{final_caption}"
            else:
                final_caption = p
            if out_sd_prompt:
                sep = " " if not (p.endswith(",") or p.endswith(" ")) else ""
                out_sd_prompt = f"{p}{sep}{out_sd_prompt}"
            else:
                out_sd_prompt = p

        if suffix and suffix.strip():
            s = suffix.strip()
            if final_caption:
                sep = " " if not (s.startswith(",") or s.startswith(" ")) else ""
                final_caption = f"{final_caption}{sep}{s}"
            else:
                final_caption = s
            if out_sd_prompt:
                sep = " " if not (s.startswith(",") or s.startswith(" ")) else ""
                out_sd_prompt = f"{out_sd_prompt}{sep}{s}"
            else:
                out_sd_prompt = s

        return (final_caption, out_image, out_sd_prompt, out_sd_negative)