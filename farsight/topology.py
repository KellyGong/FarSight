"""Communication topology: which component talks to which, over what protocol, and how that is known.

The collector reports the TCP sockets held by the service process trees (it reads the kernel's socket table and
sends nothing). Here those rows are joined across nodes into links, each link is named after the role of the
port it goes to, and the links are projected twice: between servers (cluster view) and inside one server.

Evidence of a link:
  observed  an established TCP connection was seen in a socket table
  declared  the service's configuration or discovery data says so, but no socket table is available
  inferred  it cannot appear in a socket table at all (RDMA bypasses the kernel) and follows from the settings
"""
import collections
import re

from .i18n import T

LOOPBACK = ("127.0.0.1", "0.0.0.0", "::1", "localhost", "")
MAX_PORTS = 6

# kind -> (plane, what it carries, protocol). Planes: request | control | kv | internal | monitor | other.
KINDS = {
    "http": ("request", T("推理请求（OpenAI API）"), "HTTP"),
    "request": ("request", T("Dynamo 请求面"), "TCP"),
    "response": ("request", T("Dynamo 响应流（worker 回连 Frontend）"), "TCP"),
    "events": ("control", T("Dynamo 事件面（KV 事件与负载）"), "ZeroMQ"),
    "etcd": ("control", T("服务发现与注册"), "etcd gRPC"),
    "etcd_peer": ("control", T("etcd 成员同步"), "gRPC"),
    "bootstrap": ("kv", T("KV 传输握手（Bootstrap）"), "HTTP"),
    "kv_ctrl": ("kv", T("KV 传输控制"), "ZeroMQ"),
    "kv_data": ("kv", T("KV 缓存传输"), "NIXL"),
    "flexkv": ("kv", T("FlexKV 跨节点缓存（radix-server）"), "RPC / NIXL"),
    "kv_events": ("control", T("KV 事件上报"), "ZeroMQ"),
    "ipc": ("internal", T("引擎进程间通信"), "ZeroMQ"),
    "ranks": ("internal", T("引擎各 rank 之间的连接"), "TCP"),
    "dist": ("internal", T("多卡协调（torch.distributed）"), "TCP"),
    "metrics": ("monitor", T("监控端点（/metrics、/health）"), "HTTP"),
    "ssh": ("monitor", T("面板采集（只读）"), "SSH"),
    "tcp": ("other", T("其他连接"), "TCP"),
}
PLANE_ORDER = {"request": 0, "kv": 1, "control": 2, "internal": 3, "monitor": 4, "other": 5}
EVIDENCE_ORDER = {"observed": 0, "declared": 1, "inferred": 2}


def transfer_protocol(*components):
    """(full name, short name) of the KV transfer path of a Prefill / Decode pair, from the engine settings and
    environment; (None, None) when neither says which backend is used."""
    transfer = backend = tls = ""
    for component in components:
        transfer = transfer or (component.get("settings") or {}).get("transfer") or ""
        env = component.get("env") or {}
        backend = backend or env.get("SGLANG_DISAGGREGATION_NIXL_BACKEND") or ""
        tls = tls or env.get("UCX_TLS") or ""
    if not transfer and backend:
        transfer = "nixl"
    if not transfer:
        return None, None
    name = {"nixl": "NIXL", "mooncake": "Mooncake"}.get(transfer.lower(), transfer)
    parts = [name]
    if backend:
        parts.append(backend.upper())
    wire = "RDMA" if re.search(r"(^|,)(rc|dc|ud)(_|,|$)", tls) else ("TCP" if tls else None)
    if wire:
        parts.append(wire)
    return " · ".join(parts), "%s/%s" % (name, wire) if wire else name


def classify(source, target, port, comm, instance_ports, loopback=True):
    """Kind of a connection to `port` of the component `target` (None when the listener is not a service).

    `loopback` tells whether it goes to 127.0.0.1: the engine's own message queues do (ZeroMQ over TCP when DP
    attention is on); connections among its ranks over the host's addresses are something else and stay unnamed.
    """
    if target is None:
        return "tcp"
    kind = target["kind"]
    ports = target.get("ports") or {}
    settings = target.get("settings") or {}
    if kind == "etcd":
        return "etcd" if port == ports.get("client") else "etcd_peer"
    if kind == "sglang-router":
        return "http" if port == ports.get("http") else "tcp"
    if kind == "dynamo-frontend":
        if port == ports.get("http"):
            return "http"
        if source is not None and source["kind"] == "dynamo-worker":
            return "response"
        if source is not None and source["key"] == target["key"]:
            return "events"
        return "tcp"
    if comm == "radix-server":
        return "flexkv"
    if port == ports.get("system"):
        return "metrics"
    if kind == "sglang-server" and port == ports.get("http"):
        return "http"
    if port == settings.get("bootstrap_port") and target.get("role") != "decode":
        return "bootstrap"
    if source is not None and source["key"] == target["key"]:
        if port == settings.get("nccl_port"):
            return "dist"
        base = settings.get("kv_events_port")
        if base and base <= port < base + max(1, settings.get("dp") or 16):
            return "kv_events"
        return "ipc" if loopback else "ranks"
    if source is not None and source["kind"] == "dynamo-frontend":
        known = instance_ports.get(target["key"])
        return "request" if not known or port in known else "events"
    if source is not None and source["kind"] in ("dynamo-worker", "sglang-server"):
        return "kv_ctrl" if (comm or "").startswith("sglang::") else "tcp"
    return "tcp"


class Topology(object):
    """Links of one moment, built from the node snapshots and the full service list of the aggregator."""

    def __init__(self, snapshots, services):
        self.nodes = collections.OrderedDict()      # node id -> {"name", "host", "status", "ips", "sockets", ...}
        self.components = {}                        # component key -> component (with "service", "service_name")
        self.links = []
        self.services = dict((service["id"], service) for service in services if not service.get("stale"))
        self.index(snapshots)
        self.observe()
        self.declare()
        self.infer()

    # -- indexes
    def index(self, snapshots):
        seen = collections.Counter()
        for snapshot in snapshots:
            node_id = snapshot["config"]["id"]
            hello = snapshot.get("hello") or {}
            online = snapshot["status"] == "online"
            slow = (snapshot.get("slow") or {}) if online else {}
            sockets = slow.get("sockets") or {}
            ips = set(ip for ip in hello.get("ips") or [] if ip not in LOOPBACK)
            ips.add(snapshot["config"].get("host"))
            for ip in ips:
                seen[ip] += 1
            inventory = hello.get("gpus") or []
            self.nodes[node_id] = {
                "id": node_id, "name": hello.get("hostname") or snapshot["config"].get("label") or snapshot["config"].get("host"),
                "host": snapshot["config"].get("host"), "status": snapshot["status"], "auth": snapshot["config"].get("auth"),
                "port": snapshot["config"].get("port"), "ips": ips, "gpu_count": len(inventory),
                "gpu_model": collections.Counter(gpu.get("name") for gpu in inventory).most_common(1)[0][0] if inventory else None,
                "sockets": sockets if "listen" in sockets else None, "socket_error": sockets.get("error"),
                "listeners": {}, "raw": slow.get("components") or [],
            }
        self.address = {}
        for node_id, node in self.nodes.items():
            for ip in node["ips"]:
                if seen[ip] == 1:                   # an address several nodes have (docker0, ...) identifies none of them
                    self.address[ip] = node_id
        for service in self.services.values():
            for component in service["components"]:
                self.components[component["key"]] = dict(component, service=service["id"], service_name=service["name"])
        for node_id, node in self.nodes.items():   # components that belong to no service (an etcd nobody registers in)
            for raw in node["raw"]:
                key = "%s:%s" % (node_id, raw["pid"])
                if key not in self.components:
                    self.components[key] = {"key": key, "node": node_id, "kind": raw["kind"], "role": raw.get("role") or raw["kind"],
                                            "pid": raw["pid"], "ports": raw.get("ports") or {}, "settings": raw.get("settings") or {},
                                            "env": raw.get("env") or {}, "procs": raw.get("procs") or [], "service": None,
                                            "service_name": None, "container": None, "gpus": []}
            for root, comm, host, port in (node["sockets"] or {}).get("listen") or []:
                node["listeners"].setdefault(port, (root, comm))
        self.instance_ports = collections.defaultdict(set)   # worker key -> ports the frontend's discovery lists for it
        self.unmonitored = {}                                # host of a worker that is not a monitored node -> role, ports
        self.foreign_etcd = set()                            # (host, port) of an etcd that is not on a monitored node
        for service in self.services.values():
            for item in service.get("etcd") or []:
                host, _, port = item.rpartition(":")
                if host and host not in self.address and port.isdigit():
                    self.foreign_etcd.add((host, int(port)))
            for component in service["components"]:
                for item in ((component.get("probe") or {}).get("health") or {}).get("instances") or []:
                    target = self.address.get(item.get("host"))
                    if target is None and item.get("host"):
                        role = "prefill" if item.get("component") == "prefill" else ("decode" if service.get("mode") == "disagg" else "worker")
                        entry = self.unmonitored.setdefault(item["host"], {"role": role, "ports": set()})
                        if item.get("port"):
                            entry["ports"].add(item["port"])
                    if target is None or not item.get("port"):
                        continue
                    for other in service["components"]:
                        if other["node"] == target:
                            self.instance_ports[other["key"]].add(item["port"])

    def foreign_kind(self, component, host, port, comm, inbound):
        """Kind of a connection between a component and a host that is not monitored, from what discovery says."""
        if not inbound and (host, port) in self.foreign_etcd:
            return "etcd"
        worker = self.unmonitored.get(host)
        if worker is None:
            return None
        if component["kind"] == "dynamo-frontend":
            if inbound:
                return "response" if port != (component.get("ports") or {}).get("http") else None
            return "request" if port in worker["ports"] else "events"
        if component["kind"] in ("dynamo-worker", "sglang-server") and (comm or "").startswith("sglang::"):
            return "kv_ctrl"
        return None

    def node_of(self, host, here):
        if host in LOOPBACK or host in self.nodes[here]["ips"]:
            return here
        return self.address.get(host)

    def add(self, source, target, port, kind, count, evidence, comm_source=None, comm_target=None, protocol=None, short=None):
        first = self.components.get(source.get("comp") or "") or {}
        second = self.components.get(target.get("comp") or "") or {}
        if kind == "kv_data" and protocol is None:
            protocol, short = T("传输后端未知"), "KV"
        elif kind in ("request", "response", "events") and protocol is None:
            # The connection is a fact; what runs over it is what the Dynamo runtime was configured with.
            plane = "DYN_EVENT_PLANE" if kind == "events" else "DYN_REQUEST_PLANE"
            value = ((first.get("env") or {}).get(plane) or (second.get("env") or {}).get(plane) or "").lower()
            protocol = {"zmq": "ZeroMQ", "tcp": "TCP", "http": "HTTP", "nats": "NATS"}.get(value, "TCP")
        self.links.append({"src": source, "dst": target, "port": port, "kind": kind, "n": count, "evidence": evidence,
                           "comm_src": comm_source, "comm_dst": comm_target, "protocol": protocol, "short": short or protocol,
                           "service": first.get("service") or second.get("service")})

    # -- observed connections
    def observe(self):
        explained = set()
        for node_id, node in self.nodes.items():
            for root, comm, host, port, count in (node["sockets"] or {}).get("out") or []:
                source = self.components.get("%s:%s" % (node_id, root))
                if source is None:
                    continue
                there = self.node_of(host, node_id)
                if there is None:
                    self.add({"node": node_id, "comp": source["key"]}, {"node": None, "host": host}, port,
                             self.foreign_kind(source, host, port, comm, False) or "tcp", count, "observed", comm)
                    continue
                listener = self.nodes[there]["listeners"].get(port)
                target = self.components.get("%s:%s" % (there, listener[0])) if listener else None
                if listener is None:        # no socket table over there: discovery may still say whose port this is
                    for key, ports in self.instance_ports.items():
                        candidate = self.components[key]
                        if port in ports and candidate["node"] == there and candidate["kind"] in ("dynamo-worker", "sglang-server"):
                            target = candidate
                            break
                explained.add((there, port, node_id))
                self.add({"node": node_id, "comp": source["key"]}, {"node": there, "comp": target["key"] if target else None}, port,
                         classify(source, target, port, listener[1] if listener else None, self.instance_ports, host in LOOPBACK),
                         count, "observed", comm, listener[1] if listener else None)
        for node_id, node in self.nodes.items():
            for root, comm, port, host, count in (node["sockets"] or {}).get("in") or []:
                target = self.components.get("%s:%s" % (node_id, root))
                if target is None:
                    continue
                there = self.node_of(host, node_id)
                if there is not None and (node_id, port, there) in explained:
                    continue        # already counted from the connecting side, where the client process is known
                source = {"node": there, "comp": None} if there is not None else {"node": None, "host": host}
                kind = (self.foreign_kind(target, host, port, comm, True) if there is None else None) \
                    or classify(None, target, port, comm, self.instance_ports)
                self.add(source, {"node": node_id, "comp": target["key"]}, port, kind, count, "observed", None, comm)

    # -- what the configuration says, for components on nodes without a socket table
    def declare(self):
        for service in self.services.values():
            members = service["components"]
            for component in members:
                if self.nodes.get(component["node"], {}).get("sockets") is not None:
                    continue
                here = {"node": component["node"], "comp": component["key"]}
                if component["kind"] in ("dynamo-frontend", "dynamo-worker"):
                    for etcd in members:
                        if etcd["kind"] == "etcd":
                            self.add(here, {"node": etcd["node"], "comp": etcd["key"]}, etcd["ports"].get("client"), "etcd", 0, "declared")
                if component["kind"] == "dynamo-frontend":
                    for item in ((component.get("probe") or {}).get("health") or {}).get("instances") or []:
                        target = self.address.get(item.get("host"))
                        if target is None:
                            if item.get("host"):
                                self.add(here, {"node": None, "host": item["host"]}, item.get("port"), "request", 0, "declared")
                            continue
                        role = "prefill" if item.get("component") == "prefill" else None
                        for other in members:
                            if other["node"] == target and other["kind"] == "dynamo-worker" and (other["role"] == "prefill") == (role == "prefill"):
                                self.add(here, {"node": target, "comp": other["key"]}, item.get("port"), "request", 0, "declared")
                if component["kind"] == "sglang-router":
                    for other in members:
                        if other["kind"] == "sglang-server":
                            self.add(here, {"node": other["node"], "comp": other["key"]}, other["ports"].get("http"), "http", 0, "declared")

    # -- the KV transfer itself: RDMA does not go through kernel sockets
    def infer(self):
        for service in self.services.values():
            if service.get("mode") != "disagg":
                continue
            prefills = [component for component in service["components"] if component["role"] == "prefill"]
            decodes = [component for component in service["components"] if component["role"] == "decode"]
            for prefill in prefills:
                for decode in decodes:
                    pair = set([prefill["key"], decode["key"]])
                    seen = any(link["kind"] in ("bootstrap", "kv_ctrl") and set([link["src"].get("comp"), link["dst"].get("comp")]) == pair
                               for link in self.links)
                    blind = any(self.nodes.get(component["node"], {}).get("sockets") is None for component in (prefill, decode))
                    if not seen and not blind:
                        continue
                    name, short = transfer_protocol(prefill, decode)
                    self.add({"node": prefill["node"], "comp": prefill["key"]}, {"node": decode["node"], "comp": decode["key"]}, None,
                             "kv_data", 0, "inferred", protocol=name, short=short)

    # -- projections
    def role_of(self, endpoint, kind, side):
        component = self.components.get(endpoint.get("comp") or "")
        if component is not None:
            return component["role"]
        if endpoint.get("node") is None and endpoint.get("host") in self.unmonitored:
            return self.unmonitored[endpoint["host"]]["role"]
        if endpoint.get("node") is None and kind == "etcd" and side == "dst":
            return "etcd"
        if side == "src" and kind == "http":
            return "client"
        return "other"

    def channel(self, bucket, key, link, source, target):
        plane, name, protocol = KINDS[link["kind"]]
        channel = bucket.get(key)
        if channel is None:
            channel = bucket[key] = {"kind": link["kind"], "plane": plane, "name": name, "proto": link["protocol"] or protocol,
                                     "short": link["short"] or protocol,
                                     "from": source, "to": target, "ports": set(), "n": 0, "evidence": link["evidence"],
                                     "services": set(), "from_roles": set(), "to_roles": set(), "comms": set()}
        if link["port"]:
            channel["ports"].add(link["port"])
        channel["n"] += link["n"]
        if EVIDENCE_ORDER[link["evidence"]] < EVIDENCE_ORDER[channel["evidence"]]:
            channel["evidence"] = link["evidence"]
        if link["service"]:
            channel["services"].add(link["service"])
        channel["from_roles"].add(self.role_of(link["src"], link["kind"], "src"))
        channel["to_roles"].add(self.role_of(link["dst"], link["kind"], "dst"))
        if link["comm_src"] or link["comm_dst"]:
            channel["comms"].add((link["comm_src"] or "", link["comm_dst"] or ""))
        return channel

    @staticmethod
    def finish(channels):
        result = []
        for channel in channels:
            ports = sorted(channel["ports"])
            item = dict(channel, ports=ports[:MAX_PORTS], port_count=len(ports), services=sorted(channel["services"]),
                        from_roles=sorted(channel["from_roles"]), to_roles=sorted(channel["to_roles"]))
            comms = sorted(channel["comms"])
            item["comms"] = [list(pair) for pair in comms[:4]]
            result.append(item)
        result.sort(key=lambda item: (PLANE_ORDER.get(item["plane"], 9), item["kind"], str(item["from"]), str(item["to"])))
        return result

    @staticmethod
    def endpoint_id(endpoint):
        return endpoint["node"] if endpoint.get("node") is not None else "ext:%s" % endpoint.get("host")

    def cluster(self):
        """Servers and the channels between them."""
        edges = collections.OrderedDict()
        externals = collections.OrderedDict()
        active = set()
        inside = collections.Counter()
        for link in self.links:
            first, second = self.endpoint_id(link["src"]), self.endpoint_id(link["dst"])
            for endpoint, ref in ((link["src"], first), (link["dst"], second)):
                if endpoint.get("node") is None:
                    entry = externals.setdefault(ref, {"id": ref, "host": endpoint.get("host"), "roles": set(),
                                                       "unmonitored": endpoint.get("host") in self.unmonitored})
                    entry["roles"].add(self.role_of(endpoint, link["kind"], "src" if endpoint is link["src"] else "dst"))
            if first == second:
                inside[first] += 1
                active.add(first)
                continue
            active.update((first, second))
            key = tuple(sorted((first, second)))
            self.channel(edges.setdefault(key, {}), (link["kind"], first, second), link, first, second)
        nodes = []
        for node_id, node in self.nodes.items():
            roles = []
            for component in self.components.values():
                if component["node"] == node_id and component.get("service"):
                    entry = {"service": component["service"], "service_name": component["service_name"], "role": component["role"]}
                    if entry not in roles:
                        roles.append(entry)
            nodes.append({"id": node_id, "name": node["name"], "host": node["host"], "status": node["status"], "auth": node["auth"],
                          "ssh_port": node["port"], "gpu_model": node["gpu_model"], "gpu_count": node["gpu_count"], "roles": roles,
                          "active": node_id in active or bool(roles), "inside": inside.get(node_id, 0),
                          "observed": node["sockets"] is not None, "socket_error": node["socket_error"]})
        return {
            "nodes": nodes,
            "externals": [dict(entry, roles=sorted(entry["roles"])) for entry in externals.values()],
            "edges": [{"id": "%s|%s" % key, "a": key[0], "b": key[1], "channels": self.finish(channels.values())}
                      for key, channels in edges.items()],
        }

    def node(self, node_id):
        """Components of one server, the channels among them and to the outside, and each component's inner channels."""
        node = self.nodes[node_id]
        members = [component for component in self.components.values() if component["node"] == node_id]
        order = {"frontend": 0, "router": 0, "etcd": 1, "prefill": 2, "decode": 3, "worker": 3, "server": 3}
        members.sort(key=lambda component: (order.get(component["role"], 5), component.get("service_name") or "", component["pid"]))
        edges = collections.OrderedDict()
        inner = collections.defaultdict(dict)
        peers = collections.OrderedDict()

        def ref(endpoint, link, side):
            if endpoint.get("node") == node_id:
                return endpoint.get("comp") or "local"
            ident = self.endpoint_id(endpoint)
            peer = peers.get(ident)
            if peer is None:
                other = self.nodes.get(endpoint.get("node") or "")
                peer = peers[ident] = {"id": ident, "kind": "node" if other else "external",
                                       "unmonitored": other is None and endpoint.get("host") in self.unmonitored,
                                       "name": other["name"] if other else endpoint.get("host"),
                                       "host": other["host"] if other else endpoint.get("host"),
                                       "status": other["status"] if other else None, "roles": set(), "inbound": False, "outbound": False}
            peer["roles"].add(self.role_of(endpoint, link["kind"], side))
            peer["inbound" if side == "src" else "outbound"] = True
            return ident

        local = False
        for link in self.links:
            if link["src"].get("node") != node_id and link["dst"].get("node") != node_id:
                continue
            if link["src"].get("node") == node_id and link["dst"].get("node") == node_id \
                    and link["src"].get("comp") and link["src"].get("comp") == link["dst"].get("comp"):
                channel = self.channel(inner[link["src"]["comp"]], (link["kind"], link["comm_src"], link["comm_dst"]), link,
                                       link["comm_src"], link["comm_dst"])
                continue
            first, second = ref(link["src"], link, "src"), ref(link["dst"], link, "dst")
            local = local or "local" in (first, second)
            if first == second:
                continue
            key = tuple(sorted((first, second)))
            self.channel(edges.setdefault(key, {}), (link["kind"], first, second), link, first, second)
        components = []
        for component in members:
            settings = component.get("settings") or {}
            components.append({
                "key": component["key"], "service": component.get("service"), "service_name": component.get("service_name"),
                "kind": component["kind"], "role": component["role"], "pid": component["pid"], "container": component.get("container"),
                "ports": component.get("ports") or {}, "gpus": component.get("gpus") or [], "health": component.get("health"),
                "procs": component.get("procs") or [],
                "settings": dict((name, settings[name]) for name in ("tp", "dp", "mode", "transfer", "dp_attention") if name in settings),
                "inner": self.finish(inner.get(component["key"], {}).values()),
            })
        return {
            "node": {"id": node_id, "name": node["name"], "host": node["host"], "status": node["status"]},
            "observed": node["sockets"] is not None, "socket_error": node["socket_error"],
            "components": components, "local": local,
            "peers": [dict(peer, roles=sorted(peer["roles"])) for peer in peers.values()],
            "edges": [{"id": "%s|%s" % key, "a": key[0], "b": key[1], "channels": self.finish(channels.values())}
                      for key, channels in edges.items()],
        }
