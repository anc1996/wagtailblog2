// wagtailblog3/static/blog/js/blog_page.js

$(function() {
    console.log("🚀 博客页面脚本初始化...");

    // ===================================
    // 0. 工具函数：获取 CSRF Token (这是修复 ReferenceError 的关键)
    // ===================================
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                // Does this cookie string begin with the name we want?
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    // ===================================
    // 1. 基础插件初始化 (KaTeX)
    // ===================================
    function initKaTeX() {
        try {
            if (typeof renderMathInElement !== 'undefined') {
                renderMathInElement(document.body, {
                    delimiters: [
                        {left: "$$", right: "$$", display: true},
                        {left: "\\[", right: "\\]", display: true},
                        {left: "$", right: "$", display: false},
                        {left: "\\(", right: "\\)", display: false}
                    ],
                    throwOnError: false
                });
            }
        } catch (e) { console.error("KaTeX error", e); }
    }


    // ===================================
    // 2. 表格美化
    // ===================================
    function beautifyTables() {
        try {
            $('.content-block-wrapper[data-block-type="markdown_block"] table:not([class])').each(function() {
                var table = $(this);
                var wrapper = table
                    .addClass('table table-bordered table-hover')
                    .wrap('<div class="table-responsive"></div>')
                    .parent();
                wrapper.attr({
                    role: 'region',
                    tabindex: '0',
                    'aria-label': '可横向滚动的 Markdown 表格'
                });
            });
            console.log("✅ 表格美化完成");
        } catch (e) {
            console.error("❌ 表格美化失败:", e);
        }
    }

    // ===================================
    // 3. 用户反应 (Reactions) 逻辑 (修复版：状态全量刷新)
    // ===================================
    function initReactions() {
        const reactionContainer = $('.reaction-buttons');
        if (reactionContainer.length === 0) return;

        console.log("👍 初始化反应模块");

        // 绑定点击事件
        reactionContainer.on('click', '.reaction-btn', function(e) {
            e.preventDefault();
            const btn = $(this);
            const container = btn.closest('.reaction-buttons');

            const actionUrl = container.data('action-url');
            const reactionId = btn.data('reaction-id');
            const csrftoken = getCookie('csrftoken'); // 现在 getCookie 已定义，不会报错了

            if (!actionUrl) {
                console.error("❌ 缺少 data-action-url");
                return;
            }

            // 防止快速重复点击
            if (btn.hasClass('processing')) return;
            btn.addClass('processing');

            // 发送 AJAX 请求
            $.ajax({
                url: actionUrl,
                type: 'POST',
                data: JSON.stringify({ reaction_id: reactionId }),
                contentType: 'application/json',
                headers: { 'X-CSRFToken': csrftoken },
                success: function(data) {
                    btn.removeClass('processing');

                    if (data.success) {
                        // 调用 UI 更新函数
                        updateReactionUI(container, data, reactionId);
                    } else {
                        console.error("❌ 更新失败:", data.error);
                    }
                },
                error: function(xhr, status, error) {
                    btn.removeClass('processing');
                    console.error("❌ AJAX 错误:", error);
                }
            });
        });
    }

    /**
     * UI 更新函数：无脑刷新所有按钮状态
     * 解决“只加不减”和“多选高亮”问题的核心逻辑
     */
    function updateReactionUI(container, data, clickedId) {
        const allBtns = container.find('.reaction-btn');

        // 遍历所有按钮，使用后端返回的 counts 强制覆盖前端显示
        allBtns.each(function() {
            const currentBtn = $(this);
            const btnId = currentBtn.data('reaction-id');
            const countSpan = currentBtn.find('.count');

            // A. 更新计数：如果后端没有返回该ID的计数，说明为0
            const newCount = (data.counts && data.counts[btnId]) ? data.counts[btnId] : 0;
            countSpan.text(newCount);

            // B. 更新高亮 (Active) 状态
            // 只有当前点击的按钮，且动作是 'added' 或 'changed' 时才高亮
            // 其他所有按钮一律移除高亮，防止出现两个亮着的按钮
            if (btnId === clickedId) {
                if (data.action === 'added' || data.action === 'changed') {
                    currentBtn.addClass('active');
                } else {
                    currentBtn.removeClass('active'); // 'removed'
                }
            } else {
                // 如果当前发生了 'changed' 或 'added'，说明其他按钮一定不再是活跃状态
                if (data.action === 'added' || data.action === 'changed') {
                     currentBtn.removeClass('active');
                }
                // 如果是 'removed'，说明用户取消了点赞，其他按钮本来就没亮，保持原样即可
            }
        });
    }

    // ===================================
    // 4. TOC 容器内监听 (嵌套滚动版) - 支持 H1 - 修复版
    // ===================================
    function initTOC() {
        const tocContainer = document.getElementById('toc-content');
        // ★ 获取文章独立滚动容器
        const articleScrollBox = document.getElementById('article-scroll-container');

        // 仅在三栏桌面布局中使用正文独立滚动；较窄窗口继续使用页面滚动
        const isContainerMode = (window.innerWidth >= 1280 && articleScrollBox);

        // 内容上下文
        const contentContext = document.querySelector('.article-body-content');
        if (!contentContext || !tocContainer) return;

        // 🔥 查询 h1, h2, h3, h4
        const headers = contentContext.querySelectorAll('h1, h2, h3, h4');
        if (headers.length === 0) {
            tocContainer.innerHTML = '<p class="text-muted">暂无目录</p>';
            return;
        }

        tocContainer.innerHTML = '';
        const tocList = document.createElement('ul');
        tocList.className = 'toc-list';

        // 🔥 栈初始层级改为 0，让 h1 成为第一级
        let stack = [{ level: 0, element: tocList }];

        // --- 构建目录 ---
        headers.forEach((header, index) => {
            if (!header.id) header.id = 'heading-' + index;
            const currentLevel = parseInt(header.tagName.substring(1));
            const li = document.createElement('li');
            li.className = 'toc-item';

            // 🔥 为 h1 添加特殊类名
            if (currentLevel === 1) {
                li.classList.add('toc-item-h1');
            }

            li.setAttribute('data-target', header.id);

            const entry = document.createElement('div');
            entry.className = 'toc-entry';
            const toggle = document.createElement('span');
            toggle.className = 'toc-toggle';
            const a = document.createElement('a');
            a.className = 'toc-link';
            a.textContent = header.innerText;
            a.href = 'javascript:void(0);'; // 禁用锚点

            entry.appendChild(toggle);
            entry.appendChild(a);
            li.appendChild(entry);

            // 折叠逻辑 (不变)
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                if (li.querySelector('ul')) {
                    li.classList.toggle('collapsed');
                    const icon = toggle.querySelector('i');
                    if(icon) {
                        icon.classList.toggle('fa-caret-down');
                        icon.classList.toggle('fa-caret-right');
                    }
                }
            });

            // ★★★ 点击跳转：控制内部容器滚动 ★★★
            a.addEventListener('click', (e) => {
                e.preventDefault();
                isClicking = true;

                document.querySelectorAll('.active').forEach(el => el.classList.remove('active'));
                li.classList.add('active');
                a.classList.add('active');

                if (isContainerMode) {
                    // --- 容器模式 ---
                    const headerRect = header.getBoundingClientRect();
                    const boxRect = articleScrollBox.getBoundingClientRect();
                    const relativeOffset = headerRect.top - boxRect.top;

                    articleScrollBox.scrollTo({
                        top: articleScrollBox.scrollTop + relativeOffset - 20,
                        behavior: 'smooth'
                    });
                } else {
                    // --- 移动端 Window 模式 ---
                    const targetTop = header.getBoundingClientRect().top + window.scrollY - 100;
                    window.scrollTo({ top: targetTop, behavior: 'smooth' });
                    const btn = document.getElementById('btn-hide-left');
                    if(btn && window.innerWidth < 992) btn.click();
                }

                setTimeout(() => { isClicking = false; }, 800);
            });

            // ===== 🔥🔥🔥 核心修复：改进栈处理逻辑 🔥🔥🔥 =====
            let parent = stack[stack.length - 1];

            if (currentLevel > parent.level) {
                // 情况1：需要创建更深层级的子菜单

                // 🔥 特殊处理：如果父级是根容器 (level: 0) 且当前是 h1 (level: 1)
                // 直接添加到 tocList，不创建多余的 toc-sub-menu
                if (parent.level === 0 && currentLevel === 1) {
                    tocList.appendChild(li);
                    // 更新栈：h1 的子元素应该添加到 tocList
                    stack.push({ level: currentLevel, element: tocList });
                } else {
                    // 正常情况：h2->h3, h3->h4 等需要创建子菜单
                    const newUl = document.createElement('ul');
                    newUl.className = 'toc-sub-menu';

                    // 将子菜单添加到上一个兄弟元素（即父级标题的 li）
                    const lastSibling = parent.element.lastElementChild;
                    if (lastSibling && lastSibling.tagName === 'LI') {
                        lastSibling.appendChild(newUl);
                    } else {
                        // 如果没有兄弟元素，添加到父容器
                        parent.element.appendChild(newUl);
                    }

                    newUl.appendChild(li);
                    stack.push({ level: currentLevel, element: newUl });
                }
            } else if (currentLevel === parent.level) {
                // 情况2：同级元素，直接添加到父容器
                parent.element.appendChild(li);

            } else {
                // 情况3：currentLevel < parent.level，需要回退栈找到合适的父级
                while (stack.length > 1 && currentLevel <= stack[stack.length - 1].level) {
                    stack.pop();
                }

                // 重新获取父级
                parent = stack[stack.length - 1];

                if (currentLevel > parent.level) {
                    // 回退后发现仍需创建子菜单
                    if (parent.level === 0 && currentLevel === 1) {
                        tocList.appendChild(li);
                        stack.push({ level: currentLevel, element: tocList });
                    } else {
                        const newUl = document.createElement('ul');
                        newUl.className = 'toc-sub-menu';

                        const lastSibling = parent.element.lastElementChild;
                        if (lastSibling && lastSibling.tagName === 'LI') {
                            lastSibling.appendChild(newUl);
                        } else {
                            parent.element.appendChild(newUl);
                        }

                        newUl.appendChild(li);
                        stack.push({ level: currentLevel, element: newUl });
                    }
                } else {
                    // 直接添加到父容器
                    parent.element.appendChild(li);
                }
            }
        });

        // 图标处理
        tocList.querySelectorAll('li.toc-item').forEach(item => {
            const toggle = item.querySelector('.toc-toggle');
            if (item.querySelector('ul')) {
                toggle.innerHTML = '<i class="fa fa-caret-down"></i>';
            } else {
                toggle.classList.add('placeholder');
            }
        });
        tocContainer.appendChild(tocList);

        // ★★★ 滚动监听：监听 articleScrollBox ★★★
        let isClicking = false;
        let scrollTimeout;
        const scrollTarget = isContainerMode ? articleScrollBox : window;

        const onScroll = function() {
            if (isClicking) return;
            if (scrollTimeout) clearTimeout(scrollTimeout);

            scrollTimeout = setTimeout(function() {
                let currentActiveId = null;
                const offsetThreshold = 100;

                for (let i = 0; i < headers.length; i++) {
                    const header = headers[i];

                    if (isContainerMode) {
                        const diff = header.getBoundingClientRect().top - articleScrollBox.getBoundingClientRect().top;
                        if (diff <= offsetThreshold) {
                            currentActiveId = header.id;
                        } else {
                            break;
                        }
                    } else {
                        if (header.getBoundingClientRect().top <= 150) {
                            currentActiveId = header.id;
                        } else {
                            break;
                        }
                    }
                }

                if (currentActiveId) {
                    const currentActive = tocContainer.querySelector('.toc-item.active');
                    if (currentActive && currentActive.dataset.target === currentActiveId) return;

                    document.querySelectorAll('.toc-link.active, .toc-item.active').forEach(el => el.classList.remove('active'));

                    const activeItem = tocContainer.querySelector(`.toc-item[data-target="${currentActiveId}"]`);
                    if (activeItem) {
                        activeItem.classList.add('active');
                        const link = activeItem.querySelector('.toc-link');
                        if(link) link.classList.add('active');

                        // 自动展开父级
                        let parent = activeItem.parentElement;
                        while(parent) {
                            if (parent.tagName === 'UL' && parent.parentElement.classList.contains('toc-item')) {
                                parent.parentElement.classList.remove('collapsed');
                                const icon = parent.parentElement.querySelector('.toc-toggle i');
                                if(icon) {
                                    icon.classList.remove('fa-caret-right');
                                    icon.classList.add('fa-caret-down');
                                }
                            }
                            parent = parent.parentElement;
                        }
                    }
                }
            }, 50);
        };

        scrollTarget.addEventListener('scroll', onScroll);

        console.log('✅ TOC初始化完成，已修复h1对齐问题');
    }

    // ===================================
    // 5. 移动端布局适配
    // ===================================
    function handleMobileLayout() {
        const sidebarRight = document.getElementById('sidebar-right');
        const mobilePlaceholder = document.getElementById('mobile-interactions-placeholder');
        const breakpoint = 1100;

        function adjustLayout() {
            if (window.innerWidth <= breakpoint) {
                if (sidebarRight && sidebarRight.children.length > 0 && mobilePlaceholder) {
                    while (sidebarRight.children.length > 0) {
                        mobilePlaceholder.appendChild(sidebarRight.children[0]);
                    }
                }
            } else {
                if (mobilePlaceholder && mobilePlaceholder.children.length > 0 && sidebarRight) {
                    while (mobilePlaceholder.children.length > 0) {
                        sidebarRight.appendChild(mobilePlaceholder.children[0]);
                    }
                }
            }
        }

        if (sidebarRight || mobilePlaceholder) {
            adjustLayout();
            window.addEventListener('resize', adjustLayout);
        }
    }

    // ===================================
    // [替换] Zen Mode 统一悬浮触发条
    // 找到原来的 initZenMode 函数，整个替换
    // ===================================
    function initZenMode() {
        var container = document.getElementById('blog-layout-container');
        if (!container) return;

        var triggerLeft = document.getElementById('zen-trigger-left');
        var triggerRight = document.getElementById('zen-trigger-right');

        var KEY_LEFT = 'blog_hide_left';
        var KEY_RIGHT = 'blog_hide_right';

        // 切换侧栏状态
        function toggleSide(side) {
            var hideCls = 'hide-sidebar-' + side;
            var bodyCls = 'zen-' + side + '-hidden';
            var key = (side === 'left') ? KEY_LEFT : KEY_RIGHT;

            var isHidden = container.classList.contains(hideCls);

            if (isHidden) {
                // 展开
                container.classList.remove(hideCls);
                document.body.classList.remove(bodyCls);
                localStorage.setItem(key, 'false');
            } else {
                // 收缩
                container.classList.add(hideCls);
                document.body.classList.add(bodyCls);
                localStorage.setItem(key, 'true');
            }

            // 🆕🆕🆕 通知 ResizeManager 更新分隔条显示状态 🆕🆕🆕
            if (window.resizeManager) {
                window.resizeManager.updateHandleVisibility();
            }

            // 触发 resize 让图表重绘
            setTimeout(function() {
                window.dispatchEvent(new Event('resize'));
            }, 400);
        }

        // 初始化读取本地存储
        function initState() {
            if (localStorage.getItem(KEY_LEFT) === 'true') {
                container.classList.add('hide-sidebar-left');
                document.body.classList.add('zen-left-hidden');
            }
            if (localStorage.getItem(KEY_RIGHT) === 'true') {
                container.classList.add('hide-sidebar-right');
                document.body.classList.add('zen-right-hidden');
            }
        }

        // 首次访问提示动画
        function addHintAnimation() {
            var hintKey = 'blog_zen_hint_shown';
            if (!localStorage.getItem(hintKey)) {
                if (triggerLeft) triggerLeft.classList.add('hint-animation');
                if (triggerRight) triggerRight.classList.add('hint-animation');

                setTimeout(function() {
                    if (triggerLeft) triggerLeft.classList.remove('hint-animation');
                    if (triggerRight) triggerRight.classList.remove('hint-animation');
                    localStorage.setItem(hintKey, 'true');
                }, 5000);
            }
        }

        // 执行初始化
        initState();

        // 绑定点击事件
        if (triggerLeft) {
            triggerLeft.onclick = function() {
                toggleSide('left');
            };
        }
        if (triggerRight) {
            triggerRight.onclick = function() {
                toggleSide('right');
            };
        }

        addHintAnimation();
        console.log('✅ Zen Mode 悬浮触发条初始化完成');
    }


    // ===================================
    // 执行所有初始化
    // ===================================
    beautifyTables();
    initKaTeX();

    // 确保 DOM 元素存在后再执行
    setTimeout(function() {
        handleMobileLayout();
        initTOC();
    }, 100);

    initReactions(); // 启动反应逻辑
    // 执行 Zen Mode 初始化
    initZenMode();
    console.log("🎉 博客页面脚本加载完成");
});
// BlogPage 简介：仅在内容确实超过折叠高度时显示展开按钮。
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-blog-intro]').forEach((intro) => {
        const content = intro.querySelector('.blog-intro-content');
        const toggle = intro.querySelector('.blog-intro-toggle');
        if (!content || !toggle) return;

        const label = toggle.querySelector('span');
        const collapsedLines = Number.parseInt(
            getComputedStyle(intro).getPropertyValue('--blog-intro-collapsed-lines'),
            10
        ) || 5;
        const lineHeight = Number.parseFloat(getComputedStyle(content).lineHeight);
        const collapsedHeight = lineHeight * collapsedLines;
        const isLong = content.scrollHeight > collapsedHeight + 4;

        if (!isLong) return;

        intro.classList.add('is-collapsible');
        toggle.hidden = false;

        toggle.addEventListener('click', () => {
            const expanded = intro.classList.toggle('is-expanded');
            toggle.setAttribute('aria-expanded', String(expanded));
            if (label) label.textContent = expanded ? '收起简介' : '展开简介';
        });
    });
});
