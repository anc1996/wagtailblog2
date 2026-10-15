/* 离线浏览器回归：运行 node tools/tests/test_request_guard_browser.cjs。
 * 使用已安装 Wagtail 的原生 ProgressController；不登录后台或写入业务数据。
 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const guardJs = fs.readFileSync(path.resolve(__dirname,
  "../../wagtailblog3/apps/external_content/static/external_content/js/request_guard.js"), "utf8");

function installedProgressController() {
  const readBundle = [
    "from pathlib import Path",
    "import wagtail",
    "print((Path(wagtail.__file__).parent / 'admin/static/wagtailadmin/js/core.js').read_text())",
  ].join("; ");
  const bundle = process.env.WAGTAIL_PROGRESS_BUNDLE
    ? fs.readFileSync(process.env.WAGTAIL_PROGRESS_BUNDLE, "utf8")
    : execFileSync(process.platform === "win32" ? "wsl" : "python",
      process.platform === "win32"
        ? ["-d", "Debian", "--", "/root/anaconda3/envs/wagtailblog-test/bin/python", "-c", readBundle]
        : ["-c", readBundle], { encoding: "utf8", maxBuffer: 8 * 1024 * 1024 });
  const start = /class \w+ extends \w+\.Controller\{static classes=\["active"\];static targets=\["label"\]/.exec(bundle);
  assert.ok(start, "已安装 Wagtail 的进度控制器结构变化，请更新测试适配器");
  const end = bundle.indexOf("disconnect(){this.deactivate()}}", start.index);
  assert.ok(end > start.index, "找不到原生进度控制器的结束边界");
  return bundle.slice(start.index, end + "disconnect(){this.deactivate()}}".length)
    .replace(/^class \w+ extends \w+\.Controller/, "window.NativeProgress = class extends window.ControllerAdapter");
}

const nativeProgress = installedProgressController();
const origin = "http://guard.test";

async function fixture(browser, width, config, mode = "success") {
  const context = await browser.newContext({ viewport: { width, height: 800 } });
  const page = await context.newPage();
  const state = { prepares: 0, alerts: 0, pendingRoute: null, errors: [] };
  page.on("dialog", async (dialog) => { state.alerts++; await dialog.dismiss(); });
  page.on("pageerror", (error) => state.errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === origin && url.pathname === "/admin/pages/1/edit/") {
      await route.fulfill({ contentType: "text/html", body: `<!doctype html>
        <html><head><meta charset="utf-8"></head><body>
        <script type="application/json" data-external-content-guard>${JSON.stringify(config)}</script>
        <form id="page-edit-form" action="/admin/pages/1/edit/" method="post">
          <input name="csrfmiddlewaretoken" value="fixture-only">
          <input name="title" value="Original input">
          <button type="submit" id="save" name="action-save-draft" value="1"
            data-controller="w-progress"><em>保存草稿</em></button>
        </form></body></html>` });
    } else if (url.origin === origin && url.pathname === "/prepare") {
      state.prepares++;
      if (mode === "pending") {
        state.pendingRoute = route;
      } else if (mode === "network-error") {
        await route.abort("failed");
      } else {
        await route.fulfill({ status: mode === "rejected" ? 409 : 200,
          json: mode === "rejected" ? { error: "rejected" } : { token: "fixture-token" } });
      }
    } else {
      state.errors.push("意外的联网目标");
      await route.abort();
    }
  });
  await page.goto(`${origin}/admin/pages/1/edit/`);
  await page.evaluate((source) => {
    // 仅适配 Stimulus 的属性存取；activate/deactivate/定时器运行安装包原代码。
    window.ControllerAdapter = class {
      constructor(element) {
        this.element = element;
        this.identifier = "w-progress";
        this.hasActiveClass = false;
        this.hasLabelTarget = true;
        this.labelTarget = element.querySelector("em");
        this.labelValue = "";
        this.activeValue = "正在保存";
        this.durationValue = 30000;
      }
      set loadingValue(value) {
        this._loadingValue = value;
        this.loadingValueChanged(value);
      }
      get loadingValue() { return this._loadingValue; }
    };
    (0, eval)(source);
    const button = document.querySelector("#save");
    const controller = new window.NativeProgress(button);
    controller.connect();
    button.addEventListener("click", () => controller.activate());
    window.wagtail = { app: { getControllerForElementAndIdentifier: (element, id) =>
      element === button && id === "w-progress" ? controller : null } };
    window.nativeSubmits = [];
    window.guardWarnings = 0;
    document.querySelector("form").addEventListener("external-content:unknown", () => {
      window.guardWarnings++;
    });
  }, nativeProgress);
  if (mode === "pending-json") {
    await page.evaluate(() => {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const response = await nativeFetch(...args);
        const nativeJson = response.json.bind(response);
        response.json = async () => {
          window.prepareJsonWaiting = true;
          await new Promise((resolve) => { window.releasePrepareJson = resolve; });
          return nativeJson();
        };
        return response;
      };
    });
  }
  await page.addScriptTag({ content: guardJs });
  await page.evaluate(() => {
    document.querySelector("form").addEventListener("submit", (event) => {
      if (!event.defaultPrevented) {
        const fields = new FormData(event.target);
        window.nativeSubmits.push(Object.fromEntries(fields));
      }
      // 截获最后的浏览器导航，检查防重逻辑准备好的原生提交快照。
      event.preventDefault();
    });
  });
  return { page, state, context };
}

async function settle(page) {
  // 覆盖原生 activate 及防重恢复分别排队的宏任务。
  await page.evaluate(() => new Promise((resolve) =>
    setTimeout(() => setTimeout(() => setTimeout(resolve, 0), 0), 0)));
}

async function assertButton(page, loading) {
  await settle(page);
  const state = await page.locator("#save").evaluate((button) => ({
    disabled: button.disabled,
    loading: button.classList.contains("button-longrunning-active"),
    label: button.querySelector("em").textContent,
  }));
  assert.deepEqual(state, { disabled: loading, loading, label: loading ? "正在保存" : "保存草稿" });
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  let passed = 0;
  try {
    for (const width of [1280, 390]) {
      for (const mode of ["unavailable", "rejected", "network-error", "pending", "pending-json", "success"]) {
        const { page, state, context } = await fixture(browser, width,
          mode === "unavailable" ? { unavailable: true } : { ticket: "fixture-ticket", prepare: "/prepare" },
          mode);
        try {
          await page.locator("#save").click();
          if (mode === "unavailable") {
            await assertButton(page, false);
            await page.locator("#save").click();
            await assertButton(page, false);
            assert.equal(state.alerts, 2);
            assert.equal(state.prepares, 0);
          } else if (["rejected", "network-error"].includes(mode)) {
            await page.waitForFunction(() => window.guardWarnings === 1);
            await assertButton(page, false);
            await page.locator("#save").click();
            await assertButton(page, false);
            assert.equal(state.alerts, 2);
            assert.equal(state.prepares, 1, "未知结果重交不能生成第二次准备请求");
          } else {
            if (["pending", "pending-json"].includes(mode)) {
              if (mode === "pending-json") {
                await page.waitForFunction(() => window.prepareJsonWaiting);
              }
              await assertButton(page, true);
              await page.locator("[name=title]").fill("Later input");
              await page.evaluate(() => document.querySelector("form").requestSubmit(document.querySelector("#save")));
              await assertButton(page, true);
              assert.equal(state.alerts, 1);
              assert.equal(state.prepares, 1);
              assert.deepEqual(await page.evaluate(() => window.nativeSubmits), []);
              if (mode === "pending") {
                await state.pendingRoute.fulfill({ json: { token: "fixture-token" } });
              } else {
                await page.evaluate(() => window.releasePrepareJson());
              }
            }
            await page.waitForFunction(() => window.nativeSubmits.length === 1);
            await assertButton(page, true);
            const submissions = await page.evaluate(() => window.nativeSubmits);
            assert.equal(state.prepares, 1);
            assert.equal(submissions[0].title, "Original input");
            assert.equal(submissions[0]["action-save-draft"], "1");
            assert.equal(submissions[0]._ec_token, "fixture-token");
          }
          assert.deepEqual(state.errors, []);
          if (!["pending", "pending-json", "success"].includes(mode)) {
            assert.deepEqual(await page.evaluate(() => window.nativeSubmits), []);
          }
          passed++;
          console.log(`PASS ${width}px ${mode}`);
        } finally {
          await context.close();
        }
      }
    }
    console.log(`通过 ${passed} 项浏览器回归；未访问业务服务或数据库。`);
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
