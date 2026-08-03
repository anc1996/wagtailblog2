(function () {
    "use strict";

    var LOG_PREFIX = "[BlogVditor]";
    var PAGE_LINK_ID_PATTERN = /^[1-9][0-9]{0,18}$/;

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
            this.pageLinkButton = null;
            this.pageLinkSelection = null;
            this.initialValue = this.input.value || "";
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
                this.form.addEventListener("submit", this.handleSubmit);
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
                value: this.initialValue,
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
                    "table",
                    "undo",
                    "redo",
                    "edit-mode",
                    "fullscreen",
                ],
                preview: {
                    markdown: {
                        breaks: true,
                        footnotes: true,
                        codeBlockPreview: true,
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
                        widget.editor.getValue() !== widget.initialValue
                    ) {
                        widget.editor.setValue(widget.initialValue);
                    }
                    widget.input.value = widget.initialValue;
                    widget.initialized = true;
                    widget.root.dataset.vditorReady = "true";
                    widget.observeAdminTheme();
                    widget.observeFullscreenLayout();
                    widget.bindPageLinkSelection();
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

        capturePageLinkSelection() {
            var selection = this.captureEditorSelection();
            if (selection) {
                this.pageLinkSelection = selection;
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

        syncValue(value) {
            this.input.value = value || "";
            this.input.dispatchEvent(new Event("input", { bubbles: true }));
        }

        handleSubmit() {
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
            this.input.value = nextValue;
            if (this.initialized && this.editor && this.editor.getValue() !== nextValue) {
                this.editor.setValue(nextValue);
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
            if (this.form) {
                this.form.removeEventListener("submit", this.handleSubmit);
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
        version: "2026-08-03.2",
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
