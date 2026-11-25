"""Both UI languages are complete: every source string has an English entry, in the page scripts and in the server."""
import ast
import glob
import json
import os
import re
import unittest

from farsight import i18n

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "farsight", "static", "js")
CJK = re.compile(u"[一-鿿]")
LITERAL = r'"((?:[^"\\]|\\.)*)"'
FIELDS = re.compile(r"\{(\w+)\}")


def script_sources():
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "**", "*.js"), recursive=True)):
        if os.path.basename(path) not in ("i18n.js", "i18n-en.js"):
            with open(path, encoding="utf-8") as handle:
                yield os.path.relpath(path, ROOT), handle.read()


def english_table():
    table = {}
    with open(os.path.join(SCRIPTS, "i18n-en.js"), encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"^\s+%s: (.+),$" % LITERAL, line.rstrip("\n"))
            if match:
                table[json.loads('"%s"' % match.group(1))] = json.loads(match.group(2))
    return table


class PageScripts(unittest.TestCase):
    def setUp(self):
        self.table = english_table()
        self.used = {}
        for path, source in script_sources():
            for match in re.finditer(r"\b[tN]\(\s*" + LITERAL, source):
                self.used.setdefault(json.loads('"%s"' % match.group(1)), path)

    def test_every_source_string_has_an_english_entry(self):
        self.assertGreater(len(self.used), 300)
        missing = sorted(key for key in self.used if key not in self.table)
        self.assertEqual(missing, [], "add these to static/js/i18n-en.js")

    def test_no_unused_english_entries(self):
        self.assertEqual(sorted(key for key in self.table if key not in self.used), [])

    def test_translations_use_only_the_fields_of_the_source(self):
        for key, value in self.table.items():
            for text in value if isinstance(value, list) else [value]:
                self.assertLessEqual(set(FIELDS.findall(text)), set(FIELDS.findall(key)), key)
            if isinstance(value, list):
                self.assertEqual(len(value), 2, key)
                self.assertIn("n", FIELDS.findall(key), "a [singular, plural] entry is chosen by the n field: " + key)

    def test_chinese_text_only_appears_as_a_source_string(self):
        loose = []
        for path, source in script_sources():
            for number, line in enumerate(source.splitlines(), 1):
                rest = re.sub(r"\b[tN]\(\s*" + LITERAL, "", line)
                if CJK.search(rest):
                    loose.append("%s:%d" % (path, number))
        self.assertEqual(loose, [], "wrap these strings in t() or N()")


def server_literals():
    """(template literals passed to T(...), other string literals containing Chinese) of the server package."""
    templates, loose = {}, []
    for path in sorted(glob.glob(os.path.join(ROOT, "farsight", "*.py"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        inside = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in ("T", "Text") and node.args:
                first = node.args[0]
                options = [first.body, first.orelse] if isinstance(first, ast.IfExp) else [first]
                for option in options:
                    if isinstance(option, ast.Constant) and isinstance(option.value, str):
                        templates.setdefault(option.value, name)
                        inside.add(id(option))
        if name == "i18n.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value) and id(node) not in inside:
                loose.append((name, node.value[:40]))
    return templates, loose


class Server(unittest.TestCase):
    def test_every_message_has_an_english_template_with_the_same_fields(self):
        templates, _ = server_literals()
        self.assertGreater(len(templates), 100)
        self.assertEqual(sorted(key for key in templates if key not in i18n.EN), [], "add these to i18n.EN")
        for key in templates:
            self.assertEqual(set(FIELDS.findall(i18n.EN[key])), set(FIELDS.findall(key)), key)

    def test_no_unused_english_templates(self):
        templates, _ = server_literals()
        self.assertEqual(sorted(key for key in i18n.EN if key not in templates), [])

    def test_chinese_text_only_appears_in_messages(self):
        _, loose = server_literals()
        allowed = [item for item in loose if item[0] == "server.py"]         # the two-language login page
        self.assertEqual([item for item in loose if item not in allowed], [])
        self.assertLessEqual(len(allowed), 3)

    def test_message_is_a_string_and_renders_in_both_languages(self):
        message = i18n.T("节点连接中断：{title}", title=i18n.T("连接超时"))
        self.assertEqual(message, u"节点连接中断：连接超时")
        self.assertEqual(json.dumps({"m": message}, ensure_ascii=False), u'{"m": "节点连接中断：连接超时"}')
        self.assertEqual(message.render("en"), "Node connection lost: Connection timed out")
        joined = i18n.Joined([i18n.T("连接超时"), i18n.T("连接被拒绝")], zh=u"；", en="; ")
        self.assertEqual(joined, u"连接超时；连接被拒绝")
        self.assertEqual(joined.render("en"), "Connection timed out; Connection refused")

    def test_localize_walks_containers_and_leaves_chinese_untouched(self):
        value = {"events": [{"title": i18n.T("节点已连接"), "n": 3}], "plain": u"原样"}
        self.assertIs(i18n.localize(value, "zh"), value)
        self.assertEqual(i18n.localize(value, "en"), {"events": [{"title": "Node connected", "n": 3}], "plain": u"原样"})

    def test_language_of_a_request(self):
        self.assertEqual(i18n.pick("en", "zh-CN,zh;q=0.9"), "en")
        self.assertEqual(i18n.pick(None, "en-US,en;q=0.9"), "en")
        self.assertEqual(i18n.pick(None, "zh-CN,zh;q=0.9"), "zh")
        self.assertEqual(i18n.pick(None, ""), "zh")
        self.assertEqual(i18n.pick("xx", None), "zh")


if __name__ == "__main__":
    unittest.main()
