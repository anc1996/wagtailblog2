/* Shared presentation layer for third-party video embeds. */
(function () {
    'use strict';

    var LOAD_TIMEOUT = 15000;

    function hasResourceEntry(iframe) {
        if (!window.performance || typeof window.performance.getEntriesByName !== 'function') return false;
        return window.performance.getEntriesByName(iframe.src).length > 0;
    }

    function monitorIframe(root, iframe) {
        var status = root.querySelector('[data-embed-status]');
        var statusText = root.querySelector('[data-embed-status-text]');
        var settled = false;

        function markLoaded() {
            settled = true;
            window.clearTimeout(timeoutId);
            root.classList.remove('geek-embed-container--failed');
            root.classList.add('geek-embed-container--loaded');
            if (status) status.setAttribute('aria-hidden', 'true');
        }

        function markTimedOut() {
            if (settled) return;
            root.classList.add('geek-embed-container--failed');
            if (status) status.setAttribute('aria-hidden', 'false');
            if (statusText) statusText.textContent = '外部视频加载超时，请检查平台链接或网络连接';
        }

        var timeoutId = window.setTimeout(markTimedOut, LOAD_TIMEOUT);
        iframe.addEventListener('load', markLoaded, { once: true });
        iframe.addEventListener('error', markTimedOut, { once: true });

        if (hasResourceEntry(iframe)) markLoaded();
    }

    function openEmbedDialog(root, button) {
        var stage = root.querySelector('.geek-embed-video-stage');
        if (!stage) return;

        var dialog = document.createElement('dialog');
        dialog.className = 'geek-embed-dialog';
        var shell = document.createElement('div');
        shell.className = 'geek-embed-dialog__shell';
        var header = document.createElement('div');
        header.className = 'geek-embed-dialog__header';
        var title = document.createElement('h2');
        title.textContent = root.getAttribute('aria-label') || '外部视频';
        var close = document.createElement('button');
        close.className = 'geek-embed-dialog__close';
        close.type = 'button';
        close.setAttribute('aria-label', '关闭视频放大窗口');
        close.title = '关闭';
        close.innerHTML = '<i class="fa fa-times" aria-hidden="true"></i>';
        var mount = document.createElement('div');
        mount.className = 'geek-embed-dialog__stage';
        var placeholder = document.createComment('embed-video-position');

        header.append(title, close);
        shell.append(header, mount);
        dialog.appendChild(shell);
        document.body.appendChild(dialog);

        function restore() {
            if (placeholder.parentNode) {
                placeholder.parentNode.replaceChild(stage, placeholder);
            }
            button.setAttribute('aria-expanded', 'false');
            button.focus({ preventScroll: true });
            dialog.remove();
        }

        function closeDialog() {
            if (dialog.open) dialog.close();
            else restore();
        }

        close.addEventListener('click', closeDialog);
        dialog.addEventListener('click', function (event) {
            if (event.target === dialog) closeDialog();
        });
        dialog.addEventListener('close', restore);

        stage.parentNode.insertBefore(placeholder, stage);
        mount.appendChild(stage);
        button.setAttribute('aria-expanded', 'true');
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
        window.requestAnimationFrame(function () {
            var iframe = stage.querySelector('iframe');
            if (!iframe) return;
            // Force a fresh layout after moving the cross-origin iframe into top-layer dialog.
            stage.offsetHeight;
            iframe.style.width = '100%';
            iframe.style.height = '100%';
        });
        close.focus({ preventScroll: true });
    }

    function initialize() {
        document.querySelectorAll('.geek-embed-container').forEach(function (root) {
            var collapseButton = root.querySelector('[data-embed-collapse]');
            if (collapseButton && collapseButton.dataset.collapseReady !== 'true') {
                collapseButton.dataset.collapseReady = 'true';
                collapseButton.addEventListener('click', function () {
                    var collapsed = root.classList.toggle('geek-embed-container--collapsed');
                    collapseButton.setAttribute('aria-expanded', String(!collapsed));
                    collapseButton.setAttribute('aria-label', collapsed ? '展开流媒体' : '折叠流媒体');
                    collapseButton.title = collapsed ? '展开流媒体' : '折叠流媒体';
                    collapseButton.innerHTML = collapsed
                        ? '<i class="fa fa-chevron-down" aria-hidden="true"></i>'
                        : '<i class="fa fa-chevron-up" aria-hidden="true"></i>';
                });
            }

            if (!root.hasAttribute('data-embed-video')) return;
            var button = root.querySelector('[data-embed-expand]');
            var iframe = root.querySelector('iframe');
            if (!button || !iframe || button.dataset.embedReady === 'true') return;

            button.dataset.embedReady = 'true';
            button.addEventListener('click', function () {
                openEmbedDialog(root, button);
            });
            monitorIframe(root, iframe);
        });
    }

    window.BlogEmbedPlayers = { initialize: initialize };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
}());
