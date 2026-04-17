// static/blog/js/image_blocks.js
// 图片块交互功能 - jQuery版本 - 标题显示和点击放大

$(document).ready(function() {
    initializeImageBlocks();
    optimizeImageLoading();
});

function initializeImageBlocks() {
    // 获取所有图片块
    var $imageBlocks = $('.content-block-wrapper[data-block-type="image_block"]');

    $imageBlocks.each(function() {
        var $block = $(this);
        var $img = $block.find('img');

        if ($img.length === 0) return;

        // 设置图片标题
        setupImageTitle($block, $img);

        // 设置点击放大功能
        setupImageZoom($block, $img);
    });

    // 创建模态框
    createImageModal();
}

function setupImageTitle($block, $img) {
    // 从img的alt属性获取标题
    var title = $img.attr('alt') || '';

    if (title && $.trim(title) !== '') {
        // 将标题设置为block的data属性，供CSS使用
        $block.attr('data-image-title', $.trim(title));
    }
}

function setupImageZoom($block, $img) {
    // 为图片添加点击事件
    $img.on('click', function(e) {
        e.preventDefault();

        // 获取原图URL
        var originalSrc = $img.attr('src');
        var title = $img.attr('alt') || '';

        // 显示放大图片
        showImageModal(originalSrc, title);
    });

    // 添加键盘支持
    $img.on('keydown', function(e) {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            $img.trigger('click');
        }
    });

    // 确保图片可以通过键盘访问
    $img.attr({
        'tabindex': '0',
        'role': 'button',
        'aria-label': '点击查看大图'
    });
}

function createImageModal() {
    // 检查是否已存在模态框
    if ($('#imageModal').length > 0) {
        return;
    }

    // 创建模态框HTML结构
    var modalHTML = `
        <div id="imageModal" class="image-modal" role="dialog" aria-label="图片查看器" aria-hidden="true">
            <span class="image-modal-close" aria-label="关闭">&times;</span>
            <div class="image-modal-content">
                <img id="modalImage" src="" alt="">
                <div id="modalCaption" class="image-modal-caption"></div>
            </div>
        </div>
    `;

    // 添加到页面
    $('body').append(modalHTML);

    // 获取模态框元素
    var $modal = $('#imageModal');
    var $closeBtn = $modal.find('.image-modal-close');

    // 设置关闭事件
    $closeBtn.on('click', hideImageModal);

    // 点击背景关闭
    $modal.on('click', function(e) {
        if (e.target === this) {
            hideImageModal();
        }
    });

    // 键盘ESC关闭
    $(document).on('keydown', function(e) {
        if (e.key === 'Escape' && $modal.hasClass('active')) {
            hideImageModal();
        }
    });

    // 防止模态框内容区域的点击事件冒泡
    $modal.find('.image-modal-content').on('click', function(e) {
        e.stopPropagation();
    });
}

function showImageModal(imageSrc, caption) {
    var $modal = $('#imageModal');
    var $modalImg = $('#modalImage');
    var $modalCaption = $('#modalCaption');

    if ($modal.length === 0 || $modalImg.length === 0 || $modalCaption.length === 0) {
        console.error('图片模态框元素未找到');
        return;
    }

    // 设置图片和标题
    $modalImg.attr('src', imageSrc);
    $modalImg.attr('alt', caption || '');
    $modalCaption.text(caption || '');

    // 如果没有标题则隐藏标题区域
    if (!caption || $.trim(caption) === '') {
        $modalCaption.hide();
        $modalImg.css('border-radius', '12px');
    } else {
        $modalCaption.show();
        $modalImg.css('border-radius', '12px 12px 0 0');
    }

    // 显示模态框
    $modal.addClass('active');
    $modal.attr('aria-hidden', 'false');

    // 禁用页面滚动
    $('body').css('overflow', 'hidden');

    // 图片加载完成后检查尺寸并设置拖拽功能
    $modalImg.on('load', function() {
        setupImageDrag($modal);
        checkImageSize($modalImg, $modal);
    });

    // 设置焦点到关闭按钮
    setTimeout(function() {
        $modal.find('.image-modal-close').focus();
    }, 100);
}

// 检查图片尺寸并应用相应的显示策略
function checkImageSize($img, $modal) {
    var $content = $modal.find('.image-modal-content');
    var windowWidth = $(window).width();
    var windowHeight = $(window).height();
    var imgWidth = $img[0].naturalWidth;
    var imgHeight = $img[0].naturalHeight;

    // 计算图片显示尺寸
    var maxWidth = windowWidth * 0.95;
    var maxHeight = windowHeight * 0.85;

    // 如果图片尺寸超过屏幕限制，启用大图模式
    if (imgWidth > maxWidth || imgHeight > maxHeight) {
        $content.addClass('large-image');

        // 添加拖拽提示
        $content.attr('title', '图片较大，可以拖拽查看');

        // 计算初始居中位置
        var scaleFactor = Math.min(maxWidth / imgWidth, maxHeight / imgHeight);
        var displayWidth = imgWidth * scaleFactor;
        var displayHeight = imgHeight * scaleFactor;

        $content.css({
            'left': '50%',
            'top': '50%',
            'transform': 'translate(-50%, -50%)',
            'position': 'absolute'
        });
    } else {
        $content.removeClass('large-image');
        $content.removeAttr('title');
        $content.css({
            'position': 'relative',
            'left': 'auto',
            'top': 'auto',
            'transform': 'none'
        });
    }
}

// 设置图片拖拽功能
function setupImageDrag($modal) {
    var $content = $modal.find('.image-modal-content');
    var isDragging = false;
    var startX, startY, startLeft, startTop;

    // 鼠标按下事件
    $content.on('mousedown', function(e) {
        if (!$content.hasClass('large-image')) return;

        e.preventDefault();
        isDragging = true;
        $content.addClass('dragging');

        startX = e.clientX;
        startY = e.clientY;

        var contentOffset = $content.offset();
        startLeft = contentOffset.left;
        startTop = contentOffset.top;

        // 防止文本选择
        $('body').css('user-select', 'none');
    });

    // 鼠标移动事件
    $(document).on('mousemove.imageDrag', function(e) {
        if (!isDragging) return;

        e.preventDefault();
        var deltaX = e.clientX - startX;
        var deltaY = e.clientY - startY;

        $content.css({
            'left': startLeft + deltaX + 'px',
            'top': startTop + deltaY + 'px',
            'transform': 'none'
        });
    });

    // 鼠标释放事件
    $(document).on('mouseup.imageDrag', function() {
        if (isDragging) {
            isDragging = false;
            $content.removeClass('dragging');
            $('body').css('user-select', '');
        }
    });

    // 触摸设备支持
    $content.on('touchstart', function(e) {
        if (!$content.hasClass('large-image')) return;

        e.preventDefault();
        var touch = e.originalEvent.touches[0];
        isDragging = true;

        startX = touch.clientX;
        startY = touch.clientY;

        var contentOffset = $content.offset();
        startLeft = contentOffset.left;
        startTop = contentOffset.top;
    });

    $(document).on('touchmove.imageDrag', function(e) {
        if (!isDragging) return;

        e.preventDefault();
        var touch = e.originalEvent.touches[0];
        var deltaX = touch.clientX - startX;
        var deltaY = touch.clientY - startY;

        $content.css({
            'left': startLeft + deltaX + 'px',
            'top': startTop + deltaY + 'px',
            'transform': 'none'
        });
    });

    $(document).on('touchend.imageDrag', function() {
        if (isDragging) {
            isDragging = false;
            $content.removeClass('dragging');
        }
    });
}

function hideImageModal() {
    var $modal = $('#imageModal');

    if ($modal.length === 0) return;

    // 清理拖拽事件监听器
    $(document).off('mousemove.imageDrag mouseup.imageDrag touchmove.imageDrag touchend.imageDrag');

    // 重置内容位置和状态
    var $content = $modal.find('.image-modal-content');
    $content.removeClass('large-image dragging');
    $content.removeAttr('title');
    $content.css({
        'position': 'relative',
        'left': 'auto',
        'top': 'auto',
        'transform': 'none'
    });

    // 隐藏模态框
    $modal.removeClass('active');
    $modal.attr('aria-hidden', 'true');

    // 恢复页面滚动和文本选择
    $('body').css({
        'overflow': '',
        'user-select': ''
    });

    // 清理图片源和事件监听器
    var $modalImg = $('#modalImage');
    if ($modalImg.length > 0) {
        $modalImg.off('load');
        $modalImg.attr('src', '');
    }
}

// 图片加载错误处理
function handleImageError($img) {
    $img.on('error', function() {
        console.warn('图片加载失败:', $img.attr('src'));

        // 创建错误提示占位符
        var $placeholder = $(`
            <div class="image-error-placeholder" style="
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 3rem;
                background: #f8f9fa;
                border: 2px dashed #dee2e6;
                border-radius: 12px;
                color: #6c757d;
                text-align: center;
            ">
                <i class="fa fa-image fa-3x"></i>
                <p>图片加载失败</p>
            </div>
        `);

        $img.replaceWith($placeholder);
    });
}

// 响应式图片加载优化
function optimizeImageLoading() {
    var $imageBlocks = $('.content-block-wrapper[data-block-type="image_block"] img');

    $imageBlocks.each(function() {
        var $img = $(this);

        // 添加loading="lazy"属性以实现懒加载
        $img.attr('loading', 'lazy');

        // 添加错误处理
        handleImageError($img);

        // 设置过渡动画
        $img.css('transition', 'opacity 0.3s ease');

        // 检查图片是否已经加载完成
        if (this.complete && this.naturalHeight !== 0) {
            // 图片已经加载完成，直接显示
            $img.css('opacity', '1');
        } else {
            // 图片未加载完成，设置初始透明度并监听加载事件
            $img.css('opacity', '0');

            $img.on('load', function() {
                $(this).css('opacity', '1');
            });
        }
    });
}

// 导出函数供外部使用
window.ImageBlocks = {
    initialize: initializeImageBlocks,
    showModal: showImageModal,
    hideModal: hideImageModal
};