"""Ring buffers, the event log and alert timing."""
import unittest

from farsight.events import Alerts, DEFAULT_THRESHOLDS, EventLog, node_conditions, service_conditions
from farsight.history import Ring


class RingBuffer(unittest.TestCase):
    def test_capacity_and_gaps(self):
        ring = Ring(["a", "b"], capacity=3)
        for stamp in range(5):
            ring.append(stamp, {"a": stamp * 10, "b": None if stamp == 3 else 1})
        result = ring.query()
        self.assertEqual(result["t"], [2.0, 3.0, 4.0])
        self.assertEqual(result["a"], [20.0, 30.0, 40.0])
        self.assertEqual(result["b"], [1.0, None, 1.0])

    def test_downsampling_averages_buckets_and_respects_since(self):
        ring = Ring(["v"], capacity=100)
        for stamp in range(40):
            ring.append(stamp, {"v": stamp})
        result = ring.query(since=20, points=5)
        self.assertEqual(len(result["t"]), 5)
        self.assertEqual(result["t"][-1], 39.0)
        self.assertEqual(result["v"], [21.5, 25.5, 29.5, 33.5, 37.5])
        self.assertEqual(ring.query(since=99), {"t": [], "v": []})


def condition(key="node:n1:x", level="warn", sustain=60):
    return {"key": key, "level": level, "scope": "node", "scope_id": "n1", "name": "node-1", "title": "T", "detail": "d", "sustain": sustain}


class AlertTiming(unittest.TestCase):
    def setUp(self):
        self.log = EventLog()
        self.alerts = Alerts(self.log)

    def test_condition_must_hold_before_it_alerts(self):
        self.assertEqual(self.alerts.evaluate([condition()], now=0), [])
        self.assertEqual(self.alerts.evaluate([condition()], now=59), [])
        active = self.alerts.evaluate([condition()], now=61)
        self.assertEqual([alert["key"] for alert in active], ["node:n1:x"])
        self.assertEqual(active[0]["since"], 0)
        self.assertEqual([event["title"] for event in self.log.recent()], ["T"])

    def test_a_gap_restarts_the_clock(self):
        self.alerts.evaluate([condition()], now=0)
        self.alerts.evaluate([], now=30)
        self.assertEqual(self.alerts.evaluate([condition()], now=61), [])
        self.assertEqual(len(self.alerts.evaluate([condition()], now=125)), 1)

    def test_clears_only_after_a_quiet_period_and_logs_recovery(self):
        self.alerts.evaluate([condition(sustain=0)], now=0)
        self.assertEqual(len(self.alerts.evaluate([], now=5)), 1)            # one missing sample does not clear it
        self.assertEqual(len(self.alerts.evaluate([condition(sustain=0)], now=6)), 1)
        self.assertEqual(self.alerts.evaluate([], now=30), [])
        titles = [event["title"] for event in self.log.recent()]
        self.assertEqual(titles, ["已恢复：T", "T"])

    def test_critical_first_and_scope_can_be_dropped(self):
        self.alerts.evaluate([condition("node:n1:a", "warn", 0), condition("node:n1:b", "crit", 0), condition("node:n2:c", "info", 0)], now=0)
        self.assertEqual([alert["level"] for alert in self.alerts.current()], ["crit", "warn", "info"])
        self.alerts.drop_scope("node", "n1")
        self.assertEqual([alert["key"] for alert in self.alerts.current()], ["node:n2:c"])

    def test_event_log_filters(self):
        self.log.add("info", "node", "n1", "a", "one")
        self.log.add("info", "service", "s1", "b", "two")
        self.log.add("info", "node", "n2", "c", "three")
        self.assertEqual([event["title"] for event in self.log.recent(scope_ids={("node", "n1"), ("service", "s1")})], ["two", "one"])
        self.assertEqual([event["title"] for event in self.log.recent(limit=1)], ["three"])


class Conditions(unittest.TestCase):
    def node(self, gpus, procs=(), disks=(), mem=None, zombies=0):
        snapshot = {"config": {"id": "n1"}, "fast": {"gpus": gpus, "mem": mem or {"total": 1000, "used": 100, "shmem": 0}},
                    "slow": {"gpu_procs": list(procs), "disks": list(disks), "sglang_zombies": zombies}}
        return dict((item["key"].split(":", 2)[2], item) for item in node_conditions(snapshot, {"name": "node-1"}, DEFAULT_THRESHOLDS))

    def test_idle_spin_needs_high_utilisation_no_memory_and_no_process(self):
        spinning = {"i": 3, "util": 100.0, "mem": 4.0, "mem_total": 80000.0, "temp": 60}
        self.assertIn("gpu3:spin", self.node([spinning]))
        self.assertNotIn("gpu3:spin", self.node([spinning], procs=[{"gpu": 3}]))
        self.assertNotIn("gpu3:spin", self.node([dict(spinning, mem=70000.0)]))
        self.assertNotIn("gpu3:spin", self.node([dict(spinning, util=20.0)]))

    def test_temperature_levels(self):
        self.assertEqual(self.node([{"i": 0, "util": 50, "mem": 1000, "mem_total": 80000, "temp": 86}])["gpu0:temp"]["level"], "warn")
        self.assertEqual(self.node([{"i": 0, "util": 50, "mem": 1000, "mem_total": 80000, "temp": 95}])["gpu0:temp"]["level"], "crit")

    def test_memory_shm_disk_and_zombies(self):
        found = self.node([], mem={"total": 1000, "used": 950, "shmem": 500}, zombies=4,
                          disks=[{"mount": "/dev/shm", "total": 100, "used": 96}, {"mount": "/", "total": 100, "used": 50}, {"mount": "/raid", "total": 100, "used": 99}])
        self.assertEqual(sorted(found), ["memory", "shm", "zombies"])
        self.assertEqual(found["zombies"]["level"], "info")

    def test_service_conditions(self):
        service = {"id": "s", "name": "svc", "status": "degraded", "status_detail": "x",
                   "metrics": {"err_ratio": 0.05, "ttft_p95": 30.0, "worker_queued": 3}}
        codes = sorted(item["key"].split(":")[2] for item in service_conditions(service, DEFAULT_THRESHOLDS))
        self.assertEqual(codes, ["degraded", "errors", "queue", "ttft"])
        quiet = dict(service, status="running", metrics={"err_ratio": 0.0, "ttft_p95": 0.5, "worker_queued": 0})
        self.assertEqual(service_conditions(quiet, DEFAULT_THRESHOLDS), [])


if __name__ == "__main__":
    unittest.main()
