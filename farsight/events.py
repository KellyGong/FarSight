"""Activity log and threshold alerts.

An alert becomes active only after its condition has held for its `sustain` seconds, and clears after the
condition has been absent for CLEAR_AFTER seconds, so a single noisy sample neither raises nor drops it.
"""
import collections
import itertools
import threading
import time

from .i18n import T

CLEAR_AFTER = 12.0

DEFAULT_THRESHOLDS = {
    "sustain_seconds": 60,
    "gpu_temp_warn": 85,          # degrees C
    "gpu_temp_crit": 92,
    "memory_percent": 92,         # host memory, shared memory included
    "disk_percent": 90,           # /
    "shm_percent": 95,            # /dev/shm
    "vram_percent": 99.5,         # inference servers preallocate most of the card, so this is deliberately high
    "idle_spin_util": 90,         # GPU busy with no memory and no process: teardown residue
    "idle_spin_mib": 300,
    "error_ratio": 0.01,
    "ttft_p95_ms": 10000,
    "queue_seconds": 120,         # worker scheduler queue non-empty for this long
}


class EventLog(object):
    def __init__(self, capacity=600):
        self.items = collections.deque(maxlen=capacity)
        self.counter = itertools.count(1)
        self.lock = threading.Lock()

    def add(self, level, scope, scope_id, name, title, detail="", stamp=None):
        """level: info | ok | warn | crit; scope: node | service | system."""
        event = {"id": next(self.counter), "t": stamp or time.time(), "level": level, "scope": scope,
                 "scope_id": scope_id, "name": name, "title": title, "detail": detail}
        with self.lock:
            self.items.append(event)
        return event

    def recent(self, limit=50, scope=None, scope_ids=None, since_id=0):
        with self.lock:
            items = list(self.items)
        chosen = []
        for event in reversed(items):
            if event["id"] <= since_id:
                break
            if scope_ids is not None:
                if (event["scope"], event["scope_id"]) not in scope_ids:
                    continue
            elif scope and event["scope"] != scope:
                continue
            chosen.append(event)
            if len(chosen) >= limit:
                break
        return chosen


class Alerts(object):
    """Tracks which conditions are pending, active or cleared, and writes transitions to the event log."""

    def __init__(self, log):
        self.log = log
        self.pending = {}     # key -> first time the condition was seen
        self.active = {}      # key -> alert
        self.last_seen = {}   # key -> last time the condition held

    def evaluate(self, conditions, now=None):
        """conditions: iterable of dicts with key, level, scope, scope_id, name, title, detail, sustain."""
        now = time.time() if now is None else now
        present = set()
        for condition in conditions:
            key = condition["key"]
            present.add(key)
            self.last_seen[key] = now
            if key in self.active:
                self.active[key].update(detail=condition.get("detail", ""), level=condition["level"], title=condition["title"])
                continue
            first = self.pending.setdefault(key, now)
            if now - first >= condition.get("sustain", 0):
                alert = dict(condition, since=first)
                alert.pop("sustain", None)
                self.active[key] = alert
                self.log.add(condition["level"], condition["scope"], condition["scope_id"], condition["name"],
                             condition["title"], condition.get("detail", ""), stamp=now)
        for key in list(self.pending):
            if key not in present:
                del self.pending[key]
        for key in list(self.active):
            if key not in present and now - self.last_seen.get(key, 0) >= CLEAR_AFTER:
                alert = self.active.pop(key)
                self.pending.pop(key, None)
                self.log.add("ok", alert["scope"], alert["scope_id"], alert["name"], T("已恢复：{title}", title=alert["title"]), "", stamp=now)
        for key in list(self.last_seen):
            if key not in present and key not in self.active:
                del self.last_seen[key]
        return self.current()

    def drop_scope(self, scope, scope_id):
        """Forget everything about a removed node or service without logging recoveries."""
        for store in (self.pending, self.active, self.last_seen):
            for key in [key for key in store if key.startswith("%s:%s:" % (scope, scope_id))]:
                del store[key]

    def current(self):
        order = {"crit": 0, "warn": 1, "info": 2}
        return sorted(self.active.values(), key=lambda alert: (order.get(alert["level"], 3), -alert["since"]))


def node_conditions(snapshot, summary, thresholds):
    """Alert conditions of one online node. `summary` is the node's public summary (name, ...)."""
    conditions = []
    node_id = snapshot["config"]["id"]
    name = summary["name"]
    sustain = thresholds["sustain_seconds"]

    def add(code, level, title, detail, hold=sustain):
        conditions.append({"key": "node:%s:%s" % (node_id, code), "level": level, "scope": "node", "scope_id": node_id,
                           "name": name, "title": title, "detail": detail, "sustain": hold})

    fast = snapshot.get("fast") or {}
    slow = snapshot.get("slow") or {}
    busy = set(process.get("gpu") for process in slow.get("gpu_procs") or [])
    for gpu in fast.get("gpus") or []:
        index = gpu.get("i")
        temp = gpu.get("temp")
        if temp is not None and temp >= thresholds["gpu_temp_crit"]:
            add("gpu%s:temp" % index, "crit", T("GPU {index} 温度过高", index=index), "%.0f°C" % temp, 20)
        elif temp is not None and temp >= thresholds["gpu_temp_warn"]:
            add("gpu%s:temp" % index, "warn", T("GPU {index} 温度偏高", index=index), "%.0f°C" % temp)
        util, memory, total = gpu.get("util"), gpu.get("mem"), gpu.get("mem_total")
        if (util is not None and memory is not None and util >= thresholds["idle_spin_util"]
                and memory <= thresholds["idle_spin_mib"] and index not in busy and slow):
            add("gpu%s:spin" % index, "warn", T("GPU {index} 空转", index=index),
                T("利用率 {util}%，但显存 {memory} MiB 且没有进程。常见于服务异常停止后的残留，可能需要重置该 GPU。",
                  util="%.0f" % util, memory="%.0f" % memory))
        if memory is not None and total and 100.0 * memory / total >= thresholds["vram_percent"]:
            add("gpu%s:vram" % index, "warn", T("GPU {index} 显存已满", index=index), "%.1f / %.1f GiB" % (memory / 1024.0, total / 1024.0))
    memory = fast.get("mem") or {}
    if memory.get("total"):
        percent = 100.0 * memory.get("used", 0) / memory["total"]
        if percent >= thresholds["memory_percent"]:
            add("memory", "warn", T("主机内存紧张"), T("已用 {percent}%（含共享内存 {shm} GiB）", percent="%.0f" % percent,
                                                    shm="%.0f" % (memory.get("shmem", 0) / 1024.0)))
    for disk in slow.get("disks") or []:
        if not disk.get("total"):
            continue
        percent = 100.0 * disk["used"] / disk["total"]
        if disk["mount"] == "/dev/shm" and percent >= thresholds["shm_percent"]:
            add("shm", "warn", T("/dev/shm 接近写满"), T("已用 {percent}%。新启动的服务可能因共享内存不足而失败。", percent="%.0f" % percent))
        elif disk["mount"] == "/" and percent >= thresholds["disk_percent"]:
            add("disk", "warn", T("系统盘接近写满"), T("已用 {percent}%", percent="%.0f" % percent))
    if slow.get("sglang_zombies"):
        add("zombies", "info", T("存在僵尸 SGLang 进程"), T("{count} 个已退出但未被回收的 sglang 进程", count=slow["sglang_zombies"]), 0)
    return conditions


def service_conditions(service, thresholds):
    conditions = []
    sustain = thresholds["sustain_seconds"]
    metrics = service.get("metrics") or {}

    def add(code, level, title, detail, hold=sustain):
        conditions.append({"key": "service:%s:%s" % (service["id"], code), "level": level, "scope": "service",
                           "scope_id": service["id"], "name": service["name"], "title": title, "detail": detail, "sustain": hold})

    if service["status"] == "degraded":
        add("degraded", "crit", T("服务降级"), service.get("status_detail") or T("部分组件不健康"), 10)
    ratio = metrics.get("err_ratio")
    if ratio is not None and ratio > thresholds["error_ratio"]:
        add("errors", "warn", T("请求错误率偏高"), "%.1f%%" % (100.0 * ratio))
    ttft = metrics.get("ttft_p95")
    if ttft is not None and ttft * 1000.0 > thresholds["ttft_p95_ms"]:
        add("ttft", "warn", T("首 Token 延迟偏高"), "P95 %.1f s" % ttft)
    queued = metrics.get("worker_queued")
    if queued:
        add("queue", "info", T("调度队列持续积压"), T("worker 侧等待 {count} 个请求", count=int(queued)), thresholds["queue_seconds"])
    return conditions
