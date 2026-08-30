# c12ctl — web app điều khiển Skydroid C12

Trạng thái: **pha 0–6 xong phần phần mềm** (pha 6 làm phần không bị chặn) — chạy đầy đủ với simulator và nguồn
video tổng hợp. Còn chờ phần cứng để xác nhận trên C12 thật. Xem
[PLAN_WEBAPP_C12.md](PLAN_WEBAPP_C12.md) cho phân tích giao thức, kiến trúc và
lộ trình 7 pha.

## Cài đặt

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

`--system-site-packages` là bắt buộc: `cv2` và `numpy` lấy từ hệ thống
(`python3-opencv`), không cài qua pip.

## Chạy — không cần phần cứng

Simulator đứng thay camera, nên phát triển được toàn bộ pha 0 khi chưa nối C12.

```bash
# terminal 1 — camera giả lập
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000

# terminal 2 — web app trỏ vào simulator
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000
```

Mở <http://localhost:8000>.

Thêm video tổng hợp — hai luồng đúng thông số C12 thật (1280×720@30 và
384×288@25), không cần camera:

```bash
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000 --video synthetic
```

Khung tổng hợp có **vạch quét và đồng hồ** để đo độ trễ bằng mắt: so vạch trên
trình duyệt với vạch ở nguồn là ra ngay độ trễ end-to-end, không cần dụng cụ.

Chế độ in gói ra log thay vì gửi UDP:

```bash
.venv/bin/python -m c12ctl.web.app --dry-run
```

## Pha 1 — với camera thật

Toàn bộ pha 1 gói trong một lệnh. Chỉ gửi lệnh `2r`, **read-only, an toàn tuyệt đối**.

```bash
# 1. chẩn đoán mạng trước — không cần camera, không cần root
.venv/bin/python -m c12ctl.diagnose --preflight-only

# 2. làm theo gợi ý nó in ra (thường là gán IP tĩnh), rồi chạy đầy đủ
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md
```

`diagnose` chạy preflight trước và **dừng** nếu tầng link hỏng — quét 23 lệnh trong
khi cáp chưa cắm chỉ tạo ra 23 dòng timeout vô nghĩa. Nếu vẫn muốn quét, thêm
`--skip-preflight`.

Kết quả ra hai nơi: `logs/findings.jsonl` (nối thêm mỗi lần chạy, so được giữa các
lần) và bảng markdown dán thẳng được vào tài liệu.

Mỗi lệnh đi kèm điều nó **chứng minh**. Ví dụ `read.ranging` im lặng xác nhận `SLR`
chỉ có trên C13/C14; `read.palette` trả lời xác nhận palette nằm ở `IMG` chứ không
phải `TAR`. Lệnh nào cho kết quả trái với kỳ vọng của tài liệu sẽ bị đánh dấu
**BẤT NGỜ** ở đầu báo cáo.

Giao diện web cũng làm được việc này — nút **Preflight** và **Quét lệnh đọc**:

```bash
.venv/bin/python -m c12ctl.web.app --packet-log logs/packets.jsonl
```

Cổng UDP 5000 hay bị app trợ lý hoặc ground station chiếm. Nếu gặp `PortBusyError`,
tắt chúng, hoặc chạy với `--local-port 0`.

### Diễn tập trước khi có phần cứng

Quy trình pha 1 chạy được với simulator để kiểm tra trước:

```bash
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000 &
.venv/bin/python -m c12ctl.diagnose --host 127.0.0.1 --port 15000 \
    --local-port 0 --skip-preflight -m logs/CAPABILITIES.md
```

## Pha 2 — video

MJPEG cho cả hai luồng. Chọn MJPEG chứ không phải WebRTC vì **cả hai luồng của C12
là H.265**, mà hỗ trợ HEVC trong trình duyệt rất kém — xem PLAN_WEBAPP_C12.md §4.
MJPEG chạy trên mọi trình duyệt; đo trước, tối ưu sau.

| Endpoint | |
|---|---|
| `GET /video/<name>` | luồng MJPEG (`visible` hoặc `thermal`) |
| `GET /video/<name>/snapshot.jpg` | một khung JPEG |
| `GET /api/video` | số đo: fps vào/ra, độ trễ, ms encode, KB mỗi khung |
| `POST /api/video/<name>/colormap` | tô màu phía server |

Đo được với nguồn tổng hợp trên máy dev (x86, 2 luồng cùng lúc, 1 client mỗi luồng):

```
visible   30.4 fps   5.7 Mb/s   lat 6.0 ms   enc 5.6 ms   23.6 KB/khung
thermal   25.5 fps   4.5 Mb/s   lat 4.3 ms   enc 4.0 ms   22.5 KB/khung
cpu 30.6%   rss 174 MB   (gồm cả sinh khung tổng hợp)
```

Hai luồng **lệch nhịp** (30 vs 25) nên chạy hoàn toàn độc lập — mỗi luồng một
thread, một bus, một encoder, không đồng bộ khung giữa chúng.

### Ba quyết định của tầng video

**Không hàng đợi.** Bus chỉ giữ *một* khung; khung mới đè khung cũ. Consumer chậm
nhảy cóc tới khung mới nhất thay vì lần lượt qua từng khung đã lỡ. Queue tích khung
là tích độ trễ.

**Encode một lần cho mọi client.** Encoder là một task duy nhất; client chỉ đọc
bytes đã encode sẵn. Encode theo từng kết nối là nhân CPU theo số người xem.

**Chỉ encode khi có người xem.** Không ai mở stream thì encoder ngủ.

### Decode H.265 với camera thật

Máy dev này **thiếu** `avdec_h265`, `h265parse`, `x265enc` (chưa cài
`gst-plugins-bad`/`gst-libav`). Không sao: `cv2` được build kèm FFMPEG
(libavcodec 58) nên decode RTSP H.265 được ngay, không cần cài thêm gì.

```bash
# mặc định: backend FFMPEG của cv2 — chạy được ngay
.venv/bin/python -m c12ctl.web.app --video live

# Rubik Pi 3 / QCS6490: decode phần cứng qua GStreamer
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec

# máy dev, nếu đã cài gst-plugins-bad + gst-libav
.venv/bin/python -m c12ctl.web.app --video live --decoder avdec_h265
```

Pipeline luôn có `drop=true max-buffers=1`. Thiếu nó appsink tích khung và độ trễ
tăng dần cho tới khi không dùng được.

### Tô màu ảnh nhiệt

Mặc định **tắt** ở server, vì C12 tự tô được qua lệnh `IMG` (11 palette) — camera
làm thì không tốn CPU nào và ảnh ghi ra thẻ SD có màu giống hệt màn hình. Bật
`--colormap ironbow` chỉ khi pha 1 cho thấy `IMG` không phản hồi.

## Pha 3 — lệnh ghi cho camera

Chụp ảnh, ghi hình, zoom, palette, độ phân giải, 7 tham số ảnh nhiệt. Tab
**Camera** trong giao diện web, hoặc gọi thẳng API.

Quy tắc của cả pha gói trong một câu: **mọi lệnh ghi phải xác nhận được bằng một
lệnh đọc tương ứng.** Lý do là ràng buộc của chính giao thức — lệnh ghi của C12
*không có phản hồi*, nên "đã gửi" và "đã có tác dụng" là hai chuyện khác nhau:
gói UDP có thể mất, firmware có thể không hỗ trợ command word đó, tham số ngoài
dải có thể bị bỏ qua im lặng. Cả ba trường hợp trông giống hệt nhau ở phía gửi.

Vì vậy `POST /api/camera/<action>` **không** trả về "đã gửi". Nó gửi, chờ, đọc
lại, rồi trả về thứ camera thật sự báo:

```bash
curl -X POST localhost:8000/api/camera/palette \
     -H 'content-type: application/json' -d '{"args":["IRONBOW"]}'
```

```json
{"action": "palette", "frame": "#TPUD2wIMG044A", "kind": "direct",
 "read": "read.palette", "expected": "IRONBOW", "actual": "IRONBOW",
 "ok": true, "attempts": 1, "elapsed_ms": 153.2, "note": ""}
```

| Endpoint | |
|---|---|
| `GET /api/camera` | trạng thái đệm: giá trị, tuổi, trường nào không hỗ trợ |
| `POST /api/camera/refresh` | đọc ngay, `?force=true` đọc cả trường đang im lặng |
| `POST /api/camera/<action>` | ghi rồi đọc lại xác nhận |

### Ba mức xác nhận, và ba trạng thái kết quả

`kind` cho biết bằng chứng mạnh tới đâu:

| `kind` | Lệnh | Cách xác nhận |
|---|---|---|
| `direct` | `REC` `IMG` `VID` `TAR TAS TDI TGM TIB TIC TTR` | đọc lại đúng giá trị vừa ghi |
| `relative` | `DZM` | đọc trước, ghi, đọc lại, so **chiều** thay đổi |
| `indirect` | `CAP` | không có lệnh đọc nào — chỉ suy ra từ dung lượng thẻ giảm |

`ok` có **ba** trạng thái, không phải hai:

- `true` — đọc lại thấy đúng.
- `false` — gói tới nơi nhưng camera vẫn báo giá trị cũ. Đây là ca mà "đã gửi"
  nói dối, và là lý do cả cơ chế này tồn tại.
- `null` — **không xác nhận được**: lệnh đọc im lặng, hoặc chưa cắm thẻ nên
  `CAP` không có bằng chứng nào. Gộp nó vào "thất bại" sẽ làm người dùng đi sửa
  nhầm chỗ.

Mỗi lần đọc lại thử tối đa 3 lượt cách nhau 150 ms: gói UDP có thể mất, và
camera thật cần thời gian mở file trên thẻ trước khi `REC` đổi trạng thái.

### Hai chỗ dễ sai đã xử lý

**Kỳ vọng dựng trước khi gửi.** Tham số sai (`palette NEON`) hỏng ở lúc chưa gói
nào rời backend, chứ không phải sau khi đã bắn lên dây.

**Im lặng lúc gimbal đang quay không phải bằng chứng.** Vòng điều khiển 20 Hz
chiếm hàng ưu tiên của `udp_link`, nên lệnh đọc kẹt tới hết timeout. Nếu tính đó
là "camera không hỗ trợ" thì quay gimbal vài giây là cả bảng trạng thái bị đánh
dấu chết và im 30 giây theo cơ chế giãn. Vòng poll **nghỉ** khi gimbal đang
chuyển động, và im lặng trong lúc đó không được tính. Đo với simulator: 3 giây
gimbal quay → 0 lần đọc, 3 vòng bỏ qua, không trường nào bị đánh dấu sai.

### Đệm trạng thái

Poll 1 Hz, nhưng không phải trường nào cũng đọc mỗi vòng: model và phiên bản
firmware đọc **một lần** (không đổi lúc chạy), tham số nhiệt 10 giây một lần,
thẻ nhớ 5 giây. Trường im lặng 3 lần liên tiếp bị giãn xuống 30 giây một lần —
trên phần cứng thật khá nhiều lệnh đọc sẽ không tồn tại, mà mỗi lần dò một lệnh
chết là một lần chờ hết timeout.

Giao diện **chỉ hiển thị giá trị camera trả về**, không bao giờ hiển thị giá trị
người dùng vừa chọn như thể nó đã có hiệu lực.

## Pha 4 & 5 — telemetry và điều khiển gimbal

```bash
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000 --video synthetic --max-speed 10
```

Điều khiển bằng **kéo joystick ảo**, **WASD / phím mũi tên**, hoặc **gamepad**.
`Space` / `Esc` dừng khẩn bất cứ lúc nào.

### Telemetry (pha 4)

`GAA` bật camera tự đẩy gói `GAC` chứa yaw/pitch/roll. Đây là thứ
`skydroid-c12-protocol.md` kết luận nhầm là không tồn tại — luồng đẩy mặc định
tắt nên không gửi `GAA` thì không bao giờ thấy gói nào.

Service gửi lại `GAA` mỗi giây cho tới khi thấy `GAC` đầu tiên, vì hãng ghi rõ
lệnh này **chỉ hiệu lực sau khi camera đã ra hình**.

### Vòng điều khiển (pha 5)

Trình duyệt **không** tick 20 Hz — nó chỉ báo khi trạng thái đổi. Nhịp nằm ở
backend, nơi độ trễ ổn định và không phụ thuộc tab có đang được vẽ hay không.
Đo được: khoảng giữa hai gói **min 49 ms / trung vị 50 ms / max 51 ms**.

Vì sao vẫn keepalive dù có thể thừa: hai tài liệu mâu thuẫn về việc gimbal có tự
dừng hay không, và không phân xử được từ tài liệu. Keepalive 20 Hz đúng ở **cả
hai** hành vi — có test chạy `C12Simulator` ở cả `--hold-speed` lẫn mặc định.

### Năm ngả dừng khẩn

Tất cả đi qua đúng một hàm, `GimbalController.stop_all()`:

| Ngả | Kích hoạt bởi |
|---|---|
| 1 | nút STOP đỏ, luôn hiển thị |
| 2 | phím `Space` / `Esc` |
| 3 | WebSocket đóng — đóng tab, rớt mạng, browser crash |
| 4 | watchdog: không cập nhật nào trong 500 ms |
| 5 | `SIGINT`/`SIGTERM`, và mọi exception thoát ra khỏi vòng điều khiển |

Phía trình duyệt bổ sung: `blur` và `visibilitychange` cắt input ngay — alt-tab
giữa lúc đang giữ phím là kịch bản thật, và `keyup` sẽ không bao giờ đến.

Mỗi lần dừng gửi tốc độ 0 **ba lần** trên cả hai trục: gói UDP có thể mất, một
lần là không đủ.

### Nhịp tim

Trình duyệt chỉ gửi khi state đổi, nên giữ phím 3 giây là 3 giây không có message
nào — watchdog 500 ms sẽ cắt oan. Client gửi `{"type":"ping"}` mỗi 100 ms **chỉ
trong lúc còn chuyển động**; ping làm mới watchdog mà không đổi trạng thái.

### Giới hạn

- `--max-speed` mặc định **10 °/s**, thấp có chủ ý. Trần phần cứng là 63.5 °/s.
- Giới hạn mềm ±85° từ tư thế thật, **chỉ khi telemetry còn tươi**. Tư thế quá
  hạn thì không chặn — an toàn giả còn nguy hiểm hơn không chặn. Chạm biên vẫn
  quay ngược ra được.

### GSM: vì sao không tự thăm dò

`GSM` gộp yaw+pitch vào một gói, giảm nửa lưu lượng, nhưng cần firmware gimbal
≥ 0.5. Kế hoạch ban đầu định "thăm dò lúc khởi động rồi tự lùi" — **không làm
được**: `GSM` là lệnh ghi, không có phản hồi, nên cách duy nhất để biết nó có tác
dụng là ra lệnh chuyển động thật rồi xem gimbal có nhúc nhích không. Thăm dò bằng
cách làm gimbal quay là đánh đổi sai. Mặc định dùng hai gói rời `GSY`+`GSP` —
luôn chạy. Bật `--use-gsm` khi đã biết firmware đủ mới.

## Pha 6 — ghi phiên đồng bộ

Pha 6 có bốn hạng mục, và **ba trong số đó tự chặn mình**: go2rtc/WebRTC chỉ làm
"nếu số đo pha 2 cho thấy cần" (số đo hiện có nói là không, và phép đo phân xử
phải chạy trên Rubik Pi 3), còn hiệu chuẩn FOV và hoà trộn hai luồng đều cần cảnh
quay thật để căn chỉnh. Hạng mục còn lại không vướng gì, và đã làm xong.

Câu hỏi mà nó sinh ra để trả lời, đúng câu hay gặp khi dò một thiết bị lạ: **ta
đã gửi gì, ngay trước lúc camera làm điều đó?**

Trả lời được câu đó từ ba nguồn rời — packet log một nơi, file video một nơi, số
tư thế trôi qua terminal — nghĩa là phải căn chỉnh đồng hồ bằng tay, và đó đúng
là loại việc làm lạc mất năm giây thú vị nhất. Nên mọi thứ vào **một thư mục,
trên một đồng hồ**:

```bash
.venv/bin/python -m c12ctl.web.app --video synthetic --record-fps 4
```

Bấm **Record session**, hoặc gọi API:

```bash
curl -X POST localhost:8000/api/session/start -d '{"note":"bay thử"}'
curl -X POST localhost:8000/api/session/stop
curl localhost:8000/api/session/20260830T150611          # bản tóm tắt
```

| Endpoint | |
|---|---|
| `GET /api/session` | trạng thái + danh sách phiên đã ghi |
| `POST /api/session/start` | bắt đầu, kèm ghi chú tuỳ ý |
| `POST /api/session/stop` | dừng, trả về bản tóm tắt |
| `GET /api/session/<id>` | tóm tắt: thời lượng, thống kê lệnh, dải tư thế, mốc |
| `GET /api/session/<id>/frame/<stream>/<n>.jpg` | rút một khung bất kỳ |

### Cấu trúc một phiên

```
logs/sessions/20260830T150611/
├── meta.json      cấu hình lúc ghi + bản tóm tắt lúc dừng
├── events.jsonl   mỗi dòng một sự kiện, theo đúng thứ tự đồng hồ đơn điệu
├── visible.mjpeg  các khung JPEG nối đuôi nhau
└── thermal.mjpeg
```

Đo thật với simulator, 13 giây có gimbal quay: **578 sự kiện — 348 gói, 130 gói
tư thế, 98 khung hình, 2.2 MB**. Trích một đoạn dòng thời gian:

```
t+0.008   frame     thermal #1 (22962 byte)
t+0.010   packet    tx IMG 04
t+0.011   frame     visible #1 (23220 byte)
t+0.083   packet    rx GAC 000000000000
t+0.083   attitude  yaw=0.00 pitch=0.00
```

Bảng thống kê lệnh trong bản tóm tắt đọc được ngay ra hành vi: `GSY 56/0` và
`GSP 56/0` là vòng điều khiển 20 Hz, `GAC 0/130` là luồng đẩy tư thế, `IMG 12/11`
là 12 lần ghi/đọc palette và 11 lần camera trả lời.

### Bốn quyết định

**Một đồng hồ, và là `time.monotonic()`.** Nó vốn đã là gốc thời gian của
`Frame.captured_at`, của nhật ký gói, và của telemetry — nên căn chỉnh là miễn
phí. Giờ treo tường được ghi kèm chỉ để người đọc dễ định vị, không bao giờ dùng
để sắp thứ tự, vì nó nhảy được.

**JPEG nối đuôi, không phải container.** Không cần codec, sống sót khi file bị
cắt cụt (crash giữa chừng vẫn đọc được mọi khung trước đó), và mỗi khung có sẵn
byte offset trong `events.jsonl` nên lấy khung thứ N là một lần `seek` chứ không
phải quét cả file.

**Ghi hình không tốn thêm một lần encode nào.** Recorder giữ một *viewer* trên
luồng MJPEG, nên nó dùng lại đúng bản encode chung — đúng nguyên tắc "encode một
lần cho mọi client" của pha 2. Có người đang xem thì ghi hình gần như miễn phí.

**Nhịp ghi thấp có chủ ý**, mặc định 5 fps. Nguồn 30 fps × 24 KB là 720 KB/s mỗi
luồng — đầy thẻ trước khi kịp có ích cho việc dò giao thức.

### Hai giới hạn tự cưỡng chế

Recorder **tự dừng** khi chạm trần dung lượng (mặc định 512 MB) hoặc trần thời
gian (mặc định 1 giờ), và ghi rõ cái nào đã cắt. Đầy thẻ giữa lúc bay thử trên Pi
là kết cục tệ hơn nhiều so với một bản ghi kết thúc sớm và nói rõ lý do. Chỉnh
bằng `--record-max-mb` và `--record-max-seconds`; tắt hẳn bằng `--no-record`.

## Test

```bash
.venv/bin/python -m pytest
```

524 test. Bao gồm 43 literal đã kiểm chứng từ cả hai tài liệu nguồn làm ca vàng cho
codec, property test cho bộ mã hoá, và integration test chạy qua socket thật với
simulator.

Nhóm test của pha 3 dùng ba simulator con mô hình hoá đúng ba kiểu hỏng mà lệnh
ghi không phản hồi sẽ gây ra: firmware thiếu lệnh đọc, gói ghi tới nơi nhưng
không có tác dụng, và camera đổi trạng thái chậm hơn một nhịp đọc.

`pytest.ini` tắt đích danh các plugin pytest của ROS Humble — máy dev có
`/opt/ros/humble` trên `PYTHONPATH` và `launch_testing` ở đó phụ thuộc `lark`,
thứ không có trong venv này.

## Chaos mode

Simulator giả lập được các kiểu hỏng mà phần cứng thật sẽ gây ra:

```bash
.venv/bin/python -m c12ctl.sim.c12_sim --chaos-loss 0.3      # mất 30% gói
.venv/bin/python -m c12ctl.sim.c12_sim --chaos-delay 0.5     # phản hồi trễ
.venv/bin/python -m c12ctl.sim.c12_sim --chaos-garbage 0.2   # byte rác
.venv/bin/python -m c12ctl.sim.c12_sim --no-gsm              # firmware gimbal < 0.5
.venv/bin/python -m c12ctl.sim.c12_sim --hold-speed          # gimbal giữ lệnh tốc độ
```

`--hold-speed` đáng chú ý: hai tài liệu nguồn mâu thuẫn về việc gimbal có tự dừng khi
ngừng nhận gói hay không, và không phân xử được từ tài liệu. Vòng điều khiển phải đúng
ở **cả hai** chế độ — có test khẳng định điều đó.

## An toàn

Bốn mức rủi ro cưỡng chế ở backend, không phụ thuộc frontend:

| Mức | Điều kiện |
|---|---|
| 🟢 SAFE | luôn cho phép |
| 🟡 REVERSIBLE | luôn cho phép, ghi log |
| 🟠 PHYSICAL | chỉ khi phiên đã `POST /api/arm` |
| 🔴 DANGEROUS | **không có trong registry** — không route, không UI |

Allowlist chứ không phải blocklist: lệnh chưa khai báo trong
`c12ctl/protocol/registry.py` thì không gửi được. `IPV`/`GTW` (đổi IP, đổi gateway)
nằm ngoài registry vì camera chỉ có một đường vào là Ethernet — sai là mất thiết bị,
không có đường khôi phục.

`POST /api/stop` gửi tốc độ 0 ba lần trên cả hai trục rồi disarm. Phím `Space` và
`Esc` gọi cùng endpoint. Tín hiệu `SIGINT`/`SIGTERM` cũng gửi trước khi thoát.

## Cấu trúc

Project nằm gọn trong `WebAppControlC12/`. Hai thư mục nguyên liệu dịch ngược để
**bên ngoài** vì chúng là nguồn dùng chung, nặng 228 MB, và `rcsdk-demo` có repo
git riêng của hãng:

```
Skydroid-C12/
├── WebAppControlC12/   ← project này
├── rcsdk-demo/         ← SDK Android của hãng, nguồn của PHAN_TICH_SDK_C12.md
└── SkydroidFPVApp/     ← APK + bản giải nén, nguồn của skydroid-c12-protocol.md
```

```
c12ctl/
├── protocol/codec.py      khung + checksum          ← test trước tiên
├── protocol/registry.py   nguồn sự thật duy nhất: cmd, risk, confidence
├── protocol/types.py      enum + bộ mã hoá tham số
├── transport/udp_link.py  1 socket, TX queue, RX demux, JSONL log
├── services/preflight.py  chẩn đoán cáp / IP / cổng / ping / RTSP
├── services/findings.py   bản đồ năng lực + xuất JSONL & markdown
├── diagnose.py            quy trình pha 1 trong một lệnh
├── video/bus.py           ô "khung mới nhất", thread → asyncio
├── video/source.py        nguồn tổng hợp + bắt thật (FFMPEG / GStreamer)
├── video/mjpeg.py         encode một lần, phát cho mọi client
├── video/manager.py       hai luồng của C12, thông số đo sẵn
├── services/telemetry.py  GAA/GAC, tư thế thời gian thực
├── services/gimbal.py     vòng 20 Hz, watchdog, giới hạn mềm
├── services/camera.py     đệm trạng thái + lệnh ghi có xác nhận
├── services/session.py    ghi phiên đồng bộ video + lệnh + tư thế
├── web/app.py             FastAPI + cổng rủi ro + WS điều khiển
└── sim/c12_sim.py         camera giả lập
```

Còn lại của pha 6 — cả ba đều **chờ phần cứng chứ không chờ thời gian**:
go2rtc/WebRTC (chờ số đo trên Rubik Pi 3), hiệu chuẩn FOV và click-để-ngắm, hoà
trộn hai luồng. Cộng với phần xác nhận trên C12 thật của mọi pha đã làm.

## Script cũ

`c12_ctrl.py` và `c12_probe.py` ở thư mục gốc là script dò giao thức ban đầu, giữ lại
để tham chiếu. Lưu ý `c12_probe.py` **không** gửi CRLF cuối gói — bytecode RCSDK có
gửi, và đó có thể là lý do một số lệnh đọc không phản hồi khi dùng script đó.
