const $ = (id) => document.getElementById(id);
let metadata = null, kind = 'video', activeJob = null, polling = false;
let retentionMinutes = 60;
const bytes = (n) => n ? (n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`) : 'Chưa xác định';
const duration = (s) => { if (!s) return ''; s = Math.round(s); return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2,'0')}`; };
function showError(message) { $('error').textContent = message; $('error').hidden = false; }
async function api(url, data) {
  const headers = {'Content-Type':'application/json'};
  const ownerKey = localStorage.getItem('clipdrop-owner');
  if (ownerKey) headers['X-Owner-Token'] = ownerKey;
  const response = await fetch(url, data ? {method:'POST',headers,body:JSON.stringify(data)} : {headers: ownerKey ? {'X-Owner-Token': ownerKey} : {}});
  const body = await response.json();
  if (!response.ok) { const error = new Error(typeof body.detail === 'string' ? body.detail : 'Dữ liệu không hợp lệ. Hãy kiểm tra lại.'); error.status = response.status; throw error; }
  return body;
}
function setBusy(value) { $('inspect-button').disabled = value; $('url').disabled = value; $('download-button').disabled = value; $('format').disabled = value; $('video-tab').disabled = value; $('audio-tab').disabled = value; }
function updateFormat() {
  const choice = metadata?.formats.find(f => f.id === $('format').value);
  $('size').textContent = bytes(choice?.size);
  $('format-note').textContent = choice?.needs_ffmpeg ? 'Ghép hoặc chuyển đổi bằng FFmpeg. Dung lượng thực tế có thể khác ước tính.' : 'Giữ nguyên định dạng từ nguồn. Dung lượng thực tế có thể khác ước tính.';
  $('download-button').disabled = !choice;
}
function selectKind(next) {
  kind = next;
  $('video-tab').setAttribute('aria-pressed', String(kind === 'video'));
  $('audio-tab').setAttribute('aria-pressed', String(kind === 'audio'));
  $('format').replaceChildren();
  const formats = metadata.formats.filter(f => f.kind === kind);
  for (const f of formats) { const option = new Option(`${f.ext.toUpperCase()} · ${f.label}`, f.id); $('format').add(option); }
  if (!formats.length) $('format').add(new Option('Không có định dạng phù hợp', ''));
  updateFormat();
}
$('video-tab').addEventListener('click', () => selectKind('video'));
$('audio-tab').addEventListener('click', () => selectKind('audio'));
$('format').addEventListener('change', updateFormat);
$('thumbnail').addEventListener('error', () => { $('thumbnail').hidden = true; $('thumbnail-placeholder').hidden = false; });
$('inspect-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (activeJob) return;
  $('error').hidden = true; $('job').hidden = true; $('result').hidden = true; $('empty').hidden = true; $('loading').hidden = false;
  metadata = null; setBusy(true);
  try {
    metadata = await api('/api/inspect', {url:$('url').value.trim()});
    $('video-title').textContent = metadata.title; $('uploader').textContent = metadata.uploader;
    $('source').textContent = metadata.source; $('duration').textContent = duration(metadata.duration); $('duration').hidden = !metadata.duration;
    $('thumbnail').hidden = !metadata.thumbnail; $('thumbnail-placeholder').hidden = !!metadata.thumbnail;
    if (metadata.thumbnail) $('thumbnail').src = metadata.thumbnail; else $('thumbnail').removeAttribute('src');
    $('result').hidden = false;
  } catch (error) { showError(error.message || 'Không kết nối được backend.'); $('empty').hidden = false; }
  finally { $('loading').hidden = true; setBusy(false); if (metadata) selectKind(metadata.formats.some(f => f.kind === 'video') ? 'video' : 'audio'); }
});
$('download-button').addEventListener('click', async () => {
  if (!metadata || activeJob) return;
  setBusy(true); $('error').hidden = true; $('save-file').hidden = true; $('retry-status').hidden = true;
  $('job').hidden = false; $('job-label').textContent = 'Đang chuẩn bị…'; $('percent').textContent = ''; $('job-detail').textContent = 'Bạn có thể giữ trang này mở trong khi video được xử lý.'; $('progress').removeAttribute('value');
  try {
    const job = await api('/api/downloads', {inspection_id:metadata.inspection_id,format_id:$('format').value});
    activeJob = job.job_id; sessionStorage.setItem('clipdrop-job', activeJob); poll();
  } catch (error) { showError(error.message); $('job').hidden = true; setBusy(false); }
});
function finishJob() { activeJob = null; sessionStorage.removeItem('clipdrop-job'); setBusy(false); if (!metadata) $('download-button').disabled = true; }
async function poll() {
  if (polling || !activeJob) return;
  polling = true; $('retry-status').hidden = true;
  let failures = 0;
  while (activeJob) {
    try {
      const job = await api(`/api/downloads/${activeJob}`); failures = 0;
      if (job.status === 'error') { showError(job.error); $('job-label').textContent = 'Lượt tải chưa hoàn tất'; $('progress').value = 0; finishJob(); break; }
      if (job.status === 'ready') {
        $('job-label').textContent = 'File đã sẵn sàng!'; $('percent').textContent = '100%'; $('progress').value = 100;
        $('job-detail').textContent = `Bấm bên dưới để lưu về máy. File tạm được giữ ${retentionMinutes} phút; khởi động lại máy chủ sẽ xóa phiên.`;
        $('save-file').href = job.download_url; $('save-file').hidden = false; finishJob(); break;
      }
      if (job.status === 'queued') {
        $('job-label').textContent = `Đang chờ đến lượt · vị trí ${job.position || 1}`;
        $('percent').textContent = ''; $('progress').removeAttribute('value');
        $('job-detail').textContent = 'Máy chủ xử lý lần lượt để mọi người có thể dùng chung. Bạn có thể giữ trang này mở.';
        await new Promise(resolve => setTimeout(resolve, 2500));
        continue;
      }
      const processing = job.status === 'processing';
      $('job-label').textContent = processing ? 'Đang ghép / hoàn tất file…' : 'Đang tải từ nguồn…';
      $('percent').textContent = !processing && job.percent != null ? `${job.percent}%` : '';
      if (!processing && job.percent != null) $('progress').value = job.percent; else $('progress').removeAttribute('value');
      $('job-detail').textContent = processing ? 'FFmpeg có thể cần thêm một chút thời gian.' : [job.downloaded ? bytes(job.downloaded) : 'Đang kết nối nguồn', job.speed ? `${bytes(job.speed)}/s` : '', job.eta ? `Còn khoảng ${Math.ceil(job.eta)} giây` : '', 'Tiến độ tính theo từng luồng video / audio.'].filter(Boolean).join(' · ');
    } catch (error) {
      if ([404,410].includes(error.status)) { showError(error.message); $('job').hidden = true; finishJob(); break; }
      if (++failures >= 3) { showError('Không đọc được tiến độ. Backend có thể đã dừng hoặc phiên đã hết hạn.'); $('retry-status').hidden = false; $('job-detail').textContent = 'Thử kiểm tra lại; nếu đã khởi động lại backend, hãy tải lại trang.'; break; }
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  polling = false;
}
$('retry-status').addEventListener('click', poll);
const ownerInput = $('owner-key');
if (ownerInput) {
  ownerInput.value = localStorage.getItem('clipdrop-owner') || '';
  const saveOwner = () => {
    const v = ownerInput.value.trim();
    if (v) localStorage.setItem('clipdrop-owner', v); else localStorage.removeItem('clipdrop-owner');
  };
  ownerInput.addEventListener('change', saveOwner);
}
async function init() {
  try {
    const health = await api('/api/health');
    retentionMinutes = health.ttl_minutes || 60;
    $('health').textContent = `yt-dlp ${health.yt_dlp} · FFmpeg ${health.ffmpeg ? 'sẵn sàng' : 'chưa cài'}`;
    const notes = [];
    if (health.public) {
      $('mode-label').textContent = 'Bản dùng thử miễn phí';
      $('source-hint').textContent = 'TikTok · YouTube · Facebook. Chỉ video công khai, không cần đăng nhập.';
      $('storage-hint').textContent = `File tạm được dọn sau ${retentionMinutes} phút.`;
      $('health').textContent = 'Bản miễn phí · Có giới hạn sử dụng';
      notes.push(`Tối đa ${health.max_duration_minutes} phút/video, ${health.max_file_mb} MB/file và 3 lượt tải mỗi giờ cho một phiên. Máy chủ xử lý lần lượt; hạn mức gửi file dùng chung là 100 MB/ngày.`);
    }
    if (!health.ffmpeg) notes.push('Chưa có FFmpeg: chỉ hiện định dạng tải trực tiếp. Cài FFmpeg để ghép video chất lượng cao và tạo MP3.');
    if (!health.js_runtime) notes.push('Chưa có runtime JavaScript phù hợp: một số nguồn như YouTube có thể thiếu định dạng. Xem hướng dẫn cài Deno trong README.');
    if (notes.length) { $('notice').textContent = notes.join(' '); $('notice').hidden = false; }
  } catch { $('health').textContent = 'Backend chưa kết nối'; showError('Không kết nối được backend. Hãy kiểm tra ứng dụng đã chạy.'); }
  activeJob = sessionStorage.getItem('clipdrop-job');
  if (activeJob && /^[a-f0-9]{32}$/.test(activeJob)) { setBusy(true); $('job').hidden = false; poll(); } else activeJob = null;
}
init();
