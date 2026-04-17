/*
 * ========================================================================
 * jQuery.appear - jQuery 元素可视区域检测插件
 * 项目地址 (Project URLs):
 * - GitHub: https://github.com/bas2k/jquery.appear/
 * - 作者个人网站 (Author's Website): http://bas2k.ru/
 *
 * ========================================================================
 *
 * - **用途 (Purpose)**:
 * jQuery.appear 是一个小型的 jQuery 插件，用于检测 HTML 元素是否滚动进入了浏览器的可视区域 (viewport)。
 * 当元素首次出现在可视区域或每次出现时，可以触发一个回调函数或自定义事件。
 * 它通常与动画库 (如 `wow.js` 或自定义的 CSS/JS 动画) 配合使用，当元素变得可见时，
 * 可以启动这些动画效果，实现“滚动到元素时才执行动画”的功能，从而提升用户体验和页面性能。
 *
 * - **重要性 (Importance)**:
 * 对于实现滚动触发的动画、懒加载内容、或任何需要在元素进入视窗时执行特定逻辑的场景来说非常有用。
 * 它可以帮助创建更具吸引力和动态性的网页。
 *
 * - **依赖 (Dependencies)**:
 * 依赖 jQuery 库。必须在 jQuery 库加载之后加载此插件。
 *
 * - **主要功能 (Main Functions/Capabilities)**:
 * 1.  **可视性检测**: 核心功能是判断一个或多个 jQuery 选择器选中的元素是否出现在浏览器窗口的可视范围内。
 * 2.  **事件触发**: 当元素进入可视区域时，会触发一个名为 'appear' 的自定义 jQuery 事件。
 * 3.  **回调函数**: 允许用户在元素出现时执行一个自定义的回调函数。
 * 4.  **一次性触发**: 可以配置为仅在元素第一次出现时触发回调或事件 (`one: true`)，或者每次元素进入/离开再进入可视区域时都触发。
 * 5.  **精确度控制**: 可以通过 `accX` 和 `accY` 选项微调水平和垂直方向上的检测精确度或触发的偏移量。
 * 6.  **数据传递**: 支持通过 `data` 选项向回调函数或事件处理器传递任意数据。
 * 7.  **自动检测DOM变化**: 插件会尝试在常见的 DOM 操作（如 append, remove, css, show, hide 等）后自动重新检查元素的可视状态，以适应动态内容。
 *
 * - **核心函数/方法与概念 (Core Functions/Methods & Concepts)**:
 * 1.  `$.fn.appear(fn, options)`:
 * - **说明**: jQuery.appear 插件的主要入口函数，用于在选定的 jQuery 对象上初始化 appear 功能。
 * - **参数**:
 * - `fn` (可选): 当元素出现在可视区域时要执行的回调函数。
 * - `options` (可选): 配置对象，包含以下属性：
 * -    `data`: (任意类型) 传递给回调函数或 'appear' 事件的数据。
 * -    `one`: (布尔值, 默认 `true`) 如果为 `true`，回调函数或事件仅在元素第一次出现时触发一次。如果为 `false`，则每次元素进入可视区域都会触发。
 * -    `accX`: (数字, 默认 `0`) 水平方向的精确度/偏移量。正值表示元素需要更深入视窗 `accX` 像素才算出现，负值则提前。
 * -    `accY`: (数字, 默认 `0`) 垂直方向的精确度/偏移量。
 *
 * 2.  内部 `check()` 函数:
 * - **说明**: 这是插件的核心检测逻辑。它计算元素的位置和尺寸，以及窗口的滚动位置和尺寸，然后判断元素是否在可视范围内。
 * - **触发**: 在窗口滚动 (`scroll`) 时被调用，也在 DOM 发生变化或初始化时被调用。
 *
 * 3.  内部 `modifiedFn()` 函数:
 * - **说明**: 如果用户提供了回调函数 `fn`，`modifiedFn` 会作为 'appear' 事件的处理器。它负责标记元素已出现 (`t.appeared = true`)，
 *            如果设置了 `one: true`，则解绑滚动检查，最后执行用户提供的原始回调函数 `fn`。
 *
 * 4.  `t.appeared` 属性:
 * - **说明**: 插件会给每个被监控的 jQuery 元素对象添加一个 `appeared` 布尔属性，用于追踪该元素当前是否被认为是可见的。
 *
 * 5.  自定义事件 `'appear'`:
 * - **说明**: 当元素进入可视区域时，插件会在该元素上触发 `'appear'` 自定义事件。可以通过 `$(element).on('appear', function(event, data) { ... });` 来监听。
 *
 * 6.  `$.fn.appear.checks` 数组:
 * - **说明**: 一个内部数组，用于存储所有当前页面上需要进行可视性检测的元素的 `check` 函数。
 *
 * 7.  `$.fn.appear.checkAll()`:
 * - **说明**: 遍历 `$.fn.appear.checks` 数组，并执行其中所有的 `check` 函数，从而更新所有被监控元素的可视状态。
 *
 * 8.  `$.fn.appear.run()` 和 `$.fn.appear.timeout`:
 * - **说明**: `run()` 方法用于异步（通过 `setTimeout` 延迟20毫秒）调用 `checkAll()`。
 *             这是一种性能优化手段，避免在高频事件（如连续的DOM操作）中过于频繁地执行检查，起到了类似节流 (debounce/throttle) 的效果。`timeout` 用于存储 `setTimeout` 的ID。
 *
 * 9.  **jQuery DOM操作方法扩展 (Monkey-patching)**:
 * - **说明**: 插件扩展了 jQuery 的一些常用 DOM 操作方法 (如 `append`, `prepend`, `remove`, `addClass`, `show`, `hide` 等)。
 *            在这些原始方法执行完毕后，会自动调用 `$.fn.appear.run()` 来重新检查元素的可视性，以确保动态添加或修改的元素也能被正确处理。
 *
 * - **使用示例 (Example Usage)**:
 * ```javascript
 * // 当 .my-element 首次出现在视窗时，执行回调
 * $('.my-element').appear(function() {
 *      $(this).text('I have appeared!');
 * });
 *
 * // 每次 .another-element 出现在视窗时，都触发 (one: false)
 * $('.another-element').appear(function() {
 *      console.log('Another element appeared again.');
 * }, { one: false, accY: -100 }); // 提前100px触发
 *
 * // 或者监听自定义事件
 * $('.event-element').appear(); // 初始化appear，但不直接提供回调
 * $('.event-element').on('appear', function(event, data) {
 *      console.log('Event element appeared with data:', data);
 * });
 * ```
 */




(function($) {
    $.fn.appear = function(fn, options) {

        var settings = $.extend({

            //arbitrary data to pass to fn
            data: undefined,

            //call fn only on the first appear?
            one: true,

            // X & Y accuracy
            accX: 0,
            accY: 0

        }, options);

        return this.each(function() {

            var t = $(this);

            //whether the element is currently visible
            t.appeared = false;

            if (!fn) {

                //trigger the custom event
                t.trigger('appear', settings.data);
                return;
            }

            var w = $(window);

            //fires the appear event when appropriate
            var check = function() {

                //is the element hidden?
                if (!t.is(':visible')) {

                    //it became hidden
                    t.appeared = false;
                    return;
                }

                //is the element inside the visible window?
                var a = w.scrollLeft();
                var b = w.scrollTop();
                var o = t.offset();
                var x = o.left;
                var y = o.top;

                var ax = settings.accX;
                var ay = settings.accY;
                var th = t.height();
                var wh = w.height();
                var tw = t.width();
                var ww = w.width();

                if (y + th + ay >= b &&
                    y <= b + wh + ay &&
                    x + tw + ax >= a &&
                    x <= a + ww + ax) {

                    //trigger the custom event
                    if (!t.appeared) t.trigger('appear', settings.data);

                } else {

                    //it scrolled out of view
                    t.appeared = false;
                }
            };

            //create a modified fn with some additional logic
            var modifiedFn = function() {

                //mark the element as visible
                t.appeared = true;

                //is this supposed to happen only once?
                if (settings.one) {

                    //remove the check
                    w.unbind('scroll', check);
                    var i = $.inArray(check, $.fn.appear.checks);
                    if (i >= 0) $.fn.appear.checks.splice(i, 1);
                }

                //trigger the original fn
                fn.apply(this, arguments);
            };

            //bind the modified fn to the element
            if (settings.one) t.one('appear', settings.data, modifiedFn);
            else t.bind('appear', settings.data, modifiedFn);

            //check whenever the window scrolls
            w.scroll(check);

            //check whenever the dom changes
            $.fn.appear.checks.push(check);

            //check now
            (check)();
        });
    };

    //keep a queue of appearance checks
    $.extend($.fn.appear, {

        checks: [],
        timeout: null,

        //process the queue
        checkAll: function() {
            var length = $.fn.appear.checks.length;
            if (length > 0) while (length--) ($.fn.appear.checks[length])();
        },

        //check the queue asynchronously
        run: function() {
            if ($.fn.appear.timeout) clearTimeout($.fn.appear.timeout);
            $.fn.appear.timeout = setTimeout($.fn.appear.checkAll, 20);
        }
    });

    //run checks when these methods are called
    $.each(['append', 'prepend', 'after', 'before', 'attr',
        'removeAttr', 'addClass', 'removeClass', 'toggleClass',
        'remove', 'css', 'show', 'hide'], function(i, n) {
        var old = $.fn[n];
        if (old) {
            $.fn[n] = function() {
                var r = old.apply(this, arguments);
                $.fn.appear.run();
                return r;
            }
        }
    });

})(jQuery);
