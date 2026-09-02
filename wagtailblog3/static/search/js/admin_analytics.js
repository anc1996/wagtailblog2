(() => {
    "use strict";

    const root = document.querySelector("[data-search-analytics]");
    if (!root) return;
    const form = root.querySelector("[data-analytics-form]");
    const recordsForm = root.querySelector("[data-records-form]");
    const status = root.querySelector("[data-status]");
    const customDates = root.querySelector("[data-custom-dates]");
    const dateError = root.querySelector("[data-date-error]");
    const rangeButtons = [...root.querySelectorAll("[data-range]")];
    const rangeControls = root.querySelector(".range-controls");
    let draftRange;
    let draftFrom = "";
    let draftTo = "";
    const rangePicker = document.createElement("div");
    rangePicker.className = "range-picker";
    const rangeTrigger = document.createElement("button");
    rangeTrigger.type = "button";
    rangeTrigger.className = "button range-trigger";
    rangeTrigger.setAttribute("aria-expanded", "false");
    rangeTrigger.setAttribute("aria-controls", "analytics-range-menu");
    const rangeMenu = document.createElement("div");
    rangeMenu.id = "analytics-range-menu";
    rangeMenu.className = "range-menu";
    rangeMenu.hidden = true;
    rangeControls.parentNode.insertBefore(rangePicker, rangeControls);
    rangePicker.append(rangeTrigger, rangeMenu);
    rangeMenu.append(rangeControls);
    const cancelRangeButton = document.createElement("button");
    cancelRangeButton.type = "button";
    cancelRangeButton.className = "button button-secondary";
    cancelRangeButton.textContent = "取消";
    const applyRangeButton = document.createElement("button");
    applyRangeButton.type = "submit";
    applyRangeButton.className = "button button-primary";
    applyRangeButton.dataset.rangeApply = "";
    applyRangeButton.textContent = "应用";
    const rangeMenuActions = document.createElement("div");
    rangeMenuActions.className = "range-menu-actions";
    const thisYearButton = document.createElement("button");
    thisYearButton.type = "button";
    thisYearButton.className = "button button-small";
    thisYearButton.dataset.range = "this_year";
    thisYearButton.setAttribute("aria-pressed", "false");
    thisYearButton.textContent = "今年";
    rangeControls.insertBefore(thisYearButton, rangeControls.querySelector('[data-range="custom"]'));
    rangeButtons.push(thisYearButton);
    const granularity = form.elements.granularity;
    const trendChart = root.querySelector("[data-trend-chart]");
    const topQueriesChart = root.querySelector("[data-top-queries-chart]");
    const topQueriesLegend = root.querySelector("[data-top-queries-legend]");
    const donutTotal = root.querySelector("[data-donut-total]");
    const summary = root.querySelector("[data-summary]");
    const recordsTitle = root.querySelector("[data-records-title]");
    const recordsBody = root.querySelector("[data-records-body]");
    const pagination = root.querySelector("[data-pagination]");
    const dashboardSection = root.querySelector("[data-dashboard-region]") || root;
    const recordsSection = root.querySelector("[data-records-region]") || root;
    const dashboardStatus = root.querySelector("[data-dashboard-status]") || status;
    const recordsStatus = root.querySelector("[data-records-status]") || status;
    let dashboardController = null;
    let recordsController = null;
    let dashboardRequestId = 0;
    let recordsRequestId = 0;
    let state = readState();

    function makeElement(name, text = "") {
        const element = document.createElement(name);
        if (text) element.textContent = text;
        return element;
    }

    function defaultGranularity(range) {
        return {last14: "week", last30: "week", this_month: "week", last_month: "week", this_year: "month", year: "month"}[range] || "day";
    }

    function readState() {
        const params = new URLSearchParams(window.location.search);
        const rangeAliases = {day: "today", week: "last7", month: "last30"};
        const validRanges = ["today", "yesterday", "last7", "last14", "last30", "this_month", "last_month", "this_year", "custom"];
        const requestedRange = params.get("range") === "year" ? "this_year" : params.get("range");
        const range = validRanges.includes(requestedRange) ? requestedRange : (rangeAliases[requestedRange] || "last30");
        const topN = Number.parseInt(params.get("top_n"), 10);
        const pageSize = Number.parseInt(params.get("page_size"), 10);
        const recordsRangeValue = params.get("records_range");
        const recordsRange = validRanges.includes(recordsRangeValue) ? recordsRangeValue : range;
        return {
            range,
            from: params.get("from") || "",
            to: params.get("to") || "",
            analysisQuery: params.get("analysis_q") || "",
            recordsQuery: params.get("records_q") || params.get("q") || "",
            recordsRange,
            recordsFrom: params.get("records_from") || (recordsRange === "custom" ? params.get("from") || "" : ""),
            recordsTo: params.get("records_to") || (recordsRange === "custom" ? params.get("to") || "" : ""),
            topN: Number.isInteger(topN) ? Math.min(Math.max(topN, 1), 10) : 10,
            page: Math.max(Number.parseInt(params.get("page"), 10) || 1, 1),
            pageSize: [20, 50, 100].includes(pageSize) ? pageSize : 20,
            granularity: ["day", "week", "month", "year"].includes(params.get("granularity")) ? params.get("granularity") : defaultGranularity(range),
            columns: params.get("columns") ? params.get("columns").split(",") : ["date", "query", "hits"],
        };
    }

    function setStatus(message, isError = false, retry = false, target = status, retryAction = null) {
        target.replaceChildren(makeElement("span", message));
        target.classList.toggle("is-error", isError);
        if (retry) {
            const button = makeElement("button", "重试");
            button.type = "button";
            button.className = "button button-small";
            button.addEventListener("click", retryAction || (() => loadDashboard({history: "none"})));
            target.append(button);
        }
    }

    function applyStateToForms() {
        if (!draftRange) draftRange = state.range;
        form.elements.from.value = state.from;
        form.elements.to.value = state.to;
        granularity.value = state.granularity;
        form.elements.top_n.value = String(state.topN);
        recordsForm.elements.q.value = state.recordsQuery;
        recordsForm.elements.page_size.value = String(state.pageSize);
        recordsForm.querySelectorAll("input[name=column]").forEach((input) => {
            input.checked = state.columns.includes(input.value);
        });
        customDates.hidden = draftRange !== "custom";
        if (customDates.parentElement !== rangeMenu) rangeMenu.append(customDates);
        if (!rangeMenuActions.contains(cancelRangeButton)) rangeMenuActions.append(cancelRangeButton);
        if (!rangeMenuActions.contains(applyRangeButton)) rangeMenuActions.append(applyRangeButton);
        if (!rangeMenu.contains(rangeMenuActions)) rangeMenu.append(rangeMenuActions);
        rangeButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.range === state.range)));
        updateRangeTrigger();
    }

    let payloadAvailableGranularities = null;
    let appliedFrom = "";
    let appliedTo = "";

    function formatDate(value) {
        const parts = String(value || "").split("-");
        return parts.length === 3 ? `${parts[0]}年${parts[1]}月${parts[2]}日` : String(value || "");
    }

    function updateRangeTrigger() {
        const from = appliedFrom || state.from;
        const to = appliedTo || state.to;
        rangeTrigger.textContent = from && to ? `${formatDate(from)} - ${formatDate(to)}` : (rangeButtons.find((button) => button.dataset.range === state.range)?.textContent || "时间范围");
    }

    function closeRangeMenu() {
        rangeMenu.hidden = true;
        rangeTrigger.setAttribute("aria-expanded", "false");
        form.elements.from.value = state.from;
        form.elements.to.value = state.to;
        draftRange = state.range;
        draftFrom = state.from;
        draftTo = state.to;
    }

    function openRangeMenu() {
        draftRange = state.range;
        draftFrom = state.from;
        draftTo = state.to;
        rangeMenu.hidden = false;
        rangeTrigger.setAttribute("aria-expanded", "true");
    }
    cancelRangeButton.addEventListener("click", closeRangeMenu);

    function validateCustomDates() {
        dateError.textContent = "";
        if (draftRange !== "custom" && state.range !== "custom") return true;
        const {from, to} = form.elements;
        if (!from.value || !to.value || from.value > to.value) {
            dateError.textContent = !from.value || !to.value ? "请选择开始和结束日期。" : "结束日期不能早于开始日期。";
            (from.value ? to : from).focus();
            return false;
        }
        return true;
    }

    function writeUrl(mode) {
        if (mode === "none") return;
        const params = new URLSearchParams({range: state.range, granularity: state.granularity, top_n: String(state.topN)});
        if (state.range === "custom") { params.set("from", state.from); params.set("to", state.to); }
        if (state.analysisQuery) params.set("analysis_q", state.analysisQuery);
        if (state.recordsQuery) params.set("records_q", state.recordsQuery);
        if (state.recordsRange !== state.range) params.set("records_range", state.recordsRange);
        if (state.recordsRange === "custom") {
            params.set("records_from", state.recordsFrom);
            params.set("records_to", state.recordsTo);
        }
        if (state.page !== 1) params.set("page", String(state.page));
        if (state.pageSize !== 20) params.set("page_size", String(state.pageSize));
        if (state.columns.join(",") !== "date,query,hits") params.set("columns", state.columns.join(","));
        const url = `${window.location.pathname}?${params.toString()}`;
        if (mode === "push") window.history.pushState(null, "", url); else window.history.replaceState(null, "", url);
    }

    function renderSummary(payload) {
        summary.replaceChildren();
        [["总搜索次数", payload.summary.total_searches], ["活跃查询词", payload.summary.active_queries], ["数据截至", payload.summary.latest_date || "暂无数据"]].forEach(([label, value]) => {
            const item = makeElement("div"); item.append(makeElement("dt", label), makeElement("dd", String(value))); summary.append(item);
        });
    }

    function renderTrend(payload) {
        trendChart.replaceChildren();
        const rows = payload.trend || [];
        const max = Math.max(...rows.map((row) => Number(row.hits || 0)), 0);
        if (!rows.length || max === 0) { trendChart.append(makeElement("p", "当前范围内暂无搜索趋势数据。")); return; }
        rows.forEach((row) => {
            const bar = makeElement("div"); bar.className = "analytics-bar"; bar.setAttribute("role", "presentation");
            const value = makeElement("span", String(row.hits)); value.className = "analytics-bar-value";
            const fill = makeElement("span"); fill.className = "analytics-bar-fill"; fill.style.height = `${Math.max(Number(row.hits) / max * 100, row.hits ? 4 : 1)}%`; fill.title = `${row.date}: ${row.hits}`;
            const label = makeElement("span", String(row.date).slice(5)); label.className = "analytics-bar-label";
            bar.append(value, fill, label); trendChart.append(bar);
        });
    }

    function renderTopQueries(payload) {
        topQueriesLegend.replaceChildren(); topQueriesChart.style.removeProperty("--analytics-donut"); donutTotal.textContent = String(payload.summary.total_searches || 0);
        const rows = payload.top_queries || []; if (!rows.length) { topQueriesLegend.append(makeElement("p", "当前范围内暂无热门搜索词。")); return; }
        const colors = ["#007d7e", "#276ef1", "#d85b3f", "#8b5cf6", "#b7791f", "#0f766e", "#c2410c", "#4f46e5", "#15803d", "#be123c", "#64748b"]; let offset = 0; const stops = [];
        rows.forEach((row, index) => {
            const share = Math.max(Number(row.share || 0), 0); const color = colors[index % colors.length]; stops.push(`${color} ${offset}% ${offset + share}%`); offset += share;
            const item = makeElement("div"); item.className = "analytics-query-legend-item"; item.setAttribute("role", "listitem");
            const swatch = makeElement("span"); swatch.className = "analytics-query-swatch"; swatch.style.backgroundColor = color;
            const content = makeElement("span"); content.className = "analytics-query-legend-content";
            const label = makeElement(row.is_other ? "span" : "button", row.query);
            if (row.is_other) label.className = "analytics-query-other"; else { label.type = "button"; label.className = "analytics-query-link"; label.addEventListener("click", () => { state.recordsQuery = row.query; state.page = 1; applyStateToForms(); loadRecords({history: "push", focus: true}); }); }
            const meta = makeElement("span", `${row.hits} 次 · ${row.share}%`); meta.className = "analytics-query-meta"; content.append(label, meta); item.append(swatch, content); topQueriesLegend.append(item);
        });
        topQueriesChart.style.setProperty("--analytics-donut", `conic-gradient(${stops.join(", ")})`);
    }

    function renderRecords(payload) {
        recordsBody.replaceChildren(); const columns = state.columns;
        const headCells = root.querySelectorAll("[data-records-table] thead [data-column]"); headCells.forEach((cell) => { cell.hidden = !columns.includes(cell.dataset.column); });
        (payload.records || []).forEach((record) => { const row = makeElement("tr"); [["date", record.date], ["query", record.query], ["hits", record.hits]].forEach(([column, value]) => { const cell = makeElement("td"); cell.dataset.column = column; cell.hidden = !columns.includes(column); if (column === "query") { const link = makeElement("a", value); link.href = record.url; link.target = "_blank"; link.rel = "noopener"; cell.append(link); } else cell.textContent = String(value); row.append(cell); }); recordsBody.append(row); });
        if (!(payload.records || []).length) { const row = makeElement("tr"); const cell = makeElement("td", "当前筛选条件下暂无聚合记录。"); cell.colSpan = 3; row.append(cell); recordsBody.append(row); }
    }

    function renderPagination(payload) {
        pagination.replaceChildren(); const info = payload.pagination || {}; const page = info.page || 1; const totalPages = Math.max(info.total_pages || 1, 1);
        const previous = makeElement("button", "上一页"); previous.type = "button"; previous.className = "button button-small"; previous.disabled = !info.has_previous; previous.addEventListener("click", () => { state.page = page - 1; loadRecords({history: "push", focus: true}); });
        const next = makeElement("button", "下一页"); next.type = "button"; next.className = "button button-small"; next.disabled = !info.has_next; next.addEventListener("click", () => { state.page = page + 1; loadRecords({history: "push", focus: true}); });
        pagination.append(previous, makeElement("span", `第 ${page} / ${totalPages} 页，共 ${info.total_count || 0} 条`), next);
    }

    function applyPayload(payload) {
        payloadAvailableGranularities = payload.range.available_granularities || ["day", "week", "month"];
        if (!granularity.querySelector('option[value="year"]')) {
            const yearOption = document.createElement("option");
            yearOption.value = "year";
            yearOption.textContent = "年";
            granularity.append(yearOption);
        }
        [...granularity.options].forEach((option) => { option.hidden = !payloadAvailableGranularities.includes(option.value); });
        if (!payloadAvailableGranularities.includes(state.granularity)) { state.granularity = payload.range.granularity; granularity.value = state.granularity; }
        renderSummary(payload); renderTrend(payload); renderTopQueries(payload);
        appliedFrom = payload.range.from || state.from;
        appliedTo = payload.range.to || state.to;
        updateRangeTrigger();
        const granularityLabel = {day: "日", week: "周", month: "月", year: "年"}[payload.range.granularity] || payload.range.granularity;
        root.querySelector("[data-analytics-scope]").textContent = `统计范围：${payload.range.from} 至 ${payload.range.to}，按${granularityLabel}聚合。`;
        trendChart.setAttribute("aria-label", `按${granularityLabel}搜索趋势柱状图，详细数值见图例。`);
    }

    function buildRangeParams(params) {
        if (state.range === "custom") { params.set("from", state.from); params.set("to", state.to); }
        return params;
    }

    function endpointUrl(kind) {
        const explicit = root.dataset[`${kind}Url`];
        if (explicit) return explicit;
        const base = root.dataset.url || window.location.pathname;
        return `${base.replace(/\/$/, "")}/${kind}/`;
    }

    async function requestDashboard(signal) {
        const params = buildRangeParams(new URLSearchParams({view: "dashboard", range: state.range, top_n: String(state.topN), granularity: state.granularity, analysis_q: state.analysisQuery}));
        const response = await fetch(`${endpointUrl("dashboard")}?${params.toString()}`, {headers: {Accept: "application/json", "X-Requested-With": "XMLHttpRequest"}, signal}); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.error?.message || "加载失败，请稍后重试。"); return payload;
    }

    async function requestRecords(signal) {
        const params = new URLSearchParams({view: "records", range: state.recordsRange, page: String(state.page), page_size: String(state.pageSize), records_q: state.recordsQuery});
        if (state.recordsRange === "custom") { params.set("from", state.recordsFrom); params.set("to", state.recordsTo); }
        const response = await fetch(`${endpointUrl("records")}?${params.toString()}`, {headers: {Accept: "application/json", "X-Requested-With": "XMLHttpRequest"}, signal}); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.error?.message || "加载记录失败，请稍后重试。"); return payload;
    }

    async function loadDashboard({history = "replace"} = {}) {
        if (!validateCustomDates()) return;
        if (dashboardController) dashboardController.abort();
        dashboardController = new AbortController();
        const current = ++dashboardRequestId;
        dashboardSection.setAttribute("aria-busy", "true");
        setStatus("正在更新搜索分析…", false, false, dashboardStatus);
        try {
            const payload = await requestDashboard(dashboardController.signal);
            if (current !== dashboardRequestId) return;
            applyPayload(payload); writeUrl(history); setStatus("搜索分析已更新。", false, false, dashboardStatus);
        } catch (error) {
            if (error.name !== "AbortError" && current === dashboardRequestId) setStatus(error.message, true, true, dashboardStatus, () => loadDashboard({history: "none"}));
        } finally { if (current === dashboardRequestId) dashboardSection.removeAttribute("aria-busy"); }
    }

    async function loadRecords({history = "push", focus = false} = {}) {
        if (recordsController) recordsController.abort();
        recordsController = new AbortController();
        const current = ++recordsRequestId;
        recordsSection.setAttribute("aria-busy", "true");
        setStatus("正在更新聚合搜索记录…", false, false, recordsStatus);
        try {
            const payload = await requestRecords(recordsController.signal);
            if (current !== recordsRequestId) return;
            renderRecords(payload); renderPagination(payload); writeUrl(history);
            setStatus("聚合搜索记录已更新。", false, false, recordsStatus);
            if (focus) recordsTitle.focus();
        } catch (error) {
            if (error.name !== "AbortError" && current === recordsRequestId) setStatus(error.message, true, true, recordsStatus, () => loadRecords({history: "none", focus: true}));
        } finally { if (current === recordsRequestId) recordsSection.removeAttribute("aria-busy"); }
    }

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (event.submitter?.dataset.rangeApply !== undefined) {
            if (!validateCustomDates()) return;
            state.range = draftRange;
            state.from = draftRange === "custom" ? form.elements.from.value : "";
            state.to = draftRange === "custom" ? form.elements.to.value : "";
            state.granularity = defaultGranularity(state.range);
            state.page = 1;
            closeRangeMenu();
            loadDashboard({history: "push"});
            return;
        }
        state.topN = Math.min(Math.max(Number.parseInt(form.elements.top_n.value, 10) || 10, 1), 10);
        state.page = 1;
        loadDashboard({history: "push"});
    });
    rangeTrigger.addEventListener("click", (event) => {
        event.stopPropagation();
        rangeMenu.hidden ? openRangeMenu() : closeRangeMenu();
    });
    rangeMenu.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", (event) => {
        if (!rangeMenu.hidden && !rangePicker.contains(event.target)) closeRangeMenu();
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !rangeMenu.hidden) { closeRangeMenu(); rangeTrigger.focus(); } });
    rangeButtons.forEach((button) => button.addEventListener("click", () => {
        draftRange = button.dataset.range;
        draftFrom = state.from;
        draftTo = state.to;
        customDates.hidden = draftRange !== "custom";
        dateError.textContent = "";
        if (draftRange === "custom") { form.elements.from.value = draftFrom; form.elements.to.value = draftTo; form.elements.from.focus(); }
    }));
    granularity.addEventListener("change", () => { state.granularity = granularity.value; state.page = 1; loadDashboard({history: "push"}); });
    root.querySelector("[data-analysis-reset]").addEventListener("click", () => { state.range = "last30"; state.from = ""; state.to = ""; state.analysisQuery = ""; state.granularity = "week"; state.topN = 10; state.page = 1; applyStateToForms(); loadDashboard({history: "push"}); });
    recordsForm.addEventListener("submit", (event) => { event.preventDefault(); state.recordsQuery = recordsForm.elements.q.value.trim(); state.pageSize = Number.parseInt(recordsForm.elements.page_size.value, 10) || 20; state.columns = [...recordsForm.querySelectorAll("input[name=column]:checked")].map((input) => input.value); state.page = 1; loadRecords(); });
    recordsForm.querySelector("[data-records-reset]").addEventListener("click", () => { state.recordsQuery = ""; state.pageSize = 20; state.columns = ["date", "query", "hits"]; state.page = 1; applyStateToForms(); loadRecords(); });
    window.addEventListener("popstate", () => { state = readState(); applyStateToForms(); refreshAll("none"); });
    async function refreshAll(history) { await loadDashboard({history}); await loadRecords({history: "none"}); }
    applyStateToForms(); refreshAll("replace");
})();
