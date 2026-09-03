/**
 * Mongo 孤儿正文治理后台交互脚本
 * 负责异步富文本预览（先看）、强阻断二次确认（后删）、批量安全清理及 Fencing 冲突反馈。
 */

document.addEventListener('DOMContentLoaded', () => {
  const previewModal = document.getElementById('preview-modal');
  const confirmModal = document.getElementById('confirm-modal');
  const batchConfirmModal = document.getElementById('batch-confirm-modal');
  const toastEl = document.getElementById('orphan-toast');

  const checkAllBox = document.getElementById('check-all-orphans');
  const batchToolbar = document.getElementById('batch-toolbar');
  const batchCountSpan = document.getElementById('batch-selected-count');
  const batchModalCountSpan = document.getElementById('batch-modal-count');
  const btnBatchDeleteTrigger = document.getElementById('btn-batch-delete-trigger');
  const btnFinalBatchDelete = document.getElementById('btn-final-batch-delete');

  let activeCollection = '';
  let activeMongoId = '';
  let activePageId = '';
  let activeCategory = '';

  function showToast(message, type = 'success') {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.className = 'orphan-toast is-visible is-' + type;
    setTimeout(() => {
      toastEl.className = 'orphan-toast';
    }, 4000);
  }

  function getCsrfToken() {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input) return input.value;
    const cookies = document.cookie.split(';');
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.startsWith('csrftoken=')) {
        return decodeURIComponent(cookie.substring(10));
      }
    }
    return '';
  }

  function closeAllModals() {
    if (previewModal) previewModal.classList.remove('is-open');
    if (confirmModal) confirmModal.classList.remove('is-open');
    if (batchConfirmModal) batchConfirmModal.classList.remove('is-open');
  }

  document.querySelectorAll('[data-modal-close]').forEach(btn => {
    btn.addEventListener('click', closeAllModals);
  });

  document.querySelectorAll('.orphan-modal-backdrop').forEach(backdrop => {
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) closeAllModals();
    });
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllModals();
  });

  // --- 批量多选逻辑 ---
  function updateBatchToolbar() {
    const checkedItems = document.querySelectorAll('.orphan-item-check:checked');
    const count = checkedItems.length;
    if (batchCountSpan) batchCountSpan.textContent = count;
    if (batchToolbar) {
      batchToolbar.style.display = count > 0 ? 'flex' : 'none';
    }
    if (checkAllBox) {
      const allEnabled = document.querySelectorAll('.orphan-item-check:not([disabled])');
      checkAllBox.checked = allEnabled.length > 0 && count === allEnabled.length;
    }
  }

  if (checkAllBox) {
    checkAllBox.addEventListener('change', () => {
      const isChecked = checkAllBox.checked;
      document.querySelectorAll('.orphan-item-check:not([disabled])').forEach(cb => {
        cb.checked = isChecked;
      });
      updateBatchToolbar();
    });
  }

  document.querySelectorAll('.orphan-item-check').forEach(cb => {
    cb.addEventListener('change', updateBatchToolbar);
  });

  // 触发批量清理弹窗
  if (btnBatchDeleteTrigger) {
    btnBatchDeleteTrigger.addEventListener('click', () => {
      const checkedItems = document.querySelectorAll('.orphan-item-check:checked');
      if (checkedItems.length === 0) return;
      if (batchModalCountSpan) batchModalCountSpan.textContent = checkedItems.length;
      if (batchConfirmModal) batchConfirmModal.classList.add('is-open');
    });
  }

  // 执行最终批量物理清理
  if (btnFinalBatchDelete) {
    btnFinalBatchDelete.addEventListener('click', async () => {
      btnFinalBatchDelete.disabled = true;
      btnFinalBatchDelete.textContent = '批量清理中...';

      const checkedItems = document.querySelectorAll('.orphan-item-check:checked');
      const itemsPayload = [];
      checkedItems.forEach(cb => {
        itemsPayload.push({
          collection: cb.getAttribute('data-collection'),
          mongo_id: cb.getAttribute('data-mongo-id'),
        });
      });

      try {
        const resp = await fetch('/admin/reports/mongo-orphans/cleanup/', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken(),
          },
          body: JSON.stringify({ items: itemsPayload }),
        });

        const data = await resp.json();

        if (!resp.ok || !data.success) {
          throw new Error(data.error || '批量清理失败');
        }

        showToast(`批量清理完成：成功删除 ${data.deleted_count} 条孤儿文档！`, 'success');
        closeAllModals();

        // 移除被成功清理的 DOM 行
        (data.deleted_records || []).forEach(rec => {
          const row = document.querySelector(`tr[data-row-id="${rec.mongo_id}"]`);
          if (row) {
            row.style.transition = 'opacity 0.3s ease';
            row.style.opacity = '0';
            setTimeout(() => row.remove(), 300);
          }
        });

        setTimeout(() => {
          updateBatchToolbar();
          if (data.deleted_count === itemsPayload.length) {
            window.location.reload();
          }
        }, 600);
      } catch (err) {
        showToast('批量清理遇到错误: ' + err.message, 'error');
      } finally {
        btnFinalBatchDelete.disabled = false;
        btnFinalBatchDelete.textContent = '确认批量物理删除';
      }
    });
  }

  // --- 处理【查看正文】（先看） ---
  document.querySelectorAll('[data-action="preview-orphan"]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const coll = btn.getAttribute('data-collection');
      const mongoId = btn.getAttribute('data-mongo-id');
      const pageId = btn.getAttribute('data-page-id');

      activeCollection = coll;
      activeMongoId = mongoId;
      activePageId = pageId;

      if (!previewModal) return;

      const loadingEl = previewModal.querySelector('#preview-loading');
      const contentEl = previewModal.querySelector('#preview-content');
      const btnDeleteInModal = previewModal.querySelector('#btn-preview-delete');

      loadingEl.style.display = 'flex';
      contentEl.style.display = 'none';
      if (btnDeleteInModal) btnDeleteInModal.style.display = 'none';

      previewModal.classList.add('is-open');

      try {
        const url = `/admin/reports/mongo-orphans/preview/?collection=${encodeURIComponent(coll)}&mongo_id=${encodeURIComponent(mongoId)}`;
        const resp = await fetch(url);
        const data = await resp.json();

        if (!resp.ok || data.error) {
          throw new Error(data.error || '无法读取该正文内容');
        }

        activeCategory = data.category;

        previewModal.querySelector('#preview-title-hint').textContent = data.title_hint || '(无标题)';
        previewModal.querySelector('#preview-collection').textContent = data.collection;
        previewModal.querySelector('#preview-mongo-id').textContent = data.mongo_id;
        previewModal.querySelector('#preview-page-id').textContent = data.page_id || '无归属';
        previewModal.querySelector('#preview-created-at').textContent = data.created_at || '未知时间';
        previewModal.querySelector('#preview-category-label').textContent = data.category_label;
        previewModal.querySelector('#preview-char-count').textContent = data.char_count + ' 字符';
        previewModal.querySelector('#preview-block-count').textContent = (data.block_count || 0) + ' 块';
        previewModal.querySelector('#preview-block-types').textContent = (data.block_types || []).join(', ') || '纯文本';

        const reasonBox = previewModal.querySelector('#preview-reason-box');
        reasonBox.textContent = data.orphan_reason || '';
        if (data.can_delete && data.category === 'orphan_candidate') {
          reasonBox.className = 'orphan-alert-reason is-danger';
        } else {
          reasonBox.className = 'orphan-alert-reason';
        }

        const bodyPre = previewModal.querySelector('#preview-markdown-content');
        bodyPre.textContent = data.markdown_content || '(正文内容为空)';

        const rawPre = previewModal.querySelector('#preview-raw-snippet');
        rawPre.textContent = data.raw_body_snippet || '(无原始片段)';

        // 核心亮点：审查后允许清理（包括完全孤儿与已人工审查确认的历史快照孤儿）
        if (data.can_delete && btnDeleteInModal) {
          btnDeleteInModal.style.display = 'inline-flex';
          btnDeleteInModal.className = 'button button-danger';
          if (data.category === 'referenced_missing_page') {
            btnDeleteInModal.textContent = '已确认无追溯价值，安全清理';
          } else {
            btnDeleteInModal.textContent = '以此为据安全清理';
          }
        }

        loadingEl.style.display = 'none';
        contentEl.style.display = 'flex';
      } catch (err) {
        loadingEl.style.display = 'none';
        showToast('读取正文失败: ' + err.message, 'error');
        closeAllModals();
      }
    });
  });

  // --- 处理单条【安全清理】二次确认弹窗（后删） ---
  function triggerDeleteConfirm(coll, mongoId, pageId) {
    activeCollection = coll;
    activeMongoId = mongoId;
    activePageId = pageId;

    if (!confirmModal) return;

    confirmModal.querySelector('#confirm-coll').textContent = coll;
    confirmModal.querySelector('#confirm-id').textContent = mongoId;
    confirmModal.querySelector('#confirm-page').textContent = pageId || '未知';
    confirmModal.classList.add('is-open');
  }

  document.querySelectorAll('[data-action="delete-orphan"]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const coll = btn.getAttribute('data-collection');
      const mongoId = btn.getAttribute('data-mongo-id');
      const pageId = btn.getAttribute('data-page-id');
      triggerDeleteConfirm(coll, mongoId, pageId);
    });
  });

  const btnPreviewDelete = document.getElementById('btn-preview-delete');
  if (btnPreviewDelete) {
    btnPreviewDelete.addEventListener('click', () => {
      previewModal.classList.remove('is-open');
      triggerDeleteConfirm(activeCollection, activeMongoId, activePageId);
    });
  }

  // 单条最终物理清理 POST
  const btnFinalDelete = document.getElementById('btn-final-delete');
  if (btnFinalDelete) {
    btnFinalDelete.addEventListener('click', async () => {
      btnFinalDelete.disabled = true;
      btnFinalDelete.textContent = '清理中...';

      try {
        const formData = new FormData();
        formData.append('collection', activeCollection);
        formData.append('mongo_id', activeMongoId);

        const resp = await fetch('/admin/reports/mongo-orphans/cleanup/', {
          method: 'POST',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
          body: formData,
        });

        const data = await resp.json();

        if (!resp.ok || !data.success) {
          throw new Error(data.error || '删除失败');
        }

        showToast(`正文孤儿 [${activeMongoId}] 已成功物理清理！`, 'success');
        closeAllModals();

        // 移除或更新对应行
        const row = document.querySelector(`tr[data-row-id="${activeMongoId}"]`);
        if (row) {
          row.style.transition = 'opacity 0.3s ease';
          row.style.opacity = '0';
          setTimeout(() => {
            row.remove();
            updateBatchToolbar();
          }, 300);
        } else {
          setTimeout(() => window.location.reload(), 800);
        }
      } catch (err) {
        showToast('清理失败: ' + err.message, 'error');
      } finally {
        btnFinalDelete.disabled = false;
        btnFinalDelete.textContent = '确认物理删除';
      }
    });
  }
});
