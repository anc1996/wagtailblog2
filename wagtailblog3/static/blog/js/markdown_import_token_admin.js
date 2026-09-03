/**
 * Markdown 导入 Token 后台管理增强脚本
 *
 * 功能：
 * 1. 拦截“复制 Token”动作，异步请求解密端点并将密钥明文写入系统剪贴板（避免页面跳转）；
 * 2. 拦截“重新生成 Token”动作，二次确认后异步轮换密钥、更新当前行前缀并将新密钥写入剪贴板；
 * 3. 兼容 Wagtail 8.0 原生 Toast 消息系统（w-messages:add 事件）与现代/降级剪贴板 API。
 */

(function () {
    'use strict';

    /**
     * 获取当前后台页面的 CSRF Token
     * 优先级：#wagtail-config JSON -> 页面 input 隐藏域 -> csrftoken Cookie
     */
    function getCsrfToken() {
        try {
            const configScript = document.getElementById('wagtail-config');
            if (configScript && configScript.textContent) {
                const config = JSON.parse(configScript.textContent);
                if (config.CSRF_TOKEN) {
                    return config.CSRF_TOKEN;
                }
            }
        } catch (err) {
            console.debug('读取 wagtail-config 失败:', err);
        }

        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }

        const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    /**
     * 调用 Wagtail 8 原生 Toast 通知机制
     * @param {string} text 提示文本
     * @param {'success'|'warning'|'error'|'info'} type 消息类型
     */
    function showToast(text, type = 'success') {
        try {
            document.dispatchEvent(
                new CustomEvent('w-messages:add', {
                    bubbles: true,
                    cancelable: false,
                    detail: {
                        clear: true,
                        text: text,
                        type: type,
                    },
                })
            );
        } catch (err) {
            console.warn('派发 Wagtail Toast 消息失败:', err);
        }
    }

    /**
     * 将文本安全写入系统剪贴板
     * 优先使用 navigator.clipboard，并在不支持或权限受阻时降级到 textarea execCommand
     * @param {string} text 待写入剪贴板的字符串
     * @returns {Promise<boolean>} 是否成功写入
     */
    async function copyToClipboard(text) {
        if (!text) {
            return false;
        }

        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            } catch (err) {
                console.warn('navigator.clipboard.writeText 失败，尝试降级兼容方案:', err);
            }
        }

        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.top = '-9999px';
        textarea.style.left = '-9999px';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();

        let successful = false;
        try {
            successful = document.execCommand('copy');
        } catch (err) {
            console.error('execCommand copy 降级写入失败:', err);
        }
        document.body.removeChild(textarea);
        return successful;
    }

    /**
     * 处理复制 Token 流程
     * @param {HTMLElement} btn 触发按钮
     */
    async function handleCopyToken(btn) {
        const copyUrl = btn.getAttribute('data-copy-url');
        if (!copyUrl) {
            showToast('缺少 Token 复制端点地址', 'error');
            return;
        }

        if (btn.dataset.loading === 'true') {
            return;
        }

        btn.dataset.loading = 'true';
        btn.setAttribute('aria-busy', 'true');

        try {
            const csrfToken = getCsrfToken();
            const response = await fetch(copyUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrfToken,
                    'Accept': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });

            const data = await response.json().catch(() => null);

            if (response.ok && data && data.success && data.token) {
                const copied = await copyToClipboard(data.token);
                if (copied) {
                    showToast(data.message || 'Token 密钥已成功复制到剪贴板！', 'success');
                } else {
                    showToast('未能写入剪贴板，请检查浏览器剪贴板权限', 'warning');
                }
            } else {
                const message = (data && data.message) || '复制 Token 失败，请刷新重试';
                const toastType = (data && data.can_rotate) ? 'warning' : 'error';
                showToast(message, toastType);
            }
        } catch (err) {
            console.error('请求复制 Token 发生网络错误:', err);
            showToast('网络请求异常，无法获取 Token 明文', 'error');
        } finally {
            btn.dataset.loading = 'false';
            btn.removeAttribute('aria-busy');
        }
    }

    /**
     * 处理重新生成 Token 流程
     * @param {HTMLElement} btn 触发按钮
     */
    async function handleRotateToken(btn) {
        const rotateUrl = btn.getAttribute('data-rotate-url');
        const tokenName = btn.getAttribute('data-token-name') || '该 Token';

        if (!rotateUrl) {
            showToast('缺少 Token 重新生成端点地址', 'error');
            return;
        }

        const confirmText =
            '确定要重新生成【' +
            tokenName +
            '】的密钥吗？\n\n' +
            '警告：重新生成后旧 Token 将立即失效，生成的新密钥会自动复制到剪贴板。';

        if (!window.confirm(confirmText)) {
            return;
        }

        if (btn.dataset.loading === 'true') {
            return;
        }

        btn.dataset.loading = 'true';
        btn.setAttribute('aria-busy', 'true');

        try {
            const csrfToken = getCsrfToken();
            const response = await fetch(rotateUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrfToken,
                    'Accept': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });

            const data = await response.json().catch(() => null);

            if (response.ok && data && data.success && data.token) {
                const copied = await copyToClipboard(data.token);

                // 更新表格中当前行的 Token 前缀显示
                const row = btn.closest('tr');
                if (row && data.token_prefix) {
                    const prefixCells = Array.from(row.querySelectorAll('td')).filter(function (td) {
                        return td.textContent.trim().startsWith('mdimp_');
                    });
                    if (prefixCells.length > 0) {
                        prefixCells[0].textContent = data.token_prefix;
                    }
                }

                const successMessage =
                    copied ?
                    (data.message || 'Token 已成功重新生成并复制到剪贴板！') :
                    'Token 已重新生成（前缀：' + data.token_prefix + '），但写入剪贴板失败，请手动记录';
                showToast(successMessage, copied ? 'success' : 'warning');
            } else {
                const message = (data && data.message) || '重新生成 Token 失败，请重试';
                showToast(message, 'error');
            }
        } catch (err) {
            console.error('请求重新生成 Token 发生网络错误:', err);
            showToast('网络请求异常，无法重新生成 Token', 'error');
        } finally {
            btn.dataset.loading = 'false';
            btn.removeAttribute('aria-busy');
        }
    }

    /**
     * 全局事件委托：监听“复制 Token”与“重新生成 Token”按钮点击
     */
    document.addEventListener('click', function (event) {
        const copyBtn = event.target.closest('[data-action="copy-markdown-import-token"]');
        if (copyBtn) {
            event.preventDefault();
            event.stopPropagation();
            handleCopyToken(copyBtn);
            return;
        }

        const rotateBtn = event.target.closest('[data-action="rotate-markdown-import-token"]');
        if (rotateBtn) {
            event.preventDefault();
            event.stopPropagation();
            handleRotateToken(rotateBtn);
            return;
        }
    });
})();
