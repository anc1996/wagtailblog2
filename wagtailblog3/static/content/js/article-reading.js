const ARTICLE_SELECTOR = '[data-article-reading]';
const CONTENT_SELECTOR = '[data-reading-content]';

function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(value);
    }

    return new Promise((resolve, reject) => {
        const textarea = document.createElement('textarea');
        textarea.value = value;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();

        const copied = document.execCommand('copy');
        textarea.remove();
        copied ? resolve() : reject(new Error('copy failed'));
    });
}

function createReadingChrome(article) {
    if (document.querySelector('.article-reading-progress')) return null;

    const progress = document.createElement('div');
    progress.className = 'article-reading-progress';
    progress.setAttribute('role', 'progressbar');
    progress.setAttribute('aria-label', '文章阅读进度');
    progress.setAttribute('aria-valuemin', '0');
    progress.setAttribute('aria-valuemax', '100');
    progress.setAttribute('aria-valuenow', '0');
    progress.innerHTML = '<span class="article-reading-progress__bar"></span>';

    const backToTop = document.createElement('button');
    backToTop.type = 'button';
    backToTop.className = 'article-back-to-top';
    backToTop.title = '返回文章开头';
    backToTop.setAttribute('aria-label', '返回文章开头');
    backToTop.innerHTML = '<i class="fa fa-arrow-up" aria-hidden="true"></i>';

    document.body.append(progress, backToTop);

    return {
        article,
        progress,
        bar: progress.firstElementChild,
        backToTop
    };
}

function getScrollContext(article) {
    const container = article.querySelector('.article-scroll-container');
    if (!container) return { target: window, internal: false };

    const style = window.getComputedStyle(container);
    const isScrollable = /auto|scroll/.test(style.overflowY)
        && container.scrollHeight > container.clientHeight + 8;

    return isScrollable
        ? { target: container, internal: true }
        : { target: window, internal: false };
}

function getReadingRatio(state, context) {
    if (context.internal) {
        const available = context.target.scrollHeight - context.target.clientHeight;
        return available > 0 ? context.target.scrollTop / available : 0;
    }

    const rect = state.article.getBoundingClientRect();
    const articleTop = window.scrollY + rect.top;
    const articleHeight = state.article.offsetHeight;
    const start = articleTop - window.innerHeight * 0.18;
    const end = articleTop + articleHeight - window.innerHeight * 0.72;

    return (window.scrollY - start) / Math.max(end - start, 1);
}

function initializeProgress(state) {
    let context = getScrollContext(state.article);
    let scheduled = false;

    const update = () => {
        scheduled = false;
        const ratio = Math.min(Math.max(getReadingRatio(state, context), 0), 1);
        const percentage = Math.round(ratio * 100);

        state.bar.style.transform = `scaleX(${ratio})`;
        state.progress.setAttribute('aria-valuenow', String(percentage));
        state.backToTop.classList.toggle('is-visible', ratio > 0.2);
    };

    const requestUpdate = () => {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(update);
    };

    context.target.addEventListener('scroll', requestUpdate, { passive: true });
    window.addEventListener('resize', () => {
        const nextContext = getScrollContext(state.article);
        if (nextContext.target !== context.target) {
            context.target.removeEventListener('scroll', requestUpdate);
            context = nextContext;
            context.target.addEventListener('scroll', requestUpdate, { passive: true });
        }
        requestUpdate();
    }, { passive: true });

    state.backToTop.addEventListener('click', () => {
        if (context.internal) {
            context.target.scrollTo({ top: 0, behavior: 'smooth' });
        } else {
            state.article.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    });

    update();
}

function initializeHeadingLinks(content) {
    content.querySelectorAll('h2, h3').forEach((heading, index) => {
        if (!heading.id) heading.id = `article-heading-${index}`;
        if (heading.querySelector('.article-heading-link')) return;

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'article-heading-link';
        button.title = '复制此章节链接';
        button.setAttribute('aria-label', `复制章节链接：${heading.textContent.trim()}`);
        button.innerHTML = '<i class="fa fa-link" aria-hidden="true"></i>';

        button.addEventListener('click', () => {
            const url = new URL(window.location.href);
            url.hash = heading.id;

            copyText(url.toString()).then(() => {
                button.classList.add('is-copied');
                button.title = '链接已复制';
                window.history.replaceState(null, '', `#${heading.id}`);
                window.setTimeout(() => {
                    button.classList.remove('is-copied');
                    button.title = '复制此章节链接';
                }, 1500);
            }).catch(() => {
                window.location.hash = heading.id;
            });
        });

        heading.appendChild(button);
    });
}

function initializeExternalLinks(content) {
    content.querySelectorAll('a[href]').forEach((link) => {
        if (link.matches('[data-pswp-item], .content-image__original')) return;

        let url;
        try {
            url = new URL(link.href, window.location.href);
        } catch (error) {
            return;
        }

        if (!/^https?:$/.test(url.protocol) || url.origin === window.location.origin) return;

        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.classList.add('article-external-link');
    });
}

function initializeArticleReading(article) {
    if (article.dataset.articleReadingReady === 'true') return;

    const content = article.querySelector(CONTENT_SELECTOR);
    if (!content) return;

    article.dataset.articleReadingReady = 'true';
    const state = createReadingChrome(article);
    if (state) initializeProgress(state);
    initializeHeadingLinks(content);
    initializeExternalLinks(content);
}

function initialize() {
    document.querySelectorAll(ARTICLE_SELECTOR).forEach(initializeArticleReading);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
} else {
    initialize();
}
