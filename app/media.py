"""yt-dlp integration; only public, unprotected, single videos."""
import hashlib
import ipaddress
import os
from functools import lru_cache
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

import yt_dlp
from . import policy


class MediaError(ValueError):
    pass


def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
            raise ValueError()
        if p.port not in (None, 80, 443):
            raise ValueError()
        if policy.PUBLIC and not policy.source_allowed(url):
            raise MediaError('Bản công khai hiện chỉ nhận link TikTok, YouTube và Facebook.')
        addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not policy.public_address(a[4][0]) for a in addresses):
            raise ValueError()
    except MediaError:
        raise
    except (ValueError, OSError):
        raise MediaError('Hãy nhập link HTTP/HTTPS công khai hợp lệ; không dùng địa chỉ mạng nội bộ.')
    return url


def ffmpeg_available():
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


@lru_cache(maxsize=1)
def js_runtimes():
    for name, minimum in [('deno', (2, 3, 0)), ('node', (22, 0, 0))]:
        if not shutil.which(name):
            continue
        try:
            version = subprocess.run([name, '--version'], capture_output=True, text=True, timeout=3)
            match = re.search(r'(\d+)\.(\d+)\.(\d+)', version.stdout)
            if version.returncode == 0 and match and tuple(map(int, match.groups())) >= minimum:
                return {name: {}}
        except (OSError, subprocess.TimeoutExpired):
            continue
    return {}


YOUTUBE_DOMAINS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com')


def is_youtube(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in YOUTUBE_DOMAINS)


def youtube_client_chain():
    """Player-client profiles to try in order; CLIPDROP_YOUTUBE_CLIENTS forces one list."""
    override = [name.strip() for name in os.getenv('CLIPDROP_YOUTUBE_CLIENTS', '').split(',') if name.strip()]
    if override:
        return [tuple(override)]
    # Datacenter IPs are refused differently per client, so several profiles are
    # tried: visionos needs no PO token and answers in ~1-2s with full quality formats
    # without 403 stream errors. mweb and tv are fallbacks that make use of the PO token
    # provider when configured.
    if policy.PUBLIC:
        return [('visionos',), ('mweb',), ('tv',)]
    return [('visionos',), ('mweb',), ('tv',)]


def po_token_options(clients=None):
    """Extractor args for player clients and optional PO token provider."""
    if not policy.POT_URL:
        return {}
    if clients is None:
        clients = youtube_client_chain()[0]
    args = {'youtube': {'player_client': list(clients)}}
    # visionos is token-free and must not stall on bgutil (which takes 35s+ on 0.1 CPU).
    # Only token-dependent clients (e.g. mweb, tv) query the PO token provider.
    if 'visionos' not in clients:
        args['youtubepot-bgutilhttp'] = {'base_url': [policy.POT_URL]}
    return args


def client_label(config):
    """Player clients of one extraction attempt, for error text and logs."""
    return ','.join((config.get('extractor_args') or {}).get('youtube', {}).get('player_client', [])) or 'default'


ATTEMPT_TIMEOUT_SECONDS = 20  # one stalled profile must not delay the profile behind it
ATTEMPT_BUDGET_SECONDS = 45   # whole chain must still fit the 90s inspect timeout


def extraction_attempts(url):
    """YouTube first tries each PO-token client profile, then plain yt-dlp defaults."""
    attempts = []
    if is_youtube(url) and policy.POT_URL:
        for clients in youtube_client_chain():
            config = options()
            config['extractor_args'] = po_token_options(clients)
            attempts.append(config)
    attempts.append(options())
    return attempts


def attempt_extract(config, url, timeout):
    """Extract with one profile, giving up on it after timeout seconds.

    yt-dlp offers no way to interrupt an extraction, and a profile that waits on a
    PO token provider can stall far past its socket timeouts on the free plan. The
    work runs in a daemon thread that is left behind instead of blocking the next
    profile; the worker process exits as soon as the answer is printed.
    """
    holder = {}

    def target():
        try:
            with yt_dlp.YoutubeDL(config) as ydl:
                holder['info'] = ydl.extract_info(url, download=False)
        except Exception as exc:  # the reason is reported with this profile's label
            holder['error'] = exc

    thread = threading.Thread(target=target, daemon=True, name='clipdrop-extract')
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f'không phản hồi sau {timeout:g}s')
    if 'error' in holder:
        raise holder['error']
    return holder['info']


class QuietLogger:
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def options():
    config = dict(quiet=True, no_warnings=True, logger=QuietLogger(), noplaylist=True,
                socket_timeout=20, retries=2, fragment_retries=2, cachedir=False,
                allow_unplayable_formats=False, geo_bypass=False, proxy=policy.PROXY_URL or '',
                source_address='0.0.0.0',
                js_runtimes=js_runtimes(),
                remote_components=set())
    if policy.COOKIE_FILE and os.path.isfile(policy.COOKIE_FILE):
        config['cookiefile'] = policy.COOKIE_FILE
    if policy.PUBLIC:
        config.update(external_downloader={'default': 'native'}, hls_prefer_native=True,
                      ffmpeg_location='/opt/media-tools',
                      postprocessor_args={'ffmpeg_i': ['-protocol_whitelist', 'file,pipe'], 'ffmpeg_o': ['-threads', '1']})
    return config


def guard(info):
    if not info or info.get('_type') in ('playlist', 'multi_video') or 'entries' in info:
        raise MediaError('Chỉ hỗ trợ từng video. Hãy dán link của một video trong playlist.')
    if info.get('has_drm'):
        raise MediaError('Video có DRM. Ứng dụng không hỗ trợ tải nội dung được bảo vệ.')
    if info.get('availability') not in (None, 'public', 'unlisted'):
        raise MediaError('Video bị giới hạn truy cập. Chỉ hỗ trợ video công khai, không cần đăng nhập.')
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        raise MediaError('Chưa hỗ trợ livestream hoặc video đang được xử lý.')
    if policy.PUBLIC and (info.get('duration') or 0) > policy.MAX_DURATION:
        raise MediaError('Bản miễn phí chỉ hỗ trợ video tối đa 10 phút.')


def choices_for(info, ffmpeg):
    guard(info)
    formats = [f for f in info.get('formats', []) if not f.get('has_drm')
               and f.get('url') and f.get('format_id')
               and (not policy.PUBLIC or f.get('protocol') in policy.NETWORK_PROTOCOLS)
               and f.get('ext') in ('mp4', 'webm', 'mkv', 'm4a', 'mp3', 'opus', 'ogg', 'aac', 'flac', 'wav')]
    audios = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')]
    rank = lambda f: (f.get('abr') or f.get('tbr') or 0)
    result = []

    def add(parts, kind, ext, label, convert=False):
        ids = [str(f['format_id']) for f in parts]
        key = hashlib.sha256(repr((ids, kind, ext, convert)).encode()).hexdigest()[:24]
        sizes = [f.get('filesize') or f.get('filesize_approx') for f in parts]
        if policy.PUBLIC and sum(s or 0 for s in sizes) > policy.MAX_FILE_BYTES:
            return
        result.append(dict(id=key, kind=kind, ext=ext, label=label,
                           size=sum(sizes) if all(sizes) and not convert else None,
                           height=parts[0].get('height') or 0, needs_ffmpeg=len(parts) > 1 or convert,
                           _ids=ids, _convert=convert))

    seen = set()
    for f in reversed(formats):
        if f.get('vcodec') == 'none' or f.get('video_ext') == 'none':
            continue
        ext = f['ext']
        parts = [f]
        if f.get('acodec') == 'none':
            if not ffmpeg or not audios:
                continue
            compatible = [a for a in audios if (ext == 'mp4' and a['ext'] == 'm4a')
                          or (ext == 'webm' and a['ext'] in ('webm', 'opus'))]
            audio = max(compatible or audios, key=rank)
            if not compatible:
                ext = 'mkv'
            parts.append(audio)
        signature = (f.get('height'), ext, f.get('fps'), (f.get('vcodec') or '').split('.')[0])
        if signature in seen:
            continue
        seen.add(signature)
        resolution = f"{f['height']}p" if f.get('height') else 'Gốc'
        fps = f" · {f['fps']:g} fps" if f.get('fps') else ''
        codec = (f.get('vcodec') or '').split('.')[0].upper()
        add(parts, 'video', ext, f'{resolution}{fps}' + (f' · {codec}' if codec else ' · Thông tin codec chưa có'))
    result.sort(key=lambda c: c['height'], reverse=True)
    for ext in dict.fromkeys(a['ext'] for a in audios):
        best = max((a for a in audios if a['ext'] == ext), key=rank)
        bitrate = best.get('abr') or best.get('tbr')
        add([best], 'audio', ext, f"Âm thanh gốc{f' · {bitrate:.0f} kbps' if bitrate else ''}")
    if ffmpeg:
        sources = audios or [f for f in formats if f.get('acodec') != 'none']
        if sources:
            add([max(sources, key=rank)], 'audio', 'mp3', '192 kbps', True)
    if not result:
        raise MediaError('Không có định dạng không DRM phù hợp. Có thể cần FFmpeg hoặc nguồn không cho phép tải.')
    return result


def extract(url):
    validate_url(url)
    attempts = extraction_attempts(url)
    failures = []
    # Render Free has 0.1 CPU, so a slow profile must not eat the whole 90s
    # inspect timeout: every profile gets its own wall, and the budget only
    # decides whether another profile is opened at all. Reporting the reasons
    # stays possible because a skipped profile is listed too.
    deadline = time.monotonic() + ATTEMPT_BUDGET_SECONDS if len(attempts) > 1 else None
    for attempt, config in enumerate(attempts, start=1):
        label = client_label(config)
        if deadline and attempt > 1 and time.monotonic() > deadline:
            failures.append(f'attempt {attempt}: [{label}] skipped: hết ngân sách thời gian thử client')
            continue
        try:
            info = attempt_extract(config, url, ATTEMPT_TIMEOUT_SECONDS)
            break
        except Exception as exc:
            failures.append(f'attempt {attempt}: [{label}] {str(exc).strip()[-300:]}')
    else:
        # Keep every reason with its client label so Render Logs show which
        # profile the source refused (PO token clients first, then plain yt-dlp).
        raise yt_dlp.utils.DownloadError(' | '.join(failures))
    guard(info)
    if policy.PUBLIC and not info.get('duration'):
        raise MediaError('Nguồn không cung cấp thời lượng. Bản miễn phí chưa hỗ trợ video này.')
    # Remember which client/PO token profile worked so the download reuses it:
    # media URLs handed out by a token client are refused without the same token.
    info['_clipdrop_extractor_args'] = config.get('extractor_args')
    return info


def describe(info, ffmpeg):
    choices = choices_for(info, ffmpeg)
    thumb = info.get('thumbnail')
    if thumb and urlsplit(thumb).scheme != 'https':
        thumb = None
    return dict(title=info.get('title') or 'Video', uploader=info.get('uploader') or info.get('channel') or 'Không rõ tác giả',
                duration=info.get('duration'), thumbnail=thumb, source=info.get('extractor_key') or 'Video',
                formats=[{k: v for k, v in c.items() if not k.startswith('_')} for c in choices])


def user_error(exc):
    if isinstance(exc, MediaError):
        return str(exc)
    message = str(exc).lower()
    if 'drm' in message:
        return 'Video có DRM. Ứng dụng không hỗ trợ tải nội dung được bảo vệ.'
    if any(s in message for s in ('private', 'sign in', 'login', 'members', 'premium', '403', '401', 'age-restricted')):
        return 'Nguồn yêu cầu đăng nhập hoặc đang chặn truy cập. Ứng dụng không vượt qua hạn chế này.'
    if any(s in message for s in ('429', 'too many requests', 'temporarily blocked', 'rate-limit')):
        return 'Nguồn đang giới hạn IP của máy chủ (quá nhiều yêu cầu từ datacenter). Hãy thử lại sau hoặc dùng bản local.'
    if any(s in message for s in ('bot', 'captcha', 'player response', 'player_response', 'failed to extract',
                                  'no video formats', 'needs to be reloaded')):
        return 'Nguồn đang chặn bot từ IP máy chủ. Hãy thử link khác hoặc dùng bản local tại nhà.'
    if any(s in message for s in ('video is unavailable', 'this video is unavailable', 'video does not exist', 'has been removed by the uploader', 'has been removed by')):
        return 'Video này không tồn tại, đã bị xóa hoặc đặt ở chế độ riêng tư trên YouTube.'
    if 'unsupported url' in message:
        return 'Link này chưa được yt-dlp hỗ trợ. Hãy thử link trực tiếp của một video.'
    if any(s in message for s in ('không phản hồi sau', 'hết ngân sách thời gian thử client', 'timed out', 'timeout')):
        return 'Máy chủ không lấy kịp dữ liệu từ nguồn (nguồn phản hồi chậm hoặc chặn IP datacenter). Hãy thử lại sau ít phút.'
    detail = str(exc).strip().splitlines()[-1] if str(exc).strip() else ''
    return f'Không thể đọc hoặc tải video từ nguồn này. ({detail[-120:] if detail else "Lỗi không xác định"})'
