// search/static/search/js/admin_analytics.js
$(document).ready(function () {
    const trendsContainer = $('#search-trends-container');
    if (!trendsContainer.length) {
        console.warn('Search trends container not found.');
        return;
    }

    // 使用事件委托来处理表头点击，这样在表格重建后依然有效
    trendsContainer.on('click', 'th.sortable-header', function(event) {
        const header = $(this);
        const sortBy = header.data('sort-by');
        let currentSortDirection = header.data('current-sort-direction') || ''; // asc, desc, or empty
        let newOrderBy;

        if (currentSortDirection === 'asc') {
            newOrderBy = '-' + sortBy; // 切换到降序
        } else {
            // 如果是降序或未排序，则切换到升序
            newOrderBy = sortBy;
        }
        fetchSearchTrends(newOrderBy);
    });

    function fetchSearchTrends(orderBy) {
        // 从模板中获取基础 URL (假设在 analytics.html 中定义了一个 JS 变量)
        // 或者直接硬编码，但从模板获取更灵活
        const analyticsUrlElement = $('#search-analytics-url');
        if (!analyticsUrlElement.length) {
            console.error('Search analytics URL element not found.');
            return;
        }
        const baseUrl = analyticsUrlElement.data('url');
        const url = `${baseUrl}?order_by=${orderBy}`;

        trendsContainer.addClass('loading'); // 可选：添加加载状态的class

        $.ajax({
            url: url,
            method: 'GET',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json',
            },
            success: function(data) {
                if (data.search_trends) {
                    updateTrendsTable(data.search_trends, data.current_order_by);
                } else {
                    trendsContainer.html('<p>无搜索趋势数据可显示。</p>');
                }
            },
            error: function(error) {
                console.error('Error fetching search trends:', error);
                trendsContainer.html(`<p>加载搜索趋势数据失败: ${error.message}</p>`);
            },
            complete: function() {
                trendsContainer.removeClass('loading'); // 可选：移除加载状态
            }
        });
    }

    function updateTrendsTable(trendsData, currentOrderByValue) {
        const table = trendsContainer.find('.search-trends-table');
        let tbodyHtml = '';

        trendsData.forEach(item => {
            tbodyHtml += `
                <tr>
                    <td>${item.date}</td>
                    <td>${item.total_searches} 次</td>
                </tr>
            `;
        });

        if (table.length) {
            const tbody = table.find('tbody');
            if (tbody.length) {
                tbody.html(tbodyHtml);
            } else { // 如果没有 tbody, 重建整个表格 (不太可能但作为后备)
                rebuildTable(trendsData, currentOrderByValue);
                return;
            }
        } else { // 如果连表格都没有, 重建
            rebuildTable(trendsData, currentOrderByValue);
            return;
        }

        // 更新表头的排序指示器
        const headers = trendsContainer.find('.table-sortable th.sortable-header');
        headers.each(function() {
            const th = $(this);
            const sortByField = th.data('sort-by');
            th.removeClass('sorted-asc sorted-desc');
            th.data('current-sort-direction', '');

            if (currentOrderByValue === sortByField) {
                th.addClass('sorted-asc');
                th.data('current-sort-direction', 'asc');
            } else if (currentOrderByValue === `-${sortByField}`) {
                th.addClass('sorted-desc');
                th.data('current-sort-direction', 'desc');
            }
        });
    }

    function rebuildTable(trendsData, currentOrderByValue) {
        // 这个函数用于在极端情况下（例如初始加载时表格不存在，或更新tbody失败）重建整个表格
        let tableHtml = `
            <table class="listing full-width search-trends-table table-sortable">
                <thead>
                    <tr>
                        <th class="sortable-header ${getSortClass('date', currentOrderByValue)}" data-sort-by="date" data-current-sort-direction="${getSortDirection('date', currentOrderByValue)}">
                            日期
                        </th>
                        <th class="sortable-header ${getSortClass('searches', currentOrderByValue)}" data-sort-by="searches" data-current-sort-direction="${getSortDirection('searches', currentOrderByValue)}">
                            搜索量
                        </th>
                    </tr>
                </thead>
                <tbody>
        `;
        trendsData.forEach(item => {
            tableHtml += `
                <tr>
                    <td>${item.date}</td>
                    <td>${item.total_searches} 次</td>
                </tr>
            `;
        });
        tableHtml += `
                </tbody>
            </table>
        `;
        trendsContainer.html(tableHtml);
    }

    function getSortClass(field, currentOrderBy) {
        if (currentOrderBy === field) {
            return 'sorted-asc';
        } else if (currentOrderBy === '-' + field) {
            return 'sorted-desc';
        }
        return '';
    }
    function getSortDirection(field, currentOrderBy) {
        if (currentOrderBy === field) {
            return 'asc';
        } else if (currentOrderBy === '-' + field) {
            return 'desc';
        }
        return '';
    }

    // 初始加载时，也确保表头的排序状态是正确的 (如果页面是通过普通请求加载的)
    // 这部分现在将通过模板中的 {{ current_order_by }} 来设置初始 class，
    // JS 侧重于点击后的更新。但保留一个 data-current-sort-direction 在 JS 中使用更清晰。
    const initialHeaders = trendsContainer.find('.table-sortable th.sortable-header');
    initialHeaders.each(function() {
        const th = $(this);
        const sortByField = th.data('sort-by');
        const currentOrderParam = new URLSearchParams(window.location.search).get('order_by') || 'date';
        if (currentOrderParam === sortByField) {
            th.data('current-sort-direction', 'asc');
        } else if (currentOrderParam === `-${sortByField}`) {
            th.data('current-sort-direction', 'desc');
        }
    });
});