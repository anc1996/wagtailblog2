(() => {
    'use strict';

    const carousel = document.querySelector('.home-featured-carousel');
    const toggle = document.querySelector('[data-home-carousel-toggle]');
    if (!carousel || !toggle || !window.jQuery) return;

    const $carousel = window.jQuery(carousel);
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    let manuallyPaused = false;

    const setButtonState = (paused) => {
        toggle.setAttribute('aria-pressed', String(paused));
        toggle.querySelector('i')?.classList.toggle('fa-play', paused);
        toggle.querySelector('i')?.classList.toggle('fa-pause', !paused);
        const label = paused ? '继续轮播' : '暂停轮播';
        toggle.querySelector('span').textContent = label;
        toggle.setAttribute('aria-label', label);
    };

    const stop = () => $carousel.trigger('stop.owl.autoplay');
    const play = () => {
        if (!manuallyPaused && !reduceMotion?.matches) $carousel.trigger('play.owl.autoplay');
    };

    toggle.addEventListener('click', () => {
        manuallyPaused = !manuallyPaused;
        if (manuallyPaused) stop();
        else play();
        setButtonState(manuallyPaused);
    });

    carousel.addEventListener('focusin', stop);
    carousel.addEventListener('focusout', (event) => {
        if (!carousel.contains(event.relatedTarget)) play();
    });

    reduceMotion?.addEventListener?.('change', () => {
        if (reduceMotion.matches) {
            stop();
            setButtonState(true);
        } else {
            play();
            setButtonState(manuallyPaused);
        }
    });

    // 自动播放由旧版 Owl 初始化；初始化后再同步无障碍状态和运动偏好。
    $carousel.on('initialized.owl.carousel', () => {
        const paused = Boolean(reduceMotion?.matches || manuallyPaused);
        if (paused) stop();
        setButtonState(paused);
    });

    const initiallyPaused = Boolean(reduceMotion?.matches);
    if (initiallyPaused) stop();
    setButtonState(initiallyPaused);
})();
