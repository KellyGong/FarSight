// Node detail: identity and host facts, per-GPU load with the processes holding each card, trends and activity.
import { h, icon, setText, tween, meter, setMeter, fullness, sync, fmt, pill, setPill, chip, hoverTip, section, renderFeed,
  NODE_STATUS, SERVICE_STATUS, ROLE, LEVEL, gpuModelLabel, api, toast } from "../util.js";
import { t } from "../i18n.js";
import { trends, trend } from "../charts.js";

const percent = (value) => Math.round(value) + "%";

function statTile(label, unit) {
  const value = h("span");
  const el = h("div", { class: "tile" }, h("b", null, value, unit ? h("small", { text: unit }) : null), h("span", { text: label }));
  el.set = (number, format) => tween(value, number, format || ((x) => String(Math.round(x))), true);
  return el;
}

function barCell(label) {
  const value = h("b");
  const bar = meter();
  const el = h("div", { class: "bar-cell" }, h("div", { class: "mm-head" }, h("span", { text: label }), value), bar);
  el.set = (percentValue, text, tone) => { setText(value, text); setMeter(bar, percentValue, tone); };
  return el;
}

function gpuRow(ctx, nodeId) {
  return (gpu) => {
    const util = barCell(t("利用率"));
    const vram = barCell(t("显存"));
    const temp = h("div", { class: "gpu-temp" });
    const owner = h("div", { class: "gpu-owner" });
    const head = h("button", { class: "row-head gpu-head", type: "button", "aria-expanded": "false" },
      h("div", { class: "gpu-idx", text: String(gpu.index) }), util, vram, temp, h("span", { class: "chev" }, icon("chevron")), owner);
    const body = h("div", { class: "row-body", hidden: true });
    const el = h("div", { class: "row" }, head, body);
    let open = false;
    let latest = gpu;
    let charts = null;
    let timer = null;

    async function loadHistory() {
      if (!open) return;
      try {
        const history = await api(`/api/nodes/${nodeId}/gpus/${gpu.index}?window=900`);
        charts[0].update(history.t, history[`g${gpu.index}u`]);
        charts[1].update(history.t, (history[`g${gpu.index}m`] || []).map((value) => value == null ? null : value / 1024));
      } catch (failure) { /* the next poll retries */ }
      timer = setTimeout(loadHistory, 6000);
    }
    function renderBody() {
      const table = latest.procs.length ? h("div", { class: "scroll-x" }, h("table", { class: "plain" },
        h("thead", null, h("tr", null, h("th", { text: t("进程") }), h("th", { text: t("归属") }), h("th", { text: t("用户") }), h("th", { class: "r", text: t("显存") }))),
        h("tbody", null, latest.procs.map((process) => h("tr", null,
          h("td", null, process.name || "—", h("div", { class: "muted mono", text: `pid ${process.pid}` })),
          h("td", null, process.service
            ? chip(`${process.service_name} · ${ROLE[process.role] || process.role || ""}`, process.role, () => ctx.go(`#/service/${process.service}`))
            : (process.container || t("宿主机进程"))),
          h("td", { text: process.user || "—" }),
          h("td", { class: "r num", text: `${fmt.gib(process.mem)} GiB` }))))))
        : h("div", { class: "muted", style: "margin-top:10px", text: t("这张卡上没有计算进程。") });
      if (!charts) {
        charts = [trend({ label: t("利用率"), format: percent, max: 100 }), trend({ label: t("显存占用"), format: (value) => value.toFixed(1) + " GiB" })];
      }
      body.replaceChildren(
        h("dl", { class: "kv", style: "margin-top:12px" },
          h("dt", { text: t("型号") }), h("dd", { text: latest.name || "—" }),
          h("dt", { text: t("功耗") }), h("dd", { text: latest.power == null ? "—" : `${fmt.watts(latest.power)}${latest.power_limit ? " / " + fmt.watts(latest.power_limit) : ""}` }),
          h("dt", { text: t("温度") }), h("dd", { text: latest.temp == null ? "—" : `${Math.round(latest.temp)} °C` })),
        table, h("div", { class: "charts two", style: "margin-top:12px" }, charts));
    }
    head.addEventListener("click", () => {
      open = !open;
      el.classList.toggle("open", open);
      head.setAttribute("aria-expanded", String(open));
      body.hidden = !open;
      clearTimeout(timer);
      if (open) { renderBody(); loadHistory(); }
    });
    el._update = (item) => {
      latest = item;
      const used = item.mem_total ? 100 * (item.mem || 0) / item.mem_total : null;
      util.set(item.util, fmt.pct(item.util));
      vram.set(used, item.mem == null ? "—" : `${fmt.gib(item.mem)} / ${fmt.gib(item.mem_total)} GiB`);
      setText(temp, [item.temp == null ? null : `${Math.round(item.temp)} °C`, item.power == null ? null : fmt.watts(item.power)].filter(Boolean).join(" · "));
      const owners = new Map();
      for (const process of item.procs) {
        const key = process.service ? `s:${process.service}:${process.role}` : `c:${process.container || process.user || "host"}`;
        if (!owners.has(key)) owners.set(key, process);
      }
      sync(owner, Array.from(owners, ([key, process]) => ({ key, process })), (entry) => entry.key, (entry) => {
        const process = entry.process;
        return process.service
          ? chip(`${process.service_name} · ${ROLE[process.role] || process.role || ""}`, process.role, () => ctx.go(`#/service/${process.service}`))
          : chip(process.container || process.user || t("宿主机进程"));
      });
      owner.hidden = !owners.size;
      if (open) renderBody();
    };
    el._dispose = () => clearTimeout(timer);
    return el;
  };
}

export function nodeView(ctx, nodeId) {
  let detail = null;
  let timer = null;
  let dead = false;
  const status = pill(NODE_STATUS.connecting);
  const name = h("h2");
  const sub = h("p");
  const tiles = { cpu: statTile("CPU", "%"), mem: statTile(t("内存"), "%"), gpu: statTile("GPU", "%") };
  const facts = h("dl", { class: "kv" });
  const notes = h("div", { class: "rows" });
  const hero = h("aside", { class: "col panel hero" },
    h("div", { class: "hero-top" }, h("button", { class: "back", type: "button", "aria-label": t("返回概览"), onclick: () => ctx.back() }, icon("back")), status),
    h("div", { class: "ident" }, h("div", { class: "svc-icon big", data: { kind: "node" } }, icon("server")), name, sub),
    h("div", { class: "tiles" }, tiles.cpu, tiles.mem, tiles.gpu),
    notes, facts,
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "button", onclick: async () => {
        try { await api(`/api/nodes/${nodeId}/retry`, { method: "POST", body: {} }); toast(t("正在重新连接")); } catch (failure) { toast(failure.message); }
      } }, icon("refresh"), t("重连")),
      h("button", { class: "btn", type: "button", onclick: () => detail && ctx.editNode(detail.node) }, icon("edit"), t("编辑")),
      h("button", { class: "btn danger", type: "button", onclick: () => detail && ctx.removeNode(detail.node, () => ctx.back()) }, icon("trash"), t("移除"))));

  const gpuTitle = h("span");
  const gpuRows = h("div", { class: "rows" });
  const problem = h("div", { class: "box", hidden: true });
  const cores = h("div", { class: "cores" });
  const coreNote = h("span");
  const memBar = h("div", { class: "segbar" }, h("i", { style: "--c:var(--accent)" }), h("i", { style: "--c:var(--role-decode)" }), h("i", { style: "--c:var(--axis)" }));
  const memLegend = h("div", { class: "legend" });
  const disks = h("div", { class: "meter-list" });
  const netNote = h("span");
  const makeGpu = gpuRow(ctx, nodeId);
  const middle = h("div", { class: "col panel" },
    h("div", { class: "panel-head" }, h("div", null, h("h2", { text: "GPU" }), h("p", null, gpuTitle))),
    problem, gpuRows,
    section("CPU", coreNote, h("div", { class: "box" }, cores)),
    section(t("内存"), h("span", { class: "mem-total" }), h("div", { class: "box" }, memBar, memLegend)),
    section(t("磁盘与网络"), netNote, h("div", { class: "box" }, disks)));

  const charts = trends([
    { key: "cpu", label: "CPU", format: percent, max: 100 },
    { key: "mem", label: t("内存"), format: percent, max: 100 },
    { key: "gpu", label: t("GPU 平均利用率"), format: percent, max: 100 },
    { key: "vram", label: t("显存占用"), format: percent, max: 100 },
  ], () => { clearTimeout(timer); poll(); });
  const services = h("div", { class: "list" });
  const consumers = h("div", { class: "list" });
  const feed = h("div", { class: "feed" });
  const right = h("div", { class: "col" },
    h("section", { class: "panel" }, h("div", { class: "section-title" }, h("h3", { text: t("趋势") })), charts),
    h("section", { class: "panel section" },
      h("div", { class: "section-title" }, h("h3", { text: t("本节点上的服务") }),
        h("button", { class: "btn small ghost", type: "button", onclick: () => ctx.go(`#/topology/${nodeId}`) }, icon("topology"), t("内部通信拓扑"))),
      services,
      h("div", { class: "section-title", style: "margin-top:18px" }, h("h3", { text: t("占卡情况") }), h("span", { text: t("按显存") })), consumers),
    h("section", { class: "panel section" }, h("div", { class: "section-title" }, h("h3", { text: t("活动") })), feed));
  const el = h("div", { class: "detail" }, hero, middle, right);

  function render(now) {
    const node = detail.node;
    const info = NODE_STATUS[node.status] || NODE_STATUS.stopped;
    setPill(status, info);
    setText(name, node.name);
    setText(sub, (node.auth === "local" ? t("本机") : `${node.user}@${node.host}${node.port === 22 ? "" : ":" + node.port}`) + (node.label ? " · " + node.label : ""));
    tiles.cpu.set(node.cpu);
    tiles.mem.set(node.mem);
    tiles.gpu.set(node.gpu);
    const host = detail.host || {};
    const rows = [
      [t("系统"), host.os], [t("内核"), host.kernel], ["CPU", host.cpu_model ? `${host.cpu_model} · ` + t("{n} 线程", { n: host.cores }) : null],
      [t("内存"), host.mem_total ? `${fmt.gib(host.mem_total, 0)} GiB` : null],
      ["GPU", node.gpu_count ? `${gpuModelLabel(node)}${host.driver ? " · " + t("驱动 {version}", { version: host.driver }) : ""}` : (host.hostname ? t("未检测到（无 nvidia-smi）") : null)],
      [t("已运行"), host.boot_time ? fmt.dur(now - host.boot_time) : null],
      [t("负载"), detail.live && detail.live.load.length ? detail.live.load.map((value) => value.toFixed(1)).join(" / ") : null],
      [t("认证"), node.auth === "password" ? t("密码") : node.auth === "local" ? t("本机") : t("密钥")],
      [t("采集"), host.remote_user ? t("以 {user} 运行 · Python {python}", { user: host.remote_user, python: host.python }) : null],
      [t("已连接"), host.connected_at && node.status === "online" ? fmt.dur(now - host.connected_at) : null],
    ].filter(([, value]) => value);
    sync(facts, rows.flatMap(([label, value]) => [{ key: "t" + label, tag: "dt", text: label }, { key: "d" + label, tag: "dd", text: value }]),
      (item) => item.key, (item) => { const cell = h(item.tag); cell._update = (value) => setText(cell, value.text); return cell; });
    const alerts = detail.alerts.map((alert) => ({ key: alert.key, level: alert.level, title: alert.title, detail: alert.detail }));
    if (node.error) alerts.unshift({ key: "connection", level: "crit", title: node.error.title, detail: node.error.hint + (node.error.raw ? ` (${node.error.raw})` : "") });
    const docker = (detail.system || {}).docker;
    if (node.status === "online" && docker && docker !== "ok") {
      alerts.push({ key: "docker", level: "info", title: t("读不到容器信息"), detail: docker === "unavailable" ? t("节点上没有 docker 命令，服务归属只能按进程识别。") : t("当前用户无权访问 docker，服务归属只能按进程识别。") });
    }
    sync(notes, alerts, (alert) => alert.key, (alert) => {
      const title = h("b"), text = h("p");
      const note = h("div", { class: "note" }, icon((LEVEL[alert.level] || LEVEL.info).icon), h("div", null, title, text));
      note._update = (value) => { note.dataset.tone = (LEVEL[value.level] || LEVEL.info).tone; setText(title, value.title); setText(text, value.detail); };
      return note;
    });
    notes.hidden = !alerts.length;

    const online = node.status === "online";
    problem.hidden = online || detail.gpus.length > 0;
    if (!problem.hidden) problem.replaceChildren(h("div", { class: "muted", text: t("节点连接成功后，这里会显示每张 GPU 的负载。") }));
    const used = detail.gpus.reduce((sum, gpu) => sum + (gpu.mem || 0), 0);
    const total = detail.gpus.reduce((sum, gpu) => sum + (gpu.mem_total || 0), 0);
    setText(gpuTitle, detail.gpus.length
      ? `${gpuModelLabel(node)} · ` + (online ? t("显存 {used} / {total} GiB", { used: fmt.gib(used, 0), total: fmt.gib(total, 0) }) + ` · ${fmt.watts(node.power)}` : t("离线，显示最近一次的清单"))
      : (online ? t("这个节点没有检测到 NVIDIA GPU") : ""));
    sync(gpuRows, detail.gpus, (gpu) => gpu.index, makeGpu);

    const live = detail.live;
    sync(cores, live ? live.cores.map((value, index) => ({ index, value })) : [], (core) => core.index, (core) => {
      const cell = h("i");
      let latest = core;
      hoverTip(cell, () => [h("b", { text: t("核 {n}", { n: latest.index }) }), h("span", { text: fmt.pct(latest.value) })]);
      cell._update = (value) => { latest = value; cell.style.setProperty("--v", value.value == null ? 0 : value.value); };
      return cell;
    });
    setText(coreNote, live ? t("{n} 线程 · 总利用率 {util}", { n: live.cores.length, util: fmt.pct(live.cpu) }) : "");
    const memory = live ? live.mem : null;
    if (memory && memory.total) {
      const shm = memory.shmem || 0, app = Math.max(0, memory.used - shm), free = Math.max(0, memory.total - memory.used);
      [app, shm, free].forEach((value, index) => { memBar.children[index].style.flexGrow = String(Math.max(0.0001, value)); });
      sync(memLegend, [[t("应用"), app, "var(--accent)"], [t("共享内存 /dev/shm"), shm, "var(--role-decode)"], [t("可用"), free, "var(--axis)"]].map(([label, value, color]) => ({ label, value, color })),
        (item) => item.label, (item) => {
          const amount = h("b");
          const entry = h("span", null, h("i", { style: `--c:${item.color}` }), item.label, amount);
          entry._update = (value) => setText(amount, `${fmt.gib(value.value, 0)} GiB`);
          return entry;
        });
      setText(middle.querySelector(".mem-total"), t("共 {total} GiB", { total: fmt.gib(memory.total, 0) }));
    }
    sync(disks, detail.disks, (disk) => disk.mount, (disk) => {
      const value = h("b"), bar = meter("thick");
      const row = h("div", null, h("div", { class: "mm-head" }, h("span", { class: "mono", text: disk.mount }), value), bar);
      row._update = (item) => {
        const share = item.total ? 100 * item.used / item.total : null;
        setText(value, `${fmt.bytes(item.used)} / ${fmt.bytes(item.total)} · ${fmt.pct(share)}`);
        setMeter(bar, share, fullness(share, item.mount === "/dev/shm" ? 90 : 85, 95));
      };
      return row;
    });
    setText(netNote, live && live.net ? t("网络 ↓ {rx} · ↑ {tx}", { rx: fmt.rate(live.net.rx), tx: fmt.rate(live.net.tx) }) : "");

    charts.update(detail.history);
    sync(services, detail.services.length ? detail.services : [{ id: "none" }], (service) => service.id, (service) => {
      if (service.id === "none") return h("div", { class: "muted", text: t("没有发现 Dynamo / SGLang 服务") });
      const label = h("b"), roles = h("small"), state = pill(SERVICE_STATUS.unknown);
      const row = h("button", { class: "list-row", type: "button", onclick: () => ctx.go(`#/service/${service.id}`) }, h("div", null, label, roles), state);
      row._update = (item) => {
        setText(label, item.name);
        setText(roles, item.roles.map((role) => ROLE[role] || role).join(" · "));
        setPill(state, SERVICE_STATUS[item.status] || SERVICE_STATUS.unknown);
      };
      return row;
    });
    sync(consumers, detail.consumers.length ? detail.consumers : [{ name: "\u0000none" }], (item) => item.name, (item) => {
      if (item.name === "\u0000none") return h("div", { class: "muted", text: t("没有进程占用 GPU") });
      const label = h("b"), kind = h("small"), amount = h("div", { class: "right num" });
      const row = h("div", { class: "list-row" }, h("div", null, label, kind), amount);
      row._update = (value) => {
        setText(label, value.name);
        setText(kind, `${value.kind === "service" ? t("服务") : value.kind === "container" ? t("容器") : t("用户")}${value.user && value.kind !== "user" ? " · " + value.user : ""} · GPU ${value.gpus.join(", ")}`);
        setText(amount, `${fmt.gib(value.mem)} GiB`);
      };
      return row;
    });
    renderFeed(feed, detail.events, now, false);
  }

  async function poll() {
    if (dead) return;
    try {
      detail = await api(`/api/nodes/${nodeId}?window=${charts.window()}`);
      render(ctx.state ? ctx.state.now : Date.now() / 1000);
      ctx.refreshChrome();
    } catch (failure) {
      if (failure.status === 404) { toast(t("该节点已被移除")); ctx.back(); return; }
    }
    timer = setTimeout(poll, 2000);
  }

  return {
    el,
    hero,
    title: () => (detail ? detail.node.name : (ctx.nodeById(nodeId) || {}).name) || t("节点"),
    crumb: () => [{ text: t("集群概览"), href: "#/" }, t("节点")],
    update() {},
    mount() {
      const known = ctx.nodeById(nodeId);
      if (known) { setText(name, known.name); setText(sub, `${known.user}@${known.host}`); setPill(status, NODE_STATUS[known.status] || NODE_STATUS.stopped); }
      poll();
    },
    unmount() { dead = true; clearTimeout(timer); for (const row of gpuRows.children) if (row._dispose) row._dispose(); },
  };
}
