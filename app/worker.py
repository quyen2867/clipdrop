"""Isolated process: parent enforces a hard timeout for extraction/download."""
import json
import sys
from pathlib import Path

import yt_dlp
from . import policy
from .media import MediaError, choices_for, describe, extract, ffmpeg_available, options, user_error, guard


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def run(request):
    info = extract(request['url'])
    if request['action'] == 'inspect':
        return describe(info, ffmpeg_available())
    directory = Path(request['directory'])
    choice = next((c for c in choices_for(info, ffmpeg_available()) if c['id'] == request['format_id']), None)
    if not choice:
        raise MediaError('Định dạng đã thay đổi. Hãy xem thông tin video lại và chọn chất lượng mới.')
    progress = directory / 'progress.json'

    def hook(event):
        total = event.get('total_bytes') or event.get('total_bytes_estimate')
        downloaded = event.get('downloaded_bytes', 0)
        if downloaded > policy.MAX_FILE_BYTES:
            raise MediaError('File vượt giới hạn dung lượng. Hãy chọn chất lượng thấp hơn.')
        atomic_json(progress, dict(status='processing' if event['status'] == 'finished' else 'downloading',
                                  percent=min(99, round(downloaded / total * 100, 1)) if total else None,
                                  downloaded=downloaded, total=total, speed=event.get('speed'), eta=event.get('eta')))

    def selector(ctx):
        selected = []
        for fid in choice['_ids']:
            item = next((f for f in ctx['formats'] if str(f['format_id']) == fid and not f.get('has_drm')), None)
            if not item:
                raise MediaError('Định dạng không còn khả dụng hoặc có DRM.')
            selected.append(item)
        if len(selected) == 1:
            yield selected[0]
        else:
            yield dict(format_id='+'.join(choice['_ids']), ext=choice['ext'],
                       requested_formats=selected, protocol='+'.join(f['protocol'] for f in selected))

    def match_filter(metadata, *, incomplete=False):
        guard(metadata)
        return None

    config = options()
    config.update(format=selector, outtmpl=str(directory / 'media.%(ext)s'),
                  merge_output_format=choice['ext'], progress_hooks=[hook], match_filter=match_filter,
                  max_filesize=policy.MAX_FILE_BYTES, overwrites=False)
    if choice['_convert']:
        config['postprocessors'] = [dict(key='FFmpegExtractAudio', preferredcodec='mp3', preferredquality='192')]
    with yt_dlp.YoutubeDL(config) as ydl:
        ydl.extract_info(request['url'], download=True)
    candidates = [p for p in directory.glob('media.*') if p.suffix[1:] == choice['ext']]
    if len(candidates) != 1 or not candidates[0].stat().st_size:
        raise MediaError('Không tạo được file hoàn chỉnh. Hãy thử định dạng khác.')
    if candidates[0].stat().st_size > policy.MAX_FILE_BYTES:
        raise MediaError('File vượt giới hạn dung lượng. Hãy chọn chất lượng thấp hơn.')
    return dict(path=candidates[0].name, title=info.get('title') or 'video', ext=choice['ext'])


if __name__ == '__main__':
    policy.install_network_guard()
    if policy.PUBLIC:
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (policy.MAX_FILE_BYTES, policy.MAX_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    try:
        result = dict(ok=True, data=run(json.loads(sys.stdin.read())))
    except Exception as exc:
        print(f"worker raw error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        result = dict(ok=False, error=user_error(exc))
    # yt-dlp/ffmpeg may print other output, so parent reads the final JSON line.
    print(json.dumps(result, ensure_ascii=False), flush=True)
