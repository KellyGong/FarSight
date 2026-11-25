"""HTTP server: static web UI and the JSON API. Standard library only."""
import hmac
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .hub import RequestError
from .i18n import T, localize, pick

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".json": "application/json; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
         ".woff2": "font/woff2"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
COOKIE = "farsight_token"
MAX_BODY = 64 * 1024
WINDOWS = (300, 900, 3600)
COST_WINDOWS = (60, 300, 900, 3600)

LOGIN_TEXT = {
    "zh": {"lang": "zh-CN", "lead": "请输入访问令牌。令牌在面板启动时打印在终端里。", "token": "访问令牌", "enter": "进入"},
    "en": {"lang": "en", "lead": "Enter the access token. It is printed in the terminal when the dashboard starts.",
           "token": "Access token", "enter": "Enter"},
}
LOGIN_PAGE = """<!doctype html><html lang="%(lang)s"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FarSight</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:15px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#eef0f4;color:#14171f}
form{background:#fff;padding:32px;border-radius:20px;box-shadow:0 12px 40px rgba(20,23,31,.08);width:min(360px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 20px;color:#667085;font-size:13px}
input{width:100%%;box-sizing:border-box;padding:10px 12px;border:1px solid #d5d9e2;border-radius:10px;font:inherit;margin-bottom:12px}
button{width:100%%;padding:10px;border:0;border-radius:10px;background:#14171f;color:#fff;font:inherit;cursor:pointer}
@media (prefers-color-scheme:dark){body{background:#0e1116;color:#e8ebf1}form{background:#181c24;box-shadow:none}input{background:#0e1116;border-color:#2a303c;color:inherit}button{background:#e8ebf1;color:#0e1116}p{color:#98a2b3}}</style>
<form method="get" action="/"><h1>FarSight</h1><p>%(lead)s</p>
<input name="token" type="password" autocomplete="off" autofocus placeholder="%(token)s" aria-label="%(token)s"><button>%(enter)s</button></form></html>"""


def static_files():
    """Whitelist of servable files: URL path -> absolute path."""
    files = {}
    for root, _, names in os.walk(STATIC):
        for name in names:
            path = os.path.join(root, name)
            if os.path.splitext(name)[1] in TYPES:
                files["/" + os.path.relpath(path, STATIC).replace(os.sep, "/")] = path
    return files


class Handler(BaseHTTPRequestHandler):
    server_version = "farsight"
    protocol_version = "HTTP/1.1"

    # -- plumbing
    def log_message(self, template, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), template % args))

    def reply(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, value):
        """Reply with JSON; messages are rendered in the language the page asked for (X-Lang), else the browser's."""
        lang = pick(self.headers.get("X-Lang"), self.headers.get("Accept-Language"))
        body = json.dumps(localize(value, lang), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self.reply(status, body.encode("utf-8"), "application/json; charset=utf-8")

    def host_allowed(self):
        """Without a token only loopback names are served, which stops DNS-rebinding pages from reaching the API."""
        if self.server.token:
            return True
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        return name in LOOPBACK_HOSTS

    def token_from_request(self, query):
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return (query.get("token") or [""])[0]

    def authorized(self, query):
        token = self.server.token
        if not token:
            return True
        return hmac.compare_digest(self.token_from_request(query).encode("utf-8"), token.encode("utf-8"))

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError(T("请求无效"))
        if length > MAX_BODY:
            raise RequestError(T("请求体过大"))
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise RequestError(T("请求必须是 JSON"))
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise RequestError(T("JSON 格式错误"))
        if not isinstance(value, dict):
            raise RequestError(T("请求体必须是对象"))
        return value

    def same_origin(self):
        """Write requests must come from this page: a custom header plus a matching Origin when one is sent."""
        if self.headers.get("X-Requested-With") != "farsight":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            return urlsplit(origin).netloc == self.headers.get("Host", "")
        return True

    # -- dispatch
    def handle_request(self):
        request = urlsplit(self.path)
        query = parse_qs(request.query)
        path = request.path
        hub = self.server.hub
        try:
            if not self.host_allowed():
                return self.json(403, {"error": "Host not allowed"})
            if not self.authorized(query):
                if path.startswith("/api/"):
                    return self.json(401, {"error": T("需要访问令牌")})
                text = LOGIN_TEXT[pick(None, self.headers.get("Accept-Language"))]
                return self.reply(401, (LOGIN_PAGE % text).encode("utf-8"), TYPES[".html"])
            if self.command == "GET" and "token" in query and self.server.token and not path.startswith("/api/"):
                cookie = "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000" % (COOKIE, self.server.token)
                return self.reply(303, b"", "text/plain", [("Location", "/"), ("Set-Cookie", cookie)])
            if self.command in ("GET", "HEAD"):
                return self.get(path, query, hub)
            if not self.same_origin():
                return self.json(403, {"error": T("跨站请求被拒绝")})
            return self.write(path, hub)
        except RequestError as error:
            self.json(400, {"error": error.args[0] if error.args else T("请求无效")})
        except KeyError:
            self.json(404, {"error": T("不存在")})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.json(500, {"error": T("服务端错误，详见面板日志")})

    def window(self, query, name="window", allowed=WINDOWS):
        try:
            value = int((query.get(name) or [str(allowed[1])])[0])
        except ValueError:
            raise RequestError(T("window 无效"))
        if value not in allowed:
            raise RequestError(T("window 无效"))
        return value

    def get(self, path, query, hub):
        if path == "/api/state":
            return self.json(200, hub.state())
        if path == "/api/meta":
            return self.json(200, {"version": self.server.version, "demo": hub.demo, "interval": hub.interval,
                                   "default_user": self.server.default_user, "thresholds": hub.thresholds})
        if path == "/api/events":
            try:
                limit = max(1, min(300, int((query.get("limit") or ["100"])[0])))
            except ValueError:
                raise RequestError(T("limit 无效"))
            return self.json(200, {"events": hub.log.recent(limit), "alerts": hub.alerts.current()})
        if path == "/api/topology":
            return self.json(200, hub.topology())
        if path == "/api/settings":
            return self.json(200, hub.cost_settings())
        match = re.match(r"^/api/nodes/([0-9a-zA-Z_-]{1,40})/topology$", path)
        if match:
            return self.json(200, hub.node_topology(match.group(1)))
        match = re.match(r"^/api/nodes/([0-9a-zA-Z_-]{1,40})$", path)
        if match:
            return self.json(200, hub.node_detail(match.group(1), self.window(query)))
        match = re.match(r"^/api/nodes/([0-9a-zA-Z_-]{1,40})/gpus/(\d{1,3})$", path)
        if match:
            hub.session(match.group(1))
            return self.json(200, hub.gpu_history(match.group(1), int(match.group(2)), self.window(query)))
        match = re.match(r"^/api/services/([0-9a-zA-Z_-]{1,40})$", path)
        if match:
            return self.json(200, hub.service_detail(match.group(1), self.window(query), self.window(query, "cost", COST_WINDOWS)))
        if path.startswith("/api/"):
            raise KeyError(path)
        target = self.server.files.get("/index.html" if path == "/" else path)
        if target is None:
            return self.json(404, {"error": T("不存在")})
        with open(target, "rb") as handle:
            body = handle.read()
        return self.reply(200, body, TYPES[os.path.splitext(target)[1]])

    def write(self, path, hub):
        payload = self.body()        # always consume the body so a kept-alive connection stays in sync
        if self.command == "POST" and path == "/api/nodes":
            return self.json(200, hub.add_nodes(payload))
        if self.command == "PUT" and path == "/api/settings":
            return self.json(200, hub.update_settings(payload))
        match = re.match(r"^/api/nodes/([0-9a-zA-Z_-]{1,40})(/retry)?$", path)
        if match:
            node_id = match.group(1)
            if self.command == "POST" and match.group(2):
                return self.json(200, hub.retry_node(node_id))
            if self.command == "PATCH" and not match.group(2):
                return self.json(200, hub.update_node(node_id, payload))
            if self.command == "DELETE" and not match.group(2):
                return self.json(200, hub.remove_node(node_id))
        raise KeyError(path)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, hub, token=None, verbose=False, version="", default_user="root"):
        ThreadingHTTPServer.__init__(self, address, Handler)
        self.hub = hub
        self.token = token
        self.verbose = verbose
        self.version = version
        self.default_user = default_user
        self.files = static_files()
