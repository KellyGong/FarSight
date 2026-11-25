// DOM helpers, formatting, icons, number tweening, tooltip and toast. No framework, no innerHTML:
// every label that reaches the page is untrusted data and goes through textContent.
import { t, N, getLang } from "./i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";
export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "data") Object.assign(el.dataset, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : value);
    }
  }
  for (const kid of kids.flat(3)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) if (value != null) el.setAttribute(key, value);
  for (const kid of kids) if (kid) el.append(kid);
  return el;
}

const ICONS = {
  grid: "M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z",
  server: "M4 5h16v6H4z M4 13h16v6H4z M8 8h.01 M8 16h.01",
  layers: "M12 3 3 8l9 5 9-5-9-5z M3 12.5l9 5 9-5 M3 17l9 5 9-5",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  settings: "M4 7h9 M17 7h3 M4 17h3 M11 17h9 M15 4.5v5 M9 14.5v5",
  plus: "M12 5v14 M5 12h14",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z M12 2v2 M12 20v2 M2 12h2 M20 12h2 M4.9 4.9l1.4 1.4 M17.7 17.7l1.4 1.4 M4.9 19.1l1.4-1.4 M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M20 20l-4-4",
  bell: "M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15L6 16z M10 20.5a2 2 0 0 0 4 0",
  back: "M19 12H5 M11 6l-6 6 6 6",
  refresh: "M20 11a8 8 0 0 0-14.5-4 M4 4v3.5h3.5 M4 13a8 8 0 0 0 14.5 4 M20 20v-3.5h-3.5",
  edit: "M4 20h4L19 9l-4-4L4 16v4z M13.5 6.5l4 4",
  trash: "M5 7h14 M10 7V4h4v3 M7 7l1 13h8l1-13",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 4 2.8 19.5h18.4L12 4z M12 10v4.5 M12 17.5h.01",
  x: "M6 6l12 12 M18 6 6 18",
  chevron: "M9 6l6 6-6 6",
  table: "M4 5h16v14H4z M4 10h16 M4 15h16 M10 5v14",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 11v5 M12 7.5h.01",
  bolt: "M13 2 4 14h7l-1 8 9-12h-7l1-8z",
  cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z M12 12l8-4.5 M12 12v9 M12 12 4 7.5",
  copy: "M9 9h10v10H9z M5 15V5h10",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2",
  key: "M8 14a4 4 0 1 1 3.9-5H21v4h-2v-2h-2v2h-5.1A4 4 0 0 1 8 14z",
  dot: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  topology: "M6 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4z M18 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4z M12 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4z M8 6h8 M7 7.8l4 8.4 M17 7.8l-4 8.4",
  coin: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M9 8l3 4 3-4 M12 12v5 M9.5 13h5",
  open: "M10 6h8v8 M18 6 8 16",
  user: "M12 4a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z M5 20c.6-3.6 3.4-5.5 7-5.5s6.4 1.9 7 5.5",
  monitor: "M4 5h16v11H4z M9 20h6 M12 16v4",
};

export function icon(name) {
  return s("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, s("path", { d: ICONS[name] || ICONS.dot }));
}

export function setText(el, text) {
  text = text == null ? "" : String(text);
  if (el.textContent !== text) el.textContent = text;
}

/** Animate a number from its previous value to `to`. `countUp` starts the first render from zero. */
export function tween(el, to, format = (value) => String(Math.round(value)), countUp = false) {
  if (to == null || !Number.isFinite(to)) {
    cancelAnimationFrame(el._raf);
    el._value = null;
    setText(el, "—");
    return;
  }
  const from = el._value == null ? (countUp ? 0 : null) : el._value;
  el._value = to;
  if (from == null || from === to || reducedMotion()) {
    cancelAnimationFrame(el._raf);
    setText(el, format(to));
    return;
  }
  cancelAnimationFrame(el._raf);
  const start = performance.now();
  const duration = 520;
  const step = (now) => {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    setText(el, format(from + (to - from) * eased));
    if (progress < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

export function meter(extra = "") {
  return h("div", { class: "meter " + extra, role: "presentation" }, h("i"));
}

/** Set a meter to `percent`. `tone` is "warn" / "crit" when the level itself is a concern, else null. */
export function setMeter(el, percent, tone = null) {
  const width = percent == null ? 0 : Math.max(0, Math.min(100, percent));
  const apply = () => { el.firstChild.style.width = width + "%"; };
  if (el.isConnected && el._set) apply();
  else requestAnimationFrame(() => requestAnimationFrame(apply));      // let it grow from zero on first paint
  el._set = true;
  if (tone) el.dataset.tone = tone; else delete el.dataset.tone;
}

/** Severity of a "fuller is worse" level such as host memory or disk. */
export const fullness = (percent, warn = 85, crit = 95) => percent == null ? null : percent >= crit ? "crit" : percent >= warn ? "warn" : null;

/**
 * Keyed reconciliation: children of `parent` are kept in the order of `items`, reusing elements by key.
 * `make(item)` builds an element and may give it an `_update(item, index)` method.
 */
export function sync(parent, items, keyOf, make) {
  const old = new Map();
  for (const child of Array.from(parent.children)) if (child.dataset.key !== undefined) old.set(child.dataset.key, child);
  let cursor = parent.firstElementChild;
  items.forEach((item, index) => {
    const key = String(keyOf(item));
    let el = old.get(key);
    if (el) {
      old.delete(key);
    } else {
      el = make(item);
      el.dataset.key = key;
      el.classList.add("enter");
      el.style.setProperty("--i", Math.min(index, 14));
      el.addEventListener("animationend", (event) => { if (event.target === el) el.classList.remove("enter"); });
    }
    if (el._update) el._update(item, index);
    if (el === cursor) cursor = cursor.nextElementSibling;
    else parent.insertBefore(el, cursor);
  });
  old.forEach((el) => el.remove());
}

export const fmt = {
  pct: (value) => value == null ? "—" : Math.round(value) + "%",
  int(value) {
    if (value == null || !Number.isFinite(value)) return "—";
    const abs = Math.abs(value);
    if (abs >= 1e6) return (value / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + "M";
    if (abs >= 1e4) return (value / 1e3).toFixed(0) + "k";
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + "k";
    return String(Math.round(value));
  },
  fixed: (value, digits = 1) => value == null ? "—" : value.toFixed(digits),
  gib(mib, digits) {
    if (mib == null) return "—";
    const gib = mib / 1024;
    return gib.toFixed(digits != null ? digits : gib >= 100 ? 0 : 1);
  },
  bytes(value) {
    if (value == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[index];
  },
  rate: (value) => value == null ? "—" : fmt.bytes(value) + "/s",
  secs(value) {
    if (value == null) return "—";
    if (value < 1) return Math.round(value * 1000) + " ms";
    return value.toFixed(value < 10 ? 2 : 1) + " s";
  },
  watts: (value) => value == null ? "—" : value >= 1000 ? (value / 1000).toFixed(2) + " kW" : Math.round(value) + " W",
  dur(seconds) {
    if (seconds == null) return "—";
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return t("{n} 秒", { n: seconds });
    if (seconds < 3600) return t("{n} 分钟", { n: Math.floor(seconds / 60) });
    if (seconds < 86400) return t("{h} 小时 {m} 分", { h: Math.floor(seconds / 3600), m: Math.floor((seconds % 3600) / 60) });
    return t("{d} 天 {h} 小时", { d: Math.floor(seconds / 86400), h: Math.floor((seconds % 86400) / 3600) });
  },
  ago(stamp, now) {
    if (!stamp) return "";
    const seconds = Math.max(0, Math.round((now || Date.now() / 1000) - stamp));
    if (seconds < 5) return t("刚刚");
    if (seconds < 60) return t("{n} 秒前", { n: seconds });
    if (seconds < 3600) return t("{n} 分钟前", { n: Math.floor(seconds / 60) });
    if (seconds < 86400) return t("{n} 小时前", { n: Math.floor(seconds / 3600) });
    return t("{n} 天前", { n: Math.floor(seconds / 86400) });
  },
  clock: (stamp, withSeconds = false) => new Date(stamp * 1000).toLocaleTimeString(getLang() === "en" ? "en-GB" : "zh-CN",
    { hour12: false, hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined }),
  /** An amount of money: enough digits to tell small per-token costs apart, no more. */
  money(value, currency = "") {
    if (value == null || !Number.isFinite(value)) return "—";
    const abs = Math.abs(value);
    const text = abs >= 1000 ? Math.round(value).toLocaleString("en-US") : abs >= 100 ? value.toFixed(0) : abs >= 10 ? value.toFixed(1)
      : abs >= 1 ? value.toFixed(2) : abs >= 0.1 ? value.toFixed(3) : abs === 0 ? "0" : value.toPrecision(2);
    return currency + text;
  },
};

// The `text` of a status is a source string: it is translated where it is shown (setPill, statusText).
export const SERVICE_STATUS = {
  running: { text: N("运行中"), tone: "good" },
  starting: { text: N("启动中"), tone: "info", pulse: true },
  degraded: { text: N("降级"), tone: "crit" },
  stopped: { text: N("已停止"), tone: "mute" },
  unknown: { text: N("状态未知"), tone: "warn" },
};
export const NODE_STATUS = {
  online: { text: N("在线"), tone: "good" },
  connecting: { text: N("连接中"), tone: "info", pulse: true },
  error: { text: N("连接失败"), tone: "crit" },
  need_password: { text: N("需要密码"), tone: "warn" },
  stopped: { text: N("已停止"), tone: "mute" },
};
export const HEALTH = {
  ok: { text: N("就绪"), tone: "good" },
  starting: { text: N("启动中"), tone: "info", pulse: true },
  error: { text: N("无响应"), tone: "crit" },
  unknown: { text: N("未探测"), tone: "mute" },
};
export const statusText = (info) => t(info.text);
export const ROLE = { frontend: "Frontend", prefill: "Prefill", decode: "Decode", worker: "Worker", server: "Server", router: "Router", etcd: "etcd" };
/** Role name for display; "client" and "other" are not service roles and have translated names. */
export const roleName = (role) => ROLE[role] || (role === "client" ? t("客户端") : role === "other" ? t("其他进程") : role || "");
export const LEVEL = { crit: { tone: "crit", icon: "alert" }, warn: { tone: "serious", icon: "alert" }, info: { tone: "info", icon: "info" }, ok: { tone: "good", icon: "check" } };

export function pill(info) {
  const el = h("span", { class: "pill" }, h("i"), h("span"));
  setPill(el, info);
  return el;
}
export function setPill(el, info) {
  el.dataset.tone = info.tone;
  if (info.pulse) el.dataset.pulse = ""; else delete el.dataset.pulse;
  setText(el.lastChild, t(info.text));
}

export function chip(text, role, onclick) {
  const el = h(onclick ? "button" : "span", { class: "chip", type: onclick ? "button" : null, title: text }, role ? h("i") : null, text);
  if (role) el.dataset.role = role;
  if (onclick) el.addEventListener("click", (event) => { event.stopPropagation(); onclick(event); });
  return el;
}

export function gpuModelLabel(node) {
  return node.gpu_count ? `${node.gpu_model || "GPU"} × ${node.gpu_count}` : t("无 GPU");
}

// ---- tooltip
const tipEl = () => document.getElementById("tip");
export function showTip(x, y, parts) {
  const el = tipEl();
  el.replaceChildren(...parts);
  el.hidden = false;
  const box = el.getBoundingClientRect();
  el.style.left = Math.max(8, Math.min(window.innerWidth - box.width - 8, x - box.width / 2)) + "px";
  el.style.top = Math.max(8, y - box.height - 12) + "px";
}
export function hideTip() { tipEl().hidden = true; }

/** Hover / focus tooltip for a small mark; `content()` returns the tooltip nodes. */
export function hoverTip(el, content) {
  const show = (event) => {
    const box = el.getBoundingClientRect();
    showTip(event.clientX || box.left + box.width / 2, box.top, content());
  };
  el.addEventListener("pointerenter", show);
  el.addEventListener("pointermove", show);
  el.addEventListener("pointerleave", hideTip);
  el.addEventListener("focus", show);
  el.addEventListener("blur", hideTip);
}

// ---- toast
export function toast(message, timeout = 4200) {
  const el = h("div", { class: "toast", role: "status" }, message);
  document.getElementById("toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 600);
  }, timeout);
}

// ---- API
export async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: { Accept: "application/json", "X-Lang": getLang() }, cache: "no-store", credentials: "same-origin" };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-Requested-With"] = "farsight";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  if (response.status === 401) { location.reload(); throw new Error(t("需要访问令牌")); }
  let payload = null;
  try { payload = await response.json(); } catch (error) { /* non-JSON error page */ }
  if (!response.ok) {
    const failure = new Error((payload && payload.error) || t("请求失败（HTTP {status}）", { status: response.status }));
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

export function section(title, aside, ...kids) {
  return h("section", { class: "section" },
    h("div", { class: "section-title" }, h("h3", { text: title }), typeof aside === "string" ? h("span", { text: aside }) : aside),
    ...kids);
}

let feedNow = 0;

/** Activity feed row list. */
export function renderFeed(container, events, now, showName = true) {
  if (!events.length) {
    if (!container.querySelector(".empty")) container.replaceChildren(h("div", { class: "empty", text: t("暂无活动") }));
    return;
  }
  const empty = container.querySelector(".empty");
  if (empty) empty.remove();
  feedNow = now;
  sync(container, events, (event) => event.id, (event) => {
    const level = LEVEL[event.level] || LEVEL.info;
    const time = h("time");
    const el = h("div", { class: "ev" },
      h("div", { class: "ev-ico", data: { tone: level.tone } }, icon(level.icon)),
      h("div", null, h("b", { text: (showName && event.name ? event.name + " · " : "") + event.title }),
        event.detail ? h("p", { text: event.detail, title: event.detail }) : null),
      time);
    el._update = (item) => setText(time, fmt.ago(item.t, feedNow));
    return el;
  });
}
