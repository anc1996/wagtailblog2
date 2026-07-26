(function () {
    'use strict';

    var DEFAULT_BATCH_SIZE = 8;

    function setItemVisibility(item, visible) {
        item.hidden = !visible;
        item.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    function initGallery(gallery) {
        var items = Array.prototype.slice.call(
            gallery.querySelectorAll('.gallery-item[data-gallery-index]')
        );
        var button = gallery.querySelector('.blog-gallery-toggle');
        var label = gallery.querySelector('.blog-gallery-toggle-label');
        var icon = gallery.querySelector('.blog-gallery-toggle i');
        var status = gallery.querySelector('.blog-gallery-status');
        var batchSize = parseInt(gallery.getAttribute('data-gallery-batch-size'), 10) || DEFAULT_BATCH_SIZE;
        var total = items.length;
        var visibleCount = Math.min(batchSize, total);

        if (!button || total <= batchSize) {
            items.forEach(function (item) {
                setItemVisibility(item, true);
            });
            return;
        }

        function render(announce) {
            var remaining;
            var nextBatch;
            var fullyExpanded = visibleCount >= total;

            items.forEach(function (item, index) {
                setItemVisibility(item, index < visibleCount);
            });

            remaining = Math.max(total - visibleCount, 0);
            nextBatch = Math.min(batchSize, remaining);
            button.setAttribute('aria-expanded', fullyExpanded ? 'true' : 'false');

            if (fullyExpanded) {
                label.textContent = '收起图片画廊';
                icon.className = 'fa fa-angle-up';
            } else {
                label.textContent = '再看 ' + nextBatch + ' 张';
                icon.className = 'fa fa-angle-down';
            }

            if (status) {
                status.textContent = '已显示 ' + visibleCount + ' / ' + total + ' 张';
            }

            if (announce && status) {
                status.setAttribute('data-updated', String(Date.now()));
            }
        }

        button.addEventListener('click', function () {
            var previouslyVisible = visibleCount;

            if (visibleCount >= total) {
                visibleCount = Math.min(batchSize, total);
                render(true);
                gallery.scrollIntoView({ behavior: 'smooth', block: 'start' });
                return;
            }

            visibleCount = Math.min(visibleCount + batchSize, total);
            render(true);

            var firstNewItem = items[previouslyVisible];
            if (firstNewItem) {
                var firstNewLink = firstNewItem.querySelector('.gallery-link');
                if (firstNewLink) {
                    firstNewLink.focus({ preventScroll: true });
                }
                firstNewItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        });

        render(false);
    }

    function init() {
        document.querySelectorAll('.blog-gallery[data-gallery-batch-size]').forEach(initGallery);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
