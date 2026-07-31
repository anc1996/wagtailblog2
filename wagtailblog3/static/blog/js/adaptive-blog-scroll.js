/* Adaptive three-column scrolling for the blog article page. */
(function () {
    'use strict';

    const DESKTOP_MIN = 1280;
    const layout = document.getElementById('blog-layout-container');
    const article = document.getElementById('article-scroll-container');
    const toc = document.querySelector('.toc-wrapper');
    const sidebar = document.querySelector('.sidebar-scroll-area');
    if (!layout || !article || !toc || !sidebar) return;

    let frame = 0;
    let lastMode = '';

    function isDesktop() {
        return window.innerWidth >= DESKTOP_MIN;
    }

    function clearState() {
        layout.classList.remove('adaptive-scroll-ready', 'article-is-scrollable', 'toc-is-scrollable', 'sidebar-is-scrollable');
        article.classList.remove('is-scrollable');
        toc.classList.remove('is-scrollable');
        sidebar.classList.remove('is-scrollable');
    }

    function measureOverflow(element) {
        return element.scrollHeight > element.clientHeight + 2;
    }

    function update() {
        frame = 0;

        if (!isDesktop()) {
            clearState();
            return;
        }

        // Measure natural-flow content first. This prevents short articles from
        // being placed inside an artificial viewport-sized scroll box.
        clearState();
        layout.classList.add('adaptive-scroll-ready');

        const viewportHeight = Math.max(320, window.innerHeight - 112);
        const articleNaturalHeight = article.scrollHeight;
        const tocNaturalHeight = toc.scrollHeight;
        const sidebarNaturalHeight = sidebar.scrollHeight;

        const articleNeedsScroll = articleNaturalHeight > viewportHeight + 2;
        const tocNeedsScroll = tocNaturalHeight > viewportHeight + 2;
        const sidebarNeedsScroll = sidebarNaturalHeight > viewportHeight + 2;
        const needsViewportLayout = articleNeedsScroll || tocNeedsScroll || sidebarNeedsScroll;

        if (needsViewportLayout) {
            layout.classList.add('adaptive-scroll-ready');
        } else {
            layout.classList.remove('adaptive-scroll-ready');
        }

        if (articleNeedsScroll) {
            layout.classList.add('article-is-scrollable');
            article.classList.add('is-scrollable');
        }
        if (tocNeedsScroll) {
            layout.classList.add('toc-is-scrollable');
            toc.classList.add('is-scrollable');
        }
        if (sidebarNeedsScroll) {
            layout.classList.add('sidebar-is-scrollable');
            sidebar.classList.add('is-scrollable');
        }

        const mode = [articleNeedsScroll, tocNeedsScroll, sidebarNeedsScroll].map(Boolean).join('');
        if (mode !== lastMode) {
            lastMode = mode;
            layout.dispatchEvent(new CustomEvent('adaptive-scroll-updated', {
                detail: {
                    article: articleNeedsScroll,
                    toc: tocNeedsScroll,
                    sidebar: sidebarNeedsScroll
                }
            }));
        }
    }

    function scheduleUpdate() {
        if (frame) return;
        frame = window.requestAnimationFrame(update);
    }

    window.blogAdaptiveScroll = {
        update: scheduleUpdate,
        measure: scheduleUpdate
    };

    window.addEventListener('resize', scheduleUpdate, { passive: true });
    window.addEventListener('orientationchange', scheduleUpdate, { passive: true });

    const observer = new MutationObserver(scheduleUpdate);
    observer.observe(document.getElementById('toc-content') || toc, { childList: true, subtree: true });
    observer.observe(article, { childList: true, subtree: true });
    observer.observe(sidebar, { childList: true, subtree: true });

    // Images, embeds and fonts can change measurements after initial layout.
    window.addEventListener('load', scheduleUpdate, { once: true });
    document.querySelectorAll('img, iframe, video').forEach(function (media) {
        media.addEventListener('load', scheduleUpdate, { passive: true });
    });

    scheduleUpdate();
    window.setTimeout(scheduleUpdate, 250);
    window.setTimeout(scheduleUpdate, 900);
})();
