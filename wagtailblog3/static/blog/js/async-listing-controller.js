(function (window) {
    'use strict';

    function paramsFromUrl(url) {
        var parsedUrl = new URL(url, window.location.origin);
        var params = {};
        parsedUrl.searchParams.forEach(function (value, key) {
            params[key] = value;
        });
        return params;
    }

    function compactParams(params) {
        var values = {};
        Object.keys(params || {}).forEach(function (key) {
            if (params[key] !== '' && params[key] !== null && params[key] !== undefined) {
                values[key] = params[key];
            }
        });
        return values;
    }

    function isCanceled(error) {
        return (
            error &&
            (error.code === 'ERR_CANCELED' ||
                (window.axios &&
                    typeof window.axios.isCancel === 'function' &&
                    window.axios.isCancel(error)))
        );
    }

    function shouldUseAjax(event) {
        return !(
            event.defaultPrevented ||
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
        );
    }

    function AsyncListingController(options) {
        this.options = options || {};
        this.root = document.querySelector(this.options.rootSelector);
        this.content = this.root
            ? this.root.querySelector(this.options.contentSelector)
            : null;
        this.status = this.root
            ? this.root.querySelector(this.options.statusSelector)
            : null;
        this.apiUrl = this.root ? this.root.dataset.apiUrl : '';
        this.abortController = null;
        this.requestSequence = 0;
        this.lastRequestedParams = paramsFromUrl(window.location.href);
    }

    AsyncListingController.prototype.start = function () {
        var self = this;
        if (!this.root || !this.content || !this.status || !this.apiUrl || !window.axios) {
            return false;
        }

        this.root.addEventListener('click', function (event) {
            self.handleClick(event);
        });
        this.root.addEventListener('submit', function (event) {
            self.handleSubmit(event);
        });
        window.addEventListener('popstate', function () {
            self.load(paramsFromUrl(window.location.href), {
                pushHistory: false,
                focus: false,
                scroll: false
            });
        });

        this.afterRender();
        return true;
    };

    AsyncListingController.prototype.setLoading = function (isLoading) {
        this.root.classList.toggle('is-loading', isLoading);
        this.root.setAttribute('aria-busy', String(isLoading));
        this.status.classList.toggle('is-loading', isLoading);
        this.status.textContent = isLoading ? this.options.loadingText : '';
    };

    AsyncListingController.prototype.clearError = function () {
        var error = this.root.querySelector('.async-listing-load-error');
        if (error) {
            error.remove();
        }
    };

    AsyncListingController.prototype.showError = function () {
        this.clearError();

        var error = document.createElement('div');
        error.className = 'async-listing-load-error';
        error.setAttribute('role', 'alert');

        var message = document.createElement('p');
        message.textContent = this.options.errorText;

        var retry = document.createElement('button');
        retry.type = 'button';
        retry.dataset.asyncListingRetry = 'true';
        retry.textContent = '重试';

        error.append(message, retry);
        this.root.insertBefore(error, this.content);
    };

    AsyncListingController.prototype.afterRender = function () {
        if (typeof this.options.afterRender === 'function') {
            this.options.afterRender(this.content);
        }
    };

    AsyncListingController.prototype.focusResults = function (shouldScroll) {
        var heading = this.content.querySelector(this.options.focusSelector);
        if (heading) {
            try {
                heading.focus({ preventScroll: true });
            } catch (error) {
                heading.focus();
            }
        }

        if (shouldScroll) {
            var reducedMotion =
                window.matchMedia &&
                window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            this.root.scrollIntoView({
                behavior: reducedMotion ? 'auto' : 'smooth',
                block: 'start'
            });
        }
    };

    AsyncListingController.prototype.load = async function (params, loadOptions) {
        var requestId = ++this.requestSequence;
        var options = loadOptions || {};
        this.lastRequestedParams = Object.assign({}, params || {});

        if (this.abortController) {
            this.abortController.abort();
        }
        this.abortController =
            typeof AbortController === 'undefined' ? null : new AbortController();

        this.clearError();
        this.setLoading(true);

        try {
            var response = await window.axios.get(this.apiUrl, {
                params: compactParams(this.lastRequestedParams),
                headers: {
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                signal: this.abortController ? this.abortController.signal : undefined,
                timeout: 10000
            });

            if (requestId !== this.requestSequence) {
                return;
            }

            var payload = response.data;
            if (
                !payload ||
                payload.ok !== true ||
                !payload.data ||
                typeof payload.data.html !== 'string'
            ) {
                throw new Error('Invalid asynchronous listing response');
            }

            this.content.innerHTML = payload.data.html;
            this.afterRender();

            if (payload.data.canonical_url) {
                this.lastRequestedParams = paramsFromUrl(payload.data.canonical_url);
            }

            if (options.pushHistory && payload.data.canonical_url) {
                var currentUrl = window.location.pathname + window.location.search;
                if (payload.data.canonical_url !== currentUrl) {
                    window.history.pushState(
                        { asyncListing: this.options.historyStateKey },
                        '',
                        payload.data.canonical_url
                    );
                }
            }

            if (options.focus) {
                this.focusResults(options.scroll);
            }
        } catch (error) {
            if (requestId === this.requestSequence && !isCanceled(error)) {
                this.showError();
            }
        } finally {
            if (requestId === this.requestSequence) {
                this.abortController = null;
                this.setLoading(false);
            }
        }
    };

    AsyncListingController.prototype.handleClick = function (event) {
        var retry = event.target.closest('[data-async-listing-retry]');
        if (retry && this.root.contains(retry)) {
            event.preventDefault();
            this.load(this.lastRequestedParams, {
                pushHistory: false,
                focus: true,
                scroll: false
            });
            return;
        }

        var link = event.target.closest(this.options.linkSelector);
        if (!link || !this.root.contains(link) || !shouldUseAjax(event)) {
            return;
        }

        var destination = new URL(link.href, window.location.href);
        if (
            destination.origin !== window.location.origin ||
            destination.pathname !== window.location.pathname
        ) {
            return;
        }

        event.preventDefault();
        this.load(paramsFromUrl(destination.href), {
            pushHistory: true,
            focus: true,
            scroll: true
        });
    };

    AsyncListingController.prototype.handleSubmit = function (event) {
        var form = event.target;
        if (!form.matches(this.options.formSelector)) {
            return;
        }

        event.preventDefault();
        this.load(this.options.paramsFromForm(form), {
            pushHistory: true,
            focus: true,
            scroll: true
        });
    };

    window.AsyncListingController = AsyncListingController;
})(window);
