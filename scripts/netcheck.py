# OWNER: P4 -- see docs/WORK.md
"""LAN reachability check. Run this BEFORE building anything.

Most institutional networks run AP/client isolation: every device reaches the
internet, no device reaches any other device. If that is your campus wifi,
Samvad cannot run on it -- and you need to know in week one, not week four.

The important diagnostic is the difference between two failures:

    CONNECTION REFUSED -> the host is reachable, nothing is listening there.
                          Normal if that peer has not started its node yet.
    TIMEOUT            -> you cannot reach the host at all.
                          This is the isolation signature. This is the problem.

Usage:
    python scripts/netcheck.py --peers config/peers.yaml
    python scripts/netcheck.py --peer 192.168.1.42:8000 --peer 192.168.1.43:8000

Exit code 0 if every peer is reachable (listening or refusing), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

TCP_TIMEOUT = 3.0
HTTP_TIMEOUT = 3.0
INTERNET_PROBE = ("1.1.1.1", 53)

REACHABLE = "reachable"
REFUSED = "refused"
TIMEOUT = "timeout"
DNS_FAIL = "dns"


@dataclass
class Result:
    name: str
    host: str
    port: int
    tcp: str
    health: dict | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        # refused still proves the network path works, which is what we test here
        return self.tcp in (REACHABLE, REFUSED)


def probe_tcp(host: str, port: int) -> tuple[str, str]:
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return REACHABLE, ""
    except socket.timeout:
        return TIMEOUT, f"no response in {TCP_TIMEOUT:g}s"
    except socket.gaierror as e:
        return DNS_FAIL, str(e)
    except ConnectionRefusedError:
        return REFUSED, "host reachable, nothing listening"
    except OSError as e:
        # Windows raises OSError 10060/10064 for unreachable rather than timing out
        if getattr(e, "winerror", None) in (10060, 10064, 10065):
            return TIMEOUT, "host unreachable"
        return TIMEOUT, str(e)


def probe_health(host: str, port: int) -> dict | None:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def probe_internet() -> bool:
    try:
        with socket.create_connection(INTERNET_PROBE, timeout=TCP_TIMEOUT):
            return True
    except OSError:
        return False


def local_ip() -> str:
    """The address this machine presents on the LAN. No traffic is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(INTERNET_PROBE)
        return s.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        s.close()


def load_peers(path: str) -> list[tuple[str, str, int]]:
    try:
        import yaml
    except ImportError:
        sys.exit(
            "pyyaml is not installed.\n"
            "  pip install pyyaml\n"
            "or skip the config and pass addresses directly:\n"
            "  python scripts/netcheck.py --peer 192.168.1.42:8000"
        )
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"{path} not found. Copy config/peers.example.yaml and set your LAN addresses.")
    peers = (cfg or {}).get("peers") or {}
    if not peers:
        sys.exit(f"no peers defined in {path}")
    return [(name, p["host"], int(p["port"])) for name, p in peers.items()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify peer-to-peer LAN reachability.")
    ap.add_argument("--peers", help="path to peers.yaml")
    ap.add_argument("--peer", action="append", default=[], metavar="HOST:PORT",
                    help="peer address; repeatable. Use instead of --peers")
    args = ap.parse_args()

    targets: list[tuple[str, str, int]] = []
    if args.peers:
        targets += load_peers(args.peers)
    for raw in args.peer:
        host, _, port = raw.rpartition(":")
        if not host or not port.isdigit():
            sys.exit(f"bad --peer {raw!r}; expected HOST:PORT")
        targets.append((raw, host, int(port)))
    if not targets:
        targets = load_peers("config/peers.yaml")

    me = local_ip()
    internet = probe_internet()

    print(f"\nthis device: {me}")
    print(f"internet:    {'yes' if internet else 'no'}\n")

    results: list[Result] = []
    for name, host, port in targets:
        if host in (me, "127.0.0.1", "localhost"):
            continue  # don't test ourselves
        tcp, detail = probe_tcp(host, port)
        r = Result(name, host, port, tcp, detail=detail)
        if tcp == REACHABLE:
            r.health = probe_health(host, port)
        results.append(r)

    if not results:
        print("nothing to test -- every configured peer is this device.")
        print("Run this from a laptop with other peers configured.\n")
        return 0

    width = max(len(r.name) for r in results)
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        line = f"  [{mark}]  {r.name:<{width}}  {r.host}:{r.port}  {r.tcp}"
        if r.detail:
            line += f"  ({r.detail})"
        print(line)
        if r.health:
            h = r.health
            print(f"          {'':<{width}}  node up: agent={h.get('agent')} "
                  f"lamport={h.get('lamport')} children={h.get('children')}")

    unreachable = [r for r in results if not r.ok]
    print()

    if not unreachable:
        listening = sum(1 for r in results if r.tcp == REACHABLE)
        print(f"OK -- all {len(results)} peers reachable ({listening} with a node running).")
        print("This network works for Samvad. Record it in docs/RESULTS.md.\n")
        return 0

    print(f"FAILED -- {len(unreachable)} of {len(results)} peers unreachable:")
    for r in unreachable:
        print(f"  {r.name} at {r.host}:{r.port} -- {r.tcp} ({r.detail})")
    print()

    if internet and all(r.tcp == TIMEOUT for r in unreachable):
        print("  Internet works but peers time out. That is the AP/CLIENT ISOLATION")
        print("  signature: this network forwards traffic to the internet but blocks")
        print("  device-to-device. Samvad cannot run on it.")
    print("""
  In order of preference:
    1. Phone hotspot        -- usually does not isolate. Test it now; it is
                               probably your demo network.
    2. Dedicated router     -- no internet needed
    3. Ethernet switch      -- if the lab has one
    4. Ask IT for a non-isolated SSID -- slow, so ask early

  Also rule out the mundane causes:
    - the peer's node is not running        (that shows as 'refused', not 'timeout')
    - a host firewall is blocking Python    (Windows Defender prompts on first run)
    - stale IPs in peers.yaml               (they change when you switch networks)
""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
