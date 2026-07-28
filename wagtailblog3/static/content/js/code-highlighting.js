/* Shared Highlight.js loader and accessible code-block controls. */
(function(window, document) {
    'use strict';

    const CORE_URL = '/static/vendor/highlightjs/highlight.min.js';
    const LANGUAGE_BASE_URL = '/static/vendor/highlightjs/languages/';
    const COLLAPSE_AFTER_LINES = 14;
    const COLLAPSED_VISIBLE_LINES = 10;
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
    const languageLabels = {
        bash: 'Shell', cpp: 'C++', csharp: 'C#', css: 'CSS', dos: 'Command Prompt',
        go: 'Go', java: 'Java', javascript: 'JavaScript', json: 'JSON', makefile: 'Makefile',
        nginx: 'Nginx', plaintext: 'Text', powershell: 'PowerShell', python: 'Python',
        r: 'R', ruby: 'Ruby', rust: 'Rust', sql: 'SQL', typescript: 'TypeScript',
        xml: 'HTML / XML', yaml: 'YAML'
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

    function languageLabel(language) {
        return languageLabels[language] || language || 'Text';
    }

    function lineCountFor(code) {
        return code.textContent.replace(/\n$/, '').split('\n').length;
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
        const lines = lineCountFor(code);
        const numbers = document.createElement('div');
        numbers.className = 'hljs-line-numbers';
        numbers.setAttribute('aria-hidden', 'true');
        numbers.textContent = Array.from({ length: lines }, (_, index) => index + 1).join('\n');
        pre.prepend(numbers);
    }

    function ensureCodeShell(pre, language, lineCount) {
        let shell = pre.closest('.code-block-shell');
        if (shell) return shell;

        shell = document.createElement('section');
        shell.className = 'code-block-shell';
        shell.style.setProperty('--code-collapsed-lines', COLLAPSED_VISIBLE_LINES);
        shell.dataset.codeLines = String(lineCount);
        shell.setAttribute('aria-label', `${languageLabel(language)}代码，共 ${lineCount} 行`);

        const toolbar = document.createElement('div');
        toolbar.className = 'code-block-toolbar';
        const meta = document.createElement('span');
        meta.className = 'code-block-meta';
        meta.innerHTML = `<span class="code-language">${languageLabel(language)}</span><span class="code-line-count">${lineCount} 行</span>`;
        const actions = document.createElement('div');
        actions.className = 'code-block-actions';
        toolbar.append(meta, actions);

        pre.parentNode.insertBefore(shell, pre);
        shell.append(toolbar, pre);
        return shell;
    }

    function addCopyButton(shell, code) {
        if (shell.querySelector('.code-copy-btn')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'code-copy-btn';
        button.title = '复制完整代码';
        button.setAttribute('aria-label', '复制完整代码');
        button.innerHTML = '<i class="fa fa-copy" aria-hidden="true"></i><span>Copy</span>';
        button.addEventListener('click', () => {
            copyText(code.textContent).then(() => {
                button.innerHTML = '<i class="fa fa-check" aria-hidden="true"></i><span>Copied</span>';
                button.setAttribute('aria-label', '代码已复制');
                setTimeout(() => {
                    button.innerHTML = '<i class="fa fa-copy" aria-hidden="true"></i><span>Copy</span>';
                    button.setAttribute('aria-label', '复制完整代码');
                }, 1600);
            }).catch(() => {
                button.innerHTML = '<i class="fa fa-exclamation-circle" aria-hidden="true"></i><span>Copy failed</span>';
                button.setAttribute('aria-label', '代码复制失败');
            });
        });
        shell.querySelector('.code-block-actions').appendChild(button);
    }

    function scrollContainerFor(element) {
        let parent = element.parentElement;
        while (parent && parent !== document.body) {
            const style = window.getComputedStyle(parent);
            if (/(auto|scroll)/.test(style.overflowY) && parent.scrollHeight > parent.clientHeight) return parent;
            parent = parent.parentElement;
        }
        return window;
    }

    function addCollapseControl(shell, lineCount) {
        if (lineCount <= COLLAPSE_AFTER_LINES || shell.querySelector('.code-toggle-btn')) return;
        shell.classList.add('is-collapsible', 'is-collapsed');

        const fade = document.createElement('div');
        fade.className = 'code-collapse-fade';
        fade.setAttribute('aria-hidden', 'true');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'code-toggle-btn';
        button.setAttribute('aria-expanded', 'false');

        const updateButton = (expanded) => {
            button.innerHTML = expanded
                ? '<i class="fa fa-angle-up" aria-hidden="true"></i><span>收起代码</span>'
                : `<i class="fa fa-angle-down" aria-hidden="true"></i><span>展开全部 · 共 ${lineCount} 行</span>`;
            button.setAttribute('aria-label', expanded ? '收起代码' : `展开全部代码，共 ${lineCount} 行`);
        };
        updateButton(false);

        button.addEventListener('click', () => {
            const expanding = shell.classList.contains('is-collapsed');
            const scroller = scrollContainerFor(shell);
            const topBefore = shell.getBoundingClientRect().top;
            shell.classList.toggle('is-collapsed', !expanding);
            shell.classList.toggle('is-expanded', expanding);
            button.setAttribute('aria-expanded', String(expanding));
            updateButton(expanding);

            if (!expanding && topBefore < 16) {
                const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
                if (scroller === window) {
                    window.scrollTo({ top: window.scrollY + shell.getBoundingClientRect().top - 16, behavior });
                } else {
                    const scrollerTop = scroller.getBoundingClientRect().top;
                    scroller.scrollTo({ top: scroller.scrollTop + shell.getBoundingClientRect().top - scrollerTop - 16, behavior });
                }
            }
        });
        shell.append(fade, button);
    }

    async function highlightCode(code) {
        const pre = code.parentElement;
        if (!pre || pre.dataset.highlightProcessed === 'true') return;
        pre.dataset.highlightProcessed = 'true';
        pre.classList.add('code-highlight-container');

        const language = languageFor(code);
        const lineCount = lineCountFor(code);
        if (language) await loadLanguage(language);

        delete code.dataset.highlighted;
        try {
            window.hljs.highlightElement(code);
        } catch (error) {
            console.warn('Highlight.js failed for one code block', error);
        }
        const shell = ensureCodeShell(pre, language, lineCount);
        addLineNumbers(pre, code);
        addCopyButton(shell, code);
        addCollapseControl(shell, lineCount);
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
})(window, document);
