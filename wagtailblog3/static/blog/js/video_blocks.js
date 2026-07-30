/* Blog video player integration. Plyr supplies controls; this module owns page state. */
(function () {
    'use strict';

    var players = new Map();
    var floatingStates = new Map();
    var playerOptions = {
        controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'],
        settings: ['speed'],
        speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
        seekTime: 10,
        tooltips: { controls: true, seek: true },
        keyboard: { focused: true, global: false },
        fullscreen: { enabled: true, fallback: true, iosNative: true },
        resetOnEnd: false
    };

    function markPlayerError(root, error) {
        root.classList.add('blog-video--error');
        root.setAttribute('data-video-error', '播放失败');
        if (window.console && console.warn) {
            console.warn('Blog video failed to load.', error);
        }
    }

    function pauseOtherPlayers(current) {
        players.forEach(function (player) {
            if (player !== current && !player.paused) {
                player.pause();
            }
        });
    }

    function restorePlayer(state) {
        if (!state.open) {
            return;
        }
        if (state.placeholder.parentNode) {
            state.placeholder.parentNode.replaceChild(state.container, state.placeholder);
        }
        state.root.classList.remove('blog-video--floating');
        state.button.setAttribute('aria-expanded', 'false');
        state.open = false;
        state.button.focus({ preventScroll: true });
    }

    function closeFloatingPlayer(state) {
        if (state.dialog.open) {
            state.dialog.close();
        } else {
            restorePlayer(state);
        }
    }

    function openFloatingPlayer(root, player, button) {
        var existing = floatingStates.get(player);
        if (existing && existing.open) {
            return;
        }

        var dialog = document.createElement('dialog');
        dialog.className = 'blog-video-dialog';
        dialog.setAttribute('aria-labelledby', 'blog-video-dialog-title');

        var shell = document.createElement('div');
        shell.className = 'blog-video-dialog__shell';
        var header = document.createElement('div');
        header.className = 'blog-video-dialog__header';
        var title = document.createElement('h2');
        title.id = 'blog-video-dialog-title';
        title.textContent = root.getAttribute('data-video-title') || '视频播放';
        var close = document.createElement('button');
        close.className = 'blog-video-dialog__close';
        close.type = 'button';
        close.setAttribute('aria-label', '关闭视频放大窗口');
        close.title = '关闭';
        close.innerHTML = '<i class="fa fa-times" aria-hidden="true"></i>';
        var mount = document.createElement('div');
        mount.className = 'blog-video-dialog__player';
        header.append(title, close);
        shell.append(header, mount);
        dialog.appendChild(shell);
        document.body.appendChild(dialog);

        var state = {
            root: root,
            player: player,
            button: button,
            dialog: dialog,
            close: close,
            container: player.elements.container,
            placeholder: document.createComment('video-player-position'),
            open: false
        };
        floatingStates.set(player, state);

        function open() {
            if (state.open) {
                return;
            }
            state.container.parentNode.insertBefore(state.placeholder, state.container);
            mount.appendChild(state.container);
            state.root.classList.add('blog-video--floating');
            state.button.setAttribute('aria-expanded', 'true');
            state.open = true;
            if (typeof dialog.showModal === 'function') {
                dialog.showModal();
            } else {
                dialog.setAttribute('open', '');
            }
            close.focus({ preventScroll: true });
        }

        function onDialogClose() {
            restorePlayer(state);
            dialog.remove();
            floatingStates.delete(player);
        }

        button.setAttribute('aria-expanded', 'false');
        button.addEventListener('click', open);
        close.addEventListener('click', function () {
            closeFloatingPlayer(state);
        });
        dialog.addEventListener('click', function (event) {
            if (event.target === dialog) {
                closeFloatingPlayer(state);
            }
        });
        dialog.addEventListener('close', onDialogClose);

        open();
    }

    function initializePlayer(root) {
        var video = root.querySelector('video');
        if (!video || players.has(video)) {
            return null;
        }

        var collapseButton = root.querySelector('[data-video-collapse]');
        if (collapseButton && collapseButton.dataset.collapseReady !== 'true') {
            collapseButton.dataset.collapseReady = 'true';
            collapseButton.addEventListener('click', function () {
                var collapsed = root.classList.toggle('blog-video--collapsed');
                collapseButton.setAttribute('aria-expanded', String(!collapsed));
                collapseButton.setAttribute('aria-label', collapsed ? '展开视频' : '折叠视频');
                collapseButton.title = collapsed ? '展开视频' : '折叠视频';
                collapseButton.innerHTML = collapsed
                    ? '<i class="fa fa-chevron-down" aria-hidden="true"></i>'
                    : '<i class="fa fa-chevron-up" aria-hidden="true"></i>';
            });
        }

        if (typeof window.Plyr !== 'function') {
            video.controls = true;
            root.classList.add('blog-video--native-fallback');
            return null;
        }

        video.removeAttribute('muted');
        video.setAttribute('playsinline', '');
        var player = new window.Plyr(video, playerOptions);
        var expandButton = root.querySelector('[data-video-expand]');
        players.set(video, player);
        root.classList.add('blog-video--ready');
        root.__blogVideoPlayer = player;

        if (expandButton) {
            expandButton.addEventListener('click', function (event) {
                event.stopPropagation();
                openFloatingPlayer(root, player, expandButton);
            });
        }
        player.on('play', function () {
            pauseOtherPlayers(player);
        });
        player.on('error', function (event) {
            markPlayerError(root, event);
        });
        player.on('ready', function () {
            root.classList.add('blog-video--loaded');
        });
        player.on('destroy', function () {
            var state = floatingStates.get(player);
            if (state) {
                closeFloatingPlayer(state);
            }
            players.delete(video);
            delete root.__blogVideoPlayer;
        });
        return player;
    }

    function initializeBlogVideoPlayers() {
        document.querySelectorAll('[data-video-player]').forEach(function (root) {
            initializePlayer(root);
        });
    }

    window.BlogVideoPlayers = {
        initialize: initializeBlogVideoPlayers,
        get: function (video) { return players.get(video) || null; },
        getAll: function () { return Array.from(players.values()); },
        options: playerOptions
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeBlogVideoPlayers);
    } else {
        initializeBlogVideoPlayers();
    }
}());
