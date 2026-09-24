import copy
import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from app import main, media


@pytest.fixture
def info():
    def fmt(fid, ext, video, audio, **extra):
        return dict(format_id=fid, ext=ext, vcodec=video, acodec=audio,
                    url='https://example.com/media', protocol='https', **extra)
    return dict(title='<script>title</script>', availability='public', formats=[
        fmt('combined', 'mp4', 'avc1', 'aac', height=360, filesize=100),
        fmt('audio', 'm4a', 'none', 'aac', abr=128, filesize=20),
        fmt('hd', 'mp4', 'avc1', 'none', height=1080, filesize=500),
        fmt('drm', 'mp4', 'avc1', 'none', height=2160, has_drm=True),
    ])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    main.jobs.clear()
    main.inspections.clear()
    with TestClient(main.app, base_url='http://localhost') as client:
        yield client


def test_formats_have_audio_and_exclude_drm(info):
    choices = media.choices_for(info, True)
    hd = next(c for c in choices if c['height'] == 1080)
    assert hd['_ids'] == ['hd', 'audio']
    assert hd['size'] == 520
    assert all('drm' not in c['_ids'] for c in choices)
    assert any(c['ext'] == 'mp3' for c in choices)


def test_no_ffmpeg_hides_merge_and_conversion(info):
    choices = media.choices_for(info, False)
    assert all(not c['needs_ffmpeg'] for c in choices)
    assert [c['height'] for c in choices if c['kind'] == 'video'] == [360]


def test_direct_media_with_unknown_codecs_is_downloadable():
    info = dict(formats=[dict(format_id='mp4', ext='mp4', vcodec=None,
                              url='https://example.com/video.mp4', video_ext='mp4')])
    assert media.choices_for(info, False)[0]['ext'] == 'mp4'
    assert any(c['ext'] == 'mp3' for c in media.choices_for(info, True))


@pytest.mark.parametrize('change', [dict(has_drm=True), dict(availability='private'),
    dict(availability='needs_auth'), dict(is_live=True), dict(_type='playlist', entries=[])])
def test_protected_and_unsupported_rejected(info, change):
    info.update(change)
    with pytest.raises(media.MediaError):
        media.choices_for(info, True)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/a',
    'http://127.0.0.1/a', 'http://[::1]/a', 'http://user:pass@example.com/a',
    'http://example.com:8000/a'])
def test_reject_unsafe_urls(url, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(media.MediaError): media.validate_url(url)


def test_metadata_hides_selectors(info):
    data = media.describe(info, True)
    assert all('_ids' not in f for f in data['formats'])


def test_local_request_boundary(client):
    assert client.get('/', headers={'host': 'evil.example'}).status_code == 400
    assert client.post('/api/inspect', json={'url':'https://example.com'}, headers={'origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/inspect', content='x', headers={'content-type':'text/plain'}).status_code == 415


def test_complete_job_and_attachment(client, info, monkeypatch):
    data = media.describe(info, True)
    def fake_worker(payload, timeout):
        if payload['action'] == 'inspect': return data
        path = main.Path(payload['directory']) / 'media.mp4'
        path.write_bytes(b'test-media')
        return dict(path=path.name, ext='mp4', title='Video / thử')
    monkeypatch.setattr(main, 'worker', fake_worker)
    response = client.post('/api/inspect', json={'url':'https://example.com/video'}).json()
    started = client.post('/api/downloads', json={'inspection_id':response['inspection_id'], 'format_id':response['formats'][0]['id']})
    assert started.status_code == 202
    jid = started.json()['job_id']
    for _ in range(100):
        result = client.get(f'/api/downloads/{jid}').json()
        if result['status'] == 'ready': break
        time.sleep(.01)
    assert result['status'] == 'ready'
    file = client.get(result['download_url'])
    assert file.content == b'test-media'
    assert 'attachment;' in file.headers['content-disposition']
    assert 'nosniff' == file.headers['x-content-type-options']


def test_invalid_selection_expiry_and_limit(client, info, monkeypatch):
    monkeypatch.setattr(main, 'worker', lambda *a: media.describe(info, True))
    result = client.post('/api/inspect', json={'url':'https://example.com/video'}).json()
    payload = dict(inspection_id=result['inspection_id'], format_id='a'*24)
    assert client.post('/api/downloads', json=payload).status_code == 400
    payload['format_id'] = result['formats'][0]['id']
    main.jobs.update({str(i):dict(status='downloading', updated=time.time()) for i in range(2)})
    assert client.post('/api/downloads', json=payload).status_code == 429
    main.inspections[result['inspection_id']]['created'] = 0
    assert client.post('/api/downloads', json=payload).status_code == 410
    assert client.get('/api/downloads/' + 'b'*32 + '/file').status_code == 404


def test_cleanup_keeps_running_jobs(client):
    ready, active = 'a'*32, 'b'*32
    for key, state in [(ready, 'ready'), (active, 'downloading')]:
        (main.DATA / key).mkdir()
        main.jobs[key] = dict(status=state, updated=0)
    main.cleanup()
    assert not (main.DATA / ready).exists()
    assert (main.DATA / active).exists()


def test_timeout_kills_child_process_group(monkeypatch):
    class Process:
        pid = 123456
        calls = 0
        def communicate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1: raise main.subprocess.TimeoutExpired('worker', 1)
            return '', ''
    killed = []
    monkeypatch.setattr(main.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(main.os, 'killpg', lambda *a: killed.append(a))
    with pytest.raises(RuntimeError, match='Quá thời gian'):
        main.worker({}, 1)
    assert killed == [(123456, main.signal.SIGKILL)]
