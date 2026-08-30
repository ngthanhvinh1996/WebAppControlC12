# Điểm dừng — 2026-08-30 (cuối ngày)

Ghi lại để mai làm tiếp. Trạng thái chi tiết ở [README.md](README.md), phân tích
giao thức và lộ trình ở [PLAN_WEBAPP_C12.md](PLAN_WEBAPP_C12.md).

## Đang ở đâu

524 test xanh (`.venv/bin/python -m pytest`). Pha **0–6 xong phần phần mềm** —
chạy đầy đủ với simulator và nguồn video tổng hợp.

| Pha | | Trạng thái |
|---|---|---|
| 0 | Nền móng + simulator | ✅ xong |
| 1 | Đường đọc, bản đồ năng lực | ✅ phần mềm xong · ⏳ chờ phần cứng |
| 2 | Video MJPEG hai luồng | ✅ phần mềm xong · ⏳ chờ phần cứng |
| 3 | Lệnh ghi cho camera | ✅ phần mềm xong · ⏳ chờ phần cứng |
| 4 | Telemetry GAA/GAC | ✅ phần mềm xong · ⏳ chờ phần cứng |
| 5 | Điều khiển gimbal | ✅ phần mềm xong · ⏳ chờ phần cứng |
| 6 | Tối ưu, mở rộng | ✅ ghi phiên xong · ⛔ 3 hạng mục còn lại bị chặn bởi phần cứng |

## Chạy lại từ đầu

```bash
cd ~/workdir/Qualcomm/Rubik_Pi_3/Camera/Skydroid-C12/WebAppControlC12

# terminal 1 — camera giả lập
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000

# terminal 2 — app đầy đủ: video + camera + telemetry + điều khiển
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000 --video synthetic --max-speed 10
```

Mở <http://localhost:8000>.

## Pha 6 vừa làm xong — ghi phiên đồng bộ

`c12ctl/services/session.py` + nút **Record session** + `POST /api/session/*`.

Video, lệnh và tư thế vào một thư mục trên **một đồng hồ** (`time.monotonic()`),
nên đọc lại được "ta gửi gì ngay trước lúc camera làm điều đó". Đo thật: 13 giây
có gimbal quay → 578 sự kiện, 2.2 MB.

Ba điều đáng nhớ:

- **Ghi hình không tốn thêm encode nào** — recorder giữ một *viewer* trên luồng
  MJPEG nên dùng lại bản encode chung của pha 2.
- **Tự dừng** khi chạm trần dung lượng (512 MB) hoặc thời gian (1 giờ), và nói rõ
  cái nào cắt. Đầy thẻ giữa lúc bay thử trên Pi là kết cục tệ hơn nhiều.
- **JPEG nối đuôi + offset trong `events.jsonl`**, không dùng container: không cần
  codec, sống sót khi file bị cắt cụt, lấy khung thứ N là một lần `seek`.

### Ba hạng mục pha 6 CHƯA làm — và vì sao

Không phải vì thiếu thời gian, mà vì **cả ba đều cần phần cứng mới có nghĩa**:

- **go2rtc + WebRTC** — kế hoạch tự đặt điều kiện "chỉ làm nếu số đo pha 2 cho
  thấy cần". Số đo trên máy dev (30 fps, trễ 6 ms) nói là *không* cần. Phép đo
  phân xử phải chạy trên Rubik Pi 3.
- **Hiệu chuẩn FOV + click-để-ngắm** — quy trình hiệu chuẩn là *quay một góc đã
  biết rồi đo dịch chuyển pixel*; không có camera thật thì không có gì để đo.
- **Hoà trộn hai luồng, overlay điểm nóng** — cần cảnh quay thật để căn hai camera
  lệch trục. Trộn hai nguồn tổng hợp chỉ ra ảnh vô nghĩa.

## Pha 3 — tóm tắt

`c12ctl/services/camera.py` + tab **Camera** + `POST /api/camera/<action>`.

Điều đáng nhớ nhất: endpoint **không** trả về "đã gửi", nó trả về **đã đọc lại
và thấy gì**, với `ok` ba trạng thái (`true` / `false` / `null` = không xác nhận
được). Lệnh ghi của C12 không có phản hồi, nên mất gói, firmware không hỗ trợ,
và tham số ngoài dải trông giống hệt nhau ở phía gửi — chỉ bước đọc lại mới tách
được ba ca đó.

Hai quyết định nhỏ nhưng sẽ tiếc nếu quên:

- **Kỳ vọng dựng trước khi gửi** → tham số sai không bao giờ ra tới dây.
- **Poll nghỉ khi gimbal đang quay.** Vòng 20 Hz chiếm hàng ưu tiên nên lệnh đọc
  kẹt tới hết timeout; tính đó là "không hỗ trợ" thì quay vài giây là cả bảng
  trạng thái bị đánh dấu chết. Đã đo với simulator: 3 s quay → 0 đọc, 3 vòng bỏ
  qua, không trường nào bị đánh dấu sai.

## Mai làm gì

### A. Có phần cứng → xác nhận, theo đúng thứ tự rủi ro tăng dần

```bash
# 1. cắm cáp + cấp nguồn 7.2–72 V qua JST-2P (RJ45 KHÔNG cấp nguồn)
.venv/bin/python -m c12ctl.diagnose --preflight-only
# làm theo gợi ý nó in ra, thường là:
sudo ip addr add 192.168.144.20/24 dev enp8s0 && sudo ip link set enp8s0 up

# 2. pha 1 — bản đồ năng lực, read-only, an toàn tuyệt đối
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md

# 3. pha 2 — video thật
.venv/bin/python -m c12ctl.web.app --video live

# 4. pha 3 — tab Camera. Bắt đầu bằng lệnh KHÔNG chạm cơ khí và dễ thấy nhất:
#    palette (đổi màu trên stream), rồi zoom, rồi ghi hình.
# 5. pha 5 — CHỈ khi pha 4 đã thấy tư thế thật
.venv/bin/python -m c12ctl.web.app --video live --max-speed 10
```

Pha 3 trên phần cứng thật trả lời gần hết danh sách câu hỏi mở bên dưới, mà
không phải gửi lệnh gây chuyển động nào.

**Bấm Record session trước khi làm bước 2.** Lần đầu nói chuyện với phần cứng
thật là lần chạy không lặp lại được: nếu camera làm gì đó bất ngờ, bản ghi cho
phép tua lại xem đã gửi gì ngay trước đó, thay vì phải dựng lại tình huống.

### B. Không có phần cứng → gần như đã cạn việc

Pha 0–6 xong phần làm được mà không cần camera. Ba hạng mục pha 6 còn lại đều
**chờ phần cứng chứ không chờ thời gian** (xem mục trên). Việc còn lại đáng làm
mà không cần camera, theo thứ tự giá trị giảm dần:

- **Wizard xác minh góc** (PLAN §6) — bật `GAA`, gửi `goto` +10°, đọc `GAC`, báo
  sai lệch. Dựng và test được với simulator; chạy thật thì cần camera.
- **Protocol Lab** (PLAN §6) — bản GUI của `c12_probe.py send`, vẫn qua allowlist.
- **Trang Health** — gom link/mất gói/RTSP/phiên bản/thẻ nhớ vào một chỗ.
- Đo lại pha 2 **trên Rubik Pi 3** — đây chính là phép đo phân xử cho go2rtc.

## Checklist thủ công pha 5 — không tự động hoá được

Làm với `--max-speed 10`, **tay đặt sẵn trên nút STOP**, không gian quanh gimbal
trống (dây cáp có thể bị quấn). Ba việc này test tự động không thay thế được:

- [ ] Rút cáp mạng giữa lúc gimbal đang quay → phải dừng
- [ ] Đóng tab giữa lúc đang quay → phải dừng *(đã xác nhận với simulator)*
- [ ] Kill backend giữa lúc đang quay → phải dừng *(đã xác nhận với simulator)*

Nhân tiện ghi lại luôn: **gimbal có tự dừng khi ngừng nhận gói không?** Đây là
mâu thuẫn duy nhất giữa hai tài liệu mà không phân xử được từ tài liệu. Vòng
điều khiển đã thiết kế đúng cho cả hai, nhưng biết câu trả lời thật thì tốt hơn.

## Câu hỏi mở, phần cứng trả lời

Pha 1 và pha 3 giải quyết phần lớn mà không cần gửi lệnh rủi ro nào:

- `DZM` hay `ZMC` là zoom thật của C12? (bytecode nói `DZM`)
- `IMG` có phản hồi không? Nếu không thì tab Camera sẽ báo `ok=null` cho palette
  và phải tô màu ở client (`--colormap ironbow`)
- **Trần zoom thật là bao nhiêu?** Giả thuyết 0–67 chưa xác minh. Bấm Zoom + tới
  khi `ok=false` kèm ghi chú "có thể đã chạm trần" — số cuối cùng đọc được chính
  là trần. Đây là cách rẻ nhất để chốt, và nó nằm sẵn trong UI
- `SDC` trả về format thô gì? Trường length chỉ 1 hex nên data tối đa 15 ký tự —
  điều đó loại mọi giả thuyết 2×32-bit. Đệm camera giữ lại chuỗi thô ở
  `fields.sdcard.raw` cho đúng việc này
- `CAP` có làm `free_mb` đổi không? Nếu không thì bằng chứng gián tiếp vô dụng và
  phải tìm đường khác
- `EXT`, `SLR`, `TSM` có sống trên C12 không?
- `IMG` ánh xạ chỉ số nào ra màu nào? (đối chiếu tên palette với màu thấy trên
  stream — pha 3 làm được ngay khi có hình)
- `GSM` có được firmware hỗ trợ? (không tự thăm dò được — xem README)

## Nhắc lại vài ràng buộc dễ quên

- Venv phải tạo với `--system-site-packages` (`cv2`, `numpy` lấy từ hệ thống).
- Máy dev thiếu `avdec_h265`/`h265parse`/`x265enc` — không sao, `cv2` có FFMPEG
  riêng nên decode RTSP H.265 được ngay. Trên Rubik Pi 3 dùng
  `--decoder v4l2h265dec` cho decode phần cứng.
- `pytest.ini` tắt đích danh plugin ROS Humble; đừng xoá.
- **Không bao giờ** đưa `IPV`/`GTW`/`VOM`/`IQE`/`RST`/`RTF` vào registry.
- Thêm lệnh ghi camera mới vào registry mà quên khai báo cách xác nhận trong
  `services/camera.py:WRITES` thì `test_every_camera_write_is_verifiable` hỏng —
  đó là chủ ý.
