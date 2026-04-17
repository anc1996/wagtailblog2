/**
 * 评论系统 v2.0
 * 改进点：
 * 1. 添加配置对象，消除硬编码
 * 2. 精确的 DOM 选择器
 * 3. 防抖/节流处理
 * 4. 更好的错误处理
 * 5. 可调试模式
 */

const CommentSystem = (function() {
    'use strict';

    // ===== 配置对象 =====
    const Config = {
        // 调试模式（生产环境设为 false）
        debug: true,

        // URL 配置
        urls: {
            login: '/admin/login/',
            loadComments: '/comments/load/',
            loadReplies: '/comments/load-replies/',
            postComment: '/comments/post/',
            editComment: '/comments/edit/',
            deleteComment: '/comments/delete/',
            reactComment: '/comments/react/'
        },

        // 时间配置（毫秒）
        timing: {
            messageAutoHide: 3000,
            successAutoHide: 2000,
            highlightDuration: 3000,
            scrollAnimationDuration: 500,
            slideAnimationDuration: 200,
            debounceDelay: 300
        },

        // UI 文本（便于国际化）
        text: {
            submitting: '提交中...',
            submit: '发表评论',
            saving: '保存中...',
            save: '保存',
            loading: '加载中...',
            loginRequired: '请先登录才能进行此操作！',
            deleteConfirm: '确定要删除这条评论吗？\n此操作不可撤销。',
            deleteWithReplies: '删除这条一级评论将同时删除其下的所有 {count} 条回复，确定要继续吗？',
            emptyContent: '评论内容不能为空！',
            operationFailed: '操作失败，请重试',
            loadFailed: '加载评论失败',
            editFailed: '编辑失败',
            deleteFailed: '删除失败',
            commentSuccess: '评论发表成功',
            editSuccess: '评论已更新',
            deleteSuccess: '评论已删除',
            operationSuccess: '操作成功',
            viewReplies: '查看全部 {count} 条回复',
            hideReplies: '收起 {count} 条回复',
            deleted: '此评论已被删除'
        },

        // 选择器配置
        selectors: {
            commentBox: '#comments-section',
            commentsList: '#comments-list',
            loadMoreBtn: '#load-more-comments',
            mainFormWrapper: '#main-comment-form-wrapper',
            mainFormPlacement: '#initial-comment-form-placement',
            sortBtn: '.sort-btn',
            comment: '.comment',
            nestedComment: '.nested-comment',
            // 精确选择器模板
            commentById: '#comment-{id}',
            repliesById: '#replies-{id}',
            commentTextById: '#comment-text-{id}',
            showRepliesBtn: '.show-replies-btn[data-comment-id="{id}"]'
        }
    };

    // ===== 工具函数 =====
    const Utils = {
        /**
         * 调试日志
         */
        log(...args) {
            if (Config.debug) {
                console.log('[CommentSystem]', ...args);
            }
        },

        /**
         * 错误日志
         */
        error(...args) {
            console.error('[CommentSystem Error]', ...args);
        },

        /**
         * 替换模板字符串
         */
        template(str, data) {
            return str.replace(/\{(\w+)\}/g, (match, key) => {
                return data[key] !== undefined ? data[key] : match;
            });
        },

        /**
         * 防抖函数
         */
        debounce(func, wait) {
            let timeout;
            return function executedFunction(...args) {
                const later = () => {
                    clearTimeout(timeout);
                    func.apply(this, args);
                };
                clearTimeout(timeout);
                timeout = setTimeout(later, wait);
            };
        },

        /**
         * 节流函数
         */
        throttle(func, limit) {
            let inThrottle;
            return function(...args) {
                if (!inThrottle) {
                    func.apply(this, args);
                    inThrottle = true;
                    setTimeout(() => inThrottle = false, limit);
                }
            };
        },

        /**
         * 获取评论的直接操作区域（不包括子评论）
         * 【核心修复】精确选择器
         */
        getCommentActions($comment) {
            return $comment.find('> .inner-box > .comment-content-wrapper > .comment-actions-list');
        },

        /**
         * 获取评论的回复表单容器
         */
        getReplyFormContainer($comment) {
            return $comment.find('> .inner-box > .comment-content-wrapper > .dynamic-reply-form-container').first();
        },

        /**
         * 获取评论的文本区域
         */
        getCommentText($comment) {
            const commentId = $comment.data('comment-id');
            return $comment.find(`#comment-text-${commentId}`);
        },

        /**
         * 平滑滚动到元素
         */
        scrollTo($element, offset = 100) {
            if ($element.length) {
                $('html, body').animate({
                    scrollTop: $element.offset().top - offset
                }, Config.timing.scrollAnimationDuration);
            }
        }
    };

    // ===== 模板管理器 =====
    const TemplateManager = {
        cache: {},

        getTemplate(templateId) {
            if (!this.cache[templateId]) {
                const $template = $(`#${templateId}`);
                if (!$template.length) {
                    Utils.error(`模板 ${templateId} 不存在`);
                    return $();
                }
                this.cache[templateId] = $template.children().first();
            }
            return this.cache[templateId].clone().removeClass('template-hidden');
        },

        showLoginPrompt() {
            const $commentsList = $(Config.selectors.commentsList);
            if ($commentsList.find('.login-prompt').length > 0) return;
            $commentsList.append(this.getTemplate('login-prompt-template'));
        },

        showNoComments() {
            const $commentsList = $(Config.selectors.commentsList);
            if ($commentsList.find('.no-comments').length > 0) return;
            $commentsList.append(this.getTemplate('no-comments-template'));
        },

        showLoadingIndicator($container) {
            $container = $container || $(Config.selectors.commentsList);
            const $loading = this.getTemplate('loading-indicator-template');
            $container.append($loading);
            return $loading;
        },

        showRepliesLoading($container) {
            const $loading = this.getTemplate('replies-loading-template');
            $container.append($loading);
            return $loading;
        },

        createEditForm(commentId, currentContent, editUrl) {
            const $editForm = this.getTemplate('edit-form-template');
            $editForm.find('.edit-textarea').val(currentContent);
            $editForm.find('.save-edit-btn')
                .attr('data-comment-id', commentId)
                .attr('data-url', editUrl);
            $editForm.find('.cancel-edit-btn').attr('data-comment-id', commentId);
            return $editForm;
        },

        showMessage(type, message, $container) {
            const templateId = type === 'error' ? 'error-message-template' : 'success-message-template';
            const $msg = this.getTemplate(templateId);
            const textClass = type === 'error' ? '.error-text' : '.success-text';
            $msg.find(textClass).text(message);

            if ($container && $container.length) {
                $container.append($msg);
                const hideDelay = type === 'error'
                    ? Config.timing.messageAutoHide
                    : Config.timing.successAutoHide;
                setTimeout(() => $msg.fadeOut(300, () => $msg.remove()), hideDelay);
            } else {
                // 无容器时使用 alert（可替换为 toast 组件）
                alert((type === 'error' ? '❌ ' : '✅ ') + message);
            }
        },

        showError(message, $container) {
            this.showMessage('error', message, $container);
        },

        showSuccess(message, $container) {
            this.showMessage('success', message, $container);
        }
    };

    // ===== 认证管理器 =====
    const AuthManager = {
        _isAuthenticated: null,

        init() {
            // 从服务端注入的数据获取认证状态
            try {
                const userData = JSON.parse($('#user-data').text() || '{}');
                this._isAuthenticated = userData.authenticated === true;
            } catch (e) {
                // 降级方案：检查 DOM
                this._isAuthenticated = $(Config.selectors.mainFormWrapper).find('form').length > 0;
            }
            Utils.log('认证状态:', this._isAuthenticated);
        },

        isAuthenticated() {
            if (this._isAuthenticated === null) {
                this.init();
            }
            return this._isAuthenticated;
        },

        getLoginUrl() {
            const currentUrl = encodeURIComponent(window.location.pathname + window.location.search);
            return `${Config.urls.login}?next=${currentUrl}`;
        },

        redirectToLogin() {
            Utils.log('重定向到登录页面');
            window.location.href = this.getLoginUrl();
        },

        requireAuth(callback) {
            if (this.isAuthenticated()) {
                callback();
                return true;
            }
            alert(Config.text.loginRequired);
            this.redirectToLogin();
            return false;
        }
    };

    // ===== AJAX 管理器 =====
    const AjaxManager = {
        getCsrfToken() {
            let token = null;
            if (document.cookie) {
                const cookies = document.cookie.split(';');
                for (let cookie of cookies) {
                    cookie = cookie.trim();
                    if (cookie.startsWith('csrftoken=')) {
                        token = decodeURIComponent(cookie.substring(10));
                        break;
                    }
                }
            }
            return token;
        },

        handleError(xhr, defaultMessage) {
            let message = defaultMessage || Config.text.operationFailed;

            if (xhr.status === 403) {
                message = '权限不足，请重新登录';
                setTimeout(() => AuthManager.redirectToLogin(), 1500);
            } else if (xhr.status === 429) {
                message = xhr.responseJSON?.message || '操作太频繁，请稍后再试';
            } else if (xhr.responseJSON?.message) {
                message = xhr.responseJSON.message;
            }

            TemplateManager.showError(message);
            return message;
        },

        post(url, data = {}) {
            if (typeof data === 'object' && !(data instanceof FormData)) {
                data.csrfmiddlewaretoken = this.getCsrfToken();
            }

            return $.ajax({
                url,
                method: 'POST',
                data,
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                ...(data instanceof FormData ? { processData: false, contentType: false } : {})
            });
        },

        get(url, data = {}) {
            return $.ajax({
                url,
                method: 'GET',
                data,
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            });
        }
    };

    // ===== 表单管理器 =====
    const FormManager = {
        activeReplyForm: null,
        isSubmitting: false,  // 防止重复提交

        resetReplyForms() {
            $('.active-reply-form').remove();
            $('.dynamic-reply-form-container').empty();
            $('.comment').removeClass('comment-replying');
            this.activeReplyForm = null;
            Utils.log('所有回复表单已重置');
        },

        createReplyForm(commentId, username, userId, rootCommentId) {
            Utils.log('创建回复表单:', { commentId, username, userId, rootCommentId });

            this.resetReplyForms();

            const $comment = $(`#comment-${commentId}`);
            const $targetContainer = Utils.getReplyFormContainer($comment);

            if (!$targetContainer.length) {
                Utils.error('未找到回复表单容器');
                alert('无法创建回复表单，请刷新页面重试');
                return;
            }

            const $originalForm = $(Config.selectors.mainFormWrapper).find('form');
            if (!$originalForm.length) {
                Utils.error('未找到原始表单');
                return;
            }

            const $newForm = $originalForm.clone();
            const uniqueId = `reply-form-${Date.now()}`;

            $newForm.attr('id', uniqueId);
            $newForm.find('#comment-content').attr('id', `comment-content-${uniqueId}`);
            $newForm.find('input[name="parent_id"]').val(rootCommentId);
            $newForm.find('input[name="replied_to_user_id"]').val(userId);

            const $wrapper = $('<div>')
                .addClass('active-reply-form')
                .attr('data-reply-to-comment', commentId)
                .attr('data-root-comment', rootCommentId)
                .append($newForm);

            const $textarea = $newForm.find(`#comment-content-${uniqueId}`);
            $textarea.val(`@${username} `);

            const $replyInfo = $newForm.find('#reply-to-info');
            $replyInfo.find('#reply-to-name').text(username);
            $replyInfo.removeClass('template-hidden').show();

            $targetContainer.empty().append($wrapper);
            $textarea.focus();

            const textLength = $textarea.val().length;
            $textarea[0].setSelectionRange(textLength, textLength);

            this.activeReplyForm = $wrapper;
            this.highlightTargetComment(commentId);
            Utils.scrollTo($wrapper);

            Utils.log('回复表单创建成功');
        },

        highlightTargetComment(commentId) {
            $('.comment').removeClass('comment-replying');
            $(`#comment-${commentId}`).addClass('comment-replying');
            setTimeout(() => {
                $(`#comment-${commentId}`).removeClass('comment-replying');
            }, Config.timing.highlightDuration);
        },

        submitForm(form, isReply = false) {
            if (this.isSubmitting) {
                Utils.log('正在提交中，忽略重复请求');
                return $.Deferred().reject().promise();
            }

            const $form = $(form);
            const $submitBtn = $form.find('button[type="submit"]');

            this.isSubmitting = true;
            $submitBtn.prop('disabled', true).text(Config.text.submitting);

            Utils.log('提交表单:', { isReply, action: form.action });

            const formData = new FormData(form);

            return AjaxManager.post(form.action, formData)
                .done((data) => {
                    if (data.status === 'success') {
                        this.handleSubmitSuccess(data, $form, isReply);
                        TemplateManager.showSuccess(data.message || Config.text.commentSuccess);
                    } else {
                        this.handleSubmitError(data, $form);
                    }
                })
                .fail((xhr) => {
                    AjaxManager.handleError(xhr, Config.text.operationFailed);
                })
                .always(() => {
                    this.isSubmitting = false;
                    $submitBtn.prop('disabled', false).text(Config.text.submit);
                });
        },

        handleSubmitSuccess(data, $form, isReply) {
            const parentId = $form.find('input[name="parent_id"]').val();
            Utils.log('表单提交成功:', { parentId, isReply });

            if (parentId) {
                this.handleReplySuccess(data, parentId);
            } else {
                this.handleMainCommentSuccess(data, $form);
            }
        },

        handleReplySuccess(data, parentId) {
            const $repliesContainer = $(`#replies-${parentId}`);
            const $showRepliesBtn = $(`.show-replies-btn[data-comment-id="${parentId}"]`);

            this.resetReplyForms();

            if (!$repliesContainer.length) {
                Utils.log('回复容器不存在，刷新页面');
                location.reload();
                return;
            }

            if ($repliesContainer.is(':visible') && $repliesContainer.html().trim() !== '') {
                const $newHtml = $(data.html).addClass('nested-comment');
                $repliesContainer.append($newHtml);
                Utils.log('新回复已追加');

                if ($showRepliesBtn.length) {
                    const count = $repliesContainer.children('.comment').length;
                    $showRepliesBtn.text(Utils.template(Config.text.hideReplies, { count }));
                }

                setTimeout(() => {
                    const $newReply = $repliesContainer.children('.comment').last();
                    $newReply.addClass('just-added');
                    Utils.scrollTo($newReply);
                }, 100);
            } else {
                CommentLoader.loadReplies(parentId).done(() => {
                    setTimeout(() => {
                        const $newReply = $repliesContainer.children('.comment').last();
                        $newReply.addClass('just-added');
                        Utils.scrollTo($newReply);
                    }, 300);
                });
            }
        },

        handleMainCommentSuccess(data, $form) {
            const $commentsList = $(Config.selectors.commentsList);
            $commentsList.find('.no-comments').remove();
            $commentsList.prepend(data.html);
            $form[0].reset();
            $form.find('.comment-error-message').remove();

            Utils.log('新主评论已添加');

            setTimeout(() => {
                const $newComment = $commentsList.children('.comment').first();
                $newComment.addClass('just-added');
                Utils.scrollTo($newComment);
            }, 100);
        },

        handleSubmitError(data, $form) {
            Utils.error('表单提交失败:', data);
            TemplateManager.showError(data.message || Config.text.operationFailed);

            if (data.errors) {
                $form.find('.comment-error-message').remove();
                for (const [fieldName, errorMessages] of Object.entries(data.errors)) {
                    const $input = $form.find(`[name="${fieldName}"]`);
                    if ($input.length) {
                        $input.after(`<div class="comment-error-message">${errorMessages.join('<br>')}</div>`);
                    }
                }
            }
        }
    };

    // ===== 评论加载器 =====
    const CommentLoader = {
        loadComments(pageNumber = 1, sortBy = 'hot') {
            const $commentsList = $(Config.selectors.commentsList);
            const $loadMoreBtn = $(Config.selectors.loadMoreBtn);
            const pageId = $commentsList.attr('data-page-id');

            $loadMoreBtn.hide();
            const $loading = TemplateManager.showLoadingIndicator();

            return AjaxManager.get(`${Config.urls.loadComments}${pageId}/`, {
                page: pageNumber,
                sort: sortBy
            })
            .done((data) => {
                if (data.status === 'success') {
                    if (pageNumber === 1) {
                        $commentsList.empty();
                        this.restoreMainForm();
                    }

                    $commentsList.append(data.html);
                    this.handleEmptyState(data, pageNumber);

                    if (data.has_next) {
                        $loadMoreBtn.data('page', pageNumber + 1).show();
                    }
                } else {
                    TemplateManager.showError(data.message || Config.text.loadFailed);
                }
            })
            .fail((xhr) => {
                AjaxManager.handleError(xhr, Config.text.loadFailed);
            })
            .always(() => {
                $loading.remove();
            });
        },

        handleEmptyState(data, pageNumber) {
            if (data.total_comments === 0 && pageNumber === 1) {
                if (data.is_authenticated ?? AuthManager.isAuthenticated()) {
                    TemplateManager.showNoComments();
                } else {
                    TemplateManager.showLoginPrompt();
                }
            }
        },

        restoreMainForm() {
            const $container = $(Config.selectors.mainFormWrapper);
            const $placement = $(Config.selectors.mainFormPlacement);

            if ($container.length && $placement.length) {
                $placement.append($container.show());
                const $form = $container.find('form');
                if ($form.length) {
                    $form[0].reset();
                    $form.find('#comment-parent-id').val('');
                    $form.find('#replied-to-user-id').val('');
                    $form.find('#reply-to-info').addClass('template-hidden').hide();
                    $form.find('.comment-error-message').remove();
                }
            }
        },

        loadReplies(commentId) {
            const $repliesContainer = $(`#replies-${commentId}`);
            const $btn = $(`.show-replies-btn[data-comment-id="${commentId}"]`);

            if ($repliesContainer.html().trim() !== '' &&
                !$repliesContainer.find('.replies-loading-indicator').length) {
                $repliesContainer.slideDown(Config.timing.slideAnimationDuration);
                return $.Deferred().resolve().promise();
            }

            const $loading = TemplateManager.showRepliesLoading($repliesContainer);

            return AjaxManager.get(`${Config.urls.loadReplies}${commentId}/`)
                .done((data) => {
                    if (data.status === 'success') {
                        $repliesContainer.html(data.html).slideDown(Config.timing.slideAnimationDuration);
                        if ($btn.length) {
                            $btn.text(Utils.template(Config.text.hideReplies, { count: data.reply_count }));
                        }
                        Utils.log('回复列表加载完成，共', data.reply_count, '条');
                    } else {
                        TemplateManager.showError(data.message || Config.text.loadFailed);
                    }
                })
                .fail((xhr) => {
                    AjaxManager.handleError(xhr, Config.text.loadFailed);
                })
                .always(() => {
                    $loading.remove();
                });
        }
    };

    // ===== 交互管理器 =====
    const InteractionManager = {
        // 使用节流防止快速点击
        handleVote: Utils.throttle(function(commentId, reactionType, url) {
            AuthManager.requireAuth(() => {
                const targetUrl = url || Config.urls.reactComment;

                AjaxManager.post(targetUrl, {
                    comment_id: commentId,
                    reaction_type: reactionType
                })
                .done((data) => {
                    if (data.status === 'success') {
                        InteractionManager.updateVoteDisplay(commentId, data);
                    } else {
                        TemplateManager.showError(data.message || Config.text.operationFailed);
                    }
                })
                .fail((xhr) => {
                    AjaxManager.handleError(xhr, Config.text.operationFailed);
                });
            });
        }, Config.timing.debounceDelay),

        /**
         * 【核心修复】精确更新投票显示
         */
        updateVoteDisplay(commentId, data) {
            const $comment = $(`#comment-${commentId}`);
            // 使用精确选择器，只更新当前评论的计数
            const $actions = Utils.getCommentActions($comment);

            $actions.find('.like-count').text(data.like_count);
            $actions.find('.dislike-count').text(data.dislike_count);
            $actions.find('.vote-link').removeClass('active');

            if (data.action === 'added') {
                $actions.find(`[data-reaction="${data.reaction_type}"]`).addClass('active');
            }

            Utils.log('投票更新:', commentId, data);
        },

        editComment(commentId, url) {
            AuthManager.requireAuth(() => {
                const $comment = $(`#comment-${commentId}`);
                const $commentText = Utils.getCommentText($comment);
                const $actions = Utils.getCommentActions($comment);

                // 获取纯文本内容
                let currentContent = $commentText.clone()
                    .children('.replied-to-user').remove().end()
                    .text().trim();

                // 【修复】只隐藏当前评论的操作按钮
                $actions.hide();

                const $editForm = TemplateManager.createEditForm(commentId, currentContent, url);
                $commentText.html($editForm);
            });
        },

        /**
         * 【修复】取消编辑，不再刷新页面
         */
        cancelEdit(commentId) {
            const $comment = $(`#comment-${commentId}`);
            const $commentText = Utils.getCommentText($comment);
            const $actions = Utils.getCommentActions($comment);
            const $editForm = $commentText.find('.edit-comment-form');

            if ($editForm.length) {
                // 恢复原始内容 - 需要从服务端获取或缓存
                // 简单方案：重新加载该评论区域
                CommentLoader.loadComments(1, $('.sort-btn.active').attr('data-sort') || 'hot');
            }
            $actions.show();
        },

        saveEdit(commentId, newContent, url) {
            if (!newContent.trim()) {
                TemplateManager.showError(Config.text.emptyContent);
                return $.Deferred().reject().promise();
            }

            const $saveBtn = $(`.save-edit-btn[data-comment-id="${commentId}"]`);
            $saveBtn.prop('disabled', true).text(Config.text.saving);

            return AjaxManager.post(url || Config.urls.editComment, {
                comment_id: commentId,
                content: newContent
            })
            .done((data) => {
                if (data.status === 'success') {
                    const $comment = $(`#comment-${commentId}`);
                    const $commentText = Utils.getCommentText($comment);
                    const $actions = Utils.getCommentActions($comment);

                    const repliedToHtml = $commentText.find('.replied-to-user').prop('outerHTML') || '';
                    $commentText.html(repliedToHtml + data.content);
                    $actions.show();

                    TemplateManager.showSuccess(Config.text.editSuccess, $comment);
                } else {
                    TemplateManager.showError(data.message || Config.text.editFailed);
                }
            })
            .fail((xhr) => {
                AjaxManager.handleError(xhr, Config.text.editFailed);
            })
            .always(() => {
                $saveBtn.prop('disabled', false).text(Config.text.save);
            });
        },

        deleteComment(commentId, url) {
            AuthManager.requireAuth(() => {
                const $comment = $(`#comment-${commentId}`);
                const isRootComment = $comment.hasClass('comment') && !$comment.hasClass('nested-comment');

                let confirmMessage = Config.text.deleteConfirm;
                if (isRootComment) {
                    const replyMatch = $comment.find('.show-replies-btn').text().match(/\d+/);
                    if (replyMatch && parseInt(replyMatch[0]) > 0) {
                        confirmMessage = Utils.template(Config.text.deleteWithReplies, { count: replyMatch[0] });
                    }
                }

                if (!confirm(confirmMessage)) return;

                AjaxManager.post(url || Config.urls.deleteComment, { comment_id: commentId })
                    .done((data) => {
                        if (data.status === 'success') {
                            this.handleDeleteSuccess(commentId, data, isRootComment);
                            TemplateManager.showSuccess(Config.text.deleteSuccess);
                        } else {
                            TemplateManager.showError(data.message || Config.text.deleteFailed);
                        }
                    })
                    .fail((xhr) => {
                        AjaxManager.handleError(xhr, Config.text.deleteFailed);
                    });
            });
        },

        handleDeleteSuccess(commentId, data, isRootComment) {
            const $comment = $(`#comment-${commentId}`);

            if (isRootComment && data.deleted_replies) {
                $comment.fadeOut(300, function() { $(this).remove(); });
            } else {
                const $commentText = Utils.getCommentText($comment);
                const $actions = Utils.getCommentActions($comment);

                $commentText.html(`<em>${Config.text.deleted}</em>`);
                $actions.find('a, button').hide();
                $comment.find('.show-replies-btn').hide();
                Utils.getReplyFormContainer($comment).empty();
            }

            if (FormManager.activeReplyForm) {
                FormManager.resetReplyForms();
            }
        }
    };

    // ===== 事件管理器 =====
    const EventManager = {
        bindEvents() {
            // 【优化】将事件委托到 #comments-section 而不是 document
            const $container = $(Config.selectors.commentBox);

            if (!$container.length) {
                Utils.error('评论容器不存在');
                return;
            }

            // 登录按钮
            $container.on('click', '.login-btn', (e) => {
                e.preventDefault();
                AuthManager.redirectToLogin();
            });

            // 排序按钮
            $container.on('click', '.sort-btn', function() {
                const $btn = $(this);
                $('.sort-btn').removeClass('active');
                $btn.addClass('active');
                $(Config.selectors.loadMoreBtn).data('page', 1);
                CommentLoader.loadComments(1, $btn.attr('data-sort'));
            });

            // 加载更多
            $container.on('click', '#load-more-comments', function() {
                const nextPage = $(this).data('page');
                const sortBy = $('.sort-btn.active').attr('data-sort') || 'hot';
                CommentLoader.loadComments(nextPage, sortBy);
            });

            // 展开/折叠回复
            $container.on('click', '.show-replies-btn', function() {
                const $btn = $(this);
                const commentId = $btn.attr('data-comment-id');
                const $repliesContainer = $(`#replies-${commentId}`);

                if ($repliesContainer.is(':hidden')) {
                    CommentLoader.loadReplies(commentId);
                } else {
                    $repliesContainer.slideUp(Config.timing.slideAnimationDuration, function() {
                        $(this).empty();
                    });
                    const match = $btn.text().match(/\d+/);
                    if (match) {
                        $btn.text(Utils.template(Config.text.viewReplies, { count: match[0] }));
                    }
                }
            });

            // 主表单提交
            $container.on('submit', '#initial-comment-form-placement form', function(e) {
                e.preventDefault();
                FormManager.submitForm(this, false);
            });

            // 回复表单提交
            $container.on('submit', '.active-reply-form form', function(e) {
                e.preventDefault();
                FormManager.submitForm(this, true);
            });

            // 回复按钮
            $container.on('click', '.reply-btn', function(e) {
                e.preventDefault();
                e.stopPropagation();
                const $btn = $(this);
                AuthManager.requireAuth(() => {
                    FormManager.createReplyForm(
                        $btn.attr('data-comment-id'),
                        $btn.attr('data-username'),
                        $btn.attr('data-user-id'),
                        $btn.attr('data-root-comment-id')
                    );
                });
            });

            // 取消回复
            $container.on('click', '#cancel-reply', (e) => {
                e.preventDefault();
                FormManager.resetReplyForms();
            });

            // 点赞/踩
            $container.on('click', '.vote-link', function(e) {
                e.preventDefault();
                const $btn = $(this);
                InteractionManager.handleVote(
                    $btn.attr('data-comment-id'),
                    $btn.attr('data-reaction'),
                    $btn.attr('data-url')
                );
            });

            // 编辑
            $container.on('click', '.edit-btn', function() {
                const $btn = $(this);
                InteractionManager.editComment($btn.data('comment-id'), $btn.attr('data-url'));
            });

            // 保存编辑
            $container.on('click', '.save-edit-btn', function() {
                const $btn = $(this);
                const newContent = $btn.closest('.edit-comment-form').find('.edit-textarea').val();
                InteractionManager.saveEdit($btn.data('comment-id'), newContent, $btn.attr('data-url'));
            });

            // 【修复】取消编辑 - 不再刷新页面
            $container.on('click', '.cancel-edit-btn', function() {
                InteractionManager.cancelEdit($(this).data('comment-id'));
            });

            // 删除
            $container.on('click', '.delete-btn', function() {
                const $btn = $(this);
                InteractionManager.deleteComment($btn.attr('data-comment-id'), $btn.attr('data-url'));
            });

            Utils.log('事件绑定完成');
        }
    };

    // ===== 主控制器 =====
    const Controller = {
        init() {
            Utils.log('=== 评论系统初始化 ===');

            if (!$(Config.selectors.commentBox).length) {
                Utils.log('评论区不存在，跳过初始化');
                return;
            }

            AuthManager.init();
            EventManager.bindEvents();

            const pageId = $(Config.selectors.commentsList).attr('data-page-id');
            if (pageId) {
                CommentLoader.loadComments(1, 'hot');
            }

            Utils.log('=== 初始化完成 ===');
        }
    };

    // 公开 API
    return {
        init: Controller.init,
        loadComments: CommentLoader.loadComments.bind(CommentLoader),
        isAuthenticated: AuthManager.isAuthenticated.bind(AuthManager),
        // 暴露配置以便外部自定义
        config: Config
    };
})();

// 文档就绪时初始化
$(document).ready(function() {
    CommentSystem.init();
});