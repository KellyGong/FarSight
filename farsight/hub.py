"""The hub: owns the node sessions and turns their samples into the state the web UI reads."""
import collections
import getpass
import json
import os
import re
import secrets
import socket
import threading
import time

from . import cost, ssh
from .events import Alerts, DEFAULT_THRESHOLDS, EventLog, node_conditions, service_conditions
from .history import Ring
from .i18n import T
from .node import FeedSession, NodeSession
from .services import Aggregator
from .topology import Topology

VERSION = "0.2.0"
NODE_COLUMNS = ["cpu", "mem", "gpu", "vram", "power", "rx", "tx"]
SERVICE_COLUMNS = ["inflight", "queued", "tps", "ttft_p95", "req_rate", "gpu", "tok_c", "tok_h", "tok_o", "cost"]
MAX_HOSTS_PER_REQUEST = 64


class RequestError(ValueError):
    """A client request that cannot be carried out; the message is shown to the user."""


def expand_hosts(text):
    """'10.0.0.[1-3,7] gpu-a, root@10.0.1.5:2222' -> list of (user or None, host, port or None)."""
    targets = []
    for token in re.split(r"[\s,;]+(?![^\[]*\])", str(text or "").strip()):
        if not token:
            continue
        user = port = None
        if "@" in token:
            user, token = token.split("@", 1)
        if token.count(":") == 1:                 # host:port; a bare IPv6 address has more than one colon
            head, _, tail = token.partition(":")
            if tail.isdigit():
                token, port = head, int(tail)
        bracket = re.search(r"\[([0-9,\-\s]+)\]", token)
        names = []
        if bracket:
            for part in bracket.group(1).split(","):
                part = part.strip()
                if "-" in part:
                    low, high = part.split("-", 1)
                    if low.isdigit() and high.isdigit() and int(low) <= int(high) and int(high) - int(low) < 256:
                        names.extend(str(value) for value in range(int(low), int(high) + 1))
                elif part.isdigit():
                    names.append(part)
            names = [token[:bracket.start()] + name + token[bracket.end():] for name in names]
        else:
            tail = re.match(r"^(\d+\.\d+\.\d+\.)(\d+)-(\d+)$", token)
            if tail and int(tail.group(2)) <= int(tail.group(3)) <= 255:
                names = [tail.group(1) + str(value) for value in range(int(tail.group(2)), int(tail.group(3)) + 1)]
            else:
                names = [token]
        for name in names:
            targets.append((user, name, port))
    return targets


def percent(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


class Hub(object):
    def __init__(self, config_dir, interval=2.0, slow=10.0, thresholds=None, demo=False, strict_host_key=False, prices=None):
        self.strict_host_key = strict_host_key
        self.config_dir = config_dir
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        self.nodes_file = os.path.join(config_dir, "nodes.json")
        self.settings_file = os.path.join(config_dir, "settings.json")
        self.prices_file = prices
        if demo and not prices:
            from . import demo as demo_data
            self.price_table = cost.parse_prices(demo_data.PRICES)
        else:
            self.price_table = cost.load_prices(prices)
        self.book = cost.PriceBook(self.price_table, self.load_settings(demo))
        self.topology_cache = None
        self.interval = interval
        self.slow = slow
        self.thresholds = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
        self.demo = demo
        self.log = EventLog()
        self.alerts = Alerts(self.log)
        self.aggregator = Aggregator(self.log)
        self.sessions = collections.OrderedDict()
        self.rings = {}
        self.service_rings = {}
        self.reported = {}            # node id -> last logged connection outcome
        self.lock = threading.RLock()
        self.state_cache = None
        self.services_cache = []
        self.owner = {}               # "node:pid" -> (service id, service name, role)
        self.seq = 0
        self.started = time.time()
        self.last_service_sample = 0.0
        self.stopped = False
        self.ticker = threading.Thread(target=self.tick_loop, name="hub-tick")
        self.ticker.daemon = True

    # -- lifecycle
    def start(self):
        if self.demo:
            from . import demo
            for config, feed in demo.cluster():
                self.attach(config, feed=feed)
        else:
            for config in self.load():
                self.attach(config)
        self.log.add("info", "system", "", T("面板"), T("面板已启动"),
                     T("演示数据") if self.demo else T("{count} 个节点", count=len(self.sessions)))
        self.ticker.start()

    def stop(self):
        self.stopped = True
        for session in list(self.sessions.values()):
            session.stop()

    def load(self):
        try:
            with open(self.nodes_file) as handle:
                data = json.load(handle)
        except (IOError, OSError, ValueError):
            return []
        return [item for item in data.get("nodes", []) if isinstance(item, dict) and item.get("id") and item.get("host")]

    def save(self):
        if self.demo:
            return
        nodes = [dict((key, value) for key, value in session.config.items() if key != "password")
                 for session in self.sessions.values()]
        temporary = self.nodes_file + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"version": 1, "nodes": nodes}, handle, indent=2, ensure_ascii=False)
        os.replace(temporary, self.nodes_file)

    # -- settings (cost)
    def load_settings(self, demo=False):
        if demo:
            from . import demo as demo_data
            return cost.clean_settings(demo_data.COST_SETTINGS)
        try:
            with open(self.settings_file) as handle:
                return cost.clean_settings((json.load(handle) or {}).get("cost") or {})
        except (IOError, OSError, ValueError):
            return dict(cost.DEFAULT_SETTINGS)

    def cost_settings(self):
        """The cost settings, the price table and the GPU models found on the nodes with their current price."""
        book = self.book
        with self.lock:
            sessions = list(self.sessions.values())
        found = collections.OrderedDict()
        for session in sessions:
            for gpu in (session.hello or {}).get("gpus") or []:
                name = gpu.get("name") or "GPU"
                entry = found.setdefault(name, {"name": name, "count": 0, "nodes": set(), "memory": gpu.get("mem_total"), "sample": gpu})
                entry["count"] += 1
                entry["nodes"].add(session.id)
        detected = []
        for entry in found.values():
            price, model, source = book.price(entry["sample"])
            matched = book.match(entry["sample"])
            detected.append({"name": entry["name"], "count": entry["count"], "nodes": len(entry["nodes"]),
                             "memory_gib": round(entry["memory"] / 1024.0, 1) if entry["memory"] else None,
                             "model": model, "table": matched["prices"] if matched else None, "price": price, "source": source})
        settings = book.settings
        return {
            "cost": {"tier": settings.get("tier") or "mid", "alpha": book.alpha, "alpha_default": self.price_table["alpha"],
                     "currency": book.currency, "currency_default": self.price_table["currency"],
                     "overrides": dict(settings.get("overrides") or {})},
            "detected": detected,
            "table": [{"model": entry["model"], "memory_gb": entry["memory_gb"], "prices": entry["prices"]} for entry in self.price_table["gpus"]],
            "updated": self.price_table.get("updated"), "prices_file": self.prices_file, "saved": not self.demo,
        }

    def update_settings(self, payload):
        try:
            settings = cost.clean_settings(payload.get("cost"), self.book.settings)
        except cost.SettingsError as error:
            raise RequestError(T("成本设置无效：{field}", field=error.args[0]))
        if not self.demo:
            temporary = self.settings_file + ".tmp"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    json.dump({"version": 1, "cost": settings}, handle, indent=2, ensure_ascii=False)
                os.replace(temporary, self.settings_file)
            except (IOError, OSError) as error:
                raise RequestError(T("无法保存设置：{error}", error=str(error)))
        self.book = cost.PriceBook(self.price_table, settings)
        self.log.add("info", "system", "", T("面板"), T("成本设置已更新"), "" if not self.demo else T("演示模式下不保存设置"))
        return self.cost_settings()

    def service_cost(self, service, inventories):
        """Rent of the service's GPUs and the cost of a million tokens at its throughput of the last minute."""
        book = self.book
        metrics = service.get("metrics") or {}
        rent = cost.service_rent(service["components"], inventories, book)
        util = (service.get("gpu") or {}).get("util")
        numbers = cost.figures(rent, metrics.get("tok_computed"), metrics.get("tok_hit"), metrics.get("tok_out"), book.alpha,
                               service.get("mode"), None if util is None else util / 100.0)
        return {"currency": book.currency, "alpha": book.alpha, "tier": book.settings.get("tier") or "mid", "rent": rent,
                "now": numbers, "source": metrics.get("tok_source"),
                "tokens": {"computed": metrics.get("tok_computed"), "hit": metrics.get("tok_hit"), "output": metrics.get("tok_out")},
                "partial": any(item.get("reason") == "unmonitored" for item in service.get("missing") or [])}

    # -- topology
    def topology_model(self):
        with self.lock:
            cached = self.topology_cache
            seq = self.seq
            if cached is not None and cached[0] == seq:
                return cached[1]
            sessions = list(self.sessions.values())
            services = list(self.services_cache)
        model = Topology([session.snapshot() for session in sessions], services)
        with self.lock:
            self.topology_cache = (seq, model)
        return model

    def topology(self):
        view = self.topology_model().cluster()
        view["dashboard"] = {"name": "demo-host" if self.demo else socket.gethostname()}
        view["now"] = time.time()
        return view

    def node_topology(self, node_id):
        self.session(node_id)
        return self.topology_model().node(node_id)

    def attach(self, config, password=None, feed=None):
        common = dict(interval=self.interval, slow=self.slow, on_message=self.on_message, on_status=self.on_status)
        if feed is not None:
            session = FeedSession(config, feed, **common)
        else:
            session = NodeSession(config, self.config_dir, password=password, strict_host_key=self.strict_host_key, **common)
        with self.lock:
            self.sessions[session.id] = session
        session.start()
        return session

    # -- session callbacks (run on session threads)
    def on_message(self, session, message, now):
        if message.get("type") != "fast":
            return
        ring = self.rings.get(session.id)
        gpus = message.get("gpus") or []
        if ring is None or len(ring.columns) != len(NODE_COLUMNS) + 2 * len(gpus):
            columns = list(NODE_COLUMNS)
            for gpu in gpus:
                columns += ["g%du" % gpu["i"], "g%dm" % gpu["i"]]
            ring = self.rings[session.id] = Ring(columns, capacity=max(300, int(3600 / self.interval)))
        memory = message.get("mem") or {}
        utils = [gpu["util"] for gpu in gpus if gpu.get("util") is not None]
        used = sum(gpu.get("mem") or 0 for gpu in gpus)
        total = sum(gpu.get("mem_total") or 0 for gpu in gpus)
        net = message.get("net") or {}
        values = {
            "cpu": message.get("cpu"), "mem": percent(memory.get("used", 0), memory.get("total")),
            "gpu": sum(utils) / len(utils) if utils else None, "vram": percent(used, total),
            "power": sum(gpu.get("power") or 0 for gpu in gpus) if gpus else None,
            "rx": net.get("rx"), "tx": net.get("tx"),
        }
        for gpu in gpus:
            values["g%du" % gpu["i"]] = gpu.get("util")
            values["g%dm" % gpu["i"]] = gpu.get("mem")
        ring.append(now, values)

    def on_status(self, session, previous, status, error):
        name = (session.hello or {}).get("hostname") or session.config.get("label") or session.config.get("host")
        if status == "online":
            self.reported[session.id] = "online"
            self.log.add("ok", "node", session.id, name, T("节点已连接"),
                         "%s@%s" % (session.config.get("user"), session.config.get("host")))
        elif status in ("error", "need_password") and error:
            signature = "error:" + error["code"]
            if self.reported.get(session.id) == signature:
                return                      # the same failure on a retry is not news
            was_online = self.reported.get(session.id) == "online"
            self.reported[session.id] = signature
            level = "crit" if was_online else "warn"
            if status != "error":
                title = error["title"]
            elif error["code"] == "disconnected":
                title = T("节点连接中断")
            else:
                title = T("节点连接中断：{title}" if was_online else "连接失败：{title}", title=error["title"])
            self.log.add(level, "node", session.id, name, title, error.get("raw") or error.get("hint") or "")

    # -- node management
    def add_nodes(self, payload):
        if payload.get("auth") == "local":       # the machine the dashboard runs on: same collector, no SSH
            targets = [(getpass.getuser(), "localhost", 22)]
        else:
            targets = expand_hosts(payload.get("hosts") or payload.get("host"))
        if not targets:
            raise RequestError(T("请填写至少一个主机地址"))
        if len(targets) > MAX_HOSTS_PER_REQUEST:
            raise RequestError(T("一次最多添加 {count} 个节点", count=MAX_HOSTS_PER_REQUEST))
        auth = payload.get("auth") or "key"
        if auth not in ("key", "password", "local"):
            raise RequestError(T("认证方式无效"))
        password = payload.get("password") if auth == "password" else None
        if auth == "password" and not password:
            raise RequestError(T("请选择密码方式时填写密码"))
        if password is not None and (not isinstance(password, str) or len(password) > 512 or "\n" in password):
            raise RequestError(T("密码格式无效"))
        label = str(payload.get("label") or "").strip()[:60]
        created, skipped = [], []
        prepared = []
        for user, host, port in targets:
            try:
                host, user, port, key_path = ssh.validate_target(
                    host, user or payload.get("user") or "root", port or payload.get("port") or 22,
                    payload.get("key_path") if auth == "key" else None)
            except ssh.TargetError as error:
                raise RequestError(T("{host}：{reason}", host=host, reason=error.args[0]))
            prepared.append((host, user, port, key_path))
        with self.lock:
            existing = set((s.config.get("host"), s.config.get("port"), s.config.get("user")) for s in self.sessions.values())
            for host, user, port, key_path in prepared:
                if (host, port, user) in existing:
                    skipped.append(host)
                    continue
                existing.add((host, port, user))
                config = {"id": secrets.token_hex(4), "host": host, "port": port, "user": user, "auth": auth,
                          "key_path": key_path, "label": label, "added": time.time()}
                if self.demo:
                    from . import demo
                    self.attach(config, feed=demo.added_node(config))
                else:
                    self.attach(config, password=password)
                created.append(config["id"])
            self.save()
        if not created and skipped:
            raise RequestError(T("节点已存在：{hosts}", hosts=", ".join(skipped)))
        return {"created": created, "skipped": skipped}

    def session(self, node_id):
        session = self.sessions.get(node_id)
        if session is None:
            raise KeyError(node_id)
        return session

    def update_node(self, node_id, payload):
        session = self.session(node_id)
        if isinstance(session, FeedSession):
            session.retry()
            return {"ok": True}
        config = dict(session.config)
        auth = payload.get("auth", config.get("auth"))
        if auth not in ("key", "password", "local"):
            raise RequestError(T("认证方式无效"))
        password = payload.get("password") if auth == "password" else None
        if password is not None and (not isinstance(password, str) or len(password) > 512 or "\n" in password):
            raise RequestError(T("密码格式无效"))
        try:
            host, user, port, key_path = ssh.validate_target(
                payload.get("host", config["host"]), payload.get("user", config["user"]), payload.get("port", config["port"]),
                payload.get("key_path", config.get("key_path")) if auth == "key" else None)
        except ssh.TargetError as error:
            raise RequestError(error.args[0])
        with self.lock:
            for other in self.sessions.values():
                if other.id != node_id and (other.config.get("host"), other.config.get("port"), other.config.get("user")) == (host, port, user):
                    raise RequestError(T("已存在相同地址的节点"))
            changes = {"host": host, "user": user, "port": port, "auth": auth, "key_path": key_path}
            if "label" in payload:
                changes["label"] = str(payload.get("label") or "").strip()[:60]
            if auth == "password" and not password and not session.password:
                raise RequestError(T("请填写密码"))
            if auth != "password":
                session.password = None
            self.reported.pop(node_id, None)
            session.reconfigure(changes, password=password)
            self.save()
        return {"ok": True}

    def retry_node(self, node_id):
        session = self.session(node_id)
        self.reported.pop(node_id, None)
        session.retry()
        return {"ok": True}

    def remove_node(self, node_id):
        with self.lock:
            session = self.session(node_id)
            name = (session.hello or {}).get("hostname") or session.config.get("host")
            del self.sessions[node_id]
            self.rings.pop(node_id, None)
            self.reported.pop(node_id, None)
            self.alerts.drop_scope("node", node_id)
            self.save()
        session.on_status = None
        session.stop()
        self.log.add("info", "node", node_id, name, T("节点已移除"), "")
        return {"ok": True}

    # -- summaries
    def node_summary(self, snapshot):
        config = snapshot["config"]
        hello = snapshot.get("hello") or {}
        online = snapshot["status"] == "online"
        fast = snapshot.get("fast") or {} if online else {}
        gpus = fast.get("gpus") or []
        inventory = hello.get("gpus") or []
        models = collections.Counter(gpu.get("name") for gpu in inventory)
        utils = [gpu["util"] for gpu in gpus if gpu.get("util") is not None]
        used = sum(gpu.get("mem") or 0 for gpu in gpus)
        total = sum(gpu.get("mem_total") or 0 for gpu in gpus)
        memory = fast.get("mem") or {}
        temps = [gpu["temp"] for gpu in gpus if gpu.get("temp") is not None]
        error = snapshot.get("error")
        return {
            "id": config["id"], "host": config["host"], "port": config["port"], "user": config["user"],
            "auth": config["auth"], "label": config.get("label") or "",
            "name": hello.get("hostname") or config.get("label") or config["host"],
            "status": snapshot["status"], "since": snapshot["since"],
            "error": None if not error else {"code": error["code"], "title": error["title"], "hint": error["hint"],
                                             "raw": error.get("raw", ""), "retry": error.get("retry", False)},
            "next_retry": snapshot.get("next_retry"), "attempts": snapshot.get("attempts", 0),
            "gpu_model": models.most_common(1)[0][0] if models else None, "gpu_count": len(inventory),
            "cores": hello.get("cores"), "mem_total": hello.get("mem_total"),
            "cpu": fast.get("cpu"), "load": (fast.get("load") or [None])[0],
            "mem": percent(memory.get("used", 0), memory.get("total")) if memory else None,
            "gpu": round(sum(utils) / len(utils), 1) if utils else None,
            "vram": percent(used, total) if gpus else None, "vram_used": used if gpus else None, "vram_total": total if gpus else None,
            "power": round(sum(gpu.get("power") or 0 for gpu in gpus)) if gpus else None,
            "temp": max(temps) if temps else None,
            "gpus": [[gpu.get("util"), percent(gpu.get("mem") or 0, gpu.get("mem_total"))] for gpu in gpus],
            "updated": snapshot.get("fast_at"),
        }

    def tick_loop(self):
        while not self.stopped:
            started = time.time()
            try:
                self.tick()
            except Exception as error:      # keep serving the last good state
                self.log.add("warn", "system", "", T("面板"), T("状态汇总出错"), "%s: %s" % (type(error).__name__, error))
            time.sleep(max(0.2, 1.0 - (time.time() - started)))

    def tick(self):
        now = time.time()
        with self.lock:
            sessions = list(self.sessions.values())
        snapshots = [session.snapshot() for session in sessions]
        services = self.aggregator.build(snapshots, now)
        owner = {}
        on_node = collections.defaultdict(list)
        for service in services:
            if service.get("stale"):
                continue
            for component in service["components"]:
                owner[component["key"]] = (service["id"], service["name"], component["role"])
            for node_id in service["nodes"]:
                on_node[node_id].append(service["id"])
        conditions = []
        nodes = []
        for snapshot in snapshots:
            summary = self.node_summary(snapshot)
            summary["services"] = on_node.get(summary["id"], [])
            nodes.append(summary)
            if snapshot["status"] == "online":
                conditions.extend(node_conditions(snapshot, summary, self.thresholds))
        for service in services:
            if service.get("stale"):
                self.alerts.drop_scope("service", service["id"])     # gone, not recovered: no "recovered" entry
            else:
                conditions.extend(service_conditions(service, self.thresholds))
        alerts = self.alerts.evaluate(conditions, now)
        inventories = dict((snapshot["config"]["id"], (snapshot.get("hello") or {}).get("gpus") or []) for snapshot in snapshots)
        for service in services:
            if service.get("stale"):
                service.pop("cost", None)
            else:
                service["cost"] = self.service_cost(service, inventories)
        count = collections.Counter((alert["scope"], alert["scope_id"]) for alert in alerts if alert["level"] != "info")
        for summary in nodes:
            summary["alerts"] = count.get(("node", summary["id"]), 0)
        if now - self.last_service_sample >= max(self.interval, 5.0):
            self.last_service_sample = now
            for service in services:
                if service.get("stale"):
                    continue
                ring = self.service_rings.get(service["id"])
                if ring is None:
                    ring = self.service_rings[service["id"]] = Ring(SERVICE_COLUMNS, capacity=720)
                metrics = service["metrics"]
                ring.append(now, {"inflight": metrics.get("inflight"), "queued": metrics.get("queued"), "tps": metrics.get("tps"),
                                  "ttft_p95": metrics.get("ttft_p95"), "req_rate": metrics.get("req_rate"),
                                  "gpu": service["gpu"].get("util"), "tok_c": metrics.get("tok_computed"),
                                  "tok_h": metrics.get("tok_hit"), "tok_o": metrics.get("tok_out"),
                                  "cost": service["cost"]["now"]["blended"]})
            for sid in [sid for sid in self.service_rings if sid not in self.aggregator.known]:
                del self.service_rings[sid]
        cards = []
        for service in services:
            card = dict((key, value) for key, value in service.items() if key not in ("components", "key", "cost"))
            card["alerts"] = count.get(("service", service["id"]), 0)
            summary = service.get("cost")
            card["cost"] = None if not summary else {"currency": summary["currency"], "rent": summary["rent"]["total"],
                                                     "blended": summary["now"]["blended"], "unpriced": summary["rent"]["unpriced"]}
            cards.append(card)
        online = sum(1 for summary in nodes if summary["status"] == "online")
        self.seq += 1
        state = {
            "seq": self.seq, "now": now, "version": VERSION, "demo": self.demo, "interval": self.interval,
            "summary": {
                "nodes": len(nodes), "online": online,
                "failed": sum(1 for summary in nodes if summary["status"] in ("error", "need_password") or (summary["status"] == "connecting" and summary["error"])),
                "connecting": sum(1 for summary in nodes if summary["status"] == "connecting" and not summary["error"]),
                "gpus": sum(summary["gpu_count"] for summary in nodes if summary["status"] == "online"),
                "services": sum(1 for service in services if service["status"] != "stopped"),
                "crit": sum(1 for alert in alerts if alert["level"] == "crit"),
                "warn": sum(1 for alert in alerts if alert["level"] == "warn"),
            },
            "services": cards, "nodes": nodes, "alerts": alerts, "events": self.log.recent(40),
        }
        with self.lock:
            self.state_cache = state
            self.services_cache = services
            self.owner = owner

    # -- API views
    def state(self):
        with self.lock:
            state = self.state_cache
        return state or {"seq": 0, "now": time.time(), "version": VERSION, "demo": self.demo, "interval": self.interval,
                         "summary": {"nodes": len(self.sessions), "online": 0, "failed": 0, "connecting": len(self.sessions),
                                     "gpus": 0, "services": 0, "crit": 0, "warn": 0},
                         "services": [], "nodes": [], "alerts": [], "events": []}

    def node_detail(self, node_id, window=900):
        session = self.session(node_id)
        snapshot = session.snapshot()
        summary = self.node_summary(snapshot)
        hello = snapshot.get("hello") or {}
        online = snapshot["status"] == "online"
        fast = snapshot.get("fast") or {}
        slow = snapshot.get("slow") or {}
        with self.lock:
            owner = dict(self.owner)
            services = [service for service in self.services_cache if node_id in service["nodes"] and not service.get("stale")]
            alerts = [alert for alert in self.alerts.current() if alert["scope"] == "node" and alert["scope_id"] == node_id]
        containers = slow.get("containers") or {}
        processes = collections.defaultdict(list)
        for process in slow.get("gpu_procs") or []:
            owned = owner.get("%s:%s" % (node_id, process.get("root"))) if process.get("root") else None
            processes[process.get("gpu")].append({
                "pid": process["pid"], "name": process.get("name"), "user": process.get("user"), "mem": process.get("mem"),
                "container": (containers.get(process.get("cid") or "") or {}).get("name") or process.get("cid"),
                "service": owned[0] if owned else None, "service_name": owned[1] if owned else None,
                "role": owned[2] if owned else None,
            })
        live = dict((gpu["i"], gpu) for gpu in fast.get("gpus") or []) if online else {}
        gpus = []
        for item in hello.get("gpus") or []:
            sample = live.get(item["index"], {})
            gpus.append({
                "index": item["index"], "name": item.get("name"), "uuid": item.get("uuid"),
                "util": sample.get("util"), "mem": sample.get("mem"), "mem_total": sample.get("mem_total") or item.get("mem_total"),
                "temp": sample.get("temp"), "power": sample.get("power"), "power_limit": sample.get("power_limit"),
                "procs": sorted(processes.get(item["index"], []), key=lambda entry: -(entry.get("mem") or 0)),
            })
        consumers = collections.OrderedDict()
        for index, entries in processes.items():
            for entry in entries:
                key = entry.get("service_name") or entry.get("container") or entry.get("user") or T("未知")
                bucket = consumers.setdefault(key, {"name": key, "service": entry.get("service"), "mem": 0, "gpus": set(),
                                                    "kind": "service" if entry.get("service") else ("container" if entry.get("container") else "user"),
                                                    "user": entry.get("user")})
                bucket["mem"] += entry.get("mem") or 0
                bucket["gpus"].add(index)
        consumers = sorted(({"name": c["name"], "service": c["service"], "mem": c["mem"], "gpus": sorted(g for g in c["gpus"] if g is not None),
                             "kind": c["kind"], "user": c["user"]} for c in consumers.values()), key=lambda item: -item["mem"])
        ring = self.rings.get(node_id)
        scope = set([("node", node_id)])
        return {
            "node": summary,
            "host": {
                "hostname": hello.get("hostname"), "os": hello.get("os"), "kernel": hello.get("kernel"), "arch": hello.get("arch"),
                "cpu_model": hello.get("cpu_model"), "cores": hello.get("cores"), "mem_total": hello.get("mem_total"),
                "boot_time": hello.get("boot_time"), "python": hello.get("python"), "remote_user": hello.get("user"),
                "driver": (hello.get("gpus") or [{}])[0].get("driver"), "collector": hello.get("v"), "ips": (hello.get("ips") or [])[:8],
                "caps": hello.get("caps"), "connected_at": snapshot.get("connected_at"),
            },
            "live": {"cpu": fast.get("cpu"), "cores": fast.get("cores") or [], "load": fast.get("load") or [],
                     "mem": fast.get("mem") or {}, "net": fast.get("net"), "running": fast.get("running")} if online else None,
            "gpus": gpus, "consumers": consumers,
            "disks": slow.get("disks") or [],
            "system": {"processes": slow.get("processes"), "zombies": slow.get("zombies"), "sglang_zombies": slow.get("sglang_zombies"),
                       "docker": slow.get("docker"), "slow_ms": slow.get("took_ms"), "errors": dict(slow.get("errors") or {}, **(fast.get("errors") or {})),
                       "collector_errors": snapshot.get("collector_errors")},
            "services": [{"id": s["id"], "name": s["name"], "status": s["status"], "kind": s["kind"],
                          "roles": sorted(set(c["role"] for c in s["components"] if c["node"] == node_id))} for s in services],
            "alerts": alerts, "events": self.log.recent(30, scope_ids=scope),
            "history": ring.query(since=time.time() - window, points=180, columns=NODE_COLUMNS) if ring is not None else {"t": []},
        }

    def gpu_history(self, node_id, index, window=900):
        ring = self.rings.get(node_id)
        if ring is None:
            return {"t": []}
        return ring.query(since=time.time() - window, points=180, columns=["g%du" % index, "g%dm" % index])

    def service_detail(self, service_id, window=900, cost_window=300):
        with self.lock:
            service = next((item for item in self.services_cache if item["id"] == service_id), None)
            alerts = [alert for alert in self.alerts.current()
                      if (alert["scope"] == "service" and alert["scope_id"] == service_id)
                      or (service and alert["scope"] == "node" and alert["scope_id"] in service["nodes"])]
            sessions = dict(self.sessions)
        if service is None:
            raise KeyError(service_id)
        components = []
        for component in service["components"]:
            session = sessions.get(component["node"])
            snapshot = session.snapshot() if session is not None else None
            live = {}
            if snapshot and snapshot["status"] == "online":
                live = dict((gpu["i"], gpu) for gpu in (snapshot.get("fast") or {}).get("gpus") or [])
            cards = []
            for index in component["gpus"]:
                sample = live.get(index, {})
                cards.append({"index": index, "util": sample.get("util"), "mem": sample.get("mem"), "mem_total": sample.get("mem_total"),
                              "temp": sample.get("temp"), "power": sample.get("power"),
                              "own_mem": (component.get("gpu_mem") or {}).get(index)})
            view = dict((key, component.get(key)) for key in (
                "key", "node", "node_name", "host", "kind", "role", "backend", "pid", "user", "started", "container", "image",
                "ports", "health", "health_text", "endpoints", "gpus", "metrics", "cmdline"))
            view["gpu_cards"] = cards
            view["gpu"] = component.get("gpu")
            view["node_status"] = snapshot["status"] if snapshot else "removed"
            view["node_load"] = None
            if snapshot and snapshot["status"] == "online":
                fast = snapshot.get("fast") or {}
                memory = fast.get("mem") or {}
                view["node_load"] = {"cpu": fast.get("cpu"), "mem": percent(memory.get("used", 0), memory.get("total"))}
            components.append(view)
        ring = self.service_rings.get(service_id)
        scope = set([("service", service_id)] + [("node", node_id) for node_id in service["nodes"]])
        result = dict((key, value) for key, value in service.items() if key not in ("components", "key"))
        summary = service.get("cost")
        if summary and cost_window <= 60:
            # The rates behind "now" already are averages over the last minute.
            result["cost"] = dict(summary, window=dict(summary["now"], seconds=60, span=60, tokens=summary["tokens"]))
        elif summary and ring is not None:
            mean = ring.mean(time.time() - cost_window, ["tok_c", "tok_h", "tok_o", "gpu"])
            summary = dict(summary, window=dict(
                cost.figures(summary["rent"], mean["tok_c"], mean["tok_h"], mean["tok_o"], summary["alpha"], service.get("mode"),
                             None if mean["gpu"] is None else mean["gpu"] / 100.0),
                seconds=cost_window, span=round(mean["span"]), tokens={"computed": mean["tok_c"], "hit": mean["tok_h"], "output": mean["tok_o"]}))
            result["cost"] = summary
        result["components"] = components
        result["alerts"] = alerts
        result["events"] = self.log.recent(30, scope_ids=scope)
        result["history"] = ring.query(since=time.time() - window, points=180) if ring is not None else {"t": []}
        result["node_names"] = dict((node_id, (sessions[node_id].hello or {}).get("hostname") or sessions[node_id].config.get("host"))
                                    for node_id in service["nodes"] if node_id in sessions)
        return result
