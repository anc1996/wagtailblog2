(function ($) {
    'use strict';

    // 等待 flatpickr 加载完成
    $(document).ready(function() {
        // 检查 flatpickr 是否已加载
        if (typeof window.flatpickr === 'undefined') {
            console.error('flatpickr is not loaded');
            return;
        }

        var fp = window.flatpickr;
        var Mandarin = {
            weekdays: {
                shorthand: ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],
                longhand: [
                    "星期日",
                    "星期一",
                    "星期二",
                    "星期三",
                    "星期四",
                    "星期五",
                    "星期六",
                ],
            },
            months: {
                shorthand: [
                    "一月",
                    "二月",
                    "三月",
                    "四月",
                    "五月",
                    "六月",
                    "七月",
                    "八月",
                    "九月",
                    "十月",
                    "十一月",
                    "十二月",
                ],
                longhand: [
                    "一月",
                    "二月",
                    "三月",
                    "四月",
                    "五月",
                    "六月",
                    "七月",
                    "八月",
                    "九月",
                    "十月",
                    "十一月",
                    "十二月",
                ],
            },
            rangeSeparator: " 至 ",
            weekAbbreviation: "周",
            scrollTitle: "滚动切换",
            toggleTitle: "点击切换 12/24 小时时制",
        };

        // 设置中文语言包
        fp.l10ns.zh = Mandarin;
        
        // 如果 flatpickr 已经作为 jQuery 插件加载，则设置 jQuery 插件的中文支持
        if ($.fn.flatpickr) {
            $.fn.flatpickr.zh = fp.l10ns.zh;
        }
    });
})(jQuery);
