/*
 * wagtailblog3/static/blog/js/mermaid_block.js
 * 专用于 Mermaid Block 的 jQuery 脚本
 *
 * v2.0:
 * - 提高原容器缩放上限 (500%)
 * - 提高原容器平移灵敏度 (* 3)
 * - 增加 Lightbox (弹窗) 功能，实现独立缩放/平移
 */

/**
 * ==================================================
 * 1. 原容器内缩放 (In-Block Zoom)
 * ==================================================
 * @param {HTMLElement} button - 被点击的按钮
 * @param {string} action - 'in', 'out', or 'reset'
 */
function zoomMermaid(button, action) {
    const $button = $(button);
    const $wrapper = $button.closest('.mermaid-diagram-wrapper');
    const $chartInner = $wrapper.find('.mermaid-inner');
    const $zoomLevelDisplay = $wrapper.find('.zoom-level');

    const currentTransform = $chartInner.css('transform');
    let currentScale = 1;

    if (currentTransform && currentTransform !== 'none') {
        const matrix = currentTransform.match(/matrix\(([^)]+)\)/);
        if (matrix && matrix[1]) {
            currentScale = parseFloat(matrix[1].split(',')[0]);
        }
    }

    let newScale = currentScale;
    const zoomStep = 0.2;
    const minScale = 0.2;
    const maxScale = 5.0; // ★★★ 需求 1: 上限提高到 500% ★★★

    switch(action) {
        case 'in':
            newScale = Math.min(currentScale + zoomStep, maxScale);
            break;
        case 'out':
            newScale = Math.max(currentScale - zoomStep, minScale);
            break;
        case 'reset':
            newScale = 1;
            break;
    }

    $chartInner.css('transform', `scale(${newScale})`);
    $zoomLevelDisplay.text(`${Math.round(newScale * 100)}%`);

    const $container = $wrapper.find('.mermaid-chart-container');
    $container.css('cursor', newScale > 1.2 ? 'move' : 'default');


}


/**
 * ==================================================
 * 2. ★★★ (新增) 弹窗内缩放 (Modal Zoom) ★★★
 * ==================================================
 */
function zoomMermaidModal(action) {
    const $chartInner = $('#mermaid-lightbox-body .mermaid-inner');
    if (!$chartInner.length) return; // 安全检查

    const $zoomLevelDisplay = $('.mermaid-lightbox-controls .zoom-level');

    const currentTransform = $chartInner.css('transform');
    let currentScale = 1;

    if (currentTransform && currentTransform !== 'none') {
        const matrix = currentTransform.match(/matrix\(([^)]+)\)/);
        if (matrix && matrix[1]) {
            currentScale = parseFloat(matrix[1].split(',')[0]);
        }
    }

    let newScale = currentScale;
    const zoomStep = 0.2;
    const minScale = 0.2;
    const maxScale = 10.0; // 弹窗内允许 1000% 放大

    switch(action) {
        case 'in':
            newScale = Math.min(currentScale + zoomStep, maxScale);
            break;
        case 'out':
            newScale = Math.max(currentScale - zoomStep, minScale);
            break;
        case 'reset':
            newScale = 1;
            break;
    }

    $chartInner.css('transform', `scale(${newScale})`);
    $zoomLevelDisplay.text(`${Math.round(newScale * 100)}%`);

    $('#mermaid-lightbox-body').css('cursor', newScale > 1.0 ? 'move' : 'default');
}

/**
 * ==================================================
 * 3. ★★★ (新增) 构建弹窗 HTML ★★★
 * ==================================================
 */
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
                
                <div id="mermaid-lightbox-body">
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


// ==================================================
// 4. jQuery 事件绑定
// ==================================================
$(function() {

    // 4.1. 原容器 - 折叠/展开
    $('body').on('click', '.mermaid-header', function() {
        // ... (此部分代码无变化) ...
        const $header = $(this);
        const $wrapper = $header.closest('.mermaid-diagram-wrapper');
        const $content = $wrapper.find('.mermaid-content');
        const $icon = $header.find('.toggle-icon');

        $content.toggleClass('collapsed');
        $icon.text($content.hasClass('collapsed') ? '▼' : '▲');
    });

    // 4.2. 原容器 - 缩放按钮
    $('body').on('click', '.zoom-controls button[data-zoom]', function(e) {
        e.stopPropagation();
        const action = $(this).data('zoom');

        if (action === 'fullscreen') {
            // ★★★ (新增) 全屏按钮逻辑 ★★★
            if ($('#mermaid-lightbox').length) return; // 防止重复打开

            // 1. 找到图表并克隆
            const $wrapper = $(this).closest('.mermaid-diagram-wrapper');
            const $diagram = $wrapper.find('.mermaid-inner').clone();

            // 2. 构建并添加 Modal
            $('body').append(buildMermaidModal());

            // 3. 注入图表
            $('#mermaid-lightbox-body').append($diagram);

            // 4. 显示
            $('#mermaid-lightbox').fadeIn(200);

        } else {
            // 原有缩放逻辑
            zoomMermaid(this, action);
        }
    });

    // 4.3. 原容器 - 平移拖动
    $('.mermaid-chart-container').each(function() {
        const $container = $(this);
        let isDragging = false, startX, startY, scrollLeft, scrollTop;

        function getScale() {
            // ... (此部分代码无变化) ...
            const $chartInner = $container.find('.mermaid-inner');
            const transform = $chartInner.css('transform');
            let scale = 1;
            if (transform && transform !== 'none') {
                const matrix = transform.match(/matrix\(([^)]+)\)/);
                if (matrix && matrix[1]) scale = parseFloat(matrix[1].split(',')[0]);
            }
            return scale;
        }

        $container.on('mousedown', function(e) {
            if (getScale() > 1.2) {
                isDragging = true;
                startX = e.pageX - $container.offset().left;
                startY = e.pageY - $container.offset().top;
                scrollLeft = $container.scrollLeft();
                scrollTop = $container.scrollTop();
                $container.css('cursor', 'grabbing');
            }
        });

        $container.on('mouseup mouseleave', function() {
            isDragging = false;
            $container.css('cursor', getScale() > 1.2 ? 'move' : 'default');
        });

        $container.on('mousemove', function(e) {
            if (!isDragging) return;
            e.preventDefault();
            const x = e.pageX - $container.offset().left;
            const y = e.pageY - $container.offset().top;

            // ★★★ 需求 2: 灵敏度 * 2 调整为 * 3 ★★★
            const walkX = (x - startX) * 3;
            const walkY = (y - startY) * 3;

            $container.scrollLeft(scrollLeft - walkX);
            $container.scrollTop(scrollTop - walkY);
        });
    });


    // ==================================================
    // 4.4. ★★★ (新增) 弹窗事件绑定 (使用事件委托) ★★★
    // ==================================================

    // 弹窗 - 关闭 / 最小化
    $('body').on('click', '#mermaid-lightbox-close, #mermaid-lightbox-minimize', function() {
        $('#mermaid-lightbox').fadeOut(200, function() {
            $(this).remove(); // 关闭后彻底移除
        });
    });

    // 弹窗 - 最大化 / 还原
    $('body').on('click', '#mermaid-lightbox-maximize', function() {
        $(this).closest('.mermaid-lightbox-modal').toggleClass('maximized');
    });

    // 弹窗 - 缩放按钮
    $('body').on('click', '.mermaid-lightbox-controls button[data-zoom]', function() {
        const action = $(this).data('zoom');
        zoomMermaidModal(action);
    });

    // 弹窗 - 平移拖动 (逻辑同 4.3, 仅选择器不同)
    $('body').on('mousedown', '#mermaid-lightbox-body', function(e) {
        const $container = $(this);
        const $chartInner = $container.find('.mermaid-inner');
        let scale = 1;
        const transform = $chartInner.css('transform');
        if (transform && transform !== 'none') {
            const matrix = transform.match(/matrix\(([^)]+)\)/);
            if (matrix && matrix[1]) scale = parseFloat(matrix[1].split(',')[0]);
        }

        if (scale > 1.0) { // 弹窗内超过 100% 即可拖动
            $container.data('isDragging', true);
            $container.data('startX', e.pageX - $container.offset().left);
            $container.data('startY', e.pageY - $container.offset().top);
            $container.data('scrollLeft', $container.scrollLeft());
            $container.data('scrollTop', $container.scrollTop());
            $container.css('cursor', 'grabbing');
        }
    }).on('mouseup mouseleave', '#mermaid-lightbox-body', function() {
        $(this).data('isDragging', false);
        const $container = $(this);
        // ... [计算 scale, 同上] ...
        let scale = 1;
        const transform = $container.find('.mermaid-inner').css('transform');
        if (transform && transform !== 'none') {
            const matrix = transform.match(/matrix\(([^)]+)\)/);
            if (matrix && matrix[1]) scale = parseFloat(matrix[1].split(',')[0]);
        }
        $container.css('cursor', scale > 1.0 ? 'move' : 'default');

    }).on('mousemove', '#mermaid-lightbox-body', function(e) {
        const $container = $(this);
        if (!$container.data('isDragging')) return;

        e.preventDefault();
        const x = e.pageX - $container.offset().left;
        const y = e.pageY - $container.offset().top;

        const walkX = (x - $container.data('startX')) * 3; // 弹窗内也使用 3 倍灵敏度
        const walkY = (y - $container.data('startY')) * 3;

        $container.scrollLeft($container.data('scrollLeft') - walkX);
        $container.scrollTop($container.data('scrollTop') - walkY);
    });

});