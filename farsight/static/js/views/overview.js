// Overview: one row of service cards, then the panel of compute nodes. Only the essentials are shown here;
// a click opens the detail view.
import { h, icon, setText, tween, meter, setMeter, fullness, sync, fmt, pill, setPill, chip, hoverTip,
  SERVICE_STATUS, NODE_STATUS, gpuModelLabel, api, toast } from "../util.js";
import { t } from "../i18n.js";
import { topologyPanel } from "./topology.js";

/** ["¥178/h", "¥0.65/M tok"]: what the service's GPUs cost per hour and what a million tokens cost right now. */
export function costParts(cost) {
  if (!cost || !cost.rent) return [];
  const rent = t("{amount}/小时", { amount: fmt.money(cost.rent, cost.currency) });
  return cost.blended == null ? [rent] : [rent, t("{amount}/百万 token", { amount: fmt.money(cost.blended, cost.currency) })];
}

export function serviceSubtitle(service) {
  const counts = service.counts || {};
  const parts = [];
  if (counts.prefill) parts.push(`${counts.prefill} Prefill`);
  if (counts.decode) parts.push(`${counts.decode} Decode`);
  if (counts.worker) parts.push(`${counts.worker} Worker`);
  if (counts.server) parts.push(`${counts.server} Server`);
  if (!parts.length && counts.frontend) parts.push("Frontend");
  const nodes = service.nodes.length + service.missing.filter((item) => item.reason === "unmonitored").length;
  parts.push(t("{n} 节点", { n: nodes }));
  return parts.join(" · ");
}

function serviceCard(ctx) {
  return (service) => {
    const status = pill(SERVICE_STATUS[service.status] || SERVICE_STATUS.unknown);
    const name = h("div", { class: "svc-name" });
    const sub = h("div", { class: "svc-sub" });
    const parts = h("div", { class: "svc-sub" });
    const left = h("span");
    const percent = h("b");
    const bar = meter();
    const glyph = h("div", { class: "svc-icon", data: { kind: service.kind } }, icon(service.kind === "sglang" ? "cube" : "bolt"));
    const money = h("div", { class: "svc-cost" });
    const el = h("button", { class: "card svc-card", type: "button" },
      h("div", { class: "svc-top" }, glyph, status), name, sub, parts, h("div", { class: "svc-foot" }, left, percent), bar, money);
    el.addEventListener("click", () => ctx.go(`#/service/${service.id}`, el));
    el._update = (item) => {
      el.dataset.status = item.status;
      setPill(status, SERVICE_STATUS[item.status] || SERVICE_STATUS.unknown);
      setText(name, item.name);
      name.title = item.model || item.name;
      setText(sub, item.model_short || (item.kind === "dynamo" ? "Dynamo" : "SGLang"));
      setText(parts, serviceSubtitle(item));
      const metrics = item.metrics || {};
      const bits = [];
      if (metrics.inflight != null) bits.push(t("进行中 {n}", { n: fmt.int(metrics.inflight) }));
      if (metrics.tps != null) bits.push(`${fmt.int(metrics.tps)} tok/s`);
      if (!bits.length) bits.push(item.gpu.count ? t("{n} 张 GPU", { n: item.gpu.count }) : "GPU");
      setText(left, bits.join(" · "));
      tween(percent, item.gpu.util, (value) => Math.round(value) + "%", true);
      setMeter(bar, item.gpu.util);
      const prices = costParts(item.cost);
      const line = prices.join(" · ");
      sync(money, prices.map((text, index) => ({ index, text })), (part) => part.index, () => {
        const part = h("span");
        part._update = (value) => setText(part, value.text);
        return part;
      });
      money.hidden = !prices.length;
      money.title = line ? t("GPU 租金与最近 1 分钟吞吐下每百万 token 的成本（含空闲）") : "";
      el.setAttribute("aria-label", t("{name}，{status}，GPU 平均利用率 {util}",
        { name: item.name, status: t((SERVICE_STATUS[item.status] || SERVICE_STATUS.unknown).text), util: fmt.pct(item.gpu.util) }) + (line ? "，" + line : ""));
    };
    return el;
  };
}

function miniMeter(label) {
  const value = h("b");
  const bar = meter();
  const el = h("div", { class: "mm" }, h("div", { class: "mm-head" }, h("span", { text: label }), value), bar);
  el.set = (percent, tone) => { tween(value, percent, (number) => Math.round(number) + "%"); setMeter(bar, percent, tone); };
  return el;
}

function nodeCard(ctx) {
  return (node) => {
    const dot = h("i", { class: "dot" });
    const name = h("span");
    const model = h("span", { class: "chip" });
    const sub = h("div", { class: "node-sub" });
    const body = h("div");
    const el = h("div", { class: "card node-card" },
      h("div", { class: "node-top" }, h("div", { class: "node-name" }, dot, name), model), sub, body);
    let mode = null;
    let parts = null;

    const open = () => ctx.go(`#/node/${node.id}`, el);
    el.addEventListener("click", (event) => { if (mode === "live" && !event.target.closest("button")) open(); });
    el.addEventListener("keydown", (event) => { if (mode === "live" && event.target === el && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } });

    function build(next) {
      mode = next;
      el.classList.toggle("live", mode === "live");
      if (mode === "live") { el.tabIndex = 0; el.setAttribute("role", "button"); } else { el.removeAttribute("tabindex"); el.removeAttribute("role"); }
      if (mode === "live") {
        parts = { cpu: miniMeter("CPU"), mem: miniMeter(t("内存")), gpu: miniMeter("GPU"), vram: miniMeter(t("显存")),
          cells: h("div", { class: "cells" }), foot: h("div", { class: "node-foot" }) };
        body.replaceChildren(h("div", { class: "node-meters" }, parts.cpu, parts.mem, parts.gpu, parts.vram), parts.cells, parts.foot);
      } else if (mode === "connecting") {
        parts = { text: h("div", { class: "node-sub", text: t("正在连接…") }) };
        body.replaceChildren(h("div", { class: "node-skel" }, h("i", { class: "skeleton", style: "width:82%" }), h("i", { class: "skeleton", style: "width:64%" }), h("i", { class: "skeleton", style: "width:74%" })));
      } else {
        parts = { title: h("b"), hint: h("p"), retry: h("div", { class: "retry" }), actions: h("div", { class: "node-actions" }) };
        body.replaceChildren(h("div", { class: "node-error" }, parts.title, parts.hint, parts.retry, parts.actions));
      }
    }

    el._update = (item) => {
      node = item;
      const info = NODE_STATUS[item.status] || NODE_STATUS.stopped;
      el.dataset.status = item.status;
      dot.dataset.tone = info.tone;
      if (info.pulse) dot.dataset.pulse = ""; else delete dot.dataset.pulse;
      dot.title = t(info.text);
      setText(name, item.name);
      setText(sub, (item.auth === "local" ? t("本机") : `${item.user}@${item.host}${item.port === 22 ? "" : ":" + item.port}`) + (item.label ? " · " + item.label : ""));
      model.hidden = item.status !== "online";
      setText(model, gpuModelLabel(item));
      const next = item.status === "online" ? "live" : item.status === "connecting" && !item.error ? "connecting" : "problem";
      if (next !== mode) build(next);
      if (mode === "live") {
        parts.cpu.set(item.cpu);
        parts.mem.set(item.mem, fullness(item.mem, 88, 95));
        parts.gpu.set(item.gpu);
        parts.vram.set(item.vram);
        parts.cells.hidden = !item.gpus.length;
        sync(parts.cells, item.gpus.map((gpu, index) => ({ index, util: gpu[0], vram: gpu[1] })), (gpu) => gpu.index, (gpu) => {
          const cell = h("i", { tabindex: "-1" });
          let latest = gpu;
          hoverTip(cell, () => [h("b", { text: `GPU ${latest.index}` }), h("span", { text: t("利用率 {util} · 显存 {vram}", { util: fmt.pct(latest.util), vram: fmt.pct(latest.vram) }) })]);
          cell._update = (value) => { latest = value; cell.style.setProperty("--v", value.util == null ? 0 : Math.round(value.util)); };
          return cell;
        });
        const services = item.services.map((id) => ctx.serviceById(id)).filter(Boolean);
        sync(parts.foot, services.length ? services : [{ id: "idle" }], (service) => service.id, (service) => {
          if (service.id === "idle") return h("span", { text: item.gpu_count ? t("没有发现 Dynamo / SGLang 服务") : "" });
          const tag = chip(service.name, service.kind === "sglang" ? "prefill" : "frontend", () => ctx.go(`#/service/${service.id}`));
          tag._update = (value) => { tag.lastChild.textContent = value.name; };
          return tag;
        });
        if (item.alerts) el.title = t("{n} 条告警", { n: item.alerts }); else el.removeAttribute("title");
      } else if (mode === "problem") {
        const error = item.error || { title: t(info.text), hint: "" };
        setText(parts.title, error.title);
        setText(parts.hint, error.hint);
        parts.hint.title = error.raw || "";
        const wait = item.next_retry ? Math.max(0, Math.round(item.next_retry - ctx.state.now)) : null;
        setText(parts.retry, item.status === "connecting" ? t("正在重试…") : wait != null ? t("{wait} 秒后自动重试（已尝试 {n} 次）", { wait, n: item.attempts }) : "");
        if (!parts.actions.childElementCount) {
          parts.actions.append(
            h("button", { class: "btn small", type: "button", onclick: async () => {
              if (node.status === "need_password") { ctx.editNode(node); return; }
              try { await api(`/api/nodes/${node.id}/retry`, { method: "POST", body: {} }); toast(t("正在重新连接 {host}", { host: node.host })); }
              catch (failure) { toast(failure.message); }
            } }, icon(item.status === "need_password" ? "key" : "refresh"), item.status === "need_password" ? t("输入密码") : t("重试")),
            h("button", { class: "btn small ghost", type: "button", onclick: () => ctx.editNode(node) }, icon("edit"), t("编辑")),
            h("button", { class: "btn small ghost danger", type: "button", "aria-label": t("移除节点"), onclick: () => ctx.removeNode(node) }, icon("trash")));
        }
      }
    };
    return el;
  };
}

export function overview(ctx, initial) {
  const services = h("div", { class: "svc-row" });
  const title = h("h2");
  const summary = h("p");
  let filter = "all";
  let route = initial || { tab: "nodes" };
  const seg = h("div", { class: "seg", role: "group", "aria-label": t("节点筛选") }, [["all", t("全部")], ["online", t("在线")], ["problem", t("异常")]].map(([key, label]) =>
    h("button", { type: "button", "aria-pressed": String(key === filter), text: label, onclick: (event) => {
      filter = key;
      for (const button of seg.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
      if (ctx.state) view.update(ctx.state);
    } })));
  const tabs = h("div", { class: "seg", role: "group", "aria-label": t("概览内容") },
    h("button", { type: "button", onclick: () => ctx.go("#/") }, icon("grid"), t("节点")),
    h("button", { type: "button", onclick: () => ctx.go("#/topology") }, icon("topology"), t("拓扑")));
  const grid = h("div", { class: "node-grid" });
  const empty = h("div", { class: "empty", hidden: true });
  const topology = topologyPanel(ctx);
  const nodesBody = h("div", null, grid, empty);
  const panel = h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", null, title, summary), h("div", { class: "panel-tools" }, seg, tabs)), nodesBody, topology.el);
  const el = h("div", null, services, panel);
  const makeService = serviceCard(ctx);
  const makeNode = nodeCard(ctx);

  function applyRoute() {
    const map = route.tab === "topology";
    tabs.children[0].setAttribute("aria-pressed", String(!map));
    tabs.children[1].setAttribute("aria-pressed", String(map));
    nodesBody.hidden = map;
    topology.el.hidden = !map;
    seg.hidden = map;
    setText(title, map ? t("通信拓扑") : t("计算节点"));
    if (map) topology.show(route); else topology.hide();
    if (ctx.state) view.update(ctx.state);
  }

  const view = {
    el,
    title: () => t("集群概览"),
    crumb: (state) => [t("{nodes} 个节点 · {gpus} 张 GPU · {services} 个服务",
      { nodes: state.summary.nodes, gpus: state.summary.gpus, services: state.summary.services })],
    route(next) { route = next; applyRoute(); },
    mount() { applyRoute(); },
    unmount() { topology.dispose(); },
    update(state) {
      const query = ctx.query();
      const match = (text) => !query || text.toLowerCase().includes(query);
      const shown = state.services.filter((service) => match([service.name, service.model, service.namespace, service.kind].join(" ")));
      if (state.services.length || !state.nodes.length) services.querySelector(".svc-empty")?.remove();
      sync(services, shown, (service) => service.id, makeService);
      if (!state.services.length && state.nodes.length && !services.querySelector(".svc-empty")) {
        services.append(h("div", { class: "card svc-empty", text: t("已连接的节点上还没有发现 Dynamo 或 SGLang 服务。服务启动后会自动出现在这里。") }));
      }
      const counts = state.summary;
      if (route.tab === "topology") {
        setText(summary, route.node ? t("一台服务器内部：组件、进程以及它们之间的通道") : t("服务器之间怎样通信、用什么协议"));
        return;
      }
      const problems = (node) => node.status === "error" || node.status === "need_password" || (node.status === "connecting" && Boolean(node.error));
      const nodes = state.nodes.filter((node) => match([node.name, node.host, node.label, node.gpu_model].join(" "))
        && (filter === "all" || (filter === "online" ? node.status === "online" : problems(node))));
      nodes.sort((a, b) => Number(problems(b)) - Number(problems(a)) || a.name.localeCompare(b.name, "zh-CN", { numeric: true }));
      sync(grid, nodes, (node) => node.id, makeNode);
      setText(summary, t("{n} 在线", { n: counts.online }) + (counts.connecting ? " · " + t("{n} 连接中", { n: counts.connecting }) : "")
        + (counts.failed ? " · " + t("{n} 失败", { n: counts.failed }) : ""));
      empty.hidden = nodes.length > 0;
      if (!nodes.length) {
        if (!state.nodes.length) {
          empty.replaceChildren(h("b", { text: t("还没有节点") }), t("添加节点的 IP 或主机名，面板会通过 SSH 连接并开始采集。"),
            h("div", null, h("button", { class: "btn", type: "button", onclick: () => ctx.addNode() }, icon("plus"), t("添加节点"))));
        } else {
          empty.replaceChildren(h("b", { text: t("没有符合条件的节点") }), t("调整筛选条件或搜索词。"));
        }
      }
    },
  };
  return view;
}
