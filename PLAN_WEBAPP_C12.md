# Kế hoạch web app điều khiển Skydroid C12

Tài liệu triển khai cho web app điều khiển gimbal-camera C12 chạy trên Rubik Pi 3.
Dùng làm input cho Claude Code.

Nguồn: `skydroid-c12-protocol.md` (chuỗi APK), `PHAN_TICH_SDK_C12.md` + `c12_ctrl.py`
(bytecode RCSDK), `c12_probe.py`. Toàn bộ checksum trong tài liệu này đã được tính lại
và kiểm chứng độc lập — 43/43 literal của cả hai nguồn đều khớp.

---

## 0. TL;DR cho người viết code

- Khung lệnh và checksum: **đã chắc chắn**, không cần dò.
- Ngữ nghĩa lệnh: hai nguồn mâu thuẫn 8 điểm. **Bytecode thắng chuỗi APK** — xem §1.
- Palette là `IMG`, **không phải** `TAR`. Sweep `TAR` sẽ phá cấu hình khử nhiễu.
- Telemetry **có tồn tại** (`GAA` bật push → camera đẩy `GAC`). Tài liệu APK nói không là sai.
- Video là **H.265** → HEVC trong browser rất kém. Làm MJPEG trước, WebRTC sau.
- Không chạm cơ khí trước pha 5.

---

## 1. Phân xử mâu thuẫn giữa hai nguồn

### 1.1 Nguyên tắc

| Nguồn | Cách thu thập | Độ tin |
|---|---|---|
| `PHAN_TICH_SDK_C12.md`, `c12_ctrl.py` | Dịch ngược bytecode `javap -c` từ `rcsdk-v1.9.2.aar` — đọc được cả hằng số lẫn **công thức** | **Cao** |
| `skydroid-c12-protocol.md` | `strings` trên APK + suy luận — thấy chuỗi, không thấy code sinh ra chuỗi | Trung bình |

Bytecode thắng, vì nó cho biết *công thức* chứ không chỉ kết quả. Nhưng bytecode là của
SDK dùng chung C10–C14, nên có thể mô tả lệnh mà C12 không hỗ trợ → **pha 1 chỉ gửi lệnh
đọc để lập bản đồ năng lực thật**, dữ liệu thật phân xử nốt phần còn lại.

### 1.2 Bảng chốt

| # | Vấn đề | protocol.md (APK) | Bytecode — CHỐT |
|---|---|---|---|
| 1 | Palette nhiệt | `TAR`, sweep `00`–`0A` | **`IMG`**, giá trị `01`, `03`–`0C`. `TAR` là khử nhiễu không gian 0–100 |
| 2 | Telemetry | "Không có" `[VERIFIED]` | **Có.** `GAA` bật push 0–100 Hz → camera đẩy gói `GAC` |
| 3 | Zoom | `#TPUM2wZMC01` (đích `M`) | **`#TPUD2wDZM0A`/`0B`** (đích `D`). `ZMC`/`FCC` là của model có ống kính cơ |
| 4 | Đơn vị tốc độ | byte tuỳ ý, dải ±99 | **`raw = clamp(deg_per_s / 0.5, ±127)`** → dải thật ±63.5 °/s |
| 5 | Đơn vị góc | "chưa biết, 0.1° hay 0.01°" | **`angle × 100`**, clamp `[−9000, +9000]`, int16 bù 2, + 2 ký tự tốc độ |
| 6 | `PTZ` | đoán `00`=stop, `01`–`05` | `01`–`05` **đúng**; `00` không phải stop. `0C`/`0D` = **hiệu chuẩn gimbal** |
| 7 | Reset | `RST01` "chưa rõ" | `RST00` = **reboot**; `RTF01` = **factory reset**. Cả hai ngoài phạm vi |
| 8 | Gimbal tự dừng? | "dừng sau vài chục ms" | "chạy tới khi có lệnh mới" | 

**#8 để ngỏ và không cần phân xử**: keepalive 20 Hz là tập cha của cả hai hành vi.
Nếu gimbal tự dừng, keepalive giữ nó chạy; nếu nó giữ lệnh, keepalive vô hại và watchdog
vẫn cần thiết.

### 1.3 Chi tiết sẽ làm hỏng code nếu bỏ qua

- **Kết thúc gói bằng `\r\n`** khi ghi xuống socket (có trong bytecode, thiếu trong
  protocol.md và trong `c12_probe.py` — có thể là lý do một số lệnh đọc không phản hồi).
- **`EXT` dùng tiền tố chữ thường**: `#tpUD4wEXT0…`. Hàm dựng khung tuyệt đối **không được
  `.upper()`** phần thân; checksum tính trên đúng chữ thường (`#tpUD4wEXT0110` → `FE`).
- **`setVideoConfig` timeout 4000 ms**, dài gấp nhiều lần lệnh khác → timeout khai báo
  **per-command**, không dùng hằng số chung.
- **`SDC` đọc bằng data `01`** (`#TPUD2rSDC013F`) → dung lượng tổng + còn lại; cả hai = 0
  nghĩa là chưa cắm thẻ.
- **Cổng UDP 5000 hay bị chiếm** bởi app trợ lý / ground station → backend phải báo lỗi
  rõ ràng thay vì crash. Đây là lỗi vận hành số một.

### 1.4 Bảng palette đúng (checksum đã tính sẵn)

```
WHITE_HOT #TPUD2wIMG0147   AURORA    #TPUD2wIMG074D
SEPIA     #TPUD2wIMG0349   RED_HOT   #TPUD2wIMG084E
IRONBOW   #TPUD2wIMG044A   JUNGLE    #TPUD2wIMG094F
RAINBOW   #TPUD2wIMG054B   MEDICAL   #TPUD2wIMG0A57
NIGHT     #TPUD2wIMG064C   BLACK_HOT #TPUD2wIMG0B58
GLORY_HOT #TPUD2wIMG0C59   đọc:      #TPUD2rIMG0041
```

Đúng 11 mục — khớp con số 11 palette mà protocol.md đếm được trong resource APK.
Hai nguồn độc lập, cùng con số → đây là bảng đúng.

### 1.5 Telemetry

```
#TPUG2wGAA0A46   bật push 10 Hz
#TPUG2wGAA0035   tắt push
→ camera đẩy GAC: 3 × int16 hex (yaw, pitch, roll), chia 100 ra độ
```

Lưu ý hãng: `GAA` chỉ hiệu lực **sau khi camera đã ra hình** → gửi lặp vài lần lúc khởi
động, đừng gửi một phát rồi kết luận không hỗ trợ.

### 1.6 Tốc độ và góc

```python
speed_raw = max(-127, min(127, int(deg_per_s / 0.5)))   # ±63.5 °/s, bước 0.5
angle_raw = max(-9000, min(9000, int(deg * 100)))       # ±90.00°
```

Yaw: âm = trái, dương = phải. Pitch: âm = xuống, dương = lên.

| Gói | protocol.md gọi là | Thực tế |
|---|---|---|
| `#TPUG2wGSY3264` | +50 | **+25 °/s** |
| `#TPUG2wGSYCE87` | −50 | **−25 °/s** |
| `#TPUG2wGSY7F7C` | — | **+63.5 °/s** (tối đa) |
| `#TPUG2wGSY005F` | 0 | **dừng** |

Nhãn của protocol.md lệch **đúng hệ số 2**. Ví dụ tốc độ trong README của hãng dùng hệ số
0.1 **cũ** (trước SDK v1.2.1) — tin bytecode.

```
#TPUG6wGAY0BB8103E        yaw   +30.00°, speed 0x10
#TPUG6wGAPDCD8104C        pitch −90.00°, speed 0x10
#TPUGCwGAM0BB810DCD810A3  cả hai trục một gói
```

---

## 2. Kiến trúc

Một tiến trình Python: FastAPI + asyncio, một socket UDP, video trên thread riêng.
Không microservice, không message broker — mỗi tầng thêm vào là thêm độ trễ vào vòng
điều khiển.

```
Browser (HTML + JS thuần, không build step)
  ├── WS   /ws/control      state gimbal, arm/disarm, heartbeat
  ├── WS   /ws/telemetry    tư thế GAC, trạng thái camera, log gói
  ├── REST /api/cmd/<name>  lệnh rời rạc
  └── GET  /video/<id>      MJPEG
        ↓
Backend — FastAPI / uvicorn, một tiến trình
  ├── protocol/registry.py  NGUỒN SỰ THẬT DUY NHẤT: cmd3, encoder, risk, confidence
  ├── protocol/codec.py     frame / parse / checksum
  ├── transport/udp_link.py 1 socket, TX queue, RX demux, JSONL log
  ├── services/gimbal.py    vòng 20 Hz, watchdog, giới hạn mềm
  ├── services/camera.py    lệnh rời rạc, cache trạng thái
  ├── services/telemetry.py bật GAA, parse GAC
  ├── video/*.py            GStreamer → khung mới nhất → MJPEG
  └── sim/c12_sim.py        camera giả lập, dev không cần phần cứng
        ↓
C12 @ 192.168.144.108 — UDP :5000, RTSP :554 (khả kiến), RTSP :555 (nhiệt)
```

Host phải tự gán IP tĩnh, camera không có DHCP:

```bash
sudo ip addr add 192.168.144.20/24 dev <iface>
```

### 2.1 Bốn quyết định

**Registry là nguồn sự thật duy nhất.** Mỗi lệnh khai báo một lần: mã 3 ký tự, đích,
read/write, hàm mã hoá data, hàm giải mã phản hồi, timeout riêng, **mức rủi ro**, và
**độ tin cậy** (bytecode / APK / phỏng đoán). Từ bảng đó sinh ra đồng thời: allowlist
phía service, UI trang Diagnostics, và bộ test. Không bao giờ có chuyện frontend gọi được
lệnh mà backend chưa phân loại rủi ro.

**Một socket UDP, một writer.** Camera là thiết bị nhúng nhỏ; bắn song song dễ làm nó bỏ
gói. `asyncio.DatagramProtocol`, mọi lệnh xếp hàng qua một task ghi duy nhất, khoảng cách
tối thiểu ~15 ms. Task đọc tách gói, đối chiếu `CMD3`, phân phối về *future* đang chờ
(lệnh đọc) hoặc bus telemetry (gói `GAC` tự đẩy). Lệnh tốc độ gimbal đi đường ưu tiên.

**Browser báo trạng thái, backend giữ nhịp.** Browser **không** tick 20 Hz. Nhấn phím →
gửi một message WS `{yaw: 25, pitch: 0}`. Nhả phím → `{yaw: 0, pitch: 0}`. Vòng lặp 50 ms
nằm ở backend, nơi độ trễ ổn định và không phụ thuộc tab có đang được vẽ hay không.
Dùng `GSM` (yaw+pitch một gói) thay hai gói `GSY`+`GSP` — giảm nửa lưu lượng; cần firmware
gimbal ≥ 0.5 nên thăm dò lúc khởi động và tự lùi về hai gói rời.

**Video: khung mới nhất, không hàng đợi.** Mỗi luồng RTSP đọc trên một thread riêng
(GStreamer appsink), ghi vào một ô "khung mới nhất" và đánh thức client đang chờ. Không
dùng queue — queue tích khung là tích độ trễ. JPEG encode **một lần cho mọi client**.
Hai luồng 30 fps và 25 fps chạy độc lập, **không** đồng bộ khung.

### 2.2 Cây thư mục

```
c12ctl/
├── protocol/
│   ├── codec.py          frame(), parse(), checksum() — viết & test TRƯỚC
│   ├── registry.py       bảng lệnh + risk + confidence
│   └── types.py          Palette, Resolution, AKey, RiskLevel, encoder
├── transport/
│   └── udp_link.py       socket, TX queue, RX demux, JSONL log
├── services/
│   ├── gimbal.py         vòng 20 Hz + watchdog + arm/disarm
│   ├── camera.py         lệnh rời rạc + cache trạng thái
│   └── telemetry.py      GAA/GAC
├── video/
│   ├── source.py         GStreamer → khung mới nhất
│   ├── mjpeg.py          multipart/x-mixed-replace
│   └── colormap.py       tuỳ chọn: cv2.applyColorMap phía server
├── web/
│   ├── app.py            FastAPI, route, WS
│   └── static/
├── sim/
│   └── c12_sim.py        camera giả lập UDP + video tổng hợp
└── tests/
```

---

## 3. Lớp an toàn

Camera chỉ có một đường vào là Ethernet — không UART, không nút reset dễ với. Sai một lệnh
cấu hình mạng là mất thiết bị. Lớp an toàn là ràng buộc thiết kế, không phải tính năng phụ.

### 3.1 Bốn mức rủi ro, cưỡng chế ở backend

| Mức | Gồm | Điều kiện thực thi |
|---|---|---|
| 🟢 SAFE | toàn bộ lệnh `2r` | luôn cho phép, ghi log |
| 🟡 REVERSIBLE | `CAP` `REC` `DZM` `IMG` `VID` `GAA`, tham số nhiệt | cho phép; UI hiển thị giá trị đọc về từ camera |
| 🟠 PHYSICAL | `GSY` `GSP` `GSM` `GAY` `GAP` `GAM` `PTZ01`–`05` | **chỉ khi phiên đang ARM** + watchdog sống + trong giới hạn tốc độ |
| 🔴 DANGEROUS | `IPV` `GTW` `VOM` `IQE` `RST` `RTF` `PTZ0C`/`0D` `GAR` | **không có trong registry.** Không route, không UI, không cờ bật |

**Allowlist, không phải blocklist.** Service chỉ chấp nhận lệnh *có trong registry*. Một
chuỗi tuỳ ý gửi từ trang Protocol Lab vẫn phải khớp một mục registry mới được gửi.
Blocklist luôn thua vì lệnh chưa biết mặc định được cho qua; với thiết bị không có đường
khôi phục, allowlist là hướng mặc định duy nhất chấp nhận được.

### 3.2 Đường dừng khẩn — năm ngả, cùng một hàm

`stop_all()` gửi `#TPUG2wGSY005F` + `#TPUG2wGSP0056` **ba lần**, đặt state về 0, disarm phiên.

1. Nút STOP đỏ, luôn hiển thị, cố định ở mọi tab
2. Phím `Space` và `Esc`
3. WebSocket đóng hoặc lỗi — kể cả khi tab bị đóng đột ngột
4. Watchdog: không nhận state update nào trong 500 ms
5. `SIGTERM`/`SIGINT`, và bất kỳ exception nào thoát ra khỏi vòng điều khiển

Phía browser bổ sung: `blur` và `visibilitychange` gửi state 0 ngay. Người dùng alt-tab
giữa lúc đang giữ phím là kịch bản thật, và `keyup` sẽ không bao giờ đến.

### 3.3 Giới hạn tăng dần

Backend nhận cờ `--max-speed`, mặc định **10 °/s** cho lần chạy đầu. Chỉ nâng lên 30 rồi
63.5 sau khi đã quan sát vòng điều khiển dừng đúng trong thực tế. Khi `GAC` đã chạy
(pha 4), thêm giới hạn mềm: pitch chạm ±85° thì ép tốc độ về 0 theo hướng đang đi.

### 3.4 Nhật ký

Mọi khung TX/RX ghi ra JSONL kèm timestamp đơn điệu, hướng, chuỗi thô, bản giải mã.
Vừa để truy vết khi camera vào trạng thái lạ, vừa là dữ liệu giải nốt câu hỏi giao thức.
Kèm cờ `--dry-run` in gói ra stdout thay vì gửi.

---

## 4. Video — rủi ro lớn nhất

Cả hai luồng đều **H.265**. Hỗ trợ HEVC trong browser rất kém: WebRTC chỉ nhận H.265 trên
Chrome bản rất mới **và** phải có decode phần cứng; MSE thì tuỳ nền tảng. protocol.md
khuyến nghị WebRTC cho luồng khả kiến mà không nhắc điều này — làm theo là dễ dựng xong cả
pipeline rồi mới phát hiện video không hiện trên máy đích.

| Phương án | Độ trễ | CPU | Rủi ro tương thích |
|---|---|---|---|
| **MJPEG** — decode ở server, encode JPEG | ~250–400 ms | trung bình | **không** — chạy mọi browser |
| WebRTC, chuyển mã H.265 → H.264 | ~200 ms | cao | thấp |
| WebRTC, chuyển tiếp thẳng H.265 | ~200 ms | ~0 | **cao** — phụ thuộc browser & máy |
| HLS / DASH | 2–6 s | thấp | không dùng — trễ 3 s là vô nghĩa |

**Khuyến nghị: MJPEG trước, đo, rồi mới tối ưu.** Pha 2 làm MJPEG cho cả hai luồng — chắc
chắn chạy, cho ngay chỗ móc để vẽ overlay, tái dùng đúng đoạn GStreamer sẽ dùng cho bản
native. Chỉ khi *đo được* rằng độ trễ hoặc CPU không chấp nhận được thì mới thêm go2rtc +
WebRTC ở pha 6, sau lưng cùng một interface.

Đây là chỗ lệch khỏi protocol.md một cách có chủ ý: nó chọn WebRTC làm phương án chính
ngay từ đầu, tức đặt rủi ro lớn nhất vào đường tới hạn khi chưa có gì chạy để so sánh.

```bash
# Máy dev (x86, phần mềm)
rtspsrc location=rtsp://192.168.144.108:554/stream=1 latency=200 protocols=tcp \
  ! rtph265depay ! h265parse ! avdec_h265 \
  ! videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1

# Rubik Pi 3 / QCS6490 (phần cứng)
rtspsrc location=rtsp://192.168.144.108:554/stream=1 latency=200 protocols=tcp \
  ! rtph265depay ! h265parse ! v4l2h265dec \
  ! videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1
```

`drop=true max-buffers=1` không phải chi tiết vụn — thiếu nó, appsink tích khung và độ trễ
tăng dần cho tới khi không dùng được. Chọn decoder qua config, đừng hard-code.

**Tô màu ảnh nhiệt giờ là tuỳ chọn**: vì `IMG` cho phép camera tự tô màu, đường mặc định
là để camera làm — không tốn CPU, và ảnh ghi ra thẻ SD có màu giống hệt màn hình. Giữ
`cv2.applyColorMap` phía server làm dự phòng nếu pha 1 phát hiện C12 không trả lời `IMG`,
và làm nền cho overlay sau này.

---

## 5. Lộ trình 7 pha

Thứ tự theo **rủi ro tăng dần**, không theo độ khó. Không lệnh nào chạm cơ khí trước pha 5,
và tới lúc đó đã có telemetry để nhìn thấy gimbal đang làm gì.

### Pha 0 — Nền móng và simulator 🟢 · không cần camera

- `codec.py` + test bảng trên toàn bộ 43 literal đã kiểm chứng — **viết trước mọi thứ khác**
- `registry.py` với mức rủi ro và độ tin cậy cho từng lệnh
- `c12_sim.py`: parse khung, kiểm checksum, trả lời lệnh đọc, mô phỏng gimbal tích phân
  tốc độ, đẩy `GAC`
- Chaos mode của simulator: mất gói, trả lời chậm, khung rác
- Bộ khung FastAPI + `--dry-run`

**Qua pha khi:** test codec xanh toàn bộ; app chạy end-to-end với simulator, chưa cần phần cứng.

### Pha 1 — Đường đọc, lập bản đồ năng lực thật 🟢 · cần camera

- Gán IP tĩnh `192.168.144.20/24`, xác nhận ping
- `udp_link.py` thật; xử lý rõ ràng lỗi cổng 5000 bị chiếm
- Trang Diagnostics: gọi toàn bộ lệnh `2r`, ghi phản hồi và format
- Xuất `findings.jsonl` — lệnh nào sống, dữ liệu trả về ra sao

**Qua pha khi:** đọc được `VER`, `MOD`, `SDC`; biết chắc `DZM`/`IMG` có phản hồi hay không
— trước khi ghi bất cứ thứ gì.

### Pha 2 — Video 🟢 · cần camera

- GStreamer appsink → ô khung mới nhất, một thread mỗi luồng
- MJPEG cho cả hai luồng; encode một lần dùng chung mọi client
- Bố cục PiP / cạnh nhau; đo và hiển thị độ trễ, fps, CPU
- Colormap phía client làm dự phòng nếu pha 1 cho thấy `IMG` không sống

**Qua pha khi:** hai luồng chạy song song ổn định > 10 phút, độ trễ không tăng dần.

### Pha 3 — Lệnh ghi cho camera 🟡 · cần camera

- Chụp ảnh, bắt đầu/dừng ghi, đọc lại trạng thái ghi để xác nhận
- Zoom in/out từng nấc bằng `DZM`, đọc lại hệ số
- Palette qua `IMG` — 11 giá trị, đối chiếu tên với màu thấy trên stream
- Cache trạng thái poll 1 Hz, hiển thị giá trị camera đang thật sự dùng

**Qua pha khi:** mọi lệnh ghi xác nhận được bằng một lệnh đọc tương ứng; bảng tên palette
đã đối chiếu với màu thật.

### Pha 4 — Telemetry 🟢 · cần camera · **tiên quyết cho pha 5**

- Bật `GAA` 10 Hz, gửi lặp vài lần sau khi đã có hình
- Parse `GAC`, dựng bus tư thế, đẩy lên WS telemetry
- HUD đường chân trời + số yaw/pitch/roll
- Nếu `GAA` không phản hồi: ghi nhận, pha 5 chạy chế độ mù với giới hạn tốc độ chặt hơn

**Qua pha khi:** nhìn thấy tư thế đổi thời gian thực khi **xoay gimbal bằng tay** — xác
nhận vòng phản hồi đúng mà chưa cần gửi lệnh chuyển động nào.

### Pha 5 — Điều khiển gimbal 🟠 · cần camera + không gian trống

- Arm/disarm, vòng 20 Hz, watchdog 500 ms, `stop_all()` nối vào cả năm ngả
- Chạy lần đầu với `--max-speed 10`, tay đặt sẵn trên nút STOP
- Bàn phím + joystick ảo + Gamepad; thăm dò `GSM`, tự lùi về `GSY`+`GSP`
- Ghi lại hành vi thật: gimbal có tự dừng khi ngừng gửi không? (§1.2 #8)
- **Chỉ sau đó**: `goto` tuyệt đối, xác minh bằng `GAC`, rồi preset và `PTZ05` về giữa

**Qua pha khi:** rút cáp mạng giữa lúc gimbal đang quay → phải dừng. Đóng tab giữa lúc
đang quay → phải dừng. Kill backend → phải dừng. Cả ba test bằng tay trên phần cứng thật.

### Pha 6 — Tối ưu và mở rộng 🟡 · sau khi đã đo

- go2rtc + WebRTC cho luồng khả kiến — **chỉ làm nếu số đo pha 2 cho thấy cần**
- Click-để-ngắm, hiệu chuẩn FOV
- Ghi phiên đồng bộ video + lệnh + tư thế
- Hoà trộn hai luồng, overlay điểm nóng

Web app là prototype tốt và là công cụ dò giao thức tốt, nhưng **không thay được bản
native** trên Pi: thêm một chặng bridge, không dùng trực tiếp được zero-copy của QCS6490.
Hai bản bổ trợ nhau.

---

## 6. Ý tưởng tính năng

### Biến app thành công cụ giải giao thức

Tình huống thật là hai tài liệu mâu thuẫn và một danh sách câu hỏi mở. Thay vì chạy script
rời rồi chép kết quả vào markdown bằng tay, để chính web app làm nhạc cụ đo:

- **Bản đồ năng lực** — Diagnostics gọi toàn bộ lệnh `2r`, ghi lệnh nào trả lời và format
  gì. Không rủi ro, và nó phân xử hộ `DZM` vs `ZMC`, `IMG` vs `TAR`, `SLR` có tồn tại không.
- **Wizard xác minh góc** — bật `GAA`, đọc `GAC`, gửi `goto` +10°, đọc lại, báo sai lệch.
  Xác nhận đơn vị góc bằng số đo chứ không bằng mắt.
- **Protocol Lab** — bản GUI của `c12_probe.py send`: gõ thân lệnh, thấy checksum tính sẵn,
  thấy gói sẽ gửi, thấy phản hồi. Vẫn qua allowlist.
- **Xuất phát hiện** — ghi `findings.jsonl` và sinh lại bảng markdown, để tài liệu tự cập
  nhật từ dữ liệu thật.

### Từ telemetry mà ra

- **HUD tư thế** — đường chân trời nhân tạo + số yaw/pitch, 10 Hz
- **Preset vị trí** — lưu góc hiện tại, gọi lại một nút; chỉ đáng tin khi có `GAC` xác nhận
- **Giới hạn mềm** — chặn trước khi chạm biên cơ khí
- **Click-để-ngắm** — bấm một điểm trên video, quy ra độ lệch góc theo FOV, gửi `goto`
  tương đối. FOV tự hiệu chuẩn: quay một góc đã biết, đo dịch chuyển pixel

### Vận hành

- **Gamepad API** — vài chục dòng JS, cần analog cho tốc độ tỉ lệ thay vì bật/tắt
- **Joystick ảo cảm ứng** — máy tính bảng ngoài hiện trường là kịch bản thật
- **Trang Health** — link up/down, mất gói, RTSP còn sống, phiên bản, model, dung lượng thẻ
- **Ghi phiên** — video + log lệnh + tư thế đồng bộ theo thời gian

---

## 7. Test & simulator

Simulator là *điều kiện cần* để pha 0 tồn tại (máy dev chưa nối được camera). Nhưng giá trị
lâu dài nằm ở chỗ khác: nó là cách duy nhất test các đường an toàn mà không phải rút cáp
thật hàng chục lần.

| Tầng | Kiểm gì | Cách |
|---|---|---|
| Codec | checksum, dựng khung, parse phản hồi, tiền tố chữ thường `EXT` | pytest bảng dữ liệu, 43 literal làm ca vàng |
| Mã hoá | tốc độ ↔ raw, góc ↔ int16, clamp ở biên và ngoài biên | property test `decode(encode(x)) == clamp(x)` |
| Vòng điều khiển | nhịp 20 Hz; watchdog kích sau 500 ms; state 0 khi WS đóng; lệnh Physical bị từ chối khi chưa arm | integration test trên simulator, khẳng định trên chuỗi gói simulator nhận được |
| Bền bỉ | mất 30% gói, phản hồi trễ, khung sai checksum | chaos mode — app không được treo hay kẹt gimbal ở tốc độ khác 0 |
| Thủ công | rút cáp, đóng tab, kill process giữa lúc gimbal quay | checklist pha 5, phần cứng thật, không thay được bằng test tự động |

---

## 8. Việc KHÔNG làm

Ghi rõ để sau này không ai — kể cả bạn lúc 2 giờ sáng — vô tình thêm vào.

- **Không** đưa `IPV`, `GTW` vào registry. Đổi IP sai là mất camera vĩnh viễn: không UART,
  không nút reset, chỉ còn cách quét lại cả dải mạng và cầu may.
- **Không** đưa `VOM`, `IQE` vào bản đầu. Chúng đổi cấu hình encode, có thể làm hỏng luồng
  RTSP — mất luôn cả video lẫn khả năng chẩn đoán.
- **Không** đưa `RST`, `RTF`. Reboot làm bằng cách ngắt nguồn.
- **Không** sweep mù dải `PTZ`: `0C`/`0D` khởi động hiệu chuẩn gimbal.
- **Không** sweep `TAR` để tìm palette — nó là khử nhiễu, và bạn sẽ không biết giá trị gốc
  để trả lại. Palette là `IMG`.
- **Không** dùng `GAM` hay `GAR` cho tới khi `GAY`/`GAP` đã xác minh xong bằng `GAC`.
  Sai đơn vị trên ba trục là hậu quả nhân ba.
- **Không** dùng HLS/DASH cho video. Trễ 3 giây thì điều khiển gimbal là vô nghĩa.
- **Không** để browser tick 20 Hz. Nhịp thuộc về backend.

---

## 9. Câu hỏi còn mở

Cần phần cứng mới trả lời được. Pha 1 và pha 3 giải quyết phần lớn danh sách này mà không
cần gửi một lệnh rủi ro nào.

- Gimbal có tự dừng khi ngừng nhận gói không? (đã thiết kế an toàn cho cả hai)
- `GSM` có được firmware hỗ trợ không?
- `PTZ00` thật sự làm gì?
- `EXT` và `SLR` có sống trên C12 không?
- Dải `DZM` thật là 0–67 hay khác?
- `IMG` ánh xạ chỉ số nào ra màu nào?
