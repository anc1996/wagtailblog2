(() => {
    'use strict';

    // StreamField 每个 Mermaid 块都会带入本脚本，使用全局标记避免重复注册监听器。
    if (window.__wagtailModernMermaidBridgeInitialized) return;
    window.__wagtailModernMermaidBridgeInitialized = true;

    const EDITOR_SELECTOR = '[data-mermaid-modern-editor]';
    const MESSAGE_PREFIX = 'wagtail-modern-mermaid:';
    const MODERN_EDITOR_URL = '/static/vendor/modern-mermaid/index.html?embed=1';
    const MODERN_EMBED_CSS_URL = '/static/vendor/modern-mermaid/wagtail-embed.css?v=20260814-8';
    const LEGACY_MERMAID_PATH = '/static/vendor/mermaid/mermaid.esm.min.mjs';
    const editorStates = new WeakMap();
    const legacyPromises = new Map();
    const fullscreenEditors = new Set();
    let legacyRenderSequence = 0;

    function getOrigin() {
        return window.location.origin;
    }

    function clamp(value, minimum, maximum) {
        return Math.min(Math.max(value, minimum), maximum);
    }

    function getAdminFullscreenBounds() {
        const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
        const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
        const main = document.getElementById('main');

        if (!main) {
            return { left: 0, top: 0, width: viewportWidth, height: viewportHeight };
        }

        const mainRect = main.getBoundingClientRect();
        const left = clamp(mainRect.left, 0, viewportWidth);
        const right = clamp(mainRect.right, left, viewportWidth);
        let top = clamp(mainRect.top, 0, viewportHeight);
        const header = main.querySelector('.w-slim-header');

        if (header) {
            const headerRect = header.getBoundingClientRect();
            const overlapsMain =
                headerRect.bottom > top &&
                headerRect.top < viewportHeight &&
                headerRect.right > left &&
                headerRect.left < right;

            if (overlapsMain) top = clamp(headerRect.bottom, top, viewportHeight);
        }

        return {
            left,
            top,
            width: Math.max(0, right - left),
            height: Math.max(0, viewportHeight - top),
        };
    }

    function getFields(editor) {
        return {
            code: editor.querySelector('[data-contentpath="code"] textarea'),
            renderer: editor.querySelector('[data-contentpath="renderer"] select, [data-contentpath="renderer"] input'),
            frameHost: editor.querySelector('[data-mermaid-editor-frame-host]'),
            modernMode: editor.querySelector('[data-mermaid-mode="modern"]'),
            legacyMode: editor.querySelector('[data-mermaid-mode="legacy"]'),
            legacyOutput: editor.querySelector('[data-mermaid-legacy-output]'),
            legacyPlaceholder: editor.querySelector('[data-mermaid-legacy-placeholder]'),
            legacyError: editor.querySelector('[data-mermaid-legacy-error]'),
            fallback: editor.querySelector('[data-mermaid-editor-fallback]'),
            status: editor.querySelector('[data-mermaid-renderer-status]'),
            upgrade: editor.querySelector('[data-mermaid-upgrade]'),
            viewControls: editor.querySelector('[data-mermaid-view-controls]'),
            displayToggle: editor.querySelector('[data-mermaid-display-toggle]'),
            paneToggle: editor.querySelector('[data-mermaid-pane-toggle]')
        };
    }

    function rendererName(fields) {
        return fields.renderer?.value || 'legacy-v11-current';
    }

    function setStatus(fields, renderer) {
        const legacy = renderer !== 'modern-v11.12';
        if (fields.status) {
            fields.status.textContent = legacy
                ? '当前内容使用旧版兼容渲染器'
                : '当前内容使用 Modern Mermaid 11.12';
        }
        if (fields.upgrade) fields.upgrade.hidden = !legacy;
    }

    function dispatchFieldChange(field) {
        field.dispatchEvent(new Event('input', { bubbles: true }));
        field.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function sendCode(state) {
        const { frame, code } = state.fields;
        if (!frame?.contentWindow || !state.ready) return;
        frame.contentWindow.postMessage(
            { type: `${MESSAGE_PREFIX}set-code`, code: code?.value || '' },
            getOrigin()
        );
    }

    function syncViewControlLabels(state) {
        const { displayToggle, paneToggle } = state.fields;
        if (displayToggle) {
            displayToggle.textContent = state.fullscreen ? '框架内显示' : '全页面显示';
            displayToggle.setAttribute('aria-pressed', String(state.fullscreen));
        }
        if (paneToggle) {
            const previewOnly = state.paneMode === 'preview';
            paneToggle.textContent = previewOnly ? '源码 + 预览' : '仅预览';
            paneToggle.setAttribute('aria-pressed', String(previewOnly));
        }
    }

    function applyFramePaneMode(state) {
        const frameDocument = state.fields.frame?.contentDocument;
        const main = frameDocument?.querySelector('main[data-wagtail-embed]');
        if (main) main.dataset.wagtailPaneMode = state.paneMode;
    }

    function setPaneMode(state, mode) {
        state.paneMode = mode === 'preview' ? 'preview' : 'split';
        applyFramePaneMode(state);
        syncViewControlLabels(state);
    }

    function getLegacyMermaid() {
        if (!legacyPromises.has(LEGACY_MERMAID_PATH)) {
            legacyPromises.set(LEGACY_MERMAID_PATH, import(LEGACY_MERMAID_PATH).then((module) => module.default));
        }
        return legacyPromises.get(LEGACY_MERMAID_PATH);
    }

    async function renderLegacy(state) {
        const { code, legacyOutput, legacyPlaceholder, legacyError } = state.fields;
        if (!legacyOutput) return;

        const source = code?.value?.trim() || '';
        const version = ++state.legacyRenderVersion;
        legacyOutput.replaceChildren();
        if (legacyError) {
            legacyError.hidden = true;
            legacyError.textContent = '';
        }
        if (legacyPlaceholder) legacyPlaceholder.hidden = Boolean(source);
        if (!source) return;

        try {
            const mermaid = await getLegacyMermaid();
            mermaid.initialize({
                startOnLoad: false,
                securityLevel: 'strict',
                suppressErrorRendering: true,
                theme: 'default',
                flowchart: { htmlLabels: false, useMaxWidth: false },
                sequence: { useMaxWidth: false }
            });
            const result = await mermaid.render(`wagtail-legacy-mermaid-${++legacyRenderSequence}`, source);
            if (version !== state.legacyRenderVersion) return;
            legacyOutput.innerHTML = result.svg;
            result.bindFunctions?.(legacyOutput);
        } catch (error) {
            if (version !== state.legacyRenderVersion) return;
            if (legacyError) {
                legacyError.hidden = false;
                legacyError.textContent = 'Mermaid 语法无法渲染，请检查源代码。';
            }
        }
    }

    function scheduleLegacyRender(state) {
        window.clearTimeout(state.legacyRenderTimer);
        // 输入过程中延迟渲染，避免 Mermaid 在每个按键上重复解析大型图表。
        state.legacyRenderTimer = window.setTimeout(() => renderLegacy(state), 300);
    }

    function isFullscreenToggle(button, canvas) {
        if (!button || !canvas || !canvas.contains(button)) return false;

        const label = `${button.title || ''} ${button.getAttribute('aria-label') || ''}`;
        if (/fullscreen|全屏/i.test(label)) return true;

        return Boolean(
            button.querySelector(
                '.lucide-maximize-2, .lucide-maximize2, .lucide-minimize-2, .lucide-minimize2'
            )
        );
    }

    function getFullscreenButton(frame) {
        const frameDocument = frame?.contentDocument;
        const canvas = frameDocument?.querySelector('[data-wagtail-preview-canvas]');
        if (!canvas) return null;

        return [...canvas.querySelectorAll('button')].find((button) =>
            isFullscreenToggle(button, canvas)
        ) || null;
    }

    function isModernFullscreen(frame) {
        const button = getFullscreenButton(frame);
        if (!button) return false;

        const label = `${button.title || ''} ${button.getAttribute('aria-label') || ''}`;
        return /exit|退出|收起/i.test(label);
    }

    function clearFullscreenBounds(state) {
        state.editor.style.removeProperty('--mermaid-admin-fullscreen-top');
        state.editor.style.removeProperty('--mermaid-admin-fullscreen-left');
        state.editor.style.removeProperty('--mermaid-admin-fullscreen-width');
        state.editor.style.removeProperty('--mermaid-admin-fullscreen-height');
    }

    function updateFullscreenBounds(state) {
        if (!state.fullscreen || !state.editor.isConnected) {
            clearFullscreenBounds(state);
            return;
        }

        const bounds = getAdminFullscreenBounds();
        state.editor.style.setProperty('--mermaid-admin-fullscreen-top', `${bounds.top}px`);
        state.editor.style.setProperty('--mermaid-admin-fullscreen-left', `${bounds.left}px`);
        state.editor.style.setProperty('--mermaid-admin-fullscreen-width', `${bounds.width}px`);
        state.editor.style.setProperty('--mermaid-admin-fullscreen-height', `${bounds.height}px`);
    }

    function scheduleFullscreenBounds(state) {
        if (!state.fullscreen || state.fullscreenFrame !== null) return;

        if (typeof window.requestAnimationFrame !== 'function') {
            updateFullscreenBounds(state);
            return;
        }

        state.fullscreenFrame = window.requestAnimationFrame(() => {
            state.fullscreenFrame = null;
            updateFullscreenBounds(state);
        });
    }

    function stopFullscreenLayoutObserver(state) {
        window.removeEventListener('resize', state.handleFullscreenLayoutChange);
        state.fullscreenResizeObserver?.disconnect();
        state.fullscreenResizeObserver = null;
        if (state.fullscreenFrame !== null) {
            window.cancelAnimationFrame(state.fullscreenFrame);
            state.fullscreenFrame = null;
        }
    }

    function observeFullscreenLayout(state) {
        if (state.fullscreenResizeObserver) return;

        window.addEventListener('resize', state.handleFullscreenLayoutChange);
        if (window.ResizeObserver) {
            state.fullscreenResizeObserver = new ResizeObserver(() => {
                scheduleFullscreenBounds(state);
            });
            const main = document.getElementById('main');
            const header = main?.querySelector('.w-slim-header');
            if (main) state.fullscreenResizeObserver.observe(main);
            if (header) state.fullscreenResizeObserver.observe(header);
        }
    }

    function setModernFullscreen(state, enabled) {
        const fullscreen = Boolean(enabled);
        if (state.fullscreen === fullscreen) return;

        state.fullscreen = fullscreen;
        syncViewControlLabels(state);
        state.editor.classList.toggle('mermaid-admin-editor--modern-fullscreen', fullscreen);
        if (fullscreen) {
            fullscreenEditors.add(state.editor);
            document.body.classList.add('mermaid-admin-fullscreen-active');
            observeFullscreenLayout(state);
            // 首次进入时同步写入边界，避免后台窗口暂停动画帧导致定位延迟。
            updateFullscreenBounds(state);
            scheduleFullscreenBounds(state);
            window.setTimeout(() => state.fields.frame?.focus(), 0);
            return;
        }

        fullscreenEditors.delete(state.editor);
        if (!fullscreenEditors.size) {
            document.body.classList.remove('mermaid-admin-fullscreen-active');
        }
        stopFullscreenLayoutObserver(state);
        clearFullscreenBounds(state);
    }

    function bindEmbedFullscreenControls(state, frame) {
        const frameDocument = frame.contentDocument;
        if (!frameDocument || frame.dataset.mermaidFullscreenBridgeBound) return;

        frame.dataset.mermaidFullscreenBridgeBound = 'true';
        getFullscreenButton(frame)?.setAttribute('data-wagtail-fullscreen-toggle', '');

        frameDocument.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape' || !state.fullscreen) return;

            event.preventDefault();
            event.stopPropagation();
            setModernFullscreen(state, false);
        }, true);
    }

    function scheduleEmbedLayout(state, frame) {
        window.clearTimeout(state.embedLayoutTimer);
        let attempts = 0;
        const applyLayout = () => {
            if (state.fields.frame !== frame) return;
            if (markEmbedLayout(state, frame) || attempts >= 40) return;

            attempts += 1;
            // React 在 iframe load 后才挂载面板，重试避免慢设备错过首次布局。
            state.embedLayoutTimer = window.setTimeout(applyLayout, 50);
        };
        applyLayout();
    }

    function markEmbedLayout(state, frame) {
        try {
            const frameDocument = frame.contentDocument;
            const main = frameDocument?.querySelector('main');
            const panes = main ? [...main.children] : [];
            if (!frameDocument || !main || panes.length < 3) return false;

            const editorPane = panes[0];
            const previewPane = panes[panes.length - 1];
            const toolbar = previewPane.firstElementChild;
            const canvas = previewPane.children[1];
            if (!toolbar || !canvas) return false;

            frameDocument.documentElement.dataset.wagtailEmbed = 'true';
            main.dataset.wagtailEmbed = 'true';
            editorPane.dataset.wagtailPane = 'editor';
            previewPane.dataset.wagtailPane = 'preview';
            toolbar.dataset.wagtailPreviewToolbar = 'true';
            canvas.dataset.wagtailPreviewCanvas = 'true';

            if (!frameDocument.querySelector('[data-wagtail-embed-css]')) {
                const link = frameDocument.createElement('link');
                link.rel = 'stylesheet';
                link.href = MODERN_EMBED_CSS_URL;
                link.dataset.wagtailEmbedCss = 'true';
                frameDocument.head.appendChild(link);
            }
            bindEmbedFullscreenControls(state, frame);
            applyFramePaneMode(state);
            return true;
        } catch (error) {
            // iframe 结构变化或浏览器安全策略异常时，保留 Modern Mermaid 原始功能。
            return false;
        }
    }

    function destroyModernFrame(state) {
        setModernFullscreen(state, false);
        window.clearTimeout(state.embedLayoutTimer);
        state.embedLayoutTimer = null;
        window.clearTimeout(state.initialSyncTimer);
        state.initialSyncTimer = null;
        state.ready = false;
        state.awaitingInitialSync = false;
        state.fields.frame?.remove();
        state.fields.frame = null;
        if (state.fields.fallback) state.fields.fallback.hidden = true;
    }

    function mountModernFrame(state) {
        const { frameHost } = state.fields;
        if (!frameHost || state.fields.frame) return;

        const frame = document.createElement('iframe');
        frame.className = 'mermaid-admin-editor__iframe';
        frame.dataset.mermaidEditorFrame = '';
        frame.title = 'Modern Mermaid 图表编辑器';
        frame.loading = 'eager';
        frame.referrerPolicy = 'no-referrer';
        frame.setAttribute('sandbox', 'allow-scripts allow-downloads allow-same-origin');
        frame.addEventListener('load', () => {
            state.failed = false;
            scheduleEmbedLayout(state, frame);
            if (state.fields.fallback) state.fields.fallback.hidden = true;
            sendCode(state);
        });
        frame.addEventListener('error', () => {
            state.failed = true;
            if (state.fields.fallback) state.fields.fallback.hidden = false;
        });
        state.fields.frame = frame;
        frameHost.appendChild(frame);
        frame.src = MODERN_EDITOR_URL;
    }

    function setMode(state, renderer) {
        const { fields } = state;
        const modern = renderer === 'modern-v11.12';
        if (fields.modernMode) fields.modernMode.hidden = !modern;
        if (fields.legacyMode) {
            fields.legacyMode.hidden = modern;
            fields.legacyMode.inert = modern;
        }
        setStatus(fields, renderer);

        if (modern) {
            if (fields.viewControls) fields.viewControls.hidden = false;
            window.clearTimeout(state.legacyRenderTimer);
            state.legacyRenderTimer = null;
            mountModernFrame(state);
            sendCode(state);
        } else {
            if (fields.viewControls) fields.viewControls.hidden = true;
            setModernFullscreen(state, false);
            setPaneMode(state, 'split');
            destroyModernFrame(state);
            renderLegacy(state);
        }
    }

    function bindEditor(editor) {
        if (editorStates.has(editor)) return;
        const fields = getFields(editor);
        if (!fields.code || !fields.frameHost) return;

        const state = {
            editor,
            fields,
            frame: null,
            ready: false,
            failed: false,
            legacyRenderVersion: 0,
            legacyRenderTimer: null,
            initialSyncTimer: null,
            initialSyncDeadline: 0,
            embedLayoutTimer: null,
            fullscreen: false,
            paneMode: 'split',
            fullscreenFrame: null,
            fullscreenResizeObserver: null,
            handleFullscreenLayoutChange: null,
            // iframe 首次挂载会回传内置示例；先等父页面代码确认，避免覆盖已保存正文。
            awaitingInitialSync: false
        };
        state.handleFullscreenLayoutChange = () => scheduleFullscreenBounds(state);
        syncViewControlLabels(state);
        editorStates.set(editor, state);
        setMode(state, rendererName(fields));

        fields.code.addEventListener('input', () => {
            if (rendererName(fields) === 'modern-v11.12') sendCode(state);
            else scheduleLegacyRender(state);
        });
        fields.renderer?.addEventListener('change', () => setMode(state, rendererName(fields)));
        fields.displayToggle?.addEventListener('click', () => {
            setModernFullscreen(state, !state.fullscreen);
        });
        fields.paneToggle?.addEventListener('click', () => {
            setPaneMode(state, state.paneMode === 'preview' ? 'split' : 'preview');
        });

        fields.upgrade?.addEventListener('click', () => {
            if (!fields.renderer) return;
            fields.renderer.value = 'modern-v11.12';
            dispatchFieldChange(fields.renderer);
        });
    }

    function receiveMessage(event) {
        if (event.origin !== getOrigin()) return;
        const frame = event.source;
        document.querySelectorAll(EDITOR_SELECTOR).forEach((editor) => {
            const state = editorStates.get(editor);
            if (!state || !state.fields.frame || state.fields.frame.contentWindow !== frame) return;

            const message = event.data || {};
            if (message.type === `${MESSAGE_PREFIX}ready`) {
                state.ready = true;
                state.awaitingInitialSync = true;
                state.initialSyncDeadline = Date.now() + 1200;
                window.clearTimeout(state.initialSyncTimer);
                state.initialSyncTimer = window.setTimeout(() => {
                    state.awaitingInitialSync = false;
                }, 1200);
                sendCode(state);
                return;
            }
            if (message.type !== `${MESSAGE_PREFIX}code-change` || typeof message.code !== 'string') return;
            if (state.awaitingInitialSync) {
                if (state.fields.code.value === message.code) {
                    state.awaitingInitialSync = false;
                    window.clearTimeout(state.initialSyncTimer);
                } else if (Date.now() < state.initialSyncDeadline) {
                    return;
                } else {
                    state.awaitingInitialSync = false;
                }
            }
            if (state.fields.code.value === message.code) return;
            state.fields.code.value = message.code;
            dispatchFieldChange(state.fields.code);
        });
    }

    function initialize() {
        document.querySelectorAll(EDITOR_SELECTOR).forEach(bindEditor);
        window.addEventListener('message', receiveMessage);

        const observer = new MutationObserver(() => {
            document.querySelectorAll(EDITOR_SELECTOR).forEach(bindEditor);
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
