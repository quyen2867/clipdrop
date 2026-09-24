# Clipdrop

Web app tiếng Việt để dán link, xem thông tin video, chọn chất lượng/định dạng và tải file về máy. FastAPI + HTML/CSS/JavaScript thuần, yt-dlp làm backend; FFmpeg ghép video/audio và xuất MP3. Không cần Node, database hay API key.

## Đưa bản thử nghiệm lên Render miễn phí

Xem [hướng dẫn triển khai Render](DEPLOY_RENDER.md). Repo đã có `Dockerfile` và `render.yaml` cấu hình **một Web Service Free**, không tạo database, ổ đĩa trả phí hay worker trả phí. Mã nguồn có thể để private; website sau triển khai vẫn public.

Bản public là **demo có hạn mức**, không phải dịch vụ tải video không giới hạn cho số đông. Chế độ local vẫn hoạt động như bên dưới.

## Chạy local trên Mac

Cần Python 3.10 trở lên. Mở Terminal trong thư mục `clipdrop`.

```sh
# Nếu đã có Homebrew; xem https://brew.sh nếu chưa cài
brew install python ffmpeg deno

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Mở **http://127.0.0.1:8765**. Dừng bằng `Ctrl+C` trong Terminal. Những lần sau có thể chạy `./run.command` (hoặc mở file này từ Finder sau khi đã cài `.venv`). Nếu cổng đang bận, dùng `--port 8766` rồi mở địa chỉ tương ứng. Chỉ chạy **một worker**, không dùng `--workers` vì trạng thái lưu trong bộ nhớ.

FFmpeg **và ffprobe** cần nằm trong `PATH`. Nếu thiếu, app vẫn hỗ trợ định dạng có sẵn audio/video chung một file và audio gốc; tự ẩn lựa chọn cần ghép hoặc chuyển đổi. Deno ≥ 2.3 cùng gói `yt-dlp[default]` cung cấp runtime/component JavaScript cho những nguồn như YouTube. App tự dùng Node ≥ 22 nếu máy đã có Node và thiếu Deno phù hợp; không bắt buộc cài cả hai. App không tự tải script runtime từ bên ngoài. Cài runtime xong thì khởi động lại app.

## Sử dụng

1. Dán link của một video công khai, bấm **Xem thông tin**.
2. Xem tiêu đề, tác giả, thumbnail và thời lượng khi nguồn cung cấp.
3. Chọn **Video** hoặc **Âm thanh**, chọn chất lượng và định dạng.
4. Bấm **Chuẩn bị tải xuống**, đợi tải/ghép xong rồi bấm **Tải file về máy**.

Chất lượng lấy từ nguồn, không nâng độ phân giải. MP4/WebM/MKV tùy luồng thực tế; audio có định dạng gốc và MP3 192 kbps nếu có FFmpeg. Chuyển sang MP3 không cải thiện chất lượng của âm thanh nguồn. MP4 không đồng nghĩa mọi codec đều phát được trong QuickTime; lựa chọn AVC1 thường tương thích rộng hơn. Dung lượng có thể chỉ là ước tính hoặc chưa xác định. Tiến độ tính riêng cho từng luồng nên có thể bắt đầu lại khi chuyển từ video sang audio.

Bạn có thể thử bằng video do bạn sở hữu/được phép tải hoặc video mẫu công khai, chẳng hạn [Sintel trailer trên máy chủ Blender](https://download.blender.org/durian/trailer/sintel_trailer-480p.mp4). Link MP4 trực tiếp đôi khi không cung cấp thumbnail, tác giả, độ phân giải hoặc danh sách nhiều chất lượng.

## Phạm vi và giới hạn ở chế độ local

- Chỉ dành cho cá nhân chạy local trên `127.0.0.1`. Backend truy cập Internet trực tiếp; video nằm tạm ở `.data/<job-id>/` trước khi trình duyệt lưu bản riêng.
- Không nhận cookie, mật khẩu, tài khoản, proxy hay tham số yt-dlp tùy ý. Không có cơ chế né DRM, đăng nhập, paywall hay giới hạn địa lý. Loại định dạng có `has_drm`, từ chối nguồn được báo là private/premium/restricted; yt-dlp cũng tắt `allow_unplayable_formats`. Khả năng phát hiện bảo vệ phụ thuộc metadata của nguồn/yt-dlp, không phải chứng nhận mọi video đều được phép tải.
- Chỉ một video mỗi link. Link video kèm playlist xử lý video đó; link playlist thuần, livestream và video đang xử lý bị từ chối.
- Tối đa 2 lượt tải và 2 lượt xem thông tin cùng lúc; tối đa 100 phiên thông tin và 100 lượt tải được giữ. Xem thông tin timeout 90 giây, tải/ghép timeout 30 phút. Timeout dừng cả tiến trình con FFmpeg.
- Giới hạn 2 GiB/file và khoảng 6 GiB dữ liệu tạm/lượt. Ngoài kiểm tra size do nguồn báo, backend theo dõi file thực tế mỗi giây và dừng tiến trình vượt giới hạn. Có thể vượt nhẹ giữa hai lần kiểm tra; giữ đủ dung lượng trống.
- Thông tin hết hạn sau một giờ. File tạm của lượt hoàn tất/lỗi tự dọn khoảng một giờ sau hoàn tất, quét mỗi phút khi app chạy. File tải về thư mục Downloads của trình duyệt được giữ nguyên. Khởi động lại mất danh sách phiên; file mồ côi được dọn khi đủ tuổi. Không đóng backend giữa lúc tải.
- Host/origin được kiểm tra, không bật CORS; link đầu vào chỉ HTTP/HTTPS công khai, cổng 80/443, không credential/mạng nội bộ. Đây **không phải sandbox SSRF hoàn chỉnh**: redirect, URL nhúng và DNS thay đổi bên trong extractor chưa được kiểm soát ở tầng mạng. Không mở app ra Internet/LAN hay dùng reverse proxy công khai. Để triển khai nhiều người dùng cần authentication, egress firewall, queue, quota và kho trạng thái bền vững.
- Không bảo đảm mọi website/video luôn hoạt động. Khi nguồn chặn bot, yêu cầu đăng nhập hoặc có bảo vệ, app báo lỗi và dừng.

## Cập nhật và xử lý lỗi

```sh
source .venv/bin/activate
python -m pip install --upgrade 'yt-dlp[default]'
ffmpeg -version
ffprobe -version
deno --version
```

Khởi động lại sau cập nhật. Nếu nhận “định dạng đã thay đổi”, xem thông tin video lại. Nếu phiên hết hạn hoặc backend vừa khởi động lại, tải lại trang và nhập link lại. `requirements-lock.txt` ghim các gói Python, bao gồm công cụ test; Docker dùng Python 3.12. Khi cập nhật yt-dlp cho bản Docker, cập nhật cả lock và yt-dlp-ejs tương ứng rồi kiểm thử lại.

## Cấu trúc

```text
app/main.py          API, giới hạn tác vụ, file tải và dọn file tạm
app/media.py         Kiểm tra URL, lọc DRM, tạo lựa chọn định dạng
app/worker.py        Tiến trình yt-dlp/FFmpeg độc lập có timeout
app/policy.py        Hạn mức public, cookie phiên và kiểm tra socket
app/static/          Giao diện HTML/CSS/JavaScript
tests/test_app.py    Kiểm thử API và chính sách xử lý
tests/test_public.py Kiểm thử bản public, quyền xem file và chặn mạng nội bộ
deploy/             Khởi động container và FFmpeg chỉ đọc file local
Dockerfile          Python + Node + FFmpeg, chạy non-root
render.yaml         Blueprint một dịch vụ Free
run.command          Lệnh khởi động trên Mac
```

API: `GET /api/health`, `POST /api/inspect` với `{ "url": "..." }`, `POST /api/downloads` với `{ "inspection_id": "...", "format_id": "..." }`, `GET /api/downloads/{job_id}`, `GET /api/downloads/{job_id}/file`. Client chỉ gửi ID định dạng do server cung cấp, không gửi selector hay đường dẫn file.

## Kiểm thử

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Test tự động dùng metadata/worker giả để kiểm tra DRM, giới hạn truy cập, thiếu FFmpeg, ID định dạng, download attachment, expiry, quota, timeout và host/origin mà không phụ thuộc mạng. Test này không thay thế kiểm tra tải thực tế từ từng website.

Đã kiểm tra trên Mac: 48 test tự động, bao gồm chặn kết nối mạng nội bộ sau DNS, cookie và phân tách file giữa hai phiên, quota gửi file đồng thời và hàng đợi. Bản local trước đó đã tải thật Sintel trailer thành MP4 (H.264 + AAC) và MP3, ghép hai luồng bằng FFmpeg, kiểm tra giao diện tại chiều rộng 319 px và 1280 px. Khả năng tải từng nguồn vẫn tùy điều kiện truy cập hiện tại.

Tài liệu nguồn: [yt-dlp và Python embedding](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp), [JavaScript runtime của yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [FastAPI](https://fastapi.tiangolo.com/), [FFmpeg](https://ffmpeg.org/).
