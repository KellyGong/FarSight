"""Command line entry point."""
import argparse
import getpass
import ipaddress
import json
import math
import os
import secrets
import signal
import socket
import sys

from .events import DEFAULT_THRESHOLDS
from .hub import Hub, VERSION
from .server import Server


def is_loopback(host):
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "farsight")


def load_token(config_dir):
    """A stable access token kept in the config directory (mode 600), created on first use."""
    path = os.path.join(config_dir, "token")
    try:
        with open(path) as handle:
            token = handle.read().strip()
        if len(token) >= 16:
            return token
    except (IOError, OSError):
        pass
    token = secrets.token_urlsafe(24)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    return token


def reachable_address(host):
    """An address to print in the URL when the server listens on every interface."""
    if host not in ("0.0.0.0", "::"):
        return host
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return socket.gethostname()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="farsight", description="Agentless web dashboard for Dynamo / SGLang inference clusters: nodes, GPUs, services, topology and cost, over SSH.")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--config-dir", default=default_config_dir(), help="where the node list, known_hosts and token are kept")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between hardware samples")
    parser.add_argument("--slow", type=float, default=10.0, help="seconds between service / process scans")
    parser.add_argument("--token", help="access token; by default one is generated when listening on a non-loopback address")
    parser.add_argument("--no-auth", action="store_true", help="serve a non-loopback address without a token (not recommended)")
    parser.add_argument("--strict-host-keys", action="store_true", help="refuse hosts whose key is not already in a known_hosts file")
    parser.add_argument("--thresholds", help="JSON file overriding alert thresholds")
    parser.add_argument("--prices", help="JSON table of GPU prices per GPU-hour (see examples/prices.example.json); "
                                         "without it, prices are the ones entered on the Settings page")
    parser.add_argument("--demo", action="store_true", help="synthetic cluster, no SSH; nothing is saved")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    if not 0.5 <= args.interval <= 60 or not args.interval <= args.slow <= 600:
        parser.error("--interval must be 0.5-60 s and --slow between --interval and 600 s")
    thresholds = {}
    if args.thresholds:
        try:
            with open(args.thresholds) as handle:
                thresholds = json.load(handle)
        except (IOError, OSError, ValueError) as error:
            parser.error("cannot read --thresholds: %s" % error)
        if not isinstance(thresholds, dict) or set(thresholds) - set(DEFAULT_THRESHOLDS):
            parser.error("unknown threshold; known: %s" % ", ".join(sorted(DEFAULT_THRESHOLDS)))
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in thresholds.values()):
            parser.error("thresholds must be non-negative numbers")

    config_dir = os.path.abspath(os.path.expanduser(args.config_dir))
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    token = args.token
    if not token and not is_loopback(args.host) and not args.no_auth:
        token = load_token(config_dir)

    try:
        hub = Hub(config_dir, interval=args.interval, slow=args.slow, thresholds=thresholds, demo=args.demo,
                  strict_host_key=args.strict_host_keys, prices=os.path.abspath(os.path.expanduser(args.prices)) if args.prices else None)
    except (IOError, OSError, ValueError) as error:
        parser.error("cannot read --prices: %s" % error)
    try:
        server = Server((args.host, args.port), hub, token=token, verbose=args.verbose, version=VERSION, default_user="root")
    except OSError as error:
        parser.error("cannot listen on %s:%d: %s" % (args.host, args.port, error))
    hub.start()

    address = reachable_address(args.host)
    shown = "[%s]" % address if ":" in address else address
    url = "http://%s:%d/" % (shown, args.port)
    print("FarSight %s" % VERSION, flush=True)
    print("  URL     : %s%s" % (url, "?token=" + token if token else ""), flush=True)
    print("  mode    : %s" % ("demo (synthetic data)" if args.demo else "live, %d node(s) loaded" % len(hub.sessions)), flush=True)
    print("  config  : %s" % config_dir, flush=True)
    if not token and not is_loopback(args.host):
        print("  WARNING : no access token on a non-loopback address; anyone who can reach this port can add nodes", flush=True)
    if is_loopback(args.host):
        print("  remote  : ssh -L %d:127.0.0.1:%d %s@<this host>, then open http://localhost:%d/" % (args.port, args.port, getpass.getuser(), args.port), flush=True)

    def shutdown(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
