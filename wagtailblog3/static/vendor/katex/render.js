(function () {
    "use strict";

    var delimiters = [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true }
    ];

    function render(root) {
        if (!window.renderMathInElement || !window.katex) return;

        var target = root || document;
        window.renderMathInElement(target, {
            delimiters: delimiters,
            throwOnError: false,
            ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
            ignoredClasses: ["katex"]
        });
    }

    window.renderBlogMath = render;
    document.addEventListener("DOMContentLoaded", function () {
        render(document);
    });
})();
