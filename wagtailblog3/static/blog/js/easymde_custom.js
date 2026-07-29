// wagtailblog3/static/js/easymde_custom.js

// --- 帮助函数 (Helpers) ---

/**
 * 将选定的文本用带特定样式的 <span> 标签包裹
 * @param {object} editor - EasyMDE 编辑器实例
 * @param {string} style - 要应用的 CSS 样式，例如 "color: red;"
 */
function applySpanStyle(editor, style) {
    var cm = editor.codemirror;
    var selection = cm.getSelection(); // 获取选中的文本
    var replacement = '<span style="' + style + '">' + selection + '</span>';
    cm.replaceSelection(replacement);
}

/**
 * 弹出一个输入框，询问自定义颜色
 * @param {object} editor - EasyMDE 编辑器实例
 */
function promptForColor(editor) {
    var color = prompt("请输入颜色值 (例如: red, #FF0000, 或 rgb(255, 0, 0)):", "");

    if (color) {
        // 这是一个非常基础的检查，以防止无效的输入
        // 只要它看起来像一个颜色值（包含字母、#、(、)），我们就尝试使用它
        if (color.match(/^(rgb\(.+\)|#|rgba\(.+\)|[a-zA-Z]+)$/)) {
            applySpanStyle(editor, 'color: ' + color + ';');
        } else {
            alert("输入的颜色格式无效。");
        }
    }
}

// --- LaTeX formula palette ---
var formulaGroups = [
    {
        name: "常用",
        items: [
            ["x^2", "平方", "$x^2$"],
            ["x_n", "下标", "$x_n$"],
            ["\\frac{}{}", "分数", "$\\frac{a}{b}$"],
            ["\\sqrt{}", "平方根", "$\\sqrt{x}$"],
            ["\\pm", "正负号", "$\\pm$"],
            ["\\infty", "无穷大", "$\\infty$"]
        ]
    },
    {
        name: "基础运算",
        items: [
            ["+", "加法", "$a+b$"],
            ["-", "减法", "$a-b$"],
            ["\\times", "乘法", "$a\\times b$"],
            ["\\div", "除法", "$a\\div b$"],
            ["=", "等于", "$a=b$"],
            ["\\neq", "不等于", "$a\\neq b$"],
            ["\\leq", "小于等于", "$a\\leq b$"],
            ["\\geq", "大于等于", "$a\\geq b$"]
        ]
    },
    {
        name: "分式与根式",
        items: [
            ["\\frac{}{}", "普通分式", "$\\frac{a}{b}$"],
            ["\\dfrac{}{}", "大分式", "$\\dfrac{a}{b}$"],
            ["\\sqrt{}", "平方根", "$\\sqrt{x}$"],
            ["\\sqrt[]{}", "n 次根", "$\\sqrt[n]{x}$"],
            ["\\frac{d}{dx}", "导数符号", "$\\frac{d}{dx}$"],
            ["\\left( \\right)", "可伸缩括号", "$\\left( x \\right)$"]
        ]
    },
    {
        name: "上下标",
        items: [
            ["x^{}", "上标", "$x^n$"],
            ["x_{}", "下标", "$x_n$"],
            ["x_{}^{}", "上下标", "$x_n^m$"],
            ["\\vec{}", "向量", "$\\vec{x}$"],
            ["\\hat{}", "单位向量", "$\\hat{x}$"],
            ["\\bar{}", "平均值", "$\\bar{x}$"]
        ]
    },
    {
        name: "求和与积分",
        items: [
            ["\\sum_{}^{}", "求和", "$\\sum_{i=1}^{n}$"],
            ["\\prod_{}^{}", "连乘", "$\\prod_{i=1}^{n}$"],
            ["\\int_{}^{}", "积分", "$\\int_a^b$"],
            ["\\iint", "二重积分", "$\\iint$"],
            ["\\oint", "曲线积分", "$\\oint$"],
            ["\\lim_{x\\to{}}", "极限", "$\\lim_{x\\to 0}$"]
        ]
    },
    {
        name: "极限与导数",
        items: [
            ["\\lim_{x\\to{}}", "极限", "$\\lim_{x\\to 0}$"],
            ["\\frac{d}{dx}{}", "一阶导数", "$\\frac{d}{dx}f(x)$"],
            ["\\frac{d^2}{dx^2}{}", "二阶导数", "$\\frac{d^2}{dx^2}f(x)$"],
            ["\\partial", "偏导符号", "$\\partial$"],
            ["\\nabla", "梯度", "$\\nabla f$"],
            ["\\propto", "正比于", "$a\\propto b$"]
        ]
    },
    {
        name: "希腊字母",
        items: [
            ["\\alpha", "alpha", "$\\alpha$"],
            ["\\beta", "beta", "$\\beta$"],
            ["\\gamma", "gamma", "$\\gamma$"],
            ["\\Delta", "Delta", "$\\Delta$"],
            ["\\lambda", "lambda", "$\\lambda$"],
            ["\\mu", "mu", "$\\mu$"],
            ["\\pi", "pi", "$\\pi$"],
            ["\\sigma", "sigma", "$\\sigma$"],
            ["\\omega", "omega", "$\\omega$"]
        ]
    }
];

function formulaPreview(element, source) {
    if (window.katex && typeof window.katex.render === "function") {
        try {
            window.katex.render(source, element, {throwOnError: false});
            return;
        } catch (error) {
            // Keep the raw source visible if a template is not supported.
        }
    }
    element.textContent = source;
}

function insertFormula(editor, source, displayMode) {
    var cm = editor.codemirror;
    var prefix = displayMode ? "$$\n" : "$";
    var suffix = displayMode ? "\n$$" : "$";
    var text = prefix + source + suffix;
    var start = cm.getCursor();
    cm.replaceSelection(text);
    var marker = text.indexOf("{}");
    if (marker !== -1) {
        var from = CodeMirror.Pos(start.line, start.ch + marker + 1);
        cm.setSelection(from, from);
    } else {
        cm.setCursor(CodeMirror.Pos(start.line, start.ch + text.length));
    }
    cm.focus();
}

function closeFormulaPalette() {
    var palette = document.querySelector(".mde-formula-palette");
    if (palette) palette.remove();
}

function openFormulaPalette(editor, button) {
    closeFormulaPalette();
    var palette = document.createElement("div");
    palette.className = "mde-formula-palette";
    palette.setAttribute("role", "dialog");
    palette.setAttribute("aria-label", "公式面板");
    var tabs = document.createElement("div");
    tabs.className = "mde-formula-tabs";
    var body = document.createElement("div");
    body.className = "mde-formula-body";
    palette.appendChild(tabs);
    palette.appendChild(body);

    function renderGroup(index) {
        tabs.querySelectorAll("button").forEach(function(tab, tabIndex) {
            tab.classList.toggle("is-active", tabIndex === index);
        });
        body.innerHTML = "";
        formulaGroups[index].items.forEach(function(item) {
            var cell = document.createElement("button");
            cell.type = "button";
            cell.className = "mde-formula-cell";
            cell.title = item[1] + "：" + item[0];
            var preview = document.createElement("span");
            preview.className = "mde-formula-preview";
            formulaPreview(preview, item[2].replace(/^\$|\$$/g, ""));
            var label = document.createElement("span");
            label.className = "mde-formula-label";
            label.textContent = item[1];
            cell.appendChild(preview);
            cell.appendChild(label);
            cell.addEventListener("click", function(event) {
                event.preventDefault();
                insertFormula(editor, item[0], false);
                closeFormulaPalette();
            });
            body.appendChild(cell);
        });
    }

    formulaGroups.forEach(function(group, index) {
        var tab = document.createElement("button");
        tab.type = "button";
        tab.textContent = group.name;
        tab.addEventListener("click", function() { renderGroup(index); });
        tabs.appendChild(tab);
    });
    document.body.appendChild(palette);
    var rect = button.getBoundingClientRect();
    palette.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 560)) + "px";
    palette.style.top = (rect.bottom + 8) + "px";
    renderGroup(0);
    setTimeout(function() {
        document.addEventListener("click", function outside(event) {
            if (!palette.contains(event.target) && event.target !== button) {
                closeFormulaPalette();
                document.removeEventListener("click", outside);
            }
        });
        document.addEventListener("keydown", function escape(event) {
            if (event.key === "Escape") {
                closeFormulaPalette();
                document.removeEventListener("keydown", escape);
            }
        });
    }, 0);
}

// --- EasyMDE 全局配置 ---

// 确保全局对象存在
window.wagtailMarkdown = window.wagtailMarkdown || {};
window.wagtailMarkdown.options = window.wagtailMarkdown.options || {};

// --- 关键：定义我们的全功能工具栏 ---
window.wagtailMarkdown.options.toolbar = [
    'heading',
    'bold',
    'italic',
    'strikethrough',

    // === 这是您要求的高级颜色下拉按钮 ===
    {
        name: "color-picker",       // 按钮的唯一名称
        className: "fa fa-paint-brush", // 父按钮的图标 (调色板)
        title: "文本颜色",            // 鼠标悬停时的提示

        // --- 定义下拉子菜单 ---
        children: [
            {
                name: "color-red",
                text: "R", // 在下拉菜单中显示 "R"
                className: "mde-color-red", // 自定义 CSS 类
                action: function(editor) { applySpanStyle(editor, 'color: red;'); },
                title: "红色"
            },
            {
                name: "color-green",
                text: "G",
                className: "mde-color-green",
                action: function(editor) { applySpanStyle(editor, 'color: green;'); },
                title: "绿色"
            },
            {
                name: "color-blue",
                text: "B",
                className: "mde-color-blue",
                action: function(editor) { applySpanStyle(editor, 'color: blue;'); },
                title: "蓝色"
            },
            {
                name: "color-custom",
                text: "RGB",
                className: "mde-color-custom",
                action: promptForColor, // 使用我们上面定义的函数
                title: "自定义颜色 (RGB/Hex)..."
            }
        ]
    },
    {
        name: "formula",
        text: "fx",
        className: "mde-formula-button",
        title: "插入公式",
        action: function(editor, event) {
            var button = event && event.currentTarget ? event.currentTarget : document.querySelector(".mde-formula-button");
            if (button) openFormulaPalette(editor, button);
        }
    },
    // ==================================

    '|', // 分隔符
    'quote',
    'code',
    'link',
    'image',
    'table',
    'horizontal-rule',
    '|',
    'unordered-list',
    'ordered-list',
    '|',
    'preview',
    'side-by-side',
    'fullscreen',
    'guide'
];


// ==========================================================
// 后台 EasyMDE 预览区动态渲染接管 (代码高亮 + KaTeX) - jQuery 架构版
// ==========================================================
$(document).ready(function() {
    let renderTimeout;

    // MutationObserver 必须是原生 API，用于监听底层 DOM 重绘
    const observer = new MutationObserver(function(mutations) {
        let needsRender = false;

        for (let mutation of mutations) {
            const $target = $(mutation.target);

            // 只要变动节点的自身或其祖先包含预览区类名，即触发重新渲染
            if ($target.hasClass('editor-preview-side') ||
                $target.hasClass('editor-preview') ||
                $target.closest('.editor-preview-side, .editor-preview').length > 0) {
                needsRender = true;
                break;
            }
        }

        if (needsRender) {
            // 300ms 防抖
            clearTimeout(renderTimeout);
            renderTimeout = setTimeout(function() {
                // 用 jQuery 精准获取当前激活的预览容器
                const $previews = $('.editor-preview-active, .editor-preview-active-side');

                $previews.each(function() {
                    // this 指向当前遍历到的原生 DOM 元素

                    // 1. 唤醒数学公式渲染 (KaTeX 需要传入原生 DOM 节点)
                    if (typeof renderMathInElement === 'function') {
                        renderMathInElement(this, {
                            delimiters: [
                                {left: "$$", right: "$$", display: true},
                                {left: "\\[", right: "\\]", display: true},
                                {left: "$", right: "$", display: false},
                                {left: "\\(", right: "\\)", display: false}
                            ],
                            throwOnError: false
                        });
                    }

                    // 2. 唤醒代码块高亮 (Highlight.js)
                    if (typeof hljs !== 'undefined') {
                        $(this).find('pre code').each(function() {
                            // 避免重复高亮
                            if (!$(this).hasClass('hljs')) {
                                hljs.highlightElement(this); // this 指向对应的 code 原生节点
                            }
                        });
                    }
                });
            }, 300);
        }
    });

    // 监听整个 body 的子元素变动
    observer.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true
    });
});

// --- 其他推荐的配置 ---
window.wagtailMarkdown.options.lineNumbers = true;
window.wagtailMarkdown.options.spellChecker = false;
window.wagtailMarkdown.options.placeholder = "开始编写您的 Markdown...";
window.wagtailMarkdown.options.minHeight = "400px";
window.wagtailMarkdown.options.indentWithTabs = false;
window.wagtailMarkdown.options.tabSize = 4;
