/*!
 * ========================================================================
 * WOW.js - 滚动时触发 CSS 动画的 JavaScript 库
 * 版本 (Version): v1.0.1
 * 发布日期 (Release Date): 2014-08-15
 * 版权 (Copyright): (c) 2014 Matthieu Aussaguel
 * 许可证 (License): MIT
 * 项目地址 (Project URL - 原作者): https://github.com/matthieua/WOW
 * (您文件注释中亦提及: https://github.com/graingert/wow)
 * ========================================================================
 *
 * - **用途 (Purpose)**:
 * WOW.js 是一个与 `animate.css` 配合使用的 JavaScript 库。它可以在用户向下滚动页面时，
 * 当 HTML 元素进入浏览器可视区域后，触发 `animate.css` 中预定义的 CSS 动画效果。
 * 这使得网页元素可以实现优雅的“入场”动画。
 *
 * - **重要性 (Importance)**:
 * 对于希望在用户滚动页面时为元素添加动态入场动画效果的网站来说，这是一个简单易用的解决方案。
 * 它可以有效地吸引用户的注意力，提升页面的视觉吸引力和交互感。
 *
 * - **依赖 (Dependencies)**:
 * - `animate.css` (必需): WOW.js 本身不包含动画效果，它依赖 Animate.css 提供的动画类。
 * 你需要预先在你的项目中引入 `animate.css` 文件。
 * - jQuery (可选): 虽然 WOW.js 的某些早期用法或示例可能结合 jQuery，但 v1.x.x 版本核心不直接依赖 jQuery。
 * 它可以独立运行（Vanilla JS）。
 *
 * - **主要功能 (Main Functions/Capabilities)**:
 * 1.  **滚动可视性检测**: 监听页面滚动事件，判断指定的元素是否进入了浏览器的可视区域。
 * 2.  **动画触发**: 当元素可见时，自动为该元素添加 `animate.css` 中定义的动画类名，从而触发动画。
 * 3.  **配置灵活**:
 * - 可以通过 `data-wow-*` HTML 属性为每个元素单独设置动画类型、延迟、持续时间、重复次数。
 * - 也可以在初始化 `WOW` 对象时传入全局配置。
 * 4.  **自定义设置**: 允许自定义触发动画的 CSS 类名、目标元素的类名、触发偏移量等。
 * 5.  **移动设备支持**: 可以选择在移动设备上禁用动画效果。
 * 6.  **异步内容支持**: 提供了 `.sync()` 方法，用于在动态加载新内容后重新扫描并初始化新的 WOW 元素。
 * 7.  **浏览器兼容性**: 致力于兼容主流现代浏览器，对于不支持 `MutationObserver` 的旧浏览器有降级提示。
 *
 * - **核心类/方法与概念 (Core Class/Methods & Concepts - 基于 v1.0.1)**:
 * 1.  **`WOW` 类 (构造函数)**:
 * - `new WOW(options)`: 创建并初始化 WOW.js 实例。
 * - **`options` 对象包含**:
 * - `boxClass` (默认: 'wow'): 指定需要被 WOW.js 处理的元素的 CSS 类名。
 * - `animateClass` (默认: 'animated'): 当元素可见时，添加到元素上以触发 `animate.css` 动画的类名。
 * - `offset` (默认: 0): 触发动画的偏移量（像素）。元素底部距离视口底部 `offset` 像素时触发。
 * - `mobile` (默认: true): 是否在移动设备上启用动画。
 * - `live` (默认: true): 是否使用 `MutationObserver` 自动检测并初始化动态添加的 WOW 元素。
 * 2.  **WOW 实例方法**:
 * - `init()`: 内部方法，在 DOM 加载完成后调用 `start()`。
 * - `start()`: 核心启动逻辑。查找所有 `boxClass` 元素，如果 WOW 未被禁用，则应用初始隐藏样式，并设置滚动和窗口大小调整的事件监听器，启动定时器 (`scrollCallback`) 来检查元素可见性。
 * - `stop()`: 停止 WOW.js 的所有活动，移除事件监听器和定时器。
 * - `sync()`: （在不支持 `MutationObserver` 时）手动调用以扫描并初始化页面上新增的 WOW 元素。
 * - `doSync(element)`: 内部实际执行同步操作的函数，查找新元素并准备它们。
 * - `show(box)`: 当元素可见时调用，为其添加 `animateClass` 以触发动画。
 * - `applyStyle(box, hidden)`: 应用初始样式（隐藏或显示）和通过 `data-wow-*` 属性定义的动画参数（时长、延迟、次数）。
 * - `resetStyle()`: 如果 WOW.js 被禁用（例如在移动设备上），移除隐藏样式，使所有元素可见。
 * - `customStyle(box, hidden, duration, delay, iteration)`: 实际设置元素 style 属性的函数，包括动画名称、时长等。
 * - `scrollHandler()`: 滚动或窗口调整事件的处理函数，简单地设置 `this.scrolled = true`。
 * - `scrollCallback()`: 通过 `setInterval` 定期调用，检查 `this.scrolled` 标志。如果页面已滚动，则遍历所有 WOW 元素，判断其是否可见，并对可见元素调用 `show()`。
 * - `offsetTop(element)`: 计算元素相对于文档顶部的偏移量。
 * - `isVisible(box)`: 判断元素是否在可视区域内的核心逻辑，考虑了 `offset` 和 `data-wow-offset`。
 * - `disabled()`: 判断当前是否应禁用 WOW.js (基于 `config.mobile` 和是否为移动设备)。
 * 3.  **HTML `data-wow-*` 属性**:
 * - `data-wow-duration`: 定义动画的持续时间 (例如 "2s")。
 * - `data-wow-delay`: 定义动画开始前的延迟时间 (例如 "0.5s")。
 * - `data-wow-offset`: 为特定元素覆盖全局的 `offset` 设置。
 * - `data-wow-iteration`: 定义动画的重复次数。
 * - (动画类型本身是通过 `animate.css` 的类名直接添加到元素上的，例如 `<div class="wow bounceInUp">`)
 * 4.  **内部工具 (`Util` 类)**:
 * - `extend()`: 合并对象。
 * - `isMobile()`: 检测是否为移动设备。
 * 5.  **`MutationObserver` (或其 Polyfill/回退)**:
 * - 用于在 `live: true` 时自动检测通过 JavaScript 动态添加到 DOM 中的新 WOW 元素。如果浏览器不支持，会给出警告。
 * 6.  **`WeakMap` (或其 Polyfill)**:
 * - 用于缓存动画名称，提升性能。
 *
 * - **使用示例 (Example Usage)**:
 * HTML:
 * ```html
 * <head>
 * <link rel="stylesheet" href="css/animate.css">
 * </head>
 * <body>
 * <div class="wow bounceInUp">Content to Reveal</div>
 * <div class="wow fadeInDown" data-wow-delay="0.5s">More Content</div>
 *
 * <script src="js/wow.min.js"></script>
 * <script>
 * new WOW().init();
 * </script>
 * </body>
 * ```
 *
 * - **注意**:
 * 确保在使用此插件前已正确引入 `animate.css` 文件，因为 WOW.js 依赖它来提供实际的动画效果。
 * 你提供的这个 `wow.js` 文件是未压缩的 v1.0.1 源代码，适合开发和学习。
 */

(function() {
  var MutationObserver, Util, WeakMap,
    __bind = function(fn, me){ return function(){ return fn.apply(me, arguments); }; },
    __indexOf = [].indexOf || function(item) { for (var i = 0, l = this.length; i < l; i++) { if (i in this && this[i] === item) return i; } return -1; };

  Util = (function() {
    function Util() {}

    Util.prototype.extend = function(custom, defaults) {
      var key, value;
      for (key in defaults) {
        value = defaults[key];
        if (custom[key] == null) {
          custom[key] = value;
        }
      }
      return custom;
    };

    Util.prototype.isMobile = function(agent) {
      return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(agent);
    };

    return Util;

  })();

  WeakMap = this.WeakMap || this.MozWeakMap || (WeakMap = (function() {
    function WeakMap() {
      this.keys = [];
      this.values = [];
    }

    WeakMap.prototype.get = function(key) {
      var i, item, _i, _len, _ref;
      _ref = this.keys;
      for (i = _i = 0, _len = _ref.length; _i < _len; i = ++_i) {
        item = _ref[i];
        if (item === key) {
          return this.values[i];
        }
      }
    };

    WeakMap.prototype.set = function(key, value) {
      var i, item, _i, _len, _ref;
      _ref = this.keys;
      for (i = _i = 0, _len = _ref.length; _i < _len; i = ++_i) {
        item = _ref[i];
        if (item === key) {
          this.values[i] = value;
          return;
        }
      }
      this.keys.push(key);
      return this.values.push(value);
    };

    return WeakMap;

  })());

  MutationObserver = this.MutationObserver || this.WebkitMutationObserver || this.MozMutationObserver || (MutationObserver = (function() {
    function MutationObserver() {
      console.warn('MutationObserver is not supported by your browser.');
      console.warn('WOW.js cannot detect dom mutations, please call .sync() after loading new content.');
    }

    MutationObserver.notSupported = true;

    MutationObserver.prototype.observe = function() {};

    return MutationObserver;

  })());

  this.WOW = (function() {
    WOW.prototype.defaults = {
      boxClass: 'wow',
      animateClass: 'animated',
      offset: 0,
      mobile: true,
      live: true
    };

    function WOW(options) {
      if (options == null) {
        options = {};
      }
      this.scrollCallback = __bind(this.scrollCallback, this);
      this.scrollHandler = __bind(this.scrollHandler, this);
      this.start = __bind(this.start, this);
      this.scrolled = true;
      this.config = this.util().extend(options, this.defaults);
      this.animationNameCache = new WeakMap();
    }

    WOW.prototype.init = function() {
      var _ref;
      this.element = window.document.documentElement;
      if ((_ref = document.readyState) === "interactive" || _ref === "complete") {
        this.start();
      } else {
        document.addEventListener('DOMContentLoaded', this.start);
      }
      return this.finished = [];
    };

    WOW.prototype.start = function() {
      var box, _i, _len, _ref;
      this.stopped = false;
      this.boxes = (function() {
        var _i, _len, _ref, _results;
        _ref = this.element.querySelectorAll("." + this.config.boxClass);
        _results = [];
        for (_i = 0, _len = _ref.length; _i < _len; _i++) {
          box = _ref[_i];
          _results.push(box);
        }
        return _results;
      }).call(this);
      this.all = (function() {
        var _i, _len, _ref, _results;
        _ref = this.boxes;
        _results = [];
        for (_i = 0, _len = _ref.length; _i < _len; _i++) {
          box = _ref[_i];
          _results.push(box);
        }
        return _results;
      }).call(this);
      if (this.boxes.length) {
        if (this.disabled()) {
          this.resetStyle();
        } else {
          _ref = this.boxes;
          for (_i = 0, _len = _ref.length; _i < _len; _i++) {
            box = _ref[_i];
            this.applyStyle(box, true);
          }
          window.addEventListener('scroll', this.scrollHandler, false);
          window.addEventListener('resize', this.scrollHandler, false);
          this.interval = setInterval(this.scrollCallback, 50);
        }
      }
      if (this.config.live) {
        return new MutationObserver((function(_this) {
          return function(records) {
            var node, record, _j, _len1, _results;
            _results = [];
            for (_j = 0, _len1 = records.length; _j < _len1; _j++) {
              record = records[_j];
              _results.push((function() {
                var _k, _len2, _ref1, _results1;
                _ref1 = record.addedNodes || [];
                _results1 = [];
                for (_k = 0, _len2 = _ref1.length; _k < _len2; _k++) {
                  node = _ref1[_k];
                  _results1.push(this.doSync(node));
                }
                return _results1;
              }).call(_this));
            }
            return _results;
          };
        })(this)).observe(document.body, {
          childList: true,
          subtree: true
        });
      }
    };

    WOW.prototype.stop = function() {
      this.stopped = true;
      window.removeEventListener('scroll', this.scrollHandler, false);
      window.removeEventListener('resize', this.scrollHandler, false);
      if (this.interval != null) {
        return clearInterval(this.interval);
      }
    };

    WOW.prototype.sync = function(element) {
      if (MutationObserver.notSupported) {
        return this.doSync(this.element);
      }
    };

    WOW.prototype.doSync = function(element) {
      var box, _i, _len, _ref, _results;
      if (!this.stopped) {
        if (element == null) {
          element = this.element;
        }
        if (element.nodeType !== 1) {
          return;
        }
        element = element.parentNode || element;
        _ref = element.querySelectorAll("." + this.config.boxClass);
        _results = [];
        for (_i = 0, _len = _ref.length; _i < _len; _i++) {
          box = _ref[_i];
          if (__indexOf.call(this.all, box) < 0) {
            this.applyStyle(box, true);
            this.boxes.push(box);
            this.all.push(box);
            _results.push(this.scrolled = true);
          } else {
            _results.push(void 0);
          }
        }
        return _results;
      }
    };

    WOW.prototype.show = function(box) {
      this.applyStyle(box);
      return box.className = "" + box.className + " " + this.config.animateClass;
    };

    WOW.prototype.applyStyle = function(box, hidden) {
      var delay, duration, iteration;
      duration = box.getAttribute('data-wow-duration');
      delay = box.getAttribute('data-wow-delay');
      iteration = box.getAttribute('data-wow-iteration');
      return this.animate((function(_this) {
        return function() {
          return _this.customStyle(box, hidden, duration, delay, iteration);
        };
      })(this));
    };

    WOW.prototype.animate = (function() {
      if ('requestAnimationFrame' in window) {
        return function(callback) {
          return window.requestAnimationFrame(callback);
        };
      } else {
        return function(callback) {
          return callback();
        };
      }
    })();

    WOW.prototype.resetStyle = function() {
      var box, _i, _len, _ref, _results;
      _ref = this.boxes;
      _results = [];
      for (_i = 0, _len = _ref.length; _i < _len; _i++) {
        box = _ref[_i];
        _results.push(box.setAttribute('style', 'visibility: visible;'));
      }
      return _results;
    };

    WOW.prototype.customStyle = function(box, hidden, duration, delay, iteration) {
      if (hidden) {
        this.cacheAnimationName(box);
      }
      box.style.visibility = hidden ?
 'hidden' : 'visible';
      if (duration) {
        this.vendorSet(box.style, {
          animationDuration: duration
        });
      }
      if (delay) {
        this.vendorSet(box.style, {
          animationDelay: delay
        });
      }
      if (iteration) {
        this.vendorSet(box.style, {
          animationIterationCount: iteration
        });
      }
      this.vendorSet(box.style, {
        animationName: hidden ?
 'none' : this.cachedAnimationName(box)
      });
      return box;
    };

    WOW.prototype.vendors = ["moz", "webkit"];

    WOW.prototype.vendorSet = function(elem, properties) {
      var name, value, vendor, _results;
      _results = [];
      for (name in properties) {
        value = properties[name];
        elem["" + name] = value;
        _results.push((function() {
          var _i, _len, _ref, _results1;
          _ref = this.vendors;
          _results1 = [];
          for (_i = 0, _len = _ref.length; _i < _len; _i++) {
            vendor = _ref[_i];
            _results1.push(elem["" + vendor + (name.charAt(0).toUpperCase()) + (name.substr(1))] = value);
          }
          return _results1;
        }).call(this));
      }
      return _results;
    };

    WOW.prototype.vendorCSS = function(elem, property) {
      var result, style, vendor, _i, _len, _ref;
      style = window.getComputedStyle(elem);
      result = style.getPropertyCSSValue(property);
      _ref = this.vendors;
      for (_i = 0, _len = _ref.length; _i < _len; _i++) {
        vendor = _ref[_i];
        result = result || style.getPropertyCSSValue("-" + vendor + "-" + property);
      }
      return result;
    };

    WOW.prototype.animationName = function(box) {
      var animationName;
      try {
        animationName = this.vendorCSS(box, 'animation-name').cssText;
      } catch (_error) {
        animationName = window.getComputedStyle(box).getPropertyValue('animation-name');
      }
      if (animationName === 'none') {
        return '';
      } else {
        return animationName;
      }
    };

    WOW.prototype.cacheAnimationName = function(box) {
      return this.animationNameCache.set(box, this.animationName(box));
    };

    WOW.prototype.cachedAnimationName = function(box) {
      return this.animationNameCache.get(box);
    };

    WOW.prototype.scrollHandler = function() {
      return this.scrolled = true;
    };

    WOW.prototype.scrollCallback = function() {
      var box;
      if (this.scrolled) {
        this.scrolled = false;
        this.boxes = (function() {
          var _i, _len, _ref, _results;
          _ref = this.boxes;
          _results = [];
          for (_i = 0, _len = _ref.length; _i < _len; _i++) {
            box = _ref[_i];
            if (!(box)) {
              continue;
            }
            if (this.isVisible(box)) {
              this.show(box);
              continue;
            }
            _results.push(box);
          }
          return _results;
        }).call(this);
        if (!(this.boxes.length || this.config.live)) {
          return this.stop();
        }
      }
    };

    WOW.prototype.offsetTop = function(element) {
      var top;
      while (element.offsetTop === void 0) {
        element = element.parentNode;
      }
      top = element.offsetTop;
      while (element = element.offsetParent) {
        top += element.offsetTop;
      }
      return top;
    };

    WOW.prototype.isVisible = function(box) {
      var bottom, offset, top, viewBottom, viewTop;
      offset = box.getAttribute('data-wow-offset') || this.config.offset;
      viewTop = window.pageYOffset;
      viewBottom = viewTop + Math.min(this.element.clientHeight, innerHeight) - offset;
      top = this.offsetTop(box);
      bottom = top + box.clientHeight;
      return top <= viewBottom && bottom >= viewTop;
    };

    WOW.prototype.util = function() {
      return this._util != null ? this._util : this._util = new Util();
    };

    WOW.prototype.disabled = function() {
      return !this.config.mobile && this.util().isMobile(navigator.userAgent);
    };

    return WOW;

  })();

}).call(this);