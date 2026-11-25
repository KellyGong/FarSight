"""OpenSSH client wrapper: target validation, argv construction, password via askpass, failure classification.

The system `ssh` is used instead of a Python SSH library so that ~/.ssh/config, ssh-agent and ProxyJump behave
exactly as they do in the user's terminal, and so that the dashboard has no third-party dependency.
"""
import os
import re
import shlex
import stat

from .i18n import Joined, T

HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
IPV6_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}(%[A-Za-z0-9._-]{1,32})?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}\$?$")

BOOTSTRAP = 'import sys;n=int(sys.stdin.readline());exec(compile(sys.stdin.read(n),"farsight_collector","exec"))'
ASKPASS = """#!/bin/sh
# farsight askpass helper: hands the in-memory password to ssh. It never accepts a host key.
case "$1" in
  *yes/no*) echo no ;;
  *) printf '%s\\n' "$FARSIGHT_ASKPASS_SECRET" ;;
esac
"""


class TargetError(ValueError):
    """The node address, user, port or key path is not acceptable."""


def validate_target(host, user, port, key_path=None):
    """Normalise and check a node target. Values that could be read as ssh options are rejected."""
    host = str(host or "").strip()
    user = str(user or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or host.startswith("-") or not (HOST_RE.match(host) or (":" in host and IPV6_RE.match(host))):
        raise TargetError(T("主机地址无效：只接受主机名、IPv4 或 IPv6 地址"))
    if not USER_RE.match(user):
        raise TargetError(T("用户名无效"))
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        raise TargetError(T("端口必须是 1-65535 的整数"))
    if key_path:
        key_path = os.path.expanduser(str(key_path).strip())
        if key_path.startswith("-") or "\n" in key_path or "\0" in key_path:
            raise TargetError(T("私钥路径无效"))
        if not os.path.isfile(key_path):
            raise TargetError(T("私钥文件不存在：{path}", path=key_path))
    return host, user, port, key_path or None


def remote_command(interval, slow, extra=()):
    """Shell command run on the node: find a Python 3.6+ and start the collector read from stdin."""
    args = " ".join(shlex.quote(str(item)) for item in ("--interval", interval, "--slow", slow) + tuple(extra))
    script = (
        'for p in python3 python; do '
        'if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys;sys.exit(sys.version_info<(3,6))" 2>/dev/null; then '
        'exec "$p" -u -c %s %s; fi; done; echo FARSIGHT_NO_PYTHON >&2; exit 127'
    ) % (shlex.quote(BOOTSTRAP), args)
    return "sh -c " + shlex.quote(script)


def ensure_askpass(runtime_dir):
    path = os.path.join(runtime_dir, "askpass.sh")
    try:
        with open(path) as handle:
            current = handle.read()
    except (IOError, OSError):
        current = None
    if current != ASKPASS:
        with open(path, "w") as handle:
            handle.write(ASKPASS)
    os.chmod(path, stat.S_IRWXU)
    return path


def build(host, user, port, auth, runtime_dir, key_path=None, password=None, interval=2, slow=10,
          connect_timeout=8, strict_host_key=False, extra=()):
    """Return (argv, env) for the collector session of one node. `auth` is "key" or "password"."""
    known = os.path.join(runtime_dir, "known_hosts")
    user_known = os.path.expanduser("~/.ssh/known_hosts")
    argv = [
        "ssh", "-T",
        "-o", "ConnectTimeout=%d" % connect_timeout,
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=%s" % ("yes" if strict_host_key else "accept-new"),
        "-o", "UserKnownHostsFile=%s %s" % (known, user_known),
        "-o", "UpdateHostKeys=no",
        "-o", "NoHostAuthenticationForLocalhost=no",       # port-forwarded nodes on 127.0.0.1 are checked like any other
        "-o", "LogLevel=ERROR",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        "-p", str(port),
        "-l", user,
    ]
    env = dict(os.environ)
    if auth == "password":
        argv += [
            "-o", "BatchMode=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
        ]
        env["SSH_ASKPASS"] = ensure_askpass(runtime_dir)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")          # OpenSSH < 8.4 consults askpass only when DISPLAY is set
        env["FARSIGHT_ASKPASS_SECRET"] = password or ""
    else:
        argv += ["-o", "BatchMode=yes"]
        if key_path:
            argv += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
    argv += ["--", host, remote_command(interval, slow, extra)]
    return argv, env


# (code, pattern). The first match wins, so the more specific causes come first.
FAILURES = [
    ("host_key_changed", r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed|host key for .* has changed"),
    ("bad_key", r"UNPROTECTED PRIVATE KEY FILE|bad permissions|invalid format|error in libcrypto|not accessible: No such file"),
    ("no_python", r"FARSIGHT_NO_PYTHON"),
    ("auth_failed", r"Permission denied|Too many authentication failures|Authentication failed|No more authentication methods|"
                    r"no mutual signature algorithm"),
    ("dns", r"Could not resolve hostname|Name or service not known|Temporary failure in name resolution|nodename nor servname"),
    ("refused", r"Connection refused"),
    ("unreachable", r"No route to host|Network is unreachable"),
    ("timeout", r"Connection timed out|Operation timed out|timed out"),
    ("closed", r"Connection closed by|Connection reset by|kex_exchange_identification|banner exchange|Broken pipe"),
    ("python_incomplete", r"ModuleNotFoundError: No module named|ImportError: No module named"),
    ("collector_crashed", r"Traceback \(most recent call last\)|SyntaxError"),
]
RETRYABLE = {"timeout", "refused", "unreachable", "dns", "closed", "disconnected", "no_response", "unknown", "collector_crashed"}

TEXT = {
    "host_key_changed": (T("主机密钥已变化"), T("该地址的 SSH 主机密钥与上次记录的不一致，可能是节点重装，也可能是中间人。确认无误后删除旧记录再重试。")),
    "bad_key": (T("私钥无法使用"), T("私钥文件格式不对、权限过宽（应为 600）或路径不存在。")),
    "no_python": (T("节点缺少 Python 3"), T("采集器需要节点上有 python3（3.6 及以上）。")),
    "python_incomplete": (T("节点的 Python 不完整"), T("节点上的 Python 缺少标准库模块。Debian / Ubuntu 上请安装 python3，而不是只有 python3-minimal。")),
    "dns": (T("主机名无法解析"), T("检查主机名拼写，或直接填写 IP 地址。")),
    "refused": (T("连接被拒绝"), T("该地址的 SSH 端口没有服务在监听。检查端口号，或确认节点上 sshd 已启动。")),
    "unreachable": (T("网络不可达"), T("到该地址没有路由。检查 IP 是否正确、是否在同一网络。")),
    "timeout": (T("连接超时"), T("在限定时间内没有连上。节点可能关机、IP 不对，或被防火墙拦截。")),
    "closed": (T("连接被对端关闭"), T("SSH 握手阶段被节点断开，可能是连接数限制或访问控制。")),
    "collector_crashed": (T("采集器异常退出"), T("采集脚本在节点上报错，详情见原始输出。")),
    "disconnected": (T("连接中断"), T("与节点的 SSH 会话断开，正在自动重连。")),
    "no_response": (T("节点没有响应"), T("SSH 已连上，但采集器在限定时间内没有返回数据。")),
    "need_password": (T("需要输入密码"), T("密码只保存在内存里，面板重启后需要重新输入。")),
    "unknown": (T("连接失败"), T("未能识别的失败原因，详情见原始输出。")),
}


def classify(stderr, returncode, auth="key", was_online=False):
    """Turn an ended ssh session into {code, title, hint, raw, retry}."""
    text = stderr or ""
    code = None
    for name, pattern in FAILURES:
        if re.search(pattern, text, re.IGNORECASE):
            code = name
            break
    if code is None:
        code = "disconnected" if was_online else "unknown"
    if code == "auth_failed":
        methods = re.search(r"Permission denied \(([^)]*)\)", text)
        allowed = methods.group(1) if methods else ""
        if auth == "password":
            title, hints = T("认证失败：密码未被接受"), [T("密码错误，或该节点不允许密码登录。")]
        else:
            title, hints = T("认证失败：密钥未被接受"), [T("确认用户名正确，且本机公钥已加入节点的 authorized_keys。")]
            if "password" in allowed or "keyboard-interactive" in allowed:
                hints.append(T("该节点也允许密码登录，可改用密码方式。"))
        if allowed:
            hints.append(T("节点允许的认证方式：{methods}。", methods=allowed))
        hint = Joined(hints)
    else:
        title, hint = TEXT[code]
    return {"code": code, "title": title, "hint": hint, "raw": text.strip()[-1200:],
            "retry": code in RETRYABLE, "returncode": returncode}


def problem(code, raw=""):
    title, hint = TEXT[code]
    return {"code": code, "title": title, "hint": hint, "raw": raw, "retry": code in RETRYABLE, "returncode": None}
