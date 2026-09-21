/* AGILE dashboard chart library.
 *
 * Plain SVG, no dependencies and no CDN, so the dashboard renders on an
 * air-gapped ministry network. Colours come from CSS custom properties, which
 * means light/dark theming happens without a re-render.
 *
 * Conventions enforced here:
 *   - one value axis per chart, never two;
 *   - categorical hues assigned in fixed slot order, never cycled;
 *   - sequential (single-hue) ramp for magnitude heatmaps;
 *   - 2px lines, >=8px markers, 4px rounded bar ends, 2px gap between fills;
 *   - recessive grid and axes, selective direct labels, hover tooltip on every mark.
 */
window.AgileCharts = (function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
  const SEQ = [
    "var(--seq-100)", "var(--seq-200)", "var(--seq-300)", "var(--seq-400)",
    "var(--seq-500)", "var(--seq-600)", "var(--seq-700)",
  ];
  // The four performance bands map onto the four reserved status roles, in
  // severity order. Status colours are never reused as series colours, and
  // every mark they paint also carries a number or a text badge, so the colour
  // never carries the meaning on its own.
  const STATUS = {
    "On track": "var(--status-good)",
    Progressing: "var(--status-warning)",
    Lagging: "var(--status-serious)",
    "Off track": "var(--status-critical)",
  };

  // ---------------------------------------------------------------- utils --
  function el(name, attrs, styles) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs || {}) {
      if (attrs[key] !== null && attrs[key] !== undefined) {
        node.setAttribute(key, attrs[key]);
      }
    }
    for (const key in styles || {}) node.style.setProperty(key, styles[key]);
    return node;
  }

  function fmt(value, decimals) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    const d = decimals === undefined ? (Math.abs(value) >= 100 ? 0 : 1) : decimals;
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function niceMax(value) {
    if (!value || value <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const normalised = value / magnitude;
    const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function truncate(text, max) {
    const value = String(text === null || text === undefined ? "" : text);
    return value.length > max ? value.slice(0, max - 1) + "…" : value;
  }

  // -------------------------------------------------------------- tooltip --
  let tooltip = null;
  function tip() {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "viz-tooltip";
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function bindTip(node, html) {
    node.addEventListener("mouseenter", function (event) {
      const box = tip();
      box.innerHTML = html;
      box.classList.add("visible");
      move(event);
    });
    node.addEventListener("mousemove", move);
    node.addEventListener("mouseleave", function () {
      tip().classList.remove("visible");
    });
    function move(event) {
      const box = tip();
      const width = box.offsetWidth || 180;
      const left = Math.min(event.clientX + 14, window.innerWidth - width - 12);
      box.style.left = left + "px";
      box.style.top = Math.max(8, event.clientY - box.offsetHeight - 12) + "px";
    }
  }

  function swatch(color) {
    return '<span class="swatch" style="background:' + color + '"></span>';
  }

  // ---------------------------------------------------------------- frame --
  function reset(host) {
    if (typeof host === "string") host = document.getElementById(host);
    if (!host) return null;
    host.innerHTML = "";
    return host;
  }

  function empty(host, message) {
    host = reset(host);
    if (!host) return;
    const box = document.createElement("div");
    box.className = "chart-empty";
    box.textContent = message || "No data for the current selection.";
    host.appendChild(box);
  }

  function legend(host, entries) {
    if (entries.length < 2) return; // a single series is named by the panel title
    const box = document.createElement("div");
    box.className = "viz-legend";
    entries.forEach(function (entry) {
      const item = document.createElement("span");
      item.innerHTML = '<i style="background:' + entry.color + '"></i>' + entry.label;
      box.appendChild(item);
    });
    host.appendChild(box);
  }

  function svg(host, width, height) {
    const node = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
      // A natural width so a wide chart scrolls in its box rather than being
      // squeezed until its labels collide. The stylesheet still lets a chart
      // in a narrow panel scale down to 100%.
      width: width,
    });
    host.appendChild(node);
    return node;
  }

  function gridlines(parent, x0, x1, scale, max, ticks) {
    const group = el("g", { class: "grid" });
    for (let i = 0; i <= ticks; i += 1) {
      const value = (max / ticks) * i;
      const y = scale(value);
      group.appendChild(
        el("line", { x1: x0, x2: x1, y1: y, y2: y }, { stroke: "var(--chart-grid)" })
      );
      group.appendChild(
        el("text", { x: x0 - 6, y: y + 3, "text-anchor": "end", class: "tick-label" }, {
          fill: "var(--chart-muted)", "font-size": "10px",
        })
      ).textContent = fmt(value, value >= 100 ? 0 : 1);
    }
    parent.appendChild(group);
  }

  // ------------------------------------------------------ horizontal bars --
  /**
   * Ranked horizontal bars. Used for magnitude comparisons across a named set
   * (states, cohorts, dimensions) and for part-to-whole contribution, where a
   * ranked bar reads far better than a many-slice pie.
   */
  function barChart(host, options) {
    host = reset(host);
    if (!host) return;
    const data = (options.data || []).filter(function (row) {
      return row.value !== null && row.value !== undefined && !Number.isNaN(row.value);
    });
    if (!data.length) return empty(host, options.emptyMessage);

    const rowHeight = options.rowHeight || 26;
    const labelWidth = options.labelWidth || 150;
    const padRight = 72;
    const width = 720;
    const height = data.length * rowHeight + 22;
    const max = niceMax(options.max || Math.max.apply(null, data.map((r) => Math.abs(r.value))));
    const plotWidth = width - labelWidth - padRight;
    const node = svg(host, width, height);

    // Reference line (e.g. the 100%-of-target mark) — one axis, never two.
    if (options.reference) {
      const x = labelWidth + (options.reference / max) * plotWidth;
      node.appendChild(
        el("line", { x1: x, x2: x, y1: 2, y2: height - 18, "stroke-dasharray": "3 3" }, {
          stroke: "var(--chart-axis)",
        })
      );
      node.appendChild(
        el("text", { x: x, y: height - 6, "text-anchor": "middle" }, {
          fill: "var(--chart-muted)", "font-size": "9px",
        })
      ).textContent = options.referenceLabel || String(options.reference);
    }

    data.forEach(function (row, index) {
      const y = index * rowHeight + 4;
      const barHeight = rowHeight - 10; // 2px+ surface gap between adjacent fills
      const length = Math.max(2, (Math.abs(row.value) / max) * plotWidth);
      const color = row.color || (row.status && STATUS[row.status]) || SERIES[0];

      node.appendChild(
        el("text", { x: labelWidth - 8, y: y + barHeight / 2 + 3.5, "text-anchor": "end" }, {
          fill: "var(--chart-ink)", "font-size": "11px",
        })
      ).textContent = truncate(row.label, options.labelChars || 22);

      const bar = el("rect", {
        x: labelWidth, y: y, width: length, height: barHeight, rx: 4, ry: 4,
      }, { fill: color });
      bindTip(
        bar,
        "<b>" + row.label + "</b>" + swatch(color) +
          (options.valueLabel || "Value") + ": " + fmt(row.value) + (options.suffix || "") +
          (row.note ? "<br>" + row.note : "")
      );
      node.appendChild(bar);

      // Direct value label — also the relief for the sub-3:1 light-mode aqua.
      node.appendChild(
        el("text", { x: labelWidth + length + 7, y: y + barHeight / 2 + 3.5 }, {
          fill: "var(--chart-ink)", "font-size": "10px", "font-weight": "600",
        })
      ).textContent = fmt(row.value) + (options.suffix || "");
    });

    if (options.legendEntries) legend(host, options.legendEntries);
  }

  // --------------------------------------------------------- grouped bars --
  /** Grouped vertical bars: one group per category, one bar per series. */
  function groupedBarChart(host, options) {
    host = reset(host);
    if (!host) return;
    const categories = options.categories || [];
    const series = (options.series || []).filter(function (s) {
      return (s.values || []).some((v) => v !== null && v !== undefined);
    });
    if (!categories.length || !series.length) return empty(host, options.emptyMessage);

    let max = 0;
    series.forEach(function (s) {
      (s.values || []).forEach(function (v) {
        if (v !== null && v !== undefined && v > max) max = v;
      });
    });
    max = niceMax(max);

    // A crowded chart needs room, not a smaller font. Eighteen states with
    // three series each was drawing "AdamawaBauchi" as one word and stacking
    // the value labels on top of one another, so the width grows with the
    // number of groups and the labels turn on their side when they must.
    const crowded = categories.length > 8;
    // Sized so eighteen states and three series still land inside a
    // full-width card rather than scrolling off the right of it. A chart a
    // reader has to drag sideways is one they will read only half of.
    const width = Math.max(options.width || 720, categories.length * (20 * series.length + 14));
    // Ticks are drawn to the left of the axis, so the margin has to clear the
    // longest one. At 46px a "1,000,000" tick was cut to ",000,000".
    const tickWidth = fmt(max, max >= 100 ? 0 : 1).length * 6.6 + 12;
    const margin = {
      // A rotated value label stands up off the top of its bar, so the tallest
      // bar needs that much clearance or its label is cut in half -- "100%"
      // was rendering as "10".
      top: crowded ? 46 : 18,
      right: 14,
      bottom: crowded ? 74 : 46,
      left: Math.max(46, tickWidth),
    };
    const height = (options.height || 280) + (crowded ? 28 : 0);
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const scaleY = (v) => margin.top + plotHeight - (v / max) * plotHeight;

    const node = svg(host, width, height);
    gridlines(node, margin.left, width - margin.right, scaleY, max, 4);
    node.appendChild(
      el("line", {
        x1: margin.left, x2: width - margin.right,
        y1: margin.top + plotHeight, y2: margin.top + plotHeight,
      }, { stroke: "var(--chart-axis)" })
    );

    const groupWidth = plotWidth / categories.length;
    const barWidth = Math.min(38, (groupWidth - 14) / series.length - 2);

    categories.forEach(function (category, gi) {
      const groupX = margin.left + gi * groupWidth;
      series.forEach(function (s, si) {
        const value = (s.values || [])[gi];
        if (value === null || value === undefined) return;
        const color = s.color || SERIES[si % SERIES.length];
        const x = groupX + (groupWidth - (barWidth + 2) * series.length) / 2 + si * (barWidth + 2);
        const y = scaleY(value);
        const bar = el("rect", {
          x: x, y: y, width: barWidth, height: Math.max(2, margin.top + plotHeight - y),
          rx: 4, ry: 4,
        }, { fill: color });
        bindTip(bar, "<b>" + category + "</b>" + swatch(color) + s.name + ": " +
          fmt(value) + (options.suffix || ""));
        node.appendChild(bar);

        // The direct label is the relief for a fill that sits below 3:1, so
        // it is dropped only when the bars are too narrow to carry it --
        // where it would overlap its neighbour and be unreadable anyway.
        if (series.length <= 4 && barWidth >= 14) {
          node.appendChild(
            el("text", {
              x: x + barWidth / 2,
              y: y - 5,
              "text-anchor": crowded ? "start" : "middle",
              transform: crowded ? "rotate(-90 " + (x + barWidth / 2) + " " + (y - 5) + ")" : null,
            }, {
              fill: "var(--chart-ink)", "font-size": "9px", "font-weight": "600",
            })
          ).textContent = fmt(value, 0) + (options.suffix || "");
        }
      });

      const labelX = groupX + groupWidth / 2;
      const labelY = margin.top + plotHeight + (crowded ? 10 : 16);
      node.appendChild(
        el("text", {
          x: labelX,
          y: labelY,
          "text-anchor": crowded ? "end" : "middle",
          transform: crowded ? "rotate(-45 " + labelX + " " + labelY + ")" : null,
        }, { fill: "var(--chart-muted)", "font-size": "10px" })
      ).textContent = truncate(category, crowded ? 14 : 16);
    });

    legend(host, series.map(function (s, i) {
      return { label: s.name, color: s.color || SERIES[i % SERIES.length] };
    }));
  }

  // ------------------------------------------------------------ line chart --
  /** Multi-series line chart with a shared crosshair and tooltip. */
  function lineChart(host, options) {
    host = reset(host);
    if (!host) return;
    const labels = options.labels || [];
    const series = (options.series || []).filter(function (s) {
      return (s.values || []).some((v) => v !== null && v !== undefined);
    });
    if (labels.length < 2 || !series.length) {
      return empty(host, options.emptyMessage || "Not enough periods to plot a trend yet.");
    }

    const width = 720;
    const height = 280;
    const margin = { top: 14, right: 18, bottom: 42, left: 52 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;

    let max = 0;
    series.forEach(function (s) {
      (s.values || []).forEach(function (v) {
        if (v !== null && v !== undefined && v > max) max = v;
      });
    });
    max = niceMax(max);
    const scaleX = (i) => margin.left + (labels.length === 1 ? plotWidth / 2 : (i / (labels.length - 1)) * plotWidth);
    const scaleY = (v) => margin.top + plotHeight - (v / max) * plotHeight;

    const node = svg(host, width, height);
    gridlines(node, margin.left, width - margin.right, scaleY, max, 4);
    node.appendChild(
      el("line", {
        x1: margin.left, x2: width - margin.right,
        y1: margin.top + plotHeight, y2: margin.top + plotHeight,
      }, { stroke: "var(--chart-axis)" })
    );

    labels.forEach(function (label, index) {
      if (labels.length > 8 && index % 2 === 1) return;
      node.appendChild(
        el("text", {
          x: scaleX(index), y: margin.top + plotHeight + 16, "text-anchor": "middle",
        }, { fill: "var(--chart-muted)", "font-size": "10px" })
      ).textContent = truncate(label, 12);
    });

    series.forEach(function (s, si) {
      const color = s.color || SERIES[si % SERIES.length];
      const points = [];
      (s.values || []).forEach(function (value, index) {
        if (value === null || value === undefined) return;
        points.push([scaleX(index), scaleY(value), index, value]);
      });
      if (!points.length) return;

      node.appendChild(
        el("path", {
          d: points.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" "),
          fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
          "stroke-dasharray": s.dashed ? "5 4" : null,
        }, { stroke: color })
      );

      points.forEach(function (p) {
        // 2px surface ring keeps overlapping markers separable.
        const marker = el("circle", { cx: p[0], cy: p[1], r: 4.5, "stroke-width": 2 }, {
          fill: color, stroke: "var(--chart-surface)",
        });
        bindTip(marker, "<b>" + labels[p[2]] + "</b>" + swatch(color) + s.name + ": " +
          fmt(p[3]) + (options.suffix || ""));
        node.appendChild(marker);
      });

      // Direct-label the final point so identity never rests on colour alone.
      const last = points[points.length - 1];
      node.appendChild(
        el("text", { x: last[0] - 4, y: last[1] - 10, "text-anchor": "end" }, {
          fill: "var(--chart-ink)", "font-size": "10px", "font-weight": "600",
        })
      ).textContent = fmt(last[3]) + (options.suffix || "");
    });

    legend(host, series.map(function (s, i) {
      return { label: s.name, color: s.color || SERIES[i % SERIES.length] };
    }));
  }

  // --------------------------------------------------------------- heatmap --
  /** Sequential single-hue heatmap: one hue, light to dark, never a rainbow. */
  function heatmap(host, options) {
    host = reset(host);
    if (!host) return;
    const rows = options.rows || [];
    const columns = options.columns || [];
    const cells = options.cells || [];
    if (!rows.length || !columns.length) return empty(host, options.emptyMessage);

    const lookup = {};
    // An explicit max is a hard ceiling (values above it saturate the darkest
    // step); without one, the scale grows to fit the data.
    const fixedMax = options.max !== undefined && options.max !== null;
    let max = fixedMax ? options.max : 0;
    cells.forEach(function (cell) {
      lookup[cell.row_key + "|" + cell.column_key] = cell;
      if (!fixedMax && cell.value !== null && cell.value !== undefined && cell.value > max) {
        max = cell.value;
      }
    });
    max = max || 100;
    // Values that cluster in a narrow band need the ramp to
    // span the observed range, or every cell renders the same shade.
    const min = options.min || 0;
    const span = Math.max(max - min, 1e-9);

    const cellWidth = Math.max(54, Math.min(96, 840 / Math.max(columns.length, 1)));
    const cellHeight = 22;
    const labelWidth = 118;
    const headerHeight = 54;
    const width = labelWidth + columns.length * cellWidth + 10;
    const height = headerHeight + rows.length * cellHeight + 10;
    const node = svg(host, width, height);

    columns.forEach(function (column, ci) {
      const x = labelWidth + ci * cellWidth + cellWidth / 2;
      const label = el("text", {
        x: x, y: headerHeight - 8, "text-anchor": "start",
        transform: "rotate(-38 " + x + " " + (headerHeight - 8) + ")",
      }, { fill: "var(--chart-muted)", "font-size": "10px" });
      label.textContent = truncate(options.columnLabels ? (options.columnLabels[column] || column) : column, 16);
      node.appendChild(label);
    });

    rows.forEach(function (row, ri) {
      const y = headerHeight + ri * cellHeight;
      node.appendChild(
        el("text", { x: labelWidth - 8, y: y + cellHeight / 2 + 3.5, "text-anchor": "end" }, {
          fill: "var(--chart-ink)", "font-size": "10px",
        })
      ).textContent = truncate(options.rowLabels ? (options.rowLabels[row] || row) : row, 16);

      columns.forEach(function (column, ci) {
        const cell = lookup[row + "|" + column];
        const value = cell ? cell.value : null;
        const x = labelWidth + ci * cellWidth;
        const hasValue = value !== null && value !== undefined;
        const position = hasValue
          ? Math.min(1, Math.max(0, (Math.min(value, max) - min) / span))
          : 0;
        const step = hasValue
          ? SEQ[Math.min(SEQ.length - 1, Math.floor(position * SEQ.length))]
          : "var(--surface-2)";

        // 2px inset keeps a surface gap between adjacent fills.
        const rect = el("rect", {
          x: x + 1, y: y + 1, width: cellWidth - 2, height: cellHeight - 2, rx: 3, ry: 3,
        }, { fill: step });
        bindTip(
          rect,
          "<b>" + (options.rowLabels ? options.rowLabels[row] || row : row) + "</b>" +
            (options.columnLabels ? options.columnLabels[column] || column : column) + "<br>" +
            (hasValue ? fmt(value) + (options.suffix || "") : "Not reported") +
            (cell && cell.label ? " · " + cell.label : "")
        );
        node.appendChild(rect);

        if (hasValue && cellWidth >= 54) {
          const dark = position > 0.55;
          node.appendChild(
            el("text", {
              x: x + cellWidth / 2, y: y + cellHeight / 2 + 3.5, "text-anchor": "middle",
            }, { fill: dark ? "#ffffff" : "var(--chart-ink)", "font-size": "9px" })
          ).textContent = fmt(value, 0);
        }
      });
    });

    const scale = document.createElement("div");
    scale.className = "viz-scale";
    scale.innerHTML =
      "<span>" + fmt(min, 0) + "</span><span class='ramp'>" +
      SEQ.map((step) => "<i style='background:" + step + "'></i>").join("") +
      "</span><span>" + fmt(max, 0) + (options.suffix || "") + "</span>" +
      "<span style='margin-left:10px'>" + (options.scaleLabel || "") + "</span>";
    host.appendChild(scale);
  }

  return {
    barChart: barChart,
    groupedBarChart: groupedBarChart,
    lineChart: lineChart,
    heatmap: heatmap,
    empty: empty,
    format: fmt,
    SERIES: SERIES,
    STATUS: STATUS,
  };
})();
