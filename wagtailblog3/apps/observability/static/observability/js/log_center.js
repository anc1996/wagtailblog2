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
});
