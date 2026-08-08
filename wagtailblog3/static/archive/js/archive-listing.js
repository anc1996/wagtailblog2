(function () {
    'use strict';

    function paramsFromForm(form) {
        var params = {};
        var formData = new FormData(form);

        formData.forEach(function (value, key) {
            params[key] = String(value || '');
        });

        if (!form.matches('.archive-jump-form')) {
            delete params.page;
        }

        return params;
    }

    function initialiseArchiveListing() {
        if (!window.AsyncListingController) {
            return;
        }

        var controller = new window.AsyncListingController({
            rootSelector: '#archive-listing',
            contentSelector: '.archive-results-content',
            statusSelector: '.archive-async-status',
            formSelector: '[data-archive-async-form]',
            linkSelector: 'a[data-archive-async-link]',
            focusSelector: '[data-archive-results-heading]',
            historyStateKey: 'archiveResults',
            loadingText: '正在更新归档文章…',
            errorText: '归档文章暂时无法更新，当前结果已保留。',
            paramsFromForm: paramsFromForm
        });
        controller.start();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialiseArchiveListing);
    } else {
        initialiseArchiveListing();
    }
})();
