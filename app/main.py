import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from yt_dlp.version import __version__ as yt_version

from .media import ffmpeg_available, js_runtimes
from . import policy

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv('CLIPDROP_DATA', str(ROOT / '.data')))
DATA.mkdir(parents=True, exist_ok=True)
TTL = policy.TTL
MAX_ITEMS = 100
lock = threading.RLock()
inspections = {}
jobs = {}
pool = ThreadPoolExecutor(max_workers=policy.CONCURRENCY)
inspect_slots = threading.BoundedSemaphore(1 if policy.PUBLIC else 2)


def file_sizes(directory):
    sizes = []
    for path in directory.rglob('*'):
        try:
            if path.is_file():
                sizes.append(path.stat().st_size)
        except FileNotFoundError:
            pass  # yt-dlp atomically renames partial downloads while we scan
    return sizes


def cleanup():
    now = time.time()
    with lock:
        for key, item in list(inspections.items()):
            if now - item['created'] > TTL:
                inspections.pop(key, None)
        for key, job in list(jobs.items()):
            if job['status'] in ('ready', 'error') and now - job['updated'] > TTL:
                shutil.rmtree(DATA / key, ignore_errors=True)
                jobs.pop(key, None)
        for directory in DATA.iterdir():
            if directory.is_dir() and directory.name not in jobs and now - directory.stat().st_mtime > TTL:
                shutil.rmtree(directory, ignore_errors=True)


@asynccontextmanager
async def lifespan(_app):
    policy.check_configuration()
    async def reaper():
        while True:
            cleanup()
            await asyncio.sleep(60)
    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Clipdrop', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'] + ([policy.PUBLIC_HOST] if policy.PUBLIC_HOST else []))


@app.middleware('http')
async def local_requests(request: Request, call_next):
    owner, cookie = policy.session_cookie(request.cookies.get(policy.COOKIE))
    request.state.owner = owner
    request.state.owner_bypass = policy.owner_authorized(request.headers.get('x-owner-token'))
    if request.method == 'POST':
        origin = request.headers.get('origin')
        expected_origin = policy.ORIGIN if policy.PUBLIC else f'{request.url.scheme}://{request.headers.get("host")}'
        if origin and origin != expected_origin:
            return JSONResponse({'detail': 'Yêu cầu khác nguồn bị từ chối.'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Yêu cầu khác nguồn bị từ chối.'}, status_code=403)
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'detail': 'Chỉ nhận JSON.'}, status_code=415)
        try:
            length = int(request.headers.get('content-length', '-1'))
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            return JSONResponse({'detail': 'Yêu cầu phải có độ dài hợp lệ, tối đa 4 KB.'}, status_code=413)
        if policy.PUBLIC and not request.state.owner_bypass:
            # Global limits cannot be bypassed by deleting cookies or spoofing IPs.
            # No client-supplied forwarded IP header is trusted for quota decisions.
            if not policy.rate_allowed('global-post', 20, 60) or not policy.rate_allowed(('post', owner), 12, 60):
                return JSONResponse({'detail': 'Có quá nhiều yêu cầu. Vui lòng thử lại sau một phút.'}, status_code=429)
    response = await call_next(request)
    if cookie != request.cookies.get(policy.COOKIE):
        response.set_cookie(policy.COOKIE, cookie, max_age=86400, httponly=True, secure=policy.PUBLIC, samesite='lax')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    if request.url.path == '/':
        response.headers['Cache-Control'] = 'no-store'
    if policy.PUBLIC:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


class InspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadRequest(BaseModel):
    inspection_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    format_id: str = Field(pattern=r'^[a-f0-9]{24}$')


def worker(payload, timeout):
    proc = subprocess.Popen([sys.executable, '-m', 'app.worker'], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True,
                            env={k: v for k, v in os.environ.items() if k != 'CLIPDROP_SESSION_SECRET'})
    try:
        directory = Path(payload['directory']) if 'directory' in payload else None
        deadline = time.monotonic() + timeout
        message = json.dumps(payload)
        stderr = ''
        while True:
            if directory:
                sizes = file_sizes(directory)
                if sum(sizes) > policy.MAX_JOB_BYTES or any(size > policy.MAX_FILE_BYTES for size in sizes):
                    raise RuntimeError('Video vượt giới hạn dung lượng của máy chủ. Hãy chọn chất lượng thấp hơn.')
                if shutil.disk_usage(DATA).free < 256 * 1024**2:
                    raise RuntimeError('Máy chủ tạm hết dung lượng. Hãy thử lại sau.')
                if policy.PUBLIC and sum(file_sizes(DATA)) > policy.MAX_STORAGE_BYTES:
                    raise RuntimeError('Kho file tạm đã đầy. Hãy thử lại sau khi máy chủ dọn file.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('worker', timeout)
            try:
                stdout, stderr = proc.communicate(message, timeout=min(1, remaining) if directory else remaining)
                break
            except subprocess.TimeoutExpired:
                message = None
                if not directory:
                    raise
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise RuntimeError('Quá thời gian xử lý. Hãy thử video ngắn hơn hoặc thử lại sau.')
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('Tiến trình xử lý đã dừng. Hãy thử lại.')
    if not result['ok']:
        if stderr and stderr.strip():
            print(f"worker raw error: {stderr.strip()[-2000:]}", file=sys.stderr, flush=True)
        raise RuntimeError(result['error'])
    return result['data']


@app.get('/api/health')
def health():
    return dict(ffmpeg=ffmpeg_available(), yt_dlp=yt_version, js_runtime=next(iter(js_runtimes()), None),
                pot=bool(policy.POT_URL),
                public=policy.PUBLIC, ttl_minutes=TTL // 60, max_file_mb=policy.MAX_FILE_BYTES // 1024**2,
                max_duration_minutes=policy.MAX_DURATION // 60 if policy.MAX_DURATION else None)


@app.post('/api/inspect')
def inspect_video(body: InspectRequest, request: Request):
    cleanup()
    if policy.PUBLIC and not request.state.owner_bypass and not policy.rate_allowed('inspect-hour', 60, 3600):
        raise HTTPException(429, 'Bản miễn phí đã đạt giới hạn xem thông tin trong giờ này. Vui lòng quay lại sau.')
    if not inspect_slots.acquire(blocking=False):
        raise HTTPException(429, 'Đang xem thông tin video khác. Hãy thử lại sau vài giây.')
    try:
        data = worker(dict(action='inspect', url=body.url.strip()), 90)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    finally:
        inspect_slots.release()
    token = uuid.uuid4().hex
    with lock:
        if len(inspections) >= MAX_ITEMS:
            inspections.pop(next(iter(inspections)))
        inspections[token] = dict(url=body.url.strip(), data=data, created=time.time(), owner=request.state.owner)
    return dict(inspection_id=token, **data)


def download_job(job_id, payload):
    directory = DATA / job_id
    with lock:
        jobs[job_id].update(status='downloading', updated=time.time())
    try:
        directory.mkdir()
        if policy.PUBLIC and sum(file_sizes(DATA)) + policy.MAX_JOB_BYTES > policy.MAX_STORAGE_BYTES:
            raise RuntimeError('Kho file tạm đã đầy. Vui lòng thử lại sau ít phút.')
        result = worker(dict(action='download', directory=str(directory), **payload), 600 if policy.PUBLIC else 1800)
        with lock:
            jobs[job_id].update(status='ready', result=result, updated=time.time())
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        with lock:
            jobs[job_id].update(status='error', error=str(exc), updated=time.time())


@app.post('/api/downloads', status_code=202)
def start_download(body: DownloadRequest, request: Request):
    cleanup()
    with lock:
        item = inspections.get(body.inspection_id)
        if not item or item['owner'] != request.state.owner:
            raise HTTPException(410, 'Thông tin đã hết hạn. Hãy xem thông tin video lại.')
        if body.format_id not in [f['id'] for f in item['data']['formats']]:
            raise HTTPException(400, 'Định dạng không hợp lệ.')
        if sum(j['status'] in ('queued', 'downloading') for j in jobs.values()) >= policy.QUEUE_SIZE:
            raise HTTPException(429, 'Hàng đợi đã đầy. Hãy chờ một lượt tải hoàn tất.')
        if policy.PUBLIC:
            if any(j.get('owner') == request.state.owner and j['status'] in ('queued', 'downloading') for j in jobs.values()):
                raise HTTPException(429, 'Bạn đang có một lượt tải. Hãy chờ hoàn tất trước khi tải tiếp.')
            used = sum(file_sizes(DATA))
            if used + policy.MAX_JOB_BYTES > policy.MAX_STORAGE_BYTES or shutil.disk_usage(DATA).free < policy.MAX_JOB_BYTES + 256 * 1024**2:
                raise HTTPException(503, 'Máy chủ đang thiếu dung lượng. File tạm sẽ được dọn tự động; hãy thử lại sau.')
            if not request.state.owner_bypass and (not policy.rate_allowed(('downloads', request.state.owner), 3, 3600) or not policy.rate_allowed('downloads-hour', 12, 3600)):
                raise HTTPException(429, 'Đã đạt giới hạn tải trong giờ này. Vui lòng quay lại sau.')
        if len(jobs) >= MAX_ITEMS:
            raise HTTPException(429, 'Đã đạt giới hạn phiên. File tạm tự xóa sau một giờ.')
        job_id = uuid.uuid4().hex
        jobs[job_id] = dict(status='queued', updated=time.time(), owner=request.state.owner)
        pool.submit(download_job, job_id, dict(url=item['url'], format_id=body.format_id))
    return dict(job_id=job_id)


def get_job(job_id, owner):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id) or job_id not in jobs or jobs[job_id].get('owner') != owner:
        raise HTTPException(404, 'Không tìm thấy lượt tải hoặc file đã hết hạn.')
    return jobs[job_id]


@app.get('/api/downloads/{job_id}')
def status(job_id: str, request: Request):
    with lock:
        job = dict(get_job(job_id, request.state.owner))
        position = [k for k, j in jobs.items() if j['status'] == 'queued']
    result = dict(status=job['status'])
    if job['status'] == 'queued':
        result['position'] = position.index(job_id) + 1 if job_id in position else 1
    if job['status'] == 'downloading':
        try:
            result.update(json.loads((DATA / job_id / 'progress.json').read_text()))
        except (OSError, ValueError):
            pass
    elif job['status'] == 'ready':
        result.update(percent=100, download_url=f'/api/downloads/{job_id}/file')
    elif job['status'] == 'error':
        result['error'] = job['error']
    return result


@app.get('/api/downloads/{job_id}/file')
def download_file(job_id: str, request: Request):
    with lock:
        job = dict(get_job(job_id, request.state.owner))
    if job['status'] != 'ready':
        raise HTTPException(409, 'File chưa sẵn sàng.')
    path = DATA / job_id / job['result']['path']
    if not path.is_file():
        raise HTTPException(410, 'File đã hết hạn. Hãy tạo lượt tải mới.')
    if policy.PUBLIC and not request.state.owner_bypass and not policy.reserve_transfer(path.stat().st_size):
        raise HTTPException(429, 'Bản thử nghiệm đã đạt giới hạn gửi file hôm nay. Vui lòng quay lại sau.')
    title = re.sub(r'[^\w\s.()\-]', '', job['result']['title'], flags=re.UNICODE).strip()[:120] or 'video'
    return FileResponse(path, filename=f"{title}.{job['result']['ext']}", media_type='application/octet-stream')


@app.get('/')
def index():
    return FileResponse(ROOT / 'app/static/index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')
