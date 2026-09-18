/* AGILE dashboard controller.
 *
 * One state object drives every view. Filters mutate it, then the active view
 * re-renders. A server-sent event (or a version bump from the poll fallback)
 * refreshes the active view automatically, which is what makes the dashboard
 * update the moment a state's data is ingested. */
(function () {
  "use strict";

  const api = window.AgileApi;
  const charts = window.AgileCharts;

  const state = {
    user: null,
    period: null,
    cohort: "",
    stateCode: "",
    category: "",
    indicator: "",
    view: "overview",
    periods: [],
    states: [],
    cohorts: [],
    categories: [],
    indicators: [],
    dataVersion: 0,
    loading: false,
    queryFilter: "open",
    queryState: "",
    selectedQuery: null,
  };

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------ utilities --
  function toast(message, kind) {
    const box = $("toast");
    box.textContent = message;
    box.className = "toast " + (kind || "");
    box.hidden = false;
    clearTimeout(box.dataset.timer);
    box.dataset.timer = setTimeout(() => { box.hidden = true; }, kind === "error" ? 7000 : 3800);
  }

  function slug(text) {
    return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  /** Escape text before it goes into innerHTML. */
  function esc(text) {
    return String(text === null || text === undefined ? "" : text).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
  }

  function badge(text) {
    if (!text) return "—";
    return '<span class="badge ' + slug(text) + '">' + text + "</span>";
  }

  const num = (value, decimals) => charts.format(value, decimals);
  const pct = (value) => (value === null || value === undefined ? "—" : num(value, 1) + "%");

  function table(columns, rows, options) {
    if (!rows.length) return '<p class="muted">Nothing to show for the current selection.</p>';
    const opts = options || {};
    const head = columns.map((c) => '<th class="' + (c.num ? "num" : "") + '">' + c.label + "</th>").join("");
    const body = rows
      .map(function (row) {
        const cells = columns
          .map(function (column) {
            const value = column.render ? column.render(row) : row[column.key];
            return '<td class="' + (column.num ? "num" : "") + '">' +
              (value === null || value === undefined ? "—" : value) + "</td>";
          })
          .join("");
        return "<tr>" + cells + "</tr>";
      })
      .join("");
    return "<table>" + (opts.caption ? "<caption>" + opts.caption + "</caption>" : "") +
      "<thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>";
  }

  /** Render into a result panel, dropping the "nothing here yet" styling. */
  function setPanel(id, html) {
    const panel = $(id);
    panel.classList.remove("muted");
    panel.innerHTML = html;
  }

  function fail(error, where) {
    if (error && error.status === 401) {
      api.clearSession();
      window.location.href = "/login";
      return;
    }
    console.error(where, error);
    toast((where ? where + ": " : "") + (error && error.message ? error.message : "Request failed"), "error");
  }

  // ------------------------------------------------------------ bootstrap --
  async function boot() {
    try {
      state.user = await api.get("/auth/me");
    } catch (error) {
      window.location.href = "/login";
      return;
    }

    $("user-chip").textContent = state.user.full_name + " · " + state.user.role;
    if (state.user.state_code) {
      state.stateCode = state.user.state_code;
    }

    try {
      const [periods, states, cohorts, categories, indicators] = await Promise.all([
        api.get("/reference/periods"),
        api.get("/reference/states"),
        api.get("/reference/cohorts"),
        api.get("/reference/indicator-categories"),
        api.get("/reference/indicators"),
      ]);
      state.periods = periods;
      state.states = states;
      state.cohorts = cohorts;
      state.categories = categories;
      state.indicators = indicators;
    } catch (error) {
      return fail(error, "Loading reference data");
    }

    populateFilters();
    wireEvents();
    connectLive();
    await render();
  }

  function populateFilters() {
    const periodSelect = $("filter-period");
    const closedFirst = state.periods.slice().reverse();
    periodSelect.innerHTML = closedFirst
      .map((p) => '<option value="' + p.code + '">' + p.label + "</option>")
      .join("");
    const defaultPeriod = pickDefaultPeriod();
    state.period = defaultPeriod;
    periodSelect.value = defaultPeriod;

    $("filter-cohort").innerHTML =
      '<option value="">All cohorts</option>' +
      state.cohorts.map((c) => '<option value="' + c.code + '">' + c.name + " (" + c.state_count + ")</option>").join("");

    const stateOptions = state.states
      .map((s) => '<option value="' + s.code + '">' + s.name + "</option>")
      .join("");
    $("filter-state").innerHTML = '<option value="">All states</option>' + stateOptions;
    if (state.stateCode) {
      $("filter-state").value = state.stateCode;
      if (state.user.role === "STATE_PIU") $("filter-state").disabled = true;
    }

    $("filter-category").innerHTML =
      '<option value="">All categories</option>' +
      state.categories.map((c) => '<option value="' + c.code + '">' + c.name + "</option>").join("");

    refreshIndicatorFilter();

    // Upload + report forms reuse the same reference lists.
    $("upload-state").innerHTML = stateOptions;
    if (state.stateCode) {
      $("upload-state").value = state.stateCode;
      if (state.user.role === "STATE_PIU") $("upload-state").disabled = true;
    }
    $("upload-period").innerHTML = closedFirst
      .map((p) => '<option value="' + p.code + '">' + p.label + "</option>")
      .join("");
    $("upload-period").value = defaultPeriod;

    $("query-filter-state").innerHTML = '<option value="">All states</option>' + stateOptions;
    if (state.stateCode) {
      $("query-filter-state").value = state.stateCode;
      state.queryState = state.stateCode;
      if (state.user.role === "STATE_PIU") $("query-filter-state").disabled = true;
    }

    $("report-period").innerHTML = $("upload-period").innerHTML;
    $("report-period").value = defaultPeriod;
    $("report-category").innerHTML = $("filter-category").innerHTML;
    syncReportScopeRef();
  }

  function pickDefaultPeriod() {
    const today = new Date().toISOString().slice(0, 10);
    const quarterly = state.periods.filter((p) => p.period_type === "QUARTERLY");
    const pool = quarterly.length ? quarterly : state.periods;
    const closed = pool.filter((p) => p.end_date <= today);
    const chosen = (closed.length ? closed : pool).slice(-1)[0];
    return chosen ? chosen.code : (state.periods[0] || {}).code;
  }

  function refreshIndicatorFilter() {
    const pool = state.category
      ? state.indicators.filter((i) => i.category_code === state.category)
      : state.indicators;
    $("filter-indicator").innerHTML = pool
      .map((i) => '<option value="' + i.code + '">' + i.number + ". " + i.name + "</option>")
      .join("");
    if (!pool.some((i) => i.code === state.indicator)) {
      state.indicator = pool.length ? pool[0].code : "";
    }
    $("filter-indicator").value = state.indicator;
  }

  function wireEvents() {
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
        tab.classList.add("active");
        state.view = tab.dataset.view;
        $("view-" + state.view).classList.add("active");
        render();
      });
    });

    $("filter-period").addEventListener("change", function () {
      state.period = this.value;
      render();
    });
    $("filter-cohort").addEventListener("change", function () {
      state.cohort = this.value;
      render();
    });
    $("filter-state").addEventListener("change", function () {
      state.stateCode = this.value;
      render();
    });
    $("filter-category").addEventListener("change", function () {
      state.category = this.value;
      refreshIndicatorFilter();
      render();
    });
    $("filter-indicator").addEventListener("change", function () {
      state.indicator = this.value;
      render();
    });
    $("reset-filters").addEventListener("click", function () {
      state.cohort = "";
      state.category = "";
      if (state.user.role !== "STATE_PIU") state.stateCode = "";
      $("filter-cohort").value = "";
      $("filter-category").value = "";
      if (state.user.role !== "STATE_PIU") $("filter-state").value = "";
      refreshIndicatorFilter();
      render();
    });

    $("logout-btn").addEventListener("click", async function () {
      await api.logout();
      window.location.href = "/login";
    });

    $("upload-form").addEventListener("submit", submitUpload);
    $("upload-period").addEventListener("change", function () {
      if (state.view === "upload") render();
    });
    $("upload-state").addEventListener("change", renderPeriodLock);
    $("cycle-close-btn").addEventListener("click", function () {
      cycleAction("/close", { note: $("cycle-note").value.trim() || null },
        "Cycle closed. Corrections still land through the query workflow.");
    });
    $("cycle-reopen-btn").addEventListener("click", function () {
      const reason = $("cycle-note").value.trim();
      if (!reason) {
        return toast("Say why the cycle is being reopened — every state gets back in.", "error");
      }
      cycleAction("/reopen", { reason: reason }, "Cycle reopened to every state.");
    });
    $("grant-btn").addEventListener("click", function () {
      const reason = $("cycle-note").value.trim();
      if (!reason) return toast("Say why this state may file again.", "error");
      cycleAction(
        "/reopenings",
        { state_code: $("grant-state").value, reason: reason },
        $("grant-state").value + " may file one more return for this cycle."
      );
    });
    $("template-btn").addEventListener("click", function () {
      // The template is built for one state and one period: it carries that
      // state's approved targets, locks the sub-components it does not
      // implement and shows what it already reported. There is no generic one.
      const stateCode = $("upload-state").value;
      const periodCode = $("upload-period").value;
      if (!stateCode || !periodCode) {
        toast("Choose a state and a reporting period first.", "error");
        return;
      }
      window.location.href = api.downloadUrl("/ingestion/template", {
        state: stateCode,
        period: periodCode,
      });
    });
    $("query-filter-status").addEventListener("change", function () {
      state.queryFilter = this.value;
      render();
    });
    $("query-filter-state").addEventListener("change", function () {
      state.queryState = this.value;
      state.selectedQuery = null;
      render();
    });
    $("correction-sheet-btn").addEventListener("click", function () {
      const stateCode = correctionSheetState();
      if (!stateCode) {
        return toast("Choose a state — a sheet carries only that state's flagged figures.", "error");
      }
      window.location.href = api.downloadUrl("/queries/sheets/" + stateCode, {
        period: state.period,
      });
    });
    $("correction-sheet-file").addEventListener("change", function () {
      if (this.files[0]) {
        uploadCorrectionSheet(this.files[0]);
        this.value = "";
      }
    });
    $("report-form").addEventListener("submit", submitReport);
    $("report-scope").addEventListener("change", syncReportScopeRef);
  }

  // ----------------------------------------------------------- live update --
  function connectLive() {
    const dot = $("live-dot");
    const label = $("live-label");

    const source = api.stream({
      onOpen: function () {
        dot.className = "live-dot online";
        label.textContent = "live";
      },
      onChange: function (event) {
        if (event && event.payload && event.payload.state) {
          pushActivity(event);
        }
        render(true);
      },
      onError: function () {
        dot.className = "live-dot offline";
        label.textContent = "polling";
      },
    });

    if (!source) {
      dot.className = "live-dot offline";
      label.textContent = "polling";
    }

    // Fallback (and belt-and-braces alongside SSE): poll the cheap version endpoint.
    setInterval(async function () {
      try {
        const info = await api.get("/dashboard/version");
        if (info.data_version !== state.dataVersion) {
          state.dataVersion = info.data_version;
          (info.recent || []).slice(0, 3).forEach(pushActivity);
          render(true);
        }
      } catch (error) {
        /* transient: the next tick retries */
      }
    }, 30000);
  }

  const seenEvents = new Set();
  function pushActivity(event) {
    if (!event || seenEvents.has(event.version)) return;
    seenEvents.add(event.version);
    const feed = $("activity-feed");
    if (feed.querySelector(".muted")) feed.innerHTML = "";
    const payload = event.payload || {};
    const item = document.createElement("li");
    const when = new Date(event.at || Date.now());
    item.innerHTML =
      "<time>" + when.toTimeString().slice(0, 5) + "</time><span>" +
      "<strong>" + (event.type || "update").replace("submission.", "") + "</strong> " +
      (payload.state ? payload.state + " · " : "") +
      (payload.period ? payload.period + " · " : "") +
      (payload.dqa_score !== undefined && payload.dqa_score !== null ? "DQA " + payload.dqa_score : "") +
      "</span>";
    feed.prepend(item);
    while (feed.children.length > 25) feed.removeChild(feed.lastChild);
  }

  // ---------------------------------------------------------------- render --
  async function render(quiet) {
    if (state.loading) return;
    state.loading = true;
    if (!quiet) $("filter-status").textContent = "Loading…";
    try {
      if (state.view === "overview") await renderOverview();
      else if (state.view === "kpis") await renderKpis();
      else if (state.view === "cohorts") await renderCohorts();
      else if (state.view === "quality") await renderQuality();
      else if (state.view === "queries") await renderQueries();
      else if (state.view === "states") await renderStates();
      else if (state.view === "upload") await renderUpload();
      else if (state.view === "reports") await renderReports();
      $("filter-status").textContent =
        "Updated " + new Date().toLocaleTimeString() + " · period " + state.period;
    } catch (error) {
      fail(error, "Loading view");
      $("filter-status").textContent = "Update failed";
    } finally {
      state.loading = false;
    }
  }

  function scopeParams() {
    return { period: state.period, cohort: state.cohort || undefined };
  }

  function selectedPeriodType() {
    const period = state.periods.find((p) => p.code === state.period);
    return period ? period.period_type : undefined;
  }

  // -- overview ------------------------------------------------------------
  async function renderOverview() {
    const overview = await api.get("/dashboard/overview", scopeParams());
    state.dataVersion = overview.data_version;

    $("kpi-tiles").innerHTML = overview.tiles
      .map(function (tile) {
        const unit = tile.unit === "%" || tile.unit === "/100" ? tile.unit : "";
        return (
          '<div class="tile ' + slug(tile.status) + '">' +
          '<div class="label">' + tile.label + "</div>" +
          '<div class="value">' + num(tile.value) + '<span class="unit">' + unit + "</span></div>" +
          '<div class="caption">' + (tile.caption || "") + "</div></div>"
        );
      })
      .join("");

    const dimensions = (overview.dqa_summary.dimensions || []).map(function (d) {
      return {
        label: d.dimension.charAt(0) + d.dimension.slice(1).toLowerCase(),
        value: d.score,
        status: d.score >= 90 ? "On track" : d.score >= 70 ? "Progressing" : d.score >= 50 ? "Lagging" : "Off track",
        note: "Grade: " + (d.grade || "—") + " · weight " + d.weight,
      };
    });
    charts.barChart("chart-dqa-dimensions", {
      data: dimensions,
      max: 100,
      suffix: "",
      valueLabel: "Score",
      labelWidth: 120,
      reference: 90,
      referenceLabel: "90 target",
      emptyMessage: "No submissions have been scored for this period yet.",
    });

    charts.barChart("chart-cohort-achievement", {
      data: (overview.cohorts || []).map(function (row, index) {
        return {
          label: row.cohort_name,
          value: row.average_achievement_pct,
          color: charts.SERIES[index % charts.SERIES.length],
          note: row.states_reporting + "/" + row.states_expected + " states reporting",
        };
      }),
      suffix: "%",
      valueLabel: "Average achievement",
      labelWidth: 170,
      reference: 100,
      referenceLabel: "target",
      // Two different things leave this chart empty, and saying the wrong one
      // sends someone looking for data that already arrived.
      emptyMessage: (overview.cohorts || []).some((row) => row.states_reporting)
        ? "Achievement needs approved state targets, and none are set for this period."
        : "No state has submitted for this period yet.",
    });

    const status = overview.reporting_status;
    $("reporting-caption").textContent = "Deadline " + status.due_date;
    $("reporting-status").innerHTML =
      '<div class="kv">' +
      '<div><dt>States expected</dt><dd>' + status.states_expected + "</dd></div>" +
      '<div><dt>Submitted</dt><dd>' + status.states_submitted + "</dd></div>" +
      '<div><dt>Cleared for analysis</dt><dd>' + status.states_approved + "</dd></div>" +
      '<div><dt>On-time rate</dt><dd>' + pct(status.on_time_rate_pct) + "</dd></div>" +
      "</div>" +
      table(
        [
          { label: "Cohort", key: "cohort_name" },
          { label: "States", key: "states_expected", num: true },
          { label: "Reporting", num: true, render: (r) => pct(r.reporting_rate_pct) },
          { label: "On time", num: true, render: (r) => pct(r.on_time_rate_pct) },
          { label: "Completeness", num: true, render: (r) => pct(r.completeness_pct) },
          { label: "Avg DQA", num: true, render: (r) => num(r.average_dqa_score) },
          { label: "Grade", render: (r) => badge(r.dqa_grade) },
        ],
        overview.cohorts || []
      );
  }

  // -- KPI performance -----------------------------------------------------
  async function renderKpis() {
    if (!state.indicator) {
      charts.empty("chart-trend", "Select a KPI in the filter bar.");
      return;
    }
    const scope = state.stateCode ? "STATE" : state.cohort ? "COHORT" : "NATIONAL";

    const [analysis, trend, contribution, board] = await Promise.all([
      api.get("/analytics/indicators/" + state.indicator, {
        period: state.period,
        cohort: state.cohort || undefined,
      }),
      api.get("/analytics/trend/" + state.indicator, {
        scope: scope,
        state: state.stateCode || undefined,
        cohort: state.cohort || undefined,
        // Keep the series inside the selected period's family.
        period_type: selectedPeriodType(),
        periods: 8,
      }),
      api.get("/analytics/contribution/" + state.indicator, { period: state.period }),
      api.get("/analytics/scorecard", {
        period: state.period,
        scope: scope,
        state: state.stateCode || undefined,
        cohort: state.cohort || undefined,
        category: state.category || undefined,
      }),
    ]);

    const indicator = analysis.indicator;
    const suffix = indicator.unit === "PERCENT" ? "%" : "";

    $("trend-title").textContent = indicator.code + " trend — " + (trend.scope_label || "National");
    charts.lineChart("chart-trend", {
      labels: trend.points.map((p) => p.period_label),
      series: [
        { name: "Reported", values: trend.points.map((p) => p.value) },
        { name: "Target", values: trend.points.map((p) => p.target), dashed: true },
      ],
      suffix: suffix,
    });

    charts.barChart("chart-contribution", {
      data: contribution.rows.slice(0, 12).map(function (row) {
        return {
          label: row.state_name,
          value: row.contribution_pct,
          note: "Value: " + num(row.value) + suffix + " · rank " + row.rank,
        };
      }),
      suffix: "%",
      valueLabel: "Contribution",
      emptyMessage: "No state contributed a measurable value for this KPI.",
    });

    const national = analysis.national;
    $("indicator-caption").textContent = indicator.code + " · " + indicator.name;
    $("indicator-analysis").innerHTML =
      "<h3>Layer 2 — national performance against the national target</h3>" +
      '<div class="kv">' +
      "<div><dt>National value</dt><dd>" + num(national.value) + suffix + "</dd></div>" +
      "<div><dt>National target</dt><dd>" + num(national.target) + suffix + "</dd></div>" +
      "<div><dt>Achievement</dt><dd>" + pct(national.achievement_pct) + "</dd></div>" +
      "<div><dt>Status</dt><dd>" + badge(national.status) + "</dd></div>" +
      "<div><dt>States reporting</dt><dd>" + national.states_reporting + "/" + national.states_expected + "</dd></div>" +
      "<div><dt>Aggregation</dt><dd style='font-size:.82rem'>" + national.aggregation_method + "</dd></div>" +
      "</div>" +
      "<h3>Cohort disaggregation</h3>" +
      table(
        [
          { label: "Cohort", key: "cohort_name" },
          { label: "Value", num: true, render: (r) => num(r.value) + suffix },
          { label: "Target", num: true, render: (r) => num(r.target) },
          { label: "Achievement", num: true, render: (r) => pct(r.achievement_pct) },
          { label: "Contribution", num: true, render: (r) => pct(r.contribution_pct) },
          { label: "Reporting", num: true, render: (r) => r.states_reporting + "/" + r.states_expected },
          { label: "Status", render: (r) => badge(r.status) },
        ],
        analysis.cohorts
      ) +
      "<h3>Layer 1 &amp; 3 — state performance and contribution</h3>" +
      '<div class="table-scroll">' +
      table(
        [
          { label: "State", key: "state_name" },
          { label: "Cohort", key: "cohort_code" },
          { label: "Value", num: true, render: (r) => num(r.value) + (r.value === null ? "" : suffix) },
          { label: "Target", num: true, render: (r) => num(r.target) },
          { label: "Achievement", num: true, render: (r) => pct(r.achievement_pct) },
          { label: "Contribution", num: true, render: (r) => pct(r.contribution_pct) },
          { label: "Status", render: (r) => badge(r.status) },
        ],
        analysis.states
      ) +
      "</div>";

    $("scorecard-table").innerHTML = table(
      [
        { label: "KPI", render: (r) => r.indicator.code },
        { label: "Indicator", render: (r) => r.indicator.name },
        { label: "Unit", render: (r) => r.indicator.unit },
        { label: "Value", num: true, render: (r) => num(r.value) },
        { label: "Target", num: true, render: (r) => num(r.target) },
        { label: "Achievement", num: true, render: (r) => pct(r.achievement_pct) },
        { label: "Status", render: (r) => badge(r.status) },
      ],
      board.rows
    );
  }

  // -- cohorts -------------------------------------------------------------
  async function renderCohorts() {
    const summaries = await api.get("/cohorts/summary", {
      period: state.period,
      category: state.category || undefined,
    });

    $("cohort-table").innerHTML = table(
      [
        { label: "Cohort", key: "cohort_name" },
        { label: "States", key: "states_expected", num: true },
        { label: "Reporting", num: true, render: (r) => pct(r.reporting_rate_pct) },
        { label: "On time", num: true, render: (r) => pct(r.on_time_rate_pct) },
        { label: "Completeness", num: true, render: (r) => pct(r.completeness_pct) },
        { label: "Avg DQA", num: true, render: (r) => num(r.average_dqa_score) },
        { label: "DQA grade", render: (r) => badge(r.dqa_grade) },
        { label: "Avg achievement", num: true, render: (r) => pct(r.average_achievement_pct) },
        { label: "On track", num: true, render: (r) => r.indicators_on_track + "/" + r.indicators_assessed },
        { label: "Contribution", num: true, render: (r) => pct(r.contribution_pct) },
        { label: "Strongest", key: "best_state" },
        { label: "Needs support", key: "weakest_state" },
      ],
      summaries
    );

    charts.groupedBarChart("chart-cohort-reporting", {
      categories: summaries.map((r) => r.cohort_name),
      series: [
        { name: "Reporting rate", values: summaries.map((r) => r.reporting_rate_pct) },
        { name: "On-time rate", values: summaries.map((r) => r.on_time_rate_pct) },
        { name: "Completeness", values: summaries.map((r) => r.completeness_pct) },
      ],
      suffix: "%",
    });

    charts.barChart("chart-cohort-dqa", {
      data: summaries.map(function (row, index) {
        return {
          label: row.cohort_name,
          value: row.average_dqa_score,
          color: charts.SERIES[index % charts.SERIES.length],
          note: "Grade: " + row.dqa_grade,
        };
      }),
      max: 100,
      valueLabel: "Average DQA score",
      labelWidth: 170,
      reference: 60,
      referenceLabel: "gate",
    });

    if (state.cohort) {
      const ranking = await api.get("/cohorts/" + state.cohort + "/states", {
        period: state.period,
        indicator: state.indicator || undefined,
      });
      $("within-cohort-table").innerHTML = table(
        [
          { label: "#", key: "rank", num: true },
          { label: "State", key: "state_name" },
          { label: "Avg achievement", num: true, render: (r) => pct(r.average_achievement_pct) },
          { label: "On track", num: true, render: (r) => r.indicators_on_track },
          { label: "KPIs reported", num: true, render: (r) => r.indicators_with_data },
          { label: "DQA", num: true, render: (r) => num(r.dqa_score) },
          { label: "Grade", render: (r) => badge(r.dqa_grade) },
          { label: "Days late", num: true, render: (r) => (r.days_late === null ? "—" : r.days_late) },
        ],
        ranking
      );
    } else {
      $("within-cohort-table").innerHTML =
        '<p class="muted">Pick a cohort in the filter bar to rank the states inside it.</p>';
    }
  }

  // -- data quality ---------------------------------------------------------
  async function renderQuality() {
    const [summary, heat] = await Promise.all([
      api.get("/quality/national", { period: state.period }),
      api.get("/dashboard/dqa-heatmap", { periods: 6, period_type: selectedPeriodType() }),
    ]);

    $("dqa-caption").textContent =
      summary.states_reported + " of " + summary.states_expected + " states assessed";
    $("dqa-summary").innerHTML =
      '<div class="kv">' +
      "<div><dt>National score</dt><dd>" + num(summary.national_score) + "</dd></div>" +
      "<div><dt>Grade</dt><dd>" + badge(summary.grade) + "</dd></div>" +
      "<div><dt>Figures counting</dt><dd>" + pct(summary.usable_share_pct) +
      '<span style="display:block;font-size:.72rem;font-weight:400;color:var(--text-muted)">' +
      summary.figures_counting + " of " + summary.figures_reported + "</span></dd></div>" +
      "<div><dt>Reporting rate</dt><dd>" + pct(summary.reporting_rate_pct) + "</dd></div>" +
      "<div><dt>On-time rate</dt><dd>" + pct(summary.on_time_rate_pct) + "</dd></div>" +
      "<div><dt>Cleared for analysis</dt><dd>" + summary.states_approved + "</dd></div>" +
      "</div>" +
      table(
        [
          { label: "Dimension", render: (r) => r.dimension.charAt(0) + r.dimension.slice(1).toLowerCase() },
          {
            label: "Score",
            num: true,
            render: (r) => (r.score === null ? "not assessed" : num(r.score)),
          },
          { label: "Grade", render: (r) => (r.score === null ? "—" : badge(r.grade)) },
          { label: "Weight", num: true, render: (r) => num(r.weight, 1) },
          { label: "Checks run", key: "checks_run", num: true },
          { label: "Checks failed", key: "checks_failed", num: true },
        ],
        summary.dimension_averages
      ) +
      (summary.grade_note
        ? '<p class="grade-note">' + esc(summary.grade_note) + "</p>"
        : "");

    $("dqa-table").innerHTML = table(
      [
        { label: "State", key: "state_name" },
        { label: "Cohort", key: "cohort_code" },
        { label: "Status", render: (r) => badge(r.status) },
        { label: "Score", num: true, render: (r) => num(r.overall_score) },
        { label: "Grade", render: (r) => badge(r.grade) },
        {
          label: "Figures counting",
          num: true,
          render: (r) =>
            r.figures_reported
              ? r.figures_counting + " of " + r.figures_reported +
                " (" + Math.round(r.usable_share_pct) + "%)"
              : "—",
        },
        { label: "Errors", key: "error_count", num: true },
        { label: "Warnings", key: "warning_count", num: true },
        { label: "Days late", num: true, render: (r) => (r.days_late === null ? "—" : r.days_late) },
        {
          // A capped grade always travels with its reason.
          label: "Why the grade",
          render: (r) =>
            r.grade_note
              ? esc(r.grade_note)
              : r.top_issues && r.top_issues.length
                ? esc(r.top_issues[0].message)
                : "—",
        },
      ],
      summary.scorecards
    );

    charts.heatmap("chart-dqa-heatmap", {
      rows: heat.rows,
      columns: heat.columns,
      cells: heat.cells,
      rowLabels: heat.row_labels,
      columnLabels: heat.column_labels,
      min: heat.scale_min,
      max: heat.scale_max,
      scaleLabel: "DQA score · " + (heat.grade || ""),
    });

    $("common-issues").innerHTML = table(
      [
        { label: "Rule", key: "rule_code" },
        { label: "Dimension", render: (r) => r.dimension.charAt(0) + r.dimension.slice(1).toLowerCase() },
        { label: "States affected", key: "states_affected", num: true },
        { label: "Share", num: true, render: (r) => r.share_pct + "%" },
      ],
      summary.common_issues
    );

    await renderReconciliation();
  }

  // -- tracker reconciliation ------------------------------------------------
  async function renderReconciliation() {
    let report;
    try {
      report = await api.get("/reconciliation", { period: state.period });
    } catch (error) {
      // Reconciliation only makes sense for a period that decomposes.
      $("reconciliation-summary").innerHTML =
        '<p class="muted">' + esc(error.message || "Not available for this period.") + "</p>";
      $("reconciliation-table").innerHTML = "";
      $("reconciliation-lines").innerHTML = "";
      return;
    }

    const s = report.summary;
    if (!s.child_period_type) {
      // A month has nothing finer inside it to reconcile against.
      $("reconciliation-caption").textContent = "Select a quarter to reconcile";
      $("reconciliation-summary").innerHTML =
        '<p class="muted">' + esc(s.period_label) +
        " has no finer reporting period inside it. Reconciliation compares a quarter " +
        "against its own months.</p>";
      $("reconciliation-table").innerHTML = "";
      $("reconciliation-lines").innerHTML = "";
      return;
    }

    const grain = s.child_period_type.toLowerCase();
    $("reconciliation-caption").textContent =
      s.states_tracking + " of " + s.states + " states have filed a " + grain +
      " return for " + s.period_label + " (" + s.states_with_complete_tracker + " complete)";

    if (!s.judged) {
      $("reconciliation-summary").innerHTML =
        '<p class="muted">No state has filed enough of the ' + esc(grain) +
        " tracker for this period to reconcile anything yet.</p>";
      $("reconciliation-table").innerHTML = "";
      $("reconciliation-lines").innerHTML = "";
      return;
    }

    $("reconciliation-summary").innerHTML =
      '<div class="kv">' +
      "<div><dt>States tracking</dt><dd>" + s.states_tracking + " of " + s.states +
      "</dd></div>" +
      "<div><dt>Figures reconciled</dt><dd>" + s.judged + "</dd></div>" +
      "<div><dt>Agree</dt><dd>" + s.matched + "</dd></div>" +
      "<div><dt>Agreement</dt><dd>" + pct(s.agreement) + "</dd></div>" +
      "<div><dt>Disagree</dt><dd>" + (s.by_status.MISMATCH || 0) + "</dd></div>" +
      "<div><dt>States with work to do</dt><dd>" +
      (s.states_unreconciled.length ? esc(s.states_unreconciled.join(", ")) : "none") +
      "</dd></div>" +
      "</div>";

    const reporting = report.states.filter((row) => row.parts_reported.length);
    $("reconciliation-table").innerHTML = table(
      [
        { label: "State", render: (r) => esc(r.state_name) },
        { label: "Cohort", key: "cohort_code" },
        {
          label: "Tracker returns",
          render: (r) => r.parts_reported.length + " of " + r.parts_expected.length,
        },
        { label: "Reconciled", key: "judged", num: true },
        { label: "Agree", key: "matched", num: true },
        { label: "Disagree", key: "mismatched", num: true },
        { label: "Missing from quarter", key: "framework_missing", num: true },
        { label: "Missing from tracker", key: "tracker_missing", num: true },
        { label: "Agreement", num: true, render: (r) => pct(r.agreement) },
      ],
      reporting
    );

    const lines = [];
    report.states.forEach(function (row) {
      row.lines.forEach(function (line) {
        if (line.status === "MATCHED" || line.status === "INCOMPLETE") return;
        lines.push(Object.assign({ state_name: row.state_name }, line));
      });
    });

    $("reconciliation-lines").innerHTML = table(
      [
        { label: "State", render: (r) => esc(r.state_name) },
        { label: "Code", render: (r) => esc(r.indicator_code) },
        { label: "Verdict", render: (r) => badge(r.status.replace(/_/g, " ").toLowerCase()) },
        { label: "Tracker implies", num: true, render: (r) => num(r.fine_value) },
        { label: "Quarterly return", num: true, render: (r) => num(r.coarse_value) },
        { label: "Variance", num: true, render: (r) => num(r.variance) },
        { label: "Why", render: (r) => esc(r.note) },
      ],
      lines
    );
  }

  // -- query resolution ------------------------------------------------------
  // The screen a state settles its flagged figures on. A query nobody answers
  // holds its figure out of every national total, so overdue reads loudest and
  // the figure's own state -- quarantined or counting -- is always on show.

  function canReview() {
    return (state.user.permissions || []).indexOf("data:approve") !== -1;
  }

  function canRespond() {
    return (state.user.permissions || []).indexOf("data:upload") !== -1;
  }

  /** True when this user owns the figure, i.e. may answer for it. */
  function ownsQuery(query) {
    if (!canRespond()) return false;
    if (state.user.role !== "STATE_PIU") return true;
    return query.state_id === state.user.state_id;
  }

  function queryParams() {
    const params = { period: state.period, limit: 500 };
    if (state.queryState) params.state = state.queryState;
    if (state.queryFilter === "overdue") params.overdue_only = true;
    else if (state.queryFilter === "awaiting") params.awaiting_review = true;
    else if (state.queryFilter === "verification") params.verification_only = true;
    else if (state.queryFilter === "all") params.open_only = false;
    return params;
  }

  async function renderQueries() {
    const [summary, rows] = await Promise.all([
      api.get("/queries/summary", { period: state.period, state: state.queryState || undefined }),
      api.get("/queries", queryParams()),
    ]);

    const scope = state.queryState
      ? (state.states.find((s) => s.code === state.queryState) || {}).name || state.queryState
      : "all states";
    $("query-caption").textContent =
      summary.open + " open of " + summary.total + " raised for " + summary.period_label +
      " · " + scope;
    updateQueryTabCount(summary.open);

    const tiles = [
      { label: "Open", value: summary.open, caption: "still somebody's work", status: summary.open ? "lagging" : "on-track" },
      { label: "Overdue", value: summary.overdue, caption: "past the response date", status: summary.overdue ? "off-track" : "on-track" },
      { label: "Awaiting review", value: summary.awaiting_review, caption: "state has responded", status: "" },
      { label: "For verification", value: summary.for_verification, caption: "to check on a visit", status: "" },
      { label: "Figures restated", value: summary.restated, caption: "corrected with evidence", status: "on-track" },
    ];
    $("query-tiles").innerHTML = tiles
      .map(function (tile) {
        return '<div class="tile ' + tile.status + '">' +
          '<div class="label">' + tile.label + "</div>" +
          '<div class="value">' + tile.value + "</div>" +
          '<div class="caption">' + tile.caption + "</div></div>";
      })
      .join("");

    $("query-table").innerHTML = table(
      [
        { label: "Ref", render: (r) => esc(r.reference) },
        { label: "State", render: (r) => esc(r.state_name) },
        { label: "Indicator", render: (r) => esc(r.indicator_code || "—") },
        { label: "Period", render: (r) => esc(r.period_code) },
        { label: "Rule", render: (r) => esc(r.rule_code || "—") },
        { label: "Reported", num: true, render: (r) => num(r.reported_value) },
        {
          label: "Due",
          render: (r) =>
            '<span class="' + (r.is_overdue ? "due" : "") + '">' +
            (r.due_date || "—") +
            (r.is_overdue ? " (" + r.days_overdue + "d late)" : "") + "</span>",
        },
        { label: "Status", render: (r) => badge(r.status.toLowerCase()) },
        {
          label: "Figure",
          render: (r) => (r.is_quarantined ? badge("quarantined") : badge("counting")),
        },
      ],
      rows
    );

    // The table helper has no row hooks, so wire selection on the rendered rows.
    const body = $("query-table").querySelectorAll("tbody tr");
    body.forEach(function (tr, index) {
      const row = rows[index];
      tr.classList.add("query-row");
      if (row.is_overdue) tr.classList.add("overdue");
      tr.children[6].classList.add("due");
      if (state.selectedQuery === row.id) tr.classList.add("selected");
      tr.addEventListener("click", function () {
        state.selectedQuery = row.id;
        body.forEach((other) => other.classList.remove("selected"));
        tr.classList.add("selected");
        renderQueryDetail(row.id);
      });
    });

    if (state.selectedQuery && rows.some((r) => r.id === state.selectedQuery)) {
      await renderQueryDetail(state.selectedQuery);
    } else if (!rows.length) {
      state.selectedQuery = null;
      $("query-detail-title").textContent = "Resolve a query";
      setPanel("query-detail", '<p class="muted">Nothing flagged for this selection.</p>');
    }
  }

  function updateQueryTabCount(open) {
    const chip = $("tab-query-count");
    chip.hidden = false;
    chip.textContent = open;
    chip.className = "tab-count" + (open ? "" : " none");
  }

  async function renderQueryDetail(queryId) {
    let query;
    try {
      query = await api.get("/queries/" + queryId);
    } catch (error) {
      return fail(error, "Query");
    }
    state.selectedQuery = queryId;
    $("query-detail-title").textContent =
      query.reference + " · " + (query.indicator_code || "submission") + " · " + query.state_name;

    const finding =
      '<div class="finding' + (query.is_quarantined ? " blocking" : "") + '">' +
      '<div class="rule">' + esc(query.rule_code || "finding") + " · " +
      esc(query.dimension || "") + " · " + esc(query.severity || "") + "</div>" +
      "<p>" + esc(query.title) + "</p>" +
      (query.detail ? '<p class="detail">' + esc(query.detail) + "</p>" : "") +
      "</div>";

    const figures =
      '<div class="kv">' +
      "<div><dt>As reported</dt><dd>" + num(query.reported_value) + "</dd></div>" +
      "<div><dt>As it stands</dt><dd>" + num(query.current_value) + "</dd></div>" +
      "<div><dt>Indicator</dt><dd style='font-size:.85rem'>" +
      esc(query.indicator_name || "—") + "</dd></div>" +
      "<div><dt>Response due</dt><dd style='font-size:.95rem'>" +
      (query.due_date || "—") +
      (query.is_overdue ? " · " + query.days_overdue + " days late" : "") + "</dd></div>" +
      "<div><dt>In the national total</dt><dd style='font-size:.95rem'>" +
      figureStanding(query) + "</dd></div>" +
      "</div>";

    const thread = query.responses.length
      ? '<ul class="thread">' +
        query.responses
          .map(function (response) {
            const outcome = (response.review_outcome || "").toLowerCase();
            return '<li class="' + outcome + '">' +
              '<div class="who">' +
              "<strong>" + esc(response.responder || "State") + "</strong>" +
              "<span>" + esc((response.submitted_at || "").slice(0, 16).replace("T", " ")) + "</span>" +
              (response.review_outcome ? badge(outcome) : badge("awaiting review")) +
              "</div>" +
              '<p class="narrative">' + esc(response.narrative) + "</p>" +
              (response.proposed_value === null || response.proposed_value === undefined
                ? '<p class="proposal">Figure confirmed as reported.</p>'
                : '<p class="proposal">Proposes ' + num(response.proposed_value) +
                  (response.restates_period_code
                    ? " against " + esc(response.restates_period_code)
                    : "") + ".</p>") +
              (response.evidence_summary
                ? '<p class="evidence">Evidence: ' + esc(response.evidence_summary) + "</p>"
                : "") +
              (response.evidence.length
                ? '<p class="evidence">Attached: ' +
                  response.evidence.map((e) => esc(e.filename)).join(", ") + "</p>"
                : "") +
              (response.review_note
                ? '<p class="evidence">NPCU: ' + esc(response.review_note) + "</p>"
                : "") +
              "</li>";
          })
          .join("") +
        "</ul>"
      : '<p class="muted">No response yet.</p>';

    setPanel("query-detail", finding + figures + thread + queryActions(query));
    wireQueryActions(query);
  }

  /** Whether the figure counts, and if not, what is still holding it. */
  function figureStanding(query) {
    if (!query.is_quarantined) return "counting";
    if (query.held_by && query.held_by.length) {
      // Two rules can flag one figure; settling this one does not settle those.
      return "held out — also queried by " + esc(query.held_by.join(", "));
    }
    return "held out until settled";
  }

  function queryActions(query) {
    if (!query.is_open) {
      return '<div class="query-closed">' +
        esc(query.reference) + " is " + esc(query.status.toLowerCase()) +
        (query.resolution ? " — figure " + esc(query.resolution.toLowerCase()) : "") +
        (query.resolution_note ? ". " + esc(query.resolution_note) : ".") +
        "</div>";
    }

    let html = '<div class="query-actions">';

    if (ownsQuery(query)) {
      html +=
        '<fieldset><legend>Respond</legend><form class="stack" id="respond-form">' +
        "<label>Your response" +
        '<select id="respond-kind">' +
        '<option value="confirm">The figure stands as reported</option>' +
        '<option value="correct">Propose a correction</option>' +
        "</select></label>" +
        '<label id="respond-value-label" hidden>Corrected figure' +
        '<input type="text" id="respond-value" inputmode="decimal" placeholder="e.g. 342"></label>' +
        '<label id="respond-period-label" hidden>Which period this corrects' +
        '<select id="respond-period"></select></label>' +
        "<label>Explanation" +
        '<textarea id="respond-narrative" rows="3" required ' +
        'placeholder="What the figure is, and what evidence supports it"></textarea></label>' +
        "<label>Evidence provided" +
        '<input type="text" id="respond-evidence" ' +
        'placeholder="e.g. Verified contractor certificates, June site visit"></label>' +
        "<label>Attach a document (optional)" +
        '<input type="file" id="respond-file"></label>' +
        '<div class="row"><button class="btn primary" type="submit" id="respond-btn">' +
        "Submit response</button></div></form></fieldset>";
    }

    if (canReview()) {
      const responded = query.status === "RESPONDED";
      html +=
        '<fieldset><legend>NPCU decision</legend><form class="stack" id="review-form">' +
        "<label>Note" +
        '<textarea id="review-note" rows="3" ' +
        'placeholder="Why this is accepted, returned, or referred"></textarea></label>' +
        '<div class="row">' +
        '<button class="btn primary" type="button" id="accept-btn"' +
        (responded ? "" : " disabled title=\"There is no response to accept yet\"") +
        ">Accept</button>" +
        '<button class="btn ghost" type="button" id="reject-btn"' +
        (responded ? "" : " disabled title=\"There is no response to return yet\"") +
        ">Return to state</button>" +
        '<button class="btn ghost" type="button" id="verify-btn">Refer for verification</button>' +
        '<button class="btn ghost" type="button" id="withdraw-btn">Withdraw</button>' +
        "</div></form></fieldset>";
    }

    if (!ownsQuery(query) && !canReview()) {
      html += '<p class="muted">You have read access to this query but cannot act on it.</p>';
    }
    return html + "</div>";
  }

  function wireQueryActions(query) {
    const respondForm = $("respond-form");
    if (respondForm) {
      const periodSelect = $("respond-period");
      periodSelect.innerHTML = state.periods
        .map((p) => '<option value="' + p.code + '">' + p.label + "</option>")
        .join("");
      periodSelect.value = query.period_code;

      $("respond-kind").addEventListener("change", function () {
        const correcting = this.value === "correct";
        $("respond-value-label").hidden = !correcting;
        $("respond-period-label").hidden = !correcting;
      });
      respondForm.addEventListener("submit", (event) => submitResponse(event, query));
    }

    if ($("review-form")) {
      $("accept-btn").addEventListener("click", () => reviewQuery(query, "accept"));
      $("reject-btn").addEventListener("click", () => reviewQuery(query, "reject"));
      $("verify-btn").addEventListener("click", () => reviewQuery(query, "verification"));
      $("withdraw-btn").addEventListener("click", () => reviewQuery(query, "withdraw"));
    }
  }

  async function submitResponse(event, query) {
    event.preventDefault();
    const narrative = $("respond-narrative").value.trim();
    if (!narrative) return toast("Explain the figure and cite its evidence.", "error");

    const correcting = $("respond-kind").value === "correct";
    const raw = $("respond-value").value.trim();
    if (correcting && !raw) return toast("Enter the corrected figure.", "error");
    const proposed = correcting ? Number(raw.replace(/,/g, "")) : null;
    if (correcting && !isFinite(proposed)) return toast("The corrected figure is not a number.", "error");

    const button = $("respond-btn");
    button.disabled = true;
    button.textContent = "Submitting…";
    try {
      const detail = await api.post("/queries/" + query.id + "/responses", {
        narrative: narrative,
        proposed_value: proposed,
        evidence_summary: $("respond-evidence").value.trim() || null,
        restates_period_code: correcting ? $("respond-period").value : null,
      });

      const file = $("respond-file").files[0];
      if (file) {
        const responseId = detail.responses[detail.responses.length - 1].id;
        const form = new FormData();
        form.append("file", file);
        await api.upload(
          "/queries/" + query.id + "/responses/" + responseId + "/evidence", form
        );
      }
      toast("Response submitted. It now sits with the NPCU.", "success");
      await render();
    } catch (error) {
      fail(error, "Response");
    } finally {
      button.disabled = false;
      button.textContent = "Submit response";
    }
  }

  async function reviewQuery(query, action) {
    const note = $("review-note").value.trim();
    if (action !== "accept" && !note) {
      return toast("Give a reason — the state sees it.", "error");
    }
    const payload = action === "accept" ? { note: note || null } : { reason: note };
    try {
      await api.post("/queries/" + query.id + "/" + action, payload);
      toast(
        {
          accept: "Accepted. The figure is settled and back in the national total.",
          reject: "Returned to the state.",
          verification: "Referred for physical verification.",
          withdraw: "Withdrawn. The figure is released.",
        }[action],
        "success"
      );
      await render();
    } catch (error) {
      fail(error, "Review");
    }
  }

  function correctionSheetState() {
    if (state.user.role === "STATE_PIU") return state.user.state_code || state.stateCode;
    return state.queryState || state.stateCode;
  }

  async function uploadCorrectionSheet(file) {
    const stateCode = correctionSheetState();
    if (!stateCode) return toast("Choose a state before uploading its sheet.", "error");
    const form = new FormData();
    form.append("file", file);
    form.append("period_code", state.period);
    try {
      const result = await api.upload("/queries/sheets/" + stateCode, form);
      toast(result.message, result.applied ? "success" : "error");
      if (result.warnings.length) {
        console.warn("correction sheet", result.warnings);
        toast(result.warnings[0], "error");
      }
      await render();
    } catch (error) {
      fail(error, "Correction sheet");
    }
  }

  // -- states ---------------------------------------------------------------
  async function renderStates() {
    const [rankings, heat] = await Promise.all([
      api.get("/dashboard/state-rankings", scopeParams()),
      api.get("/analytics/heatmap", {
        period: state.period,
        cohort: state.cohort || undefined,
        category: state.category || undefined,
        limit_indicators: 14,
      }),
    ]);

    $("state-table").innerHTML = table(
      [
        { label: "#", key: "rank", num: true },
        { label: "State", key: "state_name" },
        { label: "Cohort", key: "cohort_code" },
        { label: "Avg achievement", num: true, render: (r) => pct(r.average_achievement_pct) },
        { label: "KPIs on track", key: "indicators_on_track", num: true },
        { label: "KPIs reported", key: "indicators_with_data", num: true },
        { label: "DQA", num: true, render: (r) => num(r.dqa_score) },
        { label: "Grade", render: (r) => badge(r.dqa_grade) },
        { label: "Submitted", render: (r) => (r.reported ? "Yes" : badge("Not submitted")) },
      ],
      rankings
    );

    charts.heatmap("chart-heatmap", {
      rows: heat.rows,
      columns: heat.columns,
      cells: heat.cells,
      rowLabels: heat.row_labels,
      columnLabels: heat.column_labels,
      max: 150,
      suffix: "%",
      scaleLabel: "Achievement against state target (capped at 150%)",
    });
  }

  // -- upload ---------------------------------------------------------------
  // -- the reporting cycle ---------------------------------------------------
  // A closed cycle takes no new file. It is not a dead end -- a correction
  // still lands through the query workflow -- so the banner says so rather
  // than leaving a state to discover the refusal at upload time.

  function canManageReference() {
    return (state.user.permissions || []).indexOf("reference:manage") !== -1;
  }

  function periodByCode(code) {
    return state.periods.find((p) => p.code === code) || null;
  }

  async function renderPeriodLock() {
    const banner = $("period-lock");
    const period = periodByCode($("upload-period").value);
    if (!period || period.is_open) {
      banner.hidden = true;
      return;
    }

    const uploadState = $("upload-state").value;
    const granted = (period.reopened_for || []).indexOf(uploadState) !== -1;
    banner.hidden = false;
    banner.className = "lock-banner " + (granted ? "granted" : "closed");
    const closedOn = period.locked_at
      ? " on " + new Date(period.locked_at).toLocaleDateString()
      : "";
    banner.innerHTML = granted
      ? "<strong>" + esc(period.label) + " is closed, but " + esc(uploadState) +
        " holds a reopening.</strong>This upload will use it. A reopening admits one return."
      : "<strong>" + esc(period.label) + " was closed" + esc(closedOn) + ".</strong>" +
        (period.lock_note ? esc(period.lock_note) + " " : "") +
        "It takes no new file. To correct a figure, answer its query with evidence — that " +
        "works on a closed cycle and keeps the original on record. To re-file the return " +
        "itself, ask the NPCU for a reopening.";
  }

  async function renderCycle() {
    const panel = $("cycle-panel");
    if (!canManageReference()) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;

    const code = $("upload-period").value;
    const period = periodByCode(code);
    if (!period) return;

    $("cycle-caption").textContent =
      period.label + " — " + (period.is_open ? "open to submissions" : "closed");
    $("cycle-close-btn").disabled = !period.is_open;
    $("cycle-reopen-btn").disabled = period.is_open;
    $("grant-row").hidden = period.is_open;
    $("grant-state").innerHTML = state.states
      .map((row) => '<option value="' + row.code + '">' + row.name + "</option>")
      .join("");

    if (period.is_open) {
      $("reopenings-table").innerHTML =
        '<p class="muted">Nothing to reopen while the cycle is open.</p>';
      return;
    }

    const grants = await api.get("/reference/periods/" + code + "/reopenings");
    $("reopenings-table").innerHTML = table(
      [
        { label: "State", render: (r) => esc(r.state_name || r.state_code) },
        { label: "Status", render: (r) => badge(r.status.toLowerCase()) },
        { label: "Reason", render: (r) => esc(r.reason) },
        { label: "Granted by", render: (r) => esc(r.granted_by || "—") },
        { label: "Expires", render: (r) => r.expires_on || "—" },
        {
          label: "Used by",
          render: (r) => (r.consumed_submission_id ? "#" + r.consumed_submission_id : "—"),
        },
      ],
      grants
    );
  }

  async function cycleAction(path, payload, success) {
    const code = $("upload-period").value;
    try {
      await api.post("/reference/periods/" + code + path, payload);
      toast(success, "success");
      // The lock state lives on the cached period list; refresh it.
      state.periods = await api.get("/reference/periods");
      await renderPeriodLock();
      await renderCycle();
    } catch (error) {
      fail(error, "Reporting cycle");
    }
  }

  async function renderUpload() {
    await renderPeriodLock();
    await renderCycle();
    const submissions = await api.get("/ingestion/submissions", {
      period: state.period,
      state: state.stateCode || undefined,
      limit: 50,
    });
    $("submissions-table").innerHTML = table(
      [
        { label: "#", key: "id", num: true },
        { label: "State", key: "state_name" },
        { label: "Period", key: "period_code" },
        { label: "Ver", key: "version", num: true },
        { label: "Status", render: (r) => badge(r.status) },
        { label: "Rows", key: "mapped_count", num: true },
        { label: "DQA", num: true, render: (r) => num(r.dqa_score) },
        { label: "Grade", render: (r) => badge(r.dqa_grade) },
        { label: "Errors", key: "error_count", num: true },
        { label: "Warnings", key: "warning_count", num: true },
        {
          label: "Uploaded",
          render: (r) => (r.uploaded_at ? new Date(r.uploaded_at).toLocaleString() : "—"),
        },
      ],
      submissions
    );
  }

  async function submitUpload(event) {
    event.preventDefault();
    const file = $("upload-file").files[0];
    if (!file) return toast("Choose a file to upload.", "error");

    const button = $("upload-btn");
    button.disabled = true;
    button.textContent = "Ingesting…";

    const form = new FormData();
    form.append("file", file);
    form.append("state_code", $("upload-state").value);
    form.append("period_code", $("upload-period").value);
    form.append("notes", $("upload-notes").value);
    form.append("auto_approve", $("upload-approve").checked ? "true" : "false");

    try {
      const result = await api.upload("/ingestion/upload", form);
      renderUploadResult(result);
      toast(result.accepted ? "Data ingested." : "Data rejected by the quality gate.",
        result.accepted ? "success" : "error");
      await renderUpload();
    } catch (error) {
      setPanel(
        "upload-result",
        '<p class="muted">Upload failed: ' + (error.message || "unknown error") + "</p>" +
          (error.details ? "<pre class='report-md'>" + JSON.stringify(error.details, null, 2) + "</pre>" : "")
      );
      fail(error, "Upload");
    } finally {
      button.disabled = false;
      button.textContent = "Ingest";
    }
  }

  function renderUploadResult(result) {
    const s = result.submission;
    const d = result.diagnostics;
    const v = result.validation;
    const mapped = (d.column_mappings || []).filter((c) => c.mapped_field);
    const unmapped = (d.column_mappings || []).filter((c) => !c.mapped_field);

    setPanel(
      "upload-result",
      "<p>" + (result.accepted ? "✔ " : "✖ ") + result.message + "</p>" +
      '<div class="kv">' +
      "<div><dt>Submission</dt><dd>#" + s.id + " v" + s.version + "</dd></div>" +
      "<div><dt>Status</dt><dd>" + badge(s.status) + "</dd></div>" +
      "<div><dt>DQA score</dt><dd>" + num(s.dqa_score) + "</dd></div>" +
      "<div><dt>Rows mapped</dt><dd>" + s.mapped_count + "/" + s.row_count + "</dd></div>" +
      "<div><dt>Errors</dt><dd>" + s.error_count + "</dd></div>" +
      "<div><dt>Warnings</dt><dd>" + s.warning_count + "</dd></div>" +
      "</div>" +
      "<h3>Schema mapping</h3>" +
      '<p class="muted">Layout detected: ' + d.template_profile +
      (d.header_row ? " · header row " + d.header_row : "") + "</p>" +
      table(
        [
          { label: "Source column", key: "source_header" },
          { label: "Mapped to", key: "mapped_field" },
          { label: "How", key: "strategy" },
          { label: "Confidence", num: true, render: (r) => num(r.confidence * 100, 0) + "%" },
        ],
        mapped
      ) +
      (unmapped.length
        ? '<p class="muted">Ignored columns: ' + unmapped.map((c) => c.source_header).join(", ") + "</p>"
        : "") +
      (d.unmatched_indicators && d.unmatched_indicators.length
        ? '<p class="muted">Unmatched indicator labels: ' + d.unmatched_indicators.join("; ") + "</p>"
        : "") +
      "<h3>Data quality (" + num(v.overall_score) + " / 100 · " + v.grade + ")</h3>" +
      table(
        [
          { label: "Dimension", render: (r) => r.dimension.charAt(0) + r.dimension.slice(1).toLowerCase() },
          {
            label: "Score",
            num: true,
            render: (r) => (r.score === null ? "not assessed" : num(r.score)),
          },
          { label: "Grade", render: (r) => (r.score === null ? "—" : badge(r.grade)) },
          { label: "Failed", key: "checks_failed", num: true },
        ],
        v.dimensions
      ) +
      (v.issues.length
        ? "<h3>Findings (" + v.issues.length + ")</h3><ul class='issue-list'>" +
          v.issues
            .slice(0, 40)
            .map(
              (i) =>
                '<li class="' + i.severity + '"><strong>' + i.severity + "</strong> · " +
                i.rule_code + " · " + i.message + "</li>"
            )
            .join("") +
          "</ul>"
        : "<p class='muted'>No validation findings.</p>")
    );
  }

  // -- reports ---------------------------------------------------------------
  function syncReportScopeRef() {
    const scope = $("report-scope").value;
    const select = $("report-scope-ref");
    if (scope === "STATE") {
      select.disabled = false;
      select.innerHTML = state.states.map((s) => '<option value="' + s.code + '">' + s.name + "</option>").join("");
      if (state.stateCode) select.value = state.stateCode;
    } else if (scope === "COHORT") {
      select.disabled = false;
      select.innerHTML = state.cohorts.map((c) => '<option value="' + c.code + '">' + c.name + "</option>").join("");
    } else {
      select.disabled = true;
      select.innerHTML = '<option value="">All AGILE states</option>';
    }
  }

  async function renderReports() {
    const reports = await api.get("/reports", { limit: 50 });
    $("reports-table").innerHTML = table(
      [
        { label: "#", key: "id", num: true },
        { label: "Title", key: "title" },
        { label: "Scope", render: (r) => r.scope + (r.scope_ref ? " · " + r.scope_ref : "") },
        { label: "Period", key: "period_code" },
        { label: "KPIs", key: "indicator_count", num: true },
        {
          label: "Generated",
          render: (r) => (r.generated_at ? new Date(r.generated_at).toLocaleString() : "—"),
        },
        {
          label: "Download",
          render: function (r) {
            return (r.formats || [])
              .map(
                (f) =>
                  '<a href="' + api.downloadUrl("/reports/" + r.id + "/download", { format: f }) +
                  '">' + f + "</a>"
              )
              .join(" · ") || "—";
          },
        },
      ],
      reports
    );
  }

  async function submitReport(event) {
    event.preventDefault();
    const button = $("report-btn");
    button.disabled = true;
    button.textContent = "Generating…";

    const formats = Array.from(
      document.querySelectorAll("#report-form .formats input[type=checkbox][value]")
    )
      .filter((box) => box.checked)
      .map((box) => box.value);

    const scope = $("report-scope").value;
    const payload = {
      period_code: $("report-period").value,
      scope: scope,
      scope_ref: scope === "NATIONAL" ? null : $("report-scope-ref").value,
      category_codes: $("report-category").value ? [$("report-category").value] : null,
      formats: formats.length ? formats : ["markdown"],
      include_dqa: $("report-dqa").checked,
      include_queries: $("report-queries").checked,
      include_trends: $("report-trends").checked,
      include_narratives: $("report-narratives").checked,
    };

    try {
      const report = await api.post("/reports", payload);
      setPanel(
        "report-result",
        "<p><strong>" + esc(report.title) + "</strong></p>" +
        "<p>" + esc(report.summary || "") + "</p>" +
        "<p>" +
        report.artifacts
          .map(
            (a) =>
              '<a class="btn ghost" href="' + a.download_url + '">Download ' + a.format +
              " (" + Math.round(a.size_bytes / 1024) + " KB)</a>"
          )
          .join(" ") +
        "</p>" +
          // The preview is the report's own Markdown, shown as text in a
          // preformatted block -- it is a document, not markup to run.
          (report.markdown
            ? '<div class="report-md">' + esc(report.markdown.slice(0, 20000)) + "</div>"
            : "")
      );
      toast("Report generated.", "success");
      await renderReports();
    } catch (error) {
      fail(error, "Report generation");
    } finally {
      button.disabled = false;
      button.textContent = "Generate";
    }
  }

  boot();
})();
