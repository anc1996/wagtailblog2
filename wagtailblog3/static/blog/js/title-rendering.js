(function () {
    "use strict";

    var loader = document.currentScript;
    if (!loader || window.blogTitleRenderingLoaded) return;
    window.blogTitleRenderingLoaded = true;

    var katexCss = loader.dataset.katexCss;
    var katexScript = loader.dataset.katexScript;
    var autoRenderScript = loader.dataset.katexAutoRenderScript;
    var loadPromise = null;
    var scheduled = false;

    function ensureStylesheet(url) {
        if (!url || document.querySelector('link[data-title-katex-css]')) return;
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = url;
        link.dataset.titleKatexCss = "true";
        document.head.appendChild(link);
    }

    function loadScript(url, marker) {
        return new Promise(function (resolve, reject) {
            var existing = document.querySelector('script[' + marker + ']');
            if (existing) {
                if (existing.dataset.loaded === "true") resolve();
                else existing.addEventListener("load", resolve, { once: true });
                return;
            }
            var script = document.createElement("script");
            script.src = url;
            script.setAttribute(marker, "true");
            script.addEventListener("load", function () {
                script.dataset.loaded = "true";
                resolve();
            }, { once: true });
            script.addEventListener("error", reject, { once: true });
            document.head.appendChild(script);
        });
    }

    function ensureKatex() {
        if (window.katex && window.renderMathInElement) return Promise.resolve();
        if (!loadPromise) {
            ensureStylesheet(katexCss);
            loadPromise = loadScript(katexScript, "data-title-katex-script")
                .then(function () {
                    return loadScript(autoRenderScript, "data-title-katex-auto-render");
                });
        }
        return loadPromise;
    }

    function unrenderedTitles(root) {
        var scope = root && root.querySelectorAll ? root : document;
        return Array.prototype.slice.call(
            scope.querySelectorAll('.markdown-title[data-title-math="true"]:not([data-title-math-rendered])')
        );
    }

    function renderTitles(root) {
        var titles = unrenderedTitles(root);
        if (!titles.length) return;
        ensureKatex().then(function () {
            titles.forEach(function (title) {
                if (title.dataset.titleMathRendered === "true") return;
                window.renderMathInElement(title, {
                    delimiters: [
                        { left: "\\(", right: "\\)", display: false },
                        { left: "$", right: "$", display: false }
                    ],
                    ignoredClasses: ["katex"],
                    throwOnError: false
                });
                title.dataset.titleMathRendered = "true";
            });
        }).catch(function (error) {
            if (window.console) console.error("Title KaTeX loading failed", error);
        });
    }

    function scheduleRender() {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(function () {
            scheduled = false;
            renderTitles(document);
        });
    }

    function start() {
        renderTitles(document);
        if (!document.body || !window.MutationObserver) return;
        new MutationObserver(scheduleRender).observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
}());
