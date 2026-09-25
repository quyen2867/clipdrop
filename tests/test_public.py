import json
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from app import diagnostics, main, media, policy


@pytest.fixture
def public_client(monkeypatch, tmp_path):
    # Set public policy after lifespan startup; production also checks that the
    # restricted FFmpeg Docker wrappers and deployment secret are installed.
    monkeypatch.setattr(main, 'DATA', tmp_path)
    main.jobs.clear()
    main.inspections.clear()
    policy._limits.clear()
    policy._transfer.update(day='', bytes=0)
    with TestClient(main.app, base_url='https://localhost') as client:
        monkeypatch.setattr(policy, 'PUBLIC', True)
        monkeypatch.setattr(policy, 'ORIGIN', 'https://localhost')
        monkeypatch.setattr(policy, 'QUEUE_SIZE', 8)
        monkeypatch.setattr(policy, 'MAX_DURATION', 600)
        monkeypatch.setattr(policy, 'MAX_FILE_BYTES', 50 * 1024**2)
        yield client


def fake_metadata():
    return dict(title='Test', formats=[dict(id='a'*24, ext='mp4', kind='video')])


def test_session_signature_and_tampering():
    owner, cookie = policy.session_cookie()
    assert policy.session_cookie(cookie) == (owner, cookie)
    assert policy.session_cookie(cookie + 'x')[0] != owner
    assert policy.session_cookie('malformed')[0] != owner


def test_owner_token_never_authorizes_when_unset(monkeypatch):
    monkeypatch.setattr(policy, 'OWNER_TOKEN', '')
    assert not policy.owner_authorized('anything')
    assert not policy.owner_authorized('')
    assert not policy.owner_authorized(None)
    monkeypatch.setattr(policy, 'OWNER_TOKEN', 'secret-token')
    assert policy.owner_authorized('secret-token')
    assert not policy.owner_authorized('wrong')
    assert not policy.owner_authorized('')


@pytest.mark.parametrize('address', ['127.0.0.1', '10.2.3.4', '192.168.1.2', '169.254.169.254',
    '100.100.100.200', '0.0.0.0', '224.0.0.1', '::1', 'fc00::1', 'fe80::1',
    '::ffff:127.0.0.1', '64:ff9b::7f00:1', '64:ff9b:1::7f00:1', '2002:7f00:1::', 'localhost'])
def test_network_guard_rejects_actual_private_destinations(address):
    with pytest.raises(PermissionError):
        policy.network_audit('socket.connect', (None, (address, 443)))


def test_network_guard_rejects_other_ports_and_unix_sockets():
    for target in [('8.8.8.8', 22), '/var/run/docker.sock']:
        with pytest.raises(PermissionError): policy.network_audit('socket.connect', (None, target))
    policy.network_audit('socket.connect', (None, ('8.8.8.8', 443)))


def test_audit_hook_blocks_real_connect_and_dns_rebinding():
    # Isolate irreversible audit hooks. Resolving a public hostname to a private
    # destination must be blocked at socket.connect before the OS connects.
    script = '''
import socket, sys
from app.policy import network_audit
sys.addaudithook(network_audit)
socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))]
try:
    socket.create_connection(('www.youtube.com', 443), timeout=1)
except PermissionError:
    print('blocked')
else:
    raise AssertionError('connect was not blocked')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=main.ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'blocked'


@pytest.mark.parametrize('url', ['https://youtube.com.attacker.example/x', 'https://attacker.example/youtube.com',
    'https://example.com/video.mp4'])
def test_public_source_allowlist_rejects_other_sites(public_client, url):
    with pytest.raises(media.MediaError, match='TikTok'):
        media.validate_url(url)


def test_source_allowlist_supports_short_links():
    assert all(policy.source_allowed(url) for url in [
        'https://youtu.be/id', 'https://vm.tiktok.com/id', 'https://www.facebook.com/reel/id'])


def test_too_long_video_rejected(public_client):
    with pytest.raises(media.MediaError, match='10 phút'):
        media.guard(dict(duration=601))


def test_session_owns_inspection_and_download(public_client, monkeypatch):
    monkeypatch.setattr(main, 'worker', lambda *a: fake_metadata())
    data = public_client.post('/api/inspect', json={'url':'https://youtu.be/test'}).json()
    owner = policy.session_cookie(public_client.cookies.get(policy.COOKIE))[0]
    job_id = 'f'*32
    directory = main.DATA / job_id
    directory.mkdir()
    (directory / 'media.mp4').write_bytes(b'video')
    main.jobs[job_id] = dict(owner=owner, status='ready', updated=time.time(),
                             result=dict(path='media.mp4', title='test', ext='mp4'))
    assert public_client.get(f'/api/downloads/{job_id}/file').content == b'video'
    other = TestClient(main.app, base_url='https://localhost')
    try:
        assert other.post('/api/downloads', json={'inspection_id':data['inspection_id'],'format_id':'a'*24}).status_code == 410
        assert other.get(f'/api/downloads/{job_id}').status_code == 404
        assert other.get(f'/api/downloads/{job_id}/file').status_code == 404
    finally:
        other.close()


def test_public_cookie_flags_and_origin(public_client):
    response = public_client.get('/')
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
    assert public_client.post('/api/inspect', json={'url':'https://youtu.be/test'}, headers={'origin':'https://other.example'}).status_code == 403
    assert public_client.post('/api/inspect', content='a'*4097, headers={'content-type':'application/json'}).status_code == 413


def test_rate_limits_and_transfer_budget_cannot_overrun(monkeypatch):
    policy._limits.clear()
    policy._transfer.update(day='', bytes=0)
    monkeypatch.setattr(policy, 'DAILY_TRANSFER_BYTES', 10)
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(lambda _: policy.rate_allowed('test', 3, 60), range(20))) == 3
        assert sum(pool.map(lambda _: policy.reserve_transfer(3), range(20))) == 3


def test_public_queue_reports_position_and_blocks_second_job(public_client, monkeypatch):
    class Queue:
        def submit(self, *args): pass  # Leave work queued deterministically.
    monkeypatch.setattr(main, 'pool', Queue())
    monkeypatch.setattr(main, 'worker', lambda *a: fake_metadata())
    data = public_client.post('/api/inspect', json={'url':'https://youtu.be/test'}).json()
    payload = {'inspection_id':data['inspection_id'],'format_id':'a'*24}
    response = public_client.post('/api/downloads', json=payload)
    assert response.status_code == 202
    job = public_client.get('/api/downloads/' + response.json()['job_id']).json()
    assert job == dict(status='queued', position=1)
    assert public_client.post('/api/downloads', json=payload).status_code == 429


def test_storage_guard_kills_worker(public_client, monkeypatch, tmp_path):
    monkeypatch.setattr(policy, 'MAX_JOB_BYTES', 5)
    (tmp_path / 'media.part').write_bytes(b'oversized')
    class Process:
        pid = 999
        def communicate(self, *a, **k): return '', ''
    killed = []
    monkeypatch.setattr(main.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(main.os, 'killpg', lambda *a: killed.append(a))
    with pytest.raises(RuntimeError, match='dung lượng'):
        main.worker(dict(directory=str(tmp_path)), 1)
    assert killed == [(999, main.signal.SIGKILL)]


def test_pot_target_requires_loopback_http():
    assert policy.loopback_target('http://127.0.0.1:4416') == ('127.0.0.1', 4416)
    assert policy.loopback_target('http://[::1]:4416') == ('::1', 4416)
    for url in ['', 'http://0.0.0.0:4416', 'https://127.0.0.1:4416', 'http://127.0.0.1',
                'http://pot.internal:4416', 'http://localhost:4416']:
        assert policy.loopback_target(url) is None


def test_pot_url_defaults_on_with_bundled_provider_and_env_overrides(monkeypatch):
    # The public image ships the provider, so it is on without any env var.
    assert policy.pot_url(public=True, bundled=True, value='') == policy.DEFAULT_POT_URL
    assert policy.pot_url(public=False, bundled=True, value='') == ''
    assert policy.pot_url(public=True, bundled=False, value='') == ''
    # An explicit value always wins: custom loopback URL, or the off switch.
    assert policy.pot_url(public=True, bundled=True, value='http://127.0.0.1:9999') == \
        'http://127.0.0.1:9999'
    for value in ['0', 'off', 'none', ' OFF ']:
        assert policy.pot_url(public=True, bundled=True, value=value) == ''
    monkeypatch.setenv('CLIPDROP_POT_URL', 'http://127.0.0.1:4417')
    assert policy.pot_url(public=True, bundled=True) == 'http://127.0.0.1:4417'
    monkeypatch.setenv('CLIPDROP_POT_URL', '')
    assert policy.pot_url(public=True, bundled=True) == policy.DEFAULT_POT_URL


def test_network_guard_allows_only_the_configured_pot_port(monkeypatch):
    monkeypatch.setattr(policy, 'POT_TARGET', ('127.0.0.1', 4416))
    policy.network_audit('socket.connect', (None, ('127.0.0.1', 4416)))
    for target in [('127.0.0.1', 4417), ('127.0.0.2', 4416), ('8.8.8.8', 4416), ('::1', 4416)]:
        with pytest.raises(PermissionError):
            policy.network_audit('socket.connect', (None, target))


def test_youtube_attempts_use_pot_provider_then_fall_back(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setenv('CLIPDROP_YOUTUBE_CLIENTS', 'mweb')
    attempts = media.extraction_attempts('https://www.youtube.com/watch?v=x')
    assert attempts[0]['extractor_args'] == {
        'youtube': {'player_client': ['mweb']},
        'youtubepot-bgutilhttp': {'base_url': ['http://127.0.0.1:4416']}}
    assert 'extractor_args' not in attempts[1]
    assert len(media.extraction_attempts('https://youtu.be/x')) == 2
    assert len(media.extraction_attempts('https://www.tiktok.com/@a/video/1')) == 1
    monkeypatch.setattr(policy, 'POT_URL', '')
    assert len(media.extraction_attempts('https://www.youtube.com/watch?v=x')) == 1


def test_extract_reports_every_attempt_failure(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setenv('CLIPDROP_YOUTUBE_CLIENTS', 'mweb')
    seen = []

    class FakeYoutubeDL:
        def __init__(self, config):
            seen.append(config)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=False):
            raise media.yt_dlp.utils.DownloadError(
                'failed to extract any player response' if len(seen) == 1 else 'plain failure')

    monkeypatch.setattr(media.yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    with pytest.raises(media.yt_dlp.utils.YoutubeDLError) as excinfo:
        media.extract('https://www.youtube.com/watch?v=x')
    message = str(excinfo.value)
    assert 'attempt 1:' in message and 'attempt 2:' in message
    assert '[mweb]' in message and '[default]' in message
    assert 'player response' in message and 'plain failure' in message
    assert media.user_error(excinfo.value).startswith('Nguồn đang chặn bot')
    assert len(seen) == 2


def test_user_error_explains_youtube_client_refusals():
    assert media.user_error(ValueError('No video formats found!')).startswith('Nguồn đang chặn bot')
    assert media.user_error(ValueError('The page needs to be reloaded.')).startswith('Nguồn đang chặn bot')
    assert media.user_error(ValueError('Sign in to confirm you are not a bot.')).startswith('Nguồn yêu cầu đăng nhập')
    assert media.user_error(ValueError('unsupported URL')).startswith('Link này chưa được yt-dlp hỗ trợ')


def test_slow_chain_stops_opening_new_profiles(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.delenv('CLIPDROP_YOUTUBE_CLIENTS', raising=False)
    monkeypatch.setattr(media, 'ATTEMPT_BUDGET_SECONDS', -1)
    seen = []

    class FakeYoutubeDL:
        def __init__(self, config): seen.append(config)
        def __enter__(self): return self
        def __exit__(self, *_exc): return False
        def extract_info(self, _url, download=False):
            raise media.yt_dlp.utils.DownloadError('refused')

    monkeypatch.setattr(media.yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    with pytest.raises(media.yt_dlp.utils.YoutubeDLError) as excinfo:
        media.extract('https://www.youtube.com/watch?v=x')
    message = str(excinfo.value)
    assert len(seen) == 1  # the budget was spent, so later profiles were skipped
    assert message.count('skipped') == 3
    assert '[mweb]' in message and '[android_vr]' in message and '[default]' in message


def test_youtube_client_chain_orders_profiles(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.delenv('CLIPDROP_YOUTUBE_CLIENTS', raising=False)
    clients = [a['extractor_args']['youtube']['player_client']
               for a in media.extraction_attempts('https://youtu.be/x') if 'extractor_args' in a]
    assert clients == [['mweb'], ['tv'], ['android_vr']]
    monkeypatch.setenv('CLIPDROP_YOUTUBE_CLIENTS', 'tv, mweb')
    clients = [a['extractor_args']['youtube']['player_client']
               for a in media.extraction_attempts('https://youtu.be/x') if 'extractor_args' in a]
    assert clients == [['tv', 'mweb']]


def test_extract_keeps_the_working_profile_for_download(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setenv('CLIPDROP_YOUTUBE_CLIENTS', 'tv')

    class FakeYoutubeDL:
        def __init__(self, _config): pass
        def __enter__(self): return self
        def __exit__(self, *_exc): return False
        def extract_info(self, _url, download=False):
            return dict(title='Test', availability='public', duration=10, formats=[])

    monkeypatch.setattr(media.yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    info = media.extract('https://www.youtube.com/watch?v=x')
    assert info['_clipdrop_extractor_args'] == media.po_token_options(('tv',))


def test_worker_download_reuses_the_extracted_profile(monkeypatch, tmp_path):
    from app import worker
    formats = [dict(format_id='18', ext='mp4', vcodec='avc1', acodec='aac',
                    url='https://example.com/video', protocol='https')]
    info = dict(title='Test', availability='public', duration=5, formats=formats)
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    info['_clipdrop_extractor_args'] = media.po_token_options(('tv',))
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, config): seen.update(config)
        def __enter__(self): return self
        def __exit__(self, *_exc): return False
        def extract_info(self, _url, download=True): (tmp_path / 'media.mp4').write_bytes(b'media')

    monkeypatch.setattr(worker, 'extract', lambda _url: info)
    monkeypatch.setattr(worker.yt_dlp, 'YoutubeDL', FakeYoutubeDL)
    choice = media.choices_for(info, True)[0]
    result = worker.run(dict(action='download', url='https://www.youtube.com/watch?v=x',
                             format_id=choice['id'], directory=str(tmp_path)))
    assert seen['extractor_args'] == info['_clipdrop_extractor_args']
    assert result == dict(path='media.mp4', title='Test', ext='mp4')


def test_pot_provider_probe_pings_and_caches(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setattr(policy, 'POT_TARGET', ('127.0.0.1', 4416))
    calls = []

    class Response:
        status = 200
        def read(self, _size=None): return b'{"server_uptime":1.5,"version":"2.0.0"}'
        def __enter__(self): return self
        def __exit__(self, *_exc): return False

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(diagnostics.urllib.request, 'urlopen', fake_urlopen)
    diagnostics._pot.update(checked=0.0, alive=False)
    assert diagnostics.pot_provider_alive(now=100.0) is True
    assert calls == [('http://127.0.0.1:4416/ping', 2)]
    assert diagnostics.pot_provider_alive(now=105.0) is True
    assert len(calls) == 1  # cached, so Render health checks stay cheap
    assert diagnostics.pot_provider_alive(now=200.0) is True
    assert len(calls) == 2


def test_pot_provider_probe_reports_dead_or_missing_provider(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setattr(policy, 'POT_TARGET', ('127.0.0.1', 4416))

    def refused(*_args, **_kwargs):
        raise OSError('connection refused')

    monkeypatch.setattr(diagnostics.urllib.request, 'urlopen', refused)
    diagnostics._pot.update(checked=0.0, alive=True)
    assert diagnostics.pot_provider_alive(now=100.0) is False
    wrong = type('Response', (), {'status': 200, 'read': lambda self, _size=None: b'<html>',
                                  '__enter__': lambda self: self, '__exit__': lambda self, *a: False})()
    monkeypatch.setattr(diagnostics.urllib.request, 'urlopen', lambda *a, **k: wrong)
    assert diagnostics.pot_provider_alive(now=200.0) is False
    monkeypatch.setattr(policy, 'POT_TARGET', None)
    assert diagnostics.pot_provider_alive(now=300.0) is False


def test_memory_usage_reads_cgroup_v2(tmp_path):
    (tmp_path / 'memory.current').write_text(str(300 * 1024**2))
    (tmp_path / 'memory.max').write_text(str(512 * 1024**2))
    (tmp_path / 'memory.events').write_text('low 0\nhigh 3\nmax 0\noom 2\noom_kill 1\n')
    assert diagnostics.memory_usage(root=tmp_path) == (300, 512, 1)
    (tmp_path / 'memory.max').write_text('max')
    assert diagnostics.memory_usage(root=tmp_path) == (300, None, 1)
    assert diagnostics.memory_usage(root=tmp_path / 'absent') == (None, None, None)


def test_health_reports_pot_and_memory_state(public_client, monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://127.0.0.1:4416')
    monkeypatch.setattr(main, 'pot_provider_alive', lambda: True)
    monkeypatch.setattr(main, 'memory_usage', lambda: (300, 512, 1))
    body = public_client.get('/api/health').json()
    assert body['pot'] is True and body['pot_alive'] is True
    assert (body['memory_mb'], body['memory_limit_mb'], body['oom_kills']) == (300, 512, 1)
    monkeypatch.setattr(policy, 'POT_URL', '')
    monkeypatch.setattr(main, 'pot_provider_alive', lambda: False)
    body = public_client.get('/api/health').json()
    assert body['pot'] is False and body['pot_alive'] is False


def test_check_configuration_rejects_remote_pot_url(monkeypatch):
    monkeypatch.setattr(policy, 'POT_URL', 'http://pot.example:4416')
    monkeypatch.setattr(policy, 'POT_TARGET', None)
    with pytest.raises(RuntimeError, match='loopback'):
        policy.check_configuration()
