(function () {
    'use strict';

    function requestFormSubmit(form) {
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return;
        }

        form.submit();
    }

    function initialiseSearchControls() {
        var searchForm = document.getElementById('search-form');
        var searchTypeInput = document.getElementById('search-type-input');
        var filterButtons = document.querySelectorAll('.filter-button');
        var clearFiltersButton = document.getElementById('clear-filters');

        filterButtons.forEach(function (button) {
            button.addEventListener('click', function () {
                filterButtons.forEach(function (filterButton) {
                    filterButton.classList.remove('active');
                });
                button.classList.add('active');

                if (searchTypeInput) {
                    searchTypeInput.value = button.dataset.type || 'all';
                }
            });
        });

        if (!clearFiltersButton || !searchForm) {
            return;
        }

        clearFiltersButton.addEventListener('click', function (event) {
            event.preventDefault();

            var searchInput = document.getElementById('search-input');
            var startDatePicker = document.getElementById('start-date');
            var endDatePicker = document.getElementById('end-date');
            var orderBy = document.getElementById('order-by');

            if (searchInput) {
                searchInput.value = '';
            }
            if (searchTypeInput) {
                searchTypeInput.value = 'all';
            }
            filterButtons.forEach(function (filterButton) {
                filterButton.classList.toggle(
                    'active',
                    filterButton.dataset.type === 'all'
                );
            });

            [startDatePicker, endDatePicker].forEach(function (input) {
                if (!input) {
                    return;
                }
                if (input._flatpickr) {
                    input._flatpickr.clear();
                } else {
                    input.value = '';
                }
            });

            if (orderBy) {
                orderBy.value = '';
            }

            requestFormSubmit(searchForm);
        });
    }

    function initialiseSearchSuggestions() {
        var searchForm = document.getElementById('search-form');
        var searchInput = document.getElementById('search-input');
        var suggestionsBox = document.getElementById('suggestions');
        var jquery = window.jQuery;

        if (!searchForm || !searchInput || !suggestionsBox || !jquery) {
            return;
        }

        var suggestionsUrl =
            suggestionsBox.dataset.suggestionsUrl ||
            new URL('suggestions/', searchForm.action).toString();

        jquery(searchInput).on('input', function () {
            var query = searchInput.value;
            if (query.length < 2) {
                jquery(suggestionsBox).hide();
                return;
            }

            jquery.ajax({
                url: suggestionsUrl,
                data: { q: query },
                success: function (data) {
                    jquery(suggestionsBox).empty().show();
                    if (!data.suggestions || data.suggestions.length === 0) {
                        jquery(suggestionsBox).hide();
                        return;
                    }

                    data.suggestions.forEach(function (suggestion) {
                        var item = jquery('<div class="suggestion-item"></div>').text(
                            suggestion.query || suggestion
                        );
                        item.on('click', function () {
                            searchInput.value = item.text();
                            jquery(suggestionsBox).hide();
                            requestFormSubmit(searchForm);
                        });
                        jquery(suggestionsBox).append(item);
                    });
                },
                error: function () {
                    jquery(suggestionsBox).hide();
                }
            });
        });

        jquery(document).on('click', function (event) {
            if (
                !suggestionsBox.contains(event.target) &&
                event.target !== searchInput
            ) {
                jquery(suggestionsBox).hide();
            }
        });
    }

    function initialiseSearchResults() {
        var root = document.getElementById('search-results');
        var searchForm = document.getElementById('search-form');
        if (!root || !searchForm || !window.axios) {
            return;
        }

        var apiUrl = root.dataset.apiUrl;
        var content = root.querySelector('.search-results-content');
        var status = root.querySelector('.search-async-status');
        if (!apiUrl || !content || !status) {
            return;
        }

        var abortController = null;
        var requestSequence = 0;
        var lastRequestedParams = paramsFromUrl(window.location.href);

        function paramsFromUrl(url) {
            var parsedUrl = new URL(url, window.location.origin);
            return {
                query: parsedUrl.searchParams.get('query') || '',
                type: parsedUrl.searchParams.get('type') || 'all',
                start_date: parsedUrl.searchParams.get('start_date') || '',
                end_date: parsedUrl.searchParams.get('end_date') || '',
                order_by: parsedUrl.searchParams.get('order_by') || '',
                page: parsedUrl.searchParams.get('page') || '',
                cursor: parsedUrl.searchParams.get('cursor') || ''
            };
        }

        function paramsFromForm(form) {
            var formData = new FormData(form);
            return {
                query: String(formData.get('query') || ''),
                type: String(formData.get('type') || 'all'),
                start_date: String(formData.get('start_date') || ''),
                end_date: String(formData.get('end_date') || ''),
                order_by: String(formData.get('order_by') || ''),
                page: form.matches('.jump-to-page-form')
                    ? String(formData.get('page') || '')
                    : '',
                cursor: ''
            };
        }

        function requestParams(params) {
            var values = { type: params.type || 'all' };
            ['query', 'start_date', 'end_date', 'order_by', 'page', 'cursor'].forEach(function (key) {
                if (params[key]) {
                    values[key] = params[key];
                }
            });
            return values;
        }

        function setDateInputValue(input, value) {
            if (!input) {
                return;
            }

            if (input._flatpickr) {
                if (value) {
                    input._flatpickr.setDate(value, false);
                } else {
                    input._flatpickr.clear();
                }
                return;
            }

            input.value = value;
        }

        function applyFormValues(params) {
            var searchInput = document.getElementById('search-input');
            var searchTypeInput = document.getElementById('search-type-input');
            var startDateInput = document.getElementById('start-date');
            var endDateInput = document.getElementById('end-date');
            var orderByInput = document.getElementById('order-by');

            if (searchInput) {
                searchInput.value = params.query || '';
            }
            if (searchTypeInput) {
                searchTypeInput.value = params.type || 'all';
            }
            document.querySelectorAll('.filter-button').forEach(function (button) {
                button.classList.toggle(
                    'active',
                    button.dataset.type === (params.type || 'all')
                );
            });
            setDateInputValue(startDateInput, params.start_date || '');
            setDateInputValue(endDateInput, params.end_date || '');
            if (orderByInput) {
                orderByInput.value = params.order_by || '';
            }
        }

        function setLoading(isLoading) {
            root.classList.toggle('is-loading', isLoading);
            root.setAttribute('aria-busy', String(isLoading));
            status.classList.toggle('is-loading', isLoading);
            status.textContent = isLoading ? '正在更新搜索结果…' : '';
        }

        function clearError() {
            var error = root.querySelector('.search-results-load-error');
            if (error) {
                error.remove();
            }
        }

        function showError() {
            clearError();

            var error = document.createElement('div');
            error.className = 'search-results-load-error';
            error.setAttribute('role', 'alert');

            var message = document.createElement('p');
            message.textContent = '搜索结果暂时无法更新，当前结果已保留。';

            var retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'search-results-retry';
            retry.dataset.searchResultsRetry = 'true';
            retry.textContent = '重试';

            error.append(message, retry);
            root.insertBefore(error, content);
        }

        function isCanceled(error) {
            return (
                error &&
                (error.code === 'ERR_CANCELED' ||
                    (typeof window.axios.isCancel === 'function' &&
                        window.axios.isCancel(error)))
            );
        }

        function escapeRegExp(value) {
            return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }

        function highlightText(element, expression) {
            var walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
                acceptNode: function (node) {
                    return node.parentElement && node.parentElement.closest('.highlight')
                        ? NodeFilter.FILTER_REJECT
                        : NodeFilter.FILTER_ACCEPT;
                }
            });
            var textNodes = [];
            var node;

            while ((node = walker.nextNode())) {
                textNodes.push(node);
            }

            textNodes.forEach(function (textNode) {
                var text = textNode.nodeValue;
                var match;
                var lastIndex = 0;
                var fragment = document.createDocumentFragment();

                expression.lastIndex = 0;
                while ((match = expression.exec(text))) {
                    fragment.append(document.createTextNode(text.slice(lastIndex, match.index)));
                    var highlight = document.createElement('span');
                    highlight.className = 'highlight';
                    highlight.textContent = match[0];
                    fragment.append(highlight);
                    lastIndex = match.index + match[0].length;
                }

                if (lastIndex === 0) {
                    return;
                }

                fragment.append(document.createTextNode(text.slice(lastIndex)));
                textNode.parentNode.replaceChild(fragment, textNode);
            });
        }

        function highlightSearchTerms(query) {
            var terms = Array.from(
                new Set(
                    String(query || '')
                        .trim()
                        .split(/\s+/)
                        .filter(Boolean)
                )
            ).sort(function (left, right) {
                return right.length - left.length;
            });

            if (terms.length === 0) {
                return;
            }

            var expression = new RegExp(
                '(' + terms.map(escapeRegExp).join('|') + ')',
                'gi'
            );
            content.querySelectorAll('.result-title a, .result-intro').forEach(function (element) {
                highlightText(element, expression);
            });
        }

        function focusResults(shouldScroll) {
            var heading = content.querySelector('#search-results-heading');
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
            var loadOptions = options || {};
            lastRequestedParams = Object.assign({}, params);

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
                if (
                    !payload ||
                    payload.ok !== true ||
                    !payload.data ||
                    typeof payload.data.html !== 'string'
                ) {
                    throw new Error('Invalid search-results response');
                }

                content.innerHTML = payload.data.html;
                var responseParams = Object.assign(
                    { query: payload.data.query || '' },
                    payload.data.filters || {}
                );
                if (payload.data.canonical_url) {
                    responseParams = paramsFromUrl(payload.data.canonical_url);
                }
                lastRequestedParams = responseParams;
                applyFormValues(responseParams);
                highlightSearchTerms(payload.data.query);

                if (loadOptions.pushHistory && payload.data.canonical_url) {
                    var currentUrl = window.location.pathname + window.location.search;
                    if (payload.data.canonical_url !== currentUrl) {
                        window.history.pushState(
                            { searchResults: true },
                            '',
                            payload.data.canonical_url
                        );
                    }
                }

                if (loadOptions.focus) {
                    focusResults(loadOptions.scroll);
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

        searchForm.addEventListener('submit', function (event) {
            event.preventDefault();
            loadResults(paramsFromForm(searchForm), {
                pushHistory: true,
                focus: true,
                scroll: true
            });
        });

        root.addEventListener('click', function (event) {
            var retry = event.target.closest('[data-search-results-retry]');
            if (retry && root.contains(retry)) {
                event.preventDefault();
                loadResults(lastRequestedParams, {
                    pushHistory: true,
                    focus: true,
                    scroll: false
                });
                return;
            }

            var link = event.target.closest('.search-pagination a.pagination-button');
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
            if (!form.matches('.jump-to-page-form')) {
                return;
            }

            event.preventDefault();
            var pageInput = form.querySelector('input[name="page"]');
            var pageValue = Number.parseInt(pageInput.value, 10);
            var minPage = Number.parseInt(pageInput.min, 10);
            var maxPage = Number.parseInt(pageInput.max, 10);
            if (
                Number.isNaN(pageValue) ||
                pageValue < minPage ||
                pageValue > maxPage
            ) {
                window.alert('请输入有效的页码。');
                pageInput.focus();
                pageInput.value = pageInput.defaultValue;
                return;
            }

            loadResults(paramsFromForm(form), {
                pushHistory: true,
                focus: true,
                scroll: true
            });
        });

        document.querySelectorAll('a.popular-tag').forEach(function (link) {
            link.addEventListener('click', function (event) {
                if (!shouldUseAjax(event)) {
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
        });

        window.addEventListener('popstate', function () {
            loadResults(paramsFromUrl(window.location.href), {
                pushHistory: false,
                focus: false,
                scroll: false
            });
        });

        highlightSearchTerms(lastRequestedParams.query);
    }

    function initialiseSearch() {
        initialiseSearchControls();
        initialiseSearchSuggestions();
        initialiseSearchResults();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialiseSearch);
    } else {
        initialiseSearch();
    }
})();
