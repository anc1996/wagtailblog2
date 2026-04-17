// static/blog/js/video_blocks.js
// Gretzia风格视频块增强功能 - 缩略图预览 + 模态框播放

$(document).ready(function() {
    initializeGretziaVideoBlocks();
});

function initializeGretziaVideoBlocks() {
    // 获取所有视频预览块
    var $videoBlocks = $('.content-block-wrapper[data-block-type="video_block"]');

    $videoBlocks.each(function() {
        var $block = $(this);
        var $videoPreview = $block.find('.gretzia-video-preview');

        if ($videoPreview.length === 0) return;

        // 设置视频预览功能
        setupVideoPreview($videoPreview);

        // 优化视频缩略图加载
        optimizeVideoThumbnail($videoPreview);
    });

    // 初始化键盘快捷键
    setupKeyboardShortcuts();
}

function setupVideoPreview($videoPreview) {
    // 点击预览时打开模态框
    $videoPreview.on('click', function(e) {
        e.preventDefault();

        var videoUrl = $(this).attr('data-video-url');
        var videoType = $(this).attr('data-video-type') || 'video/mp4';
        var videoTitle = $(this).attr('data-video-title') || '视频播放';
        var videoWidth = $(this).attr('data-video-width');
        var videoHeight = $(this).attr('data-video-height');

        if (videoUrl) {
            openVideoModal(videoUrl, videoType, videoTitle, videoWidth, videoHeight);
        }
    });

    // 添加悬停效果增强
    $videoPreview.on('mouseenter', function() {
        $(this).find('.gretzia-play-button').addClass('pulse');
    }).on('mouseleave', function() {
        $(this).find('.gretzia-play-button').removeClass('pulse');
    });
}

function openVideoModal(videoUrl, videoType, videoTitle, videoWidth, videoHeight) {
    // 创建模态框HTML
    var modalHtml = `
        <div class="gretzia-video-modal">
            <div class="gretzia-video-modal-content">
                <span class="gretzia-video-modal-close">&times;</span>
                <video controls autoplay>
                    <source src="${videoUrl}" type="${videoType}">
                    <p>您的浏览器不支持视频播放。<a href="${videoUrl}">点击这里下载视频文件</a></p>
                </video>
                ${videoTitle ? `<div class="gretzia-video-modal-caption">${videoTitle}</div>` : ''}
                <div class="gretzia-video-controls">
                    <button class="gretzia-video-play-pause" title="播放/暂停 (空格键)">
                        <i class="fa fa-pause"></i>
                    </button>
                    <button class="gretzia-video-fullscreen" title="全屏 (F键)">
                        <i class="fa fa-expand"></i>
                    </button>
                    <button class="gretzia-video-mute" title="静音 (M键)">
                        <i class="fa fa-volume-up"></i>
                    </button>
                </div>
            </div>
        </div>
    `;

    // 将模态框添加到页面
    var $modal = $(modalHtml);
    $('body').append($modal);

    // 设置初始尺寸
    var $modalContent = $modal.find('.gretzia-video-modal-content');
    var $video = $modal.find('video');

    if (videoWidth && videoHeight) {
        // 计算合适的显示尺寸
        var maxWidth = $(window).width() * 0.9;
        var maxHeight = $(window).height() * 0.8;
        var aspectRatio = videoWidth / videoHeight;

        var displayWidth = Math.min(videoWidth, maxWidth);
        var displayHeight = Math.min(videoHeight, maxHeight);

        // 保持宽高比
        if (displayWidth / displayHeight > aspectRatio) {
            displayWidth = displayHeight * aspectRatio;
        } else {
            displayHeight = displayWidth / aspectRatio;
        }

        $modalContent.css({
            'width': displayWidth + 'px',
            'height': 'auto'
        });
    }

    // 显示模态框
    setTimeout(function() {
        $modal.addClass('active');

        // 聚焦视频元素以支持键盘控制
        $video.focus();
    }, 10);

    // 设置模态框事件
    setupModalEvents($modal);

    // 设置视频控制
    setupVideoControls($modal);

    // 设置拖拽功能
    setupModalDragging($modal);
}

function setupModalEvents($modal) {
    var $modalContent = $modal.find('.gretzia-video-modal-content');
    var $video = $modal.find('video');

    // 关闭按钮
    $modal.find('.gretzia-video-modal-close').on('click', function() {
        closeVideoModal($modal);
    });

    // 点击背景关闭
    $modal.on('click', function(e) {
        if (e.target === this) {
            closeVideoModal($modal);
        }
    });

    // ESC键关闭
    $(document).on('keydown.gretzia-video', function(e) {
        if (e.key === 'Escape') {
            closeVideoModal($modal);
        }
    });

    // 防止视频区域点击冒泡
    $modalContent.on('click', function(e) {
        e.stopPropagation();
    });
}

function setupVideoControls($modal) {
    var $video = $modal.find('video')[0];
    var $playPauseBtn = $modal.find('.gretzia-video-play-pause');
    var $fullscreenBtn = $modal.find('.gretzia-video-fullscreen');
    var $muteBtn = $modal.find('.gretzia-video-mute');

    // 播放/暂停控制
    $playPauseBtn.on('click', function() {
        if ($video.paused) {
            $video.play();
            $(this).find('i').removeClass('fa-play').addClass('fa-pause');
        } else {
            $video.pause();
            $(this).find('i').removeClass('fa-pause').addClass('fa-play');
        }
    });

    // 全屏控制
    $fullscreenBtn.on('click', function() {
        if ($video.requestFullscreen) {
            $video.requestFullscreen();
        } else if ($video.webkitRequestFullscreen) {
            $video.webkitRequestFullscreen();
        } else if ($video.mozRequestFullScreen) {
            $video.mozRequestFullScreen();
        }
    });

    // 静音控制
    $muteBtn.on('click', function() {
        if ($video.muted) {
            $video.muted = false;
            $(this).find('i').removeClass('fa-volume-off').addClass('fa-volume-up');
        } else {
            $video.muted = true;
            $(this).find('i').removeClass('fa-volume-up').addClass('fa-volume-off');
        }
    });

    // 视频事件监听
    $($video).on('play', function() {
        $playPauseBtn.find('i').removeClass('fa-play').addClass('fa-pause');
    }).on('pause', function() {
        $playPauseBtn.find('i').removeClass('fa-pause').addClass('fa-play');
    }).on('volumechange', function() {
        var $icon = $muteBtn.find('i');
        if (this.muted || this.volume === 0) {
            $icon.removeClass('fa-volume-up fa-volume-down').addClass('fa-volume-off');
        } else if (this.volume < 0.5) {
            $icon.removeClass('fa-volume-up fa-volume-off').addClass('fa-volume-down');
        } else {
            $icon.removeClass('fa-volume-down fa-volume-off').addClass('fa-volume-up');
        }
    });
}

function setupModalDragging($modal) {
    var $modalContent = $modal.find('.gretzia-video-modal-content');
    var isDragging = false;
    var dragStart = { x: 0, y: 0 };
    var modalStart = { x: 0, y: 0 };

    $modalContent.on('mousedown', function(e) {
        // 只在标题栏或边框区域开始拖拽
        if (e.target === this || $(e.target).hasClass('gretzia-video-modal-caption')) {
            isDragging = true;
            dragStart.x = e.clientX;
            dragStart.y = e.clientY;

            var offset = $modalContent.offset();
            modalStart.x = offset.left;
            modalStart.y = offset.top;

            $modalContent.addClass('dragging');
            e.preventDefault();
        }
    });

    $(document).on('mousemove.gretzia-drag', function(e) {
        if (isDragging) {
            var deltaX = e.clientX - dragStart.x;
            var deltaY = e.clientY - dragStart.y;

            $modalContent.css({
                'position': 'absolute',
                'left': modalStart.x + deltaX + 'px',
                'top': modalStart.y + deltaY + 'px',
                'margin': '0'
            });
        }
    });

    $(document).on('mouseup.gretzia-drag', function() {
        if (isDragging) {
            isDragging = false;
            $modalContent.removeClass('dragging');
        }
    });
}

function setupKeyboardShortcuts() {
    $(document).on('keydown.gretzia-video-global', function(e) {
        var $activeModal = $('.gretzia-video-modal.active');
        if ($activeModal.length === 0) return;

        var $video = $activeModal.find('video')[0];

        switch(e.key) {
            case ' ':
            case 'Enter':
                e.preventDefault();
                if ($video.paused) {
                    $video.play();
                } else {
                    $video.pause();
                }
                break;
            case 'f':
            case 'F':
                e.preventDefault();
                $activeModal.find('.gretzia-video-fullscreen').click();
                break;
            case 'm':
            case 'M':
                e.preventDefault();
                $activeModal.find('.gretzia-video-mute').click();
                break;
            case 'ArrowLeft':
                e.preventDefault();
                $video.currentTime = Math.max(0, $video.currentTime - 10);
                break;
            case 'ArrowRight':
                e.preventDefault();
                $video.currentTime = Math.min($video.duration, $video.currentTime + 10);
                break;
            case 'ArrowUp':
                e.preventDefault();
                $video.volume = Math.min(1, $video.volume + 0.1);
                break;
            case 'ArrowDown':
                e.preventDefault();
                $video.volume = Math.max(0, $video.volume - 0.1);
                break;
        }
    });
}

function closeVideoModal($modal) {
    var $video = $modal.find('video')[0];

    // 暂停视频播放
    if ($video) {
        $video.pause();
    }

    // 移除事件监听
    $(document).off('keydown.gretzia-video');
    $(document).off('mousemove.gretzia-drag');
    $(document).off('mouseup.gretzia-drag');

    // 淡出并移除模态框
    $modal.removeClass('active');
    setTimeout(function() {
        $modal.remove();
    }, 300);
}

function optimizeVideoThumbnail($videoPreview) {
    var $videoBackground = $videoPreview.find('.gretzia-video-background video');

    if ($videoBackground.length > 0) {
        // 设置视频加载优化
        $videoBackground.attr('preload', 'metadata');

        // 视频加载完成后的处理
        $videoBackground.on('loadedmetadata', function() {
            // 可以在这里添加更多的优化逻辑
            this.currentTime = 1; // 跳转到第1秒作为缩略图
        });
    }

    // 为预览添加加载动画
    $videoPreview.addClass('loading');

    // 模拟加载完成
    setTimeout(function() {
        $videoPreview.removeClass('loading').addClass('loaded');
    }, 500);
}

// 添加CSS动画类
$('<style>')
    .prop('type', 'text/css')
    .html(`
        .gretzia-play-button.pulse {
            animation: gretziaButtonPulse 1s infinite;
        }
        
        @keyframes gretziaButtonPulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
        
        .gretzia-video-preview.loading {
            opacity: 0.7;
        }
        
        .gretzia-video-preview.loaded {
            opacity: 1;
            transition: opacity 0.3s ease;
        }
    `)
    .appendTo('head');

// 导出函数供外部使用
window.GretziaVideoBlocks = {
    initialize: initializeGretziaVideoBlocks,
    openModal: openVideoModal,
    closeModal: closeVideoModal
};