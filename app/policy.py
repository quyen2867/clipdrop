"""Conservative limits for the single-instance, free public deployment."""
from collections import defaultdict, deque
import hashlib
import hmac
import ipaddress
import os
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
from pathlib import Path

PUBLIC = os.getenv('CLIPDROP_PUBLIC', '0') == '1'
PUBLIC_HOST = os.getenv('RENDER_EXTERNAL_HOSTNAME', '') or os.getenv('CLIPDROP_HOST', '')
ORIGIN = f'https://{PUBLIC_HOST}' if PUBLIC_HOST else ''
SECRET = os.getenv('CLIPDROP_SESSION_SECRET') or secrets.token_hex(32)
OWNER_TOKEN = os.getenv('CLIPDROP_OWNER_TOKEN', '')
TTL = 600 if PUBLIC else 3600
CONCURRENCY = 1 if PUBLIC else 2
QUEUE_SIZE = 8 if PUBLIC else 2  # includes running jobs
MAX_FILE_BYTES = 50 * 1024**2 if PUBLIC else 2 * 1024**3
MAX_JOB_BYTES = 160 * 1024**2 if PUBLIC else 6 * 1024**3
MAX_STORAGE_BYTES = 400 * 1024**2 if PUBLIC else 12 * 1024**3
MAX_DURATION = 600 if PUBLIC else None
DAILY_TRANSFER_BYTES = 100 * 1024**2  # public demo, resets on process restart
SOURCE_DOMAINS = ('youtube.com', 'youtu.be', 'tiktok.com', 'facebook.com', 'fb.watch', 'fb.com')
NETWORK_PROTOCOLS = ('http', 'https', 'm3u8_native', 'http_dash_segments')
_limits = defaultdict(deque)
_lock = threading.Lock()
_transfer = {'day': '', 'bytes': 0}
COOKIE = 'clipdrop_session'


def check_configuration():
    if PUBLIC and (not PUBLIC_HOST or len(os.getenv('CLIPDROP_SESSION_SECRET', '')) < 32):
        raise RuntimeError('Public mode requires a hostname and CLIPDROP_SESSION_SECRET (32+ characters).')
    if PUBLIC and not all(Path('/opt/media-tools', tool).is_file() for tool in ('ffmpeg', 'ffprobe')):
        raise RuntimeError('Public mode must use the supplied Docker image with restricted FFmpeg tools.')


def session_cookie(value=None):
    if value:
        try:
            token, signature = value.split('.')
            if len(token) == 48 and hmac.compare_digest(signature, hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()):
                return token, value
        except (ValueError, TypeError):
            pass
    token = secrets.token_hex(24)
    return token, f'{token}.{hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()}'


def owner_authorized(value=None):
    """Check the owner bypass token. Never authorizes when unset."""
    if not OWNER_TOKEN or not value:
        return False
    return hmac.compare_digest(value, OWNER_TOKEN)

def rate_allowed(key, count, seconds):
    now = time.monotonic()
    with _lock:
        if len(_limits) >= 10000:
            for old in list(_limits):
                if not _limits[old] or now - _limits[old][-1] > 3600:
                    del _limits[old]
            if key not in _limits and len(_limits) >= 10000:
                return False
        values = _limits[key]
        while values and values[0] <= now - seconds:
            values.popleft()
        if len(values) >= count:
            return False
        values.append(now)
        return True


def reserve_transfer(size):
    """Charge the whole file before serving, including range/repeated requests."""
    with _lock:
        day = time.strftime('%Y-%m-%d', time.gmtime())
        if _transfer['day'] != day:
            _transfer.update(day=day, bytes=0)
        if _transfer['bytes'] + size > DAILY_TRANSFER_BYTES:
            return False
        _transfer['bytes'] += size
        return True


def source_allowed(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in SOURCE_DOMAINS)


def public_address(address):
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address):
            # Reject translation/tunnel ranges as well as IPv4-mapped addresses.
            if ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in ipaddress.ip_network('64:ff9b::/96') or ip in ipaddress.ip_network('64:ff9b:1::/48'):
                return False
        return ip.is_global and not ip.is_multicast and not ip.is_unspecified
    except ValueError:
        return False


def network_audit(event, args):
    """Check the actual numeric connect target, after DNS, for Python transports.

    Installed only in worker processes. This covers redirects and media/manifest
    requests using yt-dlp's Python transports, including DNS rebinding to LAN.
    Docker FFmpeg wrappers separately restrict external tools to local inputs.
    """
    if event == 'socket.connect':
        _socket, address = args
        if not isinstance(address, tuple) or len(address) < 2 or not public_address(address[0]) or address[1] not in (80, 443):
            raise PermissionError('Chỉ cho phép kết nối tới địa chỉ Internet công khai qua cổng 80/443.')


def install_network_guard():
    if PUBLIC:
        sys.addaudithook(network_audit)
