import { app } from "../../scripts/app.js";

const style = document.createElement("style");
style.innerHTML = `
    .sdcodex-gallery-container {
        display: flex;
        flex-direction: column;
        gap: 6px;
        background: #1e1e24;
        border: 1px solid #3e3e4a;
        border-radius: 6px;
        padding: 8px;
        font-family: Arial, sans-serif;
        color: #eee;
        width: 100%;
        box-sizing: border-box;
        margin: 5px 0;
        overflow: hidden;
    }
    .sdcodex-gallery-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 6px;
    }
    .sdcodex-gallery-search {
        flex: 1;
        background: #111;
        border: 1px solid #444;
        color: #fff;
        border-radius: 4px;
        padding: 3px 6px;
        font-size: 11px;
    }
    .sdcodex-gallery-btn {
        background: #3a3a4a;
        border: 1px solid #555;
        color: #fff;
        border-radius: 4px;
        padding: 3px 8px;
        cursor: pointer;
        font-size: 11px;
        transition: background 0.2s;
    }
    .sdcodex-gallery-btn:hover {
        background: #4e4e60;
    }
    .sdcodex-gallery-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
    }
    .sdcodex-gallery-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(115px, 1fr));
        grid-auto-rows: 1fr;
        gap: 6px;
        min-height: 0;
        flex: 1;
        overflow-y: auto;
        background: #101014;
        border: 1px solid #2a2a30;
        border-radius: 4px;
        padding: 4px;
    }
    .sdcodex-gallery-item {
        min-height: 0;
        background: #25252b;
        border-radius: 4px;
        overflow: hidden;
        cursor: pointer;
        border: 2px solid transparent;
        position: relative;
        transition: border-color 0.15s, transform 0.1s;
    }
    .sdcodex-gallery-item:hover {
        border-color: #555;
        transform: scale(1.02);
    }
    .sdcodex-gallery-item.selected {
        border-color: #007acc;
        box-shadow: 0 0 5px rgba(0, 122, 204, 0.6);
    }
    .sdcodex-gallery-item img {
        width: 100%;
        height: 100%;
        object-fit: cover;
    }
    .sdcodex-gallery-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 10px;
        color: #aaa;
    }
    .sdcodex-gallery-status {
        color: #888;
    }
`;
document.head.appendChild(style);

// Shared helpers used by both the Gallery Browser and the Gallery Runner.
// They are attached to define a single module-level set of functions, then
// re-exposed on window for the runner node to drive.
function setWidget(node, name, val) {
    if (!node || !node.widgets) return;
    const w = node.widgets.find(widget => widget.name === name);
    if (w) {
        w.value = val;
        if (w.inputEl) w.inputEl.value = val;
        if (w.callback) w.callback(val);
    }
}

function getWidget(node, name) {
    if (!node) return "";
    const w = node.widgets?.find(widget => widget.name === name);
    return w ? w.value ?? "" : "";
}

// Get all SDCodexGalleryLoader nodes in the graph (a list of the loader nodes).
function findLoaderNodes(graph) {
    const nodes = (graph && graph._nodes) || [];
    return nodes.filter(n => n.comfyClass === "SDCodexGalleryLoader");
}

// Broadcast a selected gallery image onto the browser node and every
// SDCodexGalleryLoader node so the downstream generation workflow always runs
// the image that is highlighted/cycling.
function syncSelectionNodes(selectedImage, caption, sdPrompt, sdNegative, graph, browserRoot) {
    const graphRef = graph || app.graph;
    const browser = browserRoot
        || (graphRef._nodes || []).find(n => n.comfyClass === "SDCodexGalleryBrowser");
    if (browser) {
        setWidget(browser, "selected_image", selectedImage);
        setWidget(browser, "caption", caption);
        setWidget(browser, "sd_prompt", sdPrompt);
        setWidget(browser, "sd_negative", sdNegative);
    }
    for (const targetNode of findLoaderNodes(graphRef)) {
        setWidget(targetNode, "selected_image", selectedImage);
        setWidget(targetNode, "caption", caption);
        setWidget(targetNode, "sd_prompt", sdPrompt);
        setWidget(targetNode, "sd_negative", sdNegative);
    }
}

// Expose helpers for the runner node to use.
window.SDCodexGalleryHelpers = {
    setWidget,
    getWidget,
    syncSelectionNodes,
    findLoaderNodes,
};

app.registerExtension({
    name: "SDCodex.GalleryBrowser",
    async nodeCreated(node) {
        if (node.comfyClass !== "SDCodexGalleryBrowser") return;

        const container = document.createElement("div");
        container.className = "sdcodex-gallery-container";

        container.innerHTML = `
            <div class="sdcodex-gallery-header">
                <input type="text" class="sdcodex-gallery-search" placeholder="Search gallery...">
                <button class="sdcodex-gallery-btn refresh-btn">↻</button>
            </div>
            <div class="sdcodex-gallery-grid"></div>
            <div class="sdcodex-gallery-footer">
                <button class="sdcodex-gallery-btn prev-btn">◀ Prev</button>
                <span class="sdcodex-gallery-status">Page 1 of 1</span>
                <button class="sdcodex-gallery-btn next-btn">Next ▶</button>
            </div>
        `;

        const grid = container.querySelector(".sdcodex-gallery-grid");
        const prevBtn = container.querySelector(".prev-btn");
        const nextBtn = container.querySelector(".next-btn");
        const statusSpan = container.querySelector(".sdcodex-gallery-status");
        const refreshBtn = container.querySelector(".refresh-btn");
        const searchInput = container.querySelector(".sdcodex-gallery-search");

        let currentPage = 1;
        let totalPages = 1;
        let imagesData = [];
        let searchQuery = "";

        const getWidgetVal = (name, fallback = "") => getWidget(node, name) || fallback;

        const loadGallery = async () => {
            const folderPath = getWidgetVal("folder_path");

            grid.innerHTML = `<div style="grid-column: 1 / -1; display: flex; align-items: center; justify-content: center; height: 100%; color: #888; font-size: 11px;">Loading...</div>`;

            try {
                const url = `/sdcodex/images?folder_path=${encodeURIComponent(folderPath)}&page=${currentPage}&limit=24`;
                const response = await fetch(url);

                if (!response.ok) {
                    const errText = await response.text();
                    throw new Error(errText || "Error loading images");
                }

                const data = await response.json();
                imagesData = data.images || [];
                totalPages = data.totalPages || 1;
                currentPage = data.page || 1;

                renderGrid();
            } catch (err) {
                console.error(err);
                grid.innerHTML = `<div style="grid-column: 1 / -1; color: #ff5555; font-size: 11px; padding: 10px;">Error: ${err.message}</div>`;
            }
        };

        const renderGrid = () => {
            grid.innerHTML = "";

            const currentSelectedPath = getWidget("selected_image");

            const filteredImages = imagesData.filter(img => {
                if (!searchQuery) return true;
                const q = searchQuery.toLowerCase();
                const nameMatch = img.file_name.toLowerCase().includes(q);
                const captionMatch = img.caption && img.caption.toLowerCase().includes(q);
                return nameMatch || captionMatch;
            });

            if (filteredImages.length === 0) {
                grid.innerHTML = `<div style="grid-column: 1 / -1; display: flex; align-items: center; justify-content: center; height: 100%; color: #666; font-size: 11px;">No images found</div>`;
            } else {
                filteredImages.forEach(img => {
                    const item = document.createElement("div");
                    item.className = "sdcodex-gallery-item";

                    if (currentSelectedPath === img.image_path) {
                        item.classList.add("selected");
                    }

                    const thumbUrl = `/sdcodex/image?path=${encodeURIComponent(img.image_path)}`;
                    item.innerHTML = `<img src="${thumbUrl}" alt="${img.file_name}" loading="lazy" style="width: 100%; height: 100%; object-fit: cover;">`;

                    item.addEventListener("click", () => {
                        grid.querySelectorAll(".sdcodex-gallery-item").forEach(el => el.classList.remove("selected"));
                        item.classList.add("selected");

                        const setImagePath = img.image_path || "";
                        const setCaption = img.caption || "";
                        const setSdPrompt = img.sd_prompt || "";
                        const setSdNegative = img.sd_negative || "";

                        setWidget(node, "selected_image", setImagePath);
                        setWidget(node, "caption", setCaption);
                        setWidget(node, "sd_prompt", setSdPrompt);
                        setWidget(node, "sd_negative", setSdNegative);

                        // Sync the gallery loader nodes too.
                        syncSelectionNodes(setImagePath, setCaption, setSdPrompt, setSdNegative, app.graph);

                        app.graph.setDirtyCanvas(true, true);
                    });

                    grid.appendChild(item);
                });
            }

            statusSpan.innerText = `Page ${currentPage} of ${totalPages}`;
            prevBtn.disabled = currentPage <= 1;
            nextBtn.disabled = currentPage >= totalPages;
        };

        prevBtn.addEventListener("click", () => {
            if (currentPage > 1) {
                currentPage--;
                loadGallery();
            }
        });

        nextBtn.addEventListener("click", () => {
            if (currentPage < totalPages) {
                currentPage++;
                loadGallery();
            }
        });

        refreshBtn.addEventListener("click", () => {
            loadGallery();
        });

        searchInput.addEventListener("input", (e) => {
            searchQuery = e.target.value;
            renderGrid();
        });

        const galleryWidget = node.addDOMWidget("sdcodex_gallery_widget", "custom_gallery", container);
        galleryWidget.serializeValue = () => undefined;

        setTimeout(() => {
            const folderPathWidget = node.widgets?.find(w => w.name === "folder_path");

            if (folderPathWidget) {
                const origCallback = folderPathWidget.callback;
                folderPathWidget.callback = function(...args) {
                    if (origCallback) origCallback.apply(this, args);
                    currentPage = 1;
                    loadGallery();
                };
            }

            loadGallery();
            setTimeout(() => {
                node.onResize?.(node.size);
            }, 50);
        }, 100);

        const origOnResize = node.onResize;
        node.onResize = function(size) {
            if (origOnResize) {
                origOnResize.apply(this, arguments);
            }

            const headerFooterApprox = 90;
            const galleryHeight = Math.max(200, size[1] - headerFooterApprox);

            const gridEl = container.querySelector(".sdcodex-gallery-grid");
            if (gridEl) {
                gridEl.style.height = "100%";
            }

            galleryWidget.height = galleryHeight;
            container.style.height = galleryHeight + "px";
        };

        node.min_size = [350, 400];
        node.size = [500, 700];
        node.onResize?.(node.size);
    }
});