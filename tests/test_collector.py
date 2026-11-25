"""Collector parsers and the endpoint rule. Fixtures are sanitised samples taken from real nodes."""
import json
import os
import socket
import threading
import unittest

from farsight import collector as c

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


class HostParsers(unittest.TestCase):
    def test_cpu_busy_share_between_two_samples(self):
        before = c.parse_proc_stat("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0\nbtime 1700000000\nprocs_running 3\n")
        after = c.parse_proc_stat("cpu  200 0 200 1600 0 0 0 0 0 0\ncpu0 150 0 50 800 0 0 0 0\ncpu1 50 0 150 800 0 0 0 0\n")
        self.assertEqual(before["btime"], 1700000000)
        self.assertEqual(before["running"], 3)
        self.assertAlmostEqual(c.busy_percent(before["total"], after["total"]), 20.0)
        self.assertEqual(len(after["cores"]), 2)
        self.assertIsNone(c.busy_percent(after["total"], after["total"]))

    def test_iowait_counts_as_idle(self):
        before = c.parse_proc_stat("cpu  0 0 0 0 0 0 0 0\n")
        after = c.parse_proc_stat("cpu  10 0 0 50 40 0 0 0\n")
        self.assertAlmostEqual(c.busy_percent(before["total"], after["total"]), 10.0)

    def test_real_proc_stat_head(self):
        stat = c.parse_proc_stat(fixture("proc_stat_head.txt"))
        self.assertIsNotNone(stat["total"])
        self.assertGreater(stat["total"][1], stat["total"][0])

    def test_meminfo_counts_shared_memory_as_used(self):
        memory = c.parse_meminfo(fixture("proc_meminfo.txt"))
        self.assertGreater(memory["total"], 1000)
        self.assertAlmostEqual(memory["used"], memory["total"] - memory["available"], delta=1)     # MiB rounding
        self.assertGreater(memory["shmem"], 0)

    def test_network_ignores_virtual_interfaces(self):
        text = "Inter-|   Receive\n face |bytes\n    lo: 500 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n  eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n" \
               "docker0: 9 0 0 0 0 0 0 0 9 0 0 0 0 0 0 0\nveth1a2b: 9 0 0 0 0 0 0 0 9 0 0 0 0 0 0 0\n bond0: 10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0\n"
        self.assertEqual(c.parse_net_dev(text), (1010, 2020))

    def test_stat_line_with_spaces_and_parentheses_in_comm(self):
        fields = " ".join(["S", "4242"] + ["0"] * 17 + ["987654"] + ["0"] * 10)
        self.assertEqual(c.parse_stat_line("77 (tmux: server (1)) " + fields), ("tmux: server (1)", "S", 4242, 987654))
        self.assertIsNone(c.parse_stat_line("garbage"))

    def test_container_id_from_cgroup(self):
        cid = "ab" * 32
        self.assertEqual(c.container_id("0::/system.slice/docker-%s.scope\n" % cid), cid)
        self.assertEqual(c.container_id("12:devices:/docker/%s\n" % cid), cid)
        self.assertIsNone(c.container_id("0::/user.slice/user-1000.slice\n"))


class GpuParsers(unittest.TestCase):
    def test_generic_device_name_is_resolved_from_pci_id(self):
        inventory = c.gpu_inventory(c.parse_gpu_csv(fixture("gpu_static_rtxpro5000.csv"), c.GPU_STATIC_FIELDS))
        self.assertEqual(len(inventory), 8)
        self.assertEqual(inventory[0]["name"], "RTX PRO 5000")
        self.assertEqual(inventory[0]["mem_total"], 73415.0)
        self.assertTrue(inventory[0]["uuid"].startswith("GPU-"))

    def test_live_sample(self):
        sample = c.gpu_sample(c.parse_gpu_csv(fixture("gpu_live_rtx6000d.csv"), c.GPU_LIVE_FIELDS))
        self.assertEqual([gpu["i"] for gpu in sample], list(range(8)))
        self.assertTrue(all(0 <= gpu["util"] <= 100 for gpu in sample))
        self.assertTrue(all(gpu["mem"] <= gpu["mem_total"] for gpu in sample))

    def test_unsupported_fields_become_none(self):
        sample = c.gpu_sample(c.parse_gpu_csv("0, [N/A], 12, 24564, 41, [Not Supported], [N/A]\nnot a gpu line\n", c.GPU_LIVE_FIELDS))
        self.assertEqual(len(sample), 1)
        self.assertIsNone(sample[0]["util"])
        self.assertIsNone(sample[0]["power"])
        self.assertEqual(sample[0]["mem"], 12.0)

    def test_compute_apps_under_mps(self):
        apps = c.parse_compute_apps(fixture("compute_apps_mps.csv"))
        self.assertGreater(len(apps), 8)
        names = set(name for _, _, _, name in apps)
        self.assertIn("nvidia-cuda-mps-server", names)
        self.assertTrue(any(name.startswith("sglang::scheduler") for name in names))
        self.assertTrue(all(isinstance(pid, int) and uuid.startswith("GPU-") for pid, uuid, _, _ in apps))

    def test_process_name_with_commas(self):
        self.assertEqual(c.parse_compute_apps("9, GPU-x, 100, python a.py --x 1,2,3\n"), [(9, "GPU-x", 100.0, "python a.py --x 1,2,3")])


class ProcessClassification(unittest.TestCase):
    def classify(self, line):
        return c.classify_process(line.split())

    def test_dynamo_frontend(self):
        found = self.classify("python3 -m dynamo.frontend --http-host 0.0.0.0 --http-port 8300 --router-mode kv --router-kv-events")
        self.assertEqual(found["kind"], "dynamo-frontend")
        self.assertEqual(c.option(found["args"], "--http-port"), "8300")

    def test_dynamo_worker_backends(self):
        self.assertEqual(self.classify("python3 -m dynamo.sglang --config /opt/recipe/configs/sglang-prefill.yaml --port 32100")["backend"], "sglang")
        self.assertEqual(self.classify("/usr/bin/python3.12 -u -m dynamo.vllm --model x --disaggregation-mode decode")["backend"], "vllm")
        self.assertIsNone(self.classify("python3 -m dynamo.planner"))

    def test_native_sglang(self):
        self.assertEqual(self.classify("python -m sglang.launch_server --model-path /m --port 30000")["kind"], "sglang-server")
        found = self.classify("/usr/bin/python3 /usr/local/bin/sglang serve --model-path /m --port 32124")
        self.assertEqual(found["kind"], "sglang-server")
        self.assertEqual(c.option(found["args"], "--port"), "32124")
        self.assertEqual(self.classify("python -m sglang_router.launch_router --pd-disaggregation --prefill http://a:1 --decode http://b:2")["kind"], "sglang-router")

    def test_infrastructure(self):
        self.assertEqual(self.classify("/opt/bitnami/etcd/bin/etcd")["kind"], "etcd")
        self.assertIsNone(self.classify("/usr/bin/python3 /usr/local/bin/some-helper --name /x"))

    def test_wrappers_and_unrelated_processes_are_not_services(self):
        for line in ["bash -c python3 -m dynamo.frontend --http-port 8000",
                     "timeout 480s python3 -m sglang.launch_server --port 1",
                     "docker exec -e X=1 box python3 -m dynamo.sglang",
                     "python3 -c import dynamo.frontend",
                     "python3 train.py --note dynamo.frontend",
                     "sglang::scheduler_DP0_TP0",
                     "vim dynamo.frontend.py"]:
            self.assertIsNone(self.classify(line), line)
        self.assertIsNone(c.classify_process([]))

    def test_option_forms(self):
        argv = ["--http-port=8000", "--model_path", "/m", "--flag"]
        self.assertEqual(c.option(argv, "--http-port"), "8000")
        self.assertEqual(c.option(argv, "--model-path"), "/m")
        self.assertIsNone(c.option(argv, "--flag"))
        self.assertEqual(c.options(["--prefill", "a", "b", "--decode", "c"], "--prefill"), ["a", "b"])

    def test_role_evidence_order(self):
        self.assertEqual(c.role_of("dynamo-worker", ["--disaggregation-mode", "decode", "--config", "/x/sglang-prefill.yaml"]), "decode")
        self.assertEqual(c.role_of("dynamo-worker", ["--config", "/opt/configs/sglang-decode-tp4.yaml"]), "decode")
        self.assertEqual(c.role_of("dynamo-worker", [], ["/entry.sh", "prefill"]), "prefill")
        self.assertEqual(c.role_of("dynamo-worker", [], None, "chat-prod-prefill-r3"), "prefill")
        self.assertIsNone(c.role_of("dynamo-worker", ["--config", "/opt/agg.yaml"]))
        self.assertIsNone(c.role_of("dynamo-frontend", ["--config", "prefill"]))


class EndpointRule(unittest.TestCase):
    """HTTP goes only to ports a service declares as HTTP; a Dynamo worker's own --port is never one of them."""

    def describe(self, line, env=None):
        argv = line.split()
        found = c.classify_process(argv)
        found["argv"] = argv
        return c.describe_component(1, found, env or {})

    def test_dynamo_worker_exposes_only_its_system_port(self):
        component = self.describe("python3 -m dynamo.sglang --config /c/sglang-prefill.yaml --port 32100 --nccl-port 32150",
                                  {"DYN_SYSTEM_PORT": "20081"})
        self.assertEqual(component["ports"], {"system": 20081})
        self.assertEqual(component["role"], "prefill")

    def test_dynamo_worker_without_system_port_has_nothing_to_probe(self):
        component = self.describe("python3 -m dynamo.sglang --config /c/sglang-decode.yaml --port 30000")
        self.assertEqual(component["ports"], {})
        self.assertEqual(c.Collector(probe=True).probe_component(component), {"skipped": True})
        self.assertEqual(self.describe("python3 -m dynamo.sglang --port 1", {"DYN_SYSTEM_PORT": "-1"})["ports"], {})

    def test_frontend_and_native_server_ports(self):
        self.assertEqual(self.describe("python3 -m dynamo.frontend --http-port 8300")["ports"], {"http": 8300})
        self.assertEqual(self.describe("python3 -m dynamo.frontend")["ports"], {"http": 8000})
        self.assertEqual(self.describe("python -m sglang.launch_server --model-path /m")["ports"], {"http": 30000})
        self.assertEqual(self.describe("etcd --listen-client-urls http://0.0.0.0:2579")["ports"], {"client": 2579})
        self.assertEqual(self.describe("etcd", {"ETCD_LISTEN_CLIENT_URLS": "http://0.0.0.0:2879"})["ports"], {"client": 2879})

    def test_skip_list_and_disabled_probing(self):
        collector = c.Collector(probe=True)
        for port in c.DEFAULT_SKIP_PORTS:
            self.assertFalse(collector.allowed(1, "127.0.0.1", port))
        self.assertFalse(collector.allowed(1, "127.0.0.1", None))
        self.assertFalse(c.Collector(probe=False).allowed(1, "127.0.0.1", 8000))

    def test_listener_that_speaks_first_is_never_sent_http(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        received = []

        def serve():
            for _ in range(2):
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                connection.sendall(b"\xff\x00\x00\x00\x00\x00\x00\x00\x01\x7f")     # a ZeroMQ-style greeting
                connection.settimeout(0.6)
                try:
                    received.append(connection.recv(64))
                except OSError:
                    received.append(b"")
                connection.close()

        thread = threading.Thread(target=serve)
        thread.daemon = True
        thread.start()
        collector = c.Collector(probe=True)
        self.assertFalse(collector.allowed(4242, "127.0.0.1", port))
        self.assertFalse(collector.allowed(4242, "127.0.0.1", port))        # remembered, no second connection
        self.assertIn((4242, port), collector.talkers)
        thread.join(timeout=2)
        server.close()
        self.assertEqual(received[:1], [b""])                                 # the check itself sent nothing

    def test_silent_listener_passes_the_passive_check(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        collector = c.Collector(probe=True)
        self.assertTrue(collector.allowed(7, "127.0.0.1", server.getsockname()[1]))
        server.close()


class MetricSummaries(unittest.TestCase):
    def test_frontend_metrics(self):
        summary = c.summarize_frontend_metrics(fixture("frontend_metrics.txt"))
        self.assertGreater(summary["requests"], 0)
        self.assertGreaterEqual(summary["requests"], summary["errors"])
        self.assertGreater(summary["output_tokens"], 0)
        self.assertGreaterEqual(summary["inflight"], 0)
        self.assertEqual(summary["workers"], {"decode": 1, "prefill": 2})
        self.assertEqual(summary["models"], ["example/chat-large"])
        buckets = summary["ttft"]["buckets"]
        self.assertEqual(buckets[-1][0], "+Inf")
        self.assertEqual(buckets[-1][1], summary["ttft"]["count"])
        counts = [count for _, count in buckets]
        self.assertEqual(counts, sorted(counts))                          # cumulative
        json.dumps(summary)                                               # serialisable (no inf / nan)

    def test_active_requests_used_when_inflight_is_absent(self):
        text = 'dynamo_frontend_active_requests{model="m"} 4\ndynamo_frontend_requests_total{model="m",status="error"} 2\n' \
               'dynamo_frontend_requests_total{model="m",status="success"} 8\n'
        summary = c.summarize_frontend_metrics(text)
        self.assertEqual((summary["inflight"], summary["requests"], summary["errors"]), (4.0, 10.0, 2.0))
        both = c.summarize_frontend_metrics(text + 'dynamo_frontend_inflight_requests{model="m"} 5\n')
        self.assertEqual(both["inflight"], 5.0)

    def test_worker_metrics_decode(self):
        text = fixture("worker_decode_metrics.txt")
        summary = c.summarize_worker_metrics(text)
        self.assertTrue(summary["has_sglang"])
        self.assertEqual(summary["engine_type"], "decode")
        self.assertEqual(summary["dp_ranks"], 4)
        expected = sum(value for name, labels, value in c.parse_prometheus(text, ["sglang:num_running_reqs"]))
        self.assertEqual(summary["running"], expected)                    # one series per DP rank here
        self.assertGreater(summary["generation_tokens"], 0)
        self.assertTrue(0 <= summary["kv_usage"] <= 1)

    def test_worker_metrics_prefill(self):
        summary = c.summarize_worker_metrics(fixture("worker_prefill_metrics.txt"))
        self.assertEqual(summary["engine_type"], "prefill")
        self.assertGreater(summary["prompt_tokens"], summary["generation_tokens"])

    def test_tp_replicas_take_the_maximum_and_dp_ranks_add_up(self):
        text = "\n".join([
            'sglang:num_running_reqs{dp_rank="0",tp_rank="0"} 3', 'sglang:num_running_reqs{dp_rank="0",tp_rank="1"} 3',
            'sglang:num_running_reqs{dp_rank="1",tp_rank="0"} 5',
            'sglang:num_queue_reqs{dp_rank="0",priority=""} 2', 'sglang:num_queue_reqs{dp_rank="0",priority="high"} 2',
            'sglang:token_usage{dp_rank="0"} 0.25', 'sglang:token_usage{dp_rank="1"} 0.75',
            'sglang:generation_tokens_total{is_streaming="true",tp_rank="0"} 100', 'sglang:generation_tokens_total{is_streaming="true",tp_rank="1"} 100',
            'sglang:generation_tokens_total{is_streaming="false",tp_rank="0"} 7',
        ])
        summary = c.summarize_worker_metrics(text)
        self.assertEqual(summary["running"], 8.0)
        self.assertEqual(summary["queued"], 2.0)
        self.assertEqual(summary["kv_usage"], 0.75)
        self.assertEqual(summary["generation_tokens"], 107.0)
        self.assertEqual(summary["dp_ranks"], 2)

    def test_worker_without_engine_metrics_yet(self):
        summary = c.summarize_worker_metrics("dynamo_component_uptime_seconds 12.5\n")
        self.assertFalse(summary["has_sglang"])
        self.assertFalse(summary["serving"])                              # still loading: /health must not be asked
        self.assertEqual(summary["uptime"], 12.5)
        ready = c.summarize_worker_metrics('dynamo_component_uptime_seconds 99\ndynamo_component_inflight_requests{dynamo_endpoint="generate"} 0\n')
        self.assertTrue(ready["serving"])
        self.assertFalse(ready["has_sglang"])

    def test_frontend_health_lists_one_entry_per_worker(self):
        health = c.summarize_frontend_health(json.loads(fixture("frontend_health.json")))
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(sorted(item["component"] for item in health["instances"]), ["backend", "prefill", "prefill"])
        self.assertEqual(set(item["host"] for item in health["instances"]), {"10.0.0.24", "10.0.1.24"})
        self.assertTrue(all(isinstance(item["port"], int) for item in health["instances"]))

    def test_frontend_health_not_ready(self):
        self.assertEqual(c.summarize_frontend_health({"status": "notready", "stage": "discovery"}),
                         {"status": "notready", "instances": [], "stage": "discovery"})

    def test_prometheus_parser_skips_nan_and_other_families(self):
        samples = list(c.parse_prometheus('a_total{x="1",y="a\\"b"} 2\nb 3\na_bad NaN\na_inf +Inf\n# TYPE a_total counter\n', ["a_"]))
        self.assertEqual(samples, [("a_total", {"x": "1", "y": 'a\\"b'}, 2.0)])


class TokenClasses(unittest.TestCase):
    """Counters behind the cost split: tokens the prefill computed against tokens it took from a cache."""

    def page(self, ranks):
        lines = ['sglang:prompt_tokens_total{engine_type="prefill",is_streaming="true"} 24500',
                 'sglang:uncached_prompt_tokens_histogram_sum{engine_type="prefill"} 9100']
        for labels, scale in ranks:
            for mode, value in (("input", 9136), ("device_hit", 8475), ("host_hit", 4430), ("storage_hit", 2569)):
                lines.append('sglang:prefill_effective_tokens_total{engine_type="prefill",mode="%s",%s} %s' % (mode, labels, value * scale))
        return "\n".join(lines)

    def test_replicas_across_tp_ranks_are_counted_once(self):
        ranks = [('moe_ep_rank="%d",pp_rank="0",tp_rank="%d"' % (rank, rank), 1) for rank in range(4)]
        summary = c.summarize_worker_metrics(self.page(ranks))
        self.assertEqual((summary["computed_tokens"], summary["hit_tokens"]), (9136.0, 15474.0))
        self.assertEqual((summary["prompt_tokens"], summary["uncached_tokens"]), (24500.0, 9100.0))

    def test_dp_ranks_are_added_up(self):
        ranks = [('dp_rank="%d",tp_rank="0"' % rank, rank + 1) for rank in range(4)]
        summary = c.summarize_worker_metrics(self.page(ranks))
        self.assertEqual((summary["computed_tokens"], summary["hit_tokens"]), (9136.0 * 10, 15474.0 * 10))

    def test_engines_without_the_split_report_neither(self):
        summary = c.summarize_worker_metrics('sglang:prompt_tokens_total{engine_type="prefill"} 5')
        self.assertNotIn("computed_tokens", summary)
        self.assertNotIn("hit_tokens", summary)

    def test_inventory_keeps_the_pci_device_id_for_price_matching(self):
        rows = [{"index": "0", "uuid": "GPU-x", "name": "NVIDIA Graphics Device", "memory.total": "73415", "pci.bus_id": "00:01.0",
                 "pci.device_id": "0x2BB310DE", "driver_version": "590"}]
        self.assertEqual(c.gpu_inventory(rows)[0]["device"], "0x2BB310DE")


if __name__ == "__main__":
    unittest.main()
