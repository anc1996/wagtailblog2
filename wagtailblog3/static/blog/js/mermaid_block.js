/*
 * wagtailblog3/static/blog/js/mermaid_block.js
 * 核心升级版：完美支持缩放 (Zoom) 与硬件加速平移拖拽 (Pan)
 */
$(function() {

    // 1. 核心状态机：为每个图表容器独立保存缩放和位移状态
    function getState($container) {
        let state = $container.data('mm-state');
        if (!state) {
            state = { scale: 1, tx: 0, ty: 0, isDragging: false };
            $container.data('mm-state', state);
        }
        return state;
    }

    // 2. 核心渲染器：应用 Transform 变形进行硬件加速
    function applyTransform($container, state) {
        const $inner = $container.find('.mermaid-inner');
        // 拖拽时取消过渡动画以保证鼠标绝对跟手，缩放时恢复柔和动画
        const transition = state.isDragging ? 'none' : 'transform 0.2s ease';

        $inner.css({
            'transform': `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`,
            'transform-origin': 'center center',
            'transition': transition
        });

        // 同步更新 UI 上的百分比文字
        const $wrapper = $container.closest('.mermaid-diagram-wrapper, .mermaid-lightbox-modal');
        $wrapper.find('.zoom-level').text(`${Math.round(state.scale * 100)}%`);
    }

    // ==========================================
    // 功能 A：缩放逻辑 (支持网页内嵌和全屏弹窗)
    // ==========================================
    $('body').on('click', 'button[data-zoom]', function(e) {
        e.stopPropagation();
        const action = $(this).data('zoom');
        if (action === 'fullscreen') return; // 全屏按钮走独立逻辑

        const isModal = $(this).closest('#mermaid-lightbox').length > 0;
        const $container = isModal ? $('#mermaid-lightbox-body') : $(this).closest('.mermaid-diagram-wrapper').find('.mermaid-chart-container');

        if (!$container.length) return;

        const state = getState($container);
        const step = 0.2;
        const maxScale = isModal ? 10.0 : 5.0; // 弹窗允许放大 10 倍
        const minScale = 0.2;

        if (action === 'in') {
            state.scale = Math.min(state.scale + step, maxScale);
        } else if (action === 'out') {
            state.scale = Math.max(state.scale - step, minScale);
        } else if (action === 'reset') {
            state.scale = 1;
            state.tx = 0;
            state.ty = 0; // 重置时归位到中心
        }

        state.isDragging = false;
        applyTransform($container, state);
    });

    // ==========================================
    // 功能 B：丝滑拖拽平移逻辑
    // ==========================================

    // 给容器加上默认的“可抓取”手势光标
    $('.mermaid-chart-container').css('cursor', 'grab');

    $('body').on('mousedown', '.mermaid-chart-container, #mermaid-lightbox-body', function(e) {
        e.preventDefault(); // 防止拖拽时误选中旁边的文字

        const $container = $(this);
        const state = getState($container);

        state.isDragging = true;
        // 记录鼠标按下的初始坐标和容器的初始位移
        state.startX = e.pageX;
        state.startY = e.pageY;
        state.startTx = state.tx;
        state.startTy = state.ty;

        $container.css('cursor', 'grabbing');
        applyTransform($container, state);
    });

    $('body').on('mousemove', '.mermaid-chart-container, #mermaid-lightbox-body', function(e) {
        const $container = $(this);
        const state = getState($container);

        if (!state.isDragging) return;

        // 计算鼠标移动的差值
        const deltaX = e.pageX - state.startX;
        const deltaY = e.pageY - state.startY;

        // 实时更新当前位移
        state.tx = state.startTx + deltaX;
        state.ty = state.startTy + deltaY;

        applyTransform($container, state);
    });

    $('body').on('mouseup mouseleave', '.mermaid-chart-container, #mermaid-lightbox-body', function(e) {
        const $container = $(this);
        const state = getState($container);

        if (state.isDragging) {
            state.isDragging = false;
            $container.css('cursor', 'grab'); // 恢复手势
            applyTransform($container, state);
        }
    });

    // ==========================================
    // 功能 C：手风琴 折叠/展开
    // ==========================================
    $('body').on('click', '.mermaid-header', function() {
        const $header = $(this);
        const $wrapper = $header.closest('.mermaid-diagram-wrapper');
        const $content = $wrapper.find('.mermaid-content');
        const $icon = $header.find('.toggle-icon');

        $content.toggleClass('collapsed');
        $icon.text($content.hasClass('collapsed') ? '▼' : '▲');
    });

    // ==========================================
    // 功能 D：构建全屏弹窗 DOM
    // ==========================================
    function buildMermaidModal() {
        return `
            <div id="mermaid-lightbox">
                <div class="mermaid-lightbox-modal">
                    <div class="mermaid-lightbox-header">
                        <div class="mermaid-lightbox-window-controls">
                            <button id="mermaid-lightbox-close" title="关闭"></button>
                            <button id="mermaid-lightbox-minimize" title="最小化"></button>
                            <button id="mermaid-lightbox-maximize" title="最大化/还原"></button>
                        </div>
                        <div class="mermaid-lightbox-title">Mermaid 图表查看器</div>
                    </div>
                    
                    <div id="mermaid-lightbox-body" style="cursor: grab;">
                    </div>
                    
                    <div class="mermaid-lightbox-controls">
                        <button data-zoom="in">🔍 放大</button>
                        <button data-zoom="out">🔍 缩小</button>
                        <button data-zoom="reset">↺ 重置</button>
                        <span class="zoom-level">100%</span>
                    </div>
                </div>
            </div>
        `;
    }

    // ==========================================
    // 功能 E：全屏弹窗打开/关闭逻辑
    // ==========================================
    $('body').on('click', '.mermaid-fullscreen-btn', function(e) {
        e.stopPropagation();
        if ($('#mermaid-lightbox').length) return; // 防连点

        const $wrapper = $(this).closest('.mermaid-diagram-wrapper');
        const $diagram = $wrapper.find('.mermaid-inner').clone();

        // 关键修复：移除 SVG 被强加的 maxWidth，让其在弹窗中能自由放大不被裁剪
        $diagram.find('svg').css('max-width', 'none');

        $('body').append(buildMermaidModal());

        // 完美继承当前原图表的黑夜/白天主题
        if ($wrapper.hasClass('dark-theme')) {
            $('#mermaid-lightbox').addClass('dark-theme');
        }

        $('#mermaid-lightbox-body').append($diagram);

        // 初始化弹窗的独立坐标与缩放状态
        getState($('#mermaid-lightbox-body'));

        $('#mermaid-lightbox').fadeIn(200);
    });

    // 弹窗关闭与最小化
    $('body').on('click', '#mermaid-lightbox-close, #mermaid-lightbox-minimize', function() {
        $('#mermaid-lightbox').fadeOut(200, function() {
            $(this).remove(); // 淡出后清除DOM
        });
    });

    // 弹窗最大化/还原
    $('body').on('click', '#mermaid-lightbox-maximize', function() {
        $(this).closest('.mermaid-lightbox-modal').toggleClass('maximized');
    });

});