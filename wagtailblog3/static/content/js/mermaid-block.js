(() => {
    'use strict';

    const BLOCK_SELECTOR = '[data-mermaid-block]';
    const MIN_SCALE = 0.4;
    const MAX_SCALE = 3;
    const ZOOM_STEP = 0.2;
    const DIALOG_MARGIN = 16;
    const DIALOG_MIN_WIDTH = 520;
    const DIALOG_MIN_HEIGHT = 360;
    const DIALOG_MOBILE_QUERY = '(max-width: 767px)';
    const DIALOG_RESIZE_EDGES = ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw'];

    const states = new WeakMap();
    let mermaidPromise = null;
    let renderQueue = Promise.resolve();
    let renderSequence = 0;

    const palettes = {
        light: {
            background: '#fffdfb',
            primaryColor: '#f4e8dd',
            primaryTextColor: '#292724',
            primaryBorderColor: '#a76035',
            secondaryColor: '#e7f0ed',
            secondaryTextColor: '#292724',
            secondaryBorderColor: '#4e756a',
            tertiaryColor: '#f7f3ed',
            tertiaryTextColor: '#292724',
            tertiaryBorderColor: '#b7aaa0',
            lineColor: '#765f50',
            textColor: '#292724',
            mainBkg: '#f4e8dd',
            nodeBorder: '#a76035',
            clusterBkg: '#f7f3ed',
            clusterBorder: '#b7aaa0',
            titleColor: '#292724',
            edgeLabelBackground: '#fffdfb',
            actorBkg: '#f4e8dd',
            actorBorder: '#a76035',
            actorTextColor: '#292724',
            actorLineColor: '#8a7668',
            signalColor: '#5f4d42',
            signalTextColor: '#292724',
            labelBoxBkgColor: '#fffdfb',
            labelBoxBorderColor: '#b7aaa0',
            labelTextColor: '#292724',
            loopTextColor: '#292724',
            noteBkgColor: '#fff4cd',
            noteBorderColor: '#bb8b2f',
            noteTextColor: '#392f20',
            activationBkgColor: '#e7f0ed',
            activationBorderColor: '#4e756a',
            fontFamily: 'Lato, "Noto Sans SC", "Microsoft YaHei", sans-serif'
        },
        dark: {
            background: '#1f1d1b',
            primaryColor: '#38312c',
            primaryTextColor: '#f4eee8',
            primaryBorderColor: '#c9895c',
            secondaryColor: '#263834',
            secondaryTextColor: '#f2eee9',
            secondaryBorderColor: '#78a89a',
            tertiaryColor: '#2b2926',
            tertiaryTextColor: '#f4eee8',
            tertiaryBorderColor: '#81746a',
            lineColor: '#d4ad8d',
            textColor: '#f4eee8',
            mainBkg: '#38312c',
            nodeBorder: '#c9895c',
            clusterBkg: '#292724',
            clusterBorder: '#81746a',
            titleColor: '#fffaf5',
            edgeLabelBackground: '#262320',
            actorBkg: '#38312c',
            actorBorder: '#c9895c',
            actorTextColor: '#f4eee8',
            actorLineColor: '#a99a8e',
            signalColor: '#e4c6ad',
            signalTextColor: '#f4eee8',
            labelBoxBkgColor: '#262320',
            labelBoxBorderColor: '#81746a',
            labelTextColor: '#f4eee8',
            loopTextColor: '#f4eee8',
            noteBkgColor: '#4b4127',
            noteBorderColor: '#d4aa52',
            noteTextColor: '#fff3c4',
            activationBkgColor: '#263834',
            activationBorderColor: '#78a89a',
            fontFamily: 'Lato, "Noto Sans SC", "Microsoft YaHei", sans-serif'
        }
    };

    function preferredTheme() {
        const siteTheme = document.documentElement.dataset.theme;
        if (siteTheme === 'dark' || siteTheme === 'light') return siteTheme;
        return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function mermaidConfig(theme) {
        return {
            startOnLoad: false,
            securityLevel: 'strict',
            suppressErrorRendering: true,
            theme: 'base',
            themeVariables: palettes[theme],
            maxTextSize: 50000,
            deterministicIds: true,
            deterministicIDSeed: `wagtailblog-${theme}`,
            flowchart: { htmlLabels: false, useMaxWidth: false },
            sequence: { useMaxWidth: false },
            gantt: { useMaxWidth: false }
        };
    }

    function getMermaid() {
        if (!mermaidPromise) {
            mermaidPromise = import('/static/vendor/mermaid/mermaid.esm.min.mjs')
                .then((module) => module.default);
        }
        return mermaidPromise;
    }

    function enqueueRender(task) {
        const result = renderQueue.then(task, task);
        renderQueue = result.catch(() => undefined);
        return result;
    }

    function getState(block) {
        let state = states.get(block);
        if (!state) {
            state = {
                scale: 1,
                fitScale: 1,
                tx: 0,
                ty: 0,
                pointers: new Map(),
                renderVersion: 0,
                userAdjusted: false,
                dragging: false,
                didDrag: false
            };
            states.set(block, state);
        }
        return state;
    }

    function sourceFor(block) {
        return block.querySelector('.mermaid-raw-source')?.value.trim() || '';
    }

    function currentTheme(block) {
        return block.classList.contains('dark-theme') ? 'dark' : 'light';
    }

    function setRenderState(block, status) {
        block.dataset.mermaidState = status;
        const loading = block.querySelector('[data-mermaid-status]');
        const error = block.querySelector('[data-mermaid-error]');

        if (loading) loading.hidden = status !== 'loading';
        if (error) error.hidden = status !== 'error';
    }

    function diagramDimensions(viewport) {
        const svg = viewport.querySelector('svg');
        if (!svg) return null;

        const viewBox = svg.viewBox?.baseVal;
        const width = viewBox?.width || Number.parseFloat(svg.getAttribute('width')) || 800;
        const height = viewBox?.height || Number.parseFloat(svg.getAttribute('height')) || 480;
        return { svg, width, height };
    }

    function clampScale(value) {
        return Math.min(Math.max(value, MIN_SCALE), MAX_SCALE);
    }

    function clampPan(viewport, state) {
        const dimensions = diagramDimensions(viewport);
        if (!dimensions) return;

        const overflowX = Math.max((dimensions.width * state.scale - viewport.clientWidth) / 2, 0);
        const overflowY = Math.max((dimensions.height * state.scale - viewport.clientHeight) / 2, 0);
        const allowance = 72;

        state.tx = Math.min(Math.max(state.tx, -overflowX - allowance), overflowX + allowance);
        state.ty = Math.min(Math.max(state.ty, -overflowY - allowance), overflowY + allowance);
    }

    function applyTransform(viewport, state, zoomOutput) {
        const inner = viewport.querySelector('[data-mermaid-inner], .mermaid-inner');
        if (!inner) return;

        clampPan(viewport, state);
        inner.style.transform = `translate3d(${state.tx}px, ${state.ty}px, 0) scale(${state.scale})`;
        inner.style.transition = state.dragging ? 'none' : '';
        if (zoomOutput) zoomOutput.textContent = `${Math.round(state.scale * 100)}%`;
    }

    function fitDiagram(viewport, state, zoomOutput) {
        const dimensions = diagramDimensions(viewport);
        if (!dimensions || !viewport.clientWidth || !viewport.clientHeight) return;

        const horizontal = Math.max(viewport.clientWidth - 48, 1) / dimensions.width;
        const vertical = Math.max(viewport.clientHeight - 48, 1) / dimensions.height;
        state.fitScale = clampScale(Math.min(horizontal, vertical, 1));
        state.scale = state.fitScale;
        state.tx = 0;
        state.ty = 0;
        state.userAdjusted = false;
        applyTransform(viewport, state, zoomOutput);
    }

    function prepareSvg(block, svg, width, height) {
        svg.style.maxWidth = 'none';
        svg.style.width = `${width}px`;
        svg.style.height = `${height}px`;
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', 'Mermaid 图表');
        svg.setAttribute('focusable', 'false');

        if (!svg.querySelector('title')) {
            const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
            title.textContent = 'Mermaid 图表';
            svg.prepend(title);
        }

        block.dataset.mermaidRendered = 'true';
    }

    async function renderBlock(block, options = {}) {
        const source = sourceFor(block);
        if (!source) {
            setRenderState(block, 'error');
            return;
        }

        const state = getState(block);
        const version = ++state.renderVersion;
        setRenderState(block, 'loading');

        try {
            await enqueueRender(async () => {
                const mermaid = await getMermaid();
                const theme = currentTheme(block);
                mermaid.initialize(mermaidConfig(theme));

                const graphId = `wagtailblog-mermaid-${++renderSequence}`;
                const result = await mermaid.render(graphId, source);
                if (version !== state.renderVersion) return;

                const inner = block.querySelector('[data-mermaid-inner]');
                if (!inner) return;

                inner.innerHTML = result.svg;
                const viewport = block.querySelector('[data-mermaid-viewport]');
                const dimensions = viewport ? diagramDimensions(viewport) : null;
                if (!viewport || !dimensions) throw new Error('Mermaid returned an empty SVG');

                prepareSvg(block, dimensions.svg, dimensions.width, dimensions.height);
                result.bindFunctions?.(inner);
                setRenderState(block, 'ready');

                window.requestAnimationFrame(() => {
                    const zoomOutput = block.querySelector('[data-mermaid-zoom]');
                    if (options.preserveTransform && state.userAdjusted) {
                        applyTransform(viewport, state, zoomOutput);
                    } else {
                        fitDiagram(viewport, state, zoomOutput);
                    }
                });
            });
        } catch (error) {
            if (version === state.renderVersion) {
                setRenderState(block, 'error');
                block.querySelector('[data-mermaid-inner]')?.replaceChildren();
            }
            console.error('Mermaid render failed:', error);
        }
    }

    function updateThemeUi(block, theme) {
        const isDark = theme === 'dark';
        block.classList.toggle('dark-theme', isDark);

        const button = block.querySelector('[data-mermaid-action="theme"]');
        if (!button) return;

        const label = isDark ? '切换到亮色主题' : '切换到暗色主题';
        button.setAttribute('aria-pressed', String(isDark));
        button.setAttribute('aria-label', label);
        button.title = label;
        const icon = button.querySelector('i');
        if (icon) icon.className = `fa ${isDark ? 'fa-sun-o' : 'fa-moon-o'}`;
    }

    function setTheme(theme) {
        document.querySelectorAll(BLOCK_SELECTOR).forEach((block) => {
            updateThemeUi(block, theme);
            if (block.dataset.mermaidRendered === 'true' || block.dataset.mermaidState === 'error') {
                renderBlock(block, { preserveTransform: true });
            }
        });
    }

    function changeZoom(viewport, state, zoomOutput, amount) {
        state.scale = clampScale(state.scale + amount);
        state.userAdjusted = true;
        applyTransform(viewport, state, zoomOutput);
    }

    function pointerDistance(points) {
        if (points.length < 2) return 0;
        return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
    }

    function bindViewport(viewport, state, zoomOutput) {
        if (viewport.dataset.mermaidNavigationReady === 'true') return;
        viewport.dataset.mermaidNavigationReady = 'true';

        viewport.addEventListener('pointerdown', (event) => {
            if (event.pointerType === 'mouse' && event.button !== 0) return;
            if (event.target.closest('a, button')) return;

            viewport.setPointerCapture?.(event.pointerId);
            state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
            state.dragging = true;
            state.didDrag = false;
            state.startX = event.clientX - state.tx;
            state.startY = event.clientY - state.ty;

            if (state.pointers.size === 2) {
                state.pinchDistance = pointerDistance([...state.pointers.values()]);
                state.pinchScale = state.scale;
            }
            viewport.classList.add('is-dragging');
        });

        viewport.addEventListener('pointermove', (event) => {
            if (!state.pointers.has(event.pointerId)) return;
            state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

            if (state.pointers.size >= 2) {
                const distance = pointerDistance([...state.pointers.values()]);
                if (state.pinchDistance > 0) {
                    state.scale = clampScale(state.pinchScale * (distance / state.pinchDistance));
                }
            } else {
                state.tx = event.clientX - state.startX;
                state.ty = event.clientY - state.startY;
            }

            state.didDrag = true;
            state.userAdjusted = true;
            applyTransform(viewport, state, zoomOutput);
        });

        const finishPointer = (event) => {
            state.pointers.delete(event.pointerId);
            if (state.pointers.size === 1) {
                const point = [...state.pointers.values()][0];
                state.startX = point.x - state.tx;
                state.startY = point.y - state.ty;
            } else if (state.pointers.size === 0) {
                state.dragging = false;
                viewport.classList.remove('is-dragging');
                applyTransform(viewport, state, zoomOutput);
            }
        };

        viewport.addEventListener('pointerup', finishPointer);
        viewport.addEventListener('pointercancel', finishPointer);

        viewport.addEventListener('click', (event) => {
            if (!state.didDrag) return;
            event.preventDefault();
            event.stopPropagation();
            state.didDrag = false;
        }, true);

        viewport.addEventListener('wheel', (event) => {
            if (!event.ctrlKey && !event.metaKey) return;
            event.preventDefault();
            changeZoom(viewport, state, zoomOutput, event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
        }, { passive: false });
    }

    function copyText(value) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(value);
        }

        return new Promise((resolve, reject) => {
            const textarea = document.createElement('textarea');
            textarea.value = value;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            const copied = document.execCommand('copy');
            textarea.remove();
            copied ? resolve() : reject(new Error('Copy failed'));
        });
    }

    function downloadSvg(svg) {
        if (!svg) return;

        const clone = svg.cloneNode(true);
        clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
        const content = `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(clone)}`;
        const blob = new Blob([content], { type: 'image/svg+xml;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'mermaid-diagram.svg';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function dialogWindowControlsEnabled() {
        return !window.matchMedia(DIALOG_MOBILE_QUERY).matches;
    }

    function dialogMinimumSize() {
        const availableWidth = Math.max(window.innerWidth - DIALOG_MARGIN * 2, 0);
        const availableHeight = Math.max(window.innerHeight - DIALOG_MARGIN * 2, 0);
        return {
            width: Math.min(DIALOG_MIN_WIDTH, availableWidth),
            height: Math.min(DIALOG_MIN_HEIGHT, availableHeight)
        };
    }

    function readDialogGeometry(dialog) {
        const rect = dialog.getBoundingClientRect();
        return {
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height,
            right: rect.right,
            bottom: rect.bottom
        };
    }

    function clampDialogGeometry(geometry) {
        const minimum = dialogMinimumSize();
        const maximumWidth = Math.max(window.innerWidth - DIALOG_MARGIN * 2, 0);
        const maximumHeight = Math.max(window.innerHeight - DIALOG_MARGIN * 2, 0);
        const width = Math.min(Math.max(geometry.width, minimum.width), maximumWidth);
        const height = Math.min(Math.max(geometry.height, minimum.height), maximumHeight);
        const maxLeft = Math.max(DIALOG_MARGIN, window.innerWidth - DIALOG_MARGIN - width);
        const maxTop = Math.max(DIALOG_MARGIN, window.innerHeight - DIALOG_MARGIN - height);

        return {
            left: Math.min(Math.max(geometry.left, DIALOG_MARGIN), maxLeft),
            top: Math.min(Math.max(geometry.top, DIALOG_MARGIN), maxTop),
            width,
            height
        };
    }

    function applyDialogGeometry(dialog, geometry) {
        const next = clampDialogGeometry(geometry);
        dialog.style.inset = 'auto';
        dialog.style.margin = '0';
        dialog.style.left = `${Math.round(next.left)}px`;
        dialog.style.top = `${Math.round(next.top)}px`;
        dialog.style.width = `${Math.round(next.width)}px`;
        dialog.style.height = `${Math.round(next.height)}px`;
        return next;
    }

    function resizeDialogGeometry(origin, deltaX, deltaY, edges) {
        const minimum = dialogMinimumSize();
        const maxRight = window.innerWidth - DIALOG_MARGIN;
        const maxBottom = window.innerHeight - DIALOG_MARGIN;
        let left = origin.left;
        let top = origin.top;
        let right = origin.right;
        let bottom = origin.bottom;

        if (edges.includes('e')) {
            right = Math.min(Math.max(origin.right + deltaX, origin.left + minimum.width), maxRight);
        }
        if (edges.includes('w')) {
            left = Math.max(Math.min(origin.left + deltaX, origin.right - minimum.width), DIALOG_MARGIN);
        }
        if (edges.includes('s')) {
            bottom = Math.min(Math.max(origin.bottom + deltaY, origin.top + minimum.height), maxBottom);
        }
        if (edges.includes('n')) {
            top = Math.max(Math.min(origin.top + deltaY, origin.bottom - minimum.height), DIALOG_MARGIN);
        }

        return {
            left,
            top,
            width: right - left,
            height: bottom - top
        };
    }

    function refreshDialogViewport(viewport, state, zoomOutput) {
        window.requestAnimationFrame(() => {
            if (!viewport.isConnected) return;
            if (state.userAdjusted) {
                applyTransform(viewport, state, zoomOutput);
            } else {
                fitDiagram(viewport, state, zoomOutput);
            }
        });
    }

    function updateDialogMaximizeButton(button, maximized) {
        if (!button) return;
        const label = maximized ? '还原窗口' : '最大化窗口';
        button.setAttribute('aria-pressed', String(maximized));
        button.setAttribute('aria-label', label);
        button.title = label;
        const icon = button.querySelector('i');
        if (icon) icon.className = `fa ${maximized ? 'fa-compress' : 'fa-expand'}`;
    }

    function bindDialogWindowControls(dialog, viewport, modalState, zoomOutput) {
        const header = dialog.querySelector('.mermaid-dialog-header');
        const maximizeButton = dialog.querySelector('[data-dialog-action="maximize"]');
        const resizeHandles = [...dialog.querySelectorAll('[data-dialog-resize]')];
        const windowState = {
            maximized: false,
            geometry: null,
            restoreGeometry: null,
            interaction: null
        };

        const endInteraction = (event) => {
            const interaction = windowState.interaction;
            if (!interaction || (event && event.pointerId !== interaction.pointerId)) return;
            interaction.target.releasePointerCapture?.(interaction.pointerId);
            windowState.interaction = null;
            dialog.classList.remove('is-window-dragging', 'is-window-resizing');
            document.body.classList.remove('mermaid-window-interaction');
        };

        const onPointerMove = (event) => {
            const interaction = windowState.interaction;
            if (!interaction || event.pointerId !== interaction.pointerId) return;

            const deltaX = event.clientX - interaction.startX;
            const deltaY = event.clientY - interaction.startY;
            let next;

            if (interaction.type === 'drag') {
                next = {
                    left: interaction.origin.left + deltaX,
                    top: interaction.origin.top + deltaY,
                    width: interaction.origin.width,
                    height: interaction.origin.height
                };
            } else {
                next = resizeDialogGeometry(interaction.origin, deltaX, deltaY, interaction.edges);
            }

            windowState.geometry = applyDialogGeometry(dialog, next);
            event.preventDefault();
        };

        const beginInteraction = (event, type, edges = '') => {
            if (!dialogWindowControlsEnabled() || windowState.maximized) return;
            if (event.pointerType === 'mouse' && event.button !== 0) return;

            const origin = readDialogGeometry(dialog);
            windowState.geometry = {
                left: origin.left,
                top: origin.top,
                width: origin.width,
                height: origin.height
            };
            windowState.interaction = {
                type,
                edges,
                pointerId: event.pointerId,
                startX: event.clientX,
                startY: event.clientY,
                origin,
                target: event.currentTarget
            };
            event.currentTarget.setPointerCapture?.(event.pointerId);
            dialog.classList.add(type === 'drag' ? 'is-window-dragging' : 'is-window-resizing');
            document.body.classList.add('mermaid-window-interaction');
            event.preventDefault();
        };

        const setMaximized = (maximized) => {
            if (!dialogWindowControlsEnabled() || maximized === windowState.maximized) return;

            if (maximized) {
                const geometry = windowState.geometry || readDialogGeometry(dialog);
                windowState.restoreGeometry = {
                    left: geometry.left,
                    top: geometry.top,
                    width: geometry.width,
                    height: geometry.height
                };
                dialog.classList.add('is-window-maximized');
            } else {
                dialog.classList.remove('is-window-maximized');
                if (windowState.restoreGeometry) {
                    windowState.geometry = applyDialogGeometry(dialog, windowState.restoreGeometry);
                }
            }

            windowState.maximized = maximized;
            updateDialogMaximizeButton(maximizeButton, maximized);
            refreshDialogViewport(viewport, modalState, zoomOutput);
        };

        header?.addEventListener('pointerdown', (event) => {
            if (event.target.closest('button, a, input, select, textarea')) return;
            beginInteraction(event, 'drag');
        });

        header?.addEventListener('dblclick', (event) => {
            if (event.target.closest('button, a, input, select, textarea')) return;
            setMaximized(!windowState.maximized);
        });

        resizeHandles.forEach((handle) => {
            handle.addEventListener('pointerdown', (event) => {
                beginInteraction(event, 'resize', handle.dataset.dialogResize || '');
            });
        });

        dialog.addEventListener('pointermove', onPointerMove);
        dialog.addEventListener('pointerup', endInteraction);
        dialog.addEventListener('pointercancel', endInteraction);

        const onWindowResize = () => {
            if (dialogWindowControlsEnabled() && !windowState.maximized && windowState.geometry) {
                windowState.geometry = applyDialogGeometry(dialog, windowState.geometry);
            }
            refreshDialogViewport(viewport, modalState, zoomOutput);
        };

        window.addEventListener('resize', onWindowResize);
        updateDialogMaximizeButton(maximizeButton, false);

        return {
            toggleMaximized: () => setMaximized(!windowState.maximized),
            destroy: () => {
                endInteraction();
                window.removeEventListener('resize', onWindowResize);
            }
        };
    }

    function openFullscreen(block, opener) {
        const sourceInner = block.querySelector('[data-mermaid-inner]');
        const sourceSvg = sourceInner?.querySelector('svg');
        if (!sourceSvg) return;

        const dialog = document.createElement('dialog');
        dialog.className = `mermaid-dialog${currentTheme(block) === 'dark' ? ' dark-theme' : ''}`;
        dialog.setAttribute('aria-label', 'Mermaid 图表全屏预览');
        dialog.innerHTML = `
            <div class="mermaid-dialog-shell">
                <header class="mermaid-dialog-header">
                    <span><i class="fa fa-sitemap" aria-hidden="true"></i> Mermaid 图表</span>
                    <button class="mermaid-icon-button" type="button" data-dialog-action="close" title="关闭全屏" aria-label="关闭全屏">
                        <i class="fa fa-times" aria-hidden="true"></i>
                    </button>
                </header>
                <div class="mermaid-dialog-viewport" data-dialog-viewport tabindex="0"></div>
                <footer class="mermaid-dialog-toolbar" role="toolbar" aria-label="全屏图表工具">
                    <button class="mermaid-icon-button" type="button" data-dialog-action="zoom-out" title="缩小" aria-label="缩小"><i class="fa fa-search-minus" aria-hidden="true"></i></button>
                    <button class="mermaid-icon-button" type="button" data-dialog-action="zoom-in" title="放大" aria-label="放大"><i class="fa fa-search-plus" aria-hidden="true"></i></button>
                    <button class="mermaid-icon-button" type="button" data-dialog-action="fit" title="适应窗口" aria-label="适应窗口"><i class="fa fa-crosshairs" aria-hidden="true"></i></button>
                    <span class="mermaid-zoom-level" data-dialog-zoom aria-live="polite">100%</span>
                    <button class="mermaid-icon-button mermaid-dialog-download" type="button" data-dialog-action="download" title="下载 SVG" aria-label="下载 SVG"><i class="fa fa-download" aria-hidden="true"></i></button>
                </footer>
            </div>`;

        const header = dialog.querySelector('.mermaid-dialog-header');
        const closeButton = dialog.querySelector('[data-dialog-action="close"]');
        const headerActions = document.createElement('div');
        headerActions.className = 'mermaid-dialog-header-actions';

        const maximizeButton = document.createElement('button');
        maximizeButton.className = 'mermaid-icon-button mermaid-dialog-maximize';
        maximizeButton.type = 'button';
        maximizeButton.dataset.dialogAction = 'maximize';
        maximizeButton.setAttribute('aria-pressed', 'false');
        maximizeButton.setAttribute('aria-label', '最大化窗口');
        maximizeButton.title = '最大化窗口';
        maximizeButton.innerHTML = '<i class="fa fa-expand" aria-hidden="true"></i>';

        headerActions.append(maximizeButton);
        if (closeButton) headerActions.append(closeButton);
        header?.append(headerActions);
        dialog.insertAdjacentHTML('beforeend', DIALOG_RESIZE_EDGES.map((edge) => (
            `<span class="mermaid-dialog-resize-handle mermaid-dialog-resize-handle--${edge}" data-dialog-resize="${edge}" aria-hidden="true"></span>`
        )).join(''));

        const viewport = dialog.querySelector('[data-dialog-viewport]');
        const inner = sourceInner.cloneNode(true);
        inner.style.transform = '';
        inner.style.transition = '';
        viewport.appendChild(inner);

        const modalState = {
            scale: 1,
            fitScale: 1,
            tx: 0,
            ty: 0,
            pointers: new Map(),
            dragging: false,
            didDrag: false,
            userAdjusted: false
        };
        const zoomOutput = dialog.querySelector('[data-dialog-zoom]');
        bindViewport(viewport, modalState, zoomOutput);
        const windowControls = bindDialogWindowControls(dialog, viewport, modalState, zoomOutput);
        let dialogResizeObserver = null;

        if ('ResizeObserver' in window) {
            dialogResizeObserver = new ResizeObserver(() => {
                refreshDialogViewport(viewport, modalState, zoomOutput);
            });
            dialogResizeObserver.observe(viewport);
        }

        const close = () => {
            if (dialog.open) dialog.close();
        };

        dialog.addEventListener('click', (event) => {
            const action = event.target.closest('[data-dialog-action]')?.dataset.dialogAction;
            if (action === 'close') close();
            if (action === 'zoom-in') changeZoom(viewport, modalState, zoomOutput, ZOOM_STEP);
            if (action === 'zoom-out') changeZoom(viewport, modalState, zoomOutput, -ZOOM_STEP);
            if (action === 'fit') fitDiagram(viewport, modalState, zoomOutput);
            if (action === 'download') downloadSvg(viewport.querySelector('svg'));
            if (action === 'maximize') windowControls.toggleMaximized();
            if (event.target === dialog) close();
        });

        dialog.addEventListener('close', () => {
            dialogResizeObserver?.disconnect();
            windowControls.destroy();
            document.body.classList.remove('mermaid-modal-open');
            dialog.remove();
            window.requestAnimationFrame(() => {
                if (opener?.isConnected) opener.focus();
            });
        }, { once: true });

        document.body.appendChild(dialog);
        document.body.classList.add('mermaid-modal-open');
        dialog.showModal();
        window.requestAnimationFrame(() => {
            fitDiagram(viewport, modalState, zoomOutput);
            dialog.querySelector('[data-dialog-action="close"]')?.focus();
        });
    }

    function bindBlock(block) {
        if (block.dataset.mermaidReady === 'true') return;
        block.dataset.mermaidReady = 'true';

        const state = getState(block);
        const viewport = block.querySelector('[data-mermaid-viewport]');
        const zoomOutput = block.querySelector('[data-mermaid-zoom]');
        if (viewport) bindViewport(viewport, state, zoomOutput);

        block.addEventListener('click', async (event) => {
            const button = event.target.closest('[data-mermaid-action]');
            if (!button || !block.contains(button)) return;
            const action = button.dataset.mermaidAction;

            if (action === 'collapse') {
                const content = block.querySelector('[data-mermaid-content]');
                const expanded = button.getAttribute('aria-expanded') === 'true';
                button.setAttribute('aria-expanded', String(!expanded));
                button.setAttribute('aria-label', expanded ? '展开图表' : '折叠图表');
                button.title = expanded ? '展开图表' : '折叠图表';
                content.hidden = expanded;
                button.querySelector('i').className = `fa ${expanded ? 'fa-chevron-down' : 'fa-chevron-up'}`;
                return;
            }

            if (action === 'theme') {
                const theme = currentTheme(block) === 'dark' ? 'light' : 'dark';
                if (window.WagtailBlogTheme?.setPreference) {
                    window.WagtailBlogTheme.setPreference(theme);
                } else {
                    setTheme(theme);
                }
                return;
            }

            if (action === 'source') {
                const panel = block.querySelector('[data-mermaid-source-panel]');
                if (panel) panel.open = !panel.open;
                return;
            }

            if (action === 'copy-source') {
                const label = button.querySelector('span');
                try {
                    await copyText(sourceFor(block));
                    if (label) label.textContent = '已复制';
                    window.setTimeout(() => {
                        if (label) label.textContent = '复制源码';
                    }, 1500);
                } catch (error) {
                    console.error('Unable to copy Mermaid source:', error);
                }
                return;
            }

            if (!viewport) return;
            if (action === 'zoom-in') changeZoom(viewport, state, zoomOutput, ZOOM_STEP);
            if (action === 'zoom-out') changeZoom(viewport, state, zoomOutput, -ZOOM_STEP);
            if (action === 'fit') fitDiagram(viewport, state, zoomOutput);
            if (action === 'download') downloadSvg(viewport.querySelector('svg'));
            if (action === 'fullscreen') openFullscreen(block, button);
        });

        if ('ResizeObserver' in window && viewport) {
            const resizeObserver = new ResizeObserver(() => {
                if (block.dataset.mermaidState !== 'ready') return;
                if (state.userAdjusted) {
                    applyTransform(viewport, state, zoomOutput);
                } else {
                    fitDiagram(viewport, state, zoomOutput);
                }
            });
            resizeObserver.observe(viewport);
        }
    }

    function initialize() {
        const blocks = [...document.querySelectorAll(BLOCK_SELECTOR)];
        if (!blocks.length) return;

        const theme = preferredTheme();
        blocks.forEach((block) => {
            bindBlock(block);
            updateThemeUi(block, theme);
        });

        if ('IntersectionObserver' in window) {
            const observer = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    observer.unobserve(entry.target);
                    renderBlock(entry.target);
                });
            }, { rootMargin: '320px 0px' });
            blocks.forEach((block) => observer.observe(block));
        } else {
            blocks.forEach((block) => renderBlock(block));
        }

        window.addEventListener('site-theme-change', (event) => {
            const theme = event.detail?.theme;
            if (theme === 'dark' || theme === 'light') setTheme(theme);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
