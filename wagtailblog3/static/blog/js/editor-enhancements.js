$(document).ready(function() {
    console.log('MongoDB内容编辑器增强已加载');

    // 强制初始化StreamField
    var initStreamField = function() {
        // 查找所有StreamField容器
        const streamFields = $('[data-streamfield]');
        console.log('找到 ' + streamFields.length + ' 个StreamField字段');

        if (streamFields.length > 0) {
            // 查找添加块按钮
            const addButtons = $('.action-add-block-h2, .action-add-block');
            console.log('找到 ' + addButtons.length + ' 个添加块按钮');

            if (addButtons.length === 0) {
                // 如果没有找到添加按钮，可能需要触发初始化
                console.log('未找到添加块按钮，尝试强制初始化...');

                // 检查是否是空的StreamField
                const emptyFields = $('.empty-stream-field');
                if (emptyFields.length > 0) {
                    console.log('找到空StreamField，尝试模拟点击添加按钮');

                    // 尝试模拟点击默认添加按钮
                    const emptyAddButtons = $('.stream-menu-closed');
                    if (emptyAddButtons.length > 0) {
                        // 触发显示按钮
                        emptyAddButtons.first().removeClass('stream-menu-closed').addClass('stream-menu-open');
                        console.log('已打开StreamField菜单');
                    }
                }
            }
        }
    };

    // 初始尝试
    setTimeout(initStreamField, 500);
    // 再次尝试，以防初始加载延迟
    setTimeout(initStreamField, 1500);

    // 监控DOM变化，处理动态加载的编辑器
    var observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (mutation.addedNodes && mutation.addedNodes.length > 0) {
                // 检查是否有新的StreamField相关元素添加
                setTimeout(initStreamField, 200);
            }
        });
    });

    // 开始观察document body的变化
    observer.observe(document.body, { childList: true, subtree: true });

    // Markdown编辑器支持代码
    if(window.wagtailMarkdown) {
        console.log('发现wagtailMarkdown，正在配置...');
        // Markdown编辑器配置...
    }
});