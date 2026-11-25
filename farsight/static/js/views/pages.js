// The remaining pages: node table, service table, activity log, settings.
import { h, icon, setText, meter, setMeter, fullness, sync, fmt, pill, setPill, renderFeed, section,
  NODE_STATUS, SERVICE_STATUS, LEVEL, gpuModelLabel, api, toast } from "../util.js";
import { t, N } from "../i18n.js";
import { serviceSubtitle } from "./overview.js";

function barCell() {
  const bar = meter();
  const text = h("span", { class: "num" });
  const cell = h("td", null, bar, text);
  cell.set = (percent, tone) => { setMeter(bar, percent, tone); setText(text, fmt.pct(percent)); };
  return cell;
}

export function nodesPage(ctx) {
  const body = h("tbody");
  const empty = h("div", { class: "empty", hidden: true });
  const el = h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("全部节点") }), h("p", { text: t("点击一行查看该节点的 GPU、进程归属与趋势") }))),
    h("div", { class: "table-wrap" }, h("table", { class: "grid" },
      h("thead", null, h("tr", null, [t("节点"), t("状态"), "GPU", "CPU", t("内存"), t("GPU 利用率"), t("显存"), t("功耗"), t("服务")].map((label) => h("th", { text: label })))), body)),
    empty);
  return {
    el,
    title: () => t("节点"),
    crumb: (state) => [{ text: t("集群概览"), href: "#/" }, t("{online} / {total} 在线", { online: state.summary.online, total: state.summary.nodes })],
    update(state) {
      const query = ctx.query();
      const nodes = state.nodes.filter((node) => !query || [node.name, node.host, node.label, node.gpu_model].join(" ").toLowerCase().includes(query))
        .sort((a, b) => a.name.localeCompare(b.name, "zh-CN", { numeric: true }));
      sync(body, nodes, (node) => node.id, (node) => {
        const name = h("b"), address = h("div", { class: "muted" });
        const status = pill(NODE_STATUS.connecting);
        const model = h("td"), cpu = barCell(), mem = barCell(), gpu = barCell(), vram = barCell(), power = h("td", { class: "num" }), services = h("td");
        const row = h("tr", { class: "click", tabindex: "0" }, h("td", null, name, address), h("td", null, status), model, cpu, mem, gpu, vram, power, services);
        const open = () => ctx.go(`#/node/${node.id}`);
        row.addEventListener("click", open);
        row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
        row._update = (item) => {
          setText(name, item.name);
          setText(address, `${item.user}@${item.host}`);
          const info = NODE_STATUS[item.status] || NODE_STATUS.stopped;
          setPill(status, item.error ? { text: item.status === "connecting" ? t("重试中：{title}", { title: item.error.title }) : item.error.title, tone: "crit" } : info);
          setText(model, item.status === "online" ? gpuModelLabel(item) : "—");
          cpu.set(item.cpu); mem.set(item.mem, fullness(item.mem, 88, 95)); gpu.set(item.gpu); vram.set(item.vram);
          setText(power, item.power == null ? "—" : fmt.watts(item.power));
          setText(services, item.services.map((id) => (ctx.serviceById(id) || {}).name).filter(Boolean).join(", ") || "—");
        };
        return row;
      });
      empty.hidden = nodes.length > 0;
      if (!nodes.length) empty.replaceChildren(h("b", { text: state.nodes.length ? t("没有符合条件的节点") : t("还没有节点") }), state.nodes.length ? t("调整搜索词。") : t("点击右上角“添加节点”。"));
    },
  };
}

export function servicesPage(ctx) {
  const body = h("tbody");
  const empty = h("div", { class: "empty", hidden: true });
  const el = h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("全部服务") }), h("p", { text: t("自动发现的 Dynamo 与 SGLang 服务，点击一行查看请求链路") }))),
    h("div", { class: "table-wrap" }, h("table", { class: "grid" },
      h("thead", null, h("tr", null, [t("服务"), t("状态"), t("组成"), t("进行中"), t("等待"), t("输出 tok/s"), t("首 token P95"), t("GPU 租金 / 小时"), t("成本 / 百万 token"), t("GPU 利用率")]
        .map((label) => h("th", { text: label })))), body)),
    empty);
  return {
    el,
    title: () => t("服务"),
    crumb: (state) => [{ text: t("集群概览"), href: "#/" }, t("{n} 个服务", { n: state.summary.services })],
    update(state) {
      const query = ctx.query();
      const services = state.services.filter((service) => !query || [service.name, service.model, service.namespace].join(" ").toLowerCase().includes(query));
      sync(body, services, (service) => service.id, (service) => {
        const name = h("b"), model = h("div", { class: "muted" });
        const status = pill(SERVICE_STATUS.unknown);
        const parts = h("td"), inflight = h("td", { class: "num" }), queued = h("td", { class: "num" }), tps = h("td", { class: "num" }), ttft = h("td", { class: "num" });
        const rent = h("td", { class: "num" }), blended = h("td", { class: "num" }), gpu = barCell();
        const row = h("tr", { class: "click", tabindex: "0" }, h("td", null, name, model), h("td", null, status), parts, inflight, queued, tps, ttft, rent, blended, gpu);
        const open = () => ctx.go(`#/service/${service.id}`);
        row.addEventListener("click", open);
        row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
        row._update = (item) => {
          const metrics = item.metrics || {};
          const cost = item.cost || {};
          setText(name, item.name);
          setText(model, `${item.kind === "dynamo" ? "Dynamo" : "SGLang"}${item.model_short ? " · " + item.model_short : ""}`);
          setPill(status, SERVICE_STATUS[item.status] || SERVICE_STATUS.unknown);
          setText(parts, serviceSubtitle(item));
          setText(inflight, fmt.int(metrics.inflight)); setText(queued, fmt.int(metrics.queued));
          setText(tps, fmt.int(metrics.tps)); setText(ttft, fmt.secs(metrics.ttft_p95));
          setText(rent, cost.rent ? fmt.money(cost.rent, cost.currency) : "—");
          setText(blended, fmt.money(cost.blended, cost.blended == null ? "" : cost.currency));
          gpu.set(item.gpu.util);
        };
        return row;
      });
      empty.hidden = services.length > 0;
      if (!services.length) empty.replaceChildren(h("b", { text: t("没有发现服务") }), t("在已连接的节点上启动 Dynamo 或 SGLang 后，这里会自动出现。"));
    },
  };
}

export function activityPage(ctx) {
  const alerts = h("div", { class: "alert-list" });
  const feed = h("div", { class: "feed" });
  let events = [];
  let timer = null;
  let dead = false;
  const el = h("div", { class: "settings" },
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("当前告警") }), h("p", { text: t("条件持续成立才触发，消失后自动恢复") }))), alerts),
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("活动记录") }), h("p", { text: t("节点连接、服务变化与告警的时间线，保存在内存中") }))), feed));
  async function poll() {
    if (dead) return;
    try { events = (await api("/api/events?limit=200")).events; if (ctx.state) view.update(ctx.state); } catch (failure) { /* retried */ }
    timer = setTimeout(poll, 4000);
  }
  const view = {
    el,
    title: () => t("活动"),
    crumb: (state) => [{ text: t("集群概览"), href: "#/" }, t("{n} 条告警", { n: state.alerts.length })],
    update(state) {
      sync(alerts, state.alerts.length ? state.alerts : [{ key: "none" }], (alert) => alert.key, (alert) => {
        if (alert.key === "none") return h("div", { class: "empty", text: t("当前没有告警") });
        const title = h("b"), text = h("p");
        const note = h("div", { class: "note" }, icon((LEVEL[alert.level] || LEVEL.info).icon), h("div", null, title, text));
        note._update = (item) => {
          note.dataset.tone = (LEVEL[item.level] || LEVEL.info).tone;
          setText(title, `${item.name} · ${item.title}`);
          setText(text, `${item.detail ? item.detail + " · " : ""}` + t("自 {time}", { time: fmt.ago(item.since, state.now) }));
        };
        return note;
      });
      renderFeed(feed, events.length ? events : state.events, state.now, true);
    },
    mount() { poll(); },
    unmount() { dead = true; clearTimeout(timer); },
  };
  return view;
}

const THRESHOLD_LABELS = {
  sustain_seconds: [N("持续多久才触发"), N("秒")], gpu_temp_warn: [N("GPU 温度提醒"), "°C"], gpu_temp_crit: [N("GPU 温度严重"), "°C"],
  memory_percent: [N("主机内存"), "%"], disk_percent: [N("系统盘"), "%"], shm_percent: ["/dev/shm", "%"], vram_percent: [N("显存"), "%"],
  idle_spin_util: [N("空转判定：利用率不低于"), "%"], idle_spin_mib: [N("空转判定：显存不高于"), "MiB"],
  error_ratio: [N("请求错误率"), ""], ttft_p95_ms: [N("首 token 延迟 P95"), "ms"], queue_seconds: [N("调度队列积压"), N("秒")],
};
const TIERS = [["low", N("低")], ["mid", N("中")], ["high", N("高")]];

function segmented(label, options, current, choose) {
  const seg = h("div", { class: "seg", role: "group", "aria-label": label }, options.map(([key, text]) =>
    h("button", { type: "button", "aria-pressed": String(current() === key), text, onclick: () => { choose(key); seg.refresh(); } })));
  seg.refresh = () => Array.from(seg.children).forEach((button, index) => button.setAttribute("aria-pressed", String(options[index][0] === current())));
  return seg;
}

/** Rental prices per GPU model, the tier of the built-in table, the cache-hit ratio and the currency sign. */
function costSettings() {
  let data = null;
  let tier = "mid";
  const inputs = new Map();
  const tierSeg = segmented(t("参考价档位"), TIERS.map(([key, label]) => [key, t(label)]), () => tier, (key) => { tier = key; fill(); });
  const currency = h("input", { name: "currency", maxlength: "8", autocomplete: "off", spellcheck: "false" });
  const alpha = h("input", { name: "alpha", type: "number", min: "0", max: "1", step: "0.01" });
  const body = h("tbody");
  const note = h("p", { class: "muted" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const save = h("button", { class: "btn primary", type: "submit" }, t("保存"));
  const reset = h("button", { class: "btn", type: "button", onclick: () => { for (const input of inputs.values()) input.value = ""; } }, t("清空填写的租金"));
  const form = h("form", { class: "cost-form", novalidate: true },
    h("div", { class: "cost-options" },
      h("div", { class: "field ref" }, t("参考价档位"), tierSeg),
      h("label", { class: "field" }, t("货币符号"), currency),
      h("label", { class: "field" }, t("缓存命中折算"), alpha)),
    h("div", { class: "table-wrap" }, h("table", { class: "plain prices" },
      h("thead", null, h("tr", null, h("th", { text: t("节点上的 GPU 型号") }), h("th", { class: "r", text: t("数量") }), h("th", { class: "ref", text: t("对应的参考型号") }),
        h("th", { class: "r ref", text: t("参考价 低 / 中 / 高") }), h("th", { class: "r", text: t("每卡每小时租金") }))), body)),
    note, error, h("div", { class: "sheet-actions" }, reset, save));

  function fill() {
    if (!data) return;
    sync(body, data.detected.length ? data.detected : [{ name: "\u0000none" }], (gpu) => gpu.name, (gpu) => {
      if (gpu.name === "\u0000none") return h("tr", null, h("td", { colspan: "5", class: "muted", text: t("还没有连上带 GPU 的节点。") }));
      const input = h("input", { type: "number", min: "0", step: "0.1", inputmode: "decimal", "aria-label": t("{name} 的每卡每小时租金", { name: gpu.name }) });
      inputs.set(gpu.name, input);
      const count = h("td", { class: "r num" }), model = h("td", { class: "ref" }), reference = h("td", { class: "r num ref" });
      const row = h("tr", null, h("td", null, h("b", { text: gpu.name }), h("div", { class: "muted", text: gpu.memory_gib ? `${gpu.memory_gib} GiB` : "" })),
        count, model, reference, h("td", { class: "r" }, input));
      row._update = (item) => {
        setText(count, t("{count} 张 · {nodes} 台", { count: item.count, nodes: item.nodes }));
        setText(model, item.model || t("单价表里没有这个型号"));
        model.classList.toggle("muted", !item.model);
        setText(reference, item.table ? ["low", "mid", "high"].map((key) => item.table[key] == null ? "—" : item.table[key]).join(" / ") : "—");
        input.placeholder = item.table && item.table[tier] != null ? String(item.table[tier]) : t("未定价");
      };
      return row;
    });
  }
  function load(next) {
    data = next;
    tier = data.cost.tier;
    tierSeg.refresh();
    currency.value = data.cost.currency;
    alpha.value = String(data.cost.alpha);
    fill();
    for (const [name, input] of inputs) input.value = data.cost.overrides[name] == null ? "" : String(data.cost.overrides[name]);
    form.classList.toggle("no-table", !data.table.length);
    const updated = data.updated ? t("，更新于 {date}", { date: data.updated }) : "";
    const source = !data.table.length
      ? t("没有加载单价表：为每种 GPU 填写每卡每小时的租金，没填的型号不计入成本。启动 FarSight 时加 --prices <文件> 可以加载一份参考单价表。")
      : data.prices_file ? t("参考价来自单价表 {file}{updated}。填写的租金优先于参考价，留空则用所选档位的参考价。", { file: data.prices_file, updated })
        : t("参考价来自演示用的单价表。填写的租金优先于参考价，留空则用所选档位的参考价。");
    setText(note, source + (data.saved ? "" : " " + t("演示模式下设置只在本次运行内有效。")));
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const overrides = {};
    for (const [name, input] of inputs) {
      if (input.value.trim() === "") continue;
      const price = Number(input.value);
      if (!Number.isFinite(price) || price < 0) { error.textContent = t("{name} 的租金必须是不小于 0 的数字", { name }); error.hidden = false; input.focus(); return; }
      overrides[name] = price;
    }
    const ratio = Number(alpha.value);
    if (alpha.value.trim() === "" || !Number.isFinite(ratio) || ratio < 0 || ratio > 1) { error.textContent = t("缓存命中折算必须在 0 到 1 之间"); error.hidden = false; alpha.focus(); return; }
    save.disabled = true;
    try {
      load(await api("/api/settings", { method: "PUT", body: { cost: { tier, currency: currency.value.trim() || null, alpha: ratio, overrides } } }));
      toast(t("成本设置已保存"));
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      save.disabled = false;
    }
  });
  api("/api/settings").then(load).catch((failure) => { error.textContent = failure.message; error.hidden = false; });
  return form;
}

export function settingsPage(ctx) {
  const themeSeg = segmented(t("主题"), [["auto", t("跟随系统")], ["light", t("浅色")], ["dark", t("深色")]], () => ctx.themeChoice(), (key) => ctx.setTheme(key));
  const langSeg = segmented(t("语言"), [["zh", "\u4e2d\u6587"], ["en", "English"]], () => ctx.lang(), (key) => ctx.setLang(key));
  const thresholds = h("dl", { class: "kv" });
  const about = h("dl", { class: "kv" });
  const el = h("div", { class: "settings" },
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("外观") }), h("p", { text: t("选择保存在这个浏览器里") }))),
      h("div", { class: "options" }, h("div", { class: "field" }, t("主题"), themeSeg), h("div", { class: "field" }, t("语言"), langSeg)),
      section(t("关于"), null, about)),
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("告警阈值") }), h("p", { text: t("启动面板时用 --thresholds <文件.json> 覆盖") }))), thresholds),
    h("section", { class: "panel wide" }, h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("成本估算") }),
      h("p", { text: t("按 GPU 型号设定大致的租赁价；服务页用它和实测吞吐折算每百万 token 的成本") }))), costSettings()));
  return {
    el,
    title: () => t("设置"),
    crumb: () => [{ text: t("集群概览"), href: "#/" }, t("设置")],
    update(state) {
      const meta = ctx.meta() || {};
      const rows = Object.entries(meta.thresholds || {}).map(([key, value]) => {
        const [label, unit] = THRESHOLD_LABELS[key] || [key, ""];
        return [t(label), `${value} ${t(unit)}`.trim()];
      });
      const fill = (target, entries) => sync(target, entries.flatMap(([label, value]) => [{ key: "t" + label, tag: "dt", text: label }, { key: "d" + label, tag: "dd", text: value }]),
        (item) => item.key, (item) => { const cell = h(item.tag); cell._update = (value) => setText(cell, value.text); return cell; });
      fill(thresholds, rows);
      fill(about, [[t("版本"), state.version], [t("数据来源"), state.demo ? t("演示数据（合成）") : t("SSH 实时采集")], [t("硬件采样间隔"), t("{n} 秒", { n: state.interval })],
        [t("采集方式"), t("只读；不在节点上安装或写入任何内容")], [t("端点探测"), t("只访问服务自己声明的 HTTP 端口，不扫描端口")],
        [t("通信拓扑"), t("读取节点的 socket 表得到，不向任何端口发送数据")]]);
    },
  };
}
