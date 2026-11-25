"""HTTP API: access control, input validation and the node lifecycle, against a demo-mode hub (no SSH)."""
import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest

from farsight.hub import Hub, RequestError, expand_hosts
from farsight.server import Server

WRITE = {"Content-Type": "application/json", "X-Requested-With": "farsight"}


class HostLists(unittest.TestCase):
    def test_expansion(self):
        self.assertEqual(expand_hosts("10.0.0.[1-3,7] gpu-a, root@10.0.1.5:2222"),
                         [(None, "10.0.0.1", None), (None, "10.0.0.2", None), (None, "10.0.0.3", None), (None, "10.0.0.7", None),
                          (None, "gpu-a", None), ("root", "10.0.1.5", 2222)])
        self.assertEqual(expand_hosts("10.0.0.8-10\n"), [(None, "10.0.0.8", None), (None, "10.0.0.9", None), (None, "10.0.0.10", None)])
        self.assertEqual(expand_hosts("fe80::1"), [(None, "fe80::1", None)])
        self.assertEqual(expand_hosts("  "), [])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.hub = Hub(cls.directory, interval=0.5, slow=2.0, demo=True)
        cls.hub.start()
        cls.server = Server(("127.0.0.1", 0), cls.hub, version="test")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and cls.hub.state()["summary"]["online"] < 5:
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hub.stop()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, port=None):
        connection = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode("utf-8", "replace")
        return response.status, data, dict(response.getheaders())

    def wait_for(self, predicate, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.hub.state()
            if predicate(state):
                return state
            time.sleep(0.2)
        self.fail("condition not reached in %s s" % timeout)

    def test_state_shape(self):
        status, state, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["demo"])
        self.assertGreaterEqual(state["summary"]["online"], 5)
        self.assertGreaterEqual(len(state["services"]), 2)
        node = next(item for item in state["nodes"] if item["status"] == "online")
        for key in ("id", "name", "host", "cpu", "mem", "gpu", "vram", "gpu_count", "gpus", "services", "alerts"):
            self.assertIn(key, node)
        failed = next(item for item in state["nodes"] if item["status"] == "error")
        self.assertTrue(failed["error"]["title"] and failed["error"]["hint"])

    def test_service_and_node_detail(self):
        state = self.wait_for(lambda item: any(service["name"] == "chat-prod" for service in item["services"]))
        service = next(item for item in state["services"] if item["name"] == "chat-prod")
        status, detail, _ = self.request("GET", "/api/services/" + service["id"])
        self.assertEqual(status, 200)
        self.assertEqual(sorted(set(component["role"] for component in detail["components"])), ["decode", "etcd", "frontend", "prefill"])
        self.assertEqual([item["host"] for item in detail["missing"]], ["10.0.1.10"])
        self.assertEqual(len(detail["nodes"]), 3)
        status, node, _ = self.request("GET", "/api/nodes/" + detail["nodes"][0])
        self.assertEqual(status, 200)
        self.assertEqual(len(node["gpus"]), 8)
        self.assertIn(service["id"], [item["id"] for item in node["services"]])
        self.assertEqual(self.request("GET", "/api/nodes/%s/gpus/0" % detail["nodes"][0])[0], 200)
        self.assertEqual(self.request("GET", "/api/services/nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/nodes/nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/nodes/%s?window=7" % detail["nodes"][0])[0], 400)

    def test_topology_views(self):
        self.wait_for(lambda item: any(service["name"] == "chat-prod" for service in item["services"]))
        deadline = time.time() + 12
        while time.time() < deadline:
            status, view, _ = self.request("GET", "/api/topology")
            if view["edges"]:
                break
            time.sleep(0.3)
        self.assertEqual(status, 200)
        names = dict((node["id"], node["name"]) for node in view["nodes"])
        pairs = set(frozenset((names.get(edge["a"], edge["a"]), names.get(edge["b"], edge["b"]))) for edge in view["edges"])
        self.assertIn(frozenset(("gpu-a-24", "gpu-b-07")), pairs)
        self.assertIn(frozenset(("gpu-b-07", "gpu-b-08")), pairs)
        edge = next(edge for edge in view["edges"] if frozenset((names.get(edge["a"]), names.get(edge["b"]))) == frozenset(("gpu-a-24", "gpu-b-07")))
        kinds = set(channel["kind"] for channel in edge["channels"])
        self.assertTrue({"request", "response", "bootstrap", "kv_ctrl", "kv_data", "etcd"} <= kinds)
        self.assertEqual(view["dashboard"]["name"], "demo-host")
        self.assertTrue(any(item["unmonitored"] for item in view["externals"]))
        decode_node = next(node["id"] for node in view["nodes"] if node["name"] == "gpu-a-24")
        status, inside, _ = self.request("GET", "/api/nodes/%s/topology" % decode_node)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(component["role"] for component in inside["components"]), ["decode", "etcd", "frontend", "server"])
        self.assertTrue(any(component["inner"] for component in inside["components"]))
        self.assertTrue(any(component["procs"] for component in inside["components"]))
        self.assertEqual(self.request("GET", "/api/nodes/nope/topology")[0], 404)

    def test_messages_follow_the_language_of_the_request(self):
        chinese = self.request("GET", "/api/events")[1]["events"]
        english = self.request("GET", "/api/events", headers={"X-Lang": "en"})[1]["events"]
        browser = self.request("GET", "/api/events", headers={"Accept-Language": "en-US,en;q=0.9"})[1]["events"]
        self.assertIn(u"面板已启动", [event["title"] for event in chinese])
        self.assertIn("Dashboard started", [event["title"] for event in english])
        self.assertIn("Dashboard started", [event["title"] for event in browser])
        self.assertEqual([event["id"] for event in chinese][-1], [event["id"] for event in english][-1])
        failed = next(item for item in self.request("GET", "/api/state", headers={"X-Lang": "en"})[1]["nodes"] if item["status"] == "error")
        self.assertRegex(failed["error"]["title"] + failed["error"]["hint"], "^[\\x00-\\x7f]+$")
        status, reply, _ = self.request("POST", "/api/nodes", {"hosts": ""}, dict(WRITE, **{"X-Lang": "en"}))
        self.assertEqual((status, reply["error"]), (400, "Enter at least one host"))
        self.assertEqual(self.request("GET", "/api/nodes/nope", headers={"X-Lang": "en"})[1]["error"], "Not found")
        status, topology, _ = self.request("GET", "/api/topology", headers={"X-Lang": "en"})
        text = json.dumps(topology, ensure_ascii=False)
        self.assertNotRegex(text, u"[\u4e00-\u9fff]")

    def test_service_cost(self):
        state = self.wait_for(lambda item: any(service["name"] == "chat-prod" and (service.get("cost") or {}).get("blended") for service in item["services"]), 30)
        service = next(item for item in state["services"] if item["name"] == "chat-prod")
        self.assertEqual(service["cost"]["rent"], 8 * 2.5 + 4 * 4.0)
        self.assertEqual(service["cost"]["currency"], "$")
        status, detail, _ = self.request("GET", "/api/services/%s?cost=60" % service["id"])
        cost = detail["cost"]
        self.assertEqual((cost["rent"]["prefill"], cost["rent"]["decode"], cost["rent"]["unpriced"]), (20.0, 16.0, 0))
        self.assertTrue(cost["now"]["split"] and cost["partial"])
        self.assertEqual(cost["window"]["seconds"], 60)
        self.assertAlmostEqual(cost["now"]["hit"], cost["alpha"] * cost["now"]["miss"])
        tokens = cost["tokens"]
        self.assertAlmostEqual(cost["now"]["blended"], 36.0 / ((tokens["computed"] + tokens["hit"] + tokens["output"]) * 0.0036), places=6)
        self.assertEqual(self.request("GET", "/api/services/%s?cost=300" % service["id"])[1]["cost"]["window"]["seconds"], 300)
        self.assertEqual(self.request("GET", "/api/services/%s?cost=7" % service["id"])[0], 400)
        self.assertIn("cost", detail["history"])

    def test_cost_settings(self):
        status, settings, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertFalse(settings["saved"])                     # demo mode keeps nothing
        found = dict((gpu["name"], gpu) for gpu in settings["detected"])
        self.assertEqual((found["H100"]["price"], found["H100"]["source"], found["H100"]["model"]), (2.5, "table", "NVIDIA H100 80GB"))
        self.assertEqual((found["G200"]["price"], found["G200"]["source"], found["G200"]["model"]), (4.0, "override", None))
        self.assertEqual(self.request("PUT", "/api/settings", {"cost": {"tier": "silly"}}, WRITE)[0], 400)
        self.assertEqual(self.request("PUT", "/api/settings", {"cost": {"overrides": {"H100": -3}}}, WRITE)[0], 400)
        self.assertEqual(self.request("PUT", "/api/settings", {"cost": {"tier": "high"}})[0], 403)          # not from the page
        try:
            status, changed, _ = self.request("PUT", "/api/settings", {"cost": {"tier": "high", "overrides": {"G200": 20, "H100": 10}}}, WRITE)
            self.assertEqual(status, 200)
            self.assertEqual(changed["cost"]["tier"], "high")
            self.wait_for(lambda item: any(service["name"] == "chat-prod" and service["cost"]["rent"] == 8 * 10 + 4 * 20 for service in item["services"]))
        finally:
            self.request("PUT", "/api/settings", {"cost": {"tier": "mid", "overrides": {"G200": 4.0}}}, WRITE)

    def test_static_files_and_headers(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("FarSight", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("GET", "/js/app.js")[0], 200)
        for path in ("/../hub.py", "/js/../../hub.py", "/%2e%2e/hub.py", "/static/index.html", "/nodes.json"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_foreign_host_header_is_refused_without_a_token(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "localhost:%d" % self.port})[0], 200)

    def test_writes_need_the_page_header_and_matching_origin(self):
        body = {"hosts": "10.9.0.1"}
        self.assertEqual(self.request("POST", "/api/nodes", body, {"Content-Type": "application/json"})[0], 403)
        cross = dict(WRITE, Origin="http://evil.example")
        self.assertEqual(self.request("POST", "/api/nodes", body, cross)[0], 403)
        self.assertEqual(self.request("POST", "/api/nodes", body, {"X-Requested-With": "farsight", "Content-Type": "text/plain"})[0], 400)

    def test_rejects_bad_targets(self):
        for payload in ({"hosts": "-oProxyCommand=id"}, {"hosts": "a b;c$(id)"}, {"hosts": ""}, {"hosts": "10.0.0.1", "user": "-x"},
                        {"hosts": "10.0.0.1", "port": 70000}, {"hosts": "10.0.0.1", "auth": "magic"}, {"hosts": "10.0.0.1", "auth": "password"},
                        {"hosts": "10.0.0.[1-200]"}):
            status, data, _ = self.request("POST", "/api/nodes", payload, WRITE)
            self.assertEqual(status, 400, payload)
            self.assertTrue(data["error"])

    def test_node_lifecycle(self):
        status, data, _ = self.request("POST", "/api/nodes", {"hosts": "10.9.9.7, node-fail-1", "user": "root"}, WRITE)
        self.assertEqual(status, 200)
        good, bad = data["created"]
        state = self.wait_for(lambda item: {node["id"]: node["status"] for node in item["nodes"]}.get(good) == "online"
                              and {node["id"]: node["status"] for node in item["nodes"]}.get(bad) == "error")
        failed = next(node for node in state["nodes"] if node["id"] == bad)
        self.assertEqual(failed["error"]["code"], "auth_failed")
        self.assertEqual(self.request("POST", "/api/nodes", {"hosts": "10.9.9.7", "user": "root"}, WRITE)[0], 400)      # duplicate
        self.assertEqual(self.request("POST", "/api/nodes/%s/retry" % bad, {}, WRITE)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/nodes/" + bad, {}, WRITE)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/nodes/" + good, {}, WRITE)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/nodes/" + good, {}, WRITE)[0], 404)
        ids = [node["id"] for node in self.wait_for(lambda item: good not in [node["id"] for node in item["nodes"]])["nodes"]]
        self.assertNotIn(bad, ids)
        titles = [event["title"] for event in self.request("GET", "/api/events?limit=50")[1]["events"]]
        self.assertIn("节点已移除", titles)

    def test_password_never_leaves_the_hub(self):
        status, data, _ = self.request("POST", "/api/nodes", {"hosts": "10.9.9.8", "auth": "password", "password": "hunter2-secret"}, WRITE)
        self.assertEqual(status, 200)
        node_id = data["created"][0]
        self.wait_for(lambda item: any(node["id"] == node_id for node in item["nodes"]))
        for path in ("/api/state", "/api/nodes/" + node_id, "/api/events?limit=300"):
            self.assertNotIn("hunter2-secret", json.dumps(self.request("GET", path)[1]))
        self.request("DELETE", "/api/nodes/" + node_id, {}, WRITE)


class TokenAccess(unittest.TestCase):
    def test_token_is_required_and_the_link_sets_a_cookie(self):
        directory = tempfile.mkdtemp()
        hub = Hub(directory, demo=True)
        server = Server(("127.0.0.1", 0), hub, token="s3cr3t-token-value", version="test")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            def get(path, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", path, headers=headers or {})
                response = connection.getresponse()
                body = response.read().decode("utf-8", "replace")
                connection.close()
                return response.status, body, dict(response.getheaders())
            self.assertEqual(get("/api/state")[0], 401)
            status, body, _ = get("/")
            self.assertEqual(status, 401)
            self.assertIn("访问令牌", body)
            self.assertEqual(get("/api/state", {"Authorization": "Bearer wrong"})[0], 401)
            self.assertEqual(get("/api/state", {"Authorization": "Bearer s3cr3t-token-value"})[0], 200)
            status, _, headers = get("/?token=s3cr3t-token-value")
            self.assertEqual(status, 303)
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            self.assertEqual(get("/api/state", {"Cookie": "farsight_token=s3cr3t-token-value"})[0], 200)
            self.assertEqual(get("/api/state", {"Host": "anything.example", "Cookie": "farsight_token=s3cr3t-token-value"})[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(directory, ignore_errors=True)


class Persistence(unittest.TestCase):
    def test_saved_node_list_has_no_secret_and_is_private(self):
        import os
        directory = tempfile.mkdtemp()
        try:
            hub = Hub(directory)
            with self.assertRaises(RequestError):
                hub.add_nodes({"hosts": "10.255.255.1", "auth": "password"})
            hub.add_nodes({"hosts": "10.255.255.1", "user": "root", "auth": "password", "password": "top-secret-pw", "label": "x"})
            path = os.path.join(directory, "nodes.json")
            with open(path) as handle:
                text = handle.read()
            self.assertNotIn("top-secret-pw", text)
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
            hub.stop()
            again = Hub(directory)
            self.assertEqual([(item["host"], item["auth"], item["label"]) for item in again.load()], [("10.255.255.1", "password", "x")])
        finally:
            shutil.rmtree(directory, ignore_errors=True)


    def test_cost_settings_are_saved_privately_and_read_back(self):
        import os
        directory = tempfile.mkdtemp()
        try:
            hub = Hub(directory)
            hub.update_settings({"cost": {"tier": "low", "alpha": 0.05, "overrides": {"Card": 3.5}}})
            path = os.path.join(directory, "settings.json")
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
            hub.stop()
            again = Hub(directory)
            self.assertEqual((again.book.settings["tier"], again.book.alpha, again.book.settings["overrides"]), ("low", 0.05, {"Card": 3.5}))
            again.stop()
            with open(path, "w") as handle:
                handle.write("{broken")
            self.assertEqual(Hub(directory).book.settings["tier"], "mid")        # an unreadable file falls back to defaults
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
