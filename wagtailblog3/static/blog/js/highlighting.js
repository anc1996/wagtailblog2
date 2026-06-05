/* * wagtailblog3/static/blog/js/highlighting.js
 * 高级全栈架构：jQuery + Highlight.js 本地化动态懒加载 (v4.6 批量依赖收集与集中渲染版)
 */
$(function() {
    console.log("🚀 Code Highlighting Engine v4.6 (Batch Dependency Mode) 启动...");

    if (typeof hljs !== 'undefined') {
        hljs.configure({ ignoreUnescapedHTML: true });
    } else {
        console.error("❌ hljs 核心库未找到！请检查引入。");
        return;
    }

    const LANG_BASE_URL = '/static/blog/js/highlightjs/languages/';
    const langAliases = {
        'js': 'javascript', 'py': 'python', 'html': 'xml',
        'sh': 'bash', 'ps1': 'powershell', 'yml': 'yaml',
        'c#': 'csharp', 'c++': 'cpp'
    };

    // 复制降级方案
    function fallbackCopyTextToClipboard(text) {
        return new Promise((resolve, reject) => {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "fixed";
            textArea.style.top = "-999999px";
            textArea.style.left = "-999999px";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            try {
                const successful = document.execCommand('copy');
                if (successful) resolve();
                else reject(new Error("降级复制失败"));
            } catch (err) {
                reject(err);
            } finally {
                document.body.removeChild(textArea);
            }
        });
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        } else {
            return fallbackCopyTextToClipboard(text);
        }
    }

    // 行号生成器
    function injectLineNumbers($pre, $code) {
        if ($pre.find('.hljs-line-numbers').length > 0) return;
        let text = $code.text();
        let lines = text.split('\n');
        if (lines[lines.length - 1] === '') lines.pop();

        let numbersHtml = '';
        for (let i = 1; i <= lines.length; i++) {
            numbersHtml += i + '\n';
        }

        const $numbers = $('<div>', { class: 'hljs-line-numbers', text: numbersHtml });
        $pre.prepend($numbers);
    }

    // Copy 按钮注入器
    function injectCopyButton($pre, $code) {
        if ($pre.find('.code-copy-btn').length > 0) return;
        $pre.css('position', 'relative');
        const $btn = $('<button>', {
            class: 'code-copy-btn',
            title: '复制代码',
            html: '<i class="fa fa-copy"></i> Copy',
            css: {
                position: 'absolute', top: '8px', right: '8px',
                padding: '4px 8px', fontSize: '12px',
                color: '#8b949e', backgroundColor: '#21262d',
                border: '1px solid #30363d', borderRadius: '6px',
                cursor: 'pointer', transition: 'all 0.2s ease',
                zIndex: 10, opacity: 0.8
            }
        });

        $btn.hover(
            function() { $(this).css({ opacity: 1, borderColor: '#8b949e' }); },
            function() { $(this).css({ opacity: 0.8, borderColor: '#30363d' }); }
        );

        $btn.on('click', function() {
            const codeText = $code.text();
            copyToClipboard(codeText).then(() => {
                const originalHtml = $btn.html();
                $btn.html('<i class="fa fa-check" style="color:#3fb950;"></i> Copied!').css('borderColor', '#3fb950');
                setTimeout(() => { $btn.html(originalHtml).css('borderColor', '#30363d'); }, 2000);
            }).catch(err => {
                console.error("❌ 复制失败:", err);
                $btn.html('<i class="fa fa-times" style="color:#f85149;"></i> Failed');
            });
        });

        $pre.append($btn);
    }

    // 🌟 核心重构：收集 -> 下载 -> 等待 -> 集中渲染
    function initCodeBlocks() {
// 🔥     v4.7 升级：同时抓取 Markdown 代码块 和 Wagtail 原生代码块
        const $codeBlocks = $('.markdown-body pre > code, [data-block-type="code_block"] pre > code');
        if ($codeBlocks.length === 0) return;

        const requiredLangs = new Set();
        const blocksData = []; // 缓存解析好的 DOM，避免重复操作

        // 1. 扫描阶段：洗清标记，收集所有需要的语言
        $codeBlocks.each(function() {
            const $code = $(this);
            const $pre = $code.parent();

            if ($pre.data('hl-processed')) return;
            $pre.data('hl-processed', true);

            $pre.removeClass('highlight');
            delete this.dataset.highlighted;

            let lang = '';
            const combinedClasses = ($code.attr('class') || '') + ' ' + ($pre.attr('class') || '');
            const match = combinedClasses.match(/language-([a-zA-Z0-9\-]+)/);
            if (match && match[1]) lang = match[1].toLowerCase();

            if (langAliases[lang]) lang = langAliases[lang];

            if (lang === 'undefined' || lang === 'none') {
                $code.removeClass('language-undefined language-none hljs');
                $pre.removeClass('language-undefined language-none');
                lang = '';
            }

            if (lang) requiredLangs.add(lang);
            blocksData.push({ $pre, $code });
        });

        // 2. 下载阶段：把所有没加载过的语言放进 Promise.all 队列
        const downloadPromises = [];
        requiredLangs.forEach(lang => {
            if (!hljs.getLanguage(lang)) {
                console.log(`⏳ 加入下载队列: ${lang}...`);
                const scriptUrl = `${LANG_BASE_URL}${lang}.min.js`;
                const p = new Promise((resolve) => {
                    $.getScript(scriptUrl)
                        .done(() => {
                            console.log(`✅ 语言包装载入内存: ${lang}`);
                            resolve();
                        })
                        .fail(() => {
                            console.warn(`⚠️ 无法下载语言包: ${lang}.min.js，将回退默认渲染`);
                            resolve();
                        });
                });
                downloadPromises.push(p);
            }
        });

        // 3. 渲染阶段：死等所有下载完成，给 JS 引擎 50ms 喘息时间，然后集中轰炸渲染
        Promise.all(downloadPromises).then(() => {
            setTimeout(() => {
                console.log("🎉 所有依赖语言就绪，开始执行批量 DOM 渲染！");
                blocksData.forEach(({ $pre, $code }) => {
                    hljs.highlightElement($code[0]);
                    injectLineNumbers($pre, $code);
                    injectCopyButton($pre, $code);
                });
            }, 50); // 🌟 灵魂 50 毫秒，彻底解决渲染不到位的问题
        });
    }

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

    initCodeBlocks();
    renderMath();
});