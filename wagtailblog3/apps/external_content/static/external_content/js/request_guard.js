/* 仅桥接受控表单与其自动保存；不修改全站 fetch，不自动接管未知结果。 */
(() => {
  "use strict";
  const workflow = new Set([
    "action-submit", "action-restart-workflow",
    "action-workflow-action", "action-cancel-workflow",
  ]);
  const init = () => {
    const marker = document.querySelector("[data-external-content-guard]");
    if (!marker || marker.dataset.bound) return;
    marker.dataset.bound = "1";
    const config = JSON.parse(marker.textContent);
    const form = document.querySelector("#page-edit-form, #page-create-form") ||
      [...document.forms].find((item) => item.method.toLowerCase() === "post" &&
        item.querySelector("[name=csrfmiddlewaretoken]") &&
        new URL(item.action, location.href).pathname === location.pathname);
    if (!form) return;
    let attempt = null;
    let navigation = false;
    let userEdited = false;
    let autosaveController = null;

    // 保留用户后续输入，不将它混入尚未确认的旧尝试快照。
    form.addEventListener("input", () => { userEdited = true; });
    form.addEventListener("change", () => { userEdited = true; });

    const warn = () => {
      form.dispatchEvent(new CustomEvent("external-content:unknown", { bubbles: true }));
      try {
        window.alert("请求正在处理或结果尚未确认，请先核对原页面与保存结果；为避免重复写入，本次提交已暂停。");
      } catch (_) {}
    };

    const prepare = async (snapshot, json) => {
      if (config.unavailable || !config.ticket) throw new Error("guard unavailable");
      if (!attempt) {
        // DOM action 是绝对地址；服务端绑定同源路径和查询，不携带 scheme/netloc。
        let canonicalTarget;
        try {
          const targetUrl = new URL(form.action, window.location.href);
          if (targetUrl.origin !== window.location.origin) {
            throw new Error("cross-origin target rejected");
          }
          canonicalTarget = targetUrl.pathname + targetUrl.search;
        } catch (err) {
          throw new Error("invalid target url: " + err.message);
        }

        const att = {
          snapshot,
          token: null,
          promise: null,
          pending: true,
          unknown: false,
        };
        attempt = att;
        userEdited = false;

        const body = new FormData();
        for (const [key, value] of snapshot) body.append(key, value);
        body.set("_ec_ticket", config.ticket);
        body.set("_ec_target", canonicalTarget);

        // 同一次准备固定使用原快照与预留身份，连击不创建第二个执行身份。
        att.promise = fetch(config.prepare, {
          method: "POST",
          body,
          credentials: "same-origin",
          headers: { Accept: json ? "application/json" : "text/html" },
        }).then(async (response) => {
          att.pending = false;
          const data = await response.json();
          if (!response.ok || !data.token) {
            att.unknown = true;
            throw new Error("preparation rejected");
          }
          att.token = data.token;
          return att;
        }).catch((err) => {
          att.pending = false;
          att.unknown = true;
          throw err;
        });
      }
      return attempt.promise;
    };

    form.addEventListener("submit", async (event) => {
      if (workflow.has(event.submitter?.name) || navigation) return;
      event.preventDefault();

      if (attempt) {
        // 包括准备中：明确阻断手动提交，不能静默丢弃用户的发布意图。
        warn();
        return;
      }

      const submitter = event.submitter;
      const snapshot = new FormData(form);
      if (submitter?.name) snapshot.append(submitter.name, submitter.value);
      try {
        const ready = await prepare(snapshot, false);
        navigation = true;
        // 原生提交仍使用准备时的快照，保留被点击按钮的动作字段。
        form.addEventListener("formdata", (created) => {
          for (const key of [...created.formData.keys()]) created.formData.delete(key);
          for (const [key, value] of ready.snapshot) created.formData.append(key, value);
          created.formData.set("_ec_token", ready.token);
        }, { once: true });
        form.requestSubmit(submitter || undefined);
      } catch (_) {
        // 签发失败时不进入原生写入，也不自动换一个身份重试。
        warn();
      }
    }, true);

    const connectAutosave = () => {
      const controller = window.wagtail?.app?.getControllerForElementAndIdentifier(form, "w-autosave");
      if (!controller || controller._externalContentGuard) return !!controller;
      controller._externalContentGuard = true;
      autosaveController = controller;
      const native = controller.submit.restore ? controller.submit.restore() : controller.submit;

      const guarded = async (event) => {
        if (event.defaultPrevented) return;
        // 未知结果或旧尝试期间输入变化时暂停，不把新正文当作旧尝试重试。
        if (attempt && (attempt.unknown || userEdited)) {
          controller.activeValue = false;
          controller.clearRetries?.();
          warn();
          return;
        }

        try {
          const ready = await prepare(event.detail.formData, true);
          // 原生重试也只能携带已签发的固定快照。
          const snapshot = new FormData();
          for (const [key, value] of ready.snapshot) snapshot.append(key, value);
          snapshot.set("_ec_token", ready.token);
          event.detail.formData = snapshot;
          // 保留原生上下文及 field_updates/revision 等成功响应语义。
          await native.call(controller, event);
        } catch (_) {
          controller.activeValue = false;
          controller.clearRetries?.();
          warn();
        }
      };
      guarded.restore = () => guarded;
      controller.submit = guarded;
      // 重新安装原生 debounce；restore 始终返回受控包装，不能恢复成裸写入口。
      controller.intervalValueChanged(controller.intervalValue);

      // 原生 submit 吞掉异常并发 error 事件，必须在事件边界取消自动重试。
      form.addEventListener("w-autosave:error", () => {
        controller.activeValue = false;
        controller.clearRetries?.();
        if (attempt) {
          attempt.pending = false;
          attempt.unknown = true;
        }
        warn();
      });

      form.addEventListener("w-autosave:success", (event) => {
        const data = event.detail.data;
        // 业务可能已经提交；未取得可信新身份时保留旧尝试并提示核对。
        if (!data?._ec_ticket || data._ec_guard_unavailable) {
          if (attempt) {
            attempt.pending = false;
            attempt.unknown = true;
          }
          controller.activeValue = false;
          controller.clearRetries?.();
          config.unavailable = true;
          warn();
        } else {
          config.ticket = data._ec_ticket;
          attempt = null;
          userEdited = false;
        }
      });
      return true;
    };
    // 原生 save 在 dispatch(save) 之前已取得旧 submit 函数引用。
    // 迟挂载时必须取消这一次裸调用，待其排队结束后经包装重新保存。
    form.addEventListener("w-autosave:save", (event) => {
      if (autosaveController) return;
      event.preventDefault();
      if (!connectAutosave()) {
        warn();
        return;
      }
      queueMicrotask(() => {
        Promise.resolve(autosaveController.submitPromise).then(() =>
          autosaveController.save(event.detail.trigger));
      });
    });
    if (!connectAutosave()) {
      requestAnimationFrame(connectAutosave);
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
