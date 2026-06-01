/*
 * wagtailblog3/static/blog/js/mermaid_block.js
 * 核心逻辑：完美支持缩放 (Zoom)、硬件加速平移拖拽 (Pan)、全屏 (Fullscreen) 与动态渲染
 */
$(function() {
    // ==========================================
    // 1. Mermaid 异步加载与渲染引擎
    // ==========================================
    let mermaidPromise = null;
    function getMermaid() {
        if (!mermaidPromise) {
            // 使用动态 import 引入模块，彻底解决 MIME Type 和 inline script 限制
            mermaidPromise = import("/static/blog/js/mermaid/mermaid.esm.min.mjs").then(module => {
                const m = module.default;
                m.initialize({ startOnLoad: false, securityLevel: 'loose' });
                return m;
            });
        }
        return mermaidPromise;
    }

    async function renderChart($wrapper) {
        const m = await getMermaid();
        const source = $wrapper.find('.mermaid-raw-source').val().trim();
        if (!source) return;

        const isDark = $wrapper.hasClass('dark-theme');
        const themeName = isDark ? 'dark' : 'default';
        const graphId = 'mm-' + Math.random().toString(36).substr(2, 9);

        try {
            const { svg } = await m.render(graphId, source);
            const $inner = $wrapper.find('.mermaid-inner');
            $inner.html(svg);
            // 关键修复：强制移除生成 SVG 的 max-width，保证自由缩放
            $inner.find('svg').css('max-width', 'none');
            applyTransform($wrapper);
        } catch (error) {
            console.error("Mermaid 渲染失败:", error);
        }
    }

    // 初始化所有未被处理的图表
    $('.mermaid-diagram-wrapper:not(.mm-initialized)').each(function() {
        const $wrapper = $(this).addClass('mm-initialized');
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            $wrapper.addClass('dark-theme');
        }
        renderChart($wrapper);
    });

    // ==========================================
    // 2. 状态与动画管理 (Zoom & Pan)
    // ==========================================
    function getState($container) {
        let state = $container.data('mm-state');
        if (!state) {
            state = { scale: 1, tx: 0, ty: 0, isDragging: false };
            $container.data('mm-state', state);
        }
        return state;
    }

    function applyTransform($container) {
        // 判断是在网页内嵌还是在全屏弹窗中
        const isModal = $container.attr('id') === 'mermaid-lightbox-body';
        const $wrapper = isModal ? $('#mermaid-lightbox') : $container.closest('.mermaid-diagram-wrapper');
        const $inner = $container.find('.mermaid-inner');
        const state = getState($container);

        const transition = state.isDragging ? 'none' : 'transform 0.2s ease-out';
        $inner.css({
            'transform': `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`,
            'transform-origin': 'center center',
            'transition': transition
        });

        $wrapper.find('.zoom-level').text(Math.round(state.scale * 100) + '%');

        // ==== 🆕 新增：联动滑块，并动态绘制进度条颜色 ====
        const $slider = $wrapper.find('.mm-zoom-slider');
        if ($slider.length) {
            $slider.val(state.scale);
            const min = parseFloat($slider.attr('min'));
            const max = parseFloat($slider.attr('max'));
            const percentage = ((state.scale - min) / (max - min)) * 100;
            // 核心魔法：利用线性渐变，让滑块左侧显示主色，右侧显示边框底色
            $slider.css('background', `linear-gradient(to right, var(--mm-btn-bg) ${percentage}%, var(--mm-border) ${percentage}%)`);
        }
    }

    // ==========================================
    // 3. 按钮交互事件 (使用 jQuery 事件委托绑定到 body)
    // ==========================================

    // 折叠/展开
    $('body').on('click', '.mermaid-header', function() {
        const $wrapper = $(this).closest('.mermaid-diagram-wrapper');
        const $content = $wrapper.find('.mermaid-content');
        const $icon = $(this).find('.toggle-icon');

        $content.slideToggle(300);
        $icon.css('transform', $content.is(':visible') ? 'rotate(180deg)' : 'rotate(0deg)');
    });

    // 缩放操作 (放大、缩小、重置)
    $('body').on('click', 'button[data-zoom]', function(e) {
        e.stopPropagation();
        const action = $(this).data('zoom');
        if (action === 'fullscreen') return; // 全屏走独立逻辑

        const isModal = $(this).closest('#mermaid-lightbox').length > 0;
        const $container = isModal ? $('#mermaid-lightbox-body') : $(this).closest('.mermaid-diagram-wrapper').find('.mermaid-chart-container');

        if (!$container.length) return;
        const state = getState($container);

        if (action === 'in') state.scale = Math.min(state.scale + 0.2, 5.0);
        else if (action === 'out') state.scale = Math.max(state.scale - 0.2, 0.2);
        else if (action === 'reset') {
            state.scale = 1; state.tx = 0; state.ty = 0;
        }

        applyTransform($container);
    });

    // ==========================================
    // 🆕 3.5 监听滑块拖动事件
    // ==========================================
    $('body').on('input', '.mm-zoom-slider', function(e) {
        const val = parseFloat($(this).val());
        const isModal = $(this).closest('#mermaid-lightbox').length > 0;
        const $container = isModal ? $('#mermaid-lightbox-body') : $(this).closest('.mermaid-diagram-wrapper').find('.mermaid-chart-container');

        if (!$container.length) return;
        const state = getState($container);
        state.scale = val;
        applyTransform($container); // 触发重绘与 UI 同步
    });


    // 主题切换
    $('body').on('click', '.theme-toggle-btn', function(e) {
        e.stopPropagation();
        const $wrapper = $(this).closest('.mermaid-diagram-wrapper');
        $wrapper.toggleClass('dark-theme');
        renderChart($wrapper); // 切换完类名后重新渲染 SVG
    });

    // ==========================================
    // 4. 鼠标硬件加速拖拽平移
    // ==========================================
    $('.mermaid-chart-container, #mermaid-lightbox-body').css('cursor', 'grab');

    $('body').on('mousedown', '.mermaid-chart-container, #mermaid-lightbox-body', function(e) {
        e.preventDefault();
        const $container = $(this);
        const state = getState($container);

        state.isDragging = true;
        state.startX = e.pageX - state.tx;
        state.startY = e.pageY - state.ty;
        $container.css('cursor', 'grabbing');
        applyTransform($container);
    });

    $(window).on('mousemove', function(e) {
        // 查找当前正在拖拽的容器
        const $container = $('.mermaid-chart-container, #mermaid-lightbox-body').filter(function() {
            return ($(this).data('mm-state') || {}).isDragging;
        });

        if ($container.length) {
            const state = getState($container);
            state.tx = e.pageX - state.startX;
            state.ty = e.pageY - state.startY;
            applyTransform($container);
        }
    });

    $(window).on('mouseup', function() {
        $('.mermaid-chart-container, #mermaid-lightbox-body').each(function() {
            const state = getState($(this));
            if (state.isDragging) {
                state.isDragging = false;
                $(this).css('cursor', 'grab');
                applyTransform($(this));
            }
        });
    });

    // ==========================================
    // 5. 全屏查看功能 (带继承缩放与进度条)
    // ==========================================
    $('body').on('click', '.mermaid-fullscreen-btn', function(e) {
        e.stopPropagation();
        const $wrapper = $(this).closest('.mermaid-diagram-wrapper');
        const $diagramHtml = $wrapper.find('.mermaid-inner').html();

        // 🆕 获取原网页图表的缩放比例，让全屏打开时平滑继承
        const embeddedState = getState($wrapper.find('.mermaid-chart-container'));
        const initScale = embeddedState.scale || 1;

        // 构建全屏 HTML
        const modalHtml = `
            <div id="mermaid-lightbox" class="${$wrapper.hasClass('dark-theme') ? 'dark-theme' : ''}">
                <div class="mermaid-lightbox-modal">
                    <div class="mermaid-lightbox-header">
                        <div class="mermaid-lightbox-window-controls">
                            <button id="mermaid-lightbox-close" title="关闭" style="background:#ff5f57; border:none; width:14px; height:14px; border-radius:50%; margin-right:5px; cursor:pointer;"></button>
                        </div>
                        <div class="mermaid-lightbox-title" style="flex-grow:1; text-align:center; font-weight:bold;">图表预览</div>
                    </div>
                    <div id="mermaid-lightbox-body" class="mermaid-chart-container" style="flex-grow:1; overflow:hidden; display:flex; align-items:center; justify-content:center;">
                        <div class="mermaid-inner">${$diagramHtml}</div>
                    </div>
                    <div class="mermaid-lightbox-controls zoom-controls" style="padding:10px; display:flex; gap:15px; justify-content:center; align-items:center; border-top:1px solid var(--mm-border);">
                        <button data-zoom="out">🔍 缩小</button>
                        <input type="range" class="mm-zoom-slider" min="0.2" max="5.0" step="0.1" value="${initScale}">
                        <button data-zoom="in">🔍 放大</button>
                        <button data-zoom="reset">↺ 重置</button>
                        <span class="zoom-level">100%</span>
                    </div>
                </div>
            </div>
        `;

        $('body').append(modalHtml);

        // 🆕 初始化全屏容器的 state，并立即渲染一次让滑块铺满正确的颜色
        const $modalBody = $('#mermaid-lightbox-body');
        const modalState = getState($modalBody);
        modalState.scale = initScale;
        modalState.tx = 0; // 全屏后位置归中
        modalState.ty = 0;
        applyTransform($modalBody);
    });

    $('body').on('click', '#mermaid-lightbox-close, #mermaid-lightbox', function(e) {
        if (e.target === this) {
            $('#mermaid-lightbox').remove();
        }
    });
});