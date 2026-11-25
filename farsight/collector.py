#!/usr/bin/env python3
"""farsight node collector.

Read-only, standard library only, Python 3.6+.

It runs on the monitored node. The dashboard ships this file over SSH on stdin (nothing is installed or
written on the node) or starts it directly for the local node. One JSON object per line goes to stdout:

  {"type": "hello", ...}   once: host and GPU inventory
  {"type": "fast",  ...}   every --interval seconds: CPU, memory, per-GPU load, network
  {"type": "slow",  ...}   every --slow seconds: GPU processes, service components, containers, endpoints, disks

The collector exits as soon as stdin reaches EOF (the SSH session went away), so it leaves no orphan.

Endpoint rule: HTTP requests go only to ports a service declares as HTTP itself (the Dynamo frontend's
--http-port, a Dynamo worker's DYN_SYSTEM_PORT, a native SGLang server's --port, etcd's client URL). Ports are
never scanned: an HTTP request to a ZeroMQ or NIXL listener can drop real inference requests or crash the
listener thread.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

try:
    import pwd
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None

import urllib.error
import urllib.request

VERSION = "0.2.0"

# Ports that are never contacted, whatever a command line says: NIXL data / RPC listeners of a KV transfer side
# service seen next to SGLang workers. Such a listener reads the first bytes as a length and dies on an HTTP request.
DEFAULT_SKIP_PORTS = (17000, 17100, 19000, 19100)
HEALTH_BACKOFF = 60.0     # seconds between /health requests to a worker that answered "not ready"
ENV_KEYS = ("DYN_NAMESPACE", "DYN_SYSTEM_PORT", "DYN_HTTP_PORT", "DYN_DISCOVERY_BACKEND", "DYN_REQUEST_PLANE",
            "DYN_EVENT_PLANE", "DYN_TCP_RPC_HOST", "DYN_TCP_RPC_PORT", "DYN_TCP_RESPONSE_STREAM_PORT",
            "ETCD_ENDPOINTS", "ETCD_LISTEN_CLIENT_URLS", "SGLANG_HOST_IP", "RDMA_PEER_HOST_IP",
            "SGLANG_DISAGGREGATION_NIXL_BACKEND", "UCX_TLS", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "NATS_SERVER")
# Engine settings that describe how a worker communicates. They are read from the command line and, when the
# worker was started with --config <file>, from that file; nothing else in the file is reported.
SETTING_KEYS = ("port", "host", "nccl-port", "dist-init-addr", "disaggregation-mode", "disaggregation-bootstrap-port",
                "disaggregation-transfer-backend", "kv-events-config", "tp-size", "tp", "dp-size", "dp",
                "enable-dp-attention")
MAX_SOCKET_ROWS = 400     # per list in the socket summary of one slow sample
WORKER_BACKENDS = ("sglang", "vllm", "trtllm", "llama_cpp", "mocker")
LOCAL_FS = ("ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs", "f2fs")
VIRTUAL_IFACE = re.compile(r"^(lo|docker|veth|br-|virbr|cni|flannel|cali|tunl|kube|dummy|bonding_masters)")
PYTHON_EXE = re.compile(r"^python(\d+(\.\d+)?)?$")
# Known PCI device ids of cards nvidia-smi reports with a generic name.
PCI_NAMES = {"0x2BB310DE": "RTX PRO 5000"}


# ------------------------------------------------------------------------------------------------- parsers
def read_text(path, limit=1 << 20):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except (IOError, OSError):
        return ""


def parse_proc_stat(text):
    """Return {"total": (idle, total), "cores": [(idle, total), ...], "btime": seconds, "running": n}."""
    result = {"total": None, "cores": [], "btime": 0, "running": None}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("cpu"):
            try:
                values = [int(value) for value in parts[1:9]]
            except ValueError:
                continue
            if len(values) < 4:
                continue
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            pair = (idle, sum(values))
            if parts[0] == "cpu":
                result["total"] = pair
            else:
                result["cores"].append(pair)
        elif parts[0] == "btime" and len(parts) > 1:
            result["btime"] = int(parts[1])
        elif parts[0] == "procs_running" and len(parts) > 1:
            result["running"] = int(parts[1])
    return result


def busy_percent(previous, current):
    """Busy share between two (idle, total) samples, in percent, or None when no time passed."""
    if not previous or not current:
        return None
    total = current[1] - previous[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - float(current[0] - previous[0]) / total)))


def parse_meminfo(text):
    """Memory figures in MiB."""
    raw = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            raw[key.strip()] = int(parts[0])
    total = raw.get("MemTotal", 0)
    available = raw.get("MemAvailable", raw.get("MemFree", 0) + raw.get("Buffers", 0) + raw.get("Cached", 0))
    swap_total = raw.get("SwapTotal", 0)
    return {
        "total": total // 1024,
        "used": max(0, total - available) // 1024,
        "available": available // 1024,
        "cached": (raw.get("Cached", 0) + raw.get("Buffers", 0)) // 1024,
        "shmem": raw.get("Shmem", 0) // 1024,
        "swap_total": swap_total // 1024,
        "swap_used": max(0, swap_total - raw.get("SwapFree", 0)) // 1024,
    }


def parse_net_dev(text):
    """Sum of (rx_bytes, tx_bytes) over physical-looking interfaces."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or VIRTUAL_IFACE.match(name) or len(fields) < 9:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
    return rx, tx


def number(text):
    """nvidia-smi value -> float, or None for [N/A], [Not Supported] and friends."""
    text = text.strip()
    if not text or text.startswith("["):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_gpu_csv(text, fields):
    """Parse `nvidia-smi --query-gpu=<fields> --format=csv,noheader,nounits` into a list of dicts of strings."""
    rows = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < len(fields) or not parts[0].isdigit():
            continue
        rows.append(dict(zip(fields, parts)))
    return rows


GPU_STATIC_FIELDS = ["index", "uuid", "name", "memory.total", "pci.bus_id", "pci.device_id", "driver_version"]
GPU_LIVE_FIELDS = ["index", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw",
                   "power.limit"]


def gpu_inventory(rows):
    inventory = []
    for row in rows:
        name = row.get("name", "")
        if "Graphics Device" in name:
            name = PCI_NAMES.get(row.get("pci.device_id", "").upper().replace("0X", "0x"), name)
        inventory.append({
            "index": int(row["index"]),
            "uuid": row.get("uuid", ""),
            "name": name.replace("NVIDIA ", ""),
            "mem_total": number(row.get("memory.total", "")),
            "pci": row.get("pci.bus_id", ""),
            "device": row.get("pci.device_id", ""),
            "driver": row.get("driver_version", ""),
        })
    return inventory


def gpu_sample(rows):
    sample = []
    for row in rows:
        sample.append({
            "i": int(row["index"]),
            "util": number(row.get("utilization.gpu", "")),
            "mem": number(row.get("memory.used", "")),
            "mem_total": number(row.get("memory.total", "")),
            "temp": number(row.get("temperature.gpu", "")),
            "power": number(row.get("power.draw", "")),
            "power_limit": number(row.get("power.limit", "")),
        })
    return sample


def parse_compute_apps(text):
    """`--query-compute-apps=pid,gpu_uuid,used_memory,process_name` -> [(pid, uuid, MiB, name)]."""
    apps = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        apps.append((int(parts[0]), parts[1], number(parts[2]), parts[3] if len(parts) > 3 else ""))
    return apps


def parse_stat_line(text):
    """/proc/<pid>/stat -> (comm, state, ppid, starttime ticks) or None. comm may contain spaces and parentheses."""
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right < left:
        return None
    rest = text[right + 2:].split()
    if len(rest) < 20:
        return None
    try:
        return text[left + 1:right], rest[0], int(rest[1]), int(rest[19])
    except ValueError:
        return None


def container_id(cgroup_text):
    match = re.search(r"([0-9a-f]{64})", cgroup_text)
    return match.group(1) if match else None


def option(argv, *names):
    """Value of `--name value` or `--name=value`; `_` and `-` in the flag name are equivalent."""
    wanted = set(name.replace("_", "-") for name in names)
    for index, arg in enumerate(argv):
        if not arg.startswith("--"):
            continue
        flag, sep, value = arg.partition("=")
        if flag.replace("_", "-") in wanted:
            if sep:
                return value
            if index + 1 < len(argv):
                return argv[index + 1]
    return None


def options(argv, name):
    """All values following a multi-value flag: `--prefill a b --decode c` -> ["a", "b"] for --prefill."""
    values = []
    collecting = False
    for arg in argv:
        if arg.startswith("--"):
            flag, sep, value = arg.partition("=")
            collecting = flag.replace("_", "-") == name
            if collecting and sep:
                values.append(value)
        elif collecting:
            values.append(arg)
    return values


def to_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def parse_flat_yaml(text, keys=SETTING_KEYS):
    """Top-level `key: value` pairs of a flat YAML file, limited to `keys` (`_` and `-` are equivalent)."""
    values = {}
    for line in text.splitlines():
        if not line or line[0] in "# \t-":
            continue
        key, sep, value = line.partition(":")
        key = key.strip().replace("_", "-")
        if not sep or key not in keys:
            continue
        value = value.strip()
        if value[:1] in "'\"" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value[:400]
    return values


def engine_settings(args, file_values=None):
    """Communication settings of an SGLang engine: the config file's values, overridden by command-line flags."""
    settings = dict(file_values or {})
    for key in SETTING_KEYS:
        value = option(args, "--" + key)
        if value is not None and not value.startswith("--"):
            settings[key] = value[:400]
        elif "--" + key in args or "--" + key.replace("-", "_") in args:
            settings.setdefault(key, "true")
    result = {}
    for name, keys in (("port", ("port",)), ("nccl_port", ("nccl-port",)), ("bootstrap_port", ("disaggregation-bootstrap-port",))):
        for key in keys:
            port = to_port(settings.get(key))
            if port:
                result[name] = port
    for name, keys in (("tp", ("tp-size", "tp")), ("dp", ("dp-size", "dp"))):
        for key in keys:
            if str(settings.get(key, "")).isdigit():
                result[name] = int(settings[key])
    for name, key in (("mode", "disaggregation-mode"), ("transfer", "disaggregation-transfer-backend"), ("dist_init", "dist-init-addr")):
        if settings.get(key):
            result[name] = str(settings[key])[:80]
    if str(settings.get("enable-dp-attention", "")).lower() in ("true", "1", "yes"):
        result["dp_attention"] = True
    match = re.search(r"tcp://[^:\"']*:(\d+)", str(settings.get("kv-events-config", "")))
    if match and to_port(match.group(1)):
        result["kv_events_port"] = int(match.group(1))
    return result


def classify_process(argv):
    """Recognise a service process from its argv. Wrappers (bash -c, timeout, docker exec, ...) are not matched:
    only the interpreter or binary that is the service itself."""
    if not argv or not argv[0]:
        return None
    exe = os.path.basename(argv[0])
    if exe == "etcd":
        return {"kind": "etcd"}
    module = script = None
    rest = []
    if PYTHON_EXE.match(exe):
        index = 1
        while index < len(argv):
            arg = argv[index]
            if arg == "-m":
                if index + 1 < len(argv):
                    module = argv[index + 1]
                    rest = argv[index + 2:]
                break
            if arg == "-c":
                return None
            if arg.startswith("-"):
                index += 2 if arg in ("-X", "-W") else 1
                continue
            script = os.path.basename(arg)
            rest = argv[index + 1:]
            break
    else:
        script = exe
        rest = argv[1:]
    if module == "dynamo.frontend" or script == "dynamo-frontend":
        return {"kind": "dynamo-frontend", "args": rest}
    if module and module.startswith("dynamo."):
        backend = module.split(".")[1]
        if backend in WORKER_BACKENDS:
            return {"kind": "dynamo-worker", "backend": backend, "args": rest}
        return None
    if module == "sglang.launch_server":
        return {"kind": "sglang-server", "args": rest}
    if script == "sglang" and rest and rest[0] == "serve":
        return {"kind": "sglang-server", "args": rest[1:]}
    if module in ("sglang_router.launch_router", "sglang.srt.disaggregation.mini_lb") or script in ("sglang-router", "sglang_router"):
        return {"kind": "sglang-router", "args": rest}
    return None


def role_of(kind, args, container_cmd=None, container_name=None, settings=None):
    """prefill / decode for a worker, from the most to the least direct evidence; None when it is aggregated."""
    if kind not in ("dynamo-worker", "sglang-server"):
        return None
    mode = option(args, "--disaggregation-mode") or (settings or {}).get("mode")
    if mode in ("prefill", "decode"):
        return mode
    config = option(args, "--config") or ""
    match = re.search(r"(prefill|decode)", os.path.basename(config))
    if match:
        return match.group(1)
    if container_cmd and container_cmd[-1] in ("prefill", "decode"):
        return container_cmd[-1]
    if container_name:
        match = re.search(r"-(prefill|decode)(-|$)", container_name)
        if match:
            return match.group(1)
    return None


def describe_component(pid, found, env, env_source=None, container_cmd=None, container_name=None, file_values=None):
    """Describe a recognised service process, including the only ports the collector may send HTTP to.

    Those are ports the service declares as HTTP itself: the Dynamo frontend's --http-port, a Dynamo worker's
    DYN_SYSTEM_PORT, a native SGLang server's --port and etcd's client port. A Dynamo worker's own --port is
    deliberately absent: under `dynamo.sglang` it is not an HTTP server, and SGLang binds ZeroMQ sockets next to it.
    """
    args = found.get("args", [])
    kind = found["kind"]
    settings = engine_settings(args, file_values) if kind in ("dynamo-worker", "sglang-server") else {}
    component = {
        "pid": pid, "kind": kind, "backend": found.get("backend"),
        "role": role_of(kind, args, container_cmd, container_name, settings),
        "cmdline": " ".join(found.get("argv") or [])[:600],
        "env": env, "env_source": env_source, "ports": {},
        "model": option(args, "--served-model-name") or option(args, "--model-path", "--model"),
    }
    if kind == "dynamo-frontend":
        host = option(args, "--http-host") or "0.0.0.0"
        component["http_host"] = "127.0.0.1" if host in ("0.0.0.0", "::", "", "localhost") else host
        component["ports"]["http"] = to_port(option(args, "--http-port")) or to_port(env.get("DYN_HTTP_PORT")) or 8000
        component["router"] = option(args, "--router-mode")
        component["discovery"] = option(args, "--discovery-backend") or env.get("DYN_DISCOVERY_BACKEND")
    elif kind == "dynamo-worker":
        port = to_port(env.get("DYN_SYSTEM_PORT"))
        if port:
            component["ports"]["system"] = port
        component["discovery"] = option(args, "--discovery-backend") or env.get("DYN_DISCOVERY_BACKEND")
        component["config"] = option(args, "--config")
        component["settings"] = settings
    elif kind == "sglang-server":
        host = option(args, "--host") or "127.0.0.1"
        component["http_host"] = "127.0.0.1" if host in ("0.0.0.0", "::", "", "localhost") else host
        component["ports"]["http"] = to_port(option(args, "--port")) or settings.get("port") or 30000
        component["settings"] = settings
    elif kind == "sglang-router":
        component["ports"]["http"] = to_port(option(args, "--port"))
        component["prefill"] = options(args, "--prefill")[:16]
        component["decode"] = options(args, "--decode")[:16]
    elif kind == "etcd":
        urls = option(found.get("argv") or [], "--listen-client-urls") or env.get("ETCD_LISTEN_CLIENT_URLS") or ""
        match = re.search(r":(\d+)", urls.split(",")[0].split("//")[-1])
        component["ports"]["client"] = to_port(match.group(1)) if match else 2379
    return component


SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text, prefixes):
    """Yield (name, labels, value) for samples whose metric name starts with one of `prefixes`."""
    prefixes = tuple(prefixes)
    for line in text.splitlines():
        if not line.startswith(prefixes):
            continue
        match = SAMPLE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        labels = dict(LABEL.findall(match.group(2))) if match.group(2) else {}
        yield match.group(1), labels, value


FRONTEND_PREFIXES = ("dynamo_frontend_", "dynamo_component_router_worker_registered")
FRONTEND_HISTOGRAMS = {
    "dynamo_frontend_time_to_first_token_seconds": "ttft",
    "dynamo_frontend_inter_token_latency_seconds": "itl",
    "dynamo_frontend_request_duration_seconds": "duration",
}
FRONTEND_SUMS = {
    "dynamo_frontend_input_sequence_tokens_sum": "input_tokens",
    "dynamo_frontend_cached_tokens_sum": "cached_tokens",
    "dynamo_frontend_output_tokens_total": "output_tokens",
    "dynamo_frontend_inflight_requests": "inflight",
    "dynamo_frontend_queued_requests": "queued",
}


def summarize_frontend_metrics(text):
    """Compact summary of a Dynamo frontend /metrics page; counters stay cumulative, rates are taken later."""
    summary = {"inflight": 0.0, "queued": 0.0, "requests": 0.0, "errors": 0.0, "output_tokens": 0.0,
               "input_tokens": 0.0, "cached_tokens": 0.0, "models": [], "workers": {}}
    histograms = {}
    seen_inflight = False
    registered = {}
    models = set()
    for name, labels, value in parse_prometheus(text, FRONTEND_PREFIXES):
        if labels.get("model"):
            models.add(labels["model"])
        if name in FRONTEND_SUMS:
            summary[FRONTEND_SUMS[name]] += value
            seen_inflight = seen_inflight or name == "dynamo_frontend_inflight_requests"
        elif name == "dynamo_frontend_active_requests" and not seen_inflight:
            summary.setdefault("_active", 0.0)
            summary["_active"] += value
        elif name == "dynamo_frontend_requests_total":
            summary["requests"] += value
            if labels.get("status", "success") != "success":
                summary["errors"] += value
        elif name == "dynamo_component_router_worker_registered":
            if value >= 1:
                registered.setdefault(labels.get("worker_type", "worker"), set()).add(
                    labels.get("router_worker_id") or labels.get("worker_id") or "")
        else:
            for base, key in FRONTEND_HISTOGRAMS.items():
                if not name.startswith(base):
                    continue
                entry = histograms.setdefault(key, {"sum": 0.0, "count": 0.0, "buckets": {}})
                suffix = name[len(base):]
                if suffix == "_bucket" and "le" in labels:
                    entry["buckets"][labels["le"]] = entry["buckets"].get(labels["le"], 0.0) + value
                elif suffix == "_sum":
                    entry["sum"] += value
                elif suffix == "_count":
                    entry["count"] += value
                break
    active = summary.pop("_active", None)
    if not seen_inflight and active is not None:
        summary["inflight"] = active
    for key, entry in histograms.items():
        buckets = []
        for edge, count in entry["buckets"].items():
            try:
                buckets.append((float("inf") if edge in ("+Inf", "inf") else float(edge), count))
            except ValueError:
                continue
        buckets.sort()
        entry["buckets"] = [["+Inf" if edge == float("inf") else edge, count] for edge, count in buckets]
        summary[key] = entry
    summary["models"] = sorted(models)
    summary["workers"] = dict((kind, len(ids)) for kind, ids in registered.items())
    return summary


WORKER_PREFIXES = ("sglang:", "vllm:", "dynamo_component_")
WORKER_GAUGE_SUM = {"sglang:num_running_reqs": "running", "sglang:num_queue_reqs": "queued",
                    "sglang:gen_throughput": "gen_throughput"}
WORKER_GAUGE_MAX = {"sglang:token_usage": "kv_usage", "sglang:cache_hit_rate": "cache_hit_rate"}
WORKER_COUNTERS = {"sglang:prompt_tokens_total": "prompt_tokens", "sglang:generation_tokens_total": "generation_tokens",
                   "sglang:uncached_prompt_tokens_histogram_sum": "uncached_tokens"}


def summarize_worker_metrics(text):
    """Compact summary of a worker /metrics page (SGLang series).

    Scheduler gauges are reported once per TP rank: take the maximum inside a DP rank, then add the DP ranks.
    A series with a non-empty `priority` label is a per-priority breakdown of the total and is skipped.
    """
    per_rank = {}
    maxima = {}
    counters = {}
    summary = {"engine_type": None, "model": None, "dp_ranks": 0, "has_sglang": False, "serving": False}
    ranks = set()
    for name, labels, value in parse_prometheus(text, WORKER_PREFIXES):
        if name == "dynamo_component_uptime_seconds":
            summary["uptime"] = max(summary.get("uptime", 0.0), value)
            continue
        # A worker that is still loading exports nothing but its uptime; any other series means its endpoints exist.
        summary["serving"] = True
        if not name.startswith("sglang:"):
            continue
        summary["has_sglang"] = True
        if labels.get("engine_type"):
            summary["engine_type"] = labels["engine_type"]
        if labels.get("model_name") or labels.get("model"):
            summary["model"] = labels.get("model_name") or labels.get("model")
        if labels.get("priority"):
            continue
        rank = labels.get("dp_rank", "0")
        if name in WORKER_GAUGE_SUM:
            ranks.add(rank)
            bucket = per_rank.setdefault(WORKER_GAUGE_SUM[name], {})
            bucket[rank] = max(bucket.get(rank, 0.0), value)
        elif name in WORKER_GAUGE_MAX:
            key = WORKER_GAUGE_MAX[name]
            maxima[key] = max(maxima.get(key, 0.0), value)
        elif name in WORKER_COUNTERS or name == "sglang:prefill_effective_tokens_total":
            if name in WORKER_COUNTERS:
                key = WORKER_COUNTERS[name]
            else:       # tokens the prefill actually computed ("input") against tokens served from a cache ("*_hit")
                mode = labels.get("mode", "")
                key = "computed_tokens" if mode == "input" else ("hit_tokens" if mode.endswith("_hit") else None)
                if key is None:
                    continue
            # One series per label set of the token manager; replicas across TP ranks carry the same value.
            identity = tuple(sorted((k, v) for k, v in labels.items() if k not in ("tp_rank", "pp_rank", "moe_ep_rank")))
            bucket = counters.setdefault(key, {})
            bucket[identity] = max(bucket.get(identity, 0.0), value)
    for key, bucket in per_rank.items():
        summary[key] = sum(bucket.values())
    summary.update(maxima)
    for key, bucket in counters.items():
        summary[key] = sum(bucket.values())
    summary["dp_ranks"] = len(ranks)
    return summary


def summarize_frontend_health(payload):
    """Dynamo frontend /health -> status plus one entry per worker instance (generate endpoints only)."""
    result = {"status": str(payload.get("status", "unknown")), "instances": []}
    if payload.get("stage"):
        result["stage"] = str(payload["stage"])
    seen = set()
    for item in payload.get("instances") or []:
        if not isinstance(item, dict) or item.get("endpoint") != "generate":
            continue
        transport = item.get("transport") or {}
        address = ""
        if isinstance(transport, dict):
            for value in transport.values():
                if isinstance(value, str):
                    address = value
                    break
        host, _, tail = address.partition(":")
        key = (item.get("component"), item.get("instance_id"))
        if key in seen:
            continue
        seen.add(key)
        result["instances"].append({
            "namespace": item.get("namespace"),
            "component": item.get("component"),
            "id": str(item.get("instance_id")),
            "host": host or None,
            "port": to_port(tail.split("/")[0]) if tail else None,
        })
    return result


SOCKET_USERS = re.compile(r'\("([^"]*)",pid=(\d+),fd=\d+\)')


def parse_socket_address(text):
    """'10.0.0.1:80', '[::ffff:10.0.0.1]:80', '*:80', '127.0.0.53%lo:53' -> (host, port); (None, None) if unusable."""
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        return None, None
    host = host.strip("[]").split("%", 1)[0]
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in ("*", "::", ""):
        host = "0.0.0.0"
    return host, int(port)


def owners_of(table, roots):
    """{pid: root pid} for every process that is, or descends from, one of the `roots` (the service components)."""
    owner = {}
    for pid in table:
        chain = []
        walker = pid
        found = None
        while walker > 1 and len(chain) < 64:
            if walker in owner:
                found = owner[walker]
                break
            if walker in roots:
                found = walker
                break
            chain.append(walker)
            walker = table.get(walker, ("", "", 0, 0))[2]
        if found is not None:
            for member in chain:
                owner[member] = found
            owner[pid] = found
    return owner


def summarize_sockets(listen_text, established_text, owner, limit=MAX_SOCKET_ROWS):
    """TCP sockets held by the service process trees, from `ss -Htlnp` and `ss -Htnp` output.

    Reading the kernel's socket table sends nothing to anybody. A connection whose local port is one the same
    tree listens on was accepted by it ("in"); any other connection was opened by it ("out"). Returns
    {"listen": [[root, comm, host, port]], "out": [[root, comm, peer host, peer port, connections]],
     "in": [[root, comm, local port, peer host, connections]]}.
    """
    listeners = {}
    for line in listen_text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        host, port = parse_socket_address(parts[3])
        if port is None:
            continue
        for comm, pid in SOCKET_USERS.findall(line):
            root = owner.get(int(pid))
            if root is not None:
                listeners.setdefault((root, port), (comm, host))
                break
    outgoing, incoming = {}, {}
    for line in established_text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "ESTAB":
            continue
        local_host, local_port = parse_socket_address(parts[3])
        peer_host, peer_port = parse_socket_address(parts[4])
        if local_port is None or peer_port is None:
            continue
        seen = set()
        for comm, pid in SOCKET_USERS.findall(line):
            root = owner.get(int(pid))
            if root is None or root in seen:
                continue
            seen.add(root)
            if (root, local_port) in listeners:
                key = (root, listeners[(root, local_port)][0], local_port, peer_host)
                incoming[key] = incoming.get(key, 0) + 1
            else:
                key = (root, comm, peer_host, peer_port)
                outgoing[key] = outgoing.get(key, 0) + 1
    def rows(counts):
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [list(key) + [count] for key, count in ordered]
    listen = sorted([root, comm, host, port] for (root, port), (comm, host) in listeners.items())[:limit]
    return {"listen": listen, "out": rows(outgoing), "in": rows(incoming)}


def process_groups(table, owner, gpu_of=None, limit=12):
    """{root pid: [{"comm", "n", "gpus"}]}: the live processes of each service tree, grouped by process name."""
    groups = {}
    for pid, root in owner.items():
        entry = table.get(pid)
        if not entry or entry[1] == "Z":
            continue
        bucket = groups.setdefault(root, {}).setdefault(entry[0], {"comm": entry[0], "n": 0, "gpus": set(), "root": False})
        bucket["n"] += 1
        bucket["root"] = bucket["root"] or pid == root
        bucket["gpus"].update((gpu_of or {}).get(pid, ()))
    result = {}
    for root, buckets in groups.items():
        ordered = sorted(buckets.values(), key=lambda item: (not item["root"], -len(item["gpus"]), -item["n"], item["comm"]))[:limit]
        result[root] = [{"comm": item["comm"], "n": item["n"], "gpus": sorted(item["gpus"])} for item in ordered]
    return result


def select_mounts(mounts_text, minimum_gib=100):
    """Local mount points worth reporting: /, /dev/shm and large local data volumes. Network file systems are
    left out on purpose (statvfs on a stale mount blocks)."""
    chosen = []
    seen = set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount, kind = parts[1], parts[2]
        if kind not in LOCAL_FS or mount in seen:
            continue
        if kind == "tmpfs" and mount != "/dev/shm":
            continue
        if kind == "overlay" and mount != "/":
            continue
        if mount.startswith(("/var/lib/docker", "/var/lib/kubelet", "/run", "/sys", "/proc", "/snap", "/boot")):
            continue
        seen.add(mount)
        chosen.append((mount, kind))
    result = []
    for mount, kind in chosen:
        try:
            stat = os.statvfs(mount)
        except OSError:
            continue
        total = stat.f_blocks * stat.f_frsize
        if total <= 0:
            continue
        if mount not in ("/", "/dev/shm") and total < minimum_gib * (1 << 30):
            continue
        result.append({"mount": mount, "fs": kind, "total": total, "used": total - stat.f_bavail * stat.f_frsize})
    return result[:8]


# ------------------------------------------------------------------------------------------------- helpers
def run(command, timeout=10):
    """(returncode, stdout, stderr); returncode is None when the command is missing or timed out."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        return None, "", str(error)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None, "", "timed out after %ss" % timeout
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def die_with_parent():
    """preexec_fn: ask the kernel to terminate this child when the collector dies (Linux)."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def local_ips():
    """IPv4 addresses of this host, used to match service instances to nodes."""
    code, out, err = run(["hostname", "-I"], timeout=3)
    return [item for item in out.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", item)][:64]


def user_name(uid):
    if pwd is not None:
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            pass
    return str(uid)


def process_uid(pid):
    try:
        return os.stat("/proc/%d" % pid).st_uid
    except OSError:
        return None


def process_argv(pid):
    raw = read_text("/proc/%d/cmdline" % pid, 65536)
    if not raw:
        return []
    if "\0" not in raw:
        return raw.split()
    parts = raw.split("\0")
    while parts and parts[-1] == "":       # setproctitle pads the old argv area with NULs
        parts.pop()
    return parts


def process_env(pid):
    """Whitelisted variables of a process, or None when its environment cannot be read."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as handle:
            raw = handle.read(1 << 20)
    except (IOError, OSError):
        return None
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key.decode("ascii", "replace") in ENV_KEYS:
            env[key.decode("ascii", "replace")] = value.decode("utf-8", "replace")[:300]
    return env


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url, timeout=2.0, limit=4 << 20):
    """(status, body bytes). Proxies are bypassed. Raises OSError-family errors on connection problems."""
    request = urllib.request.Request(url, headers={"User-Agent": "farsight-collector/" + VERSION})
    try:
        response = OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read(limit)
        finally:
            error.close()
    try:
        return response.getcode(), response.read(limit)
    finally:
        response.close()


def greets_first(host, port, wait=0.25):
    """True if the listener speaks before the client does (ZeroMQ, SSH, ...). Nothing is sent."""
    try:
        connection = socket.create_connection((host, port), timeout=wait)
    except (socket.timeout, OSError):
        return False
    try:
        connection.settimeout(wait)
        return len(connection.recv(1)) > 0
    except (socket.timeout, OSError):
        return False
    finally:
        connection.close()


# ------------------------------------------------------------------------------------------------- GPU sampling
class GpuSampler(object):
    """Per-GPU load from one long-running `nvidia-smi -lms` process; falls back to one query per sample."""

    def __init__(self, interval, count):
        self.interval = interval
        self.count = count
        self.latest = None        # (monotonic time, [gpu sample])
        self.error = None
        self.process = None
        self.failures = 0
        self.lock = threading.Lock()
        self.stopped = False
        self.query = "--query-gpu=" + ",".join(GPU_LIVE_FIELDS)

    def start(self):
        if self.count:
            thread = threading.Thread(target=self.loop, name="gpu-sampler")
            thread.daemon = True
            thread.start()

    def loop(self):
        while not self.stopped and self.failures < 3:
            try:
                self.process = subprocess.Popen(
                    ["nvidia-smi", self.query, "--format=csv,noheader,nounits", "-lms", str(int(self.interval * 1000))],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=die_with_parent)
            except OSError as error:
                self.error = str(error)
                self.failures = 3
                break
            started = time.time()
            batch = []
            for raw in iter(self.process.stdout.readline, b""):
                rows = parse_gpu_csv(raw.decode("utf-8", "replace"), GPU_LIVE_FIELDS)
                if not rows:
                    continue
                if batch and int(rows[0]["index"]) <= int(batch[-1]["index"]):
                    batch = []
                batch.append(rows[0])
                if len(batch) >= self.count:
                    with self.lock:
                        self.latest = (time.time(), gpu_sample(batch))
                        self.error = None
                    batch = []
            err = self.process.stderr.read().decode("utf-8", "replace").strip()
            self.process.wait()
            if self.stopped:
                return
            self.error = err[-300:] or "nvidia-smi exited with %s" % self.process.returncode
            self.failures = 0 if time.time() - started > 60 else self.failures + 1
            time.sleep(2)

    def read(self):
        """(sample or None, age in seconds or None, error or None)."""
        with self.lock:
            latest = self.latest
        if latest and time.time() - latest[0] <= max(3 * self.interval, 6):
            return latest[1], time.time() - latest[0], None
        if not self.count:
            return [], None, None
        if self.failures >= 3 or latest is None:
            code, out, err = run(["nvidia-smi", self.query, "--format=csv,noheader,nounits"], timeout=10)
            rows = parse_gpu_csv(out, GPU_LIVE_FIELDS)
            if rows:
                return gpu_sample(rows), 0.0, None
            return None, None, (err.strip() or self.error or "nvidia-smi returned no data")[-300:]
        return None, None, self.error or "GPU sample is stale"

    def stop(self):
        self.stopped = True
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


# ------------------------------------------------------------------------------------------------- collector
class Collector(object):
    def __init__(self, interval=2.0, slow=10.0, probe=True, skip_ports=DEFAULT_SKIP_PORTS, out=None):
        self.interval = interval
        self.slow_interval = slow
        self.probe = probe
        self.skip_ports = set(skip_ports)
        self.out = out or sys.stdout
        self.write_lock = threading.Lock()
        self.previous_cpu = None
        self.previous_net = None
        self.previous_net_time = None
        self.boot_time = 0
        self.ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.inventory = []
        self.sampler = None
        self.has_nvidia = shutil.which("nvidia-smi") is not None
        self.has_docker = shutil.which("docker") is not None
        self.has_ss = shutil.which("ss") is not None
        self.inspect_cache = {}
        self.config_cache = {}    # worker pid -> whitelisted values of its --config file
        self.talkers = set()      # (pid, port) of listeners that greeted first: never sent HTTP
        self.checked = set()      # (pid, port) already passed the passive check
        self.unready = {}         # worker pid -> (time, health) of its last "not ready" answer
        self.listed = {}          # native SGLang server pid -> (time, models, status) of its last /v1/models answer
        self.slow_busy = False

    # -- output
    def emit(self, message):
        line = json.dumps(message, separators=(",", ":"))
        with self.write_lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (IOError, OSError, ValueError):
                self.shutdown()

    def shutdown(self):
        if self.sampler is not None:
            self.sampler.stop()
        os._exit(0)

    # -- hello
    def hello(self):
        errors = {}
        stat = parse_proc_stat(read_text("/proc/stat"))
        self.boot_time = stat["btime"]
        model = ""
        for line in read_text("/proc/cpuinfo", 1 << 16).splitlines():
            if line.startswith("model name"):
                model = line.partition(":")[2].strip()
                break
        pretty = ""
        for line in read_text("/etc/os-release", 1 << 14).splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.partition("=")[2].strip().strip('"')
        if self.has_nvidia:
            code, out, err = run(["nvidia-smi", "--query-gpu=" + ",".join(GPU_STATIC_FIELDS), "--format=csv,noheader,nounits"], timeout=20)
            self.inventory = gpu_inventory(parse_gpu_csv(out, GPU_STATIC_FIELDS))
            if not self.inventory:
                errors["nvidia-smi"] = (err.strip() or out.strip() or "no GPU reported")[-300:]
        uname = os.uname()
        uid = os.getuid()
        message = {
            "type": "hello", "v": VERSION, "t": time.time(),
            "hostname": uname[1], "kernel": uname[2], "arch": uname[4], "os": pretty,
            "cpu_model": model, "cores": len(stat["cores"]) or (os.cpu_count() or 0),
            "mem_total": parse_meminfo(read_text("/proc/meminfo"))["total"],
            "boot_time": self.boot_time, "python": sys.version.split()[0], "ips": local_ips(),
            "user": user_name(uid), "root": uid == 0,
            "caps": {"nvidia_smi": self.has_nvidia, "docker": self.has_docker, "ss": self.has_ss},
            "gpus": self.inventory, "interval": self.interval, "slow": self.slow_interval,
        }
        if errors:
            message["errors"] = errors
        self.emit(message)
        self.sampler = GpuSampler(self.interval, len(self.inventory))
        self.sampler.start()

    # -- fast tier
    def fast(self):
        now = time.time()
        errors = {}
        stat = parse_proc_stat(read_text("/proc/stat"))
        previous = self.previous_cpu
        self.previous_cpu = stat
        cpu = None
        cores = []
        if previous:
            cpu = busy_percent(previous["total"], stat["total"])
            if len(previous["cores"]) == len(stat["cores"]):
                for before, after in zip(previous["cores"], stat["cores"]):
                    value = busy_percent(before, after)
                    cores.append(None if value is None else int(round(value)))
        rx, tx = parse_net_dev(read_text("/proc/net/dev"))
        net = None
        if self.previous_net is not None and now > self.previous_net_time:
            span = now - self.previous_net_time
            net = {"rx": max(0, int((rx - self.previous_net[0]) / span)), "tx": max(0, int((tx - self.previous_net[1]) / span))}
        self.previous_net, self.previous_net_time = (rx, tx), now
        load = read_text("/proc/loadavg", 256).split()
        gpus, age, gpu_error = (self.sampler.read() if self.sampler else ([], None, None))
        if gpu_error:
            errors["nvidia-smi"] = gpu_error
        message = {
            "type": "fast", "t": now,
            "cpu": None if cpu is None else round(cpu, 1), "cores": cores,
            "load": [float(value) for value in load[:3]] if len(load) >= 3 else [],
            "running": stat["running"],
            "mem": parse_meminfo(read_text("/proc/meminfo")),
            "gpus": gpus, "gpu_age": None if age is None else round(age, 1), "net": net,
        }
        if errors:
            message["errors"] = errors
        self.emit(message)

    # -- slow tier
    def scan_processes(self):
        """One pass over /proc: {pid: (comm, state, ppid, starttime)} and the argv of candidate service processes."""
        table = {}
        candidates = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            parsed = parse_stat_line(read_text("/proc/%d/stat" % pid, 4096))
            if not parsed:
                continue
            table[pid] = parsed
            comm = parsed[0]
            if parsed[1] != "Z" and comm.startswith(("python", "etcd", "sglang", "dynamo")):
                argv = process_argv(pid)
                if argv:
                    candidates[pid] = argv
        return table, candidates

    def docker_ps(self):
        """({id: {name, image, status}}, state) with state ok / unavailable / denied / error."""
        if not self.has_docker:
            return {}, "unavailable"
        code, out, err = run(["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"], timeout=8)
        if code != 0:
            return {}, "denied" if "permission denied" in err.lower() else "error"
        containers = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                containers[parts[0]] = {"name": parts[1], "image": parts[2], "status": parts[3]}
        return containers, "ok"

    def docker_inspect(self, ids):
        """Whitelisted details of the given containers, cached per container id."""
        missing = [cid for cid in ids if cid not in self.inspect_cache]
        if missing and self.has_docker:
            code, out, err = run(["docker", "inspect"] + missing, timeout=20)
            try:
                items = json.loads(out) if out.strip() else []
            except ValueError:
                items = []
            for item in items:
                config = item.get("Config") or {}
                host = item.get("HostConfig") or {}
                env = {}
                for pair in config.get("Env") or []:
                    key, sep, value = pair.partition("=")
                    if sep and key in ENV_KEYS:
                        env[key] = value[:300]
                devices = []
                for request in host.get("DeviceRequests") or []:
                    devices.extend(request.get("DeviceIDs") or [])
                    if request.get("Count") == -1 and not request.get("DeviceIDs"):
                        devices.append("all")
                self.inspect_cache[item.get("Id", "")] = {
                    "cmd": [str(part)[:200] for part in (config.get("Cmd") or [])][-6:],
                    "env": env, "devices": devices, "network": host.get("NetworkMode") or "",
                    "started": (item.get("State") or {}).get("StartedAt"),
                    "restarts": item.get("RestartCount", 0),
                }
        for cid in list(self.inspect_cache):
            if cid not in ids:
                del self.inspect_cache[cid]
        return dict((cid, self.inspect_cache[cid]) for cid in ids if cid in self.inspect_cache)

    def config_values(self, pid, argv):
        """Communication settings from the file a worker was started with (--config), read through the process's
        own root so that a path inside a container resolves. Only SETTING_KEYS are kept."""
        if pid in self.config_cache:
            return self.config_cache[pid]
        values = None
        path = option(argv, "--config")
        if path and path.startswith("/") and "\0" not in path:
            target = "/proc/%d/root%s" % (pid, path)
            try:
                if os.path.isfile(target) and os.path.getsize(target) <= (1 << 20):
                    values = parse_flat_yaml(read_text(target, 1 << 20))
            except (IOError, OSError):
                values = None
        self.config_cache[pid] = values
        return values

    def socket_table(self, owner):
        """Summary of the service trees' TCP sockets, or the reason it is unavailable."""
        if not self.has_ss:
            return {"error": "ss not found"}
        code, listening, err = run(["ss", "-Htlnp"], timeout=8)
        if code != 0:
            return {"error": (err.strip() or "ss failed")[-200:]}
        code, established, err = run(["ss", "-Htnp"], timeout=8)
        if code != 0:
            return {"error": (err.strip() or "ss failed")[-200:]}
        summary = summarize_sockets(listening, established, owner)
        if not summary["listen"]:
            # Every service component listens on something. Seeing none of it means the socket owners are hidden:
            # the kernel shows them only to root (or for one's own processes).
            return {"error": "sockets of the service processes are not visible to user %s" % user_name(os.getuid())}
        return summary

    def allowed(self, pid, host, port):
        """Whether an HTTP request may go to host:port, a port the process `pid` declared as HTTP."""
        if not self.probe or not port or port in self.skip_ports:
            return False
        key = (pid, port)
        if key in self.talkers:
            return False
        if key not in self.checked:
            if greets_first(host, port):
                self.talkers.add(key)
                return False
            self.checked.add(key)
        return True

    def fetch_json(self, url):
        status, body = http_get(url)
        try:
            return status, json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return status, None

    def probe_component(self, component):
        """Health and metrics of one component through its declared HTTP port."""
        pid = component["pid"]
        kind = component["kind"]
        probe = {}
        try:
            if kind == "dynamo-frontend":
                host, port = component["http_host"], component["ports"].get("http")
                if not self.allowed(pid, host, port):
                    return {"skipped": True}
                base = "http://%s:%d" % (host, port)
                status, payload = self.fetch_json(base + "/health")
                probe["health"] = summarize_frontend_health(payload) if isinstance(payload, dict) else {"status": "http %s" % status}
                status, body = http_get(base + "/metrics", timeout=3.0)
                if status == 200:
                    probe["metrics"] = summarize_frontend_metrics(body.decode("utf-8", "replace"))
                    probe["models"] = probe["metrics"]["models"][:16]
            elif kind == "dynamo-worker":
                port = component["ports"].get("system")
                if not self.allowed(pid, "127.0.0.1", port):
                    return {"skipped": True}
                base = "http://127.0.0.1:%d" % port
                status, body = http_get(base + "/metrics", timeout=3.0)
                if status == 200:
                    probe["metrics"] = summarize_worker_metrics(body.decode("utf-8", "replace"))
                # /health answers 503 until the worker is ready, and the worker logs every 503 as an error. So it is
                # asked only once the metrics page shows that the endpoints exist, and after a "not ready" answer
                # it is left alone for HEALTH_BACKOFF seconds (the previous answer is reported meanwhile).
                held = self.unready.get(pid)
                if not (probe.get("metrics") or {}).get("serving"):
                    probe["health"] = {"status": "notready"}
                elif held and time.time() - held[0] < HEALTH_BACKOFF:
                    probe["health"] = held[1]
                else:
                    status, payload = self.fetch_json(base + "/health")
                    if isinstance(payload, dict):
                        endpoints = payload.get("endpoints")
                        probe["health"] = {"status": str(payload.get("status", "http %s" % status)),
                                           "endpoints": endpoints if isinstance(endpoints, dict) else None}
                    else:
                        probe["health"] = {"status": "http %s" % status}
                    if status == 200:
                        self.unready.pop(pid, None)
                    else:
                        self.unready[pid] = (time.time(), probe["health"])
            elif kind == "sglang-server":
                host, port = component["http_host"], component["ports"].get("http")
                if not self.allowed(pid, host, port):
                    return {"skipped": True}
                base = "http://%s:%d" % (host, port)
                # /health is not used: in some builds it shares the handler of /health_generate and runs a generation.
                # The HTTP server only comes up once the model is loaded, so an answered /metrics means "ready";
                # /v1/models is asked only when metrics are disabled, and then at most once per HEALTH_BACKOFF.
                status, body = http_get(base + "/metrics", timeout=3.0)
                if status == 200:
                    probe["metrics"] = summarize_worker_metrics(body.decode("utf-8", "replace"))
                    probe["health"] = {"status": "ready"}
                else:
                    probe["metrics_disabled"] = True
                    listed = self.listed.get(pid)
                    if not listed or time.time() - listed[0] >= HEALTH_BACKOFF:
                        status, payload = self.fetch_json(base + "/v1/models")
                        models = None
                        if isinstance(payload, dict):
                            models = [str(item.get("id")) for item in payload.get("data") or [] if isinstance(item, dict)][:16]
                        listed = self.listed[pid] = (time.time(), models, status)
                    if listed[1] is not None:
                        probe["models"] = listed[1]
                        probe["health"] = {"status": "ready"}
                    else:
                        probe["health"] = {"status": "http %s" % listed[2]}
            elif kind == "etcd":
                port = component["ports"].get("client")
                if not self.allowed(pid, "127.0.0.1", port):
                    return {"skipped": True}
                status, payload = self.fetch_json("http://127.0.0.1:%d/health" % port)
                healthy = isinstance(payload, dict) and str(payload.get("health")).lower() == "true"
                probe["health"] = {"status": "healthy" if healthy else "http %s" % status}
        except Exception as error:  # connection refused while starting, timeouts, ...
            probe["error"] = "%s: %s" % (type(error).__name__, str(error)[:200])
        return probe

    def slow(self):
        started = time.time()
        errors = {}
        table, candidates = self.scan_processes()
        containers, docker_state = self.docker_ps()
        container_of = {}

        def cid_of(pid):
            if pid not in container_of:
                container_of[pid] = container_id(read_text("/proc/%d/cgroup" % pid, 8192))
            return container_of[pid]

        # service components
        matched = {}
        for pid, argv in candidates.items():
            found = classify_process(argv)
            if found:
                found["argv"] = argv
                matched[pid] = found
        for pid in list(matched):      # a forked child keeps its parent's command line: keep the topmost
            parent = table.get(pid, ("", "", 0, 0))[2]
            hops = 0
            while parent > 1 and hops < 64:
                if parent in matched and matched[parent]["kind"] == matched[pid]["kind"]:
                    del matched[pid]
                    break
                parent = table.get(parent, ("", "", 0, 0))[2]
                hops += 1
        details = self.docker_inspect(sorted(set(cid for cid in (cid_of(pid) for pid in matched) if cid)))
        components = []
        for pid in sorted(matched):
            found = matched[pid]
            cid = cid_of(pid)
            inspected = details.get(cid) or {}
            env = process_env(pid)
            if env:
                env_source = "process"
            else:       # unreadable, or already emptied because the process is exiting: use what the container declares
                env = dict(inspected.get("env") or {})
                env_source = "container" if inspected else None
            name = (containers.get(cid) or {}).get("name")
            stat = table.get(pid)
            uid = process_uid(pid)
            file_values = self.config_values(pid, found["argv"]) if found["kind"] in ("dynamo-worker", "sglang-server") else None
            component = describe_component(pid, found, env, env_source, inspected.get("cmd"), name, file_values)
            component["user"] = None if uid is None else user_name(uid)
            component["started"] = self.boot_time + stat[3] / float(self.ticks) if stat and self.boot_time else None
            component["cid"] = cid[:12] if cid else None
            component["probe"] = self.probe_component(component)
            components.append(component)
        self.talkers = set(key for key in self.talkers if key[0] in table)
        self.checked = set(key for key in self.checked if key[0] in table)
        self.unready = dict((pid, value) for pid, value in self.unready.items() if pid in table)
        self.listed = dict((pid, value) for pid, value in self.listed.items() if pid in table)
        self.config_cache = dict((pid, value) for pid, value in self.config_cache.items() if pid in matched)
        owner = owners_of(table, matched)

        # GPU processes
        gpu_processes = []
        if self.inventory:
            code, out, err = run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory,process_name",
                                  "--format=csv,noheader,nounits"], timeout=10)
            if code != 0:
                errors["compute-apps"] = (err.strip() or out.strip())[-200:]
            index_of = dict((gpu["uuid"], gpu["index"]) for gpu in self.inventory)
            for pid, uuid, memory, name in parse_compute_apps(out):
                root = owner.get(pid)
                cid = cid_of(pid)
                uid = process_uid(pid)
                stat = table.get(pid)
                gpu_processes.append({
                    "pid": pid, "gpu": index_of.get(uuid), "mem": memory,
                    "name": name[:80] or (stat[0] if stat else ""),
                    "user": None if uid is None else user_name(uid),
                    "cid": cid[:12] if cid else None, "root": root,
                })
        gpu_of = {}
        for process in gpu_processes:
            if process["gpu"] is not None:
                gpu_of.setdefault(process["pid"], set()).add(process["gpu"])
        groups = process_groups(table, owner, gpu_of)
        for component in components:
            component["procs"] = groups.get(component["pid"], [])
        sockets = self.socket_table(owner) if components else {"listen": [], "out": [], "in": []}
        zombies = sum(1 for parsed in table.values() if parsed[1] == "Z")
        sglang_zombies = sum(1 for parsed in table.values() if parsed[1] == "Z" and parsed[0].startswith("sglang"))
        short = {}
        for cid, info in containers.items():
            entry = dict(info)
            entry.update(details.get(cid) or {})
            entry.pop("env", None)
            short[cid[:12]] = entry
        message = {
            "type": "slow", "t": time.time(), "took_ms": int((time.time() - started) * 1000),
            "components": components, "gpu_procs": gpu_processes, "containers": short, "docker": docker_state,
            "sockets": sockets,
            "disks": select_mounts(read_text("/proc/mounts")),
            "processes": len(table), "zombies": zombies, "sglang_zombies": sglang_zombies,
        }
        if errors:
            message["errors"] = errors
        self.emit(message)

    def slow_guarded(self):
        try:
            self.slow()
        except Exception as error:
            self.emit({"type": "error", "where": "slow", "message": "%s: %s" % (type(error).__name__, str(error)[:300])})
        finally:
            self.slow_busy = False

    # -- main loop
    def run(self, once=False):
        self.hello()
        self.previous_cpu = parse_proc_stat(read_text("/proc/stat"))
        self.previous_net, self.previous_net_time = parse_net_dev(read_text("/proc/net/dev")), time.time()
        time.sleep(0.5)
        if once:
            deadline = time.time() + 5
            while self.inventory and self.sampler.latest is None and self.sampler.failures < 3 and time.time() < deadline:
                time.sleep(0.1)
            self.fast()
            self.slow()
            self.sampler.stop()
            return
        next_fast = time.monotonic()
        next_slow = next_fast
        while True:
            now = time.monotonic()
            if now >= next_slow and not self.slow_busy:
                self.slow_busy = True
                next_slow = now + self.slow_interval
                thread = threading.Thread(target=self.slow_guarded, name="slow-tier")
                thread.daemon = True
                thread.start()
            try:
                self.fast()
            except Exception as error:
                self.emit({"type": "error", "where": "fast", "message": "%s: %s" % (type(error).__name__, str(error)[:300])})
            next_fast += self.interval
            delay = next_fast - time.monotonic()
            if delay < 0:
                next_fast = time.monotonic()
                delay = 0
            time.sleep(delay)


def watch_stdin(collector):
    """EOF on stdin means the dashboard (or the SSH session) is gone: stop at once."""
    try:
        while sys.stdin.readline():
            pass
    except Exception:
        pass
    collector.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description="farsight node collector (read-only)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between fast samples")
    parser.add_argument("--slow", type=float, default=10.0, help="seconds between slow samples")
    parser.add_argument("--once", action="store_true", help="print one hello / fast / slow and exit")
    parser.add_argument("--no-probe", action="store_true", help="never contact service HTTP endpoints")
    parser.add_argument("--skip-ports", default=",".join(str(port) for port in DEFAULT_SKIP_PORTS))
    parser.add_argument("--no-stdin-watch", action="store_true", help="keep running after stdin closes")
    args = parser.parse_args(argv)
    skip = [int(port) for port in args.skip_ports.split(",") if port.strip().isdigit()]
    collector = Collector(interval=max(0.5, args.interval), slow=max(2.0, args.slow), probe=not args.no_probe, skip_ports=skip)
    if not args.once and not args.no_stdin_watch:
        watcher = threading.Thread(target=watch_stdin, args=(collector,), name="stdin-watch")
        watcher.daemon = True
        watcher.start()
    try:
        collector.run(once=args.once)
    except KeyboardInterrupt:
        pass
    finally:
        if collector.sampler is not None:
            collector.sampler.stop()


if __name__ == "__main__":
    main()
