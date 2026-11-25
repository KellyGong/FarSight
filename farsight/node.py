"""One monitored node: connection state machine, collector stream reader, reconnect with backoff."""
import collections
import json
import os
import subprocess
import sys
import threading
import time

from . import ssh
from .i18n import T

COLLECTOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector.py")
BACKOFF = (3, 6, 12, 25, 45, 60)
FIRST_MESSAGE_TIMEOUT = 30.0


class NodeSession(object):
    """Keeps one collector running on one node and holds its latest samples.

    status: connecting | online | error | need_password | stopped
    """

    def __init__(self, config, runtime_dir, interval=2.0, slow=10.0, password=None, on_message=None, on_status=None,
                 strict_host_key=False):
        self.strict_host_key = strict_host_key
        self.config = dict(config)
        self.id = config["id"]
        self.runtime_dir = runtime_dir
        self.interval = interval
        self.slow_interval = slow
        self.password = password
        self.on_message = on_message
        self.on_status = on_status
        self.status = "connecting"
        self.error = None
        self.since = time.time()
        self.connected_at = None
        self.attempts = 0
        self.next_retry = None
        self.hello = None
        self.fast = None
        self.slow = None
        self.fast_at = None
        self.slow_at = None
        self.last_message = None
        self.collector_errors = collections.deque(maxlen=5)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = False
        self.restart = False
        self.process = None
        self.thread = threading.Thread(target=self.run, name="node-%s" % self.id)
        self.thread.daemon = True

    # -- control
    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped = True
        self.wake.set()
        self.kill()

    def retry(self):
        """Reconnect now, whatever the current state."""
        self.attempts = 0
        self.restart = True
        self.wake.set()
        self.kill()

    def reconfigure(self, config, password=None):
        with self.lock:
            self.config.update(config)
            if password is not None:
                self.password = password
        self.retry()

    def kill(self):
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
        except OSError:
            pass

    # -- state
    def set_status(self, status, error=None):
        with self.lock:
            changed = status != self.status or (error or {}).get("code") != (self.error or {}).get("code")
            previous = self.status
            self.status = status
            self.error = error
            if changed:
                self.since = time.time()
            if status == "online":
                self.connected_at = time.time()
                self.next_retry = None
        if changed and self.on_status:
            self.on_status(self, previous, status, error)

    def command(self):
        config = self.config
        if config.get("auth") == "local":
            return [sys.executable, "-u", COLLECTOR_PATH, "--interval", str(self.interval), "--slow", str(self.slow_interval)], None, False
        argv, env = ssh.build(config["host"], config["user"], config["port"], config.get("auth", "key"), self.runtime_dir,
                              key_path=config.get("key_path"), password=self.password,
                              interval=self.interval, slow=self.slow_interval, strict_host_key=self.strict_host_key)
        return argv, env, True

    # -- worker thread
    def run(self):
        while not self.stopped:
            self.restart = False
            self.wake.clear()
            if self.config.get("auth") == "password" and not self.password:
                self.set_status("need_password", ssh.problem("need_password"))
                self.wake.wait()
                continue
            # While retrying, keep showing why the last attempt failed instead of flipping to a blank "connecting".
            self.next_retry = None
            self.set_status("connecting", self.error if self.attempts else None)
            outcome = self.session()
            if self.stopped:
                break
            if self.restart:
                continue
            self.attempts += 1
            if outcome["retry"]:
                delay = BACKOFF[min(self.attempts - 1, len(BACKOFF) - 1)]
                self.next_retry = time.time() + delay
                self.set_status("error", outcome)
                self.wake.wait(delay)
            else:
                self.next_retry = None
                self.set_status("error", outcome)
                self.wake.wait()
        self.set_status("stopped")

    def session(self):
        """Run one collector session to its end and return the classified reason it ended."""
        try:
            argv, env, ship = self.command()
        except ssh.TargetError as error:
            return dict(ssh.problem("unknown", str(error)), retry=False)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, start_new_session=True)
        except OSError as error:
            return dict(ssh.problem("unknown", T("无法启动 ssh：{error}", error=str(error))), retry=False)
        self.process = process
        tail = collections.deque(maxlen=40)
        state = {"online": False, "killed": None, "started": time.time()}

        def drain():
            for raw in iter(process.stderr.readline, b""):
                tail.append(raw.decode("utf-8", "replace"))

        def ship_source():
            try:
                with open(COLLECTOR_PATH, "rb") as handle:
                    source = handle.read()
                process.stdin.write(str(len(source.decode("utf-8"))).encode() + b"\n" + source)
                process.stdin.flush()
            except (OSError, ValueError):
                pass

        def watchdog():
            while process.poll() is None:
                time.sleep(1.0)
                now = time.time()
                if not state["online"]:
                    if now - state["started"] > FIRST_MESSAGE_TIMEOUT:
                        state["killed"] = "no_response"
                        self.kill()
                elif self.last_message and now - self.last_message > max(20.0, 6 * self.interval):
                    state["killed"] = "disconnected"
                    self.kill()

        helpers = [threading.Thread(target=drain), threading.Thread(target=watchdog)]
        if ship:
            helpers.append(threading.Thread(target=ship_source))
        for helper in helpers:
            helper.daemon = True
            helper.start()
        for raw in iter(process.stdout.readline, b""):
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                tail.append(raw.decode("utf-8", "replace")[:300])
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            now = time.time()
            with self.lock:
                self.last_message = now
                if kind == "hello":
                    self.hello = message
                elif kind == "fast":
                    self.fast, self.fast_at = message, now
                elif kind == "slow":
                    self.slow, self.slow_at = message, now
                elif kind == "error":
                    self.collector_errors.append({"t": now, "where": message.get("where"), "message": message.get("message")})
            if kind == "hello":
                state["online"] = True
                self.attempts = 0
                self.set_status("online")
            if self.on_message:
                try:
                    self.on_message(self, message, now)
                except Exception:   # a consumer bug must not take the session down
                    pass
        returncode = process.wait()
        helpers[0].join(timeout=2)
        self.process = None
        if state["killed"] and not self.restart and not self.stopped:
            return ssh.problem(state["killed"], "".join(tail).strip()[-1200:])
        return ssh.classify("".join(tail), returncode, self.config.get("auth", "key"), state["online"])

    # -- views
    def public_config(self):
        config = self.config
        return {"id": self.id, "host": config.get("host"), "port": config.get("port"), "user": config.get("user"),
                "auth": config.get("auth"), "key_path": config.get("key_path"), "label": config.get("label") or "",
                "added": config.get("added")}

    def snapshot(self):
        """Consistent copy of the current state for readers on other threads."""
        with self.lock:
            return {"config": self.public_config(), "status": self.status, "error": self.error, "since": self.since,
                    "connected_at": self.connected_at, "attempts": self.attempts, "next_retry": self.next_retry,
                    "hello": self.hello, "fast": self.fast, "slow": self.slow, "fast_at": self.fast_at,
                    "slow_at": self.slow_at, "collector_errors": list(self.collector_errors)}


class FeedSession(NodeSession):
    """A node fed by an in-process generator of collector messages (demo mode and tests)."""

    def __init__(self, config, feed, **kwargs):
        NodeSession.__init__(self, config, runtime_dir="", **kwargs)
        self.feed = feed

    def run(self):
        self.set_status("connecting")
        for delay, message in self.feed(self):
            if self.stopped:
                break
            if delay:
                self.wake.wait(delay)
                self.wake.clear()
            if self.stopped:
                break
            if message is None:
                continue
            if message.get("type") == "status":      # scripted status change: {"type": "status", "status": ..., "error": ...}
                self.set_status(message["status"], message.get("error"))
                continue
            now = time.time()
            kind = message.get("type")
            with self.lock:
                self.last_message = now
                if kind == "hello":
                    self.hello = message
                elif kind == "fast":
                    self.fast, self.fast_at = message, now
                elif kind == "slow":
                    self.slow, self.slow_at = message, now
            if kind == "hello":
                self.set_status("online")
            if self.on_message:
                self.on_message(self, message, now)
        self.set_status("stopped")

    def kill(self):
        pass
