import PhotoSwipeLightbox from '../../vendor/photoswipe/photoswipe-lightbox.esm.min.js';

const photoswipeModuleUrl = new URL(
    '../../vendor/photoswipe/photoswipe.esm.min.js',
    import.meta.url
).href;

function getCaption(itemElement) {
    if (!itemElement) return '';

    return itemElement.dataset.pswpCaption
        || itemElement.querySelector('img')?.alt
        || '';
}

function registerCaption(lightbox) {
    lightbox.on('uiRegister', () => {
        lightbox.pswp.ui.registerElement({
            name: 'editorial-caption',
            order: 9,
            isButton: false,
            appendTo: 'root',
            html: '',
            onInit: (element, pswp) => {
                const update = () => {
                    const caption = getCaption(pswp.currSlide?.data?.element);
                    element.textContent = caption;
                    element.hidden = !caption;
                };

                pswp.on('change', update);
                update();
            }
        });
    });
}

function initializeGallery(gallery) {
    if (gallery.dataset.imageViewerReady === 'true') return;

    const items = gallery.querySelectorAll('a[data-pswp-item]');
    if (!items.length) return;

    gallery.dataset.imageViewerReady = 'true';

    const lightbox = new PhotoSwipeLightbox({
        gallery,
        children: 'a[data-pswp-item]',
        pswpModule: () => import(photoswipeModuleUrl),
        bgOpacity: 0.94,
        showHideAnimationType: 'zoom',
        wheelToZoom: true,
        paddingFn: (viewportSize) => ({
            top: viewportSize.x < 768 ? 18 : 42,
            bottom: viewportSize.x < 768 ? 72 : 88,
            left: viewportSize.x < 768 ? 12 : 42,
            right: viewportSize.x < 768 ? 12 : 42
        })
    });

    registerCaption(lightbox);
    lightbox.init();
}

function initializeImageViewers(root = document) {
    root.querySelectorAll('[data-content-image-gallery]').forEach(initializeGallery);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initializeImageViewers(), { once: true });
} else {
    initializeImageViewers();
}

window.ContentImageViewer = {
    initialize: initializeImageViewers
};
