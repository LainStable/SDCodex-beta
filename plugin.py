"""SDCodex AI Renamer plugin entrypoint."""

import importlib.util
import os
import sys

plugin_dir = os.path.dirname(os.path.abspath(__file__))
_BACKEND_NAME = "_sdcodex_betarenamer_backend"


def _load_backend():
    """Import ``plugin_backend.py`` under a plugin-private module name.

    GalleryDL ships a module with the same filename, and the core imports
    plugins alphabetically, so a plain ``from plugin_backend import ...``
    bound whichever copy reached ``sys.modules`` first. That silently skipped
    this blueprint and every ``/betarenamer/api`` call hit the SPA handler.
    """
    if _BACKEND_NAME in sys.modules:
        return sys.modules[_BACKEND_NAME]
    path = os.path.join(plugin_dir, "plugin_backend.py")
    spec = importlib.util.spec_from_file_location(_BACKEND_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load AI Renamer backend from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BACKEND_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_BACKEND_NAME, None)
        raise
    return module


def init_plugin(app, db, plugin_info=None):
    backend = _load_backend()

    if "beta_renamer_bp" not in app.blueprints:
        app.register_blueprint(backend.ai_renamer_bp)

    # Vendored copy of the Gallery browser (grid, folders and media viewer) so
    # this beta plugin does not depend on the Gallery plugin being installed.
    try:
        from betarenamer_gallery_browser import gallery_bp

        if "betarenamer_gallery" not in app.blueprints:
            app.register_blueprint(gallery_bp)
    except Exception:
        app.logger.warning("Beta Renamer: vendored Gallery browser unavailable", exc_info=True)
    return True
