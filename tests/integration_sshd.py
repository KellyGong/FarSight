"""Password and key authentication against a throwaway sshd container (optional; needs docker).

    python3 -m unittest tests.integration_sshd -v

The container listens on 127.0.0.1 only and is removed afterwards. No real account is ever tried with a wrong
password. Set FARSIGHT_SSHD_IMAGE to reuse an image that already has openssh-server and python3.
"""
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from farsight.node import NodeSession

IMAGE = os.environ.get("FARSIGHT_SSHD_IMAGE", "farsight-sshd-test:latest")
NAME = "farsight-sshd-test-%d" % os.getpid()
PASSWORD = "correct-horse-battery"
DOCKERFILE = """FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server python3 && rm -rf /var/lib/apt/lists/* \\
 && mkdir -p /run/sshd && useradd -m -s /bin/bash tester && echo 'tester:%s' | chpasswd \\
 && useradd -m -s /bin/bash nopython \\
 && sed -i -E 's/^#?PasswordAuthentication.*/PasswordAuthentication yes/; s/^#?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
CMD ["/usr/sbin/sshd", "-D", "-e", "-p", "2222"]
""" % PASSWORD


def docker(*args, **kwargs):
    return subprocess.run(("docker",) + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, **kwargs)


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


@unittest.skipUnless(shutil.which("docker"), "docker is not available")
class PasswordAndKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.mkdtemp()
        if docker("image", "inspect", IMAGE).returncode != 0:
            context = tempfile.mkdtemp()
            with open(os.path.join(context, "Dockerfile"), "w") as handle:
                handle.write(DOCKERFILE)
            args = ["build", "--network", "host", "-t", IMAGE]
            for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                if os.environ.get(name):
                    args += ["--build-arg", "%s=%s" % (name, os.environ[name])]
            built = docker(*(args + [context]), timeout=900)
            shutil.rmtree(context, ignore_errors=True)
            if built.returncode != 0:
                raise unittest.SkipTest("cannot build the sshd test image: " + built.stdout[-400:])
        cls.port = free_port()
        started = docker("run", "-d", "--rm", "--name", NAME, "-p", "127.0.0.1:%d:2222" % cls.port, IMAGE)
        if started.returncode != 0:
            raise unittest.SkipTest("cannot start the sshd test container: " + started.stdout[-300:])
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.3)
        cls.user_known_hosts = digest(os.path.expanduser("~/.ssh/known_hosts"))

    @classmethod
    def tearDownClass(cls):
        docker("rm", "-f", NAME)
        shutil.rmtree(cls.runtime, ignore_errors=True)

    def connect(self, user="tester", auth="password", password=None, wait=20):
        session = NodeSession({"id": "t", "host": "127.0.0.1", "port": self.port, "user": user, "auth": auth}, self.runtime,
                              interval=1.0, slow=3.0, password=password)
        session.start()
        deadline = time.time() + wait
        while time.time() < deadline and session.status == "connecting":
            time.sleep(0.2)
        self.addCleanup(session.stop)
        return session

    def test_correct_password_connects_and_collects(self):
        session = self.connect(password=PASSWORD)
        self.assertEqual(session.status, "online", session.error)
        deadline = time.time() + 10
        while time.time() < deadline and session.fast is None:
            time.sleep(0.2)
        self.assertEqual(session.hello["user"], "tester")
        self.assertFalse(session.hello["caps"]["nvidia_smi"])           # no GPU tools in the container: degrade, do not fail
        self.assertEqual(session.hello["gpus"], [])
        self.assertIsNotNone(session.fast["mem"]["total"])

    def test_wrong_password_is_reported_and_not_retried(self):
        session = self.connect(password="definitely-wrong")
        self.assertEqual(session.status, "error")
        self.assertEqual(session.error["code"], "auth_failed")
        self.assertIn("密码", session.error["title"])
        self.assertFalse(session.error["retry"])
        self.assertIsNone(session.next_retry)

    def test_password_node_without_a_password_waits_for_one(self):
        session = self.connect(password=None, wait=3)
        self.assertEqual(session.status, "need_password")
        session.reconfigure({}, password=PASSWORD)
        deadline = time.time() + 20
        while time.time() < deadline and session.status != "online":
            time.sleep(0.2)
        self.assertEqual(session.status, "online", session.error)

    def test_key_mode_fails_cleanly_and_mentions_the_password_option(self):
        session = self.connect(auth="key")
        self.assertEqual(session.status, "error")
        self.assertEqual(session.error["code"], "auth_failed")
        self.assertIn("密码", session.error["hint"])

    def test_host_key_goes_to_the_dashboard_file_not_the_users(self):
        self.connect(password=PASSWORD)
        found = subprocess.run(["ssh-keygen", "-F", "[127.0.0.1]:%d" % self.port, "-f", os.path.join(self.runtime, "known_hosts")],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        self.assertIn("ssh-", found.stdout)                              # recorded (possibly hashed) in the dashboard's own file
        self.assertEqual(digest(os.path.expanduser("~/.ssh/known_hosts")), self.user_known_hosts)

    def test_the_password_is_not_on_any_command_line(self):
        session = self.connect(password=PASSWORD)
        self.assertEqual(session.status, "online", session.error)
        listing = subprocess.run(["ps", "-eo", "args"], stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertNotIn(PASSWORD, listing)


if __name__ == "__main__":
    unittest.main()
