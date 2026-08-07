(function () {
    'use strict';

    function initialiseAuthorPostResults() {
        var root = document.getElementById('author-post-results');
        if (!root || !window.axios) {
            return;
        }

        var apiUrl = root.dataset.apiUrl;
        var content = root.querySelector('.author-post-content');
        var status = root.querySelector('.author-post-async-status');
        if (!apiUrl || !content || !status) {
            return;
        }

        var abortController = null;
        var requestSequence = 0;
        var lastRequestedParams = paramsFromUrl(window.location.href);

        function paramsFromUrl(url) {
            var parsedUrl = new URL(url, window.location.origin);
            return {
                q: parsedUrl.searchParams.get('q') || '',
                page: parsedUrl.searchParams.get('page') || ''
            };
        }

        function setLoading(isLoading) {
            root.classList.toggle('is-loading', isLoading);
            root.setAttribute('aria-busy', String(isLoading));
            status.classList.toggle('is-loading', isLoading);
            status.textContent = isLoading ? '正在加载文章…' : '';
        }

        function clearError() {
            var error = root.querySelector('.author-post-load-error');
            if (error) {
                error.remove();
            }
        }

        function showError() {
            clearError();

            var error = document.createElement('div');
            error.className = 'author-post-load-error';
            error.setAttribute('role', 'alert');

            var message = document.createElement('p');
            message.textContent = '文章列表暂时无法加载，现有结果没有被更改。';

            var retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'author-post-retry';
            retry.dataset.authorPostsRetry = 'true';
            retry.textContent = '重试';

            error.append(message, retry);
            root.insertBefore(error, content);
        }

        function isCanceled(error) {
            return (
                error &&
                (error.code === 'ERR_CANCELED' ||
                    (typeof window.axios.isCancel === 'function' && window.axios.isCancel(error)))
            );
        }

        function requestParams(params) {
            var values = {};
            if (params.q) {
                values.q = params.q;
            }
            if (params.page) {
                values.page = params.page;
            }
            return values;
        }

        function focusResults(shouldScroll) {
            var heading = content.querySelector('#author-posts-heading');
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
                root.scrollIntoView({
                    behavior: reducedMotion ? 'auto' : 'smooth',
                    block: 'start'
                });
            }
        }

        async function loadResults(params, options) {
            var requestId = ++requestSequence;
            lastRequestedParams = {
                q: params.q || '',
                page: params.page || ''
            };

            if (abortController) {
                abortController.abort();
            }
            abortController =
                typeof AbortController === 'undefined' ? null : new AbortController();

            clearError();
            setLoading(true);

            try {
                var response = await window.axios.get(apiUrl, {
                    params: requestParams(lastRequestedParams),
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    signal: abortController ? abortController.signal : undefined,
                    timeout: 10000
                });

                if (requestId !== requestSequence) {
                    return;
                }

                var payload = response.data;
                if (!payload || payload.ok !== true || !payload.data || typeof payload.data.html !== 'string') {
                    throw new Error('Invalid author-post response');
                }

                content.innerHTML = payload.data.html;

                if (options.pushHistory && payload.data.canonical_url) {
                    var currentUrl = window.location.pathname + window.location.search;
                    if (payload.data.canonical_url !== currentUrl) {
                        window.history.pushState(
                            { authorPostResults: true },
                            '',
                            payload.data.canonical_url
                        );
                    }
                }

                if (options.focus) {
                    focusResults(options.scroll);
                }
            } catch (error) {
                if (requestId === requestSequence && !isCanceled(error)) {
                    showError();
                }
            } finally {
                if (requestId === requestSequence) {
                    abortController = null;
                    setLoading(false);
                }
            }
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

        root.addEventListener('click', function (event) {
            var retry = event.target.closest('[data-author-posts-retry]');
            if (retry && root.contains(retry)) {
                event.preventDefault();
                loadResults(lastRequestedParams, {
                    pushHistory: true,
                    focus: true,
                    scroll: false
                });
                return;
            }

            var link = event.target.closest(
                '.author-search-clear, .custom-pagination a.pagination-button'
            );
            if (!link || !root.contains(link) || !shouldUseAjax(event)) {
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
            loadResults(paramsFromUrl(destination.href), {
                pushHistory: true,
                focus: true,
                scroll: true
            });
        });

        root.addEventListener('submit', function (event) {
            var form = event.target;
            if (
                !form.matches('.author-post-search') &&
                !form.matches('.pagination-jump-form')
            ) {
                return;
            }

            event.preventDefault();
            var formData = new FormData(form);
            loadResults(
                {
                    q: String(formData.get('q') || ''),
                    page: form.matches('.pagination-jump-form')
                        ? String(formData.get('page') || '')
                        : ''
                },
                {
                    pushHistory: true,
                    focus: true,
                    scroll: true
                }
            );
        });

        window.addEventListener('popstate', function () {
            loadResults(paramsFromUrl(window.location.href), {
                pushHistory: false,
                focus: false,
                scroll: false
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialiseAuthorPostResults);
    } else {
        initialiseAuthorPostResults();
    }
})();
