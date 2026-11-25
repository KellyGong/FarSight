// Small-multiple trend charts: one series, one axis, 2px line over a 10% wash, hairline grid,
// crosshair + tooltip on hover and keyboard focus. Values are also reachable through the data-table dialog.
import { h, s, setText, showTip, hideTip, fmt, icon } from "./util.js";
import { t } from "./i18n.js";

const W = 300, H = 72, PAD = 3;

function niceMax(value) {
  if (!(value > 0)) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 1.5, 2, 3, 5, 7.5, 10]) if (value <= step * power) return step * power;
  return 10 * power;
}

/**
 * One trend chart. `spec`: { label, format(value) -> string, max (fixed y maximum, optional) }.
 * Returns the <figure>; call `figure.update(times, values)` with parallel arrays (null = gap).
 */
export function trend(spec) {
  const value = h("b", { text: "—" });
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", tabindex: "0", role: "img" });
  const grid = [0, 0.5, 1].map((share) => s("line", { class: "grid", x1: 0, x2: W, y1: PAD + (H - 2 * PAD) * share, y2: PAD + (H - 2 * PAD) * share }));
  const area = s("path", { class: "area" });
  const line = s("path", { class: "line" });
  const cross = s("line", { class: "cross", y1: 0, y2: H, visibility: "hidden" });
  svg.append(...grid, area, line, cross);
  const dot = h("i", { class: "chart-dot", hidden: true });
  const left = h("span"), peak = h("span"), right = h("span");
  const none = h("div", { class: "none", text: t("暂无数据"), hidden: true });
  const plot = h("div", { class: "plot" }, svg, dot);
  const figure = h("figure", { class: "chart" },
    h("figcaption", null, h("span", { text: spec.label }), value), plot, none, h("div", { class: "axis" }, left, peak, right));
  let data = { t: [], v: [], max: 1 };
  let active = -1;

  const xOf = (index) => {
    const first = data.t[0], last = data.t[data.t.length - 1];
    return last > first ? ((data.t[index] - first) / (last - first)) * W : W;
  };
  const yOf = (number) => H - PAD - (Math.min(number, data.max) / data.max) * (H - 2 * PAD);

  function focusPoint(index, clientX) {
    if (index < 0 || data.v[index] == null) { hide(); return; }
    active = index;
    const x = xOf(index), y = yOf(data.v[index]);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    dot.hidden = false;
    dot.style.left = (x / W) * 100 + "%";
    dot.style.top = (y / H) * 100 + "%";
    const box = svg.getBoundingClientRect();
    showTip(clientX != null ? clientX : box.left + (x / W) * box.width, box.top + (y / H) * box.height,
      [h("b", { text: spec.format(data.v[index]) }), h("span", { text: fmt.clock(data.t[index], true) })]);
  }
  function hide() {
    active = -1;
    cross.setAttribute("visibility", "hidden");
    dot.hidden = true;
    hideTip();
  }
  function nearest(clientX) {
    const box = svg.getBoundingClientRect();
    const target = ((clientX - box.left) / box.width) * W;
    let best = -1, distance = Infinity;
    for (let index = 0; index < data.t.length; index += 1) {
      if (data.v[index] == null) continue;
      const gap = Math.abs(xOf(index) - target);
      if (gap < distance) { distance = gap; best = index; }
    }
    return best;
  }
  svg.addEventListener("pointermove", (event) => focusPoint(nearest(event.clientX), event.clientX));
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("blur", hide);
  svg.addEventListener("focus", () => {
    for (let index = data.v.length - 1; index >= 0; index -= 1) if (data.v[index] != null) { focusPoint(index); break; }
  });
  svg.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { hide(); return; }
    const step = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    if (!step) return;
    event.preventDefault();
    let index = active < 0 ? data.v.length - 1 : active + step;
    while (index >= 0 && index < data.v.length && data.v[index] == null) index += step;
    if (index >= 0 && index < data.v.length) focusPoint(index);
  });

  figure.update = (times, values) => {
    const points = (values || []).filter((number) => number != null);
    const last = points.length ? points[points.length - 1] : null;
    setText(value, last == null ? "—" : spec.format(last));
    const empty = !points.length || !times || times.length < 2;
    plot.hidden = empty;
    none.hidden = !empty;
    if (empty) { setText(left, ""); setText(peak, ""); setText(right, ""); svg.setAttribute("aria-label", `${spec.label}: ${t("暂无数据")}`); return; }
    const top = Math.max(...points);
    data = { t: times, v: values, max: spec.max || niceMax(top) };
    let path = "", fill = "", open = false, startX = 0, lastX = 0;
    for (let index = 0; index < values.length; index += 1) {
      if (values[index] == null) {
        if (open) { fill += `L${lastX.toFixed(1)} ${H} L${startX.toFixed(1)} ${H} Z`; open = false; }
        continue;
      }
      const x = xOf(index), y = yOf(values[index]);
      if (!open) { path += `M${x.toFixed(1)} ${y.toFixed(1)}`; fill += `M${x.toFixed(1)} ${y.toFixed(1)}`; startX = x; open = true; }
      else { path += `L${x.toFixed(1)} ${y.toFixed(1)}`; fill += `L${x.toFixed(1)} ${y.toFixed(1)}`; }
      lastX = x;
    }
    if (open) fill += `L${lastX.toFixed(1)} ${H} L${startX.toFixed(1)} ${H} Z`;
    line.setAttribute("d", path);
    area.setAttribute("d", fill);
    setText(left, fmt.clock(times[0]));
    setText(right, fmt.clock(times[times.length - 1]));
    setText(peak, t("峰值 {value}", { value: spec.format(top) }));
    svg.setAttribute("aria-label", t("{label}：当前 {now}，峰值 {peak}。左右方向键逐点读取。", { label: spec.label, now: spec.format(last), peak: spec.format(top) }));
    if (active >= 0 && document.activeElement === svg) focusPoint(Math.min(active, values.length - 1));
  };
  return figure;
}

/** A group of trend charts sharing one history payload, a time-range control and a data-table view. */
export function trends(specs, onWindow, initialWindow = 900) {
  const figures = specs.map((spec) => Object.assign(trend(spec), { _key: spec.key }));
  let history = { t: [] };
  let current = initialWindow;
  const ranges = [[300, t("5 分钟")], [900, t("15 分钟")], [3600, t("1 小时")]];
  const seg = h("div", { class: "seg", role: "group", "aria-label": t("时间范围") }, ranges.map(([seconds, label]) =>
    h("button", { type: "button", "aria-pressed": String(seconds === current), text: label, onclick: (event) => {
      current = seconds;
      for (const button of seg.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
      onWindow(seconds);
    } })));
  const table = h("button", { class: "btn small ghost", type: "button", "aria-label": t("以表格查看趋势数据"), title: t("数据表"), onclick: () => openTable(specs, history) }, icon("table"));
  const el = h("div", null,
    h("div", { class: "trend-tools" }, seg, table),
    h("div", { class: "charts" }, figures));
  el.update = (payload) => {
    history = payload || { t: [] };
    for (const figure of figures) figure.update(history.t, history[figure._key]);
  };
  el.window = () => current;
  return el;
}

function openTable(specs, history) {
  const rows = [];
  for (let index = (history.t || []).length - 1; index >= 0; index -= 1) {
    rows.push(h("tr", null, h("td", { class: "num", text: fmt.clock(history.t[index], true) }),
      specs.map((spec) => h("td", { class: "r num", text: history[spec.key] && history[spec.key][index] != null ? spec.format(history[spec.key][index]) : "—" }))));
  }
  const dialog = h("dialog", { class: "sheet wide" },
    h("div", { class: "sheet-head" }, h("div", null, h("h2", { text: t("趋势数据") }), h("p", { text: t("与图表相同的采样，最新在前") })),
      h("button", { class: "back", type: "button", "aria-label": t("关闭"), onclick: () => dialog.close() }, icon("x"))),
    h("div", { class: "sheet-body" }, h("div", { class: "scroll-x" }, h("table", { class: "plain" },
      h("thead", null, h("tr", null, h("th", { text: t("时间") }), specs.map((spec) => h("th", { class: "r", text: spec.label })))),
      h("tbody", null, rows.length ? rows : h("tr", null, h("td", { colspan: specs.length + 1, class: "muted", text: t("暂无数据") })))))));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
