import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "SDCodex.GalleryLoader",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "SDCodexGalleryLoader") return;

        const origOnCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function() {
            origOnCreated?.apply(this, arguments);
            this.min_size = [350, 200];
            this.size = [400, 300];
        };
    }
});
