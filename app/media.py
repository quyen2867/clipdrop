"""yt-dlp integration; only public, unprotected, single videos."""
import hashlib
import ipaddress
import os
from functools import lru_cache
import re
import shutil
import socket
import subprocess
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


def youtube_clients():
    clients = [name.strip() for name in os.getenv('CLIPDROP_YOUTUBE_CLIENTS', 'mweb').split(',') if name.strip()]
    return clients or ['mweb']


def po_token_options():
    """Extractor args for the bundled PO token provider, when one is configured."""
    if not policy.POT_URL:
        return {}
    return {'youtube': {'player_client': youtube_clients()},
            'youtubepot-bgutilhttp': {'base_url': [policy.POT_URL]}}


def extraction_attempts(url):
    """YouTube first tries the PO token provider, then plain yt-dlp defaults."""
    attempts = []
    pot = po_token_options() if is_youtube(url) else {}
    if pot:
        config = options()
        config['extractor_args'] = pot
        attempts.append(config)
    attempts.append(options())
    return attempts


class QuietLogger:
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def options():
    config = dict(quiet=True, no_warnings=True, logger=QuietLogger(), noplaylist=True,
                socket_timeout=20, retries=2, fragment_retries=2, cachedir=False,
                allow_unplayable_formats=False, geo_bypass=False, proxy='',
                js_runtimes=js_runtimes(),
                remote_components=set())
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
    failures = []
    for attempt, config in enumerate(extraction_attempts(url), start=1):
        try:
            with yt_dlp.YoutubeDL(config) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except yt_dlp.utils.YoutubeDLError as exc:
            failures.append(f'attempt {attempt}: {str(exc).strip()[-300:]}')
    else:
        # Keep every reason so logs show whether the PO token path or the
        # plain yt-dlp path was the one the source refused.
        raise yt_dlp.utils.DownloadError(' | '.join(failures))
    guard(info)
    if policy.PUBLIC and not info.get('duration'):
        raise MediaError('Nguồn không cung cấp thời lượng. Bản miễn phí chưa hỗ trợ video này.')
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
    if any(s in message for s in ('bot', 'captcha', 'player response', 'player_response', 'failed to extract')):
        return 'Nguồn đang chặn bot từ IP máy chủ. Hãy thử link khác hoặc dùng bản local tại nhà.'
    if 'unsupported url' in message:
        return 'Link này chưa được yt-dlp hỗ trợ. Hãy thử link trực tiếp của một video.'
    return 'Không thể đọc hoặc tải video từ nguồn này. Kiểm tra link, mạng và phiên bản yt-dlp rồi thử lại.'
