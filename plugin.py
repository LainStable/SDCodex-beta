"""SDCodex ComfyCaption plugin entrypoint."""

import os
import sys

plugin_dir = os.path.dirname(os.path.abspath(__file__))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)


def init_plugin(app, db, plugin_info=None):
    """Register caption APIs and the ComfyUI node installer."""
    from betacaption_backend import caption_bp
    from betacaption_models_backend import models_bp
    from betacaption_nodes_backend import nodes_bp

    if "beta_caption_bp" not in app.blueprints:
        app.register_blueprint(caption_bp)
        app.logger.info("ComfyCaption API mounted at /betacaption/api")

    if "beta_caption_models_bp" not in app.blueprints:
        app.register_blueprint(models_bp)
        app.logger.info("ComfyCaption Models API mounted at /betacaption/models")

    if "beta_comfyui_nodes_bp" not in app.blueprints:
        app.register_blueprint(nodes_bp)
        app.logger.info("ComfyCaption node installer mounted at /betacaption/models/nodes")

    # Vendored copy of the Gallery browser (grid, folders and media viewer) so
    # this beta plugin does not depend on the Gallery plugin being installed.
    try:
        from betacaption_gallery_browser import gallery_bp

        if "betacaption_gallery" not in app.blueprints:
            app.register_blueprint(gallery_bp)
    except Exception:
        app.logger.warning("Beta Caption: vendored Gallery browser unavailable", exc_info=True)

    return True
