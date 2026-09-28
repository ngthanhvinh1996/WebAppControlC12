# Điểm dừng — 2026-09-28

Trạng thái chi tiết ở [README.md](README.md), tra cứu nhanh giao thức ở
[SO_TAY_C12.md](SO_TAY_C12.md), lộ trình ở [PLAN_WEBAPP_C12.md](PLAN_WEBAPP_C12.md).

> **Nhánh: `develop`.** Toàn bộ việc dưới đây nằm ở đó. `main` còn ở `618e405`,
> chưa có control trên ảnh video, chưa có debug log. Clone lên máy mới thì
> `git checkout develop` — đã vấp một lần vì chuyện này.

## Đang ở đâu

549 test xanh (`.venv/bin/python -m pytest`). **Lần đầu chạy được trên phần cứng
thật** — máy target có camera, chạy `develop`, điều khiển camera được.

| Pha | | Trạng thái |
|---|---|---|
| 0 | Nền móng + simulator | ✅ xong |
| 1 | Đường đọc, bản đồ năng lực | ✅ phần mềm xong · ⏳ **chưa quét trên camera thật** |
| 2 | Video MJPEG hai luồng | ✅ phần mềm xong · ⏳ chưa đo trên Rubik Pi |
| 3 | Lệnh ghi cho camera | ✅ phần mềm xong · ⏳ chưa xác nhận đủ trên camera thật |
| 4 | Telemetry GAA/GAC | ✅ phần mềm xong · ⏳ chưa xác nhận tư thế thật |
| 5 | Điều khiển gimbal | ✅ phần mềm xong · 🟢 **điều khiển được trên phần cứng** · ⏳ checklist an toàn chưa chạy |
| 6 | Tối ưu, mở rộng | ✅ ghi phiên + debug log xong · ⛔ 3 hạng mục còn lại vẫn chờ số đo trên Pi |

## Mốc vừa qua: chạy được trên phần cứng

Những gì **đã biết chắc** từ lần chạy đó:

- App chạy trên máy target đang gắn camera.
- Điều khiển camera được từ giao diện web.
- Nguyên nhân lần đầu không thấy control trên ảnh: clone về đang ở `main`.

Những gì **chưa có số liệu** — vì lần chạy đó không bật ghi phiên và chưa lấy
debug log về:

- [ ] độ trễ video thật, fps thật trên Rubik Pi
- [ ] có dùng `--decoder v4l2h265dec` (decode phần cứng) hay không
- [ ] tư thế `GAC` thật có về không, có tươi không
- [ ] soft limit ±85° có chạm và chặn đúng không
- [ ] watchdog 500 ms qua link thật
- [ ] camera trả lời những lệnh đọc nào (bản đồ năng lực)

Lần chạy tới, hai thứ này thu hết những gạch đầu dòng trên mà không tốn công gì:
bấm **Record session** trước khi làm, và sau đó gửi `logs/debug/c12ctl.log`. Thấy
gì lạ thì bấm phím **M** ngay lúc đó.

## Làm gì tiếp — theo thứ tự giá trị

### 1. Quét bản đồ năng lực (read-only, an toàn tuyệt đối)

```bash
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md
```

Một lệnh này trả lời gần hết mục "Câu hỏi mở" bên dưới, **không gửi lệnh gây
chuyển động nào**. Kết quả nối thêm vào `logs/findings.jsonl` nên so được giữa các
lần chạy. Đây là việc đáng làm đầu tiên trong lần cắm camera tới.

### 2. Chạy checklist an toàn pha 5

Chưa làm trên phần cứng thật. Xem mục checklist bên dưới — ba việc đó test tự
động không thay thế được.

### 3. Đo pha 2 trên Rubik Pi 3

```bash
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec
# rồi đọc: curl -s localhost:8000/api/video | python3 -m json.tool
```

Đây chính là **phép đo phân xử cho go2rtc/WebRTC**: kế hoạch tự đặt điều kiện
"chỉ làm nếu số đo cho thấy cần". Số đo trên máy dev (30 fps, trễ 6 ms) nói là
*không* cần — nhưng phép đo phải chạy trên Pi mới có giá trị. Debug log ghi sẵn
fps/độ trễ/CPU/nhiệt SoC mỗi 10 giây, nên chỉ cần chạy vài phút rồi gửi file.

### 4. Merge `develop` → `main`

Sau khi 1–3 sạch. Tám commit đang chờ (xem mục "Từ 30/08 tới nay").

### 5. Việc phần mềm còn lại, không cần camera

Theo thứ tự giá trị giảm dần:

- **Wizard xác minh góc** (PLAN §6) — bật `GAA`, gửi `goto` +10°, đọc `GAC`, báo
  sai lệch. Dựng và test được với simulator; chạy thật thì cần camera.
- **Protocol Lab** (PLAN §6) — bản GUI của `c12_probe.py send`, vẫn qua allowlist.
- **Trang Health** — gom link/mất gói/RTSP/phiên bản/thẻ nhớ vào một chỗ.

## Checklist thủ công pha 5 — chưa chạy trên phần cứng

Làm với `--max-speed 10`, **tay đặt sẵn trên nút STOP**, không gian quanh gimbal
trống (dây cáp có thể bị quấn). Nên dùng mode **D-pad**, mỗi lần một trục.

- [ ] Bấm `Space` giữa lúc đang quay → phải dừng
- [ ] Rút cáp mạng giữa lúc đang quay → phải dừng
- [ ] Đóng tab giữa lúc đang quay → phải dừng *(đã xác nhận với simulator)*
- [ ] Kill backend giữa lúc đang quay → phải dừng *(đã xác nhận với simulator)*
- [ ] Quay tới gần ±85° → soft limit phải chặn trước giới hạn cơ

Nhân tiện ghi lại luôn: **gimbal có tự dừng khi ngừng nhận gói không?** Đây là
mâu thuẫn duy nhất giữa hai tài liệu mà không phân xử được từ tài liệu. Vòng điều
khiển đã thiết kế đúng cho cả hai, nhưng biết câu trả lời thật thì tốt hơn.

## Câu hỏi mở, phần cứng trả lời

Pha 1 và pha 3 giải quyết phần lớn mà không cần gửi lệnh rủi ro nào:

- `DZM` hay `ZMC` là zoom thật của C12? (bytecode nói `DZM`)
- `IMG` có phản hồi không? Nếu không thì tab Camera báo `ok=null` cho palette và
  phải tô màu ở client (`--colormap ironbow`)
- **Trần zoom thật là bao nhiêu?** Giả thuyết 0–67 chưa xác minh. Bấm Zoom + tới
  khi `ok=false` kèm ghi chú "có thể đã chạm trần" — số cuối cùng đọc được chính
  là trần. Rẻ nhất, và nằm sẵn trong UI
- `SDC` trả về format thô gì? Trường length chỉ 1 hex nên data tối đa 15 ký tự —
  loại mọi giả thuyết 2×32-bit. Đệm camera giữ chuỗi thô ở `fields.sdcard.raw`
- `CAP` có làm `free_mb` đổi không? Nếu không thì bằng chứng gián tiếp vô dụng
- `EXT`, `SLR`, `TSM` có sống trên C12 không?
- `IMG` ánh xạ chỉ số nào ra màu nào? (đối chiếu tên palette với màu trên stream)
- `GSM` có được firmware hỗ trợ? (không tự thăm dò được — chạy `--use-gsm
  --max-speed 5`, gimbal đứng im nghĩa là không hỗ trợ, bỏ cờ là về `GSY`+`GSP`)

## Ba hạng mục pha 6 vẫn chờ phần cứng

Không phải thiếu thời gian, mà **cả ba đều cần phần cứng mới có nghĩa**:

- **go2rtc + WebRTC** — chờ phép đo trên Rubik Pi 3 (mục 3 ở trên).
- **Hiệu chuẩn FOV + click-để-ngắm** — quy trình là *quay một góc đã biết rồi đo
  dịch chuyển pixel*; cần camera thật mới có gì để đo.
- **Hoà trộn hai luồng, overlay điểm nóng** — cần cảnh quay thật để căn hai camera
  lệch trục. Trộn hai nguồn tổng hợp chỉ ra ảnh vô nghĩa.

## Từ 30/08 tới nay đã thêm gì

Tám commit trên `develop`, chưa merge vào `main` (`618e405`):

| | |
|---|---|
| `aaa9551` | đưa control gimbal lên chính ảnh video — mắt không rời khỏi hình khi đang quay |
| `2f5adba` | panel dùng được trước khung hình đầu tiên (nguồn live chưa có frame) |
| `b60a30b` | hai mode nhập liệu: **Joystick** (hai trục) và **D-pad** (một trục một lần) |
| `4d1bdf0` | thu nhỏ cụm điều khiển để ảnh ngắn không bị viền đen |
| `3b04efe` | dòng **WARNING** khi điều khiển lúc chưa ARM, kèm lý do server trả về |
| `0532485` | **debug log** ghi ra file: môi trường, gói, snapshot 10 s, sự kiện trình duyệt |
| `a94d454` | nhả tay (vector 0) không còn bị tính là lệnh bị từ chối |
| `f09a004` | `SO_TAY_C12.md` — tra cứu gói tin, bảng lệnh, cách gửi, cách test |

Mode **D-pad** sinh ra cho đúng lúc này: khi cần câu trả lời dứt khoát từ phần
cứng kiểu "chỉ yaw thôi có quay không?", không bị trục thứ hai lẫn vào.

## Chạy lại từ đầu

### Trên máy target (có camera)

```bash
cd ~/.../WebAppControlC12
git checkout develop && git pull        # phải ra f09a004 trở lên

.venv/bin/python -m c12ctl.diagnose --preflight-only    # mạng thông chưa
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec --max-speed 10
```

### Trên máy dev (không camera)

```bash
# terminal 1 — camera giả lập
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000
# terminal 2
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000 --video synthetic --max-speed 10
```

Mở <http://localhost:8000>.

## Nhắc lại vài ràng buộc dễ quên

- **Nhánh là `develop`.** Không thấy control trên ảnh video = đang ở `main`, hoặc
  trình duyệt còn cache `index.html` (Ctrl+Shift+R).
- Venv phải tạo với `--system-site-packages` (`cv2`, `numpy` lấy từ hệ thống).
- Máy dev thiếu `avdec_h265`/`h265parse`/`x265enc` — không sao, `cv2` có FFMPEG
  riêng nên decode RTSP H.265 được ngay. Trên Rubik Pi 3 dùng
  `--decoder v4l2h265dec` cho decode phần cứng.
- `pytest.ini` tắt đích danh plugin ROS Humble; đừng xoá.
- **Không bao giờ** đưa `IPV`/`GTW`/`VOM`/`IQE`/`RST`/`RTF` vào registry.
- Thêm lệnh ghi camera mới vào registry mà quên khai báo cách xác nhận trong
  `services/camera.py:WRITES` thì `test_every_camera_write_is_verifiable` hỏng —
  đó là chủ ý.
- Debug log bật sẵn ở `logs/debug/c12ctl.log`, xoay vòng 8 MB × 3. Có sự cố thì
  đó là file đầu tiên cần gửi.
