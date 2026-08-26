(() => {
    'use strict';

    const STORAGE_KEY = 'wagtailblog-site-theme';
    const MODES = new Set(['auto', 'light', 'dark']);
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');

    function readPreference() {
        try {
            const value = window.localStorage.getItem(STORAGE_KEY);
            return MODES.has(value) ? value : 'auto';
        } catch (error) {
            return 'auto';
        }
    }

    function resolveTheme(preference) {
        return preference === 'auto' ? (media?.matches ? 'dark' : 'light') : preference;
    }

    function modeLabel(preference) {
        return {
            auto: '跟随系统',
            light: '白天模式',
            dark: '夜间模式'
        }[preference];
    }

    function closeMenus(except = null) {
        document.querySelectorAll('[data-site-theme-control]').forEach((control) => {
            if (control === except) return;
            control.classList.remove('is-open');
            control.querySelector('[data-site-theme-toggle]')?.setAttribute('aria-expanded', 'false');
            const menu = control.querySelector('[data-site-theme-menu]');
            if (menu) menu.hidden = true;
        });
    }

    function updateControls(preference, theme) {
        const labels = {
            auto: 'fa-adjust',
            light: 'fa-sun-o',
            dark: 'fa-moon-o'
        };

        document.querySelectorAll('[data-site-theme-control]').forEach((control) => {
            const toggle = control.querySelector('[data-site-theme-toggle]');
            const icon = toggle?.querySelector('i');
            const label = `阅读主题：${modeLabel(preference)}`;
            if (toggle) {
                toggle.setAttribute('aria-label', label);
                toggle.title = label;
                toggle.dataset.resolvedTheme = theme;
            }
            if (icon) icon.className = `fa ${labels[preference]}`;

            control.querySelectorAll('[data-site-theme-option]').forEach((option) => {
                const selected = option.dataset.siteThemeOption === preference;
                option.setAttribute('aria-checked', String(selected));
                option.classList.toggle('is-selected', selected);
            });
        });
    }

    function applyPreference(preference, options = {}) {
        const normalized = MODES.has(preference) ? preference : 'auto';
        const theme = resolveTheme(normalized);
        const root = document.documentElement;

        root.dataset.themePreference = normalized;
        root.dataset.theme = theme;
        root.style.colorScheme = theme;

        if (options.persist) {
            try {
                window.localStorage.setItem(STORAGE_KEY, normalized);
            } catch (error) {
                // Persistence is optional when storage is unavailable.
            }
        }

        updateControls(normalized, theme);
        window.dispatchEvent(new CustomEvent('site-theme-change', {
            detail: { preference: normalized, theme }
        }));
    }

    function toggleMenu(control) {
        const menu = control.querySelector('[data-site-theme-menu]');
        const toggle = control.querySelector('[data-site-theme-toggle]');
        if (!menu || !toggle) return;

        const willOpen = menu.hidden;
        closeMenus(control);
        control.classList.toggle('is-open', willOpen);
        toggle.setAttribute('aria-expanded', String(willOpen));
        menu.hidden = !willOpen;
        if (willOpen) menu.querySelector('[aria-checked="true"]')?.focus();
    }

    // 主题菜单使用方向键循环选项，避免键盘用户只能逐项离开菜单。
    function moveMenuFocus(menu, current, direction) {
        const options = [...menu.querySelectorAll('[data-site-theme-option]')];
        if (!options.length) return;
        const index = Math.max(options.indexOf(current), 0);
        const nextIndex = (index + direction + options.length) % options.length;
        options[nextIndex].focus();
    }

    document.addEventListener('click', (event) => {
        const toggle = event.target.closest('[data-site-theme-toggle]');
        if (toggle) {
            event.preventDefault();
            toggleMenu(toggle.closest('[data-site-theme-control]'));
            return;
        }

        const option = event.target.closest('[data-site-theme-option]');
        if (option) {
            const control = option.closest('[data-site-theme-control]');
            const toggle = control?.querySelector('[data-site-theme-toggle]');
            applyPreference(option.dataset.siteThemeOption, { persist: true });
            closeMenus();
            toggle?.focus();
            return;
        }

        if (!event.target.closest('[data-site-theme-control]')) closeMenus();
    });

    document.addEventListener('keydown', (event) => {
        const option = event.target.closest('[data-site-theme-option]');
        if (option) {
            const menu = option.closest('[data-site-theme-menu]');
            if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
                event.preventDefault();
                moveMenuFocus(menu, option, 1);
                return;
            }
            if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
                event.preventDefault();
                moveMenuFocus(menu, option, -1);
                return;
            }
            if (event.key === 'Home' || event.key === 'End') {
                event.preventDefault();
                const options = menu?.querySelectorAll('[data-site-theme-option]');
                options?.[event.key === 'Home' ? 0 : options.length - 1]?.focus();
                return;
            }
        }

        if (event.key !== 'Escape') return;
        const openControl = document.querySelector('[data-site-theme-control].is-open');
        if (!openControl) return;
        closeMenus();
        openControl.querySelector('[data-site-theme-toggle]')?.focus();
    });

    media?.addEventListener?.('change', () => {
        if (readPreference() === 'auto') applyPreference('auto');
    });

    window.WagtailBlogTheme = Object.freeze({
        getPreference: readPreference,
        getTheme: () => resolveTheme(readPreference()),
        setPreference: (preference) => applyPreference(preference, { persist: true })
    });

    const initialize = () => applyPreference(readPreference());
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
