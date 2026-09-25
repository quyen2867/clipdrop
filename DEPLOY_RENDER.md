# Triển khai Clipdrop trên Render Free

## Cấu hình có sẵn

`render.yaml` tạo **một Web Service với `plan: free`** ở Singapore. Docker cài Python, FFmpeg/ffprobe, Node cho yt-dlp, một PO token provider (bgutil) chạy loopback cho YouTube, và FFmpeg wrapper chỉ đọc file local. Không cần database, disk trả phí, cron hay worker riêng. Render cấp HTTPS và tên miền `*.onrender.com`.

Đây là bản thử nghiệm có hạn mức cho nhiều người ghé dùng lần lượt. Free chỉ có 0.1 CPU / 512 MB RAM theo tài liệu Render tại thời điểm chuẩn bị; không bảo đảm tải đồng thời nhiều người hoặc hoạt động ổn định 24/7.

## Các bước triển khai

1. Đăng nhập [Render Dashboard](https://dashboard.render.com/). Nếu phải tạo tài khoản/đồng ý điều khoản, chủ tài khoản tự hoàn tất.
2. Chọn **New → Blueprint**, kết nối GitHub và chọn repo `clipdrop` chứa mã nguồn này. Nếu Render cần quyền truy cập GitHub, chỉ cấp cho repo này.
3. Render đọc `render.yaml`. Kiểm tra danh sách chỉ có **Clipdrop – Web Service – Free** và chi phí **$0**. Không chọn database hoặc nâng cấp.
4. Bấm **Deploy Blueprint**. Render tự tạo `CLIPDROP_SESSION_SECRET`; không chia sẻ giá trị này.
5. Chờ trạng thái **Live**, mở URL do Render cấp. Kiểm tra `/api/health`: `public: true`, `ffmpeg: true`, `js_runtime: node`, `pot: true`, `max_file_mb: 50`.
6. Thử một video ngắn công khai mà bạn có quyền tải. Chỉ chia sẻ URL sau khi kiểm tra đọc thông tin và nhận file thực tế trên chính máy chủ Render.

Không nhập thẻ/phương thức thanh toán nếu mục tiêu là **không phát sinh phí**. Render có thể yêu cầu xác minh tài khoản tùy trường hợp; nếu không có lựa chọn miễn phí phù hợp, dừng tại đó. Khi không có phương thức thanh toán, tài liệu Render nói dịch vụ bị tạm dừng thay vì tính phí vượt hạn mức. Đã có phương thức thanh toán trong tài khoản thì không được xem cấu hình `plan: free` là bảo đảm không phát sinh phí băng thông.

## Hạn mức bản public

| Hạng mục | Giá trị mặc định |
|---|---|
| Nguồn đầu vào | TikTok, YouTube, Facebook, bao gồm link rút gọn chính thức |
| Nội dung | Công khai, không DRM/đăng nhập/trả phí; phải biết thời lượng |
| Thời lượng | Tối đa 10 phút/video |
| File | Tối đa 50 MiB/file |
| Xử lý đồng thời | 1 lượt tải + 1 lượt xem thông tin |
| Hàng chờ | Tối đa 8 lượt, tính cả lượt đang chạy |
| Mỗi phiên | 1 lượt đang chờ/chạy; 3 lượt tải/giờ |
| Toàn ứng dụng | 12 lượt tải/giờ; 60 lần xem thông tin/giờ |
| Gửi file cho người dùng | Tối đa 100 MiB/ngày, tính toàn bộ size mỗi lần GET/Range |
| File tạm | Dọn 10 phút sau hoàn tất; quét mỗi phút |
| Dung lượng tạm | 160 MiB/lượt, 400 MiB tổng; giám sát mỗi giây |
| Timeout | 90 giây đọc thông tin; 10 phút tải/ghép |

MB trên giao diện tính theo 1024² byte. Hạn mức gửi file rất thấp để làm bản thử nghiệm; nhiều người vẫn có thể mở trang nhưng không đồng nghĩa nhiều lượt tải mỗi ngày. Sửa hằng số trong `app/policy.py` nếu sau này đổi máy chủ/hạn mức. Không nâng hạn mức chỉ để né quota của nhà cung cấp.

**Hàng đợi, hạn mức, phiên và số liệu gửi file nằm trong bộ nhớ**: restart/redeploy/ngủ của Render có thể xóa chúng. Bộ đếm gửi file không bao gồm HTML/CSS/API, dữ liệu lấy từ nguồn và lưu lượng khác, không phải hạn mức thanh toán tổng. Cookie phiên có thể được làm mới nên quota mỗi phiên chỉ ngăn lạm dụng nhẹ; hạn mức toàn app vẫn áp dụng. Muốn vận hành quy mô lớn cần trạng thái bền vững, chống bot và quota ở tầng hạ tầng.

## Bảo vệ có sẵn

- Chỉ hostname chính xác của dịch vụ được chấp nhận; `RENDER_EXTERNAL_HOSTNAME` do Render cung cấp. POST kiểm tra Origin; không bật CORS; JSON tối đa 4 KB. Không tin header IP chuyển tiếp để quyết định quota.
- Cookie phiên ký HMAC, Secure/HttpOnly/SameSite. Thông tin video, trạng thái và file chỉ trả cho phiên tạo chúng. Đây là phiên ẩn danh, không phải đăng nhập tài khoản.
- Tiến trình yt-dlp kiểm tra **địa chỉ IP thực tế ở sự kiện socket.connect**, từ chối mạng nội bộ/link-local/multicast, IPv4-mapped/NAT64 phổ biến và cổng ngoài 80/443. Việc này cũng áp dụng redirect/URL nhúng qua Python transports. Thử nghiệm gồm DNS rebinding về loopback. Ngoại lệ duy nhất là đúng một cặp host:port loopback khai báo trong `CLIPDROP_POT_URL` (provider PO token đi kèm); để biến này trống thì không còn ngoại lệ nào.
- Docker dùng bản phụ thuộc ghim, không cài curl-cffi hoặc downloader mạng ngoài; HLS/DASH dùng downloader native. FFmpeg/ffprobe qua wrapper chỉ cho `file,pipe`; Node chỉ dùng solver đi kèm yt-dlp-ejs, không bật tải remote components. Plugin `bgutil-ytdlp-pot-provider` được ghim trong lock và lấy từ chính package cài sẵn, không tải script lúc chạy. Secret ký phiên không được truyền cho tiến trình tải.
- Chạy non-root, giới hạn kích thước file bằng OS trong worker public, theo dõi đĩa, dừng nhóm tiến trình khi timeout. Không nhận đường dẫn, selector, cookie nguồn hay tùy chọn dòng lệnh từ client.

Các lớp này giảm rủi ro của bản demo, **không thay thế firewall egress/container sandbox và rà soát bảo mật cho dịch vụ lớn**. Không thêm downloader/plugin hoặc nới protocol FFmpeg mà chưa kiểm tra lại. Chế độ public chỉ chạy bằng Dockerfile đi kèm để bảo đảm có wrappers.

## YouTube, PO token và IP datacenter

YouTube chấm điểm IP datacenter (Render, AWS/GCP) là bot và có thể trả “Sign in to confirm you're not a bot” hoặc không trả player response, trong khi TikTok/Facebook vẫn tải được. Cách xử lý đã có trong repo:

- Image Docker chạy kèm **bgutil PO token provider** (`brainicism/bgutil-ytdlp-pot-provider:2.0.0`) trên `127.0.0.1:4416`; `deploy/start.sh` khởi động nó trước uvicorn và chờ `/ping`. Provider chỉ nhận kết nối loopback nên không lộ ra Internet.
- App gọi provider qua `CLIPDROP_POT_URL` (đã khai báo trong `render.yaml`) và thử client `mweb` cho YouTube trước, sau đó tự quay lại client mặc định nếu thất bại. `/api/health` báo `pot: true` khi đã bật.
- Provider **không bảo đảm** vượt qua bot check: nó giúp traffic trông hợp lệ hơn, không phải thuốc chữa chắc chắn. Không dùng cookie tài khoản hay proxy trả phí cho bản demo.

Nếu YouTube vẫn bị chặn: xem **Logs** và tìm dòng `worker raw error:` để đọc lỗi gốc của yt-dlp; thử lại sau vài giờ; hoặc tạo dịch vụ ở region khác (Render không đổi region tại chỗ, sẽ có URL mới). Cập nhật `yt-dlp` trong `requirements-lock.txt` rồi deploy lại khi YouTube thay đổi.

## Vận hành và giới hạn Render

- Không có truy cập 15 phút, Free có thể ngủ; lần mở sau mất khoảng một phút. File tạm mất khi restart/redeploy/ngủ. Không dùng dịch vụ giữ thức giả tạo.
- Render có thể tạm dừng Free vì lưu lượng khởi tạo từ dịch vụ quá cao. Một downloader video dễ chạm giới hạn này.
- Một số nguồn chặn IP datacenter hoặc yêu cầu đăng nhập: app báo lỗi, không né chặn và không bảo đảm cả ba nền tảng luôn tải được.
- Auto-deploy tắt để tránh build ngoài ý muốn. Sau khi cập nhật mã đã test, chọn **Manual Deploy → Deploy latest commit**.
- Xem lỗi trong **Logs**; không đăng log chứa URL riêng tư/cookie. Muốn dừng chia sẻ, dùng **Suspend Service** trên Render. Không nâng plan ngoài phạm vi miễn phí đã chọn.
- Nếu dùng tên miền riêng sau này, cần bổ sung hostname/origin được phép; cấu hình hiện chỉ dành cho hostname Render.

Nguồn: [Render Free](https://render.com/docs/free), [Docker trên Render](https://render.com/docs/docker), [Blueprint](https://render.com/docs/blueprint-spec), [biến môi trường mặc định](https://render.com/docs/environment-variables).
