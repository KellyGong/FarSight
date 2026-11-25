"""Communication topology: socket rows of the nodes joined into links, naming, and the two projections."""
import unittest

from farsight import collector
from farsight.services import Aggregator
from farsight.topology import Topology, classify, transfer_protocol
from tests.test_services import ETCD, NS, etcd, frontend, node, procs, worker

TRANSFER = {"SGLANG_DISAGGREGATION_NIXL_BACKEND": "UCX", "UCX_TLS": "rc_verbs,tcp:aux,cuda_copy"}
INSTANCES = [{"namespace": NS, "component": "backend", "id": "1", "host": "10.0.0.24", "port": 37777},
             {"namespace": NS, "component": "prefill", "id": "2", "host": "10.0.1.23", "port": 41001},
             {"namespace": NS, "component": "prefill", "id": "3", "host": "10.0.1.99", "port": 41003}]


def split_cluster(prefill_sockets=True):
    """Frontend + Decode + etcd on one node, Prefill on another, one more Prefill on a host that is not monitored."""
    decode = worker(20, "decode", "c2", 18082)
    decode["settings"] = {"port": 30000, "nccl_port": 30050, "mode": "decode", "transfer": "nixl", "dp": 4}
    decode["env"].update(TRANSFER, DYN_REQUEST_PLANE="tcp", DYN_EVENT_PLANE="zmq")
    prefill = worker(30, "prefill", "c4", 18081)
    prefill["settings"] = {"port": 30000, "nccl_port": 30050, "bootstrap_port": 30001, "kv_events_port": 5557, "mode": "prefill",
                           "transfer": "nixl", "dp": 4}
    prefill["env"].update(TRANSFER, DYN_REQUEST_PLANE="tcp", DYN_EVENT_PLANE="zmq")
    front = frontend(10, "c1", INSTANCES)
    front["env"].update(DYN_REQUEST_PLANE="tcp", DYN_EVENT_PLANE="zmq")
    first = node("d", "10.0.0.24", "gpu-a-24", [front, decode, etcd(5, "c3")], procs(20, [0, 1, 2, 3]))
    first["hello"]["ips"] = ["10.0.0.24", "172.17.0.1"]
    first["slow"]["sockets"] = {
        "listen": [[5, "etcd", "0.0.0.0", 2379], [10, "python3", "0.0.0.0", 8000], [10, "python3", "10.0.0.24", 39001],
                   [20, "python3", "0.0.0.0", 18082], [20, "python3", "10.0.0.24", 37777], [20, "sglang::schedul", "10.0.0.24", 40101],
                   [20, "python3", "127.0.0.1", 30234]],
        "out": [[10, "python3", "10.0.0.24", 2379, 1], [10, "python3", "10.0.0.24", 37777, 1], [10, "python3", "10.0.1.23", 41001, 1],
                [10, "python3", "10.0.1.23", 41010, 1], [10, "python3", "10.0.1.99", 41003, 1], [20, "python3", "10.0.0.24", 2379, 1],
                [20, "python3", "10.0.0.24", 39001, 5], [20, "sglang::schedul", "10.0.1.23", 30001, 8],
                [20, "sglang::schedul", "10.0.1.23", 42001, 4], [20, "sglang::schedul", "127.0.0.1", 30234, 4],
                [20, "sglang::schedul", "10.0.0.24", 40101, 3]],
        "in": [[10, "python3", 8000, "10.0.0.201", 7], [10, "python3", 39001, "10.0.1.23", 3], [10, "python3", 39001, "10.0.1.99", 2],
               [20, "python3", 18082, "127.0.0.1", 1], [20, "python3", 37777, "10.0.0.24", 1]],
    }
    second = node("p", "10.0.1.23", "gpu-b-07", [prefill], procs(30, [0, 1, 2, 3]))
    second["hello"]["ips"] = ["10.0.1.23", "172.17.0.1"]
    second["slow"]["sockets"] = {
        "listen": [[30, "python3", "0.0.0.0", 18081], [30, "python3", "10.0.1.23", 41001], [30, "python3", "0.0.0.0", 41010],
                   [30, "python3", "0.0.0.0", 30001], [30, "sglang::schedul", "10.0.1.23", 42001], [30, "sglang::schedul", "0.0.0.0", 5557],
                   [30, "sglang::schedul", "0.0.0.0", 30050], [30, "radix-server", "0.0.0.0", 17000]],
        "out": [[30, "python3", "10.0.0.24", 2379, 1], [30, "python3", "10.0.0.24", 39001, 3], [30, "sglang::schedul", "10.0.0.24", 40101, 9],
                [30, "python3", "10.0.1.23", 5557, 1], [30, "sglang::schedul", "127.0.0.1", 30050, 4],
                [30, "sglang::schedul", "10.0.1.23", 30001, 1]],
        "in": [[30, "python3", 41001, "10.0.0.24", 1], [30, "radix-server", 17000, "10.0.9.9", 1]],
    } if prefill_sockets else {"error": "ss not found"}
    snapshots = [first, second]
    return Topology(snapshots, Aggregator().build(snapshots, 200.0))


def channels(view, first, second):
    """{(kind, from, to): channel} of the edge between two ends of a projection."""
    key = "%s|%s" % tuple(sorted((first, second)))
    edge = next(edge for edge in view["edges"] if edge["id"] == key)
    return dict(((channel["kind"], channel["from"], channel["to"]), channel) for channel in edge["channels"])


class SocketRows(unittest.TestCase):
    LISTEN = "\n".join([
        'LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:* users:(("python3",pid=100,fd=7))',
        'LISTEN 0 4096 *:2379 *:* users:(("etcd",pid=50,fd=5))',
        'LISTEN 0 128 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=9,fd=3))',
        'LISTEN 0 100 127.0.0.1:30234 0.0.0.0:* users:(("python3",pid=200,fd=9),("sglang::schedul",pid=210,fd=9))',
    ])
    ESTABLISHED = "\n".join([
        'ESTAB 0 0 10.0.0.24:8000 10.0.0.201:51000 users:(("python3",pid=100,fd=20))',
        'ESTAB 0 0 10.0.0.24:8000 10.0.0.201:51001 users:(("python3",pid=100,fd=21))',
        'ESTAB 0 0 [::ffff:10.0.0.24]:2379 [::ffff:10.0.0.24]:44000 users:(("etcd",pid=50,fd=8))',
        'ESTAB 0 0 10.0.0.24:44000 10.0.0.24:2379 users:(("python3",pid=100,fd=30))',
        'ESTAB 0 0 127.0.0.1:55000 127.0.0.1:30234 users:(("sglang::schedul",pid=210,fd=14))',
        'ESTAB 0 0 127.0.0.1:30234 127.0.0.1:55000 users:(("python3",pid=200,fd=15))',
        'SYN-SENT 0 1 10.0.0.24:44010 10.0.9.9:80 users:(("python3",pid=100,fd=31))',
        'ESTAB 0 0 10.0.0.24:22 10.0.0.6:40000 users:(("sshd",pid=7,fd=4))',
    ])

    def test_addresses(self):
        self.assertEqual(collector.parse_socket_address("10.0.0.1:80"), ("10.0.0.1", 80))
        self.assertEqual(collector.parse_socket_address("[::ffff:10.0.0.1]:2379"), ("10.0.0.1", 2379))
        self.assertEqual(collector.parse_socket_address("*:2379"), ("0.0.0.0", 2379))
        self.assertEqual(collector.parse_socket_address("127.0.0.53%lo:53"), ("127.0.0.53", 53))
        self.assertEqual(collector.parse_socket_address("0.0.0.0:*"), (None, None))

    def test_process_trees(self):
        table = {1: ("systemd", "S", 0, 0), 100: ("python3", "S", 1, 0), 200: ("python3", "S", 1, 0), 210: ("sglang::schedul", "S", 200, 0),
                 211: ("sglang::schedul", "Z", 200, 0), 50: ("etcd", "S", 1, 0), 7: ("sshd", "S", 1, 0)}
        owner = collector.owners_of(table, {100: {}, 200: {}, 50: {}})
        self.assertEqual(owner, {100: 100, 200: 200, 210: 200, 211: 200, 50: 50})
        groups = collector.process_groups(table, owner, {210: {0, 1}})
        self.assertEqual(groups[200], [{"comm": "python3", "n": 1, "gpus": []}, {"comm": "sglang::schedul", "n": 1, "gpus": [0, 1]}])

    def test_only_service_sockets_and_direction_from_the_listening_port(self):
        owner = {100: 100, 200: 200, 210: 200, 50: 50}
        rows = collector.summarize_sockets(self.LISTEN, self.ESTABLISHED, owner)
        self.assertEqual(rows["listen"], [[50, "etcd", "0.0.0.0", 2379], [100, "python3", "0.0.0.0", 8000], [200, "python3", "127.0.0.1", 30234]])
        self.assertEqual(sorted(rows["in"]), [[50, "etcd", 2379, "10.0.0.24", 1], [100, "python3", 8000, "10.0.0.201", 2],
                                               [200, "python3", 30234, "127.0.0.1", 1]])
        # the half-open connection and sshd (not a service process) are left out
        self.assertEqual(sorted(rows["out"]), [[100, "python3", "10.0.0.24", 2379, 1], [200, "sglang::schedul", "127.0.0.1", 30234, 1]])

    def test_row_limit_keeps_the_busiest(self):
        lines = ['ESTAB 0 0 10.0.0.24:%d 10.0.0.%d:9000 users:(("python3",pid=100,fd=3))' % (40000 + index, index % 3) for index in range(9)]
        rows = collector.summarize_sockets("", "\n".join(lines), {100: 100}, limit=2)
        self.assertEqual(len(rows["out"]), 2)
        self.assertEqual([row[-1] for row in rows["out"]], [3, 3])

    def test_hidden_owners_are_reported_instead_of_an_empty_table(self):
        # Without root, `ss -p` lists the sockets of other users' processes without their owners.
        probe = collector.Collector(out=None)
        probe.has_ss = True
        outputs = {"-Htlnp": 'LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*', "-Htnp": 'ESTAB 0 0 10.0.0.24:8000 10.0.0.201:51000'}
        original = collector.run
        collector.run = lambda command, timeout=10: (0, outputs[command[1]], "")
        try:
            self.assertIn("not visible", probe.socket_table({100: 100})["error"])
            outputs["-Htlnp"] += ' users:(("python3",pid=100,fd=7))'
            self.assertEqual(probe.socket_table({100: 100})["listen"], [[100, "python3", "0.0.0.0", 8000]])
        finally:
            collector.run = original


class EngineSettings(unittest.TestCase):
    YAML = "\n".join(["# engine", "model-path: /models/secret-model", "port: 31600", "nccl_port: 31650", "disaggregation-mode: decode",
                      "disaggregation-transfer-backend: nixl  # RDMA", "enable-dp-attention: true", "tp-size: 1", "dp-size: 4",
                      "kv-events-config: '{\"publisher\":\"zmq\",\"endpoint\":\"tcp://*:7557\"}'", "  nested: ignored", "api-key: hunter2"])

    def test_only_whitelisted_keys_are_read_from_the_file(self):
        values = collector.parse_flat_yaml(self.YAML)
        self.assertNotIn("api-key", values)
        self.assertNotIn("model-path", values)
        self.assertEqual(values["disaggregation-transfer-backend"], "nixl")
        self.assertEqual(values["nccl-port"], "31650")

    def test_command_line_overrides_the_file(self):
        settings = collector.engine_settings(["--config", "x.yaml", "--port", "32000", "--disaggregation-bootstrap-port=32001"],
                                             collector.parse_flat_yaml(self.YAML))
        self.assertEqual(settings, {"port": 32000, "nccl_port": 31650, "bootstrap_port": 32001, "tp": 1, "dp": 4, "mode": "decode",
                                    "transfer": "nixl", "dp_attention": True, "kv_events_port": 7557})

    def test_component_takes_its_role_from_the_file(self):
        found = {"kind": "dynamo-worker", "backend": "sglang", "args": ["--config", "/opt/x.yaml"], "argv": ["python3"]}
        component = collector.describe_component(7, found, {"DYN_SYSTEM_PORT": "18081"}, file_values={"disaggregation-mode": "prefill"})
        self.assertEqual(component["role"], "prefill")
        self.assertEqual(component["settings"], {"mode": "prefill"})
        self.assertEqual(component["ports"], {"system": 18081})     # still the only port the collector may send HTTP to


class Naming(unittest.TestCase):
    def test_port_roles(self):
        front = {"key": "d:10", "kind": "dynamo-frontend", "role": "frontend", "ports": {"http": 8000}}
        work = {"key": "p:30", "kind": "dynamo-worker", "role": "prefill", "ports": {"system": 18081},
                "settings": {"bootstrap_port": 30001, "nccl_port": 30050, "kv_events_port": 5557, "dp": 4}}
        other = {"key": "d:20", "kind": "dynamo-worker", "role": "decode", "ports": {"system": 18082}, "settings": {"bootstrap_port": 30001}}
        store = {"key": "d:5", "kind": "etcd", "role": "etcd", "ports": {"client": 2379}}
        known = {"p:30": {41001}}
        self.assertEqual(classify(None, front, 8000, "python3", known), "http")
        self.assertEqual(classify(work, front, 39001, "python3", known), "response")
        self.assertEqual(classify(front, front, 44000, "python3", known), "events")
        self.assertEqual(classify(front, work, 41001, "python3", known), "request")
        self.assertEqual(classify(front, work, 41010, "python3", known), "events")
        self.assertEqual(classify(front, store, 2379, "etcd", known), "etcd")
        self.assertEqual(classify(store, store, 2380, "etcd", known), "etcd_peer")
        self.assertEqual(classify(other, work, 30001, "python3", known), "bootstrap")
        self.assertEqual(classify(work, other, 30001, "python3", known), "tcp")           # decode has no bootstrap server
        self.assertEqual(classify(other, work, 42001, "sglang::schedul", known), "kv_ctrl")
        self.assertEqual(classify(work, work, 30050, "sglang::schedul", known), "dist")
        self.assertEqual(classify(work, work, 5559, "sglang::schedul", known), "kv_events")
        self.assertEqual(classify(work, work, 30234, "python3", known, loopback=True), "ipc")
        self.assertEqual(classify(work, work, 36000, "sglang::schedul", known, loopback=False), "ranks")
        self.assertEqual(classify(None, work, 17000, "radix-server", known), "flexkv")
        self.assertEqual(classify(None, work, 18081, "python3", known), "metrics")
        self.assertEqual(classify(front, None, 9999, None, known), "tcp")

    def test_transfer_path_comes_from_settings_and_environment(self):
        self.assertEqual(transfer_protocol({"settings": {"transfer": "nixl"}, "env": TRANSFER}), ("NIXL · UCX · RDMA", "NIXL/RDMA"))
        self.assertEqual(transfer_protocol({"settings": {"transfer": "mooncake"}, "env": {}}), ("Mooncake", "Mooncake"))
        self.assertEqual(transfer_protocol({"settings": {}, "env": {"UCX_TLS": "tcp"}}), (None, None))


class Cluster(unittest.TestCase):
    def setUp(self):
        self.view = split_cluster().cluster()

    def test_channels_between_the_two_servers(self):
        found = channels(self.view, "d", "p")
        self.assertEqual(sorted((kind, source) for kind, source, _ in found),
                         [("bootstrap", "d"), ("etcd", "p"), ("events", "d"), ("kv_ctrl", "d"), ("kv_ctrl", "p"), ("kv_data", "p"),
                          ("request", "d"), ("response", "p")])
        self.assertEqual(found[("request", "d", "p")]["ports"], [41001])
        self.assertEqual((found[("request", "d", "p")]["from_roles"], found[("request", "d", "p")]["to_roles"]), (["frontend"], ["prefill"]))
        self.assertEqual(found[("events", "d", "p")]["proto"], "ZeroMQ")
        self.assertEqual(found[("etcd", "p", "d")]["to_roles"], ["etcd"])

    def test_a_connection_seen_from_both_ends_is_counted_once(self):
        response = channels(self.view, "d", "p")[("response", "p", "d")]
        self.assertEqual((response["n"], response["evidence"]), (3, "observed"))

    def test_kv_transfer_is_inferred_because_rdma_bypasses_sockets(self):
        transfer = channels(self.view, "d", "p")[("kv_data", "p", "d")]
        self.assertEqual((transfer["evidence"], transfer["proto"], transfer["short"], transfer["n"]), ("inferred", "NIXL · UCX · RDMA", "NIXL/RDMA", 0))

    def test_hosts_that_are_not_monitored(self):
        hosts = dict((item["host"], item) for item in self.view["externals"])
        self.assertEqual((hosts["10.0.1.99"]["roles"], hosts["10.0.1.99"]["unmonitored"]), (["prefill"], True))
        self.assertEqual((hosts["10.0.0.201"]["roles"], hosts["10.0.0.201"]["unmonitored"]), (["client"], False))
        worker_host = channels(self.view, "d", "ext:10.0.1.99")
        self.assertEqual(sorted(kind for kind, _, _ in worker_host), ["request", "response"])
        self.assertEqual(channels(self.view, "d", "ext:10.0.0.201")[("http", "ext:10.0.0.201", "d")]["n"], 7)
        self.assertIn(("flexkv", "ext:10.0.9.9", "p"), channels(self.view, "p", "ext:10.0.9.9"))

    def test_an_address_every_node_has_identifies_none(self):
        self.assertNotIn("172.17.0.1", split_cluster().address)

    def test_node_summaries(self):
        nodes = dict((item["id"], item) for item in self.view["nodes"])
        self.assertEqual(sorted(entry["role"] for entry in nodes["d"]["roles"]), ["decode", "etcd", "frontend"])
        self.assertTrue(nodes["d"]["observed"] and nodes["d"]["active"])
        self.assertGreater(nodes["d"]["inside"], 0)


class WithoutSocketTable(unittest.TestCase):
    def test_declared_links_stand_in_and_are_marked(self):
        view = split_cluster(prefill_sockets=False).cluster()
        nodes = dict((item["id"], item) for item in view["nodes"])
        self.assertFalse(nodes["p"]["observed"])
        self.assertEqual(nodes["p"]["socket_error"], "ss not found")
        found = channels(view, "d", "p")
        self.assertEqual(found[("etcd", "p", "d")]["evidence"], "declared")
        self.assertEqual(found[("request", "d", "p")]["evidence"], "observed")      # the frontend's side still shows it
        self.assertEqual(found[("kv_data", "p", "d")]["evidence"], "inferred")


class InsideOneServer(unittest.TestCase):
    def test_components_their_channels_and_peers(self):
        view = split_cluster().node("d")
        roles = [component["role"] for component in view["components"]]
        self.assertEqual(roles, ["frontend", "etcd", "decode"])
        decode = view["components"][2]
        self.assertEqual(sorted((channel["kind"], channel["from"], channel["to"]) for channel in decode["inner"]),
                         [("ipc", "sglang::schedul", "python3"), ("ranks", "sglang::schedul", "sglang::schedul")])
        self.assertEqual(sorted(kind for kind, _, _ in channels(view, "d:10", "d:20")), ["request", "response"])
        self.assertIn(("etcd", "d:10", "d:5"), channels(view, "d:10", "d:5"))
        self.assertTrue(view["local"])                                   # something local reads the worker's metrics page
        self.assertIn(("metrics", "local", "d:20"), channels(view, "d:20", "local"))
        peers = dict((peer["id"], peer) for peer in view["peers"])
        self.assertEqual((peers["p"]["kind"], peers["p"]["roles"]), ("node", ["prefill"]))
        self.assertTrue(peers["ext:10.0.1.99"]["unmonitored"])
        self.assertEqual(peers["ext:10.0.0.201"]["roles"], ["client"])

    def test_prefill_side(self):
        view = split_cluster().node("p")
        inner = sorted(channel["kind"] for channel in view["components"][0]["inner"])
        self.assertEqual(inner, ["bootstrap", "dist", "kv_events"])
        self.assertEqual(view["components"][0]["settings"]["transfer"], "nixl")


if __name__ == "__main__":
    unittest.main()
