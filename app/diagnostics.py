"""Read-only probes so /api/health can show what the container is really doing."""
import time
import urllib.request
from pathlib import Path

from . import policy

CGROUP = Path('/sys/fs/cgroup')
POT_PROBE_SECONDS = 15
POT_MARKER = b'server_uptime'
_pot = {'checked': 0.0, 'alive': False}


def pot_provider_alive(now=None):
    """Ping the PO token provider once per interval; cached so health stays cheap."""
    if not policy.POT_TARGET:
        return False
    now = time.monotonic() if now is None else now
    if now - _pot['checked'] < POT_PROBE_SECONDS:
        return _pot['alive']
    alive = False
    try:
        with urllib.request.urlopen(f"{policy.POT_URL.rstrip('/')}/ping", timeout=2) as response:
            alive = response.status == 200 and POT_MARKER in response.read(256)
    except (OSError, ValueError):
        alive = False
    _pot.update(checked=now, alive=alive)
    return alive


def memory_usage(root=None):
    """(used MiB, limit MiB, oom kills) from cgroup v2, or (None, None, None) elsewhere.

    A non-zero kill count is how you tell "YouTube refused us" apart from
    "the free instance ran out of RAM and the kernel killed something".
    """
    base = CGROUP if root is None else Path(root)
    try:
        used = int((base / 'memory.current').read_text().strip()) // 1024**2
        raw_limit = (base / 'memory.max').read_text().strip()
        limit = int(raw_limit) // 1024**2 if raw_limit.isdigit() else None
        kills = 0
        for line in (base / 'memory.events').read_text().splitlines():
            name, _, value = line.partition(' ')
            if name == 'oom_kill' and value.strip().isdigit():
                kills = int(value)
        return used, limit, kills
    except (OSError, ValueError):
        return None, None, None
