(function ($) {
    'use strict';

    $(function () {
        $('[data-archive-tree]').each(function () {
            var $tree = $(this);

            $tree.on('click', '.month-toggle', function () {
                var $toggle = $(this);
                var $monthsPanel = $tree.find($toggle.attr('data-target'));
                var willExpand = $toggle.attr('aria-expanded') !== 'true';

                $toggle.attr('aria-expanded', String(willExpand));
                $toggle.attr('aria-label', (willExpand ? '收起' : '展开') + $toggle.attr('aria-controls').replace('months-', '') + '年月份');
                $toggle.find('i.fa')
                    .toggleClass('fa-plus', !willExpand)
                    .toggleClass('fa-minus', willExpand);

                if (willExpand) {
                    $monthsPanel.prop('hidden', false).hide().slideDown(180);
                } else {
                    $monthsPanel.stop(true, true).slideUp(180, function () {
                        $monthsPanel.prop('hidden', true).removeAttr('style');
                    });
                }
            });

            $tree.on('click', '.archive-more-toggle', function () {
                var $toggle = $(this);
                var expanded = $toggle.attr('aria-expanded') === 'true';
                var $olderYears = $tree.find('.archive-year-older');
                var hiddenCount = $olderYears.length;

                $toggle.attr('aria-expanded', String(!expanded));
                $toggle.find('.archive-more-label').text(
                    expanded ? '查看更早的 ' + hiddenCount + ' 个年份' : '收起更早年份'
                );
                $olderYears.toggleClass('is-hidden', expanded);
            });
        });
    });
})(jQuery);
