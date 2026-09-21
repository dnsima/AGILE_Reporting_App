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

  /* The only scope is the period. Cohort, category and KPI were global
     filters once; they now live where they belong -- cohort as a dimension
     two overview charts cut by, category as the component tabs themselves,
     and a KPI as a row in its component's table. stateCode survives only to
     hold a state PIU inside its own data. */
  const state = {
    user: null,
    period: null,
    stateCode: "",
    view: "overview",
    periods: [],
    states: [],
    cohorts: [],
    components: [],
    dataVersion: 0,
    analysisStale: false,
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
      const [periods, states, cohorts, components] = await Promise.all([
        api.get("/reference/periods"),
        api.get("/reference/states"),
        api.get("/reference/cohorts"),
        api.get("/reference/indicator-categories"),
      ]);
      state.periods = periods;
      state.states = states;
      state.cohorts = cohorts;
      state.components = components;
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

    const stateOptions = state.states
      .map((s) => '<option value="' + s.code + '">' + s.name + "</option>")
      .join("");

    // Upload + report forms still need the reference lists.
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
    $("report-category").innerHTML =
      '<option value="">All indicators</option>' +
      state.components
        .map((c) => '<option value="' + c.code + '">' + c.name + "</option>")
        .join("");
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
    $("workbook-btn").addEventListener("click", function () {
      // Built from the same payload the board draws, so the sheet a reader
      // downloads cannot disagree with the screen they downloaded it from.
      window.location.href = api.downloadUrl("/reports/analysis-workbook", {
        period: state.period,
      });
      toast("Building the workbook for " + state.period + "…", "info");
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
        // A new return or a settled query changes the figures, so the drawn
        // model is stale even though the period has not changed.
        state.analysisStale = true;
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
          state.analysisStale = true;
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
      (payload.verdict || "") +
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
      /* Six of the nine tabs are views of one payload, so they are loaded
         once and drawn together. Switching between them redraws nothing and
         re-requests nothing -- which is also why they cannot disagree. */
      if (ANALYSIS_VIEWS.indexOf(state.view) !== -1) await renderAnalysis();
      else if (state.view === "queries") await renderQueries();
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

  /* The tabs the analysis model feeds. */
  const ANALYSIS_VIEWS = ["overview", "pdo", "c1", "c2", "c3", "quality"];

  /**
   * Load the analysis model and draw every panel from it.
   *
   * Drawn once per period, not once per tab. The old board redrew each panel
   * from its own request when you switched to it, which is how the panels
   * came to disagree in the first place.
   */
  async function renderAnalysis() {
    const drawn = window.AGILEAnalysis.model();
    if (drawn && drawn.period_code === state.period && !state.analysisStale) return;
    await window.AGILEAnalysis.load(api, state.period);
    state.analysisStale = false;
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
          render: (r) => badge(DISCLOSURE_LABEL[r.disclosure] || "counting"),
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
      '<div class="finding' + (query.disclosure === "UNFIT" ? " blocking" : "") + '">' +
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

  /* What the platform is saying about a figure. Never "excluded": every
     reported figure counts towards the national totals whatever its label.
     Holding the doubtful ones out made this platform's totals disagree with
     the NPCU's own published report by nearly 200,000 on one indicator, so
     the label now says what to trust, not what was counted. */
  const DISCLOSURE_LABEL = {
    CLEAN: "clean",
    QUERIED: "queried",
    UNFIT: "not fit for use",
    CORRECTED: "corrected",
  };

  /** How the figure stands: it counts either way, but here is what it carries. */
  function figureStanding(query) {
    const label = DISCLOSURE_LABEL[query.disclosure] || "counting";
    if (query.disclosure === "CLEAN" || query.disclosure === "CORRECTED") {
      return label + " — counting towards the national total";
    }
    if (query.held_by && query.held_by.length) {
      // Two rules can flag one figure; settling this one does not settle those.
      return label + " — counting, but also queried by " + esc(query.held_by.join(", "));
    }
    return label + " — counting towards the national total, and disclosed as such";
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
        { label: "Fitness", render: (r) => badge(r.fitness_verdict || "not assessed") },
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
      "<div><dt>Fitness</dt><dd>" + badge(s.fitness_verdict || "not assessed") + "</dd></div>" +
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
      include_data_quality: $("report-dqa").checked,
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
