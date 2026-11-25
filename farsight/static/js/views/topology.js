// Communication topology. Cluster level: servers as boxes, one line per pair of servers that talk, labelled with
// the protocols in use. Server level (click a server): its components with their processes, the channels among
// them and to other servers. Lines are drawn from the measured positions of the boxes, which CSS lays out in
// columns that follow the request path. Every channel is also listed in the table under the drawing.
import { h, s, icon, setText, sync, chip, api, NODE_STATUS, ROLE, roleName, hoverTip } from "../util.js";
import { t, N } from "../i18n.js";

const EVIDENCE = {
  observed: { text: N("实测"), tone: "good", hint: N("在节点的 socket 表里看到了已建立的 TCP 连接") },
  declared: { text: N("配置"), tone: "info", hint: N("来自服务的配置或注册信息；该节点没有可用的 socket 表") },
  inferred: { text: N("推断"), tone: "warn", hint: N("RDMA 不经过内核 socket，无法实测；根据引擎的传输设置得出") },
};
const PLANES = { request: N("请求"), kv: N("KV 传输"), control: N("控制"), internal: N("内部"), monitor: N("监控"), other: N("其他") };

const unique = (values) => Array.from(new Set(values));
const portsText = (channel) => {
  if (!channel.ports.length) return "—";
  const more = channel.port_count - channel.ports.length;
  return channel.ports.map((port) => ":" + port).join(" ") + (more > 0 ? ` +${more}` : "");
};

/** Boxes in columns plus the lines between them. `render({ columns, edges })`; an edge is { id, a, b, channels }. */
function diagram(onSelect) {
  const lines = s("svg", { class: "topo-lines", "aria-hidden": "true" });
  const columns = h("div", { class: "topo-cols" });
  const labels = h("div", { class: "topo-labels" });
  const el = h("div", { class: "topo" }, lines, columns, labels);
  let edges = [];
  let inner = "right";
  let selected = null;
  let frame = 0;
  const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); };
  const observer = new ResizeObserver(schedule);
  observer.observe(el);
  observer.observe(columns);

  function render(model, selection) {
    selected = selection;
    sync(columns, model.columns, (column) => column.key, () => {
      const head = h("div", { class: "topo-col-title" });
      const list = h("div", { class: "topo-col-items" });
      const column = h("div", { class: "topo-col" }, head, list);
      column._update = (item) => {
        setText(head, item.title || "");
        sync(list, item.boxes, (box) => box.id, (box) => box.make());
      };
      return column;
    });
    edges = model.edges;
    inner = model.inner || "right";
    schedule();
  }

  function draw() {
    const base = el.getBoundingClientRect();
    if (!base.width) return;
    const boxes = new Map();
    Array.from(columns.children).forEach((column, index) => {
      for (const box of column.lastChild.children) {
        const rect = box.getBoundingClientRect();
        boxes.set(box.dataset.key, { column: index, left: rect.left - base.left, right: rect.right - base.left,
          top: rect.top - base.top, bottom: rect.bottom - base.top });
      }
    });
    // A line between columns leaves the left box on its right side and enters the right box on its left side; a
    // line inside one column runs along `inner` side of both boxes. Lines sharing a side are spread along it in
    // the order of where they go, so they do not cross right at the box.
    const plan = [];
    const sides = new Map();
    const attach = (key, entry) => { if (!sides.has(key)) sides.set(key, []); sides.get(key).push(entry); };
    for (const edge of edges) {
      let first = boxes.get(edge.a), second = boxes.get(edge.b);
      if (!first || !second) continue;
      let flipped = false;
      if (first.column > second.column || (first.column === second.column && first.top > second.top)) {
        [first, second] = [second, first];
        flipped = true;
      }
      const same = first.column === second.column;
      const protocols = unique(edge.channels.map((channel) => channel.short || channel.proto));
      const item = { edge, first, second, same, span: second.column - first.column,
        text: protocols.slice(0, 3).join(" · ") + (protocols.length > 3 ? ` +${protocols.length - 3}` : "") };
      plan.push(item);
      attach(`${flipped ? edge.b : edge.a}:${same ? inner : "right"}`, { item, end: "first", other: (second.top + second.bottom) / 2 });
      attach(`${flipped ? edge.a : edge.b}:${same ? inner : "left"}`, { item, end: "second", other: (first.top + first.bottom) / 2 });
    }
    for (const [key, entries] of sides) {
      const box = boxes.get(key.slice(0, key.lastIndexOf(":")));
      entries.sort((one, two) => one.other - two.other);
      entries.forEach((entry, index) => { entry.item[entry.end + "Y"] = box.top + (box.bottom - box.top) * (index + 1) / (entries.length + 1); });
    }
    const depths = new Map();
    for (const item of plan) {
      const y1 = item.firstY, y2 = item.secondY;
      if (item.same) {
        const depth = depths.get(item.first.column) || 0;
        depths.set(item.first.column, depth + 1);
        const reach = (34 + 22 * depth) * (inner === "left" ? -1 : 1);
        const x1 = inner === "left" ? item.first.left : item.first.right, x2 = inner === "left" ? item.second.left : item.second.right;
        const edgeX = inner === "left" ? Math.min(x1, x2) : Math.max(x1, x2);
        item.path = `M${x1} ${y1} C${edgeX + reach} ${y1} ${edgeX + reach} ${y2} ${x2} ${y2}`;
        item.side = inner === "left" ? "end" : "start";
        item.spots = [0, -26, 26, -52, 52].map((shift) => [edgeX + reach * 0.75, (y1 + y2) / 2 + shift]);
        continue;
      }
      const x1 = item.first.right, x2 = item.second.left;
      const reach = (x2 - x1) / 2;
      const curve = (u) => [((1 - u) ** 3) * x1 + 3 * ((1 - u) ** 2) * u * (x1 + reach) + 3 * (1 - u) * (u ** 2) * (x2 - reach) + (u ** 3) * x2,
        ((1 - u) ** 3) * y1 + 3 * ((1 - u) ** 2) * u * y1 + 3 * (1 - u) * (u ** 2) * y2 + (u ** 3) * y2];
      item.path = `M${x1} ${y1} C${x1 + reach} ${y1} ${x2 - reach} ${y2} ${x2} ${y2}`;
      if (item.span === 1) {
        item.spots = [0.5, 0.4, 0.6, 0.32, 0.68, 0.25, 0.75].map(curve);
      } else {
        // The line passes behind the boxes of the columns in between; its label sits in the first gap.
        let low = 0, high = 0.5;
        const target = x1 + 62;
        for (let step = 0; step < 18; step += 1) { const middle = (low + high) / 2; if (curve(middle)[0] < target) low = middle; else high = middle; }
        item.spots = [low, low * 0.75, low * 1.25].map(curve);
      }
    }
    // A label takes the first of its spots along the line that is clear of the labels placed before it.
    const taken = [];
    for (const item of plan) {
      const width = Math.min(200, 22 + 6.3 * item.text.length);
      const centre = (spot) => spot[0] + (item.side === "start" ? width / 2 : item.side === "end" ? -width / 2 : 0);
      const clear = (spot) => !taken.some((other) => Math.abs(other.x - centre(spot)) < (other.width + width) / 2 + 6 && Math.abs(other.y - spot[1]) < 26);
      item.at = item.spots.find(clear) || item.spots[0];
      taken.push({ x: centre(item.at), y: item.at[1], width });
    }
    lines.setAttribute("width", base.width);
    lines.setAttribute("height", base.height);
    lines.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`);
    sync(lines, plan, (item) => item.edge.id, () => {
      const path = s("path");
      path._update = (item) => {
        path.setAttribute("d", item.path);
        const solid = item.edge.channels.some((channel) => channel.evidence === "observed");
        path.classList.toggle("soft", !solid);
        path.classList.toggle("sel", selected === item.edge.id);
        path.classList.toggle("dim", selected !== null && selected !== item.edge.id);
      };
      return path;
    });
    sync(labels, plan, (item) => item.edge.id, (item) => {
      const text = h("span");
      const label = h("button", { class: "topo-label", type: "button" }, text);
      let latest = item;
      label.addEventListener("click", (event) => { event.stopPropagation(); onSelect(selected === latest.edge.id ? null : latest.edge); });
      const lift = (on) => { const path = lines.querySelector(`[data-key="${CSS.escape(latest.edge.id)}"]`); if (path) path.classList.toggle("hot", on); };
      label.addEventListener("pointerenter", () => lift(true));
      label.addEventListener("pointerleave", () => lift(false));
      label.addEventListener("focus", () => lift(true));
      label.addEventListener("blur", () => lift(false));
      label._update = (next) => {
        latest = next;
        setText(text, next.text);
        label.style.left = next.at[0] + "px";
        label.style.top = next.at[1] + "px";
        label.dataset.side = next.side || "center";
        label.classList.toggle("sel", selected === next.edge.id);
        label.classList.toggle("dim", selected !== null && selected !== next.edge.id);
        label.setAttribute("aria-pressed", String(selected === next.edge.id));
        label.setAttribute("aria-label", t("{a} 与 {b} 之间：{list}。点击查看通道明细。",
          { a: next.edge.nameA, b: next.edge.nameB, list: unique(next.edge.channels.map((channel) => channel.proto)).join(", ") }));
      };
      return label;
    });
  }
  el.addEventListener("click", () => { if (selected !== null) onSelect(null); });
  return { el, render, dispose: () => { observer.disconnect(); cancelAnimationFrame(frame); } };
}

function roleChips(roles) {
  return unique(roles).map((role) => chip(roleName(role), ["client", "other"].includes(role) ? null : role));
}

// ------------------------------------------------------------------------------------------- cluster level
function clusterModel(ctx, data) {
  const linked = new Set();
  const acting = new Map();          // node id -> roles its processes play in the channels (for nodes without a service)
  for (const edge of data.edges) {
    linked.add(edge.a); linked.add(edge.b);
    for (const channel of edge.channels) {
      for (const [end, roles] of [[channel.from, channel.from_roles], [channel.to, channel.to_roles]]) {
        if (!acting.has(end)) acting.set(end, new Set());
        roles.forEach((role) => acting.get(end).add(role));
      }
    }
  }
  const tier = (roles) => roles.some((role) => role === "frontend" || role === "router") ? 1
    : roles.includes("prefill") ? 2 : roles.some((role) => role === "decode" || role === "worker") ? 3
      : roles.includes("server") ? 1 : roles.includes("client") ? 0 : roles.includes("etcd") ? 3 : 4;
  const columns = [
    { key: "c0", title: t("请求来源"), boxes: [] }, { key: "c1", title: t("入口"), boxes: [] }, { key: "c2", title: "Prefill", boxes: [] },
    { key: "c3", title: "Decode / Worker", boxes: [] }, { key: "c4", title: t("其他"), boxes: [] },
  ];
  const names = {};
  const idle = [];
  for (const node of data.nodes) {
    names[node.id] = node.name;
    const roles = node.roles.map((entry) => entry.role);
    if (!roles.length && !linked.has(node.id)) { idle.push(node); continue; }
    const others = roles.length ? [] : Array.from(acting.get(node.id) || []).filter((role) => role === "client" || role === "other");
    columns[roles.length ? tier(roles) : 0].boxes.push({ id: node.id, node, others, make: () => serverBox(ctx) });
  }
  for (const external of data.externals) {
    names[external.id] = external.host;
    columns[tier(external.roles)].boxes.push({ id: external.id, external, make: () => externalBox(ctx) });
  }
  const edges = data.edges.map((edge) => Object.assign({}, edge, { nameA: names[edge.a] || edge.a, nameB: names[edge.b] || edge.b }));
  return { columns: columns.filter((column) => column.boxes.length), edges, names, idle };
}

function serverBox(ctx) {
  const dot = h("i", { class: "dot" });
  const name = h("b");
  const host = h("span", { class: "topo-sub" });
  const roles = h("div", { class: "topo-roles" });
  const others = h("div", { class: "topo-roles" });
  const foot = h("span", { class: "topo-foot" });
  const el = h("button", { class: "topo-box server", type: "button" }, h("div", { class: "topo-head" }, dot, name), host, roles, others, foot);
  let node = null;
  el.addEventListener("click", (event) => { event.stopPropagation(); if (node) ctx.go(`#/topology/${node.id}`); });
  el._update = (spec) => {
    node = spec.node;
    const info = NODE_STATUS[node.status] || NODE_STATUS.stopped;
    dot.dataset.tone = info.tone;
    setText(name, node.name);
    setText(host, node.host + (node.gpu_count ? ` · ${node.gpu_model || "GPU"} × ${node.gpu_count}` : ""));
    const groups = new Map();
    for (const entry of node.roles) {
      if (!groups.has(entry.service)) groups.set(entry.service, { key: entry.service, name: entry.service_name, roles: [] });
      groups.get(entry.service).roles.push(entry.role);
    }
    sync(roles, Array.from(groups.values()), (group) => group.key, () => {
      const label = h("small");
      const chips = h("div");
      const row = h("div", { class: "topo-service" }, label, chips);
      row._update = (group) => { setText(label, group.name); chips.replaceChildren(...roleChips(group.roles)); };
      return row;
    });
    others.replaceChildren(...roleChips(spec.others || []));
    setText(foot, node.status !== "online" ? t(info.text)
      : !node.observed && node.roles.length ? t("没有 socket 数据")
        : node.inside ? t("内部 {n} 条通道 · 点击查看", { n: node.inside }) : t("点击查看内部"));
    el.title = !node.observed && node.status === "online" && node.socket_error
      ? t("读不到这台节点的 socket 表（{reason}），连线来自配置", { reason: node.socket_error }) : "";
    el.setAttribute("aria-label", t("{name}，查看服务器内部的通信拓扑", { name: node.name }));
  };
  return el;
}

function externalBox(ctx) {
  const glyph = h("span", { class: "topo-glyph" });
  const name = h("b");
  const sub = h("span", { class: "topo-sub" });
  const roles = h("div", { class: "topo-roles" });
  const action = h("div");
  const el = h("div", { class: "topo-box external" }, h("div", { class: "topo-head" }, glyph, name), sub, roles, action);
  el._update = (spec) => {
    const external = spec.external;
    const client = external.roles.includes("client") && external.roles.length === 1;
    glyph.replaceChildren(icon(client ? "user" : "server"));
    setText(name, external.host);
    setText(sub, client ? t("发起推理请求的主机") : external.unmonitored ? t("服务的一部分，但没有加入面板") : t("未加入面板的主机"));
    roles.replaceChildren(...roleChips(external.roles.filter((role) => role !== "client")));
    if (external.unmonitored && !action.childElementCount) {
      action.append(h("button", { class: "btn small", type: "button", onclick: (event) => { event.stopPropagation(); ctx.addNode({ hosts: external.host }); } },
        icon("plus"), t("添加此节点")));
    }
  };
  return el;
}

// ------------------------------------------------------------------------------------------- server level
function serverModel(ctx, data) {
  const names = { local: t("本机其他进程") };
  const parts = [], left = [], right = [];
  for (const component of data.components) {
    names[component.key] = component.container || `${ROLE[component.role] || component.role} · pid ${component.pid}`;
    parts.push({ id: component.key, component, make: () => componentBox(ctx) });
  }
  if (data.local) left.push({ id: "local", peer: { id: "local", kind: "local", name: names.local, roles: [] }, make: () => peerBox(ctx) });
  for (const peer of data.peers) {
    names[peer.id] = peer.name;
    (peer.outbound ? right : left).push({ id: peer.id, peer, make: () => peerBox(ctx) });
  }
  const columns = [
    { key: "in", title: t("连入"), boxes: left }, { key: "parts", title: t("这台服务器上的组件"), boxes: parts },
    { key: "out", title: t("其他服务器"), boxes: right },
  ].filter((column) => column.boxes.length);
  const edges = data.edges.map((edge) => Object.assign({}, edge, { nameA: names[edge.a] || edge.a, nameB: names[edge.b] || edge.b }));
  // Lines between two components of this server run on the left of their column, clear of the lines to other servers.
  return { columns, edges, names, idle: [], inner: "left" };
}

function componentBox(ctx) {
  const role = h("span");
  const name = h("b");
  const sub = h("span", { class: "topo-sub" });
  const facts = h("span", { class: "topo-sub" });
  const procs = h("div", { class: "topo-procs" });
  const innerTitle = h("small", { class: "topo-inner-title" });
  const inner = h("div", { class: "topo-inner" });
  const el = h("div", { class: "topo-box component" }, h("div", { class: "topo-head" }, role, name), sub, facts, procs, innerTitle, inner);
  el._update = (spec) => {
    const component = spec.component;
    role.replaceChildren(chip(ROLE[component.role] || component.role, component.role));
    setText(name, component.container || `pid ${component.pid}`);
    name.title = component.container || "";
    setText(sub, [component.service_name, `pid ${component.pid}`].filter(Boolean).join(" · "));
    const settings = component.settings || {};
    const bits = [];
    if (settings.tp) bits.push(`TP${settings.tp}`);
    if (settings.dp) bits.push(`DP${settings.dp}`);
    if (settings.transfer) bits.push(settings.transfer);
    for (const [label, port] of Object.entries(component.ports || {})) bits.push(`${label} :${port}`);
    setText(facts, bits.join(" · "));
    facts.hidden = !bits.length;
    sync(procs, component.procs, (group) => group.comm, () => {
      const label = h("span", { class: "mono" });
      const count = h("span");
      const gpus = h("span", { class: "topo-gpus" });
      const row = h("div", { class: "topo-proc" }, label, count, gpus);
      row._update = (group) => {
        setText(label, group.comm);
        setText(count, "×" + group.n);
        setText(gpus, group.gpus.length ? "GPU " + group.gpus.join(" ") : "");
      };
      return row;
    });
    const channels = component.inner || [];
    innerTitle.hidden = inner.hidden = !channels.length;
    setText(innerTitle, t("进程之间"));
    sync(inner, channels.slice(0, 8), (channel) => `${channel.kind}|${channel.from}|${channel.to}`, () => {
      const path = h("span", { class: "mono" });
      const what = h("span");
      const row = h("div", { class: "topo-inner-row" }, path, what);
      row._update = (channel) => {
        setText(path, `${channel.from || "?"} → ${channel.to || "?"}`);
        setText(what, `${channel.proto} · ${channel.name}`);
        row.title = `${portsText(channel)} · ${t("{n} 条连接", { n: channel.n })}`;
      };
      return row;
    });
  };
  return el;
}

function peerBox(ctx) {
  const glyph = h("span", { class: "topo-glyph" });
  const name = h("b");
  const sub = h("span", { class: "topo-sub" });
  const roles = h("div", { class: "topo-roles" });
  const action = h("div");
  const el = h("div", { class: "topo-box external" }, h("div", { class: "topo-head" }, glyph, name), sub, roles, action);
  let peer = null;
  el.addEventListener("click", (event) => { if (peer && peer.kind === "node" && !event.target.closest("button")) { event.stopPropagation(); ctx.go(`#/topology/${peer.id}`); } });
  el._update = (spec) => {
    peer = spec.peer;
    const client = peer.roles.includes("client") && peer.roles.length === 1;
    el.classList.toggle("link", peer.kind === "node");
    el.classList.toggle("server", peer.kind === "node");
    el.classList.toggle("external", peer.kind !== "node");
    glyph.replaceChildren(icon(peer.kind === "local" ? "cube" : client ? "user" : "server"));
    setText(name, peer.name);
    setText(sub, peer.kind === "local" ? t("不属于任何服务的本机进程") : peer.kind === "node" ? `${peer.host} · ${t("点击查看这台服务器")}`
      : client ? t("发起推理请求的主机") : peer.unmonitored ? t("服务的一部分，但没有加入面板") : t("未加入面板的主机"));
    roles.replaceChildren(...roleChips(peer.roles.filter((item) => item !== "client" || peer.kind === "node")));
    if (peer.unmonitored && !action.childElementCount) {
      action.append(h("button", { class: "btn small", type: "button", onclick: (event) => { event.stopPropagation(); ctx.addNode({ hosts: peer.host }); } },
        icon("plus"), t("添加此节点")));
    }
  };
  return el;
}

// ------------------------------------------------------------------------------------------- the panel
export function topologyPanel(ctx) {
  let nodeId = null;
  let data = null;
  let model = null;
  let selected = null;
  let timer = null;
  let active = false;
  let failure = null;
  const back = h("button", { class: "btn small", type: "button", onclick: () => ctx.go("#/topology") }, icon("back"), t("集群拓扑"));
  const where = h("b");
  const detail = h("button", { class: "btn small ghost", type: "button", onclick: () => nodeId && ctx.go(`#/node/${nodeId}`) }, icon("server"), t("节点详情"));
  const crumb = h("div", { class: "topo-crumb" }, back, where, detail);
  const collect = h("div", { class: "topo-collect" });
  const graph = diagram((edge) => { selected = edge ? edge.id : null; paint(); });
  const scroll = h("div", { class: "topo-scroll" }, graph.el);
  const idle = h("div", { class: "topo-idle" });
  const legend = h("div", { class: "topo-legend" },
    h("span", null, h("i", { class: "line" }), t("实测到的连接")), h("span", null, h("i", { class: "line soft" }), t("仅来自配置或推断")),
    h("span", { class: "muted", text: t("点击连线上的协议看通道明细，点击服务器看它内部的通信") }));
  const tableTitle = h("h3");
  const clear = h("button", { class: "btn small ghost", type: "button", hidden: true, onclick: () => { selected = null; paint(); } }, icon("back"), t("全部连接"));
  const summaryBody = h("tbody");
  const summary = h("table", { class: "plain channels" },
    h("thead", null, h("tr", null, [t("两端"), t("协议"), t("通道数")].map((label, index) => h("th", { class: index === 2 ? "r" : null, text: label })), h("th"))),
    summaryBody);
  const body = h("tbody");
  const channels = h("table", { class: "plain channels" },
    h("thead", null, h("tr", null, [t("通道"), t("协议"), t("方向"), t("端口"), t("连接数"), t("依据")].map((label, index) =>
      h("th", { class: index === 4 ? "r" : null, text: label })))), body);
  const table = h("div", { class: "topo-table" }, h("div", { class: "section-title" }, tableTitle, clear), h("div", { class: "table-wrap" }, summary, channels));
  const empty = h("div", { class: "empty", hidden: true });
  const el = h("div", { class: "topo-panel" }, crumb, collect, scroll, legend, idle, table, empty);

  function paint() {
    crumb.hidden = !nodeId;
    if (!data) {
      scroll.hidden = legend.hidden = idle.hidden = table.hidden = collect.hidden = true;
      empty.hidden = false;
      empty.replaceChildren(h("b", { text: failure ? t("读不到拓扑数据") : t("正在读取拓扑…") }), failure || "");
      return;
    }
    model = nodeId ? serverModel(ctx, data) : clusterModel(ctx, data);
    if (selected && !model.edges.some((edge) => edge.id === selected)) selected = null;
    const boxes = model.columns.reduce((sum, column) => sum + column.boxes.length, 0);
    empty.hidden = boxes > 0;
    scroll.hidden = legend.hidden = table.hidden = !boxes;
    if (!boxes) {
      empty.replaceChildren(h("b", { text: nodeId ? t("这台服务器上没有 Dynamo / SGLang 组件") : t("还没有可画的拓扑") }),
        nodeId ? t("服务启动后，这里会画出它的组件和它们之间的通信。") : t("节点上发现 Dynamo 或 SGLang 服务后，这里会画出服务器之间的通信。"));
    }
    if (nodeId) {
      setText(where, `${data.node.name} · ${data.node.host}`);
      collect.hidden = data.observed || !boxes;
      setText(collect, data.observed ? "" : t("读不到这台服务器的 socket 表{reason}，下面只有配置里声明的通道。采集器需要 root 权限和 ss 命令。",
        { reason: data.socket_error ? `（${data.socket_error}）` : "" }));
    } else {
      const online = data.nodes.filter((node) => node.status === "online").length;
      collect.hidden = false;
      collect.replaceChildren(icon("monitor"), h("span", null, h("b", { text: t("面板 {name}", { name: data.dashboard.name }) }),
        " ", t("经 SSH 只读采集 {total} 台服务器（{online} 台在线）；它不出现在下面的图里。", { total: data.nodes.length, online })));
    }
    graph.render(model, selected);
    idle.hidden = !model.idle.length;
    if (model.idle.length) {
      const label = h("span", { class: "muted", text: t("没有参与服务通信的节点") });
      idle.replaceChildren(label, ...model.idle.map((node) => {
        const info = NODE_STATUS[node.status] || NODE_STATUS.stopped;
        const tag = h("button", { class: "chip", type: "button", onclick: () => ctx.go(`#/node/${node.id}`) }, h("i", { class: "dot", data: { tone: info.tone } }), node.name);
        tag.title = `${node.host} · ${t(info.text)}`;
        return tag;
      }));
    }
    const chosen = selected ? model.edges.find((edge) => edge.id === selected) : null;
    clear.hidden = !chosen;
    summary.hidden = Boolean(chosen);
    channels.hidden = !chosen;
    if (!chosen) {
      // One row per pair of ends; the channels of a pair open with a click on the row or on the line's label.
      setText(tableTitle, t("全部连接（{n}）", { n: model.edges.length }));
      sync(summaryBody, model.edges.length ? model.edges : [{ id: "\u0000none" }], (edge) => edge.id, (edge) => {
        if (edge.id === "\u0000none") return h("tr", null, h("td", { colspan: "4", class: "muted", text: t("没有观察到通道") }));
        const ends = h("td"), protocols = h("td"), count = h("td", { class: "r num" });
        const tr = h("tr", { class: "click", tabindex: "0" }, ends, protocols, count, h("td", { class: "r" }, h("span", { class: "chev" }, icon("chevron"))));
        let latest = edge;
        const open = () => { selected = latest.id; paint(); };
        tr.addEventListener("click", open);
        tr.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
        tr._update = (item) => {
          latest = item;
          setText(ends, `${item.nameA} ⇄ ${item.nameB}`);
          protocols.replaceChildren(...unique(item.channels.map((channel) => channel.short || channel.proto)).map((name) => h("span", { class: "chip", text: name })));
          setText(count, String(item.channels.length));
        };
        return tr;
      });
      return;
    }
    setText(tableTitle, t("{a} 与 {b} 之间的通道", { a: chosen.nameA, b: chosen.nameB }));
    const rows = chosen.channels.map((channel) => ({ key: `${channel.kind}|${channel.from}|${channel.to}`, channel }));
    sync(body, rows, (row) => row.key, () => {
      const what = h("td"), protocol = h("td"), way = h("td"), ports = h("td", { class: "mono" }), count = h("td", { class: "r num" }), proof = h("td");
      const tr = h("tr", null, what, protocol, way, ports, count, proof);
      tr._update = (item) => {
        const channel = item.channel;
        what.replaceChildren(channel.name, h("div", { class: "muted", text: t(PLANES[channel.plane] || PLANES.other) }));
        protocol.replaceChildren(h("span", { class: "chip", text: channel.proto }));
        const from = channel.from_roles.map(roleName).join(" / ") || (model.names[channel.from] || channel.from);
        const to = channel.to_roles.map(roleName).join(" / ") || (model.names[channel.to] || channel.to);
        way.replaceChildren(`${from} → ${to}`, h("div", { class: "muted", text: `${model.names[channel.from] || channel.from} → ${model.names[channel.to] || channel.to}` }));
        setText(ports, portsText(channel));
        setText(count, channel.evidence === "observed" ? String(channel.n) : "—");
        const evidence = EVIDENCE[channel.evidence] || EVIDENCE.observed;
        const badge = h("span", { class: "pill", data: { tone: evidence.tone } }, h("i"), h("span", { text: t(evidence.text) }));
        badge.title = t(evidence.hint);
        proof.replaceChildren(badge);
      };
      return tr;
    });
  }

  async function poll() {
    if (!active) return;
    const target = nodeId;
    try {
      const next = await api(target ? `/api/nodes/${target}/topology` : "/api/topology");
      if (!active || target !== nodeId) return;
      data = next;
      failure = null;
    } catch (error) {
      if (!active || target !== nodeId) return;
      if (error.status === 404 && target) { ctx.go("#/topology"); return; }
      failure = error.message;
    }
    paint();
    timer = setTimeout(poll, 4000);
  }

  return {
    el,
    title: () => (nodeId ? (data && data.node ? data.node.name : (ctx.nodeById(nodeId) || {}).name) || t("服务器") : null),
    show(route) {
      const next = route.node || null;
      if (active && next === nodeId) return;
      active = true;
      nodeId = next;
      data = null;
      selected = null;
      failure = null;
      clearTimeout(timer);
      paint();
      poll();
    },
    hide() { active = false; clearTimeout(timer); },
    dispose() { active = false; clearTimeout(timer); graph.dispose(); },
  };
}
