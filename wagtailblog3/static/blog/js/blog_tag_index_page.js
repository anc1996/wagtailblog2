(function () {
    'use strict';

    function paramsFromForm(form) {
        var params = {};
        var formData = new FormData(form);
        formData.forEach(function (value, key) {
            params[key] = String(value || '');
        });

        if (!form.matches('.tag-page-jump')) {
            delete params.page;
        }
        return params;
    }

    function initialiseDatePickers(content) {
        if (!window.flatpickr) {
            return;
        }

        var startInput = content.querySelector('#start-date');
        var endInput = content.querySelector('#end-date');
        if (!startInput || !endInput) {
            return;
        }

        if (startInput._flatpickr) {
            startInput._flatpickr.destroy();
        }
        if (endInput._flatpickr) {
            endInput._flatpickr.destroy();
        }

        var startPicker = null;
        var endPicker = null;
        var localeOptions = {};
        if (window.flatpickr.l10ns && window.flatpickr.l10ns.zh) {
            localeOptions.locale = window.flatpickr.l10ns.zh;
        }

        startPicker = window.flatpickr(startInput, Object.assign({
            dateFormat: 'Y-m-d',
            maxDate: endInput.value || 'today',
            onChange: function (selectedDates) {
                if (!endPicker || !selectedDates[0]) {
                    return;
                }
                endPicker.set('minDate', selectedDates[0]);
                if (
                    endPicker.selectedDates[0] &&
                    endPicker.selectedDates[0] < selectedDates[0]
                ) {
                    endPicker.clear();
                }
            }
        }, localeOptions));

        endPicker = window.flatpickr(endInput, Object.assign({
            dateFormat: 'Y-m-d',
            minDate: startInput.value || null,
            maxDate: 'today',
            onChange: function (selectedDates) {
                if (!startPicker || !selectedDates[0]) {
                    return;
                }
                startPicker.set('maxDate', selectedDates[0]);
                if (
                    startPicker.selectedDates[0] &&
                    startPicker.selectedDates[0] > selectedDates[0]
                ) {
                    startPicker.clear();
                }
            }
        }, localeOptions));
    }

    function initialiseTagIndex() {
        if (!window.AsyncListingController) {
            return;
        }

        var controller = new window.AsyncListingController({
            rootSelector: '#tag-index-results',
            contentSelector: '.tag-index-results-content',
            statusSelector: '.tag-index-async-status',
            formSelector: '[data-tag-async-form]',
            linkSelector: 'a[data-tag-async-link]',
            focusSelector: '[data-tag-results-heading]',
            historyStateKey: 'tagIndexResults',
            loadingText: '正在更新标签内容…',
            errorText: '标签内容暂时无法更新，当前结果已保留。',
            paramsFromForm: paramsFromForm,
            afterRender: initialiseDatePickers
        });
        controller.start();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            window.setTimeout(initialiseTagIndex, 0);
        });
    } else {
        window.setTimeout(initialiseTagIndex, 0);
    }
})();
