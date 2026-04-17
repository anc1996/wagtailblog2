// static/search/js/search.js

$(document).ready(function() {
    // 搜索过滤器
    const filterButtons = $('.filter-button');
    const searchTypeInput = $('#search-type-input');
    const searchForm = $('#search-form');

    filterButtons.each(function() {
        $(this).on('click', function() {
            filterButtons.removeClass('active');
            $(this).addClass('active');
            const type = $(this).data('type');
            searchTypeInput.val(type);
        });
    });

    // 清除筛选按钮
    const clearFiltersBtn = $('#clear-filters');
    if (clearFiltersBtn.length) {
        clearFiltersBtn.on('click', function(e) {
            e.preventDefault();

            // 清空搜索框 (可选，根据需求决定)
            $('#search-input').val('');

            // 重置类型筛选到 'all' 并更新按钮状态
            searchTypeInput.val('all');
            filterButtons.removeClass('active');
            $('.filter-button[data-type="all"]').addClass('active');

            // 清空日期输入
            const startDatePicker = $('#start-date');
            const endDatePicker = $('#end-date');

            if (startDatePicker.length && startDatePicker[0]._flatpickr) {
                startDatePicker[0]._flatpickr.clear();
            } else {
                startDatePicker.val('');
            }

            if (endDatePicker.length && endDatePicker[0]._flatpickr) {
                endDatePicker[0]._flatpickr.clear();
            } else {
                endDatePicker.val('');
            }

            // 重置排序到默认值
            $('#order-by').val('');

            // 提交表单以应用清除的筛选条件
            searchForm.submit();
        });
    }

    // 实时搜索建议
    const searchInput = $('#search-input');
    const suggestionsBox = $('#suggestions');

    if (searchInput.length && suggestionsBox.length) {
        searchInput.on('input', function() {
            const query = $(this).val();
            if (query.length >= 2) {
                $.ajax({
                    url: suggestionsBox.data('suggestions-url') || '/search/suggestions/',
                    data: { 'q': query },
                    success: function(data) {
                        suggestionsBox.empty().show();
                        if (data.suggestions && data.suggestions.length > 0) {
                            data.suggestions.forEach(function(suggestion) {
                                const item = $('<div class="suggestion-item"></div>').text(suggestion.query || suggestion);
                                item.on('click', function() {
                                    searchInput.val($(this).text());
                                    suggestionsBox.hide();
                                    searchForm.submit();
                                });
                                suggestionsBox.append(item);
                            });
                        } else {
                            suggestionsBox.hide();
                        }
                    },
                    error: function() {
                        suggestionsBox.hide();
                    }
                });
            } else {
                suggestionsBox.hide();
            }
        });
    }

    // 处理点击页面其他地方时隐藏建议框
    $(document).on('click', function(e) {
        if (suggestionsBox.length && !suggestionsBox.is(e.target) && suggestionsBox.has(e.target).length === 0 && searchInput.length && !searchInput.is(e.target)) {
            suggestionsBox.hide();
        }
    });

    // 高亮搜索词
    function highlightSearchTerms() {
        if (typeof currentSearchQuery === 'undefined' || !currentSearchQuery || currentSearchQuery === 'None') return;

        const terms = currentSearchQuery.split(' ').filter(term => term.length > 0);
        const elementsToHighlight = $('.result-title a, .result-intro');

        elementsToHighlight.each(function() {
            let originalHtml = $(this).html();
            let newHtml = originalHtml;
            terms.forEach(term => {
                const regex = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
                newHtml = newHtml.replace(regex, function(match) {
                    if (match.toLowerCase().includes('<span class="highlight">')) {
                        return match;
                    }
                    return '<span class="highlight">' + match + '</span>';
                });
            });
            if (newHtml !== originalHtml) {
                $(this).html(newHtml);
            }
        });
    }

    // 应用高亮
    highlightSearchTerms();

    // 表单提交前的处理
    searchForm.on('submit', function(e) {
        // 清理空的日期和排序参数
        const searchQueryVal = $('#search-input').val();
        const startDateVal = $('#start-date').val();
        const endDateVal = $('#end-date').val();
        const orderByVal = $('#order-by').val();

        // 如果值为空或者是'None'，禁用该字段避免在URL中出现
        if (!searchQueryVal || searchQueryVal === 'None') {
            $('#search-input').prop('disabled', true);
        }
        if (!startDateVal || startDateVal === 'None') {
            $('#start-date').prop('disabled', true);
        }
        if (!endDateVal || endDateVal === 'None') {
            $('#end-date').prop('disabled', true);
        }
        if (!orderByVal || orderByVal === 'None') {
            $('#order-by').prop('disabled', true);
        }

        // 确保 type 字段在提交时启用
        $('#search-type-input').prop('disabled', false);
    });

    // 处理分页跳转表单
    const jumpFormNav = document.getElementById('jump-to-page-form-nav');
    if (jumpFormNav) {
        jumpFormNav.addEventListener('submit', function(event) {
            const pageInput = jumpFormNav.querySelector('input[name="page"]');
            const pageValue = parseInt(pageInput.value, 10);
            const maxPage = parseInt(pageInput.getAttribute('max'), 10);
            const minPage = parseInt(pageInput.getAttribute('min'), 10);

            if (isNaN(pageValue) || pageValue < minPage || pageValue > maxPage) {
                event.preventDefault();
                alert('请输入有效的页码 (在 ' + minPage + ' 和 ' + maxPage + ' 之间)。');
                pageInput.focus();
                pageInput.value = pageInput.defaultValue;
            }
        });
    }

    // 初始化时检查并清理None值
    function cleanNoneValues() {
        const searchInputField = $('#search-input');
        const startDateField = $('#start-date');
        const endDateField = $('#end-date');
        const orderByField = $('#order-by');

        if (searchInputField.val() === 'None') {
            searchInputField.val('');
        }
        if (startDateField.val() === 'None') {
            startDateField.val('');
        }
        if (endDateField.val() === 'None') {
            endDateField.val('');
        }
        if (orderByField.val() === 'None') {
            orderByField.val('');
        }
    }

    // 页面加载时清理None值
    cleanNoneValues();
});