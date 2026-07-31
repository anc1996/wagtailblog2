/* Blog audio player integration. Plyr supplies controls; this module owns page behavior. */
(function () {
    'use strict';

    var players = new Map();
    var playerOptions = {
        controls: ['play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'settings', 'download'],
        settings: ['speed'],
        speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
        seekTime: 10,
        tooltips: { controls: true, seek: true },
        keyboard: { focused: true, global: false },
        resetOnEnd: false
    };

    function pauseOtherPlayers(current) {
        players.forEach(function (player) {
            if (player !== current && !player.paused) {
                player.pause();
            }
        });
    }

    function markAudioError(root, event) {
        root.classList.add('audio-is-error');
        root.setAttribute('data-audio-error', '播放失败');
        if (window.console && console.warn) {
            console.warn('Blog audio failed to load.', event);
        }
    }

    function initializePlayer(root) {
        var audio = root.querySelector('audio');
        if (!audio || players.has(audio)) {
            return null;
        }

        if (typeof window.Plyr !== 'function') {
            audio.controls = true;
            root.classList.add('audio-native-fallback');
            return null;
        }

        var player = new window.Plyr(audio, playerOptions);
        players.set(audio, player);
        root.classList.add('audio-is-ready');
        root.__blogAudioPlayer = player;

        player.on('play', function () {
            pauseOtherPlayers(player);
        });
        player.on('error', function (event) {
            markAudioError(root, event);
        });
        player.on('ready', function () {
            root.classList.add('audio-is-loaded');
        });
        player.on('destroy', function () {
            players.delete(audio);
            delete root.__blogAudioPlayer;
        });
        return player;
    }

    function initializeBlogAudioPlayers() {
        document.querySelectorAll('[data-audio-player]').forEach(function (root) {
            initializePlayer(root);
        });
    }

    window.BlogAudioPlayers = {
        initialize: initializeBlogAudioPlayers,
        get: function (audio) { return players.get(audio) || null; },
        getAll: function () { return Array.from(players.values()); },
        options: playerOptions
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeBlogAudioPlayers);
    } else {
        initializeBlogAudioPlayers();
    }
}());
