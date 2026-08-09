(() => {
  "use strict";

  function initializeContentAnalytics() {
  const canvas = document.getElementById("content-analytics-trend");
  const dataNode = document.getElementById("content-analytics-trend-data");
  if (canvas && dataNode && typeof Chart !== "undefined") {
    const data = JSON.parse(dataNode.textContent);
    new Chart(canvas, {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          { label: "浏览量", data: data.views, borderColor: "#007d7e", backgroundColor: "rgba(0,125,126,.12)", tension: 0.25, fill: true },
          { label: "独立访客", data: data.visitors, borderColor: "#d97706", backgroundColor: "rgba(217,119,6,.08)", tension: 0.25 },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false }, scales: { y: { beginAtZero: true } } },
    });
  }

  const articles = document.querySelector("[data-analytics-articles]");
  if (!articles) return;
  const content = articles.querySelector("[data-analytics-content]");
  const status = articles.querySelector("[data-analytics-status]");
  let controller;
  let lastUrl;

  function initialUrl() {
    const url = new URL(articles.dataset.url, window.location.origin);
    const filters = new URLSearchParams(window.location.search);
    filters.delete("export");
    filters.delete("page");
    url.search = filters.toString();
    return url;
  }

  async function loadArticles(url) {
    if (controller) controller.abort();
    controller = new AbortController();
    lastUrl = new URL(url, window.location.origin);
    articles.setAttribute("aria-busy", "true");
    articles.classList.add("is-loading");
    status.textContent = "正在加载文章数据…";

    try {
      const response = await fetch(lastUrl, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      content.innerHTML = await response.text();
      status.textContent = "文章数据已更新。";
    } catch (error) {
      if (error.name === "AbortError") return;
      status.innerHTML = '文章数据加载失败。<button type="button" class="button button-small" data-analytics-retry>重试</button>';
    } finally {
      articles.setAttribute("aria-busy", "false");
      articles.classList.remove("is-loading");
    }
  }

  articles.addEventListener("click", (event) => {
    const pageLink = event.target.closest("[data-analytics-page]");
    if (pageLink) {
      event.preventDefault();
      loadArticles(new URL(pageLink.href, lastUrl));
      return;
    }
    if (event.target.closest("[data-analytics-retry]")) loadArticles(lastUrl);
  });

  loadArticles(initialUrl());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeContentAnalytics, { once: true });
  } else {
    initializeContentAnalytics();
  }
})();
