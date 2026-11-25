"""Group the service components reported by the nodes into services and derive service-level metrics.

A Dynamo service is the set of components that share a discovery backend and namespace (the pair
ETCD_ENDPOINTS + DYN_NAMESPACE): one or more frontends, the prefill / decode (or aggregated) workers and the
etcd they register in. The frontend's /health instance list tells which hosts the workers run on, so hosts
that are not monitored yet can be pointed out. A native SGLang server is a service of its own.
"""
import collections
import hashlib
import re

from .i18n import Joined, T

# <deployment>-[sglang-|vllm-]<role>[-suffix]: a container naming scheme that tells the deployment and the role.
CONTAINER_RE = re.compile(r"^(?P<deployment>.+?)-(?:(?:sglang|vllm)-)?(?P<role>prefill|decode|frontend)(?:-.*)?$")
LOOPBACK = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "")
ROLE_ORDER = {"frontend": 0, "router": 0, "prefill": 1, "decode": 2, "worker": 2, "server": 2, "etcd": 9}
KEEP_STOPPED = 900.0
MINOR_SHARE = 0.1      # a GPU where a component holds under 10% of its own per-card peak is not "its" GPU
INFINITY = float("inf")
# Cumulative series in the collector's metric summaries; everything else there is a gauge.
COUNTERS = ("requests", "errors", "output_tokens", "input_tokens", "cached_tokens", "prompt_tokens", "generation_tokens",
            "computed_tokens", "hit_tokens", "uncached_tokens")


def quantile(q, older, newer):
    """Quantile of the observations made between two cumulative bucket lists [[upper bound, count], ...].

    Linear interpolation inside the bucket, as Prometheus' histogram_quantile does. None without observations.
    """
    before = dict((str(edge), count) for edge, count in (older or []))
    deltas = []
    for edge, count in newer or []:
        upper = INFINITY if str(edge) in ("+Inf", "inf") else float(edge)
        deltas.append((upper, count - before.get(str(edge), 0.0)))
    deltas.sort()
    if not deltas or deltas[-1][1] <= 0:
        return None
    rank = q * deltas[-1][1]
    lower, below = 0.0, 0.0
    for upper, count in deltas:
        if count >= rank:
            if upper == INFINITY:
                return lower
            if count <= below:
                return upper
            return lower + (upper - lower) * (rank - below) / (count - below)
        lower, below = upper, count
    return lower


class Tracker(object):
    """Recent cumulative samples per component: counters become rates, histogram buckets become quantiles."""

    def __init__(self, horizon=180.0):
        self.horizon = horizon
        self.series = {}

    def add(self, key, stamp, counters, histograms=None):
        queue = self.series.setdefault(key, collections.deque())
        if queue and stamp <= queue[-1][0]:
            return
        if queue and any(value < queue[-1][1].get(name, 0.0) for name, value in counters.items()):
            queue.clear()          # the process restarted and its counters started again from zero
        queue.append((stamp, dict(counters), histograms or {}))
        while queue and stamp - queue[0][0] > self.horizon:
            queue.popleft()

    def span(self, key, window):
        queue = self.series.get(key)
        if not queue or len(queue) < 2:
            return None
        newest = queue[-1]
        oldest = newest
        for sample in queue:
            if newest[0] - sample[0] <= window:
                oldest = sample
                break
        if newest[0] - oldest[0] < 5.0:
            return None
        return oldest, newest

    def rate(self, key, name, window=60.0):
        pair = self.span(key, window)
        if not pair or name not in pair[0][1] or name not in pair[1][1]:
            return None
        return max(0.0, (pair[1][1][name] - pair[0][1][name]) / (pair[1][0] - pair[0][0]))

    def delta(self, key, name, window=60.0):
        pair = self.span(key, window)
        if not pair or name not in pair[0][1] or name not in pair[1][1]:
            return None
        return max(0.0, pair[1][1][name] - pair[0][1][name])

    def quantiles(self, key, name, fractions, window=60.0):
        pair = self.span(key, window)
        if not pair or name not in pair[1][2]:
            return [None for _ in fractions]
        older = (pair[0][2].get(name) or {}).get("buckets")
        newer = pair[1][2][name].get("buckets")
        return [quantile(fraction, older, newer) for fraction in fractions]

    def prune(self, alive):
        for key in [key for key in self.series if key not in alive]:
            del self.series[key]


def split_endpoints(value):
    """'http://a:2379,http://b:2379' -> [("a", "2379"), ("b", "2379")]."""
    result = []
    for item in re.split(r"[,\s]+", value or ""):
        item = re.sub(r"^[a-zA-Z]+://", "", item.strip()).rstrip("/")
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep:
            host, port = item, "2379"
        result.append((host, port))
    return result


def service_id(key):
    return "svc-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def short_model(name):
    if not name:
        return None
    return name.rstrip("/").split("/")[-1]


def gpu_stats(indices, fast):
    """Aggregate load of the given GPU indices from a node's latest fast sample."""
    chosen = [gpu for gpu in (fast or {}).get("gpus") or [] if gpu.get("i") in indices]
    if not chosen:
        return {"count": len(indices), "util": None, "mem": None, "mem_total": None, "power": None, "temp": None}
    utils = [gpu["util"] for gpu in chosen if gpu.get("util") is not None]
    temps = [gpu["temp"] for gpu in chosen if gpu.get("temp") is not None]
    return {
        "count": len(chosen),
        "util": round(sum(utils) / len(utils), 1) if utils else None,
        "mem": sum(gpu.get("mem") or 0 for gpu in chosen),
        "mem_total": sum(gpu.get("mem_total") or 0 for gpu in chosen),
        "power": round(sum(gpu.get("power") or 0 for gpu in chosen), 1),
        "temp": max(temps) if temps else None,
    }


def merge_gpu(parts):
    parts = [part for part in parts if part and part.get("count")]
    count = sum(part["count"] for part in parts)
    weighted = [(part["util"], part["count"]) for part in parts if part.get("util") is not None]
    weight = sum(size for _, size in weighted)
    return {
        "count": count,
        "util": round(sum(util * size for util, size in weighted) / weight, 1) if weight else None,
        "mem": sum(part.get("mem") or 0 for part in parts) if parts else None,
        "mem_total": sum(part.get("mem_total") or 0 for part in parts) if parts else None,
        "power": round(sum(part.get("power") or 0 for part in parts), 1) if parts else None,
    }


class Aggregator(object):
    def __init__(self, log=None):
        self.log = log
        self.tracker = Tracker()
        self.known = {}        # service id -> {"service": last public dict, "seen": time, "ready": set of component keys}
        self.first_seen = {}
        self.placed = {}       # component key -> grouping key it was last placed under with firm evidence

    # -- component view
    def component(self, snapshot, raw, now):
        node_id = snapshot["config"]["id"]
        slow = snapshot["slow"]
        hello = snapshot.get("hello") or {}
        container = (slow.get("containers") or {}).get(raw.get("cid") or "") or {}
        kind = raw["kind"]
        if kind == "dynamo-frontend":
            role = "frontend"
        elif kind == "dynamo-worker":
            role = raw.get("role") or "worker"
        elif kind == "sglang-server":
            role = raw.get("role") or "server"
        elif kind == "sglang-router":
            role = "router"
        else:
            role = kind
        probe = dict(raw.get("probe") or {})
        probe["_at"] = snapshot.get("slow_at")      # counters are sampled once per slow tick, not once per build
        health = probe.get("health") or {}
        status = str(health.get("status", "")).lower()
        age = now - raw["started"] if raw.get("started") else None
        if probe.get("skipped") or (not health and not probe.get("error")):
            state = "unknown"
        elif probe.get("error") and not health:
            state = "starting" if age is None or age < 1800 else "error"
        elif status in ("healthy", "ready"):
            state = "ok"
        elif status in ("notready", "starting", "unhealthy") or status.startswith("http 503"):
            state = "starting"
        else:
            state = "error"
        # GPUs the component computes on. Helper processes of the same tree (KV transfer agents, ...) keep a small
        # context on every card; a card counts only when the tree holds a meaningful share of memory there.
        held = collections.defaultdict(float)
        for process in slow.get("gpu_procs") or []:
            if process.get("root") == raw["pid"] and process.get("gpu") is not None:
                held[process["gpu"]] += process.get("mem") or 0.0
        peak = max(held.values()) if held else 0.0
        indices = sorted(index for index, amount in held.items() if amount >= MINOR_SHARE * peak)
        if not indices and kind in ("dynamo-worker", "sglang-server"):
            devices = [device for device in container.get("devices") or [] if str(device).isdigit()]
            total = len(hello.get("gpus") or [])
            if devices and len(devices) < total:
                indices = sorted(int(device) for device in devices)
        memory = dict((index, held[index]) for index in indices if index in held)
        return {
            "key": "%s:%s" % (node_id, raw["pid"]), "node": node_id,
            "node_name": hello.get("hostname") or snapshot["config"].get("host"),
            "host": snapshot["config"].get("host"),
            "kind": kind, "role": role, "backend": raw.get("backend"), "pid": raw["pid"], "user": raw.get("user"),
            "started": raw.get("started"), "container": container.get("name") or raw.get("cid"),
            "image": container.get("image"), "ports": raw.get("ports") or {}, "cmdline": raw.get("cmdline"),
            "health": state, "health_text": health.get("status") or probe.get("error") or "",
            "endpoints": health.get("endpoints"), "gpus": indices, "gpu_mem": memory,
            "gpu": gpu_stats(indices, snapshot.get("fast")),
            "env": raw.get("env") or {}, "discovery": raw.get("discovery"), "probe": probe,
            "settings": raw.get("settings") or {}, "procs": raw.get("procs") or [],
            "model": raw.get("model"), "router": raw.get("router"),
            "prefill_urls": raw.get("prefill"), "decode_urls": raw.get("decode"),
        }

    # -- grouping
    def dynamo_key(self, component):
        env = component["env"]
        namespace = env.get("DYN_NAMESPACE") or ""
        health = component["probe"].get("health") or {}
        if not namespace and health.get("instances"):
            names = collections.Counter(item.get("namespace") for item in health["instances"] if item.get("namespace"))
            if len(names) == 1:
                namespace = list(names)[0]
        if (component.get("discovery") or env.get("DYN_DISCOVERY_BACKEND")) == "file":
            return "dynamo|file:%s|%s" % (component["node"], namespace)
        endpoints = []
        for host, port in split_endpoints(env.get("ETCD_ENDPOINTS")):
            endpoints.append("%s:%s" % (component["host"] if host in LOOPBACK else host, port))
        if endpoints:
            return "dynamo|%s|%s" % (",".join(sorted(endpoints)), namespace)
        match = CONTAINER_RE.match(component.get("container") or "")
        if match:
            return "dynamo|name:%s|" % match.group("deployment")
        return "dynamo|node:%s|%s" % (component["node"], namespace)

    def build(self, snapshots, now):
        """Return the public list of services for the current node snapshots."""
        online = [snapshot for snapshot in snapshots if snapshot["status"] == "online" and snapshot.get("slow")]
        address = {}       # IP or configured host -> node id
        offline = set()
        for snapshot in snapshots:
            node_id = snapshot["config"]["id"]
            address[snapshot["config"].get("host")] = node_id
            for ip in (snapshot.get("hello") or {}).get("ips") or []:
                address.setdefault(ip, node_id)
            if snapshot["status"] != "online":
                offline.add(node_id)
        components = []
        for snapshot in online:
            for raw in snapshot["slow"].get("components") or []:
                components.append((snapshot, self.component(snapshot, raw, now)))

        groups = collections.OrderedDict()
        etcds = []
        routers = []
        servers = []
        for snapshot, component in components:
            kind = component["kind"]
            if kind in ("dynamo-frontend", "dynamo-worker"):
                key = self.dynamo_key(component)
                # A process that is exiting loses its environment and its container before it disappears. It stays
                # in the service it was placed in rather than showing up for a moment as a service of its own.
                if key.split("|")[1].startswith(("name:", "node:")):
                    key = self.placed.get(component["key"], key)
                else:
                    self.placed[component["key"]] = key
                groups.setdefault(key, []).append(component)
            elif kind == "etcd":
                etcds.append(component)
            elif kind == "sglang-router":
                routers.append(component)
            elif kind == "sglang-server":
                servers.append(component)
        # A frontend whose namespace is unknown belongs to the only service on the same etcd, if there is one.
        for key in [key for key in groups if key.startswith("dynamo|") and key.endswith("|") and "|name:" not in key and "|node:" not in key]:
            prefix = key[:-1]
            siblings = [other for other in groups if other != key and other.startswith(prefix + "|")]
            if len(siblings) == 1:
                groups[siblings[0]].extend(groups.pop(key))
        # etcd joins every service registered in it
        for etcd in etcds:
            endpoint = "%s:%s" % (etcd["host"], etcd["ports"].get("client"))
            ips = set(ip for ip, node_id in address.items() if node_id == etcd["node"])
            for key, members in groups.items():
                parts = key.split("|")
                if len(parts) < 3 or parts[1].startswith(("file:", "name:", "node:")):
                    continue
                for item in parts[1].split(","):
                    host, _, port = item.rpartition(":")
                    if port == str(etcd["ports"].get("client")) and (host in ips or item == endpoint):
                        members.append(etcd)
                        break
        # native SGLang: servers behind a router form one service, the others stand alone
        claimed = set()
        for router in routers:
            members = [router]
            for url in (router.get("prefill_urls") or []) + (router.get("decode_urls") or []):
                for host, port in split_endpoints(url):
                    for server in servers:
                        if str(server["ports"].get("http")) == port and address.get(host) == server["node"]:
                            members.append(server)
                            claimed.add(server["key"])
            groups["sglang|router:%s:%s" % (router["node"], router["ports"].get("http"))] = members
        for server in servers:
            if server["key"] not in claimed:
                groups["sglang|%s:%s" % (server["node"], server["ports"].get("http"))] = [server]

        services = []
        alive = set()
        for key, members in groups.items():
            service = self.service(key, members, address, offline, now)
            services.append(service)
            for member in members:
                alive.add(member["key"])
        self.tracker.prune(alive)
        self.placed = dict((key, value) for key, value in self.placed.items() if key in alive)
        services = self.reconcile(services, offline, now)
        order = {"degraded": 0, "starting": 1, "running": 2, "unknown": 3, "stopped": 4}
        services.sort(key=lambda item: (order.get(item["status"], 5), item["name"]))
        return services

    def total(self, members, name, window=60.0):
        """Sum of a counter's rate over `members`; None when none of them has it."""
        rates = [self.tracker.rate(member["key"], name, window) for member in members]
        rates = [rate for rate in rates if rate is not None]
        return sum(rates) if rates else None

    def token_rates(self, mode, fronts, measured, output):
        """Tokens per second by class: input the prefill computed, input served from a cache, output.

        The engine's own split is used when it exports one; older engines only tell cached from uncached prompt
        tokens; without worker metrics the frontend's counters are used.
        """
        if mode == "disagg":
            inputs = [member for member in measured if member["role"] == "prefill"]
        else:
            inputs = [member for member in measured if member["role"] != "prefill"]
        computed, hit, source = self.total(inputs, "computed_tokens"), self.total(inputs, "hit_tokens"), "engine"
        if computed is None:
            uncached, prompt = self.total(inputs, "uncached_tokens"), self.total(inputs, "prompt_tokens")
            if uncached is not None and prompt is not None:
                computed, hit, source = uncached, max(0.0, prompt - uncached), "engine"
        if computed is None and fronts:
            total, cached = self.total(fronts, "input_tokens"), self.total(fronts, "cached_tokens")
            if total is not None:
                hit = min(cached or 0.0, total)
                computed, source = total - hit, "frontend"
        if computed is None:
            prompt = self.total(inputs, "prompt_tokens")
            if prompt is not None:
                computed, hit, source = prompt, None, "prompt"
        return {"tok_computed": computed, "tok_hit": hit, "tok_out": output, "tok_source": source if computed is not None else None}

    # -- one service
    def service(self, key, members, address, offline, now):
        sid = service_id(key)
        kind = key.split("|")[0]
        members = sorted(members, key=lambda item: (ROLE_ORDER.get(item["role"], 5), item["node_name"] or "", item["pid"]))
        frontends = [member for member in members if member["role"] == "frontend"]
        workers = [member for member in members if member["role"] in ("prefill", "decode", "worker", "server")]
        counts = collections.Counter(member["role"] for member in members)
        parts = key.split("|")
        namespace = parts[2] if kind == "dynamo" and len(parts) > 2 else None
        etcd = parts[1].split(",") if kind == "dynamo" and not parts[1].startswith(("file:", "name:", "node:")) else []
        mode = "disagg" if counts.get("prefill") or counts.get("decode") else "agg"

        # metrics
        for member in members:
            metrics = member["probe"].get("metrics")
            if not metrics:
                continue
            counters = dict((name, metrics[name]) for name in COUNTERS if isinstance(metrics.get(name), (int, float)))
            histograms = dict((name, value) for name, value in metrics.items() if isinstance(value, dict) and "buckets" in value)
            self.tracker.add(member["key"], member["probe"].get("_at") or now, counters, histograms)
        metrics = {"source": None}
        fronts = [member for member in frontends if member["probe"].get("metrics")]
        if fronts:
            metrics["source"] = "frontend"
            metrics["inflight"] = sum(member["probe"]["metrics"].get("inflight", 0) for member in fronts)
            metrics["queued"] = sum(member["probe"]["metrics"].get("queued", 0) for member in fronts)
            rates = [self.tracker.rate(member["key"], "requests") for member in fronts]
            errors = [self.tracker.rate(member["key"], "errors") for member in fronts]
            tokens = [self.tracker.rate(member["key"], "output_tokens") for member in fronts]
            metrics["req_rate"] = sum(rate for rate in rates if rate is not None) if any(rate is not None for rate in rates) else None
            metrics["tps"] = sum(rate for rate in tokens if rate is not None) if any(rate is not None for rate in tokens) else None
            if metrics["req_rate"]:
                metrics["err_ratio"] = sum(rate for rate in errors if rate is not None) / metrics["req_rate"]
            else:
                metrics["err_ratio"] = None
            first = fronts[0]["key"]
            metrics["ttft_p50"], metrics["ttft_p95"] = self.tracker.quantiles(first, "ttft", (0.5, 0.95))
            metrics["itl_p50"], metrics["itl_p95"] = self.tracker.quantiles(first, "itl", (0.5, 0.95))
            cached = self.tracker.delta(first, "cached_tokens")
            prompt = self.tracker.delta(first, "input_tokens")
            metrics["cache_ratio"] = min(1.0, cached / prompt) if cached is not None and prompt else None
            registered = collections.Counter()
            for member in fronts:
                registered.update(member["probe"]["metrics"].get("workers") or {})
            metrics["registered"] = dict(registered)
        measured = [member for member in workers if (member["probe"].get("metrics") or {}).get("has_sglang")]
        if measured:
            decoders = [member for member in measured if member["role"] != "prefill"] or measured
            metrics["running"] = sum(member["probe"]["metrics"].get("running", 0) for member in decoders)
            metrics["worker_queued"] = sum(member["probe"]["metrics"].get("queued", 0) for member in measured)
            usage = [member["probe"]["metrics"].get("kv_usage") for member in measured if member["probe"]["metrics"].get("kv_usage") is not None]
            metrics["kv_usage"] = max(usage) if usage else None
            if not fronts:
                metrics["source"] = "worker"
                metrics["inflight"] = metrics["running"]
                metrics["queued"] = metrics["worker_queued"]
                rates = [self.tracker.rate(member["key"], "generation_tokens") for member in decoders]
                metrics["tps"] = sum(rate for rate in rates if rate is not None) if any(rate is not None for rate in rates) else None
        metrics.update(self.token_rates(mode, fronts, measured, metrics.get("tps")))
        for member in members:
            summary = member["probe"].get("metrics") or {}
            member["metrics"] = dict((name, summary.get(name)) for name in
                                     ("running", "queued", "kv_usage", "gen_throughput", "cache_hit_rate", "inflight", "dp_ranks")
                                     if summary.get(name) is not None)
            if member["role"] in ("prefill", "decode", "worker", "server") and summary.get("has_sglang"):
                counter = "prompt_tokens" if member["role"] == "prefill" else "generation_tokens"
                member["metrics"]["tps"] = self.tracker.rate(member["key"], counter)

        # topology: hosts named by the frontend that are not monitored, or whose node is offline
        missing = collections.OrderedDict()
        for member in frontends:
            for item in (member["probe"].get("health") or {}).get("instances") or []:
                host = item.get("host")
                if not host:
                    continue
                node_id = address.get(host)
                role = "prefill" if item.get("component") == "prefill" else ("decode" if mode == "disagg" else "worker")
                if node_id is None:
                    missing.setdefault(host, {"host": host, "role": role, "reason": "unmonitored"})
                elif node_id in offline:
                    missing.setdefault(host, {"host": host, "role": role, "reason": "offline", "node": node_id})
        if not frontends:
            for member in workers:
                peer = member["env"].get("RDMA_PEER_HOST_IP")
                if peer and peer not in address:
                    other = "decode" if member["role"] == "prefill" else "prefill"
                    missing.setdefault(peer, {"host": peer, "role": other, "reason": "unmonitored"})
            for item in etcd:
                host = item.rpartition(":")[0]
                if host and host not in address:
                    missing.setdefault(host, {"host": host, "role": "frontend / etcd", "reason": "unmonitored"})

        # status
        memory = self.known.setdefault(sid, {"ready": set(), "seen": now, "service": None})
        active = frontends + workers + [member for member in members if member["role"] == "router"]
        waiting = [member for member in active if member["health"] == "starting"]
        broken = [member for member in active if member["health"] == "error"]
        for member in active:
            if member["health"] == "ok":
                memory["ready"].add(member["key"])
        detail = ""
        if broken:
            status = "degraded"
            detail = Joined([T("{role}（{node}）无响应", role=member["role"], node=member["node_name"]) for member in broken[:3]],
                            zh="；", en="; ")
        elif waiting:
            relapsed = [member for member in waiting if member["key"] in memory["ready"]]
            status = "degraded" if relapsed else "starting"
            detail = Joined([T("{role}（{node}）不再就绪" if member in relapsed else "{role}（{node}）启动中",
                               role=member["role"], node=member["node_name"]) for member in waiting[:3]], zh="；", en="; ")
        elif frontends and workers:
            status = "running"
            # A Prefill / Decode service answers requests only once both sides have registered with the frontend.
            kinds = set(item.get("component") for member in frontends
                        for item in (member["probe"].get("health") or {}).get("instances") or [])
            absent = None
            if mode == "disagg" and any(member["health"] == "ok" for member in frontends):
                absent = "Prefill" if "prefill" not in kinds else ("Decode" if "backend" not in kinds else None)
            if absent:
                status = "degraded" if memory.get("served") else "starting"
                detail = T("尚无 {role} worker 注册，服务还不能接收请求", role=absent)
            else:
                memory["served"] = True
        elif frontends:
            instances = sum(len((member["probe"].get("health") or {}).get("instances") or []) for member in frontends)
            status = "running" if instances else "starting"
            detail = "" if instances else T("frontend 已启动，尚无 worker 注册")
        elif workers:
            status = "running" if all(member["health"] in ("ok", "unknown") for member in workers) else "starting"
        else:
            status = "unknown"

        # naming
        deployments = collections.Counter()
        for member in members:
            match = CONTAINER_RE.match(member.get("container") or "")
            if match and member["role"] != "etcd":
                deployments[match.group("deployment")] += 1
        models = []
        for member in fronts:
            models.extend(member["probe"].get("models") or member["probe"]["metrics"].get("models") or [])
        for member in workers:
            models.append((member["probe"].get("metrics") or {}).get("model") or (member["probe"].get("models") or [None])[0] or member.get("model"))
        models = [model for model in models if model]
        model = collections.Counter(models).most_common(1)[0][0] if models else None
        if deployments:
            name = deployments.most_common(1)[0][0]
        elif namespace:
            name = namespace
        elif model:
            name = short_model(model)
        else:
            name = "%s @ %s" % ("Dynamo" if kind == "dynamo" else "SGLang", members[0]["node_name"])
        endpoint = None
        for member in frontends or [member for member in members if member["role"] in ("router", "server")]:
            port = member["ports"].get("http")
            if port:
                endpoint = "http://%s:%s" % (member["host"], port)
                break
        backends = collections.Counter(member["backend"] for member in workers if member.get("backend"))
        self.first_seen.setdefault(sid, now)
        return {
            "id": sid, "kind": kind, "name": name, "model": model, "model_short": short_model(model),
            "backend": backends.most_common(1)[0][0] if backends else ("sglang" if kind == "sglang" else None),
            "mode": mode, "status": status, "status_detail": detail, "namespace": namespace, "etcd": etcd,
            "since": self.first_seen[sid], "counts": dict(counts),
            "nodes": sorted(set(member["node"] for member in members)), "missing": list(missing.values()),
            "metrics": metrics, "gpu": merge_gpu([member["gpu"] for member in workers]),
            "endpoint": endpoint, "components": members, "key": key,
        }

    # -- lifecycle across builds
    def reconcile(self, services, offline, now):
        """Log appearances, status changes and disappearances; keep vanished services visible for a while."""
        current = dict((service["id"], service) for service in services)
        for sid, service in current.items():
            memory = self.known.setdefault(sid, {"ready": set(), "seen": now, "service": None})
            previous = memory.get("service")
            if self.log is not None:
                if previous is None:
                    self.log.add("info", "service", sid, service["name"], T("发现服务"),
                                 "%s · %s" % (service["kind"], service.get("model_short") or service.get("namespace") or ""), stamp=now)
                    if service["status"] == "running":
                        self.log.add("ok", "service", sid, service["name"], T("服务运行中"), "", stamp=now)
                elif "gone" in memory:
                    self.log.add("info", "service", sid, service["name"], T("服务重新出现"), service.get("status_detail") or "", stamp=now)
                elif previous["status"] != service["status"] and "degraded" not in (previous["status"], service["status"]):
                    # Degradation and recovery from it are logged by the alert of the same name, once.
                    titles = {"running": ("ok", T("服务已就绪")), "starting": ("info", T("服务启动中")), "unknown": ("warn", T("服务状态未知"))}
                    level, title = titles.get(service["status"], ("info", T("服务状态变化")))
                    self.log.add(level, "service", sid, service["name"], title, service.get("status_detail") or "", stamp=now)
            memory["service"] = service
            memory["seen"] = now
            memory.pop("gone", None)
        for sid in list(self.known):
            if sid in current:
                continue
            memory = self.known[sid]
            last = memory.get("service")
            if last is None or now - memory["seen"] > KEEP_STOPPED:
                del self.known[sid]
                self.first_seen.pop(sid, None)
                continue
            ghost = dict(last)
            if any(node_id in offline for node_id in last["nodes"]):
                ghost["status"] = "unknown"
                ghost["status_detail"] = T("所在节点离线，状态未知")
                memory["seen"] = now if "gone" not in memory else memory["seen"]
            else:
                if "gone" not in memory:
                    memory["gone"] = now
                    memory["seen"] = now
                    if self.log is not None:
                        self.log.add("warn", "service", sid, last["name"], T("服务已消失"), T("所有组件进程均已退出"), stamp=now)
                ghost["status"] = "stopped"
                ghost["status_detail"] = T("进程已退出")
                ghost["gone"] = memory["gone"]
            ghost["stale"] = True
            ghost["metrics"] = {"source": None}
            services.append(ghost)
        return services
