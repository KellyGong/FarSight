"""Synthetic cluster for `--demo`: no SSH, nothing is saved.

The feeds emit the same hello / fast / slow messages a real collector does, so the demo exercises the real
aggregation, history and alert code. Every number here is made up.
"""
import math
import random
import time

from . import ssh

MODEL = "example/chat-large"
# Prices of the demo: "H100" is priced by a price table, "G200" (a made-up model no table knows) by the user.
PRICES = {"currency": "$", "cache_hit_cost_ratio": 0.1, "gpus": [
    {"model": "NVIDIA H100 80GB", "match": {"name_regex": "H100", "memory_gib_range": [78, 82]}, "memory_gb": 80,
     "price_per_gpu_hour": {"low": 2.0, "mid": 2.5, "high": 3.0}}]}
COST_SETTINGS = {"overrides": {"G200": 4.0}}
CACHED_SHARE = 0.71       # share of the input tokens served from a cache in the demo workload
PLANES = {"DYN_REQUEST_PLANE": "tcp", "DYN_EVENT_PLANE": "zmq"}
TRANSFER = {"SGLANG_DISAGGREGATION_NIXL_BACKEND": "UCX", "UCX_TLS": "rc_verbs,tcp:aux,cuda_copy,cuda_ipc"}
TTFT_EDGES = [0.047, 0.1, 0.22, 0.47, 1, 2.2, 4.7, 10, 22, 48]
ITL_EDGES = [0.0047, 0.01, 0.022, 0.047, 0.1, 0.22, 0.47]


def wave(now, period, phase=0.0):
    return 0.5 + 0.5 * math.sin(2 * math.pi * (now / period) + phase)


class DemoNode(object):
    """Scripted node. `plan(now, node)` returns the per-GPU load and the components for the current moment."""

    def __init__(self, hostname, ip, gpu_name, gpu_mib, cores, mem_gib, plan, seed, shm=0.04, spin=None):
        self.hostname, self.ip, self.gpu_name, self.gpu_mib = hostname, ip, gpu_name, gpu_mib
        self.cores, self.mem_mib, self.plan, self.shm, self.spin = cores, mem_gib * 1024, plan, shm, spin
        self.random = random.Random(seed)
        self.started = time.time()
        self.counters = {}

    def hello(self):
        return {"type": "hello", "v": "demo", "t": time.time(), "hostname": self.hostname, "kernel": "5.15.0-1069-nvidia",
                "arch": "x86_64", "os": "Ubuntu 22.04.4 LTS", "cpu_model": "Intel(R) Xeon(R) Platinum 8468V", "cores": self.cores,
                "mem_total": self.mem_mib, "boot_time": int(self.started - 41 * 86400), "python": "3.10.12", "ips": [self.ip],
                "user": "root", "root": True, "caps": {"nvidia_smi": True, "docker": True},
                "gpus": [{"index": index, "uuid": "GPU-demo-%s-%d" % (self.hostname, index), "name": self.gpu_name,
                          "mem_total": float(self.gpu_mib), "pci": "00000000:%02x:00.0" % (24 + index * 17), "device": "",
                          "driver": "590.44.01"}
                         for index in range(8)], "interval": 2, "slow": 10}

    def counter(self, name, rate, now):
        """A cumulative counter that grows by `rate` per second."""
        value, last = self.counters.get(name, (self.random.uniform(2e4, 9e4) * max(rate, 1) / 50.0, now))
        value += max(0.0, rate) * max(0.0, now - last)
        self.counters[name] = (value, now)
        return value

    def histogram(self, name, edges, rate, center, now):
        """Cumulative buckets of a latency histogram whose observations sit around `center` seconds."""
        total = self.counter(name + ":count", rate, now)
        buckets = []
        for edge in edges:
            share = 1.0 / (1.0 + math.exp(-2.6 * (math.log(edge) - math.log(center))))
            buckets.append([edge, round(self.counter("%s:%s" % (name, edge), rate * share, now))])
        buckets.append(["+Inf", round(total)])
        return {"sum": self.counter(name + ":sum", rate * center, now), "count": round(total), "buckets": buckets}

    def fast(self, state):
        now = time.time()
        gpus = []
        for index in range(8):
            util, memory = state["gpus"].get(index, (0.0, 0.0))
            if self.spin is not None and index == self.spin:
                util, memory = 100.0, 4.0
            util = max(0.0, min(100.0, util + self.random.uniform(-4, 4) * (util > 1)))
            gpus.append({"i": index, "util": round(util), "mem": round(memory), "mem_total": float(self.gpu_mib),
                         "temp": round(34 + 0.42 * util + self.random.uniform(-1, 1)),
                         "power": round(60 + 3.4 * util + self.random.uniform(-6, 6), 1), "power_limit": 500.0})
        cpu = state.get("cpu", 1.5) + self.random.uniform(-0.6, 0.6)
        used = self.mem_mib * (state.get("mem", 0.05) + self.shm)
        return {"type": "fast", "t": now, "cpu": round(max(0.1, cpu), 1),
                "cores": [max(0, min(100, int(cpu + self.random.uniform(-cpu, cpu * 2.5)))) for _ in range(self.cores)],
                "load": [round(cpu * self.cores / 100.0, 2)] * 3, "running": 3,
                "mem": {"total": self.mem_mib, "used": int(used), "available": int(self.mem_mib - used), "cached": int(self.mem_mib * 0.3),
                        "shmem": int(self.mem_mib * self.shm), "swap_total": 0, "swap_used": 0},
                "gpus": gpus, "gpu_age": 0.4,
                "net": {"rx": int(state.get("net", 2e5) * self.random.uniform(0.8, 1.2)), "tx": int(state.get("net", 2e5) * self.random.uniform(0.8, 1.2))}}

    def slow(self, state):
        procs = []
        for component in state["components"]:
            for index in component.pop("_gpus", []):
                procs.append({"pid": component["pid"] + 40 + index, "gpu": index, "mem": state["gpus"].get(index, (0, 0))[1],
                              "name": "sglang::scheduler_DP%d_TP%d" % (index % 4, index % 4), "user": "root",
                              "cid": component["cid"], "root": component["pid"]})
        for index, memory, cid, name, user in state.get("others", []):
            procs.append({"pid": 70000 + index, "gpu": index, "mem": memory, "name": name, "user": user, "cid": cid, "root": None})
        return {"type": "slow", "t": time.time(), "took_ms": 180, "components": state["components"], "gpu_procs": procs,
                "containers": state.get("containers", {}), "docker": "ok",
                "sockets": state.get("sockets") or {"listen": [], "out": [], "in": []},
                "disks": [{"mount": "/", "fs": "ext4", "total": 875 << 30, "used": 412 << 30},
                          {"mount": "/dev/shm", "fs": "tmpfs", "total": 1008 << 30, "used": int((1008 << 30) * min(0.99, self.shm * 2.2))},
                          {"mount": "/raid", "fs": "xfs", "total": 14194 << 30, "used": 6532 << 30}],
                "processes": 2400, "zombies": state.get("zombies", 0), "sglang_zombies": state.get("zombies", 0)}

    def feed(self, session):
        yield 0.4 + self.random.uniform(0, 1.2), self.hello()
        tick = 0
        while True:
            state = self.plan(time.time(), self)
            if tick % 5 == 0:
                yield 0, self.slow(state)
            yield 0, self.fast(state)
            tick += 1
            yield session.interval, None


def engine_processes(gpus):
    return [{"comm": "python3", "n": 1, "gpus": []}, {"comm": "sglang::schedul", "n": len(gpus), "gpus": list(gpus)},
            {"comm": "sglang::data_pa", "n": 1, "gpus": []}, {"comm": "sglang::detoken", "n": 1, "gpus": []}]


def engine_sockets(pid, base, ranks=4):
    """Listening sockets and loopback connections inside one SGLang engine whose --port is `base`."""
    listen = [[pid, "python3", "127.0.0.1", base + 234], [pid, "sglang::detoken", "127.0.0.1", base + 235],
              [pid, "sglang::schedul", "0.0.0.0", base + 50]]
    out = [[pid, "sglang::schedul", "127.0.0.1", base + 234, ranks], [pid, "python3", "127.0.0.1", base + 235, 1],
           [pid, "sglang::schedul", "127.0.0.1", base + 50, ranks]]
    return listen, out


def worker(pid, cid, role, node, namespace, etcd, port, gpus, ready=True, running=0.0, queued=0.0, usage=0.0, rate=0.0,
           backend="sglang", prompt=None, base=30000):
    """`rate` is the output rate in tokens per second, `prompt` the input rate (prefill and aggregated workers)."""
    now = time.time()
    split = role in ("prefill", "decode")
    probe = {"health": {"status": "ready" if ready else "notready", "endpoints": {"generate": "ready" if ready else "notready"}}}
    if ready:
        prompt = rate * 0.5 if prompt is None else prompt
        probe["metrics"] = {"engine_type": role if split else None, "model": MODEL, "dp_ranks": len(gpus),
                            "has_sglang": True, "serving": True, "uptime": now - node.started, "running": round(running),
                            "queued": round(queued), "gen_throughput": round(rate, 1), "kv_usage": round(usage, 3), "cache_hit_rate": 0.62,
                            "prompt_tokens": node.counter("%d:prompt" % pid, prompt, now),
                            "generation_tokens": node.counter("%d:gen" % pid, rate if role != "prefill" else 0.2, now)}
        if role != "decode":
            probe["metrics"]["computed_tokens"] = node.counter("%d:computed" % pid, prompt * (1 - CACHED_SHARE), now)
            probe["metrics"]["hit_tokens"] = node.counter("%d:hit" % pid, prompt * CACHED_SHARE, now)
    config = "/opt/recipe/configs/sglang-%s.yaml" % role if split else "/opt/configs/agg.yaml"
    settings = {"port": base, "nccl_port": base + 50, "tp": 1, "dp": len(gpus), "dp_attention": True}
    env = dict(PLANES, DYN_NAMESPACE=namespace, ETCD_ENDPOINTS=etcd, DYN_SYSTEM_PORT=str(port))
    if split:
        settings.update(mode=role, transfer="nixl", bootstrap_port=base + 1)
        env.update(TRANSFER)
    if role == "prefill":
        settings["kv_events_port"] = 5557
    return {"pid": pid, "kind": "dynamo-worker", "backend": backend, "role": role if split else None,
            "cmdline": "python3 -m dynamo.%s --config %s" % (backend, config), "user": "root", "started": node.started,
            "cid": cid, "env": env, "env_source": "process", "ports": {"system": port}, "model": None, "discovery": "etcd",
            "settings": settings, "procs": engine_processes(gpus), "probe": probe, "_gpus": gpus}


def frontend(pid, cid, node, namespace, etcd, port, instances, load, registered):
    now = time.time()
    rate = 6.0 * load
    metrics = {"inflight": round(48 * load), "queued": round(10 * load * load), "requests": node.counter("fe:req", rate, now),
               "errors": node.counter("fe:err", rate * 0.002, now), "output_tokens": node.counter("fe:out", 3200 * load, now),
               "input_tokens": node.counter("fe:in", 3200 * load * 38, now),
               "cached_tokens": node.counter("fe:cached", 3200 * load * 38 * CACHED_SHARE, now),
               "models": [MODEL], "workers": registered,
               "ttft": node.histogram("fe:ttft", TTFT_EDGES, rate, 0.55 + 1.6 * load, now),
               "itl": node.histogram("fe:itl", ITL_EDGES, rate * 180, 0.016 + 0.012 * load, now)}
    return {"pid": pid, "kind": "dynamo-frontend", "backend": None, "role": None,
            "cmdline": "python3 -m dynamo.frontend --http-host 0.0.0.0 --http-port %d --router-mode kv --router-kv-events" % port,
            "user": "root", "started": node.started, "cid": cid, "env": dict(PLANES, DYN_NAMESPACE=namespace, ETCD_ENDPOINTS=etcd),
            "env_source": "process", "ports": {"http": port}, "http_host": "127.0.0.1", "router": "kv", "discovery": "etcd", "model": None,
            "procs": [{"comm": "python3", "n": 1, "gpus": []}],
            "probe": {"health": {"status": "healthy", "instances": instances}, "models": [MODEL], "metrics": metrics}}


def etcd_component(pid, cid, node, port):
    return {"pid": pid, "kind": "etcd", "backend": None, "role": None, "cmdline": "etcd", "user": "root", "started": node.started,
            "cid": cid, "env": {}, "env_source": "process", "ports": {"client": port}, "model": None,
            "procs": [{"comm": "etcd", "n": 1, "gpus": []}], "probe": {"health": {"status": "healthy"}}}


def cluster():
    """[(node config, feed)] of the demo cluster."""
    namespace, etcd = "chat_prod_sglang_pd", "http://10.0.0.24:2379"
    period = 240.0
    born = time.time()

    def load(now):
        return 0.25 + 0.7 * wave(now, period)

    def prefill_plan(cid_prefix, spin_note, request_port, other):
        def plan(now, node):
            level = load(now)
            gpus = dict((index, (30 + 68 * level, 76500)) for index in range(4))
            pid = 41200
            component = worker(pid, cid_prefix + "a1", "prefill", node, namespace, etcd, 18081, [0, 1, 2, 3],
                               running=2 + 6 * level, queued=4 * level * level, usage=0.12 + 0.2 * level, rate=40 + 160 * level,
                               prompt=3200 * level * 38 / 3.0)
            listen, out = engine_sockets(pid, 30000)
            listen += [[pid, "python3", "0.0.0.0", 18081], [pid, "python3", node.ip, request_port], [pid, "python3", "0.0.0.0", request_port + 9],
                       [pid, "python3", "0.0.0.0", 30001], [pid, "radix-server", "0.0.0.0", 17000]]
            listen += [[pid, "sglang::schedul", "0.0.0.0", 5557 + rank] for rank in range(4)]
            listen += [[pid, "sglang::schedul", node.ip, 42001 + rank] for rank in range(4)]
            out += [[pid, "python3", "10.0.0.24", 2379, 1], [pid, "python3", "10.0.0.24", 39001, round(4 + 14 * level)],
                    [pid, "sglang::schedul", node.ip, 30001, 4], [pid, "radix-server", other, 17000, 1]]
            out += [[pid, "python3", node.ip, 5557 + rank, 1] for rank in range(4)]
            out += [[pid, "sglang::schedul", "10.0.0.24", 40101 + rank, 9] for rank in range(4)]
            return {"cpu": 4 + 9 * level, "mem": 0.2, "net": 4e7 * level, "gpus": gpus, "components": [component],
                    "sockets": {"listen": listen, "out": out, "in": []},
                    "containers": {cid_prefix + "a1": {"name": "chat-prod-sglang-prefill" + spin_note, "image": "example/dynamo-sglang:prefill",
                                                        "status": "Up 3 hours", "devices": ["0", "1", "2", "3"]}}}
        return plan

    def decode_plan(now, node):
        level = load(now)
        gpus = dict((index, (22 + 60 * level, 168400)) for index in range(4))
        gpus.update(dict((index, (35 + 30 * wave(now, 90, index), 179200)) for index in range(4, 8)))
        instances = [{"namespace": namespace, "component": "backend", "id": "7001", "host": "10.0.0.24", "port": 37777},
                     {"namespace": namespace, "component": "prefill", "id": "7002", "host": "10.0.1.23", "port": 41001},
                     {"namespace": namespace, "component": "prefill", "id": "7003", "host": "10.0.1.24", "port": 41002},
                     {"namespace": namespace, "component": "prefill", "id": "7004", "host": "10.0.1.10", "port": 41003}]
        listen, out = engine_sockets(52300, 31600)
        listen += [[52000, "etcd", "0.0.0.0", 2379], [52100, "python3", "0.0.0.0", 8000], [52100, "python3", "10.0.0.24", 39001],
                   [52300, "python3", "0.0.0.0", 18082], [52300, "python3", "10.0.0.24", 37777], [52300, "python3", "0.0.0.0", 37786],
                   [53400, "python", "0.0.0.0", 30000]]
        listen += [[52300, "sglang::schedul", "10.0.0.24", 40101 + rank] for rank in range(4)]
        out += [[52100, "python3", "10.0.0.24", 2379, 1], [52100, "python3", "10.0.0.24", 37777, 1], [52100, "python3", "10.0.0.24", 37786, 1],
                [52300, "python3", "10.0.0.24", 2379, 1], [52300, "python3", "10.0.0.24", 39001, round(8 + 40 * level)]]
        for host, port in (("10.0.1.23", 41001), ("10.0.1.24", 41002), ("10.0.1.10", 41003)):
            out += [[52100, "python3", host, port, 1], [52100, "python3", host, port + 9, 1]]
        for host in ("10.0.1.23", "10.0.1.24"):
            out.append([52300, "sglang::schedul", host, 30001, 12])
            out += [[52300, "sglang::schedul", host, 42001 + rank, 4] for rank in range(4)]
        inbound = [[52100, "python3", 8000, "10.0.0.201", round(20 + 60 * level)], [52100, "python3", 39001, "10.0.1.10", round(4 + 14 * level)],
                   [53400, "python", 30000, "10.0.0.202", round(3 + 9 * wave(now, 90))]]
        components = [
            frontend(52100, "c0ffee000001", node, namespace, etcd, 8000, instances, level, {"prefill": 3, "decode": 1}),
            worker(52300, "c0ffee000002", "decode", node, namespace, etcd, 18082, [0, 1, 2, 3], running=6 + 40 * level,
                   queued=2 * level * level, usage=0.2 + 0.45 * level, rate=300 + 2900 * level, base=31600),
            etcd_component(52000, "c0ffee000003", node, 2379),
            {"pid": 53400, "kind": "sglang-server", "backend": None, "role": None,
             "cmdline": "python -m sglang.launch_server --model-path /models/Qwen3-32B --served-model-name Qwen/Qwen3-32B --port 30000 --tp 4 --enable-metrics",
             "user": "liwei", "started": node.started + 3600, "cid": None, "env": {}, "env_source": "process", "ports": {"http": 30000},
             "http_host": "127.0.0.1", "model": "Qwen/Qwen3-32B", "settings": {"port": 30000, "tp": 4},
             "procs": [{"comm": "python", "n": 1, "gpus": []}, {"comm": "sglang::schedul", "n": 1, "gpus": [4, 5, 6, 7]},
                       {"comm": "sglang::detoken", "n": 1, "gpus": []}],
             "probe": {"health": {"status": "ready"}, "models": ["Qwen/Qwen3-32B"],
                       "metrics": {"engine_type": None, "model": "Qwen/Qwen3-32B", "dp_ranks": 1, "has_sglang": True,
                                   "running": round(3 + 9 * wave(now, 90)), "queued": 0, "gen_throughput": round(400 + 500 * wave(now, 90), 1),
                                   "kv_usage": 0.31, "cache_hit_rate": 0.4, "prompt_tokens": node.counter("q:prompt", 2400, now),
                                   "uncached_tokens": node.counter("q:uncached", 1440, now),
                                   "generation_tokens": node.counter("q:gen", 400 + 500 * wave(now, 90), now)}},
             "_gpus": [4, 5, 6, 7]},
        ]
        return {"cpu": 6 + 12 * level, "mem": 0.08, "net": 5e7 * level, "gpus": gpus, "components": components,
                "sockets": {"listen": listen, "out": out, "in": inbound},
                "containers": {"c0ffee000001": {"name": "chat-prod-sglang-frontend", "image": "example/dynamo-sglang:decode", "status": "Up 3 hours"},
                               "c0ffee000002": {"name": "chat-prod-sglang-decode", "image": "example/dynamo-sglang:decode", "status": "Up 3 hours",
                                                "devices": ["0", "1", "2", "3"]},
                               "c0ffee000003": {"name": "chat-prod-etcd", "image": "bitnamilegacy/etcd", "status": "Up 2 days"}}}

    def starting_plan(now, node):
        ready = now - born > 150
        level = wave(now, 70) if ready else 0.0
        loaded = min(1.0, (now - born) / 150.0)
        gpus = dict((index, (55 * level if ready else 3.0, 161000 * loaded)) for index in range(4))
        health = {"status": "healthy", "instances": [{"namespace": "qa_eval", "component": "backend", "id": "9001", "host": "10.0.0.17", "port": 40112}]} \
            if ready else {"status": "notready", "instances": []}
        front = frontend(61000, "beef00000001", node, "qa_eval", "http://10.0.0.17:2379", 8100, health["instances"], 0.15 * level, {"aggregated": 1} if ready else {})
        front["probe"]["health"] = health
        component = worker(61200, "beef00000002", None, node, "qa_eval", "http://10.0.0.17:2379", 18090, [0, 1, 2, 3], ready=ready,
                           running=5 * level, usage=0.1 * level, rate=420 * level, prompt=5200 * level)
        listen, out = engine_sockets(61200, 30000)
        listen += [[60900, "etcd", "0.0.0.0", 2379], [61000, "python3", "0.0.0.0", 8100], [61000, "python3", "10.0.0.17", 39100],
                   [61200, "python3", "0.0.0.0", 18090], [61200, "python3", "10.0.0.17", 40112]]
        out += [[61000, "python3", "10.0.0.17", 2379, 1], [61200, "python3", "10.0.0.17", 2379, 1]]
        if ready:
            out += [[61000, "python3", "10.0.0.17", 40112, 1], [61200, "python3", "10.0.0.17", 39100, round(1 + 5 * level)]]
        inbound = [[61000, "python3", 8100, "10.0.0.203", round(1 + 6 * level)]] if ready else []
        return {"cpu": 9 if not ready else 3 + 4 * level, "mem": 0.11, "gpus": gpus,
                "sockets": {"listen": listen, "out": out, "in": inbound},
                "components": [front, component, etcd_component(60900, "beef00000003", node, 2379)],
                "containers": {"beef00000001": {"name": "qa-eval-frontend", "image": "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.4.0", "status": "Up 2 minutes"},
                               "beef00000002": {"name": "qa-eval-worker", "image": "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.4.0", "status": "Up 2 minutes"},
                               "beef00000003": {"name": "qa-eval-etcd", "image": "bitnamilegacy/etcd", "status": "Up 2 minutes"}}}

    def idle_plan(now, node):
        gpus = {2: (96.0, 71200.0), 3: (94.0, 70800.0)}
        return {"cpu": 2.2, "mem": 0.04, "gpus": gpus, "components": [], "zombies": 6,
                "others": [(2, 71200.0, "ab12cd34ef56", "python train.py", "chenyu"), (3, 70800.0, "ab12cd34ef56", "python train.py", "chenyu")],
                "containers": {"ab12cd34ef56": {"name": "chenyu-finetune", "image": "nvcr.io/nvidia/pytorch:25.10-py3", "status": "Up 9 hours"}}}

    nodes = [
        DemoNode("gpu-a-24", "10.0.0.24", "G200", 196608, 192, 2015, decode_plan, 1),
        DemoNode("gpu-b-07", "10.0.1.23", "H100", 81559, 128, 1024, prefill_plan("aa00000000", "", 41001, "10.0.1.24"), 2, shm=0.44, spin=6),
        DemoNode("gpu-b-08", "10.0.1.24", "H100", 81559, 128, 1024, prefill_plan("bb00000000", "-r1", 41002, "10.0.1.23"), 3, shm=0.43),
        DemoNode("gpu-a-17", "10.0.0.17", "G200", 196608, 192, 2015, starting_plan, 4),
        DemoNode("gpu-a-25", "10.0.0.25", "G200", 196608, 192, 2015, idle_plan, 5),
    ]
    result = []
    for index, node in enumerate(nodes):
        config = {"id": "demo%d" % (index + 1), "host": node.ip, "port": 22, "user": "root", "auth": "key", "label": "", "added": born}
        result.append((config, node.feed))
    result.append(({"id": "demo8", "host": "10.0.0.28", "port": 22, "user": "root", "auth": "key", "label": "", "added": born},
                   failing("root@10.0.0.28: Permission denied (publickey,password).", 255, 1.6)))
    result.append(({"id": "demo9", "host": "10.0.1.12", "port": 22, "user": "root", "auth": "key", "label": "", "added": born},
                   failing("ssh: connect to host 10.0.1.12 port 22: Connection timed out", 255, 8.0)))
    return result


def failing(stderr, returncode, delay, auth="key"):
    def feed(session):
        while True:
            yield delay, {"type": "status", "status": "error", "error": ssh.classify(stderr, returncode, auth)}
            yield 3600, None
            yield 0, {"type": "status", "status": "connecting"}
    return feed


def added_node(config):
    """Feed of a node added through the UI in demo mode. Hosts whose name contains "fail", "refuse" or "timeout"
    (or a password of "wrong") show the matching failure; any other host comes up as an idle node."""
    host = config["host"]
    if "fail" in host or host.endswith(".99"):
        if config.get("auth") == "password":
            return failing("%s@%s: Permission denied (publickey,password)." % (config["user"], host), 255, 1.8, "password")
        return failing("%s@%s: Permission denied (publickey,password)." % (config["user"], host), 255, 1.2)
    if "refuse" in host:
        return failing("ssh: connect to host %s port %s: Connection refused" % (host, config["port"]), 255, 0.6)
    if "timeout" in host or host.endswith(".98"):
        return failing("ssh: connect to host %s port %s: Connection timed out" % (host, config["port"]), 255, 8.0)

    def plan(now, node):
        return {"cpu": 1.1, "mem": 0.03, "gpus": {}, "components": []}

    name = host if not host[0].isdigit() else "node-" + host.split(".")[-1]
    return DemoNode(name, host if host[0].isdigit() else "10.9.9.%d" % (abs(hash(host)) % 250 + 1), "G200", 196608, 192, 2015, plan, abs(hash(host)) % 1000).feed
