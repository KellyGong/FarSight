"""SSH target validation, command construction and failure classification."""
import os
import shlex
import tempfile
import unittest

from farsight import ssh


class Targets(unittest.TestCase):
    def test_accepts_names_and_addresses(self):
        self.assertEqual(ssh.validate_target(" 10.0.0.24 ", "root", "22"), ("10.0.0.24", "root", 22, None))
        self.assertEqual(ssh.validate_target("gpu-a.cluster.local", "svc_user", 2222)[0], "gpu-a.cluster.local")
        self.assertEqual(ssh.validate_target("[fe80::1]", "root", 22)[0], "fe80::1")

    def test_rejects_anything_ssh_could_read_as_an_option(self):
        for host in ["-oProxyCommand=id", "-F/etc/passwd", "a b", "a;b", "$(id)", "a`id`", "host\nname", "", "user@host", "a/b"]:
            with self.assertRaises(ssh.TargetError, msg=host):
                ssh.validate_target(host, "root", 22)
        for user in ["-l", "ro ot", "a;b", "", "root@x"]:
            with self.assertRaises(ssh.TargetError, msg=user):
                ssh.validate_target("10.0.0.1", user, 22)
        for port in [0, 65536, "x", None, -1]:
            with self.assertRaises(ssh.TargetError, msg=str(port)):
                ssh.validate_target("10.0.0.1", "root", port)
        with self.assertRaises(ssh.TargetError):
            ssh.validate_target("10.0.0.1", "root", 22, "/no/such/key")
        with self.assertRaises(ssh.TargetError):
            ssh.validate_target("10.0.0.1", "root", 22, "-oProxyCommand=id")


class Commands(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp()

    def test_key_mode_never_prompts(self):
        argv, env = ssh.build("10.0.0.1", "root", 22, "key", self.runtime)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertNotIn("FARSIGHT_ASKPASS_SECRET", env)
        self.assertEqual(argv[-3:-1], ["--", "10.0.0.1"])           # host comes after the option terminator
        self.assertIn("StrictHostKeyChecking=accept-new", argv)

    def test_password_mode_uses_askpass_and_keeps_the_secret_out_of_argv(self):
        argv, env = ssh.build("10.0.0.1", "root", 2222, "password", self.runtime, password="s3cret pass")
        self.assertNotIn("s3cret pass", " ".join(argv))
        self.assertEqual(env["FARSIGHT_ASKPASS_SECRET"], "s3cret pass")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertIn("PubkeyAuthentication=no", argv)
        self.assertIn("BatchMode=no", argv)
        self.assertTrue(os.access(env["SSH_ASKPASS"], os.X_OK))
        self.assertEqual(os.stat(env["SSH_ASKPASS"]).st_mode & 0o077, 0)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")

    def test_explicit_key(self):
        key = os.path.join(self.runtime, "id_test")
        open(key, "w").close()
        argv, _ = ssh.build("10.0.0.1", "root", 22, "key", self.runtime, key_path=key)
        self.assertEqual(argv[argv.index("-i") + 1], key)
        self.assertIn("IdentitiesOnly=yes", argv)

    def test_remote_command_is_one_shell_word_running_the_bootstrap(self):
        command = ssh.remote_command(2, 10)
        words = shlex.split(command)
        self.assertEqual(words[:2], ["sh", "-c"])
        self.assertEqual(len(words), 3)
        self.assertIn(ssh.BOOTSTRAP, shlex.split(words[2]))
        self.assertIn("FARSIGHT_NO_PYTHON", words[2])


class Failures(unittest.TestCase):
    CASES = [
        ("root@10.0.0.28: Permission denied (publickey,password).", 255, "key", "auth_failed", False),
        ("Permission denied, please try again.\nroot@h: Permission denied (publickey,password).", 255, "password", "auth_failed", False),
        ("ssh: connect to host 127.0.0.1 port 2: Connection refused", 255, "key", "refused", True),
        ("ssh: connect to host 10.255.255.1 port 22: Connection timed out", 255, "key", "timeout", True),
        ("ssh: Could not resolve hostname nope.invalid: Name or service not known", 255, "key", "dns", True),
        ("ssh: connect to host 10.1.1.1 port 22: No route to host", 255, "key", "unreachable", True),
        ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.", 255, "key", "host_key_changed", False),
        ("kex_exchange_identification: Connection closed by remote host", 255, "key", "closed", True),
        ("FARSIGHT_NO_PYTHON", 127, "key", "no_python", False),
        ("@ WARNING: UNPROTECTED PRIVATE KEY FILE! @\nLoad key \"/k\": bad permissions\nroot@h: Permission denied (publickey).", 255, "key", "bad_key", False),
        ("Traceback (most recent call last):\n  File \"farsight_collector\", line 1\nValueError: x", 1, "key", "collector_crashed", True),
        ("Traceback (most recent call last):\n  File \"farsight_collector\", line 21, in <module>\nModuleNotFoundError: No module named 'argparse'", 1, "key", "python_incomplete", False),
        ("", 255, "key", "unknown", True),
    ]

    def test_classification_table(self):
        for stderr, code, auth, expected, retry in self.CASES:
            outcome = ssh.classify(stderr, code, auth)
            self.assertEqual((outcome["code"], outcome["retry"]), (expected, retry), stderr)
            self.assertTrue(outcome["title"] and outcome["hint"])

    def test_auth_hint_depends_on_the_method_tried(self):
        key = ssh.classify("root@h: Permission denied (publickey,password).", 255, "key")
        self.assertIn("密钥", key["title"])
        self.assertIn("密码", key["hint"])                            # the node also accepts passwords: say so
        password = ssh.classify("root@h: Permission denied (publickey,password).", 255, "password")
        self.assertIn("密码", password["title"])
        only_key = ssh.classify("root@h: Permission denied (publickey).", 255, "key")
        self.assertNotIn("可改用密码", only_key["hint"])

    def test_session_that_was_online_ends_as_disconnected(self):
        outcome = ssh.classify("", 255, "key", was_online=True)
        self.assertEqual((outcome["code"], outcome["retry"]), ("disconnected", True))


if __name__ == "__main__":
    unittest.main()
