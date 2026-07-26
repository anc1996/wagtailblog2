/* Shared Highlight.js loader for article, comment, and future content modules. */
(function(window, document, $) {
    'use strict';

    const CORE_URL = '/static/vendor/highlightjs/highlight.min.js';
    const LANGUAGE_BASE_URL = '/static/vendor/highlightjs/languages/';
    const CODE_SELECTOR = [
        '.markdown-body pre > code',
        '[data-block-type="code_block"] pre > code',
        '.comment-markdown pre > code',
        '[data-code-highlight] pre > code'
    ].join(', ');
    const languageAliases = {
        js: 'javascript', py: 'python', html: 'xml', htm: 'xml',
        sh: 'bash', shell: 'bash', ps1: 'powershell', yml: 'yaml',
        cmd: 'dos', text: 'plaintext', txt: 'plaintext', md: 'markdown',
        csv: 'plaintext', mermaid: 'plaintext', 'c#': 'csharp', 'c++': 'cpp'
    };
    const languageLoads = new Map();
    let coreLoad = null;
    let configured = false;

    function ensureHighlighter() {
        if (window.hljs) return Promise.resolve(window.hljs);
        if (coreLoad) return coreLoad;
        coreLoad = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = CORE_URL;
            script.async = true;
            script.onload = () => resolve(window.hljs);
            script.onerror = () => reject(new Error('Highlight.js core failed to load'));
            document.head.appendChild(script);
        });
        return coreLoad;
    }

    function configureHighlighter() {
        if (!configured && window.hljs) {
            window.hljs.configure({ ignoreUnescapedHTML: true });
            configured = true;
        }
    }

    function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise((resolve, reject) => {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.left = '-9999px';
            document.body.appendChild(textarea);
            textarea.select();
            try {
                document.execCommand('copy') ? resolve() : reject(new Error('copy failed'));
            } catch (error) {
                reject(error);
            } finally {
                textarea.remove();
            }
        });
    }

    function languageFor(code) {
        const classes = `${code.className || ''} ${code.parentElement?.className || ''}`;
        const match = classes.match(/(?:lang(?:uage)?)-([a-zA-Z0-9#+.-]+)/i);
        if (!match) return '';
        const raw = match[1].toLowerCase();
        return languageAliases[raw] || raw;
    }

    function loadLanguage(language) {
        if (!language || window.hljs.getLanguage(language)) return Promise.resolve();
        if (languageLoads.has(language)) return languageLoads.get(language);

        const promise = new Promise((resolve) => {
            const script = document.createElement('script');
            script.src = `${LANGUAGE_BASE_URL}${language}.min.js`;
            script.async = true;
            script.onload = resolve;
            script.onerror = () => {
                console.warn(`Highlight.js language unavailable: ${language}`);
                resolve();
            };
            document.head.appendChild(script);
        });
        languageLoads.set(language, promise);
        return promise;
    }

    function addLineNumbers(pre, code) {
        if (pre.querySelector('.hljs-line-numbers')) return;
        const lines = code.textContent.replace(/\n$/, '').split('\n').length;
        const numbers = document.createElement('div');
        numbers.className = 'hljs-line-numbers';
        numbers.setAttribute('aria-hidden', 'true');
        numbers.textContent = Array.from({ length: lines }, (_, index) => index + 1).join('\n');
        pre.prepend(numbers);
    }

    function addCopyButton(pre, code) {
        if (pre.querySelector('.code-copy-btn')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'code-copy-btn';
        button.title = '复制代码';
        button.innerHTML = '<i class="fa fa-copy" aria-hidden="true"></i> Copy';
        button.addEventListener('click', () => {
            copyText(code.textContent).then(() => {
                button.innerHTML = '<i class="fa fa-check" aria-hidden="true"></i> Copied';
                setTimeout(() => {
                    button.innerHTML = '<i class="fa fa-copy" aria-hidden="true"></i> Copy';
                }, 1600);
            }).catch(() => {
                button.textContent = 'Copy failed';
            });
        });
        pre.appendChild(button);
    }

    async function highlightCode(code) {
        const pre = code.parentElement;
        if (!pre || pre.dataset.highlightProcessed === 'true') return;
        pre.dataset.highlightProcessed = 'true';
        pre.classList.add('code-highlight-container');

        const language = languageFor(code);
        if (language) await loadLanguage(language);

        delete code.dataset.highlighted;
        try {
            window.hljs.highlightElement(code);
        } catch (error) {
            console.warn('Highlight.js failed for one code block', error);
        }
        addLineNumbers(pre, code);
        addCopyButton(pre, code);
    }

    async function highlightWithin(root) {
        const scope = root && root.querySelectorAll ? root : document;
        const nodes = [];
        if (scope.matches && scope.matches(CODE_SELECTOR)) nodes.push(scope);
        nodes.push(...scope.querySelectorAll(CODE_SELECTOR));
        if (!nodes.length) return [];

        try {
            await ensureHighlighter();
            configureHighlighter();
            return Promise.all(nodes.map(highlightCode));
        } catch (error) {
            console.error(error);
            return [];
        }
    }

    function observeDynamicContent() {
        if (!window.MutationObserver || !document.body) return;
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) highlightWithin(node);
                });
            });
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    function start() {
        window.ContentHighlighter = { highlightWithin };
        highlightWithin(document);
        observeDynamicContent();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})(window, document, window.jQuery);
