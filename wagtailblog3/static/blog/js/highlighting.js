/* * wagtailblog3/static/blog/js/highlighting.js
 * 纯 jQuery 架构重构版
 */
$(function() {
    console.log("🚀 Markdown Enhancer v3.0 (jQuery Mode) 启动...");

    // 函数 1: 为官方 Highlight.js 生成的代码块添加复制按钮
    function addCopyButtons() {
        // jQuery 选择器遍历所有的代码块
        $('pre > code').each(function() {
            const $code = $(this);
            const $pre = $code.parent();

            // 给 pre 加上相对定位
            $pre.css('position', 'relative');

            // jQuery 一键创建按钮、写入属性并附带 CSS 样式
            const $button = $('<button>', {
                class: 'code-copy-button',
                type: 'button',
                text: 'Copy',
                css: {
                    position: 'absolute',
                    top: '8px',
                    right: '8px',
                    padding: '4px 8px',
                    fontSize: '12px',
                    borderRadius: '4px',
                    cursor: 'pointer',
                    background: 'rgba(255,255,255,0.2)',
                    color: '#fff',
                    border: 'none'
                }
            });

            // 绑定点击复制事件
            $button.on('click', function() {
                const $btn = $(this);
                // 调用浏览器原生剪贴板 API
                navigator.clipboard.writeText($code.text()).then(function() {
                    $btn.text('Copied!').css('background', 'rgba(0,255,0,0.4)');

                    // 2秒后恢复原状
                    setTimeout(function() {
                        $btn.text('Copy').css('background', 'rgba(255,255,255,0.2)');
                    }, 2000);
                }).catch(function(err) {
                    console.error('复制失败', err);
                    $btn.text('Error');
                });
            });

            // 将按钮追加到 pre 容器中
            $pre.append($button);
        });
    }

    // 函数 2: 渲染数学公式 (这部分调用外部独立引擎，维持原状)
    function renderMath() {
        if (typeof renderMathInElement === 'function') {
            renderMathInElement(document.body, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false}
                ],
                throwOnError: false
            });
        }
    }

    // 执行增强功能
    addCopyButtons();
    renderMath();
});