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

// --- 其他推荐的配置 ---
window.wagtailMarkdown.options.lineNumbers = true;
window.wagtailMarkdown.options.spellChecker = false;
window.wagtailMarkdown.options.placeholder = "开始编写您的 Markdown...";
window.wagtailMarkdown.options.minHeight = "400px";
window.wagtailMarkdown.options.indentWithTabs = false;
window.wagtailMarkdown.options.tabSize = 4;