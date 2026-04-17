// wagtailblog3/static/blog/js/highlighting.js
// 全新的增强脚本，请完整替换旧文件

document.addEventListener('DOMContentLoaded', () => {
    console.log("🚀 Markdown Enhancer v2.0 启动...");

    // 函数 1: 添加代码复制按钮
    function addCopyButtons() {
        const codeBlocks = document.querySelectorAll('div.highlight');
        codeBlocks.forEach((block, index) => {
            const button = document.createElement('button');
            button.className = 'code-copy-button';
            button.type = 'button';
            button.innerText = 'Copy';

            button.addEventListener('click', () => {
                // 找到 pre > code 或者 pre 元素来获取文本
                const codeElement = block.querySelector('pre');
                if (codeElement) {
                    navigator.clipboard.writeText(codeElement.innerText).then(() => {
                        button.innerText = 'Copied!';
                        button.classList.add('copied');
                        setTimeout(() => {
                            button.innerText = 'Copy';
                            button.classList.remove('copied');
                        }, 2000);
                    }).catch(err => {
                        console.error('复制失败', err);
                        button.innerText = 'Error';
                    });
                }
            });

            block.appendChild(button);
        });
        if (codeBlocks.length > 0) {
            console.log(`📋 为 ${codeBlocks.length} 个代码块添加了复制按钮`);
        }
    }

    // 函数 2: 渲染数学公式
    function renderMath() {
        if (typeof renderMathInElement === 'function') {
            renderMathInElement(document.body, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false}
                ],
                throwOnError: false
            });
            console.log('🧮 数学公式渲染完成');
        }
    }

    // 执行所有增强功能
    addCopyButtons();
    renderMath();

    console.log("🎉 Markdown 增强完成!");
});