document.addEventListener("DOMContentLoaded", () => {
  const refresh = document.querySelector("#log-auto-refresh");
  if (refresh) {
    const saved = window.sessionStorage.getItem("observability-refresh") || refresh.dataset.current || "0";
    refresh.value = saved;
    if (Number(saved) > 0) {
      window.setTimeout(() => window.location.reload(), Number(saved) * 1000);
    }
    refresh.addEventListener("change", () => {
      window.sessionStorage.setItem("observability-refresh", refresh.value);
      window.location.search = refresh.value === "0" ? "" : `?refresh=${refresh.value}`;
    });
  }

  document.querySelectorAll(".log-copy").forEach((button) => {
    button.addEventListener("click", () => navigator.clipboard.writeText(button.parentElement.querySelector(".log-raw").textContent));
  });
  document.querySelectorAll(".log-copy-trace").forEach((button) => {
    button.addEventListener("click", () => navigator.clipboard.writeText(button.parentElement.querySelector(".log-traceback").textContent));
  });

  const selectionForm = document.querySelector("[data-log-selection-form]");
  if (selectionForm) {
    const targetType = selectionForm.querySelector("#id_target_type");
    const clearTarget = selectionForm.querySelector("#id_target");
    const updateTarget = () => {
      clearTarget.disabled = targetType.value === "business" || targetType.value === "all";
    };
    targetType.addEventListener("change", updateTarget);
    updateTarget();
  }

  const period = document.querySelector("#id_period");
  const customStart = document.querySelector("#id_custom_start");
  const customEnd = document.querySelector("#id_custom_end");
  if (period && customStart && customEnd) {
    const updateCustomTime = () => {
      const hidden = period.value !== "custom";
      customStart.closest(".field").hidden = hidden;
      customEnd.closest(".field").hidden = hidden;
    };
    period.addEventListener("change", updateCustomTime);
    updateCustomTime();
  }

  const formatBytes = (bytes) => {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / (1024 ** index);
    return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
  };

  let activeSelection = null;
  let previewRequest = 0;

  const dialogElements = () => {
    const form = document.querySelector("[data-log-dialog-form]");
    return form ? { form, dialog: form.closest("[data-log-dialog]") } : null;
  };

  const setPreviewError = (form, message) => {
    const error = form.closest("[data-log-dialog]").querySelector("[data-preview-error]");
    error.textContent = message;
    error.hidden = !message;
    form.querySelector("[data-log-submit]").disabled = true;
  };

  const updateSubmitState = (form) => {
    const expected = form.closest("[data-log-dialog]").querySelector("[data-preview-confirmation]").textContent;
    const confirmationInput = form.elements.confirmation;
    form.querySelector("[data-log-submit]").disabled = confirmationInput.value !== expected || !form.elements.preview_token.value;
  };

  const renderPreview = (form, data) => {
    const dialog = form.closest("[data-log-dialog]");
    dialog.querySelector("[data-preview-target]").textContent = data.target_label;
    dialog.querySelector("[data-preview-current-count]").textContent = data.current.file_count;
    dialog.querySelector("[data-preview-current-size]").textContent = formatBytes(data.current.total_bytes);
    dialog.querySelector("[data-preview-rotated-count]").textContent = data.rotated.file_count;
    dialog.querySelector("[data-preview-rotated-size]").textContent = formatBytes(data.rotated.total_bytes);
    dialog.querySelector("[data-preview-total-count]").textContent = data.total.file_count;
    dialog.querySelector("[data-preview-total-size]").textContent = formatBytes(data.total.total_bytes);
    dialog.querySelector("[data-preview-confirmation]").textContent = data.confirmation_text;

    const rotations = dialog.querySelector("[data-preview-rotations]");
    rotations.replaceChildren(...data.rotations.map((rotation) => {
      const row = document.createElement("tr");
      [`.${rotation.rotation}`, rotation.file_count, formatBytes(rotation.total_bytes)].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      });
      return row;
    }));

    form.elements.idempotency_key.value = data.idempotency_key;
    form.elements.preview_token.value = data.preview_token;
    form.elements.confirmation.value = "";
    setPreviewError(form, "");
    updateSubmitState(form);
  };

  const loadPreview = async (form) => {
    const requestId = ++previewRequest;
    form.elements.preview_token.value = "";
    setPreviewError(form, "正在重新计算清理预览…");
    const query = new URLSearchParams({
      target_type: activeSelection.targetType,
      target: activeSelection.target,
      kind: form.elements.kind.value,
      scope: form.elements.scope.value,
    });
    try {
      const response = await fetch(`${form.dataset.previewUrl}?${query}`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const data = await response.json();
      if (requestId !== previewRequest) return;
      if (!response.ok) {
        const firstError = Object.values(data.errors || {}).flat()[0];
        throw new Error(firstError?.message || "无法生成清理预览");
      }
      renderPreview(form, data);
    } catch (error) {
      if (requestId === previewRequest) setPreviewError(form, error.message || "无法生成清理预览");
    }
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".js-log-clear");
    if (!link) return;
    const trigger = document.querySelector("#log-clear-dialog-trigger");
    if (!trigger) return;
    event.preventDefault();
    activeSelection = {
      targetType: link.dataset.targetType,
      target: link.dataset.target || "",
      kind: link.dataset.kind || "",
      scope: link.dataset.scope || "all",
    };
    trigger.click();
    window.setTimeout(() => {
      const elements = dialogElements();
      if (!elements) {
        window.location.assign(link.href);
        return;
      }
      const { form } = elements;
      form.elements.target_type.value = activeSelection.targetType;
      form.elements.target.value = activeSelection.target;
      form.elements.kind.value = activeSelection.kind;
      form.elements.scope.value = activeSelection.scope;
      loadPreview(form);
    }, 0);
  });

  document.addEventListener("change", (event) => {
    const form = event.target.closest("[data-log-dialog-form]");
    if (form && (event.target.name === "kind" || event.target.name === "scope")) {
      loadPreview(form);
    }
  });

  document.addEventListener("input", (event) => {
    const form = event.target.closest("[data-log-dialog-form]");
    if (form && event.target.name === "confirmation") updateSubmitState(form);
  });

  // ==========================================================================
  // 审计台账 (Audits) 高级筛选折叠与详情模态弹窗交互
  // ==========================================================================
  const toggleAdvBtn = document.querySelector("#toggle-advanced-btn");
  const advPanel = document.querySelector("#advanced-filter-panel");
  if (toggleAdvBtn && advPanel) {
    toggleAdvBtn.addEventListener("click", () => {
      const isHidden = advPanel.hidden;
      advPanel.hidden = !isHidden;
      toggleAdvBtn.setAttribute("aria-expanded", String(isHidden));
      toggleAdvBtn.textContent = isHidden ? "高级筛选 ▴" : "高级筛选 ▾";
    });

    // 如果高级筛选中已有填写的参数，默认展开
    const hasAdvancedValues = Array.from(advPanel.querySelectorAll("input, select")).some(
      (input) => input.value && input.value.trim() !== ""
    );
    if (hasAdvancedValues) {
      advPanel.hidden = false;
      toggleAdvBtn.setAttribute("aria-expanded", "true");
      toggleAdvBtn.textContent = "高级筛选 ▴";
    }
  }

  // 审计详情模态窗口
  const auditModal = document.querySelector("#audit-detail-modal");
  if (auditModal) {
    let currentAuditData = null;

    const closeModal = () => {
      auditModal.hidden = true;
      auditModal.style.display = "none";
      document.body.style.overflow = "";
    };

    const openModal = () => {
      auditModal.hidden = false;
      auditModal.style.display = "flex";
      document.body.style.overflow = "hidden";
      const firstFocus = auditModal.querySelector(".log-modal-close");
      if (firstFocus) firstFocus.focus();
    };

    // 全局事件委托：关闭弹窗（含右上角叉号、底部关闭按钮与背景暗层遮罩）
    document.addEventListener("click", (e) => {
      if (e.target.closest(".js-close-audit-modal")) {
        closeModal();
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !auditModal.hidden) {
        closeModal();
      }
    });

    // Tab 标签切换
    const tabs = auditModal.querySelectorAll(".log-modal-tab");
    const panels = auditModal.querySelectorAll(".log-tab-panel");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => {
          t.classList.remove("active");
          t.setAttribute("aria-selected", "false");
        });
        panels.forEach((p) => {
          p.classList.remove("active");
          p.hidden = true;
        });
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        const targetPanel = auditModal.querySelector(`#${tab.dataset.tab}`);
        if (targetPanel) {
          targetPanel.classList.add("active");
          targetPanel.hidden = false;
        }
      });
    });

    // 复制 JSON 数据
    const copyBtn = document.querySelector("#copy-audit-json-btn");
    const copyFeedback = document.querySelector("#copy-feedback");
    if (copyBtn && copyFeedback) {
      copyBtn.addEventListener("click", () => {
        if (!currentAuditData) return;
        const text = JSON.stringify(currentAuditData.raw_details || {}, null, 2);
        navigator.clipboard.writeText(text).then(() => {
          copyFeedback.hidden = false;
          window.setTimeout(() => {
            copyFeedback.hidden = true;
          }, 2000);
        });
      });
    }

    // 全量清单模糊搜索
    const allFilesFilter = document.querySelector("#all-files-filter");
    if (allFilesFilter) {
      allFilesFilter.addEventListener("input", () => {
        const query = allFilesFilter.value.toLowerCase().trim();
        const rows = auditModal.querySelectorAll("#all-files-tbody tr");
        rows.forEach((row) => {
          if (row.classList.contains("empty-hint-row")) return;
          const text = row.textContent.toLowerCase();
          row.style.display = text.includes(query) ? "" : "none";
        });
      });
    }

    // 格式化文件大小
    const renderFileSize = (bytes) => {
      if (typeof formatBytes === "function") return formatBytes(bytes);
      if (!bytes) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      const i = Math.floor(Math.log(bytes) / Math.log(1024));
      return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
    };

    // 渲染模态弹窗数据
    const populateModal = (data) => {
      currentAuditData = data;
      const setEl = (sel, val) => {
        const el = document.querySelector(sel);
        if (el) el.textContent = val ?? "-";
      };

      setEl("#audit-modal-title", `审计详情报告 #${data.id} - ${data.target}`);
      setEl("#audit-modal-subtitle", `操作人: ${data.user} | IP: ${data.ip_address} | 执行时间: ${data.created_at} | 耗时: ${data.duration_display}`);

      // KPI 卡片
      setEl("#kpi-freed", renderFileSize(data.bytes_freed));
      setEl("#kpi-files", String(data.files_before ?? 0));
      setEl("#kpi-changed", String(data.changed_files ? data.changed_files.length : 0));
      setEl("#kpi-failed", String(data.failed_files ?? 0));
      setEl("#kpi-duration", data.duration_display || "-");

      setEl("#tab-changed-count", String(data.changed_files ? data.changed_files.length : 0));
      setEl("#tab-all-count", String(data.file_results ? data.file_results.length : 0));

      // 1. 变动文件列表
      const changedTbody = document.querySelector("#changed-files-tbody");
      if (changedTbody) {
        changedTbody.innerHTML = "";
        const changedItems = (data.file_results || []).filter(
          (item) => item.outcome === "truncated" || item.outcome === "unlinked" || (item.bytes_freed && item.bytes_freed > 0)
        );

        if (changedItems.length === 0) {
          changedTbody.innerHTML = '<tr><td colspan="5" class="empty-hint">未发生实际物理截断或删除变动（匹配文件已处于就绪或缺失状态）</td></tr>';
        } else {
          changedItems.forEach((item) => {
            const tr = document.createElement("tr");
            const actionText = item.action === "truncate" ? "原地截断" : "删除清理";
            const statusText = item.succeeded ? '<span class="status-dot dot-success"></span>成功' : '<span class="status-dot dot-error"></span>失败';
            tr.innerHTML = `
              <td><code>${item.file}</code></td>
              <td><span class="log-tag-badge ${item.action === 'truncate' ? 'tag-domain' : 'tag-business'}">${actionText}</span></td>
              <td>${renderFileSize(item.bytes_before)}</td>
              <td class="text-freed-bold">${renderFileSize(item.bytes_freed)}</td>
              <td>${statusText}</td>
            `;
            changedTbody.appendChild(tr);
          });
        }
      }

      // 2. 全量文件列表
      const allTbody = document.querySelector("#all-files-tbody");
      if (allTbody) {
        allTbody.innerHTML = "";
        const fileResults = data.file_results || [];
        if (fileResults.length === 0) {
          allTbody.innerHTML = '<tr class="empty-hint-row"><td colspan="6" class="empty-hint">暂无匹配文件</td></tr>';
        } else {
          fileResults.forEach((item) => {
            const tr = document.createElement("tr");
            let outcomeLabel = item.outcome;
            if (item.outcome === "already_absent") outcomeLabel = "文件已不存在 (无需清理)";
            else if (item.outcome === "truncated") outcomeLabel = "原地截断清空内容";
            else if (item.outcome === "unlinked") outcomeLabel = "物理删除轮转归档";

            tr.innerHTML = `
              <td><code>${item.file}</code></td>
              <td>${item.action}</td>
              <td><small>${outcomeLabel}</small></td>
              <td>${renderFileSize(item.bytes_before)}</td>
              <td>${renderFileSize(item.bytes_freed)}</td>
              <td>${item.succeeded ? '<span class="status-dot dot-success"></span>' : '<span class="status-dot dot-error"></span>'}${item.succeeded ? '成功' : '失败'}</td>
            `;
            allTbody.appendChild(tr);
          });
        }
      }

      // 3. ES 同步与元数据
      const indexSync = data.index_sync || {};
      const reqMeta = data.request_meta || {};
      setEl("#sync-state", indexSync.state || "-");
      setEl("#sync-deleted", `${indexSync.deleted ?? 0} 条`);
      setEl("#sync-attempts", `${indexSync.attempts ?? 0} 次`);
      setEl("#sync-completed-at", indexSync.completed_at || "-");
      setEl("#sync-error", indexSync.last_error || "无");
      setEl("#meta-user-agent", reqMeta.user_agent || "未知");

      // 4. 原始 JSON
      const rawJson = document.querySelector("#raw-json-display");
      if (rawJson) {
        rawJson.textContent = JSON.stringify(data.raw_details || {}, null, 2);
      }

      // 默认选中第一个 Tab
      if (tabs.length > 0) {
        tabs[0].click();
      }
    };

    // 全局事件委托：监听行内查看审计明细按钮点击（防止事件冒泡或动态渲染失效）
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest(".js-open-audit-detail");
      if (!btn) return;
      e.preventDefault();
      const detailUrl = btn.dataset.detailUrl;
      if (!detailUrl) return;

      openModal();
      const subtitleEl = document.querySelector("#audit-modal-subtitle");
      if (subtitleEl) subtitleEl.textContent = "正在读取审计明细...";

      try {
        const resp = await fetch(detailUrl, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (!resp.ok) throw new Error("获取审计明细失败");
        const data = await resp.json();
        populateModal(data);
      } catch (err) {
        if (subtitleEl) subtitleEl.textContent = `加载失败：${err.message}`;
      }
    });
  }
});
