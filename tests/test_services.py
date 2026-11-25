"""Grouping components into services, service status, and rates / quantiles from cumulative samples."""
import unittest

from farsight.events import EventLog
from farsight.services import Aggregator, Tracker, quantile, split_endpoints

ETCD = "http://10.0.0.24:2379"
NS = "demo_pd"


def worker(pid, role, cid, port, health="ready", metrics=None):
    probe = {"health": {"status": health}}
    if metrics is not None:
        probe["metrics"] = dict({"has_sglang": True, "serving": True}, **metrics)
    return {"pid": pid, "kind": "dynamo-worker", "backend": "sglang", "role": role, "cid": cid, "started": 1000.0,
            "env": {"DYN_NAMESPACE": NS, "ETCD_ENDPOINTS": ETCD, "DYN_SYSTEM_PORT": str(port)}, "ports": {"system": port},
            "discovery": "etcd", "probe": probe, "cmdline": "python3 -m dynamo.sglang", "model": None}


def frontend(pid, cid, instances, metrics=None, env=None):
    return {"pid": pid, "kind": "dynamo-frontend", "backend": None, "role": None, "cid": cid, "started": 1000.0,
            "env": {"DYN_NAMESPACE": NS, "ETCD_ENDPOINTS": ETCD} if env is None else env, "ports": {"http": 8000},
            "discovery": "etcd", "cmdline": "python3 -m dynamo.frontend",
            "probe": {"health": {"status": "healthy", "instances": instances}, "models": ["org/model-x"],
                      "metrics": metrics or {"inflight": 3, "queued": 1, "requests": 10, "errors": 0, "output_tokens": 100, "workers": {"prefill": 2, "decode": 1}}}}


def etcd(pid, cid, port=2379):
    return {"pid": pid, "kind": "etcd", "role": None, "cid": cid, "started": 1.0, "env": {}, "ports": {"client": port},
            "probe": {"health": {"status": "healthy"}}, "cmdline": "etcd"}


def node(node_id, host, hostname, components, gpu_procs=(), containers=None, status="online", slow_at=100.0, gpus=8):
    return {"config": {"id": node_id, "host": host, "port": 22, "user": "root"}, "status": status,
            "hello": {"hostname": hostname, "ips": [host], "gpus": [{"index": index} for index in range(gpus)]},
            "fast": {"gpus": [{"i": index, "util": 50.0 + index, "mem": 1000.0, "mem_total": 2000.0, "power": 100.0, "temp": 60} for index in range(gpus)]},
            "slow": {"components": list(components), "gpu_procs": list(gpu_procs), "containers": containers or {}}, "slow_at": slow_at}


def procs(root, indices, mem=80000.0, start=0):
    return [{"pid": root + 10 + index, "gpu": index, "mem": mem, "root": root, "cid": None, "name": "sglang::scheduler"} for index in indices]


INSTANCES = [{"namespace": NS, "component": "backend", "id": "1", "host": "10.0.0.24", "port": 1},
             {"namespace": NS, "component": "prefill", "id": "2", "host": "10.0.1.23", "port": 2},
             {"namespace": NS, "component": "prefill", "id": "3", "host": "10.0.1.99", "port": 3}]


def cluster(decode_health="ready"):
    decode_node = node("d", "10.0.0.24", "h20-24", [frontend(10, "c1", INSTANCES), worker(20, "decode", "c2", 18082, decode_health), etcd(5, "c3")],
                       procs(20, [0, 1, 2, 3]),
                       {"c1": {"name": "llm-prod-sglang-frontend"}, "c2": {"name": "llm-prod-sglang-decode"}, "c3": {"name": "llm-etcd"}})
    prefill_node = node("p", "10.0.1.23", "r6k-23", [worker(30, "prefill", "c4", 18081)], procs(30, [4, 5, 6, 7]),
                        {"c4": {"name": "llm-prod-sglang-prefill"}})
    return [decode_node, prefill_node]


class Grouping(unittest.TestCase):
    def test_components_on_two_nodes_form_one_service(self):
        services = Aggregator().build(cluster(), now=200.0)
        self.assertEqual(len(services), 1)
        service = services[0]
        self.assertEqual(service["kind"], "dynamo")
        self.assertEqual(service["name"], "llm-prod")                        # deployment prefix of the container names
        self.assertEqual(service["counts"], {"frontend": 1, "prefill": 1, "decode": 1, "etcd": 1})
        self.assertEqual(service["nodes"], ["d", "p"])
        self.assertEqual(service["mode"], "disagg")
        self.assertEqual(service["namespace"], NS)
        self.assertEqual(service["etcd"], ["10.0.0.24:2379"])
        self.assertEqual(service["model"], "org/model-x")
        self.assertEqual(service["endpoint"], "http://10.0.0.24:8000")
        self.assertEqual(service["status"], "running")
        self.assertEqual(service["gpu"]["count"], 8)
        self.assertEqual([component["role"] for component in service["components"]], ["frontend", "prefill", "decode", "etcd"])

    def test_host_named_by_the_frontend_but_not_monitored_is_reported(self):
        service = Aggregator().build(cluster(), now=200.0)[0]
        self.assertEqual(service["missing"], [{"host": "10.0.1.99", "role": "prefill", "reason": "unmonitored"}])

    def test_instance_on_an_offline_node_is_reported_as_offline(self):
        nodes = cluster()
        nodes.append(node("x", "10.0.1.99", "r6k-99", [], status="error"))
        service = Aggregator().build(nodes, now=200.0)[0]
        self.assertEqual(service["missing"], [{"host": "10.0.1.99", "role": "prefill", "reason": "offline", "node": "x"}])

    def test_loopback_etcd_endpoint_resolves_to_the_node_address(self):
        nodes = cluster()
        for component in nodes[0]["slow"]["components"]:
            if "ETCD_ENDPOINTS" in component["env"]:
                component["env"]["ETCD_ENDPOINTS"] = "http://127.0.0.1:2379"
        self.assertEqual(len(Aggregator().build(nodes, now=200.0)), 1)

    def test_different_namespaces_on_one_etcd_are_different_services(self):
        nodes = cluster()
        other = worker(40, "decode", "c9", 18090)
        other["env"]["DYN_NAMESPACE"] = "other_ns"
        nodes[1]["slow"]["components"].append(other)
        services = Aggregator().build(nodes, now=200.0)
        self.assertEqual(sorted(service["namespace"] for service in services), ["demo_pd", "other_ns"])
        self.assertTrue(all(service["counts"].get("etcd") == 1 for service in services))      # the shared etcd shows in both

    def test_helper_contexts_do_not_claim_extra_gpus(self):
        nodes = cluster()
        nodes[1]["slow"]["gpu_procs"] = procs(30, [4, 5, 6, 7]) + procs(30, [0, 1, 2, 3], mem=466.0)
        service = Aggregator().build(nodes, now=200.0)[0]
        prefill = [component for component in service["components"] if component["role"] == "prefill"][0]
        self.assertEqual(prefill["gpus"], [4, 5, 6, 7])
        self.assertEqual(service["gpu"]["count"], 8)

    def test_exiting_component_stays_in_its_service(self):
        aggregator = Aggregator(EventLog())
        aggregator.build(cluster(), now=0.0)
        nodes = cluster()
        dying = nodes[1]["slow"]["components"][0]
        dying["env"], dying["cid"] = {}, None                    # what a process looks like while it exits
        services = aggregator.build(nodes, now=10.0)
        self.assertEqual([(service["name"], service["counts"].get("prefill")) for service in services], [("llm-prod", 1)])
        self.assertEqual([event["title"] for event in aggregator.log.recent() if event["title"] == "发现服务"], ["发现服务"])

    def test_native_sglang_server_is_its_own_service(self):
        server = {"pid": 70, "kind": "sglang-server", "role": None, "cid": None, "started": 1.0, "env": {}, "ports": {"http": 30000},
                  "model": "Qwen/Qwen3-32B", "cmdline": "python -m sglang.launch_server",
                  "probe": {"health": {"status": "ready"}, "models": ["Qwen/Qwen3-32B"], "metrics": {"has_sglang": True, "running": 4, "queued": 0}}}
        services = Aggregator().build([node("n", "10.0.0.7", "h20-7", [server], procs(70, [0, 1]))], now=10.0)
        self.assertEqual((services[0]["kind"], services[0]["name"], services[0]["counts"], services[0]["status"]),
                         ("sglang", "Qwen3-32B", {"server": 1}, "running"))
        self.assertEqual(services[0]["metrics"]["source"], "worker")
        self.assertEqual(services[0]["metrics"]["inflight"], 4)


class Status(unittest.TestCase):
    def test_worker_that_never_was_ready_means_starting(self):
        service = Aggregator().build(cluster("notready"), now=200.0)[0]
        self.assertEqual(service["status"], "starting")

    def test_worker_that_was_ready_and_is_not_anymore_means_degraded(self):
        aggregator = Aggregator()
        aggregator.build(cluster("ready"), now=200.0)
        service = aggregator.build(cluster("notready"), now=210.0)[0]
        self.assertEqual(service["status"], "degraded")
        self.assertIn("decode", service["status_detail"])

    def test_split_service_is_not_running_until_both_sides_registered(self):
        aggregator = Aggregator()
        decode_only = cluster()[:1]
        decode_only[0]["slow"]["components"][0]["probe"]["health"]["instances"] = INSTANCES[:1]
        service = aggregator.build(decode_only, now=0.0)[0]
        self.assertEqual(service["status"], "starting")
        self.assertIn("Prefill", service["status_detail"])
        self.assertEqual(aggregator.build(cluster(), now=10.0)[0]["status"], "running")
        self.assertEqual(aggregator.build(decode_only, now=20.0)[0]["status"], "degraded")      # it had been serving

    def test_degradation_is_left_to_the_alert_log(self):
        log = EventLog()
        aggregator = Aggregator(log)
        aggregator.build(cluster("ready"), now=0.0)
        aggregator.build(cluster("notready"), now=10.0)
        aggregator.build(cluster("ready"), now=20.0)
        self.assertEqual([event["title"] for event in reversed(log.recent())], ["发现服务", "服务运行中"])

    def test_lifecycle_events_and_stopped_service_is_kept_then_dropped(self):
        log = EventLog()
        aggregator = Aggregator(log)
        aggregator.build(cluster("notready"), now=0.0)
        aggregator.build(cluster("ready"), now=10.0)
        empty = [node("d", "10.0.0.24", "h20-24", []), node("p", "10.0.1.23", "r6k-23", [])]
        gone = aggregator.build(empty, now=20.0)
        self.assertEqual([(service["status"], service.get("stale")) for service in gone], [("stopped", True)])
        self.assertEqual(aggregator.build(empty, now=20.0 + 901)[0:], [])
        titles = [event["title"] for event in reversed(log.recent())]
        self.assertEqual(titles, ["发现服务", "服务已就绪", "服务已消失"])

    def test_service_on_an_offline_node_is_unknown_not_stopped(self):
        log = EventLog()
        aggregator = Aggregator(log)
        aggregator.build(cluster(), now=0.0)
        nodes = cluster()
        for item in nodes:
            item["status"] = "error"
        ghost = aggregator.build(nodes, now=10.0)[0]
        self.assertEqual(ghost["status"], "unknown")
        self.assertNotIn("服务已消失", [event["title"] for event in log.recent()])
        self.assertEqual(aggregator.build(cluster(), now=20.0)[0]["status"], "running")


class Rates(unittest.TestCase):
    def test_rate_and_reset(self):
        tracker = Tracker()
        tracker.add("k", 0.0, {"requests": 100.0})
        self.assertIsNone(tracker.rate("k", "requests"))
        tracker.add("k", 10.0, {"requests": 150.0})
        tracker.add("k", 10.0, {"requests": 999.0})                           # same sample time: ignored
        self.assertAlmostEqual(tracker.rate("k", "requests"), 5.0)
        tracker.add("k", 20.0, {"requests": 3.0})                             # the process restarted
        self.assertIsNone(tracker.rate("k", "requests"))
        tracker.add("k", 30.0, {"requests": 23.0})
        self.assertAlmostEqual(tracker.rate("k", "requests"), 2.0)

    def test_gauges_do_not_look_like_counter_resets(self):
        aggregator = Aggregator()
        for step, (inflight, requests) in enumerate([(9, 100), (2, 160), (5, 220)]):
            nodes = cluster()
            nodes[0]["slow_at"] = 100.0 + 10 * step
            nodes[0]["slow"]["components"][0]["probe"]["metrics"].update(inflight=inflight, requests=requests)
            service = aggregator.build(nodes, now=100.0 + 10 * step)[0]
        self.assertAlmostEqual(service["metrics"]["req_rate"], 6.0)
        self.assertEqual(service["metrics"]["inflight"], 5)

    def test_same_slow_sample_is_counted_once_however_often_the_state_is_built(self):
        aggregator = Aggregator()
        for now in (100.0, 101.0, 102.0, 103.0):
            service = aggregator.build(cluster(), now=now)[0]
        self.assertIsNone(service["metrics"]["req_rate"])                    # one sample: no rate yet, and no zero rate

    def test_quantile_interpolates_inside_the_bucket(self):
        older = [[0.1, 0], [1, 0], ["+Inf", 0]]
        newer = [[0.1, 10], [1, 90], ["+Inf", 100]]
        self.assertAlmostEqual(quantile(0.5, older, newer), 0.1 + 0.9 * (50 - 10) / 80)
        self.assertAlmostEqual(quantile(0.95, older, newer), 1.0)           # falls in +Inf: report the last finite bound
        self.assertIsNone(quantile(0.5, newer, newer))
        self.assertAlmostEqual(quantile(0.5, None, [[1, 4], [2, 8], ["+Inf", 8]]), 1.0)

    def test_split_endpoints(self):
        self.assertEqual(split_endpoints("http://a:2379, https://b:2380/ c"), [("a", "2379"), ("b", "2380"), ("c", "2379")])
        self.assertEqual(split_endpoints(None), [])


class TokenRates(unittest.TestCase):
    """Input tokens computed / served from a cache and output tokens, the basis of the cost per million tokens."""

    def build(self, prefill_metrics, front_metrics, agg=False):
        aggregator = Aggregator()
        for step, stamp in enumerate((100.0, 160.0)):
            scaled = lambda metrics: dict((key, value * step if isinstance(value, (int, float)) else value) for key, value in metrics.items())
            front = frontend(10, "c1", INSTANCES, dict({"inflight": 0, "queued": 0, "workers": {}}, **scaled(front_metrics)))
            first = node("d", "10.0.0.24", "h20-24", [front, worker(20, None if agg else "decode", "c2", 18082, metrics=scaled({"generation_tokens": 6000.0})), etcd(5, "c3")],
                         procs(20, [0, 1, 2, 3]), slow_at=stamp)
            members = [first] if agg else [first, node("p", "10.0.1.23", "r6k-23", [worker(30, "prefill", "c4", 18081, metrics=scaled(prefill_metrics))],
                                                       procs(30, [4, 5, 6, 7]), slow_at=stamp)]
            services = aggregator.build(members, stamp)
        return services[0]["metrics"]

    def test_engine_split_is_preferred(self):
        metrics = self.build({"computed_tokens": 60000.0, "hit_tokens": 240000.0, "prompt_tokens": 300000.0},
                             {"output_tokens": 6000.0, "input_tokens": 1.0, "cached_tokens": 1.0})
        self.assertEqual((metrics["tok_computed"], metrics["tok_hit"], metrics["tok_out"], metrics["tok_source"]), (1000.0, 4000.0, 100.0, "engine"))

    def test_older_engines_tell_only_cached_from_uncached(self):
        metrics = self.build({"uncached_tokens": 60000.0, "prompt_tokens": 300000.0}, {"output_tokens": 6000.0})
        self.assertEqual((metrics["tok_computed"], metrics["tok_hit"], metrics["tok_source"]), (1000.0, 4000.0, "engine"))

    def test_frontend_counters_stand_in_when_no_worker_is_measured(self):
        metrics = self.build({}, {"output_tokens": 6000.0, "input_tokens": 300000.0, "cached_tokens": 240000.0})
        self.assertEqual((metrics["tok_computed"], metrics["tok_hit"], metrics["tok_source"]), (1000.0, 4000.0, "frontend"))

    def test_cache_ratio_never_exceeds_one(self):
        metrics = self.build({}, {"output_tokens": 6000.0, "input_tokens": 300000.0, "cached_tokens": 330000.0})
        self.assertEqual(metrics["cache_ratio"], 1.0)
        self.assertEqual((metrics["tok_computed"], metrics["tok_hit"]), (0.0, 5000.0))


if __name__ == "__main__":
    unittest.main()
