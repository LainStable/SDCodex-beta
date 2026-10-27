from .sdcodex_gallery_browser import SDCodexGalleryBrowser
from .sdcodex_gallery_loader import SDCodexGalleryLoader
from .sdcodex_gallery_runner import SDCodexGalleryRunner

NODE_CLASS_MAPPINGS = {
    "SDCodexGalleryBrowser": SDCodexGalleryBrowser,
    "SDCodexGalleryLoader": SDCodexGalleryLoader,
    "SDCodexGalleryRunner": SDCodexGalleryRunner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SDCodexGalleryBrowser": "SDCodex Gallery Browser",
    "SDCodexGalleryLoader": "SDCodex Gallery Loader",
    "SDCodexGalleryRunner": "SDCodex Gallery Runner",
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]