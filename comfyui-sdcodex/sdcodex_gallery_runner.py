import os


class SDCodexGalleryRunner:
    """Control node that cycle-runs the workflow across the images shown in a
    connected SDCodexGalleryBrowser node.

    The actual looping is driven from the frontend (see
    js/sdcodex_gallery_runner.js): a Start button runs the workflow for the
    currently selected image, then advances to the next image in the folder,
    until Stop is pressed or the configured number of images is cycled.

    Widgets
    -------
    executions_per_image : how many times to run the workflow on each image
        before advancing to the next.
    max_images           : how many images to cycle through before auto-stop.
        0 / blank means "keep going until the end of the image list".

    The optional ``current_image`` input should be wired to the Gallery
    Browser node's ``selected_image`` output so the runner can locate the
    browser node and read its ``folder_path`` / current selection. If not
    wired, the first SDCodexGalleryBrowser found in the graph is used.
    """

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "executions_per_image": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 999,
                    "step": 1,
                    "tooltip": "How many times to run the workflow on each image before advancing to the next."
                }),
                "max_images": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 100000,
                    "step": 1,
                    "tooltip": "How many images to cycle through before stopping. 0 / blank = until the end of the image list."
                }),
            },
            "optional": {
                "current_image": ("STRING", {"forceInput": True, "default": ""}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("current_image",)
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "SDCodex"

    def execute(self, executions_per_image=1, max_images=0, current_image=""):
        # The selection / looping is orchestrated by the frontend runner (see
        # js/sdcodex_gallery_runner.js). This execute() is a pass-through: it
        # forwards the currently selected image so the node composes in the
        # graph (e.g. for display or logging) even before Start is pressed.
        return (current_image or "",)