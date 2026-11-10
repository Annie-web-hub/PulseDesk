document.addEventListener("DOMContentLoaded", () => {
  const source = document.getElementById("analytics-data");
  if (!source) return;
  let data;
  try {
    data = JSON.parse(source.textContent || "{}");
  } catch (_error) {
    return;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  const palette = {
    opened: "#83b5ff",
    resolved: "#78d9b0",
    active: "#d9ff65",
    critical: "#ff7965",
    high: "#f4bb68",
    medium: "#83b5ff",
    low: "#78d9b0",
    grid: "#303b49",
    axis: "#8190a0",
    panel: "#151b26"
  };

  const svgNode = (tag, attrs = {}, textValue = null) => {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (textValue !== null) node.textContent = String(textValue);
    return node;
  };
  const addText = (svg, x, y, value, attrs = {}) => {
    svg.append(svgNode("text", {x, y, ...attrs}, value));
  };
  const showTooltip = (tooltip, wrap, target, textValue) => {
    if (!tooltip || !wrap || !target) return;
    tooltip.textContent = textValue;
    tooltip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const left = targetRect.left - wrapRect.left + targetRect.width / 2;
    const top = targetRect.top - wrapRect.top;
    tooltip.style.left = Math.max(8, Math.min(wrapRect.width - 175, left + 10)) + "px";
    tooltip.style.top = Math.max(5, top - 38) + "px";
  };
  const attachPointTooltip = (point, tooltip, wrap, label) => {
    point.addEventListener("pointerenter", () => showTooltip(tooltip, wrap, point, label));
    point.addEventListener("pointermove", () => showTooltip(tooltip, wrap, point, label));
    point.addEventListener("pointerleave", () => { if (tooltip) tooltip.hidden = true; });
    point.addEventListener("focus", () => showTooltip(tooltip, wrap, point, label));
    point.addEventListener("blur", () => { if (tooltip) tooltip.hidden = true; });
  };

  // Line chart: ticket intake against completed work.
  const trendSvg = document.getElementById("incident-trend-chart");
  if (trendSvg) {
    const values = Array.isArray(data.trend) ? data.trend : [];
    const width = 820, height = 270;
    const left = 46, right = 18, top = 16, bottom = 45;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const maxValue = Math.max(1, ...values.flatMap((item) => [item.opened, item.resolved]));
    const yMax = Math.max(4, Math.ceil(maxValue / 4) * 4);
    const xFor = (index) => left + (values.length <= 1 ? .5 : index / (values.length - 1)) * plotWidth;
    const yFor = (value) => top + plotHeight - (value / yMax) * plotHeight;
    for (let step = 0; step <= 4; step += 1) {
      const value = yMax * step / 4;
      const y = yFor(value);
      trendSvg.append(svgNode("line", {x1:left, y1:y, x2:width-right, y2:y, stroke:palette.grid, "stroke-dasharray":"3 5"}));
      addText(trendSvg, left - 12, y + 3, Math.round(value), {fill:palette.axis, "font-size":"10", "text-anchor":"end", "font-family":"IBM Plex Mono"});
    }
    const labelIndexes = new Set([0, values.length - 1]);
    const labelCount = data.days <= 30 ? 7 : 6;
    for (let index = 0; index < labelCount; index += 1) {
      labelIndexes.add(Math.round(index * Math.max(0, values.length - 1) / Math.max(1, labelCount - 1)));
    }
    labelIndexes.forEach((index) => {
      if (!values[index]) return;
      addText(trendSvg, xFor(index), height - 15, values[index].label, {
        fill:palette.axis, "font-size":"9", "text-anchor":"middle", "font-family":"IBM Plex Mono"
      });
    });

    [
      {key:"opened", color:palette.opened, name:"Opened"},
      {key:"resolved", color:palette.resolved, name:"Resolved"}
    ].forEach((series) => {
      const points = values.map((item, index) => ({x:xFor(index), y:yFor(item[series.key]), value:item[series.key], label:item.label}));
      const pathData = points.map((point, index) => (index ? "L" : "M") + point.x + " " + point.y).join(" ");
      trendSvg.append(svgNode("path", {d:pathData, fill:"none", stroke:series.color, "stroke-width":"2.8", "stroke-linecap":"round", "stroke-linejoin":"round"}));
      points.forEach((point) => {
        const circle = svgNode("circle", {
          cx:point.x, cy:point.y, r:4.2, fill:series.color, stroke:palette.panel, "stroke-width":2,
          tabindex:0, role:"graphics-symbol", "aria-label":point.label + ": " + point.value + " incidents " + series.name.toLowerCase()
        });
        circle.append(svgNode("title", {}, point.label + " · " + series.name + ": " + point.value));
        trendSvg.append(circle);
        attachPointTooltip(circle, document.querySelector("[data-chart-tooltip]"), trendSvg.parentElement, point.label + " · " + series.name + ": " + point.value);
      });
    });
    trendSvg.append(svgNode("line", {x1:left, y1:top+plotHeight, x2:width-right, y2:top+plotHeight, stroke:palette.grid}));
  }

  // Donut chart: mix of incident priorities created during the reporting window.
  const donutSvg = document.getElementById("priority-donut-chart");
  if (donutSvg) {
    const priorities = data.priority_rows || [];
    const total = priorities.reduce((sum, row) => sum + Number(row.total || 0), 0);
    const cx = 110, cy = 92, radius = 57, circumference = 2 * Math.PI * radius;
    donutSvg.append(svgNode("circle", {cx, cy, r:radius, fill:"none", stroke:"#303b49", "stroke-width":16}));
    const colors = {Critical:palette.critical, High:palette.high, Medium:palette.medium, Low:palette.low};
    let offset = 0;
    priorities.forEach((item) => {
      const value = Number(item.total || 0);
      if (!value || !total) return;
      const length = circumference * value / total;
      const arc = svgNode("circle", {
        cx, cy, r:radius, fill:"none", stroke:colors[item.priority] || palette.axis,
        "stroke-width":16, "stroke-dasharray":length + " " + (circumference - length),
        "stroke-dashoffset":-offset, transform:"rotate(-90 " + cx + " " + cy + ")",
        tabindex:0, role:"graphics-symbol",
        "aria-label":item.priority + ": " + value + " incidents"
      });
      arc.append(svgNode("title", {}, item.priority + " · " + value + " incidents"));
      donutSvg.append(arc);
      offset += length;
    });
    if (!total) addText(donutSvg, cx, cy + 4, "NO DATA", {fill:palette.axis, "font-size":"10", "text-anchor":"middle", "font-family":"IBM Plex Mono"});
  }

  // Stacked workload chart: current active queue and recent resolved work by assignee.
  const teamSvg = document.getElementById("team-capacity-chart");
  if (teamSvg) {
    const rows = (data.team_rows || []).slice(0, 8);
    const width = 760, left = 132, right = 48, top = 12, rowHeight = 29;
    const plotWidth = width - left - right;
    const maxTotal = Math.max(1, ...rows.map((row) => Number(row.active || 0) + Number(row.resolved || 0)));
    rows.forEach((row, index) => {
      const y = top + index * rowHeight;
      const active = Number(row.active || 0);
      const resolved = Number(row.resolved || 0);
      const activeWidth = active / maxTotal * plotWidth;
      const resolvedWidth = resolved / maxTotal * plotWidth;
      addText(teamSvg, left - 12, y + 13, row.assignee.length > 17 ? row.assignee.slice(0, 16) + "…" : row.assignee, {
        fill:"#c4ced8", "font-size":"10", "text-anchor":"end", "font-family":"Space Grotesk"
      });
      teamSvg.append(svgNode("rect", {x:left, y:y, width:plotWidth, height:11, rx:4, fill:"#252f3c"}));
      teamSvg.append(svgNode("rect", {x:left, y:y, width:activeWidth, height:11, rx:4, fill:palette.active}));
      teamSvg.append(svgNode("rect", {x:left+activeWidth, y:y, width:resolvedWidth, height:11, rx:4, fill:palette.resolved}));
      addText(teamSvg, width - right + 12, y + 10, active + resolved, {
        fill:palette.axis, "font-size":"9", "text-anchor":"start", "font-family":"IBM Plex Mono"
      });
      const group = svgNode("g", {tabindex:0, role:"graphics-symbol", "aria-label":row.assignee + ": " + active + " active and " + resolved + " resolved"});
      group.append(svgNode("rect", {x:left, y:y-4, width:Math.max(12, activeWidth+resolvedWidth), height:19, fill:"transparent"}));
      group.append(svgNode("title", {}, row.assignee + " · " + active + " active · " + resolved + " resolved"));
      teamSvg.append(group);
      attachPointTooltip(group, document.querySelector("[data-team-tooltip]"), teamSvg.parentElement, row.assignee + ": " + active + " active · " + resolved + " resolved");
    });
    if (!rows.length) addText(teamSvg, 380, 100, "No team data yet", {fill:palette.axis, "font-size":"12", "text-anchor":"middle"});
    teamSvg.setAttribute("viewBox", "0 0 760 " + Math.max(90, top + rows.length * rowHeight + 8));
  }
});