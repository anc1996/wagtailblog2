(function () {
    "use strict";

    var LOG_PREFIX = "[BlogVditor]";
    var PAGE_LINK_ID_PATTERN = /^[1-9][0-9]{0,18}$/;
    var IMAGE_ID_PATTERN = /^[1-9][0-9]{0,18}$/;
    var IMAGE_FORMAT_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
    var IMAGE_UPLOAD_TOKEN_PATTERN =
        /<!--blog-vditor-upload:[A-Za-z0-9]+-->/g;
    var IMAGE_UPLOAD_CONCURRENCY = 2;
    var formUploadStates = new WeakMap();

    function log(event, details) {
        console.info(LOG_PREFIX, event, details || {});
    }

    function logError(event, error, details) {
        console.error(LOG_PREFIX, event, details || {}, error);
    }

    function summarizeValue(value) {
        var text = value == null ? "" : String(value);
        return {
            characters: text.length,
            lines: text ? text.split(/\r?\n/).length : 0,
        };
    }

    function isEscaped(text, index) {
        var slashCount = 0;
        for (var cursor = index - 1; cursor >= 0 && text[cursor] === "\\"; cursor -= 1) {
            slashCount += 1;
        }
        return slashCount % 2 === 1;
    }

    function nextMathMarker(text, start) {
        for (var index = start; index < text.length; index += 1) {
            if (text[index] !== "$" || isEscaped(text, index)) {
                continue;
            }
            if (text[index + 1] === "$") {
                return { index: index, display: true };
            }
            if (text[index - 1] !== "$" && text[index + 1] !== "$") {
                return { index: index, display: false };
            }
        }
        return null;
    }

    function closingMathMarker(text, start, display) {
        var markerLength = display ? 2 : 1;
        for (var index = start + markerLength; index < text.length; index += 1) {
            if (text[index] !== "$" || isEscaped(text, index)) {
                continue;
            }
            if (display) {
                if (text[index + 1] === "$") {
                    return index;
                }
            } else if (text[index - 1] !== "$" && text[index + 1] !== "$") {
                return index;
            }
        }
        return -1;
    }

    function createMathFragment(text, document) {
        var fragment = document.createDocumentFragment();
        var cursor = 0;
        var changed = false;

        while (cursor < text.length) {
            var marker = nextMathMarker(text, cursor);
            if (!marker) {
                fragment.appendChild(document.createTextNode(text.slice(cursor)));
                break;
            }

            if (marker.index > cursor) {
                fragment.appendChild(
                    document.createTextNode(text.slice(cursor, marker.index))
                );
            }
            var close = closingMathMarker(text, marker.index, marker.display);
            if (close < 0) {
                fragment.appendChild(document.createTextNode(text.slice(marker.index)));
                break;
            }

            var markerLength = marker.display ? 2 : 1;
            var formula = text.slice(marker.index + markerLength, close).trim();
            if (!formula) {
                fragment.appendChild(
                    document.createTextNode(text.slice(marker.index, close + markerLength))
                );
            } else {
                var math = document.createElement(marker.display ? "div" : "span");
                math.className = "language-math";
                math.textContent = formula;
                fragment.appendChild(math);
                changed = true;
            }
            cursor = close + markerLength;
        }

        return changed ? fragment : null;
    }

    function transformTableMath(table, document) {
        var walker = document.createTreeWalker(table, NodeFilter.SHOW_TEXT);
        var textNodes = [];
        var node;
        while ((node = walker.nextNode())) {
            var parent = node.parentElement;
            if (
                parent &&
                !parent.closest("code, pre, script, style, textarea, .language-math")
            ) {
                textNodes.push(node);
            }
        }
        textNodes.forEach(function (textNode) {
            var fragment = createMathFragment(textNode.nodeValue || "", document);
            if (fragment) {
                textNode.replaceWith(fragment);
            }
        });
    }

    function readEmbedAttribute(markup, name) {
        var pattern = new RegExp(
            "\\b" + name + "\\s*=\\s*([\\\"'])(.*?)\\1",
            "i"
        );
        var match = pattern.exec(markup || "");
        return match ? match[2] : "";
    }

    function splitMarkdownTableCells(line) {
        var value = String(line || "").trim();
        if (value.charAt(0) === "|") {
            value = value.slice(1);
        }
        if (value.charAt(value.length - 1) === "|" && value.charAt(value.length - 2) !== "\\") {
            value = value.slice(0, -1);
        }
        var cells = [];
        var current = "";
        var escaped = false;
        for (var index = 0; index < value.length; index += 1) {
            var character = value.charAt(index);
            if (character === "\\" && !escaped) {
                escaped = true;
                current += character;
                continue;
            }
            if (character === "|" && !escaped) {
                cells.push(current.trim());
                current = "";
            } else {
                current += character;
            }
            escaped = false;
        }
        cells.push(current.trim());
        return cells;
    }

    function isMarkdownTableDivider(line) {
        var cells = splitMarkdownTableCells(line);
        return cells.length > 1 && cells.every(function (cell) {
            return /^:?-{1,}:?$/.test(cell.trim());
        });
    }

    var TABLE_IMAGE_EMBED_PATTERN = /<embed\b[^>]*\bembedtype\s*=\s*["']image["'][^>]*\/?\s*>/gi;
    var EDITOR_TABLE_IMAGE_EMBED_PATTERN = /&lt;embed\b([\s\S]*?)\/?&gt;/gi;

    function isTableImageEmbed(markup) {
        return (
            readEmbedAttribute(markup, "embedtype").toLowerCase() === "image" &&
            IMAGE_ID_PATTERN.test(readEmbedAttribute(markup, "id")) &&
            IMAGE_FORMAT_PATTERN.test(readEmbedAttribute(markup, "format"))
        );
    }

    function transformMarkdownTableEmbeds(source, transform, pattern) {
        var lines = String(source || "").split(/\r?\n/);
        var inFence = false;
        for (var index = 0; index < lines.length - 1; index += 1) {
            var line = lines[index];
            if (/^\s*(```|~~~)/.test(line)) {
                inFence = !inFence;
                continue;
            }
            if (
                inFence ||
                line.indexOf("|") === -1 ||
                !isMarkdownTableDivider(lines[index + 1])
            ) {
                continue;
            }
            var rowIndex = index;
            while (
                rowIndex < lines.length &&
                lines[rowIndex].trim() &&
                lines[rowIndex].indexOf("|") !== -1
            ) {
                lines[rowIndex] = lines[rowIndex].replace(
                    pattern || TABLE_IMAGE_EMBED_PATTERN,
                    function (markup) {
                        return isTableImageEmbed(markup)
                            ? transform(markup)
                            : markup;
                    }
                );
                rowIndex += 1;
            }
            index = rowIndex - 1;
        }
        return lines.join("\n");
    }

    function encodeMarkdownTableImageEmbedsForEditor(source) {
        return transformMarkdownTableEmbeds(source, function (markup) {
            return markup.replace(/</g, "&lt;").replace(/>/g, "&gt;");
        });
    }

    function decodeMarkdownTableImageEmbedsFromEditor(source) {
        return transformMarkdownTableEmbeds(
            source,
            function (markup) {
                var decoded = markup
                    .replace(/^&lt;/i, "<")
                    .replace(/&gt;$/i, ">");
                return isTableImageEmbed(decoded) ? decoded : markup;
            },
            EDITOR_TABLE_IMAGE_EMBED_PATTERN
        );
    }

    function isSafePreviewImageSource(source) {
        return /^(?:https?:\/\/|\/|\.\.?\/)/i.test(String(source || ""));
    }

    function collectMarkdownTableImageSpecs(source) {
        var lines = String(source || "").split(/\r?\n/);
        var htmlTableStarts = [];
        var htmlTablePattern = /<table\b/gi;
        var match;
        while ((match = htmlTablePattern.exec(source || ""))) {
            htmlTableStarts.push(match.index);
        }
        var candidates = [];
        var markdownTableCount = 0;
        var offset = 0;
        var inFence = false;
        for (var index = 0; index < lines.length - 1; index += 1) {
            var line = lines[index];
            if (/^\s*(```|~~~)/.test(line)) {
                inFence = !inFence;
                offset += line.length + 1;
                continue;
            }
            if (!inFence && line.indexOf("|") !== -1 && isMarkdownTableDivider(lines[index + 1])) {
                var rows = [];
                rows.push(splitMarkdownTableCells(lines[index]));
                var rowIndex = index + 2;
                while (rowIndex < lines.length && lines[rowIndex].trim() && lines[rowIndex].indexOf("|") !== -1) {
                    rows.push(splitMarkdownTableCells(lines[rowIndex]));
                    rowIndex += 1;
                }
                var tableImages = [];
                rows.forEach(function (cells, sourceRow) {
                    cells.forEach(function (cell, sourceColumn) {
                        var embedPattern = /<embed\b[^>]*\bembedtype\s*=\s*[\"']image[\"'][^>]*\/?\s*>/gi;
                        var embedMatch;
                        while ((embedMatch = embedPattern.exec(cell))) {
                            tableImages.push({
                                row: sourceRow,
                                column: sourceColumn,
                                id: readEmbedAttribute(embedMatch[0], "id"),
                                format: readEmbedAttribute(embedMatch[0], "format"),
                                src: readEmbedAttribute(embedMatch[0], "src"),
                                alt: readEmbedAttribute(embedMatch[0], "alt"),
                            });
                        }
                    });
                });
                var start = offset;
                var tableIndex =
                    htmlTableStarts.filter(function (position) {
                        return position < start;
                    }).length + markdownTableCount;
                if (tableImages.length) {
                    candidates.push({ tableIndex: tableIndex, images: tableImages });
                }
                markdownTableCount += 1;
                index = rowIndex - 1;
                offset = lines.slice(0, rowIndex).join("\n").length + (rowIndex ? 1 : 0);
                continue;
            }
            offset += line.length + 1;
        }
        return candidates;
    }

    function restoreMarkdownTableImages(root, source, document) {
        var specs = collectMarkdownTableImageSpecs(source);
        var tables = Array.from(root.querySelectorAll("table"));
        specs.forEach(function (spec) {
            var table = tables[spec.tableIndex];
            if (!table) {
                return;
            }
            var rows = Array.from(table.querySelectorAll("tr"));
            spec.images.forEach(function (imageSpec) {
                var row = rows[imageSpec.row];
                var cells = row && Array.from(row.children).filter(function (cell) {
                    return cell.tagName === "TD" || cell.tagName === "TH";
                });
                var cell = cells && cells[imageSpec.column];
                if (
                    !cell ||
                    !IMAGE_ID_PATTERN.test(imageSpec.id) ||
                    !IMAGE_FORMAT_PATTERN.test(imageSpec.format) ||
                    !imageSpec.src ||
                    !isSafePreviewImageSource(imageSpec.src) ||
                    cell.querySelector('[data-blog-inline-image-id="' + imageSpec.id + '"]')
                ) {
                    return;
                }
                var image = document.createElement("img");
                image.src = imageSpec.src;
                image.alt = imageSpec.alt || "";
                image.loading = "lazy";
                image.dataset.blogInlineImageId = imageSpec.id;
                if (imageSpec.format === "fullwidth" || imageSpec.format === "fullwidth_web") {
                    image.className = "richtext-image full-width";
                }
                cell.appendChild(image);
            });
        });
    }

    function updateFormUploadState(form, delta) {
        if (!form) {
            return;
        }
        var sharedCoordinator = window.BlogEditorUploadCoordinator;
        if (
            sharedCoordinator &&
            typeof sharedCoordinator.update === "function"
        ) {
            sharedCoordinator.update(form, delta);
            return;
        }
        var state = formUploadStates.get(form);
        if (!state) {
            state = { count: 0, controls: [] };
            formUploadStates.set(form, state);
        }
        state.count = Math.max(0, state.count + delta);

        if (state.count > 0 && state.controls.length === 0) {
            state.controls = Array.from(
                form.querySelectorAll('button[type="submit"], input[type="submit"]')
            ).map(function (control) {
                var saved = {
                    element: control,
                    disabled: control.disabled,
                    ariaBusy: control.getAttribute("aria-busy"),
                };
                control.disabled = true;
                control.setAttribute("aria-busy", "true");
                return saved;
            });
        } else if (state.count === 0 && state.controls.length > 0) {
            state.controls.forEach(function (saved) {
                if (!saved.element.isConnected) {
                    return;
                }
                saved.element.disabled = saved.disabled;
                if (saved.ariaBusy == null) {
                    saved.element.removeAttribute("aria-busy");
                } else {
                    saved.element.setAttribute("aria-busy", saved.ariaBusy);
                }
            });
            state.controls = [];
        }
    }

    function createUploadIdentity() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID().replace(/-/g, "");
        }
        return (
            Date.now().toString(36) +
            Math.random().toString(36).slice(2) +
            Math.random().toString(36).slice(2)
        );
    }

    function dataUrlToImageFile(dataUrl, identity) {
        var match = /^data:(image\/[A-Za-z0-9.+-]+)(;base64)?,([\s\S]+)$/.exec(
            dataUrl || ""
        );
        if (!match) {
            return null;
        }
        try {
            var bytes;
            if (match[2]) {
                var binary = window.atob(match[3]);
                bytes = new Uint8Array(binary.length);
                for (var index = 0; index < binary.length; index += 1) {
                    bytes[index] = binary.charCodeAt(index);
                }
            } else {
                bytes = new TextEncoder().encode(decodeURIComponent(match[3]));
            }
            var extension = match[1].split("/")[1].replace(/[^A-Za-z0-9]/g, "");
            return new File(
                [bytes],
                "pasted-image-" + identity + "." + (extension || "png"),
                { type: match[1] }
            );
        } catch (error) {
            logError("image-paste:data-url-invalid", error);
            return null;
        }
    }

    function findNamedInput(element, name) {
        var candidates = [];
        if (element && element.matches && element.matches("input, select, textarea, button")) {
            candidates.push(element);
        }
        if (element && element.querySelectorAll) {
            candidates = candidates.concat(
                Array.from(element.querySelectorAll("input, select, textarea, button"))
            );
        }
        return candidates.find(function (candidate) {
            return candidate.name === name;
        });
    }

    function applyAttributes(element, attributes) {
        if (!element || !attributes) {
            return;
        }
        Object.keys(attributes).forEach(function (name) {
            var value = attributes[name];
            if (value === false || value == null) {
                element.removeAttribute(name);
            } else if (value === true) {
                element.setAttribute(name, "");
            } else {
                element.setAttribute(name, String(value));
            }
        });
    }

    function isDarkAdminTheme() {
        var root = document.documentElement;
        var body = document.body;
        return (
            root.dataset.theme === "dark" ||
            body.dataset.theme === "dark" ||
            root.classList.contains("theme-dark") ||
            body.classList.contains("theme-dark") ||
            window.matchMedia("(prefers-color-scheme: dark)").matches
        );
    }

    function clamp(value, minimum, maximum) {
        return Math.min(Math.max(value, minimum), maximum);
    }

    function getAdminFullscreenBounds() {
        var viewportWidth = window.innerWidth || document.documentElement.clientWidth;
        var viewportHeight = window.innerHeight || document.documentElement.clientHeight;
        var main = document.getElementById("main");

        if (!main) {
            return {
                left: 0,
                top: 0,
                width: viewportWidth,
                height: viewportHeight,
            };
        }

        var mainRect = main.getBoundingClientRect();
        var left = clamp(mainRect.left, 0, viewportWidth);
        var right = clamp(mainRect.right, left, viewportWidth);
        var top = clamp(mainRect.top, 0, viewportHeight);
        var header = main.querySelector(".w-slim-header");

        if (header) {
            var headerRect = header.getBoundingClientRect();
            var headerOverlapsMain =
                headerRect.bottom > top &&
                headerRect.top < viewportHeight &&
                headerRect.right > left &&
                headerRect.left < right;

            if (headerOverlapsMain) {
                top = clamp(headerRect.bottom, top, viewportHeight);
            }
        }

        return {
            left: left,
            top: top,
            width: Math.max(0, right - left),
            height: Math.max(0, viewportHeight - top),
        };
    }

    class VditorMarkdownWidget {
        constructor(element, name, parentCapabilities) {
            this.input = findNamedInput(element, name);
            if (!this.input) {
                throw new Error('No Vditor source input found with name "' + name + '"');
            }

            this.name = name;
            this.idForLabel = this.input.id;
            this.parentCapabilities = parentCapabilities || new Map();
            this.root = this.input.closest("[data-vditor-field]") || element;
            this.editorElement = this.root.querySelector("[data-vditor-editor]");
            this.editor = null;
            this.form = this.input.form;
            this.handleSubmit = this.handleSubmit.bind(this);
            this.handlePageLinkPointerDown =
                this.capturePageLinkSelection.bind(this);
            this.handleImagePointerDown =
                this.captureImageSelection.bind(this);
            this.handlePaste = this.handlePaste.bind(this);
            this.pageLinkButton = null;
            this.pageLinkSelection = null;
            this.imageButton = null;
            this.imageSelection = null;
            this.pendingUploadCount = 0;
            this.uploadAbortControllers = new Set();
            this.uploadStatusElement = null;
            this.destroyed = false;
            this.initialValue = this.input.value || "";
            this.initialEditorValue = encodeMarkdownTableImageEmbedsForEditor(
                this.initialValue
            );
            this.initialized = false;
            this.themeObserver = null;
            this.fullscreenObserver = null;
            this.fullscreenResizeObserver = null;
            this.fullscreenFrame = null;
            this.handleFullscreenLayoutChange =
                this.scheduleFullscreenLayout.bind(this);

            log("widget:construct", {
                name: this.name,
                inputId: this.input.id,
                vditorAvailable: typeof window.Vditor === "function",
                value: summarizeValue(this.initialValue),
            });

            if (this.form) {
                this.form.addEventListener("submit", this.handleSubmit, true);
            }

            this.init();
        }

        init() {
            if (!window.Vditor || !this.editorElement) {
                this.root.dataset.vditorReady = "false";
                logError("widget:init:unavailable", new Error("Vditor assets are unavailable"), {
                    name: this.name,
                    vditorAvailable: Boolean(window.Vditor),
                    editorElementAvailable: Boolean(this.editorElement),
                });
                return;
            }

            var cdn = this.input.dataset.vditorCdn || "/static/vendor/vditor";
            var widget = this;

            log("widget:init:start", {
                name: this.name,
                mode: this.input.dataset.vditorMode || "sv",
                cdn: cdn,
            });

            this.editor = new window.Vditor(this.editorElement, {
                cdn: cdn,
                mode: this.input.dataset.vditorMode || "sv",
                lang: this.input.dataset.vditorLocale || "zh_CN",
                height: 480,
                minHeight: 320,
                value: this.initialEditorValue,
                cache: { enable: false },
                toolbar: [
                    "headings",
                    "bold",
                    "italic",
                    "strike",
                    "line",
                    "quote",
                    "list",
                    "ordered-list",
                    "check",
                    "code",
                    "inline-code",
                    "link",
                    {
                        name: "blog-page-link",
                        tip: "站内页面链接",
                        tipPosition: "n",
                        icon: '<svg><use xlink:href="#vditor-icon-link"></use></svg>',
                        click: function (event) {
                            widget.openPageChooser(event);
                        },
                    },
                    {
                        name: "blog-image",
                        tip: "图片",
                        tipPosition: "n",
                        icon: '<svg class="icon icon-image" aria-hidden="true"><use href="#icon-image"></use></svg>',
                        click: function (event) {
                            widget.openImageChooser(event);
                        },
                    },
                    "table",
                    "undo",
                    "redo",
                    "edit-mode",
                    "fullscreen",
                ],
                preview: {
                    transform: function (html) {
                        return widget.transformPreview(html);
                    },
                    markdown: {
                        breaks: true,
                        footnotes: true,
                        codeBlockPreview: true,
                        // 明确开启 $$...$$ 块公式预览；$...$ 行内公式由 Vditor 默认数学解析器处理。
                        mathBlockPreview: true,
                    },
                    theme: {
                        current: isDarkAdminTheme() ? "dark" : "light",
                        path: cdn + "/dist/css/content-theme",
                    },
                },
                theme: isDarkAdminTheme() ? "dark" : "classic",
                input: function (value) {
                    if (!widget.initialized) {
                        return;
                    }
                    widget.syncValue(
                        typeof value === "string" ? value : widget.editor.getValue()
                    );
                },
                after: function () {
                    if (
                        widget.editor &&
                        widget.editor.getValue() !== widget.initialEditorValue
                    ) {
                        widget.editor.setValue(widget.initialEditorValue);
                    }
                    widget.input.value = widget.initialValue;
                    widget.initialized = true;
                    widget.root.dataset.vditorReady = "true";
                    widget.observeAdminTheme();
                    widget.observeFullscreenLayout();
                    widget.bindPageLinkSelection();
                    widget.bindImageSelection();
                    widget.bindPasteHandler();
                    log("widget:init:ready", {
                        name: widget.name,
                        value: summarizeValue(widget.initialValue),
                    });
                },
            });
        }

        observeAdminTheme() {
            if (!window.MutationObserver || this.themeObserver) {
                return;
            }

            var widget = this;
            this.themeObserver = new MutationObserver(function () {
                widget.syncAdminTheme();
                widget.scheduleFullscreenLayout();
            });

            this.themeObserver.observe(document.documentElement, {
                attributes: true,
                attributeFilter: ["class", "data-theme"],
            });
            this.themeObserver.observe(document.body, {
                attributes: true,
                attributeFilter: ["class", "data-theme"],
            });
        }

        observeFullscreenLayout() {
            if (!this.editorElement) {
                return;
            }

            var widget = this;

            if (window.MutationObserver) {
                this.fullscreenObserver = new MutationObserver(function () {
                    widget.scheduleFullscreenLayout();
                });
                this.fullscreenObserver.observe(this.editorElement, {
                    attributes: true,
                    attributeFilter: ["class"],
                });
            }

            window.addEventListener("resize", this.handleFullscreenLayoutChange);

            if (window.ResizeObserver) {
                this.fullscreenResizeObserver = new ResizeObserver(function () {
                    widget.scheduleFullscreenLayout();
                });

                var main = document.getElementById("main");
                var header = main && main.querySelector(".w-slim-header");
                if (main) {
                    this.fullscreenResizeObserver.observe(main);
                }
                if (header) {
                    this.fullscreenResizeObserver.observe(header);
                }
            }

            this.scheduleFullscreenLayout();
        }

        scheduleFullscreenLayout() {
            if (!this.editorElement) {
                return;
            }

            if (!this.editorElement.classList.contains("vditor--fullscreen")) {
                this.clearFullscreenBounds();
                return;
            }

            if (this.fullscreenFrame !== null) {
                return;
            }

            var widget = this;
            var update = function () {
                widget.fullscreenFrame = null;
                widget.updateFullscreenBounds();
            };

            if (window.requestAnimationFrame) {
                this.fullscreenFrame = window.requestAnimationFrame(update);
            } else {
                update();
            }
        }

        updateFullscreenBounds() {
            if (
                !this.editorElement ||
                !this.editorElement.classList.contains("vditor--fullscreen")
            ) {
                this.clearFullscreenBounds();
                return;
            }

            var bounds = getAdminFullscreenBounds();
            this.editorElement.style.setProperty(
                "--blog-vditor-fullscreen-top",
                bounds.top + "px"
            );
            this.editorElement.style.setProperty(
                "--blog-vditor-fullscreen-left",
                bounds.left + "px"
            );
            this.editorElement.style.setProperty(
                "--blog-vditor-fullscreen-width",
                bounds.width + "px"
            );
            this.editorElement.style.setProperty(
                "--blog-vditor-fullscreen-height",
                bounds.height + "px"
            );
        }

        clearFullscreenBounds() {
            if (!this.editorElement) {
                return;
            }

            this.editorElement.style.removeProperty(
                "--blog-vditor-fullscreen-top"
            );
            this.editorElement.style.removeProperty(
                "--blog-vditor-fullscreen-left"
            );
            this.editorElement.style.removeProperty(
                "--blog-vditor-fullscreen-width"
            );
            this.editorElement.style.removeProperty(
                "--blog-vditor-fullscreen-height"
            );
        }

        syncAdminTheme() {
            if (!this.editor || typeof this.editor.setTheme !== "function") {
                return;
            }

            var dark = isDarkAdminTheme();
            this.editor.setTheme(dark ? "dark" : "classic", dark ? "dark" : "light");
        }

        transformPreview(html) {
            var template = document.createElement("template");
            template.innerHTML = html || "";
            // Lute 不解析原生 HTML 表格内的数学标记；转换成 Vditor 自己的
            // language-math 节点后，内置 KaTeX 渲染器会继续处理这些公式。
            template.content.querySelectorAll("table").forEach(function (table) {
                transformTableMath(table, document);
            });
            // 后台预览中的宽表格使用独立滚动容器，避免合并单元格挤压编辑器布局。
            template.content.querySelectorAll("table").forEach(function (table) {
                var parent = table.parentElement;
                if (
                    parent &&
                    parent.classList.contains("blog-markdown-table-scroll")
                ) {
                    return;
                }
                var wrapper = document.createElement("div");
                wrapper.className = "blog-markdown-table-scroll";
                wrapper.setAttribute("role", "region");
                wrapper.setAttribute("tabindex", "0");
                wrapper.setAttribute("aria-label", "可横向滚动的 Markdown 表格");
                table.replaceWith(wrapper);
                wrapper.appendChild(table);
            });
            template.content
                .querySelectorAll('embed[embedtype="image"]')
                .forEach(function (embed) {
                    var imageId = embed.getAttribute("id") || "";
                    var formatName = embed.getAttribute("format") || "";
                    var source = embed.getAttribute("src") || "";
                    if (
                        !IMAGE_ID_PATTERN.test(imageId) ||
                        !IMAGE_FORMAT_PATTERN.test(formatName) ||
                        !source
                    ) {
                        embed.remove();
                        return;
                    }
                    var image = document.createElement("img");
                    image.src = source;
                    image.alt = embed.getAttribute("alt") || "";
                    image.loading = "lazy";
                    image.dataset.blogInlineImageId = imageId;
                    if (
                        formatName === "fullwidth" ||
                        formatName === "fullwidth_web"
                    ) {
                        image.className = "richtext-image full-width";
                    } else if (formatName === "left" || formatName === "right") {
                        image.className = "richtext-image " + formatName;
                    }
                    ["width", "height"].forEach(function (attribute) {
                        var value = embed.getAttribute(attribute) || "";
                        if (/^[1-9][0-9]{0,5}$/.test(value)) {
                            image.setAttribute(attribute, value);
                        }
                    });
                    embed.replaceWith(image);
                });
            restoreMarkdownTableImages(
                template.content,
                this.input.value ||
                    (this.editor && typeof this.editor.getValue === "function"
                        ? this.editor.getValue()
                        : ""),
                document
            );
            return template.innerHTML;
        }

        bindPageLinkSelection() {
            if (!this.editorElement) {
                return;
            }

            var button = this.editorElement.querySelector(
                'button[data-type="blog-page-link"]'
            );
            if (!button || this.pageLinkButton === button) {
                return;
            }

            this.unbindPageLinkSelection();
            this.pageLinkButton = button;
            button.addEventListener(
                "pointerdown",
                this.handlePageLinkPointerDown,
                true
            );
            button.addEventListener(
                "mousedown",
                this.handlePageLinkPointerDown,
                true
            );
        }

        unbindPageLinkSelection() {
            if (!this.pageLinkButton) {
                return;
            }

            this.pageLinkButton.removeEventListener(
                "pointerdown",
                this.handlePageLinkPointerDown,
                true
            );
            this.pageLinkButton.removeEventListener(
                "mousedown",
                this.handlePageLinkPointerDown,
                true
            );
            this.pageLinkButton = null;
        }

        bindImageSelection() {
            if (!this.editorElement) {
                return;
            }
            var button = this.editorElement.querySelector(
                'button[data-type="blog-image"]'
            );
            if (!button || this.imageButton === button) {
                return;
            }
            this.unbindImageSelection();
            this.imageButton = button;
            button.addEventListener(
                "pointerdown",
                this.handleImagePointerDown,
                true
            );
            button.addEventListener(
                "mousedown",
                this.handleImagePointerDown,
                true
            );
        }

        unbindImageSelection() {
            if (!this.imageButton) {
                return;
            }
            this.imageButton.removeEventListener(
                "pointerdown",
                this.handleImagePointerDown,
                true
            );
            this.imageButton.removeEventListener(
                "mousedown",
                this.handleImagePointerDown,
                true
            );
            this.imageButton = null;
        }

        bindPasteHandler() {
            if (this.editorElement) {
                this.editorElement.addEventListener("paste", this.handlePaste, true);
            }
        }

        unbindPasteHandler() {
            if (this.editorElement) {
                this.editorElement.removeEventListener(
                    "paste",
                    this.handlePaste,
                    true
                );
            }
        }

        capturePageLinkSelection() {
            var selection = this.captureEditorSelection();
            if (selection) {
                this.pageLinkSelection = selection;
            }
        }

        captureImageSelection() {
            var selection = this.captureEditorSelection();
            if (selection) {
                this.imageSelection = selection;
            }
        }

        captureEditorSelection() {
            if (
                !this.editorElement ||
                typeof window.getSelection !== "function"
            ) {
                return null;
            }

            var mode =
                this.editor && typeof this.editor.getCurrentMode === "function"
                    ? this.editor.getCurrentMode()
                    : null;
            var modeElement = this.getModeElement(mode);
            if (!modeElement) {
                return null;
            }

            var selection = window.getSelection();
            var range =
                selection && selection.rangeCount > 0
                    ? selection.getRangeAt(0)
                    : null;
            if (!range || !modeElement.contains(range.commonAncestorContainer)) {
                var modeState = this.editor && this.editor.vditor
                    ? this.editor.vditor[mode]
                    : null;
                range = modeState && modeState.range ? modeState.range : null;
            }
            if (!range || !modeElement.contains(range.commonAncestorContainer)) {
                return null;
            }

            var before = range.cloneRange();
            before.selectNodeContents(modeElement);
            before.setEnd(range.startContainer, range.startOffset);

            return {
                mode: mode,
                start: before.toString().length,
                end: before.toString().length + range.toString().length,
                text: range.toString(),
            };
        }

        getModeElement(mode) {
            if (!this.editor || !this.editor.vditor || !mode) {
                return null;
            }

            var modeState = this.editor.vditor[mode];
            return modeState && modeState.element ? modeState.element : null;
        }

        buildRangeFromOffsets(element, start, end) {
            if (!element) {
                return null;
            }

            var range = element.ownerDocument.createRange();
            var walker = element.ownerDocument.createTreeWalker(
                element,
                NodeFilter.SHOW_TEXT
            );
            var textLength = 0;
            var startPoint = null;
            var endPoint = null;
            var node;

            while ((node = walker.nextNode())) {
                var nextLength = textLength + node.data.length;
                if (!startPoint && start <= nextLength) {
                    startPoint = {
                        node: node,
                        offset: Math.max(0, start - textLength),
                    };
                }
                if (!endPoint && end <= nextLength) {
                    endPoint = {
                        node: node,
                        offset: Math.max(0, end - textLength),
                    };
                    break;
                }
                textLength = nextLength;
            }

            if (!startPoint) {
                range.selectNodeContents(element);
                range.collapse(false);
                return range;
            }

            endPoint = endPoint || startPoint;
            range.setStart(startPoint.node, startPoint.offset);
            range.setEnd(endPoint.node, endPoint.offset);
            return range;
        }

        restoreEditorSelection(savedSelection) {
            if (
                !savedSelection ||
                typeof window.getSelection !== "function" ||
                (this.editor &&
                    typeof this.editor.getCurrentMode === "function" &&
                    this.editor.getCurrentMode() !== savedSelection.mode)
            ) {
                return false;
            }

            var modeElement = this.getModeElement(savedSelection.mode);
            var range = this.buildRangeFromOffsets(
                modeElement,
                savedSelection.start,
                savedSelection.end
            );
            var selection = window.getSelection();
            if (!range || !selection) {
                return false;
            }

            try {
                selection.removeAllRanges();
                selection.addRange(range);
                var modeState = this.editor.vditor[savedSelection.mode];
                modeState.range = range.cloneRange();
                return true;
            } catch (error) {
                logError("page-link:selection:restore-failed", error, {
                    name: this.name,
                });
                return false;
            }
        }

        buildPageLinkMarkup(page, linkText) {
            var pageId = page && page.id != null ? String(page.id) : "";
            if (!PAGE_LINK_ID_PATTERN.test(pageId)) {
                return null;
            }

            var title = typeof linkText === "string" ? linkText.trim() : "";
            if (!title && page && typeof page.title === "string") {
                title = page.title.trim();
            }
            if (!title && page && typeof page.adminTitle === "string") {
                title = page.adminTitle.trim();
            }
            if (!title) {
                title = pageId;
            }

            var anchor = document.createElement("a");
            anchor.setAttribute("linktype", "page");
            anchor.setAttribute("id", pageId);
            if (page && typeof page.url === "string" && page.url) {
                anchor.setAttribute("href", page.url);
            }
            anchor.textContent = title;
            return anchor.outerHTML;
        }

        openPageChooser(event) {
            var chooserUrl = this.input.dataset.vditorPageChooserUrl;
            if (!chooserUrl) {
                logError(
                    "page-link:chooser:url-missing",
                    new Error("Wagtail page chooser URL is unavailable"),
                    { name: this.name }
                );
                return;
            }

            if (
                typeof window.ModalWorkflow !== "function" ||
                !window.PAGE_CHOOSER_MODAL_ONLOAD_HANDLERS
            ) {
                logError(
                    "page-link:chooser:unavailable",
                    new Error("Wagtail page chooser assets are unavailable"),
                    { name: this.name }
                );
                return;
            }

            var widget = this;
            var selection = this.pageLinkSelection || this.captureEditorSelection();
            this.pageLinkSelection = null;
            var triggerElement = event && event.currentTarget;

            try {
                window.ModalWorkflow({
                    url: chooserUrl,
                    triggerElement: triggerElement || document.activeElement,
                    onload: window.PAGE_CHOOSER_MODAL_ONLOAD_HANDLERS,
                    responses: {
                        pageChosen: function (page) {
                            widget.insertChosenPageLink(page, selection);
                        },
                    },
                });
                log("page-link:chooser:open", { name: this.name });
            } catch (error) {
                logError("page-link:chooser:open-failed", error, {
                    name: this.name,
                });
            }
        }

        insertChosenPageLink(page, selection) {
            var pageId = page && page.id != null ? String(page.id) : "";
            var markup = this.buildPageLinkMarkup(
                page,
                selection && selection.text
            );
            if (!markup) {
                logError(
                    "page-link:chosen:invalid",
                    new Error("Wagtail page chooser returned an invalid page ID"),
                    { name: this.name, pageId: pageId }
                );
                return;
            }

            if (!this.editor) {
                logError(
                    "page-link:insert:editor-unavailable",
                    new Error("Vditor is unavailable"),
                    { name: this.name, pageId: pageId }
                );
                return;
            }

            try {
                this.editor.focus();
                this.restoreEditorSelection(selection);
                if (
                    typeof this.editor.getCurrentMode === "function" &&
                    this.editor.getCurrentMode() === "sv" &&
                    typeof this.editor.insertMD === "function"
                ) {
                    this.editor.insertMD(markup);
                } else {
                    this.editor.insertValue(markup);
                }
                this.syncValue(this.editor.getValue());
                log("page-link:inserted", {
                    name: this.name,
                    pageId: pageId,
                });
            } catch (error) {
                logError("page-link:insert:failed", error, {
                    name: this.name,
                    pageId: pageId,
                });
            }
        }

        buildImageMarkup(image) {
            var imageId = image && image.id != null ? String(image.id) : "";
            var formatName = image && typeof image.format === "string"
                ? image.format
                : "";
            if (
                !IMAGE_ID_PATTERN.test(imageId) ||
                !IMAGE_FORMAT_PATTERN.test(formatName)
            ) {
                return null;
            }

            var embed = document.createElement("embed");
            embed.setAttribute("embedtype", "image");
            embed.setAttribute("id", imageId);
            embed.setAttribute("format", formatName);
            embed.setAttribute(
                "alt",
                image && typeof image.alt === "string" ? image.alt : ""
            );

            var preview = image && image.preview;
            if (preview && typeof preview.url === "string" && preview.url) {
                embed.setAttribute("src", preview.url);
                ["width", "height"].forEach(function (attribute) {
                    var value = Number(preview[attribute]);
                    if (Number.isInteger(value) && value > 0 && value <= 999999) {
                        embed.setAttribute(attribute, String(value));
                    }
                });
            }
            return embed.outerHTML.replace(/\s*\/?>(?:<\/embed>)?$/, " />");
        }

        openImageChooser(event) {
            var chooserUrl = this.input.dataset.vditorImageChooserUrl;
            if (!chooserUrl) {
                logError(
                    "image:chooser:url-missing",
                    new Error("Wagtail image chooser URL is unavailable"),
                    { name: this.name }
                );
                return;
            }
            if (typeof window.ImageChooserModal !== "function") {
                logError(
                    "image:chooser:unavailable",
                    new Error("Wagtail image chooser assets are unavailable"),
                    { name: this.name }
                );
                return;
            }

            var widget = this;
            var selection = this.imageSelection || this.captureEditorSelection();
            this.imageSelection = null;
            try {
                new window.ImageChooserModal(chooserUrl).open(
                    {
                        triggerElement:
                            (event && event.currentTarget) || document.activeElement,
                    },
                    function (image) {
                        widget.insertChosenImage(image, selection);
                    }
                );
                log("image:chooser:open", { name: this.name });
            } catch (error) {
                logError("image:chooser:open-failed", error, { name: this.name });
            }
        }

        insertChosenImage(image, selection) {
            var markup = this.buildImageMarkup(image);
            if (!markup || !this.editor) {
                logError(
                    "image:chosen:invalid",
                    new Error("Wagtail image chooser returned invalid image data"),
                    { name: this.name, imageId: image && image.id }
                );
                return;
            }
            try {
                this.editor.focus();
                if (selection) {
                    this.restoreEditorSelection({
                        mode: selection.mode,
                        start: selection.end,
                        end: selection.end,
                    });
                }
                this.insertMarkdown(markup);
                log("image:inserted", {
                    name: this.name,
                    imageId: String(image.id),
                    format: image.format,
                });
            } catch (error) {
                logError("image:insert:failed", error, {
                    name: this.name,
                    imageId: image && image.id,
                });
            }
        }

        insertMarkdown(markdown, selection) {
            if (!this.editor) {
                return;
            }
            this.editor.focus();
            if (selection) {
                this.restoreEditorSelection(selection);
            }
            if (typeof this.editor.insertMD === "function") {
                this.editor.insertMD(markdown);
            } else {
                this.editor.insertValue(markdown);
            }
            this.syncValue(this.editor.getValue());
        }

        buildClipboardPayload(clipboardData) {
            if (!clipboardData) {
                return null;
            }
            var files = Array.from(clipboardData.files || []).filter(function (file) {
                return file && /^image\//i.test(file.type || "");
            });
            var html = clipboardData.getData("text/html") || "";
            var plainText = clipboardData.getData("text/plain") || "";
            var entries = [];
            var markdown = plainText;

            if (html) {
                var documentFromClipboard = new DOMParser().parseFromString(
                    html,
                    "text/html"
                );
                Array.from(documentFromClipboard.querySelectorAll("img")).forEach(
                    function (imageNode) {
                        var source = imageNode.getAttribute("src") || "";
                        var identity = createUploadIdentity();
                        var file = null;
                        if (files.length > 0 && (
                            /^data:image\//i.test(source) ||
                            /^(?:file|blob|cid):/i.test(source) ||
                            !source
                        )) {
                            file = files.shift();
                        } else if (/^data:image\//i.test(source)) {
                            file = dataUrlToImageFile(source, identity);
                        }
                        if (!file) {
                            return;
                        }
                        var sentinel = "BLOGVDITORUPLOAD" + identity + "TOKEN";
                        var token = "<!--blog-vditor-upload:" + identity + "-->";
                        entries.push({
                            file: file,
                            alt: imageNode.getAttribute("alt") || "",
                            sentinel: sentinel,
                            token: token,
                        });
                        imageNode.replaceWith(
                            documentFromClipboard.createTextNode(sentinel)
                        );
                    }
                );

                if (this.editor && typeof this.editor.html2md === "function") {
                    markdown = this.editor.html2md(
                        documentFromClipboard.body.innerHTML
                    );
                }
            }

            files.forEach(function (file) {
                var identity = createUploadIdentity();
                var token = "<!--blog-vditor-upload:" + identity + "-->";
                entries.push({
                    file: file,
                    alt: "",
                    sentinel: null,
                    token: token,
                });
                markdown += (markdown ? "\n\n" : "") + token;
            });

            entries.forEach(function (entry) {
                if (entry.sentinel && markdown.indexOf(entry.sentinel) !== -1) {
                    markdown = markdown.split(entry.sentinel).join(entry.token);
                } else if (markdown.indexOf(entry.token) === -1) {
                    markdown += (markdown ? "\n\n" : "") + entry.token;
                }
            });
            return entries.length > 0 ? { markdown: markdown, entries: entries } : null;
        }

        handlePaste(event) {
            var payload;
            try {
                payload = this.buildClipboardPayload(event.clipboardData);
            } catch (error) {
                logError("image-paste:parse-failed", error, { name: this.name });
                return;
            }
            if (!payload) {
                return;
            }

            event.preventDefault();
            event.stopImmediatePropagation();
            var selection = this.captureEditorSelection();
            this.insertMarkdown(payload.markdown, selection);
            log("image-paste:captured", {
                name: this.name,
                imageCount: payload.entries.length,
            });
            this.startUploadBatch(payload.entries);
        }

        ensureUploadStatus() {
            if (this.uploadStatusElement) {
                return this.uploadStatusElement;
            }
            var status = document.createElement("div");
            status.className = "blog-vditor-upload-status";
            status.setAttribute("role", "status");
            status.setAttribute("aria-live", "polite");
            status.hidden = true;
            this.root.appendChild(status);
            this.uploadStatusElement = status;
            return status;
        }

        setUploadStatus(message, isError) {
            var status = this.ensureUploadStatus();
            status.textContent = message || "";
            status.hidden = !message;
            status.classList.toggle("blog-vditor-upload-status--error", Boolean(isError));
        }

        startUploadBatch(entries) {
            if (!entries.length) {
                return;
            }
            var widget = this;
            var nextIndex = 0;
            var failedCount = 0;
            this.pendingUploadCount += entries.length;
            updateFormUploadState(this.form, entries.length);
            this.setUploadStatus(
                "正在上传 " + this.pendingUploadCount + " 张图片…",
                false
            );

            async function worker() {
                while (nextIndex < entries.length && !widget.destroyed) {
                    var entry = entries[nextIndex];
                    nextIndex += 1;
                    try {
                        var image = await widget.uploadClipboardImage(entry);
                        var markup = widget.buildImageMarkup(image);
                        if (!markup) {
                            throw new Error("The upload response is invalid");
                        }
                        widget.replaceUploadToken(entry.token, markup);
                        log("image-paste:upload-complete", {
                            name: widget.name,
                            imageId: String(image.id),
                            fileType: entry.file.type || "",
                            fileSize: entry.file.size,
                        });
                    } catch (error) {
                        failedCount += 1;
                        widget.replaceUploadToken(entry.token, "");
                        logError("image-paste:upload-failed", error, {
                            name: widget.name,
                            fileType: entry.file.type || "",
                            fileSize: entry.file.size,
                        });
                    } finally {
                        widget.pendingUploadCount = Math.max(
                            0,
                            widget.pendingUploadCount - 1
                        );
                        updateFormUploadState(widget.form, -1);
                        if (widget.pendingUploadCount > 0) {
                            widget.setUploadStatus(
                                "正在上传 " + widget.pendingUploadCount + " 张图片…",
                                false
                            );
                        }
                    }
                }
            }

            var workerCount = Math.min(IMAGE_UPLOAD_CONCURRENCY, entries.length);
            var workers = [];
            for (var index = 0; index < workerCount; index += 1) {
                workers.push(worker());
            }
            Promise.all(workers).then(function () {
                if (failedCount > 0) {
                    widget.setUploadStatus(
                        failedCount + " 张图片上传失败，请重新粘贴或使用图片按钮。",
                        true
                    );
                } else if (widget.pendingUploadCount === 0) {
                    widget.setUploadStatus("", false);
                }
            });
        }

        async uploadClipboardImage(entry) {
            var uploadUrl = this.input.dataset.vditorImageUploadUrl;
            if (!uploadUrl) {
                throw new Error("Wagtail image upload URL is unavailable");
            }
            var maxSize = Number(this.input.dataset.vditorMaxImageSize) ||
                10 * 1024 * 1024;
            if (!/^image\//i.test(entry.file.type || "")) {
                throw new Error("The clipboard file is not an image");
            }
            if (entry.file.size > maxSize) {
                throw new Error("The clipboard image exceeds the upload size limit");
            }

            var lastError = null;
            for (var attempt = 0; attempt < 2; attempt += 1) {
                var controller = new AbortController();
                this.uploadAbortControllers.add(controller);
                try {
                    var formData = new FormData();
                    formData.append(
                        "file",
                        entry.file,
                        entry.file.name || "pasted-image"
                    );
                    formData.append("alt", entry.alt || "");
                    formData.append("format", "fullwidth_web");
                    var headers = {};
                    var wagtailConfig = window.wagtailConfig || {};
                    if (wagtailConfig.CSRF_TOKEN) {
                        headers[
                            wagtailConfig.CSRF_HEADER_NAME || "X-CSRFToken"
                        ] = wagtailConfig.CSRF_TOKEN;
                    }
                    var response = await window.fetch(uploadUrl, {
                        method: "POST",
                        body: formData,
                        headers: headers,
                        credentials: "same-origin",
                        signal: controller.signal,
                    });
                    var payload = null;
                    try {
                        payload = await response.json();
                    } catch (parseError) {
                        lastError = parseError;
                    }
                    if (!response.ok) {
                        var responseError = new Error(
                            payload && payload.error && payload.error.code
                                ? payload.error.code
                                : "Image upload failed with status " + response.status
                        );
                        responseError.retryable = response.status >= 500;
                        throw responseError;
                    }
                    if (
                        !payload ||
                        !payload.image ||
                        !IMAGE_ID_PATTERN.test(String(payload.image.id || ""))
                    ) {
                        throw new Error("The image upload response is invalid");
                    }
                    return {
                        id: payload.image.id,
                        format: payload.image.format,
                        alt: payload.image.alt || "",
                        preview: payload.preview,
                    };
                } catch (error) {
                    lastError = error;
                    if (
                        controller.signal.aborted ||
                        this.destroyed ||
                        attempt > 0 ||
                        error.retryable !== true
                    ) {
                        throw error;
                    }
                } finally {
                    this.uploadAbortControllers.delete(controller);
                }
            }
            throw lastError || new Error("Image upload failed");
        }

        replaceUploadToken(token, replacement) {
            if (!this.editor) {
                return false;
            }
            var currentValue = this.editor.getValue();
            if (currentValue.indexOf(token) === -1) {
                log("image-paste:token-removed", { name: this.name });
                return false;
            }
            var nextValue = currentValue.split(token).join(replacement);
            this.editor.setValue(nextValue);
            this.syncValue(nextValue);
            return true;
        }

        syncValue(value) {
            // Wagtail autosave reads this textarea without submitting the form.
            // Keep transient upload tokens inside Vditor only, never in revisions.
            this.input.value = decodeMarkdownTableImageEmbedsFromEditor(value).replace(
                IMAGE_UPLOAD_TOKEN_PATTERN,
                ""
            );
            this.input.dispatchEvent(new Event("input", { bubbles: true }));
        }

        handleSubmit(event) {
            if (this.pendingUploadCount > 0) {
                if (event) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                }
                this.setUploadStatus(
                    "图片仍在上传，请等待上传完成后再保存。",
                    true
                );
                log("widget:submit:blocked", {
                    name: this.name,
                    pendingImages: this.pendingUploadCount,
                });
                return;
            }
            if (this.editor) {
                this.syncValue(this.editor.getValue());
                log("widget:submit:sync", {
                    name: this.name,
                    value: summarizeValue(this.input.value),
                });
            }
        }

        getValue() {
            return this.input.value;
        }

        getState() {
            return this.input.value;
        }

        setState(value) {
            var nextValue = value == null ? "" : String(value);
            this.initialValue = nextValue;
            this.initialEditorValue = encodeMarkdownTableImageEmbedsForEditor(
                nextValue
            );
            this.input.value = nextValue;
            if (
                this.initialized &&
                this.editor &&
                this.editor.getValue() !== this.initialEditorValue
            ) {
                this.editor.setValue(this.initialEditorValue);
            }
            log("widget:set-state", {
                name: this.name,
                initialized: this.initialized,
                value: summarizeValue(nextValue),
            });
        }

        setInvalid(invalid) {
            if (invalid) {
                this.input.setAttribute("aria-invalid", "true");
            } else {
                this.input.removeAttribute("aria-invalid");
            }
        }

        getValueForLabel() {
            return this.getValue();
        }

        getTextLabel(options) {
            var value = String(this.getValue() || "").trim();
            var maxLength = options && options.maxLength;
            if (maxLength && value.length > maxLength) {
                return value.substring(0, maxLength - 1) + "...";
            }
            return value;
        }

        focus() {
            if (this.editor && typeof this.editor.focus === "function") {
                this.editor.focus();
                return;
            }
            this.input.focus();
        }

        setCapabilityOptions(capability, options) {
            var target = this.parentCapabilities.get(capability);
            if (target) {
                Object.assign(target, options);
            }
        }

        destroy() {
            this.destroyed = true;
            if (this.form) {
                this.form.removeEventListener("submit", this.handleSubmit, true);
            }
            if (this.themeObserver) {
                this.themeObserver.disconnect();
                this.themeObserver = null;
            }
            if (this.fullscreenObserver) {
                this.fullscreenObserver.disconnect();
                this.fullscreenObserver = null;
            }
            if (this.fullscreenResizeObserver) {
                this.fullscreenResizeObserver.disconnect();
                this.fullscreenResizeObserver = null;
            }
            if (this.fullscreenFrame !== null && window.cancelAnimationFrame) {
                window.cancelAnimationFrame(this.fullscreenFrame);
                this.fullscreenFrame = null;
            }
            this.unbindPageLinkSelection();
            this.unbindImageSelection();
            this.unbindPasteHandler();
            this.uploadAbortControllers.forEach(function (controller) {
                controller.abort();
            });
            this.uploadAbortControllers.clear();
            if (this.pendingUploadCount > 0) {
                updateFormUploadState(this.form, -this.pendingUploadCount);
                this.pendingUploadCount = 0;
            }
            window.removeEventListener("resize", this.handleFullscreenLayoutChange);
            if (this.editor) {
                this.editor.destroy();
                this.editor = null;
            }
            log("widget:destroy", { name: this.name });
        }
    }

    class VditorMarkdownWidgetAdapter {
        constructor(html) {
            this.html = html;
        }

        render(placeholder, name, id, initialState, parentCapabilities, options) {
            log("adapter:render:start", {
                name: name,
                id: id,
                value: summarizeValue(initialState),
            });

            try {
                var container = document.createElement("div");
                container.innerHTML = this.html
                    .split("__NAME__").join(name)
                    .split("__ID__").join(id)
                    .trim();
                var nodes = Array.from(container.childNodes);
                var elements = nodes.filter(function (node) {
                    return node.nodeType === Node.ELEMENT_NODE;
                });
                placeholder.replaceWith.apply(placeholder, nodes);

                var root = elements.length === 1 ? elements[0] : elements[0];
                if (!root) {
                    throw new Error("Vditor widget template did not produce an element");
                }
                applyAttributes(root, options && options.attributes);

                var widget = new VditorMarkdownWidget(root, name, parentCapabilities);
                widget.setState(initialState);
                log("adapter:render:complete", { name: name, id: id });
                return widget;
            } catch (error) {
                logError("adapter:render:failed", error, { name: name, id: id });
                throw error;
            }
        }

        getByName(name, container) {
            return new VditorMarkdownWidget(container, name);
        }
    }

    if (!window.telepath || typeof window.telepath.register !== "function") {
        logError(
            "telepath:register:failed",
            new Error("window.telepath.register is unavailable"),
            { telepathAvailable: Boolean(window.telepath) }
        );
        return;
    }

    window.telepath.register(
        "blog.widgets.VditorMarkdownWidget",
        VditorMarkdownWidgetAdapter
    );
    log("telepath:registered", {
        registerAvailable: typeof window.telepath.register === "function",
        unpackAvailable: typeof window.telepath.unpack === "function",
        vditorAvailable: typeof window.Vditor === "function",
    });

    window.BlogVditorDebug = {
        version: "2026-08-04.1",
        inspect: function () {
            return Array.from(document.querySelectorAll("[data-vditor-field]")).map(
                function (field) {
                    var input = field.querySelector("[data-vditor-markdown]");
                    return {
                        ready: field.dataset.vditorReady || "pending",
                        inputId: input ? input.id : null,
                        value: summarizeValue(input ? input.value : ""),
                    };
                }
            );
        },
    };
})();
