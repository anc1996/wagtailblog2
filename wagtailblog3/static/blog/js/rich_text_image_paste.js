(function () {
    "use strict";

    var LOG_PREFIX = "[BlogRichText]";
    var IMAGE_ID_PATTERN = /^[1-9][0-9]{0,18}$/;
    var BODY_VALUE_NAME_PATTERN = /^body-([0-9]+)-value$/;
    var script = document.currentScript;
    var uploadUrl = script ? script.dataset.uploadUrl || "" : "";
    var configuredMaxSize = script
        ? Number(script.dataset.maxImageSize)
        : 0;
    var maxImageSize = configuredMaxSize > 0
        ? configuredMaxSize
        : 10 * 1024 * 1024;

    function log(event, details) {
        console.info(LOG_PREFIX, event, details || {});
    }

    function logError(event, error, details) {
        console.error(LOG_PREFIX, event, details || {}, error);
    }

    function createUploadCoordinator() {
        var states = new WeakMap();

        function restoreControls(state) {
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

        return {
            update: function (form, delta) {
                if (!form || !delta) {
                    return;
                }
                var state = states.get(form);
                if (!state) {
                    state = {
                        count: 0,
                        controls: [],
                        handleSubmit: function (event) {
                            if (state.count < 1) {
                                return;
                            }
                            event.preventDefault();
                            event.stopImmediatePropagation();
                            console.warn(LOG_PREFIX, "form:submit-blocked", {
                                pendingUploads: state.count,
                            });
                        },
                    };
                    states.set(form, state);
                }

                var previousCount = state.count;
                state.count = Math.max(0, state.count + delta);
                if (previousCount === 0 && state.count > 0) {
                    state.controls = Array.from(
                        form.querySelectorAll(
                            'button[type="submit"], input[type="submit"]'
                        )
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
                    form.addEventListener("submit", state.handleSubmit, true);
                } else if (previousCount > 0 && state.count === 0) {
                    restoreControls(state);
                    form.removeEventListener("submit", state.handleSubmit, true);
                }
            },
            pending: function (form) {
                var state = form ? states.get(form) : null;
                return state ? state.count : 0;
            },
        };
    }

    var uploadCoordinator = window.BlogEditorUploadCoordinator;
    if (!uploadCoordinator || typeof uploadCoordinator.update !== "function") {
        uploadCoordinator = createUploadCoordinator();
        window.BlogEditorUploadCoordinator = uploadCoordinator;
    }

    function dataUrlToImageFile(dataUrl, index) {
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
                for (var offset = 0; offset < binary.length; offset += 1) {
                    bytes[offset] = binary.charCodeAt(offset);
                }
            } else {
                bytes = new TextEncoder().encode(decodeURIComponent(match[3]));
            }
            var extension = match[1]
                .split("/")[1]
                .replace(/[^A-Za-z0-9]/g, "");
            return new File(
                [bytes],
                "pasted-image-" + String(index + 1) + "." + (extension || "png"),
                { type: match[1] }
            );
        } catch (error) {
            logError("image-paste:data-url-invalid", error);
            return null;
        }
    }

    function parseClipboardHtml(html) {
        var template = document.createElement("template");
        template.innerHTML = html || "";
        var dataUrls = [];
        template.content.querySelectorAll("img").forEach(function (image) {
            var source = image.getAttribute("src") || "";
            if (/^data:image\//i.test(source)) {
                dataUrls.push(source);
            }
            image.remove();
        });
        template.content.querySelectorAll("script, style").forEach(function (node) {
            node.remove();
        });
        return {
            dataUrls: dataUrls,
            hasText: Boolean((template.content.textContent || "").trim()),
        };
    }

    function fileSignature(file) {
        return [
            file.name || "",
            file.type || "",
            file.size || 0,
            file.lastModified || 0,
        ].join("|");
    }

    function buildClipboardImages(clipboardData) {
        if (!clipboardData) {
            return null;
        }

        var files = [];
        var signatures = new Set();
        var hasNonImageFile = false;

        Array.from(clipboardData.items || []).forEach(function (item) {
            if (item.kind !== "file") {
                return;
            }
            var file = item.getAsFile();
            if (!file) {
                return;
            }
            if (!/^image\//i.test(file.type || "")) {
                hasNonImageFile = true;
                return;
            }
            var signature = fileSignature(file);
            if (!signatures.has(signature)) {
                signatures.add(signature);
                files.push(file);
            }
        });

        Array.from(clipboardData.files || []).forEach(function (file) {
            if (!/^image\//i.test(file.type || "")) {
                hasNonImageFile = true;
                return;
            }
            var signature = fileSignature(file);
            if (!signatures.has(signature)) {
                signatures.add(signature);
                files.push(file);
            }
        });

        if (hasNonImageFile) {
            return null;
        }

        var html = clipboardData.getData("text/html") || "";
        var plainText = clipboardData.getData("text/plain") || "";
        var parsedHtml = html ? parseClipboardHtml(html) : null;

        // Mixed text/image clipboard fragments stay on Draftail's native path so
        // their rich-text structure is not silently discarded.
        if ((parsedHtml && parsedHtml.hasText) || (!html && plainText.trim())) {
            return null;
        }

        if (files.length === 0 && parsedHtml) {
            parsedHtml.dataUrls.forEach(function (dataUrl, index) {
                var file = dataUrlToImageFile(dataUrl, index);
                if (file) {
                    files.push(file);
                }
            });
        }

        if (
            files.length === 0 &&
            /^data:image\/[A-Za-z0-9.+-]+(?:;base64)?,/i.test(plainText.trim())
        ) {
            var plainFile = dataUrlToImageFile(plainText.trim(), 0);
            if (plainFile) {
                files.push(plainFile);
            }
        }

        return files.length > 0 ? files : null;
    }

    function findBodyRichTextInput(draftailRoot) {
        var node = draftailRoot;
        while (node && node !== document.body) {
            var candidates = Array.from(
                node.querySelectorAll('[name^="body-"][name$="-value"]')
            ).filter(function (candidate) {
                return BODY_VALUE_NAME_PATTERN.test(candidate.name || "") &&
                    candidate.draftailEditor;
            });
            if (candidates.length === 1) {
                var input = candidates[0];
                var match = BODY_VALUE_NAME_PATTERN.exec(input.name);
                var form = input.form;
                var typeField = form
                    ? form.elements.namedItem("body-" + match[1] + "-type")
                    : null;
                var deletedField = form
                    ? form.elements.namedItem("body-" + match[1] + "-deleted")
                    : null;
                if (
                    typeField &&
                    typeField.value === "rich_text" &&
                    !(deletedField && deletedField.value)
                ) {
                    return input;
                }
                return null;
            }
            if (candidates.length > 1) {
                return null;
            }
            node = node.parentElement;
        }
        return null;
    }

    function getEditorContext(eventTarget) {
        if (!eventTarget || !eventTarget.closest) {
            return null;
        }
        var editable = eventTarget.closest('[contenteditable="true"]');
        var draftailRoot = editable
            ? editable.closest(".Draftail-Editor")
            : null;
        if (!draftailRoot) {
            return null;
        }
        var input = findBodyRichTextInput(draftailRoot);
        if (
            !input ||
            !input.draftailEditor ||
            typeof input.draftailEditor.getEditorState !== "function" ||
            typeof input.draftailEditor.onChange !== "function"
        ) {
            return null;
        }
        return {
            input: input,
            editor: input.draftailEditor,
            root: draftailRoot,
            field: draftailRoot.closest(".w-field") || draftailRoot.parentElement,
            form: input.form,
        };
    }

    function ensureStatus(context) {
        var container = context.field || context.root;
        var status = container.querySelector("[data-blog-rich-text-upload-status]");
        if (!status) {
            status = document.createElement("div");
            status.dataset.blogRichTextUploadStatus = "true";
            status.className = "w-help-text blog-rich-text-upload-status";
            status.setAttribute("role", "status");
            status.setAttribute("aria-live", "polite");
            status.hidden = true;
            container.appendChild(status);
        }
        return status;
    }

    function setStatus(context, message, isError) {
        var status = ensureStatus(context);
        status.textContent = message || "";
        status.hidden = !message;
        status.classList.toggle(
            "blog-rich-text-upload-status--error",
            Boolean(isError)
        );
    }

    function lockEditor(context) {
        var blockedEvents = [
            "beforeinput",
            "compositionstart",
            "cut",
            "drop",
            "keydown",
            "mousedown",
            "paste",
            "pointerdown",
        ];
        var blocker = function (event) {
            event.preventDefault();
            event.stopImmediatePropagation();
        };
        blockedEvents.forEach(function (eventName) {
            context.root.addEventListener(eventName, blocker, true);
        });
        context.root.dataset.blogRichTextUploading = "true";
        context.root.setAttribute("aria-busy", "true");
        context.root.classList.add("blog-rich-text-uploading");

        return function () {
            blockedEvents.forEach(function (eventName) {
                context.root.removeEventListener(eventName, blocker, true);
            });
            delete context.root.dataset.blogRichTextUploading;
            context.root.removeAttribute("aria-busy");
            context.root.classList.remove("blog-rich-text-uploading");
        };
    }

    async function uploadImage(file) {
        if (!/^image\//i.test(file.type || "")) {
            throw new Error("clipboard_file_is_not_an_image");
        }
        if (file.size > maxImageSize) {
            throw new Error("clipboard_image_too_large");
        }

        var lastError = null;
        for (var attempt = 0; attempt < 2; attempt += 1) {
            try {
                var formData = new FormData();
                formData.append("file", file, file.name || "pasted-image");
                formData.append("alt", "");
                formData.append("format", "left");
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
                            : "image_upload_http_" + response.status
                    );
                    responseError.retryable = response.status >= 500;
                    throw responseError;
                }
                if (
                    !payload ||
                    !payload.image ||
                    !IMAGE_ID_PATTERN.test(String(payload.image.id || "")) ||
                    !payload.preview ||
                    !payload.preview.url
                ) {
                    throw new Error("invalid_image_upload_response");
                }
                return {
                    id: payload.image.id,
                    src: payload.preview.url,
                    alt: "",
                    format: "left",
                };
            } catch (error) {
                lastError = error;
                if (attempt > 0 || error.retryable !== true) {
                    throw error;
                }
            }
        }
        throw lastError || new Error("image_upload_failed");
    }

    function insertImages(context, originalSelection, images) {
        var DraftJS = window.DraftJS;
        var nextState = context.editor.getEditorState();
        if (originalSelection) {
            nextState = DraftJS.EditorState.forceSelection(
                nextState,
                originalSelection
            );
        }

        images.forEach(function (image) {
            var contentWithEntity = nextState.getCurrentContent().createEntity(
                "IMAGE",
                "IMMUTABLE",
                image
            );
            var entityKey = contentWithEntity.getLastCreatedEntityKey();
            nextState = DraftJS.EditorState.set(nextState, {
                currentContent: contentWithEntity,
            });
            nextState = DraftJS.AtomicBlockUtils.insertAtomicBlock(
                nextState,
                entityKey,
                " "
            );
        });
        context.editor.onChange(nextState);
    }

    async function startUploadBatch(context, files) {
        var editorState = context.editor.getEditorState();
        var originalSelection = editorState.getSelection();
        var unlockEditor = lockEditor(context);
        uploadCoordinator.update(context.form, files.length);
        setStatus(context, "正在上传 " + files.length + " 张图片…", false);
        log("image-paste:captured", {
            field: context.input.name,
            imageCount: files.length,
        });

        try {
            var results = await Promise.all(
                files.map(async function (file) {
                    try {
                        var image = await uploadImage(file);
                        log("image-paste:upload-complete", {
                            field: context.input.name,
                            imageId: String(image.id),
                            fileType: file.type || "",
                            fileSize: file.size,
                        });
                        return { image: image, error: null };
                    } catch (error) {
                        logError("image-paste:upload-failed", error, {
                            field: context.input.name,
                            fileType: file.type || "",
                            fileSize: file.size,
                        });
                        return { image: null, error: error };
                    }
                })
            );
            var images = results
                .filter(function (result) {
                    return Boolean(result.image);
                })
                .map(function (result) {
                    return result.image;
                });
            var failedCount = results.length - images.length;

            if (
                images.length > 0 &&
                context.input.isConnected &&
                context.input.draftailEditor === context.editor
            ) {
                insertImages(context, originalSelection, images);
            }

            if (failedCount > 0) {
                setStatus(
                    context,
                    failedCount + " 张图片上传失败，请重新粘贴或使用图片按钮。",
                    true
                );
            } else {
                setStatus(context, "", false);
            }
        } catch (error) {
            logError("image-paste:batch-failed", error, {
                field: context.input.name,
                imageCount: files.length,
            });
            setStatus(context, "图片上传失败，请重新粘贴或使用图片按钮。", true);
        } finally {
            uploadCoordinator.update(context.form, -files.length);
            unlockEditor();
        }
    }

    function handlePaste(event) {
        var context = getEditorContext(event.target);
        if (!context) {
            return;
        }
        if (context.root.dataset.blogRichTextUploading === "true") {
            event.preventDefault();
            event.stopImmediatePropagation();
            return;
        }
        if (
            !uploadUrl ||
            !window.fetch ||
            !window.DraftJS ||
            !window.DraftJS.EditorState ||
            !window.DraftJS.AtomicBlockUtils
        ) {
            logError(
                "image-paste:unavailable",
                new Error("Draftail image upload dependencies are unavailable"),
                { field: context.input.name }
            );
            return;
        }

        var files;
        try {
            files = buildClipboardImages(event.clipboardData);
        } catch (error) {
            logError("image-paste:parse-failed", error, {
                field: context.input.name,
            });
            return;
        }
        if (!files) {
            return;
        }

        event.preventDefault();
        event.stopImmediatePropagation();
        startUploadBatch(context, files);
    }

    document.addEventListener("paste", handlePaste, true);
    log("ready", {
        uploadConfigured: Boolean(uploadUrl),
        maxImageSize: maxImageSize,
    });
})();
