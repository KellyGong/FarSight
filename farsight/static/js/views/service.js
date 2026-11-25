// Service detail: what the service is and how it is doing, the request path Frontend -> Prefill -> Decode with the
// load of every component's node and GPUs, what it costs, then trends and activity.
import { h, icon, setText, tween, meter, setMeter, sync, fmt, pill, setPill, chip, section, renderFeed,
  SERVICE_STATUS, HEALTH, ROLE, LEVEL, api, toast } from "../util.js";
import { t, N } from "../i18n.js";
import { trends } from "../charts.js";
import { serviceSubtitle } from "./overview.js";

const STAGES = [
  { key: "frontend", roles: ["frontend", "router"], title: "Frontend", note: N("接收请求并路由") },
  { key: "prefill", roles: ["prefill"], title: "Prefill", note: N("处理输入，生成 KV 缓存") },
  { key: "decode", roles: ["decode", "worker", "server"], title: "Decode", note: N("逐 token 生成输出") },
  { key: "etcd", roles: ["etcd"], title: N("服务发现"), note: "etcd" },
];
const COST_WINDOWS = [[60, N("1 分钟")], [300, N("5 分钟")], [900, N("15 分钟")], [3600, N("1 小时")]];

function statTile(label, unit) {
  const value = h("span");
  const small = h("small", { text: unit || "" });
  const el = h("div", { class: "tile" }, h("b", null, value, small), h("span", { text: label }));
  el.set = (number, format) => tween(value, number, format || fmt.int, true);
  el.unit = (text) => setText(small, text);
  return el;
}

function componentRow(ctx) {
  return (component) => {
    const title = h("b"), where = h("span");
    const loadValue = h("b"), loadBar = meter();
    const load = h("div", { class: "comp-load" }, h("div", { class: "mm-head" }, h("span", { text: "GPU" }), loadValue), loadBar);
    const stats = h("div", { class: "comp-stats" });
    const health = pill(HEALTH.unknown);
    const head = h("button", { class: "row-head comp-head", type: "button", "aria-expanded": "false" },
      h("div", { class: "comp-title" }, title, where), load, h("div", { class: "comp-side" }, stats, health), h("span", { class: "chev" }, icon("chevron")));
    const body = h("div", { class: "row-body", hidden: true });
    const el = h("div", { class: "row" }, head, body);
    let open = false;
    let latest = component;

    function renderBody() {
      const item = latest;
      const cards = h("div", { class: "gpu-mini" });
      for (const gpu of item.gpu_cards) {
        const used = gpu.mem_total ? 100 * (gpu.mem || 0) / gpu.mem_total : null;
        const utilBar = meter(), memBar = meter();
        setMeter(utilBar, gpu.util); setMeter(memBar, used);
        cards.append(h("div", null,
          h("div", { class: "mm-head" }, h("span", { text: `GPU ${gpu.index}` }), h("span", { class: "muted", text: [gpu.temp == null ? null : Math.round(gpu.temp) + " °C", gpu.power == null ? null : fmt.watts(gpu.power)].filter(Boolean).join(" · ") })),
          h("div", { class: "mm-head" }, h("span", { text: t("利用率") }), h("b", { text: fmt.pct(gpu.util) })), utilBar,
          h("div", { class: "mm-head" }, h("span", { text: t("显存") }), h("b", { text: gpu.mem == null ? "—" : `${fmt.gib(gpu.mem)} / ${fmt.gib(gpu.mem_total)} GiB` })), memBar));
      }
      const ports = Object.entries(item.ports || {}).map(([name, port]) => `${name} ${port}`).join(" · ");
      // Name only the endpoints that are not ready; a long list of "ready" says nothing.
      const states = Object.entries(item.endpoints || {});
      const waiting = states.filter(([, state]) => state !== "ready");
      const endpoints = !states.length ? "" : waiting.length
        ? waiting.map(([name, state]) => `${name}: ${state}`).join(", ")
        : t("{n} 个端点全部就绪", { n: states.length });
      body.replaceChildren(
        h("dl", { class: "kv", style: "margin-top:12px" },
          h("dt", { text: t("节点") }), h("dd", null, chip(`${item.node_name} · ${item.host}`, null, () => ctx.go(`#/node/${item.node}`))),
          item.node_load ? [h("dt", { text: t("节点负载") }), h("dd", { text: t("CPU {cpu} · 内存 {mem}", { cpu: fmt.pct(item.node_load.cpu), mem: fmt.pct(item.node_load.mem) }) })] : null,
          h("dt", { text: t("进程") }), h("dd", { text: `pid ${item.pid}${item.user ? " · " + item.user : ""}${item.started ? " · " + t("已运行 {time}", { time: fmt.dur(ctx.now() - item.started) }) : ""}` }),
          item.container ? [h("dt", { text: t("容器") }), h("dd", { class: "mono", text: item.container })] : null,
          item.image ? [h("dt", { text: t("镜像") }), h("dd", { class: "mono", text: item.image })] : null,
          ports ? [h("dt", { text: t("端口") }), h("dd", { text: ports })] : null,
          h("dt", { text: t("健康") }), h("dd", { text: item.health_text ? `${item.health_text}${endpoints ? " (" + endpoints + ")" : ""}` : t("未探测：该组件没有声明可访问的 HTTP 端口") }),
          item.cmdline ? [h("dt", { text: t("命令行") }), h("dd", { class: "mono", text: item.cmdline })] : null),
        ...(item.gpu_cards.length ? [cards] : []));
    }
    head.addEventListener("click", () => {
      open = !open;
      el.classList.toggle("open", open);
      head.setAttribute("aria-expanded", String(open));
      body.hidden = !open;
      if (open) renderBody();
    });
    el._update = (item) => {
      latest = item;
      setText(title, item.container || `${ROLE[item.role] || item.role} · pid ${item.pid}`);
      setText(where, `${item.node_name} · ${item.gpus.length ? "GPU " + item.gpus.join(", ") : t("不占 GPU")}`);
      load.style.visibility = item.gpus.length ? "visible" : "hidden";
      setText(loadValue, fmt.pct(item.gpu && item.gpu.util));
      setMeter(loadBar, item.gpu && item.gpu.util);
      const metrics = item.metrics || {};
      const bits = [];
      if (metrics.inflight != null) bits.push([t("进行中"), fmt.int(metrics.inflight)]);
      if (metrics.running != null) bits.push([t("运行"), fmt.int(metrics.running)]);
      if (metrics.queued != null) bits.push([t("排队"), fmt.int(metrics.queued)]);
      if (metrics.kv_usage != null) bits.push(["KV", fmt.pct(metrics.kv_usage * 100)]);
      if (metrics.tps != null) bits.push(["tok/s", fmt.int(metrics.tps)]);
      sync(stats, bits.slice(0, 3).map(([label, value]) => ({ label, value })), (bit) => bit.label, (bit) => {
        const value = h("b");
        const entry = h("span", null, bit.label + " ", value);
        entry._update = (next) => setText(value, next.value);
        return entry;
      });
      setPill(health, item.node_status !== "online" ? { text: N("节点离线"), tone: "mute" } : (HEALTH[item.health] || HEALTH.unknown));
      if (open) renderBody();
    };
    return el;
  };
}

export function serviceView(ctx, serviceId) {
  let detail = null;
  let timer = null;
  let dead = false;
  let costWindow = 300;
  let currency = "";
  const status = pill(SERVICE_STATUS.unknown);
  const glyph = h("div", { class: "svc-icon big" }, icon("bolt"));
  const name = h("h2");
  const sub = h("p");
  const tiles = { inflight: statTile(t("进行中")), queued: statTile(t("排队")), tps: statTile(t("输出 tok/s")) };
  tiles.inflight.title = t("已进入、尚未完成的请求数");
  tiles.tps.title = t("最近 1 分钟平均每秒输出的 token 数");
  const notes = h("div", { class: "rows" });
  const numbers = h("dl", { class: "kv" });
  const checks = h("div", { class: "checks" });
  const facts = h("dl", { class: "kv" });
  const hero = h("aside", { class: "col panel hero" },
    h("div", { class: "hero-top" }, h("button", { class: "back", type: "button", "aria-label": t("返回概览"), onclick: () => ctx.back() }, icon("back")), status),
    h("div", { class: "ident" }, glyph, name, sub),
    h("div", { class: "tiles" }, tiles.inflight, tiles.queued, tiles.tps),
    notes,
    h("div", null, h("div", { class: "sub-label", text: t("最近 1 分钟") }), numbers),
    h("div", null, h("div", { class: "sub-label", text: t("健康检查") }), checks),
    h("div", null, h("div", { class: "sub-label", text: t("接入信息") }), facts));

  const pathNote = h("p");
  const steps = h("div", { class: "steps" });
  const resources = { gpus: statTile("GPU"), util: statTile(t("平均利用率"), "%"), vram: statTile(t("显存占用"), "GiB"), power: statTile(t("功耗"), "kW") };
  const makeRow = componentRow(ctx);

  const money = { rent: statTile(t("GPU 租金")), blended: statTile(t("综合成本")), output: statTile(t("输出成本")), idle: statTile(t("其中空闲租金")) };
  money.rent.title = t("这个服务占用的 GPU 每小时的租金，不随负载变化");
  money.blended.title = t("全部租金 ÷ 这段时间的全部 token（输入 + 输出）");
  money.idle.title = t("租金 ×（1 − GPU 忙时占比）：没有换到 token 的那部分");
  const costSeg = h("div", { class: "seg", role: "group", "aria-label": t("成本统计窗口") }, COST_WINDOWS.map(([seconds, label]) =>
    h("button", { type: "button", "aria-pressed": String(seconds === costWindow), text: t(label), onclick: (event) => {
      costWindow = seconds;
      for (const button of costSeg.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
      clearTimeout(timer);
      poll();
    } })));
  const costRows = h("dl", { class: "kv cost-rows" });
  const costNotes = h("div", { class: "cost-notes" });
  const costBox = h("div", { class: "box" }, costRows, costNotes);
  const costSection = section(t("成本估算"), costSeg, h("div", { class: "tiles four" }, money.rent, money.blended, money.output, money.idle), costBox);
  const middle = h("div", { class: "col panel" },
    h("div", { class: "panel-head" }, h("div", null, h("h2", { text: t("请求链路") }), pathNote)), steps,
    section(t("资源汇总"), t("该服务占用的 GPU"), h("div", { class: "tiles four" }, resources.gpus, resources.util, resources.vram, resources.power)),
    costSection);

  const charts = trends([
    { key: "tps", label: t("输出吞吐"), format: (value) => fmt.int(value) + " tok/s" },
    { key: "ttft_p95", label: t("首 token 延迟 P95"), format: fmt.secs },
    { key: "inflight", label: t("进行中请求"), format: fmt.int },
    { key: "gpu", label: t("GPU 平均利用率"), format: (value) => Math.round(value) + "%", max: 100 },
    { key: "cost", label: t("综合成本 / 百万 token"), format: (value) => fmt.money(value, currency) },
  ], () => { clearTimeout(timer); poll(); });
  const feed = h("div", { class: "feed" });
  const right = h("div", { class: "col" },
    h("section", { class: "panel" }, h("div", { class: "section-title" }, h("h3", { text: t("趋势") })), charts),
    h("section", { class: "panel section" }, h("div", { class: "section-title" }, h("h3", { text: t("活动") })), feed));
  const el = h("div", { class: "detail" }, hero, middle, right);

  const kv = (target, entries, emptyText) => {
    const cells = entries.length ? entries.flatMap(([label, value]) => [{ key: "t" + label, tag: "dt", text: label }, { key: "d" + label, tag: "dd", text: value }])
      : [{ key: "empty", tag: "dt", text: emptyText }];
    sync(target, cells, (item) => item.key, (item) => { const cell = h(item.tag); cell._update = (value) => setText(cell, value.text); return cell; });
  };

  function stage(definition) {
    const dot = h("div", { class: "step-dot" }, icon("check"));
    const count = h("span");
    const rows = h("div", { class: "rows" });
    const element = h("div", { class: "step" }, dot, h("div", null, h("div", { class: "step-head" }, h("b", { text: t(definition.title) }), count), rows));
    element._update = (group) => {
      const states = group.components.map((component) => component.node_status !== "online" ? "unknown" : component.health);
      const tone = states.includes("error") ? "crit" : states.includes("starting") ? "info" : states.every((state) => state === "ok") && states.length ? "good" : "mute";
      dot.dataset.tone = group.missing.length && tone === "good" ? "serious" : tone;
      dot.replaceChildren(icon(tone === "crit" || (group.missing.length && tone === "good") ? "alert" : tone === "good" ? "check" : tone === "info" ? "clock" : "dot"));
      setText(count, `${t(definition.note)} · ` + t("{n} 个", { n: group.components.length })
        + (group.missing.length ? t("，另有 {n} 个不在监控中", { n: group.missing.length }) : ""));
      const items = group.components.map((component) => ({ key: component.key, component }))
        .concat(group.missing.map((missing) => ({ key: "missing:" + missing.host, missing })));
      sync(rows, items, (item) => item.key, (item) => {
        if (item.missing) {
          const text = h("span");
          const button = h("button", { class: "btn small", type: "button" });
          const ghost = h("div", { class: "absent" }, text, button);
          ghost._update = (next) => {
            const missing = next.missing;
            text.replaceChildren(h("b", { text: missing.host }), " ", missing.reason === "offline" ? t("上的实例：该节点当前离线") : t("上还有实例，但这个节点没有加入面板"));
            button.replaceChildren(icon(missing.reason === "offline" ? "server" : "plus"), missing.reason === "offline" ? t("查看节点") : t("添加此节点"));
            button.onclick = () => missing.reason === "offline" ? ctx.go(`#/node/${missing.node}`) : ctx.addNode({ hosts: missing.host });
          };
          return ghost;
        }
        const row = makeRow(item.component);
        const update = row._update;
        row._update = (next) => update(next.component);
        return row;
      });
    };
    return element;
  }

  function renderCost(service) {
    const cost = service.cost;
    costSection.hidden = !cost;
    if (!cost) return;
    currency = cost.currency;
    const per = (value) => value == null ? null : t("{amount} / 百万 token", { amount: fmt.money(value, currency) });
    const figures = cost.window || cost.now;
    const rent = cost.rent;
    money.rent.unit(`${currency}/h`);
    money.blended.unit(`${currency}/M`);
    money.output.unit(`${currency}/M`);
    money.idle.unit(`${currency}/h`);
    const plain = (value) => fmt.money(value);
    money.rent.set(rent.total || null, plain);
    money.blended.set(figures.blended, plain);
    money.output.set(figures.output, plain);
    money.idle.set(figures.idle_rent, plain);
    money.output.title = figures.split ? t("Decode 的租金 ÷ 输出 token") : t("全部租金 ÷ 输出 token");
    const tokens = (cost.window && cost.window.tokens) || cost.tokens || {};
    const rate = (value) => value == null ? "—" : fmt.int(value);
    const rows = [];
    if (figures.split) {
      rows.push([t("输入 · 未命中缓存"), per(figures.miss)], [t("输入 · 命中缓存（按未命中的 {alpha} 倍）", { alpha: cost.alpha }), per(figures.hit)],
        [t("输出"), per(figures.output)]);
    }
    rows.push([t("这段时间的吞吐"), tokens.computed == null && tokens.output == null ? null
      : t("计算 {computed} · 命中 {hit} · 输出 {output} tok/s", { computed: rate(tokens.computed), hit: rate(tokens.hit), output: rate(tokens.output) })]);
    rows.push([t("GPU 忙时占比"), figures.busy == null ? null : fmt.pct(100 * figures.busy)]);
    for (const item of rent.items) {
      const role = item.role === "other" ? t("GPU") : ROLE[item.role];
      rows.push([t("租金 · {role}", { role }) + ` · ${item.name}`, item.unit == null ? t("{n} 张，没有单价", { n: item.count })
        : `${item.count} × ${fmt.money(item.unit, currency)} = ${fmt.money(item.amount, currency)}/h` + (item.source === "override" ? " · " + t("自定义单价") : "")]);
    }
    kv(costRows, rows.filter(([, value]) => value != null), "—");
    const notes = [];
    if (rent.unpriced) notes.push(t("{n} 张 GPU 没有单价，没有计入租金。到“设置”里为这个型号填写单价。", { n: rent.unpriced }));
    if (cost.partial) notes.push(t("这个服务有节点没有加入面板：租金和吞吐只统计了已加入的部分。"));
    if (rent.total && figures.blended == null) notes.push(t("这段时间没有吞吐，无法折算成每百万 token 的成本。"));
    if (cost.source === "prompt") notes.push(t("引擎没有区分命中缓存的输入，输入全部按未命中计。"));
    if (cost.source === "frontend") notes.push(t("输入的命中 / 未命中取自 Frontend 的计数。"));
    if (cost.window && cost.window.seconds > 60 && cost.window.span < cost.window.seconds * 0.8) {
      notes.push(t("面板目前只有最近 {span} 的数据，所选窗口尚未填满。", { span: fmt.dur(cost.window.span) }));
    }
    notes.push(t("口径：全部租金（含空闲）÷ 所选窗口内的吞吐。单价是大致的租赁价，可以在“设置”里修改。"));
    sync(costNotes, notes.map((text) => ({ text })), (note) => note.text, (note) => h("p", { text: note.text }));
  }

  function render(now) {
    const service = detail;
    setPill(status, SERVICE_STATUS[service.status] || SERVICE_STATUS.unknown);
    glyph.dataset.kind = service.kind;
    glyph.replaceChildren(icon(service.kind === "sglang" ? "cube" : "bolt"));
    setText(name, service.name);
    const flavor = service.kind === "dynamo" ? `Dynamo${service.backend ? " + " + (service.backend === "sglang" ? "SGLang" : service.backend) : ""}` : "SGLang";
    setText(sub, `${flavor} · ${service.mode === "disagg" ? t("Prefill / Decode 分离") : t("聚合部署")}${service.model ? " · " + service.model : ""}`);
    const metrics = service.metrics || {};
    tiles.inflight.set(metrics.inflight);
    tiles.queued.set(metrics.queued);
    tiles.tps.set(metrics.tps);
    tiles.queued.title = metrics.source === "worker" ? t("worker 调度队列里等待的请求数") : t("已进入、还在等待首个 token 的请求数");

    const alerts = service.alerts.map((alert) => ({ key: alert.key, level: alert.level, title: (alert.scope === "node" ? alert.name + ": " : "") + alert.title, detail: alert.detail }));
    if (service.status_detail && !alerts.some((alert) => alert.key.endsWith(":degraded"))) {
      alerts.unshift({ key: "status", level: service.status === "degraded" ? "crit" : "info", title: t((SERVICE_STATUS[service.status] || SERVICE_STATUS.unknown).text), detail: service.status_detail });
    }
    sync(notes, alerts.slice(0, 4), (alert) => alert.key, (alert) => {
      const title = h("b"), text = h("p");
      const note = h("div", { class: "note" }, icon((LEVEL[alert.level] || LEVEL.info).icon), h("div", null, title, text));
      note._update = (value) => { note.dataset.tone = (LEVEL[value.level] || LEVEL.info).tone; setText(title, value.title); setText(text, value.detail); };
      return note;
    });
    notes.hidden = !alerts.length;

    const rows = [
      [t("首 token 延迟 P50 / P95"), metrics.ttft_p50 == null ? null : `${fmt.secs(metrics.ttft_p50)} / ${fmt.secs(metrics.ttft_p95)}`],
      [t("token 间延迟 P95"), metrics.itl_p95 == null ? null : fmt.secs(metrics.itl_p95)],
      [t("请求速率"), metrics.req_rate == null ? null : `${metrics.req_rate.toFixed(metrics.req_rate < 10 ? 2 : 1)} req/s`],
      [t("错误率"), metrics.err_ratio == null ? null : `${(100 * metrics.err_ratio).toFixed(2)}%`],
      [t("输入缓存命中"), metrics.cache_ratio == null ? null : fmt.pct(100 * metrics.cache_ratio)],
      [t("KV 池占用（最高）"), metrics.kv_usage == null ? null : fmt.pct(100 * metrics.kv_usage)],
      [t("Decode 运行中"), metrics.running == null ? null : fmt.int(metrics.running)],
    ].filter(([, value]) => value != null);
    kv(numbers, rows, service.stale ? t("服务已停止，没有实时指标") : t("指标需要约 20 秒的采样才会出现"));

    const registered = metrics.registered || {};
    const list = [];
    const byRole = (role) => service.components.filter((component) => component.role === role);
    for (const front of byRole("frontend")) {
      const state = front.health === "ok" ? t("健康") : front.health === "starting" ? t("尚未就绪") : front.health === "unknown" ? t("未探测") : t("无响应");
      list.push({ key: front.key, ok: front.health === "ok", wait: front.health === "starting",
        text: `Frontend (${front.node_name}:${(front.ports || {}).http || "?"}) ${state}` });
    }
    for (const role of ["prefill", "decode", "worker", "server"]) {
      const members = byRole(role);
      if (!members.length) continue;
      const ready = members.filter((component) => component.health === "ok").length;
      const unknown = members.filter((component) => component.health === "unknown").length;
      const extra = registered[role === "worker" ? "aggregated" : role];
      list.push({ key: role, ok: ready === members.length, wait: members.some((component) => component.health === "starting"),
        text: `${ROLE[role]}: ` + t("{ready} / {total} 就绪", { ready, total: members.length })
          + (unknown ? t("，{n} 个未探测", { n: unknown }) : "") + (extra != null ? t("；Frontend 已注册 {n} 个", { n: extra }) : "") });
    }
    for (const etcd of byRole("etcd")) list.push({ key: etcd.key, ok: etcd.health === "ok", text: `etcd (${etcd.node_name}:${(etcd.ports || {}).client}) ${etcd.health === "ok" ? t("健康") : t("异常")}` });
    for (const missing of service.missing) {
      list.push({ key: "m" + missing.host, ok: false, soft: true, text: missing.reason === "offline" ? t("{host} 上的实例所在节点离线", { host: missing.host }) : t("{host} 上有实例但未加入面板", { host: missing.host }) });
    }
    sync(checks, list.length ? list : [{ key: "none", ok: false, soft: true, text: t("没有可检查的组件") }], (item) => item.key, (item) => {
      const text = h("span");
      const row = h("div", { class: "check" }, h("span"), text);
      row._update = (value) => {
        row.dataset.tone = value.ok ? "good" : value.wait ? "info" : value.soft ? "serious" : "crit";
        row.firstChild.replaceWith(icon(value.ok ? "check" : value.wait ? "clock" : "alert"));
        setText(text, value.text);
      };
      return row;
    });
    kv(facts, [
      [t("地址"), service.endpoint], ["Namespace", service.namespace], ["etcd", (service.etcd || []).join(", ") || null],
      [t("模型"), service.model], [t("发现时间"), service.since ? fmt.ago(service.since, now) : null],
    ].filter(([, value]) => value), "—");

    setText(pathNote, serviceSubtitle(service));
    const groups = STAGES.map((definition) => ({
      definition,
      components: service.components.filter((component) => definition.roles.includes(component.role)),
      missing: service.missing.filter((missing) => definition.roles.includes(missing.role)),
    })).filter((group) => group.components.length || group.missing.length);
    const unplaced = service.missing.filter((missing) => !STAGES.some((definition) => definition.roles.includes(missing.role)));
    if (unplaced.length) {
      const first = groups.find((group) => group.definition.key === "frontend") || { definition: STAGES[0], components: [], missing: [] };
      if (!groups.includes(first)) groups.unshift(first);
      first.missing = first.missing.concat(unplaced);
    }
    sync(steps, groups, (group) => group.definition.key, (group) => stage(group.definition));
    resources.gpus.set(service.gpu.count);
    resources.util.set(service.gpu.util);
    resources.vram.set(service.gpu.mem == null ? null : service.gpu.mem / 1024, (value) => value.toFixed(value >= 100 ? 0 : 1));
    resources.power.set(service.gpu.power == null ? null : service.gpu.power / 1000, (value) => value.toFixed(2));
    renderCost(service);
    charts.update(service.history);
    renderFeed(feed, service.events, now, true);
  }

  async function poll() {
    if (dead) return;
    try {
      detail = await api(`/api/services/${serviceId}?window=${charts.window()}&cost=${costWindow}`);
      render(ctx.now());
      ctx.refreshChrome();
    } catch (failure) {
      if (failure.status === 404) { toast(t("该服务已不存在")); ctx.back(); return; }
    }
    timer = setTimeout(poll, 2000);
  }

  return {
    el,
    hero,
    title: () => (detail ? detail.name : (ctx.serviceById(serviceId) || {}).name) || t("服务"),
    crumb: () => [{ text: t("集群概览"), href: "#/" }, t("服务")],
    update() {},
    mount() {
      const known = ctx.serviceById(serviceId);
      if (known) { setText(name, known.name); setPill(status, SERVICE_STATUS[known.status] || SERVICE_STATUS.unknown); glyph.dataset.kind = known.kind; }
      poll();
    },
    unmount() { dead = true; clearTimeout(timer); },
  };
}
