/*
 * =================================================================
 * 文件: blog_index_page.js
 * 描述: 使用 jQuery 实现博客索引页筛选表单中主/次排序的联动效果。
 * =================================================================
*/

// 等同于 document.addEventListener('DOMContentLoaded', ...)，确保在DOM加载完毕后执行
$(function() {

    // 使用jQuery选择器获取元素
    const $primarySort = $('#sort_primary');
    const $secondarySort = $('#sort_secondary');

    // 定义所有可能的次要排序选项 (此部分逻辑不变)
    const options = {
        title: [
            { value: 'title_asc', text: '标题 (A→Z)' },
            { value: 'title_desc', text: '标题 (Z→A)' }
        ],
        date: [
            { value: 'date_desc', text: '时间 (新→旧)' },
            { value: 'date_asc', text: '时间 (旧→新)' }
        ]
    };

    // 功能函数：根据主排序的值，更新次要排序的选项 (使用jQuery重写)
    function updateSecondaryOptions() {
        const primaryValue = $primarySort.val(); // 使用 .val() 获取值

        // 使用 .empty() 清空当前选项，比 innerHTML='' 更安全
        $secondarySort.empty();

        const secondaryType = primaryValue.includes('date') ? 'title' : 'date';

        // 使用 $.each 循环并用 jQuery 的方式添加选项
        $.each(options[secondaryType], function(index, opt) {
            $secondarySort.append(
                $('<option>', {
                    value: opt.value,
                    text: opt.text
                })
            );
        });
    }

    // 功能函数：在页面加载时，设置下拉框的默认选中项 (使用jQuery重写)
    function setInitialValues() {
        // 1. 设置主排序的选中项
        //    从主排序元素上直接读取 data-* 属性来获取从后端模板传递的值
        const primaryFromServer = $primarySort.data('current-value');
        if (primaryFromServer) {
            $primarySort.val(primaryFromServer); // 使用 .val(value) 设置选中
        }

        // 2. 立即更新一次次要排序的选项列表
        updateSecondaryOptions();

        // 3. 设置次要排序的选中项
        const secondaryFromServer = $secondarySort.data('current-value');
        if (secondaryFromServer) {
            $secondarySort.val(secondaryFromServer);
        }
    }

    // 使用jQuery的 .on('change', ...) 绑定事件监听
    $primarySort.on('change', updateSecondaryOptions);

    // 初始化页面
    setInitialValues();

});