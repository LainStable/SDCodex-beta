import { app } from "../../scripts/app.js";

// Register custom styles for the gallery widget
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
        grid-auto-rows: 115px;
        gap: 6px;
        min-height: 100px;
        overflow-y: auto;
        background: #101014;
        border: 1px solid #2a2a30;
        border-radius: 4px;
        padding: 4px;
    }
    .sdcodex-gallery-item {
        height: 115px;
        min-height: 115px;
        max-height: 115px;
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

app.registerExtension({
    name: "SDCodex.GallerySelector",
    async nodeCreated(node) {
        if (node.comfyClass !== "SDCodexGallery") return;

        // Create DOM container for our custom gallery widget
        const container = document.createElement("div");
        container.className = "sdcodex-gallery-container";

        // HTML Structure
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

        // Local state
        let currentPage = 1;
        let totalPages = 1;
        let imagesData = [];
        let searchQuery = "";

        // Function to fetch and display the images
        const loadGallery = async () => {
            // Find inputs from node widgets
            const modeWidget = node.widgets.find(w => w.name === "mode");
            const folderPathWidget = node.widgets.find(w => w.name === "folder_path");
            const rootWidget = node.widgets.find(w => w.name === "sdcodex_root");

            const mode = modeWidget ? modeWidget.value : "Saved Gallery";
            const folderPath = folderPathWidget ? folderPathWidget.value : "";
            const rootPath = rootWidget ? rootWidget.value : "";

            grid.innerHTML = `<div style="grid-column: 1 / -1; display: flex; align-items: center; justify-content: center; height: 100%; color: #888; font-size: 11px;">Loading...</div>`;
            
            try {
                const url = `/sdcodex/images?mode=${encodeURIComponent(mode)}&folder_path=${encodeURIComponent(folderPath)}&sdcodex_root=${encodeURIComponent(rootPath)}&page=${currentPage}&limit=24`;
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

        // Render the image grid based on current state and search filter
        const renderGrid = () => {
            grid.innerHTML = "";

            const selectedImageWidget = node.widgets.find(w => w.name === "selected_image");
            const currentSelectedPath = selectedImageWidget ? selectedImageWidget.value : "";

            // Filter images if search query exists
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
                    
                    // Apply explicit inline height dimensions to prevent squishing under any container sizing
                    item.style.height = "115px";
                    item.style.minHeight = "115px";
                    item.style.maxHeight = "115px";
                    
                    // Highlight if currently selected
                    if (currentSelectedPath === img.image_path) {
                        item.classList.add("selected");
                    }

                    const isGif = img.image_path.toLowerCase().endsWith(".gif");
                    const thumbUrl = `/sdcodex/image?path=${encodeURIComponent(img.image_path)}`;
                    
                    item.innerHTML = `<img src="${thumbUrl}" alt="${img.file_name}" loading="lazy" style="width: 100%; height: 100%; object-fit: cover;">`;
                    
                    item.addEventListener("click", () => {
                        // De-select all items
                        grid.querySelectorAll(".sdcodex-gallery-item").forEach(el => el.classList.remove("selected"));
                        item.classList.add("selected");

                        // Update widgets
                        if (selectedImageWidget) {
                            selectedImageWidget.value = img.image_path;
                            if (selectedImageWidget.callback) selectedImageWidget.callback(img.image_path);
                        }

                        const captionWidget = node.widgets.find(w => w.name === "caption");
                        if (captionWidget) {
                            captionWidget.value = img.caption || "";
                            if (captionWidget.callback) captionWidget.callback(img.caption || "");
                        }

                        // Trigger visual graph update
                        app.graph.setDirtyCanvas(true, true);
                    });

                    grid.appendChild(item);
                });
            }

            // Update footer pagination controls
            statusSpan.innerText = `Page ${currentPage} of ${totalPages}`;
            prevBtn.disabled = currentPage <= 1;
            nextBtn.disabled = currentPage >= totalPages;
        };

        // Hook up pagination actions
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

        // Refresh button
        refreshBtn.addEventListener("click", () => {
            loadGallery();
        });

        // Search input
        searchInput.addEventListener("input", (e) => {
            searchQuery = e.target.value;
            renderGrid();
        });

        // Add the DOM widget to the node
        const galleryWidget = node.addDOMWidget("sdcodex_gallery_widget", "custom_gallery", container);
        galleryWidget.serializeValue = () => undefined; // Prevent serializing raw HTML
        
        // Auto-refresh when relevant widgets change values
        setTimeout(() => {
            const modeWidget = node.widgets.find(w => w.name === "mode");
            const folderPathWidget = node.widgets.find(w => w.name === "folder_path");
            const rootWidget = node.widgets.find(w => w.name === "sdcodex_root");

            const onWidgetChanged = () => {
                currentPage = 1;
                loadGallery();
            };

            if (modeWidget) {
                const origCallback = modeWidget.callback;
                modeWidget.callback = function(...args) {
                    if (origCallback) origCallback.apply(this, args);
                    onWidgetChanged();
                };
            }

            if (folderPathWidget) {
                const origCallback = folderPathWidget.callback;
                folderPathWidget.callback = function(...args) {
                    if (origCallback) origCallback.apply(this, args);
                    onWidgetChanged();
                };
            }

            if (rootWidget) {
                const origCallback = rootWidget.callback;
                rootWidget.callback = function(...args) {
                    if (origCallback) origCallback.apply(this, args);
                    onWidgetChanged();
                };
            }

            // Perform initial load and force resize calculation
            loadGallery();
            setTimeout(() => {
                node.onResize?.(node.size);
            }, 50);
        }, 100);

        // Set up custom onResize handler to dynamically adjust gallery height to half the node height
        const origOnResize = node.onResize;
        node.onResize = function(size) {
            if (origOnResize) {
                origOnResize.apply(this, arguments);
            }

            // Gallery takes exactly half of the node space
            // Ensure grid height is at least 240px to display at least 2 full lines of 115px thumbnails
            const galleryHeight = Math.max(320, size[1] / 2);
            const gridHeight = galleryHeight - 80;

            const gridEl = container.querySelector(".sdcodex-gallery-grid");
            if (gridEl) {
                gridEl.style.height = gridHeight + "px";
            }

            // Set heights on the LiteGraph widget and its container to prevent flexbox shrinking on canvas
            galleryWidget.height = galleryHeight;
            container.style.height = galleryHeight + "px";
        };

        // Set minimum node dimensions to prevent layout overlap
        node.min_size = [350, 720];

        // Set default node dimensions: 500px wide (fits 4 columns), 800px tall (fits standard text boxes and gallery)
        node.size = [500, 800];
        node.onResize?.(node.size);
    }
});
