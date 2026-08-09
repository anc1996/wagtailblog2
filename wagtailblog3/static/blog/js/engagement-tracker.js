(() => {
  "use strict";

  const root = document.querySelector("[data-article-reading]");
  const content = document.querySelector("[data-reading-content]");
  if (!root || !content || !root.dataset.analyticsUrl || !root.dataset.pageId) return;

  function createSessionId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();

    // 局域网 HTTP 不暴露 randomUUID；会话 ID 只用于幂等，不作为身份或安全凭据。
    const bytes = new Uint8Array(16);
    if (globalThis.crypto?.getRandomValues) {
      globalThis.crypto.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    return [
      hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
      hex.slice(16, 20), hex.slice(20),
    ].join("-");
  }

  const csrfToken = root.dataset.csrfToken;
  const sessionId = createSessionId();
  let sequence = 0;
  let maxScroll = 0;
  let activeSeconds = 0;
  let engaged = false;
  let lastTick = Date.now();
  let lastSent = Date.now();
  let timer;

  function visibleAndFocused() {
    return document.visibilityState === "visible" && document.hasFocus();
  }

  function updateProgress() {
    const rect = content.getBoundingClientRect();
    const height = Math.max(content.offsetHeight, 1);
    const seen = Math.min(height, Math.max(0, window.innerHeight - rect.top));
    maxScroll = Math.max(maxScroll, Math.min(100, Math.round((seen / height) * 100)));
  }

  function payload() {
    return {
      page_id: Number(root.dataset.pageId), session_id: sessionId, sequence: ++sequence,
      engaged, max_scroll_percent: maxScroll, active_reading_seconds: Math.min(1800, activeSeconds),
    };
  }

  function send(beacon) {
    if (!csrfToken || !activeSeconds) return;
    const data = payload();
    lastSent = Date.now();
    if (beacon && navigator.sendBeacon) {
      const form = new FormData();
      form.append("csrfmiddlewaretoken", csrfToken);
      Object.entries(data).forEach(([key, value]) => form.append(key, String(value)));
      navigator.sendBeacon(root.dataset.analyticsUrl, form);
      return;
    }
    fetch(root.dataset.analyticsUrl, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify(data), keepalive: true,
    }).catch(() => {});
  }

  function tick() {
    const now = Date.now();
    if (visibleAndFocused()) activeSeconds += Math.floor((now - lastTick) / 1000);
    lastTick = now;
    activeSeconds = Math.min(activeSeconds, 1800);
    updateProgress();
    engaged = engaged || activeSeconds >= 10;
    if (activeSeconds && now - lastSent >= 30000) send(false);
  }

  window.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("pagehide", () => send(true));
  document.addEventListener("visibilitychange", () => { lastTick = Date.now(); });
  updateProgress();
  timer = window.setInterval(tick, 1000);
  window.addEventListener("beforeunload", () => window.clearInterval(timer));
})();
