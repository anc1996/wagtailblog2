// wagtailblog3/static/blog/js/resize-manager.js

/**
 * =====================================================
 * 智能三栏可调布局管理器
 * 版本：2.0
 * 功能：支持拖动调整侧栏宽度，带约束、吸附、持久化
 * =====================================================
 */

// ========================================
// 🔥 配置对象（这是之前缺少的！）
// ========================================
const BREAKPOINTS = {
  MOBILE: 768,        // 移动端
  TABLET: 1100,       // 平板端
  DESKTOP_MIN: 1280,  // 桌面最小宽度
  DESKTOP_OPT: 1600   // 桌面最佳宽度
};

const LAYOUT_CONFIG = {
  // 绝对约束（px）
  absolute: {
    left: {
      min: 200,
      max: 400,
      default: 272  // 17rem ≈ 272px
    },
    center: {
      min: 500,
      max: 1000,
      optimal: 700
    },
    right: {
      min: 250,
      max: 450,
      default: 320  // 20rem ≈ 320px
    },
    handleWidth: 8
  },

  // 相对约束（%）
  relative: {
    left: {
      min: 0.12,
      max: 0.25,
      default: 0.17
    },
    center: {
      min: 0.35,
      max: 0.65,
      optimal: 0.50
    },
    right: {
      min: 0.15,
      max: 0.30,
      default: 0.20
    }
  },

  // 吸附点配置
  snapPoints: {
    threshold: 15,  // 吸附阈值（px）
    points: {
      left: [200, 272, 300, 350, 400],
      right: [250, 300, 320, 400, 450]
    }
  },

  // 性能配置
  performance: {
    dragThrottle: 16,
    resizeDebounce: 300,
    saveDebounce: 500
  },

  // 存储配置
  storage: {
    key: 'blog_layout_widths_v2',
    expireDays: 365
  }
};

// ========================================
// 主类
// ========================================
class SmartResizeManager {
  constructor() {
    // DOM 元素
    this.container = document.getElementById('blog-layout-container');
    this.leftSidebar = document.getElementById('sidebar-left');
    this.centerColumn = document.getElementById('blog-center-column');
    this.rightSidebar = document.getElementById('sidebar-right');
    this.leftHandle = document.getElementById('resize-handle-left');
    this.rightHandle = document.getElementById('resize-handle-right');

    if (!this.container) {
      console.warn('❌ 布局容器未找到，跳过初始化');
      return;
    }

    // 拖动状态
    this.state = {
      isDragging: false,
      currentSide: null,
      startX: 0,
      startWidth: 0,
      containerWidth: 0
    };

    // 配置
    this.config = LAYOUT_CONFIG;

    // 初始化
    this.init();
  }

  // ========================================
  // 初始化
  // ========================================
  init() {
    // 检查屏幕宽度
    if (!this.checkScreenSize()) {
      console.log('⚠️ 屏幕宽度不足，拖动功能已禁用');
      return;
    }

    // 加载保存的宽度
    this.loadSavedWidths();

    // 绑定事件
    this.bindEvents();

    // 监听窗口resize
    this.setupResizeObserver();

    // 创建遮罩层
    this.createOverlay();

    console.log('✅ 智能布局管理器初始化完成');
  }

  // ========================================
  // 检查屏幕尺寸
  // ========================================
  checkScreenSize() {
    const width = window.innerWidth;
    return width >= BREAKPOINTS.DESKTOP_MIN;
  }

  // ========================================
  // 创建拖动遮罩层（防止iframe干扰）
  // ========================================
  createOverlay() {
    if (document.querySelector('.resize-overlay')) return;

    const overlay = document.createElement('div');
    overlay.className = 'resize-overlay';
    document.body.appendChild(overlay);
  }

  // ========================================
  // 绑定事件
  // ========================================
  bindEvents() {
    // 左侧分隔条
    if (this.leftHandle) {
      this.leftHandle.addEventListener('mousedown', (e) => this.startDrag(e, 'left'));
      this.leftHandle.addEventListener('dblclick', () => this.resetWidth('left'));
    }

    // 右侧分隔条
    if (this.rightHandle) {
      this.rightHandle.addEventListener('mousedown', (e) => this.startDrag(e, 'right'));
      this.rightHandle.addEventListener('dblclick', () => this.resetWidth('right'));
    }

    // 全局鼠标事件
    document.addEventListener('mousemove', (e) => this.onDrag(e));
    document.addEventListener('mouseup', () => this.stopDrag());

    // 监听Zen模式切换
    this.observeZenMode();
  }

  // ========================================
  // 监听Zen模式切换，更新分隔条显示
  // ========================================
  observeZenMode() {
    const observer = new MutationObserver(() => {
      this.updateHandleVisibility();
    });

    observer.observe(this.container, {
      attributes: true,
      attributeFilter: ['class']
    });
  }

  // ========================================
  // 更新分隔条可见性
  // ========================================
  updateHandleVisibility() {
    if (!this.checkScreenSize()) {
      if (this.leftHandle) this.leftHandle.style.display = 'none';
      if (this.rightHandle) this.rightHandle.style.display = 'none';
      return;
    }

    const leftHidden = this.container.classList.contains('hide-sidebar-left');
    const rightHidden = this.container.classList.contains('hide-sidebar-right');

    if (this.leftHandle) {
      this.leftHandle.style.display = leftHidden ? 'none' : 'block';
    }

    if (this.rightHandle) {
      this.rightHandle.style.display = rightHidden ? 'none' : 'block';
    }
  }

  // ========================================
  // 窗口resize监听
  // ========================================
  setupResizeObserver() {
    let resizeTimeout;

    window.addEventListener('resize', () => {
      clearTimeout(resizeTimeout);

      resizeTimeout = setTimeout(() => {
        if (!this.checkScreenSize()) {
          this.disable();
        } else {
          this.recalculateLayout();
        }
      }, this.config.performance.resizeDebounce);
    });
  }

  // ========================================
  // 禁用拖动功能
  // ========================================
  disable() {
    if (this.leftHandle) this.leftHandle.style.display = 'none';
    if (this.rightHandle) this.rightHandle.style.display = 'none';
    console.log('⚠️ 屏幕宽度不足，拖动功能已禁用');
  }

  // ========================================
  // 重新计算布局（窗口resize后）
  // ========================================
  recalculateLayout() {
    const saved = this.getSavedWidths();
    if (!saved) return;

    const newContainerWidth = this.container.offsetWidth;
    const leftPx = newContainerWidth * (saved.left.percent / 100);
    const rightPx = newContainerWidth * (saved.right.percent / 100);

    const constrained = this.applyConstraints({
      left: leftPx,
      right: rightPx
    }, newContainerWidth);

    if (this.validateLayout(constrained, newContainerWidth)) {
      this.applyWidths(constrained);
    } else {
      console.warn('布局验证失败，恢复默认');
      this.resetToDefault();
    }
  }

  // ========================================
  // 开始拖动
  // ========================================
  startDrag(e, side) {
    e.preventDefault();

    this.state.isDragging = true;
    this.state.currentSide = side;
    this.state.startX = e.clientX;
    this.state.containerWidth = this.container.offsetWidth;

    const sidebar = side === 'left' ? this.leftSidebar : this.rightSidebar;
    this.state.startWidth = sidebar.offsetWidth;

    const handle = side === 'left' ? this.leftHandle : this.rightHandle;
    handle.classList.add('dragging');
    document.body.classList.add('resizing');

    console.log(`🖱️ 开始拖动${side === 'left' ? '左' : '右'}侧栏，起始宽度: ${this.state.startWidth}px`);
  }

  // ========================================
  // 拖动中
  // ========================================
  onDrag(e) {
    if (!this.state.isDragging) return;

    if (this.dragRaf) return;

    this.dragRaf = requestAnimationFrame(() => {
      this.performDrag(e);
      this.dragRaf = null;
    });
  }

  performDrag(e) {
    const { currentSide, startX, startWidth, containerWidth } = this.state;

    const deltaX = e.clientX - startX;

    let newWidth = currentSide === 'left'
      ? startWidth + deltaX
      : startWidth - deltaX;

    const otherSide = currentSide === 'left' ? 'right' : 'left';
    const otherSidebar = currentSide === 'left' ? this.rightSidebar : this.leftSidebar;
    const otherWidth = otherSidebar.offsetWidth;

    const centerWidth = containerWidth - newWidth - otherWidth;

    // 检查中间栏最小宽度
    if (centerWidth < this.config.absolute.center.min) {
      newWidth = containerWidth - otherWidth - this.config.absolute.center.min;

      const minWidth = this.getConstraints(currentSide, containerWidth).min;
      if (newWidth < minWidth) {
        console.warn(`⚠️ 无法继续拖动：中间栏已达最小宽度`);
        return;
      }
    }

    // 应用约束
    const constraints = this.getConstraints(currentSide, containerWidth);
    newWidth = this.clamp(newWidth, constraints.min, constraints.max);

    // 吸附检测
    const snapped = this.checkSnap(newWidth, currentSide);
    if (snapped !== null) {
      newWidth = snapped;

      const handle = currentSide === 'left' ? this.leftHandle : this.rightHandle;
      handle.classList.add('snapping');
      setTimeout(() => handle.classList.remove('snapping'), 300);
    }

    // 应用宽度
    this.applyWidth(currentSide, newWidth);

    // 🔥 触发 window resize 事件，让页面内容重新计算
    if (!this._resizeThrottle) {
      this._resizeThrottle = true;
      requestAnimationFrame(() => {
        window.dispatchEvent(new Event('resize'));
        this._resizeThrottle = false;
      });
    }

    // 更新提示
    this.updateTooltip(currentSide, newWidth, containerWidth);

    // 触发resize事件
    window.dispatchEvent(new Event('resize'));
  }

  // ========================================
  // 停止拖动
  // ========================================
  stopDrag() {
    if (!this.state.isDragging) return;

    const { currentSide } = this.state;

    const handle = currentSide === 'left' ? this.leftHandle : this.rightHandle;
    handle.classList.remove('dragging');
    document.body.classList.remove('resizing');

    this.saveWidths();

    this.state.isDragging = false;
    this.state.currentSide = null;

    console.log(`✅ 拖动结束，已保存宽度`);
  }

  // ========================================
  // 获取约束条件
  // ========================================
  getConstraints(side, containerWidth) {
    const abs = this.config.absolute[side];
    const rel = this.config.relative[side];

    const relMin = containerWidth * rel.min;
    const relMax = containerWidth * rel.max;

    return {
      min: Math.max(abs.min, relMin),
      max: Math.min(abs.max, relMax)
    };
  }

  // ========================================
  // 应用约束
  // ========================================
  applyConstraints(widths, containerWidth) {
    const leftConstraints = this.getConstraints('left', containerWidth);
    const rightConstraints = this.getConstraints('right', containerWidth);

    return {
      left: this.clamp(widths.left, leftConstraints.min, leftConstraints.max),
      right: this.clamp(widths.right, rightConstraints.min, rightConstraints.max)
    };
  }

  // ========================================
  // 吸附检测
  // ========================================
  checkSnap(width, side) {
    const snapPoints = this.config.snapPoints.points[side];
    const threshold = this.config.snapPoints.threshold;

    for (const point of snapPoints) {
      if (Math.abs(width - point) < threshold) {
        return point;
      }
    }

    return null;
  }

  // ========================================
  // 验证布局
  // ========================================
  validateLayout(widths, containerWidth) {
    const { left, right } = widths;
    const centerWidth = containerWidth - left - right;

    const centerMin = this.config.absolute.center.min;
    const centerMax = this.config.absolute.center.max;

    if (centerWidth < centerMin || centerWidth > centerMax) {
      console.error(`❌ 布局验证失败：中间栏宽度 ${centerWidth}px 不在范围内 [${centerMin}, ${centerMax}]`);
      return false;
    }

    const leftConstraints = this.getConstraints('left', containerWidth);
    const rightConstraints = this.getConstraints('right', containerWidth);

    if (left < leftConstraints.min || left > leftConstraints.max) {
      console.error(`❌ 布局验证失败：左侧栏宽度 ${left}px 不在范围内`);
      return false;
    }

    if (right < rightConstraints.min || right > rightConstraints.max) {
      console.error(`❌ 布局验证失败：右侧栏宽度 ${right}px 不在范围内`);
      return false;
    }

    const total = left + centerWidth + right;
    if (Math.abs(total - containerWidth) > 10) {
      console.error(`❌ 布局验证失败：总宽度 ${total}px 不等于容器宽度 ${containerWidth}px`);
      return false;
    }

    return true;
  }

  // ========================================
  // 应用单侧宽度
  // ========================================
  applyWidth(side, width) {
    const varName = side === 'left' ? '--sidebar-left-width' : '--sidebar-right-width';
    this.container.style.setProperty(varName, `${width}px`);
  }

  // ========================================
  // 应用两侧宽度
  // ========================================
  applyWidths(widths) {
    if (widths.left !== undefined) {
      this.applyWidth('left', widths.left);
    }
    if (widths.right !== undefined) {
      this.applyWidth('right', widths.right);
    }
  }

  // ========================================
  // 更新提示
  // ========================================
  updateTooltip(side, width, containerWidth) {
    const handle = side === 'left' ? this.leftHandle : this.rightHandle;
    const tooltip = handle.querySelector('.resize-tooltip');

    if (!tooltip) return;

    const percent = ((width / containerWidth) * 100).toFixed(1);

    tooltip.querySelector('.tooltip-width').textContent = `${Math.round(width)}px`;
    tooltip.querySelector('.tooltip-percent').textContent = `${percent}%`;
  }

  // ========================================
  // 保存宽度
  // ========================================
  saveWidths() {
    const containerWidth = this.container.offsetWidth;
    const leftWidth = this.leftSidebar.offsetWidth;
    const rightWidth = this.rightSidebar.offsetWidth;

    const data = {
      left: {
        px: leftWidth,
        percent: (leftWidth / containerWidth * 100).toFixed(2)
      },
      right: {
        px: rightWidth,
        percent: (rightWidth / containerWidth * 100).toFixed(2)
      },
      timestamp: Date.now(),
      screenWidth: containerWidth
    };

    try {
      localStorage.setItem(this.config.storage.key, JSON.stringify(data));
      console.log('💾 宽度已保存:', data);
    } catch (e) {
      console.error('❌ 保存失败:', e);
    }
  }

  // ========================================
  // 加载保存的宽度
  // ========================================
  loadSavedWidths() {
    const saved = this.getSavedWidths();

    if (!saved) {
      console.log('未找到保存的宽度，使用默认值');
      return;
    }

    const expireTime = this.config.storage.expireDays * 24 * 60 * 60 * 1000;
    if (Date.now() - saved.timestamp > expireTime) {
      console.log('保存的宽度已过期，使用默认值');
      localStorage.removeItem(this.config.storage.key);
      return;
    }

    const currentWidth = this.container.offsetWidth;
    const leftPx = currentWidth * (saved.left.percent / 100);
    const rightPx = currentWidth * (saved.right.percent / 100);

    const constrained = this.applyConstraints({
      left: leftPx,
      right: rightPx
    }, currentWidth);

    if (this.validateLayout(constrained, currentWidth)) {
      this.applyWidths(constrained);
      console.log('✅ 已恢复保存的宽度:', constrained);
    } else {
      console.warn('保存的宽度验证失败，使用默认值');
      this.resetToDefault();
    }
  }

  // ========================================
  // 获取保存的宽度
  // ========================================
  getSavedWidths() {
    try {
      const data = localStorage.getItem(this.config.storage.key);
      return data ? JSON.parse(data) : null;
    } catch (e) {
      console.error('❌ 读取保存的宽度失败:', e);
      return null;
    }
  }

  // ========================================
  // 重置单侧宽度
  // ========================================
  resetWidth(side) {
    const defaultPx = this.config.absolute[side].default;
    this.applyWidth(side, defaultPx);
    this.saveWidths();

    console.log(`🔄 已重置${side === 'left' ? '左' : '右'}侧栏宽度: ${defaultPx}px`);

    const handle = side === 'left' ? this.leftHandle : this.rightHandle;
    handle.classList.add('snapping');
    setTimeout(() => handle.classList.remove('snapping'), 300);
  }

  // ========================================
  // 重置到默认布局
  // ========================================
  resetToDefault() {
    this.applyWidths({
      left: this.config.absolute.left.default,
      right: this.config.absolute.right.default
    });
    this.saveWidths();
    console.log('🔄 已重置为默认布局');
  }

  // ========================================
  // 工具函数：限制范围
  // ========================================
  clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }
}

// ========================================
// 初始化
// ========================================
let resizeManager;

$(function() {
  setTimeout(() => {
    if (window.innerWidth >= BREAKPOINTS.DESKTOP_MIN) {
      resizeManager = new SmartResizeManager();
      window.resizeManager = resizeManager;

      console.log('🎉 拖动布局系统已启动');
    } else {
      console.log('⚠️ 屏幕宽度 < 1280px，拖动功能未启用');
    }
  }, 200);
});