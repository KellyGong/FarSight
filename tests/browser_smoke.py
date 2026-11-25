"""Optional browser check of the web UI against a demo-mode server (needs Playwright and a Chromium).

    python -m venv /tmp/farsight-browser && /tmp/farsight-browser/bin/pip install playwright
    /tmp/farsight-browser/bin/playwright install chromium          # or pass --chromium /path/to/chrome
    /tmp/farsight-browser/bin/python tests/browser_smoke.py --artifacts /tmp/farsight-shots

The script starts its own `--demo` server on a free port, drives the page the way a user would, and writes
screenshots of the main views in both themes and at phone width. It exits non-zero on the first failed check.
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start_server(port, config_dir):
    process = subprocess.Popen([sys.executable, "-m", "farsight", "--demo", "--port", str(port), "--config-dir", config_dir],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            opener.open("http://127.0.0.1:%d/api/meta" % port, timeout=1).close()
            return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise SystemExit("the demo server did not start")


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls horizontally"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--chromium", help="path to a Chromium / Chrome executable (default: Playwright's own)")
    parser.add_argument("--artifacts", default="/tmp/farsight-shots")
    args = parser.parse_args()
    os.makedirs(args.artifacts, exist_ok=True)
    port = free_port()
    server = start_server(port, os.path.join(args.artifacts, "config"))
    url = "http://127.0.0.1:%d/" % port
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1480, "height": 1000}, color_scheme="light", locale="zh-CN")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append("page error: %s" % error))
            # The invalid-host submission below is answered with 400 on purpose; the browser logs that as an error.
            expected = "status of 400"
            page.on("console", lambda message: errors.append("console: " + message.text)
                    if message.type == "error" and expected not in message.text else None)
            shot = lambda name, **kw: page.screenshot(path=os.path.join(args.artifacts, name + ".png"), **kw)

            # overview
            page.goto(url, wait_until="load")
            expect(page.locator(".svc-card")).to_have_count(3, timeout=20000)
            expect(page.locator(".node-card.live")).to_have_count(5, timeout=20000)
            expect(page.locator(".node-card[data-status=error] .node-error b").first).to_contain_text("认证失败")
            expect(page.locator("#title")).to_have_text("集群概览")
            expect(page.locator(".svc-card", has_text="chat-prod").locator(".svc-cost")).to_contain_text("/小时", timeout=30000)
            no_overflow(page)
            page.wait_for_timeout(1500)
            shot("overview-light")

            # language: the button at the top right switches every label and the server's messages, and it is remembered
            assert page.evaluate("document.documentElement.lang") == "zh-CN"
            page.locator("#lang").click()
            expect(page.locator("#title")).to_have_text("Cluster overview")
            expect(page.locator("#add")).to_contain_text("Add node")
            expect(page.locator(".node-card[data-status=error] .node-error b").first).to_contain_text("Authentication failed", timeout=10000)
            assert page.evaluate("document.documentElement.lang") == "en"
            assert not page.evaluate("/[\u4e00-\u9fff]/.test(document.querySelector('.shell').innerText.replace('中', ''))"), "Chinese text left in the English page"
            no_overflow(page)
            page.reload(wait_until="load")
            expect(page.locator("#title")).to_have_text("Cluster overview", timeout=20000)
            page.locator(".panel-tools button", has_text="Topology").click()
            expect(page.locator(".topo-box.server")).to_have_count(4, timeout=20000)
            page.wait_for_timeout(1200)
            shot("topology-en")
            page.locator(".panel-tools button", has_text="Nodes").click()
            page.locator("#lang").click()
            expect(page.locator("#title")).to_have_text("集群概览")

            # search filters both rows; Escape clears it
            page.keyboard.press("/")
            page.keyboard.type("gpu-b")
            expect(page.locator(".node-card")).to_have_count(2)
            page.keyboard.press("Escape")
            expect(page.locator(".node-card.live")).to_have_count(5)
            page.locator(".seg button", has_text="异常").click()
            expect(page.locator(".node-card.live")).to_have_count(0)
            page.locator(".seg button", has_text="全部").click()

            # theme toggle persists across a reload
            assert page.evaluate("document.documentElement.dataset.theme") == "light"
            page.locator("#theme").click()
            page.wait_for_timeout(600)
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.reload(wait_until="load")
            expect(page.locator(".svc-card")).to_have_count(3, timeout=20000)
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.wait_for_timeout(1500)
            shot("overview-dark")

            # topology: servers and the protocols between them; a line's label opens its channels
            page.locator(".panel-tools button", has_text="拓扑").click()
            expect(page.locator(".topo-box.server")).to_have_count(4, timeout=20000)
            expect(page.locator(".topo-box.external")).to_have_count(3)
            expect(page.locator(".topo-lines path")).to_have_count(6)
            expect(page.locator(".topo-idle .chip")).to_have_count(3)
            label = page.locator('.topo-label[aria-label*="gpu-a-24"][aria-label*="gpu-b-07"]')
            expect(label).to_contain_text("TCP")
            label.click()
            rows = page.locator("table.channels:not([hidden]) tbody tr")
            expect(rows.filter(has_text="Dynamo 请求面")).to_have_count(1)
            expect(rows.filter(has_text="NIXL")).to_contain_text("推断")
            expect(page.locator(".topo-lines path.sel")).to_have_count(1)
            page.wait_for_timeout(800)
            no_overflow(page)
            shot("topology-dark", full_page=True)
            page.locator(".topo-table .btn", has_text="全部连接").click()
            expect(page.locator("table.channels:not([hidden]) tbody tr")).to_have_count(6)
            # a server opens its inside: components, their processes, the channels among them
            page.locator(".topo-box.server", has_text="gpu-a-24").click()
            expect(page.locator(".topo-box.component")).to_have_count(4, timeout=20000)
            expect(page.locator(".topo-box.component", has_text="chat-prod-sglang-decode")).to_contain_text("sglang::schedul")
            expect(page.locator(".topo-inner-row").first).to_be_visible()
            expect(page.locator(".topo-crumb b")).to_contain_text("gpu-a-24")
            page.wait_for_timeout(1000)
            no_overflow(page)
            shot("topology-inside-dark", full_page=True)
            page.go_back()
            expect(page.locator(".topo-box.server")).to_have_count(4)
            page.locator(".panel-tools button", has_text="节点").click()
            expect(page.locator(".node-card.live")).to_have_count(5)

            # service detail: card -> detail -> expand a component -> cost -> back
            page.locator(".svc-card", has_text="chat-prod").click()
            expect(page.locator(".detail .hero h2")).to_have_text("chat-prod")
            expect(page.locator(".step")).to_have_count(4)
            expect(page.locator(".absent")).to_contain_text("10.0.1.10")
            expect(page.locator(".chart")).to_have_count(5)
            expect(page.locator(".cost-rows")).to_contain_text("输入 · 未命中缓存")
            expect(page.locator(".cost-rows")).to_contain_text("8 × $2.50 = $20.0/h")
            page.locator(".section .seg button", has_text="1 分钟").click()
            expect(page.locator(".section .seg button", has_text="1 分钟")).to_have_attribute("aria-pressed", "true")
            page.locator(".comp-head").nth(1).click()
            expect(page.locator(".row.open .gpu-mini > div")).to_have_count(4)
            page.wait_for_timeout(1200)
            no_overflow(page)
            shot("service-dark")
            page.locator("#theme").click()
            page.wait_for_timeout(800)
            shot("service-light")
            page.locator(".hero .back").click()
            expect(page.locator(".svc-card")).to_have_count(3)
            expect(page.locator("#title")).to_have_text("集群概览")

            # node detail: GPUs, process ownership, chart tooltip by pointer and by keyboard
            page.locator(".node-card.live", has_text="gpu-a-24").click()
            expect(page.locator(".detail .hero h2")).to_have_text("gpu-a-24")
            expect(page.locator(".gpu-head")).to_have_count(8)
            page.locator(".gpu-head").first.click()
            expect(page.locator(".row.open table.plain tbody tr")).to_have_count(1)
            expect(page.locator(".row.open table.plain")).to_contain_text("chat-prod")
            chart = page.locator(".col:last-child .chart svg").first
            chart.hover()
            expect(page.locator("#tip")).to_be_visible()
            chart.focus()
            page.keyboard.press("ArrowLeft")
            expect(page.locator("#tip")).to_be_visible()
            page.keyboard.press("Escape")
            page.locator(".trend-tools .btn").click()
            expect(page.locator("dialog.sheet.wide table")).to_be_visible()
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            no_overflow(page)
            shot("node-light", full_page=True)
            page.locator(".hero .back").click()

            # add nodes: validation message, a failing node, a working node, then remove both
            page.locator("#add").click()
            page.locator("dialog.sheet button[type=submit]").click()
            expect(page.locator(".form-error")).to_contain_text("请填写主机地址")
            page.locator("dialog.sheet textarea").fill("-oProxyCommand=id")
            page.locator("dialog.sheet button[type=submit]").click()
            expect(page.locator(".form-error")).to_contain_text("主机地址无效")
            shot("add-dialog")
            page.locator("dialog.sheet textarea").fill("node-fail-9\n10.9.9.9")
            page.locator("dialog.sheet button[type=submit]").click()
            expect(page.locator("dialog.sheet")).to_have_count(0)
            failed = page.locator(".node-card", has_text="node-fail-9")
            expect(failed.locator(".node-error b")).to_contain_text("认证失败", timeout=15000)
            expect(page.locator(".node-card.live", has_text="node-9")).to_have_count(1, timeout=15000)
            failed.locator("button[aria-label=移除节点]").click()
            page.locator("dialog.sheet button", has_text="移除").click()
            expect(page.locator(".node-card", has_text="node-fail-9")).to_have_count(0, timeout=10000)
            page.locator(".node-card.live", has_text="node-9").click()
            page.locator(".hero .btn.danger").click()
            page.locator("dialog.sheet button", has_text="移除").click()
            expect(page.locator("#title")).to_have_text("集群概览", timeout=10000)
            expect(page.locator(".node-card", has_text="node-9")).to_have_count(0, timeout=10000)

            # the other pages render
            for hash_, title in (("#/nodes", "节点"), ("#/services", "服务"), ("#/activity", "活动"), ("#/settings", "设置")):
                page.evaluate("hash => { location.hash = hash }", hash_)
                expect(page.locator("#title")).to_have_text(title)
                no_overflow(page)
            expect(page.locator(".settings dl.kv").first).to_be_visible()

            # cost settings: a rent entered for a GPU model changes the services' rent
            price = page.locator('table.prices input[aria-label*="G200"]')
            expect(price).to_have_value("4")
            expect(page.locator("table.prices")).to_contain_text("NVIDIA H100")
            price.fill("20")
            page.locator(".cost-form button[type=submit]").click()
            expect(page.locator(".toast", has_text="成本设置已保存")).to_have_count(1)
            page.evaluate("location.hash = '#/'")
            expect(page.locator(".svc-card", has_text="chat-prod").locator(".svc-cost")).to_contain_text("$100/小时", timeout=10000)
            page.evaluate("location.hash = '#/settings'")
            page.locator('table.prices input[aria-label*="G200"]').fill("4")
            page.locator(".cost-form button[type=submit]").click()
            page.wait_for_timeout(700)
            shot("settings-light", full_page=True)

            # phone width
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("location.hash = '#/'")
            expect(page.locator(".svc-card")).to_have_count(3)
            page.wait_for_timeout(800)
            no_overflow(page)
            shot("overview-phone", full_page=True)
            page.locator(".svc-card", has_text="chat-prod").click()
            expect(page.locator(".detail .hero h2")).to_have_text("chat-prod")
            page.wait_for_timeout(800)
            no_overflow(page)
            shot("service-phone", full_page=True)
            page.evaluate("location.hash = '#/topology'")
            expect(page.locator(".topo-box.server")).to_have_count(4, timeout=20000)
            page.wait_for_timeout(800)
            no_overflow(page)
            shot("topology-phone", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    if errors:
        raise SystemExit("browser errors:\n  " + "\n  ".join(errors[:10]))
    print("Browser checks passed. Screenshots: %s" % args.artifacts)


if __name__ == "__main__":
    main()
