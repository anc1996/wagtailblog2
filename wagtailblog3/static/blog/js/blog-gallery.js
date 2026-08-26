(function () {
    'use strict';

    var DEFAULT_BATCH_SIZE = 8;

    function initGallery(gallery) {
        var grid = gallery.querySelector('.gallery-grid');
        var button = gallery.querySelector('.blog-gallery-toggle');
        var label = gallery.querySelector('.blog-gallery-toggle-label');
        var icon = gallery.querySelector('.blog-gallery-toggle i');
        var status = gallery.querySelector('.blog-gallery-status');
        var apiUrl = gallery.getAttribute('data-gallery-api-url');
        var page = parseInt(gallery.getAttribute('data-gallery-page'), 10) || 1;
        var total = parseInt(gallery.getAttribute('data-gallery-total'), 10) || 0;
        var batchSize = parseInt(gallery.getAttribute('data-gallery-batch-size'), 10) || DEFAULT_BATCH_SIZE;
        var loadedCount = Math.min(batchSize, total);
        var loading = false;

        if (!grid || !button || !apiUrl || total <= batchSize) {
            return;
        }

        function setStatus(message) {
            if (status) {
                status.textContent = message;
            }
        }

        function updateButton(loadedCount) {
            var remaining = Math.max(total - loadedCount, 0);
            var nextBatch = Math.min(batchSize, remaining);
            var finished = remaining === 0;

            button.hidden = finished;
            button.disabled = loading;
            button.setAttribute('aria-expanded', finished ? 'true' : 'false');
            if (label) {
                label.textContent = finished ? '已显示全部图片' : '再看 ' + nextBatch + ' 张';
            }
            if (icon) {
                icon.className = finished ? 'fa fa-check' : 'fa fa-angle-down';
            }
            if (!loading) {
                setStatus('已显示 ' + loadedCount + ' / ' + total + ' 张');
            }
        }

        button.addEventListener('click', function () {
            var nextPage;
            var url;
            var previousLoadedCount;

            if (loading || button.hidden) {
                return;
            }

            loading = true;
            previousLoadedCount = loadedCount;
            button.disabled = true;
            gallery.setAttribute('aria-busy', 'true');
            setStatus('正在加载图片…');
            nextPage = page + 1;
            url = apiUrl + (apiUrl.indexOf('?') === -1 ? '?' : '&') + 'page=' + nextPage;

            fetch(url, {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                credentials: 'same-origin'
            }).then(function (response) {
                if (!response.ok) {
                    throw new Error('gallery request failed');
                }
                return response.json();
            }).then(function (payload) {
                var data = payload && payload.ok && payload.data;
                var firstNewLink;

                if (!data || !data.html) {
                    throw new Error('gallery response invalid');
                }

                grid.insertAdjacentHTML('beforeend', data.html);
                page = data.page || nextPage;
                gallery.setAttribute('data-gallery-page', String(page));
                loadedCount = Math.min(data.loaded_count || (page * batchSize), total);
                firstNewLink = grid.querySelector('.gallery-item[data-gallery-index="' + previousLoadedCount + '"] .gallery-link');
                loading = false;
                gallery.setAttribute('aria-busy', 'false');
                updateButton(loadedCount);

                if (firstNewLink) {
                    firstNewLink.focus({ preventScroll: true });
                }
            }).catch(function () {
                loading = false;
                gallery.setAttribute('aria-busy', 'false');
                button.disabled = false;
                setStatus('加载失败，请重试');
            });
        });

        updateButton(loadedCount);
    }

    function init() {
        document.querySelectorAll('.blog-gallery[data-gallery-api-url]').forEach(initGallery);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
