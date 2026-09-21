/**
 * The analysis board: every panel drawn from one payload.
 *
 * The board used to assemble each panel from its own request under its own
 * subset of five filters, and the panels disagreed with each other. A
 * reporting rate read "18 of 11 states" because the denominator honoured the
 * cohort filter and the numerator did not; the data-quality board ignored
 * both. Three fixes went in at three call sites and a fourth panel was
 * always waiting.
 *
 * So there is one request here, for one reporting period, and every panel
 * reads the same arrays out of it. Cohort is a dimension two charts cut by,
 * not a filter over the board. A single state is a column in the table, and
 * a bar in the charts, not a scope that shrinks the national total. A panel
 * cannot disagree with the workbook, because neither computes anything.
 */
window.AGILEAnalysis = (function () {
  "use strict";

  var charts = window.AgileCharts;
  var model = null;

  var COMPONENTS = ["PDO", "C1", "C2", "C3"];
  var SEV_NAME = { C: "CRITICAL", H: "HIGH", M: "MEDIUM" };

  /* The headline figures, named by code rather than by position, so a
     recode of the framework breaks loudly instead of quietly showing the
     wrong indicator. These four went dead once before when the framework
     went from 70 indicators to 53 and nobody noticed for a quarter. */
  var HEADLINES = [
    { code: "PDO-01", label: "Students benefiting", note: "Direct interventions, JSS-SSS" },
    { code: "PDO-04", label: "Girls enrolled (JS1-SS3)", note: "Against target" },
    { code: "PDO-07", label: "Girls completion rate", note: "Against target" },
    { code: "PDO-08", label: "JS3 completion rate", note: "Against target" },
    { code: "C1.2-05", label: "Schools receiving SIG", note: "Against target" },
    { code: "C2.2a-01", label: "Life skills participation", note: "Against target" },
    { code: "C2.2c-01", label: "Out-of-school girls in NFE", note: "Against target" },
    { grm: true, label: "GRM resolution rate", note: "Target 90%" }
  ];

  var $ = function (id) { return document.getElementById(id); };

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function fmt(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: decimals || 0,
      maximumFractionDigits: decimals === undefined ? 1 : decimals
    });
  }

  function find(code) {
    if (!model) return null;
    for (var i = 0; i < model.indicators.length; i += 1) {
      if (model.indicators[i].code === code) return model.indicators[i];
    }
    return null;
  }

  function byComponent(component) {
    return model.indicators.filter(function (row) { return row.component === component; });
  }

  function targeted(component) {
    return byComponent(component).filter(function (row) {
      return row.target && row.achievement_pct !== null;
    });
  }

  /* The band a percentage of target falls in. One place, so a tile, a table
     cell and a bar never disagree about whether 79.6% is on track. */
  function band(pct) {
    if (pct === null || pct === undefined) return "";
    if (pct >= 80) return "good";
    if (pct >= 50) return "warn";
    return "bad";
  }

  function bandColour(pct) {
    var name = band(pct);
    if (name === "good") return "var(--status-good)";
    if (name === "warn") return "var(--status-warning)";
    if (name === "bad") return "var(--status-serious)";
    return "var(--chart-muted)";
  }

  // ---------------------------------------------------------------- tiles --
  function renderHeadlines() {
    var host = $("kpis");
    if (!host) return;

    host.innerHTML = HEADLINES.map(function (spec) {
      var value;
      var note = spec.note;
      var tone = "neutral";

      if (spec.grm) {
        var received = find("C3.0-06");
        var addressed = find("C3.0-07");
        if (!received || !addressed || !received.achieved) {
          return tile(spec.label, "—", "No grievances recorded", "neutral");
        }
        var rate = (addressed.achieved / received.achieved) * 100;
        value = rate.toFixed(1) + "%";
        tone = rate >= 90 ? "good" : rate >= 70 ? "warn" : "bad";
        note = fmt(addressed.achieved, 0) + " of " + fmt(received.achieved, 0) + " addressed";
        return tile(spec.label, value, note, tone);
      }

      var row = find(spec.code);
      if (!row) {
        /* Say so rather than print a blank. An indicator that has vanished
           from the framework is a thing someone needs to fix, not a gap. */
        return tile(spec.label, "—", spec.code + " is not in this framework", "neutral");
      }
      value = row.national_display;
      if (row.achievement_pct !== null && row.achievement_pct !== undefined) {
        tone = band(row.achievement_pct);
        note = row.achievement_pct.toFixed(1) + "% of target " +
               (row.rate ? fmt(row.target, 1) + "%" : fmt(row.target, 0));
      }
      var flag = row.national_flag ? ' <span class="flag-dot fd-' + row.national_flag + '"></span>' : "";
      return tile(spec.label + flag, value, note, tone);
    }).join("");
  }

  function tile(label, value, note, tone) {
    return '<div class="kpi ' + tone + '">' +
      '<div class="kpi-lbl">' + label + "</div>" +
      '<div class="kpi-val">' + esc(value) + "</div>" +
      '<div class="kpi-note ' + tone + '">' + esc(note) + "</div></div>";
  }

  // --------------------------------------------------------------- tables --
  /**
   * One component's indicators: every state as a column, the national total,
   * the target and the achievement.
   *
   * This is the shape of the NPCU's own analysis workbook, deliberately. A
   * reader who knows that sheet can read this table without being taught it,
   * and the two can be checked against each other cell by cell.
   */
  function buildTable(component) {
    var rows = byComponent(component);
    if (!rows.length) {
      return '<p class="muted">No indicator in this component was reported for this period.</p>';
    }

    var html = '<div class="legend">' +
      '<span><i class="sw sw-C"></i> Critical — the figure cannot be true as reported</span>' +
      '<span><i class="sw sw-H"></i> High — movement large enough to change a national reading</span>' +
      '<span><i class="sw sw-M"></i> Medium — a coverage gap or an unexplained change</span>' +
      '<span><i class="sw sw-good"></i> ≥80% of target</span>' +
      '<span><i class="sw sw-warn"></i> 50–79%</span>' +
      '<span><i class="sw sw-bad"></i> &lt;50%</span>' +
      '</div><div class="tbl-wrap"><table><thead><tr>' +
      '<th class="sticky-l left">Indicator</th><th>Code</th><th>Type</th>';

    model.states.forEach(function (name) { html += "<th>" + esc(name) + "</th>"; });
    html += "<th>National</th><th>Target</th><th>% of target</th></tr></thead><tbody>";

    rows.forEach(function (row) {
      var severity = row.flag_severity || {};
      var worst = row.national_flag || severity[Object.keys(severity)[0]];
      var dot = worst ? ' <span class="flag-dot fd-' + worst + '"></span>' : "";
      html += '<tr title="' + esc(row.flag_note) + '">' +
        '<td class="left sticky-l">' + esc(row.name) + dot + "</td>" +
        '<td class="code">' + esc(row.code) + "</td>" +
        '<td class="type">' + esc(row.type) + "</td>";

      model.states.forEach(function (name, index) {
        var sev = severity[name];
        var cls = sev ? "f" + sev : "";
        if (!sev && row.boolean) {
          cls = row.states[index] === null ? "" : row.states[index] >= 1 ? "yes" : "no";
        }
        html += '<td class="' + cls + '">' + esc(row.display[index]) + "</td>";
      });

      html += '<td class="' + (row.national_flag ? "f" + row.national_flag : "natcol") + '">' +
        esc(row.national_display) + "</td>";
      html += '<td class="tgtcol">' +
        (row.target ? esc(row.rate ? fmt(row.target, 1) + "%" : fmt(row.target, 0))
                    : (row.boolean ? "All " + row.expected : "—")) + "</td>";
      html += '<td class="pctcol ' + (band(row.achievement_pct) ? "p-" + band(row.achievement_pct) : "") + '">' +
        (row.achievement_pct !== null && row.achievement_pct !== undefined
          ? row.achievement_pct.toFixed(1) + "%" : "—") + "</td></tr>";
    });

    html += "</tbody></table></div>" +
      '<p class="note">Hover an indicator to read its data quality note. A dash is an unreported ' +
      "figure, which is not the same as a reported zero. Rate indicators are the unweighted " +
      "average of the states that reported.</p>";
    return html;
  }

  // --------------------------------------------------------------- charts --
  function drawScorecard() {
    var all = [];
    COMPONENTS.forEach(function (component) {
      targeted(component).forEach(function (row) { all.push(row); });
    });
    all.sort(function (a, b) { return a.achievement_pct - b.achievement_pct; });

    charts.barChart($("chScore"), {
      /* Capped so one indicator at 450% does not squash the other thirty
         into the axis. The label still carries the real number. */
      data: all.map(function (row) {
        return {
          label: row.code,
          value: Math.min(row.achievement_pct, 170),
          color: bandColour(row.achievement_pct),
          note: row.name + " — " + fmt(row.achieved, row.rate ? 1 : 0) +
                (row.rate ? "%" : "") + " of " + fmt(row.target, row.rate ? 1 : 0) +
                (row.rate ? "%" : "") + " (" + row.achievement_pct.toFixed(1) + "%)"
        };
      }),
      max: 170,
      reference: 100,
      referenceLabel: "target",
      suffix: "%",
      labelWidth: 86,
      labelChars: 12,
      valueLabel: "Achievement",
      emptyMessage: "No indicator in this period carries a target."
    });
  }

  function drawEnrolment() {
    var row = find("PDO-04");
    if (!row) return charts.empty($("chEnrol"), "PDO-04 is not in this framework.");
    var severity = row.flag_severity || {};

    var pairs = model.states.map(function (name, index) {
      return { label: name, value: row.states[index], severity: severity[name] };
    }).filter(function (pair) { return pair.value !== null; })
      .sort(function (a, b) { return b.value - a.value; });

    charts.barChart($("chEnrol"), {
      data: pairs.map(function (pair) {
        return {
          label: pair.label,
          value: pair.value,
          /* A flagged state is coloured as flagged, not as large. Reading
             the tallest bar as the best performer is exactly the mistake
             this board exists to stop. */
          color: pair.severity ? "var(--status-serious)" : "var(--series-1)",
          note: pair.severity ? SEV_NAME[pair.severity] + " data quality flag" : undefined
        };
      }),
      labelWidth: 96,
      labelChars: 13,
      valueLabel: "Girls enrolled",
      emptyMessage: "No state reported this indicator."
    });
  }

  /** Cohort as a dimension: the same national arrays, cut two ways. */
  function cohortSeries(codes, aggregate) {
    var groups = model.cohorts || [];
    return groups.map(function (cohort, index) {
      return {
        name: cohort.name + " (" + cohort.states.length + ")",
        color: "var(--series-" + ((index % 3) + 1) + ")",
        values: codes.map(function (code) {
          var row = find(code);
          if (!row) return null;
          var values = cohort.states.map(function (name) {
            return row.states[model.states.indexOf(name)];
          }).filter(function (value) { return value !== null && value !== undefined; });
          return aggregate(values);
        })
      };
    });
  }

  var mean = function (values) {
    if (!values.length) return null;
    var live = values.filter(function (v) { return v > 0; });
    if (!live.length) return 0;
    return Math.round((live.reduce(function (a, b) { return a + b; }, 0) / live.length) * 10) / 10;
  };
  var total = function (values) {
    return values.reduce(function (a, b) { return a + b; }, 0);
  };

  function drawCohortRates() {
    charts.groupedBarChart($("chCohortRate"), {
      categories: ["Overall", "JS3", "SS3"],
      series: cohortSeries(["PDO-07", "PDO-08", "PDO-09"], mean),
      suffix: "%",
      emptyMessage: "Completion rates were not reported this period."
    });
  }

  function drawCohortReach() {
    charts.groupedBarChart($("chCohortReach"), {
      categories: ["Life skills", "OOS girls in NFE", "Social safety net"],
      series: cohortSeries(["C2.2a-01", "C2.2c-01", "C2.3-06"], total),
      emptyMessage: "Demand-side indicators were not reported this period."
    });
  }

  function drawCompletion() {
    var series = [
      { code: "PDO-07", name: "Overall" },
      { code: "PDO-08", name: "JS3" },
      { code: "PDO-09", name: "SS3" }
    ].map(function (spec, index) {
      var row = find(spec.code);
      return {
        name: spec.name,
        color: "var(--series-" + (index + 1) + ")",
        values: row ? row.states.map(function (v) { return v === null ? null : v; }) : []
      };
    });

    charts.groupedBarChart($("chComplete"), {
      categories: model.states,
      series: series,
      suffix: "%",
      emptyMessage: "Completion rates were not reported this period."
    });
  }

  function drawComponentChart(component, canvasId) {
    var rows = targeted(component).slice().sort(function (a, b) {
      return a.achievement_pct - b.achievement_pct;
    });
    charts.barChart($(canvasId), {
      data: rows.map(function (row) {
        return {
          label: row.code,
          value: Math.min(row.achievement_pct, 170),
          color: bandColour(row.achievement_pct),
          note: row.name + " — " + fmt(row.achieved, row.rate ? 1 : 0) +
                " of " + fmt(row.target, row.rate ? 1 : 0)
        };
      }),
      max: 170,
      reference: 100,
      referenceLabel: "target",
      suffix: "%",
      labelWidth: 86,
      labelChars: 12,
      valueLabel: "Achievement",
      emptyMessage: "No indicator in this component carries a target this period."
    });
  }

  function drawGrm() {
    var received = find("C3.0-06");
    var addressed = find("C3.0-07");
    if (!received || !addressed) {
      return charts.empty($("chGRM"), "Grievance indicators were not reported this period.");
    }
    charts.groupedBarChart($("chGRM"), {
      categories: model.states,
      series: [
        { name: "Received", color: "var(--series-1)", values: received.states },
        { name: "Addressed", color: "var(--series-3)", values: addressed.states }
      ],
      emptyMessage: "Grievance indicators were not reported this period."
    });
  }

  function drawPolicy() {
    var adopted = find("C3.0-01");
    var implementing = find("C3.0-02");
    if (!adopted || !implementing) {
      return charts.empty($("chPolicy"), "Policy indicators were not reported this period.");
    }
    var yes = function (row) {
      return row.states.filter(function (v) { return v !== null && v >= 1; }).length;
    };
    var no = function (row) {
      return row.states.filter(function (v) { return v !== null && v < 1; }).length;
    };
    charts.groupedBarChart($("chPolicy"), {
      categories: ["Policy adopted", "Policy implemented"],
      series: [
        { name: "Yes", color: "var(--status-good)", values: [yes(adopted), yes(implementing)] },
        { name: "No", color: "var(--status-serious)", values: [no(adopted), no(implementing)] }
      ],
      emptyMessage: "Policy indicators were not reported this period."
    });
  }

  // ------------------------------------------------------- quality register --
  /**
   * The open findings, as the workbook's flags sheet lists them: one row per
   * finding kind per indicator, with the states named alongside.
   *
   * Deliberately no score. A grade averages a quarter's problems into one
   * number that reads "Excellent" while a state's return is unusable; what a
   * reader needs is the finding itself, the states it touches, and what it
   * does to the national figure.
   */
  function renderQuality() {
    var host = $("dqList");
    if (!host) return;
    if (!model.flags.length) {
      host.innerHTML = '<p class="muted">No open finding against this period’s returns.</p>';
      return;
    }
    host.innerHTML = model.flags.map(function (flag) {
      var severity = flag.severity === "CRITICAL" ? "C" : "H";
      /* Where the states' findings differ, each one's own figures are shown
         under its own name. A register that prints one state's numbers above
         a list of four states' names is one nobody can act on. */
      var detail = (flag.details || []).length
        ? '<ul class="dq-detail">' + flag.details.map(function (line) {
            return "<li><b>" + esc(line.state) + ":</b> " + esc(line.issue) + "</li>";
          }).join("") + "</ul>"
        : "";
      return '<div class="dq-item ' + severity + '">' +
        '<div class="dq-head"><span class="sev ' + severity + '">' + esc(flag.severity) + "</span>" +
        '<span class="dq-code">' + esc(flag.code) + "</span>" +
        '<span class="dq-ind">' + esc(flag.indicator) + "</span>" +
        '<span class="dq-states">' + esc(flag.states.join(", ")) + "</span></div>" +
        '<div class="dq-txt">' + esc(flag.issue) + "</div>" + detail + "</div>";
    }).join("");
  }

  function renderSummary() {
    var host = $("analysis-caption");
    if (!host) return;
    var flagged = model.indicators.filter(function (row) {
      return Object.keys(row.flag_severity || {}).length || row.national_flag;
    }).length;
    host.textContent = model.period_label + " · " + model.indicators.length +
      " indicators · " + model.states_reporting + " of " + model.states.length +
      " states reporting · " + flagged + " indicators carrying a flag";
  }

  // ----------------------------------------------------------------- draw --
  function renderAll() {
    renderHeadlines();
    renderSummary();
    COMPONENTS.forEach(function (component) {
      var host = $("t-" + component);
      if (host) host.innerHTML = buildTable(component);
    });
    renderQuality();

    drawScorecard();
    drawEnrolment();
    drawCohortRates();
    drawCohortReach();
    drawCompletion();
    drawComponentChart("PDO", "chPDO");
    drawComponentChart("C1", "chC1");
    drawComponentChart("C2", "chC2");
    drawGrm();
    drawPolicy();
  }

  return {
    /** Load one period's analysis model and draw every panel from it. */
    load: function (api, periodCode) {
      return api.get("/dashboard/analysis", { period: periodCode }).then(function (payload) {
        model = payload;
        renderAll();
        return payload;
      });
    },
    model: function () { return model; },
    redraw: renderAll
  };
})();
