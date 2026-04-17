
$(document).ready(function() {
    // 文章归档年份折叠/展开
    $('.archive-tree .month-toggle').on('click keydown', function(event) {
        // 允许通过 Enter 或 Space 键触发
        if (event.type === 'keydown' && (event.key !== 'Enter' && event.key !== ' ')) {
            return;
        }
        event.preventDefault(); // 阻止默认行为，特别是对于 enter 键

        var $this = $(this);
        var targetId = $this.data('target');
        var $monthsList = $(targetId);
        var $icon = $this.find('i.fa');

        $monthsList.slideToggle(250, function() { // 250ms 动画时长
            if ($monthsList.is(':visible')) {
                $icon.removeClass('fa-plus-square-o').addClass('fa-minus-square-o');
                $this.attr('aria-expanded', 'true');
            } else {
                $icon.removeClass('fa-minus-square-o').addClass('fa-plus-square-o');
                $this.attr('aria-expanded', 'false');
            }
        });
    });
});
