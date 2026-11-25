// Entry point: routing, polling, shell chrome (rail, title, search, theme, language) and view transitions.
import { h, icon, setText, api, reducedMotion, hideTip } from "./util.js";
import { t, N, getLang, setLang } from "./i18n.js";
import { overview } from "./views/overview.js";
import { nodeView } from "./views/node.js";
import { serviceView } from "./views/service.js";
import { nodesPage, servicesPage, activityPage, settingsPage } from "./views/pages.js";
import { nodeDialog, confirmRemove } from "./dialogs.js";

const $ = (id) => document.getElementById(id);
const root = $("view");
const store = { state: null, meta: null, view: null, route: null, source: null, failures: 0, lastList: "#/" };

const RAIL = [
  { hash: "#/", icon: "grid", label: N("概览"), match: (route) => route.name === "overview" },
  { hash: "#/nodes", icon: "server", label: N("节点"), match: (route) => route.name === "nodes" || route.name === "node" },
  { hash: "#/services", icon: "layers", label: N("服务"), match: (route) => route.name === "services" || route.name === "service" },
  { hash: "#/activity", icon: "activity", label: N("活动"), match: (route) => route.name === "activity" },
  { hash: "#/settings", icon: "settings", label: N("设置"), match: (route) => route.name === "settings" },
];

function parse(hash) {
  const parts = (hash || "#/").replace(/^#\/?/, "").split("/").filter(Boolean);
  if (!parts.length) return { name: "overview", tab: "nodes" };
  if (parts[0] === "topology") return { name: "overview", tab: "topology", node: parts[1] || null };
  if (parts[0] === "node" && parts[1]) return { name: "node", id: parts[1] };
  if (parts[0] === "service" && parts[1]) return { name: "service", id: parts[1] };
  if (["nodes", "services", "activity", "settings"].includes(parts[0])) return { name: parts[0] };
  return { name: "overview", tab: "nodes" };
}

const ctx = {
  get state() { return store.state; },
  now: () => (store.state ? store.state.now : Date.now() / 1000),
  meta: () => store.meta,
  query: () => $("search").value.trim().toLowerCase(),
  nodeById: (id) => (store.state ? store.state.nodes.find((node) => node.id === id) : null),
  serviceById: (id) => (store.state ? store.state.services.find((service) => service.id === id) : null),
  go(hash, source) {
    store.source = source || null;
    if (location.hash === hash) navigate(); else location.hash = hash;
  },
  back() { ctx.go(store.lastList || "#/"); },
  addNode: (preset) => nodeDialog({ preset: preset || {}, defaults: { user: store.meta ? store.meta.default_user : "root" } }),
  editNode: (node) => nodeDialog({ node }),
  removeNode: (node, done) => confirmRemove(node, done),
  refreshChrome: () => chrome(),
  themeChoice: () => { try { return localStorage.getItem("farsight-theme") || "auto"; } catch (error) { return "auto"; } },
  setTheme(choice) {
    try { if (choice === "auto") localStorage.removeItem("farsight-theme"); else localStorage.setItem("farsight-theme", choice); } catch (error) { /* storage blocked */ }
    applyTheme();
  },
  lang: () => getLang(),
  setLang: (next) => changeLang(next),
};

// ---- theme
function applyTheme() {
  const choice = ctx.themeChoice();
  const dark = choice === "auto" ? window.matchMedia("(prefers-color-scheme: dark)").matches : choice === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("theme").replaceChildren(icon(dark ? "sun" : "moon"));
  const label = dark ? t("切换到浅色主题") : t("切换到深色主题");
  $("theme").title = label;
  $("theme").setAttribute("aria-label", label);
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
$("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  const change = () => ctx.setTheme(next);
  if (document.startViewTransition && !reducedMotion()) document.startViewTransition(change); else change();
});

// ---- language
function applyLang() {
  const current = getLang();
  for (const part of $("lang").children) part.classList.toggle("on", part.dataset.lang === current);
  const label = current === "zh" ? t("切换到英文界面") : t("切换到中文界面");
  $("lang").title = label;
  $("lang").setAttribute("aria-label", label);
  $("skip").textContent = t("跳至内容");
  $("nav").setAttribute("aria-label", t("主导航"));
  $("logo").setAttribute("aria-label", t("FarSight 概览"));
  $("search").placeholder = t("筛选节点或服务");
  $("search").setAttribute("aria-label", t("筛选节点或服务"));
  $("add").replaceChildren(icon("plus"), t("添加节点"));
  $("rail").replaceChildren(...RAIL.map((item) => {
    const link = h("a", { class: "rail-btn", href: item.hash, title: t(item.label), "aria-label": t(item.label) }, icon(item.icon));
    link._item = item;
    return link;
  }));
  applyTheme();
}

function changeLang(next) {
  if (next === getLang()) return;
  setLang(next);
  applyLang();
  if (store.route) swap(store.route);            // every view builds its text when it is created
  refresh();                                     // messages from the server come in the requested language
}
$("lang").addEventListener("click", () => changeLang(getLang() === "zh" ? "en" : "zh"));

// ---- chrome
function chrome() {
  const state = store.state;
  const view = store.view;
  if (!view) return;
  const title = view.title(state);
  setText($("title"), title);
  document.title = `${title} · FarSight`;
  const crumb = state ? view.crumb(state) : [];
  const nodes = [];
  crumb.forEach((part, index) => {
    if (index) nodes.push(h("span", { text: "›" }));
    nodes.push(typeof part === "string" ? h("span", { text: part }) : h("a", { href: part.href, text: part.text }));
  });
  if (state && state.demo) nodes.push(h("span", { text: "· " + t("演示数据") }));
  $("crumb").replaceChildren(...nodes);
  for (const link of $("rail").children) {
    const active = link._item.match(store.route);
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  const count = state ? state.summary.crit + state.summary.warn : 0;
  const bell = $("bell");
  let badge = bell.querySelector(".badge");
  if (count && !badge) { badge = h("span", { class: "badge" }); bell.append(badge); }
  if (badge) { badge.hidden = !count; setText(badge, String(count)); }
  const label = count ? t("告警：{n} 条", { n: count }) : t("告警");
  bell.setAttribute("aria-label", label);
  bell.title = label;
  $("search").parentElement.hidden = !["overview", "nodes", "services"].includes(store.route.name);
}

// ---- navigation
function create(route) {
  if (route.name === "node") return nodeView(ctx, route.id);
  if (route.name === "service") return serviceView(ctx, route.id);
  if (route.name === "nodes") return nodesPage(ctx);
  if (route.name === "services") return servicesPage(ctx);
  if (route.name === "activity") return activityPage(ctx);
  if (route.name === "settings") return settingsPage(ctx);
  return overview(ctx, route);
}

function swap(route) {
  hideTip();
  if (store.view && store.view.unmount) store.view.unmount();
  const view = create(route);
  store.view = view;
  store.route = route;
  root.replaceChildren(view.el);
  if (view.mount) view.mount();
  if (store.state) view.update(store.state);
  chrome();
  window.scrollTo({ top: 0 });
}

const isDetail = (route) => route.name === "node" || route.name === "service";

function navigate() {
  const route = parse(location.hash);
  const previous = store.route;
  const source = store.source;
  store.source = null;
  if (!isDetail(route)) store.lastList = location.hash || "#/";
  if (previous && previous.name === route.name && previous.id === route.id) {
    store.route = route;                         // same view, another part of it (overview: nodes / topology)
    const apply = () => { if (store.view.route) store.view.route(route); chrome(); };
    if (store.view.route && document.startViewTransition && !reducedMotion()) document.startViewTransition(apply); else apply();
    return;
  }
  if (!previous || !document.startViewTransition || reducedMotion()) { swap(route); return; }
  // Shared-element morph. Exactly one element is named "hero" on each side of the transition:
  // the clicked card and the detail's identity panel (which carries the name through its CSS class).
  if (isDetail(route) && source) source.style.viewTransitionName = "hero";
  let target = null;
  const transition = document.startViewTransition(() => {
    if (source) source.style.viewTransitionName = "";
    swap(route);
    if (isDetail(previous) && !isDetail(route)) {
      target = root.querySelector(`[data-key="${CSS.escape(previous.id)}"]`);
      if (target) { target.classList.remove("enter"); target.style.viewTransitionName = "hero"; }
    }
  });
  transition.finished.finally(() => { if (target) target.style.viewTransitionName = ""; });
}

// ---- polling
function banner(text) {
  const el = $("banner");
  el.hidden = !text;
  setText(el, text || "");
}

async function refresh() {
  try {
    store.state = await api("/api/state");
    store.failures = 0;
    banner("");
    if (store.view) { store.view.update(store.state); chrome(); }
  } catch (failure) {
    store.failures += 1;
    if (store.failures >= 2) banner(t("与面板服务的连接中断，页面上是最后一次取得的数据，正在重试…"));
  }
}

async function tick() {
  await refresh();
  const interval = store.meta ? Math.max(1000, Math.min(5000, store.meta.interval * 1000)) : 2000;
  setTimeout(tick, document.hidden ? Math.max(interval, 5000) : interval);
}

$("add").addEventListener("click", () => ctx.addNode());
$("bell").addEventListener("click", () => ctx.go("#/activity"));
$("search-icon").replaceChildren(icon("search"));
$("bell").replaceChildren(icon("bell"));
$("search").addEventListener("input", () => { if (store.view && store.state) store.view.update(store.state); });
document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if ((event.key === "/" && !typing) || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
    if ($("search").parentElement.hidden) return;
    event.preventDefault();
    $("search").focus();
    $("search").select();
  } else if (event.key === "Escape" && document.activeElement === $("search")) {
    $("search").value = "";
    $("search").dispatchEvent(new Event("input"));
    $("search").blur();
  }
});
window.addEventListener("hashchange", navigate);

(async function start() {
  applyLang();
  store.route = null;
  try { store.meta = await api("/api/meta"); } catch (failure) { /* the state poll reports connection problems */ }
  swap(parse(location.hash));
  tick();
})();
