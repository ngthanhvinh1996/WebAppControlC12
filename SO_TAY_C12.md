# Sổ tay C12 — gói tin, định dạng, cách gửi, cách test

Ghi chú tra cứu nhanh cho Skydroid C12 trên Rubik Pi 3. Mọi khung lệnh trong file
này được **sinh thẳng từ `c12ctl/protocol/registry.py`**, không gõ tay — checksum
đúng, copy dán chạy được ngay.

> **Nhánh:** code đầy đủ nằm ở **`develop`**. `main` là bản cũ, chưa có điều khiển
> gimbal trên ảnh video, chưa có debug log. Clone về máy target thì nhớ
> `git checkout develop` — đây là chỗ đã vấp một lần.

---

## 1. Tra nhanh 30 giây

| | |
|---|---|
| Camera | `192.168.144.108`, UDP cổng `5000`, **không có DHCP** |
| Host | phải tự gán IP tĩnh cùng dải, ví dụ `192.168.144.20/24` |
| Video | RTSP `:554/stream=1` (visible) · `:555/stream=2` (thermal), cả hai H.265 |
| Nguồn | 7.2–72 V qua JST-2P. **RJ45 không cấp nguồn** |
| Khung lệnh | `#TP` + src + dest + len + rw + cmd3 + data + crc2, **kết thúc bằng CRLF** |
| Tốc độ gimbal | ±63.5 °/s, bước 0.5 (raw = °/s ÷ 0.5, chặn ±127) |
| Góc tuyệt đối | ±90.00°, raw = độ × 100 |
| Chạy trên Pi | `.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec --max-speed 10` |

Ba lệnh hay cần nhất:

```bash
.venv/bin/python -m c12ctl.diagnose --preflight-only    # mạng có thông không
.venv/bin/python -m c12ctl.diagnose -m logs/CAP.md      # camera trả lời lệnh nào
.venv/bin/python -m c12ctl.web.app --video live         # chạy app
```

---

## 2. Gói tin — cấu trúc khung

```
#TP U D 2 w REC 01 44
│   │ │ │ │  │   │  └── checksum: sum(byte) & 0xFF, 2 ký tự hex HOA
│   │ │ │ │  │   └───── data, số ký tự đúng bằng trường len
│   │ │ │ │  └───────── command word, 3 ký tự
│   │ │ │ └──────────── 'r' = đọc, 'w' = ghi
│   │ │ └────────────── len: SỐ KÝ TỰ của data, 1 ký tự hex (0..F)
│   │ └──────────────── dest: D = camera, G = gimbal
│   └────────────────── src: U = host (máy mình)
└────────────────────── header "#TP"
```

Bốn điều dễ sai, cả bốn đều lấy từ bytecode RCSDK:

1. **Phải có `\r\n` cuối gói** khi ghi xuống socket. `c12_probe.py` thiếu phần
   này, và nhiều khả năng đó là lý do vài lệnh đọc ở đó không bao giờ trả lời.
2. **`len` đếm KÝ TỰ, không đếm byte dữ liệu.** `GAM` có 12 ký tự data nên len là
   `C`, không phải `6`.
3. **Lệnh `EXT` dùng header thường `#tp`.** Checksum tính trên đúng chuỗi thường
   đó — không bao giờ được `.upper()` thân gói.
4. **Camera gộp nhiều khung vào một datagram** và không phải lúc nào cũng chèn
   CRLF giữa chúng. Tách khung phải dựa vào `len` (cắt đúng `12 + len` ký tự),
   không dựa vào dấu phân cách.

Khung trả về đảo src/dest: mình gửi `#TPUD...`, camera trả `#TPDU...`; gimbal đẩy
tư thế về thì là `#TPDG...`.

### Checksum — làm tay một lần cho nhớ

```
body  = #TPUG2wGSY14
bytes = #=35 T=84 P=80 U=85 G=71 2=50 w=119 G=71 S=83 Y=89 1=49 4=52
sum   = 868  →  868 & 0xFF = 100  →  "64"
frame = #TPUG2wGSY14 + 64 = #TPUG2wGSY1464
```

Một dòng Python:

```python
checksum = lambda body: "%02X" % (sum(body.encode()) & 0xFF)
```

---

## 3. Mã hoá tham số

| Kiểu | Công thức | Ví dụ |
|---|---|---|
| Tốc độ (°/s) | `raw = int(dps / 0.5)`, chặn ±127 → hex 2 ký tự, bù 2 | `10 °/s → 20 → "14"` · `-5 °/s → -10 → "F6"` |
| Góc (độ) | `raw = int(deg * 100)`, chặn ±9000 → hex 4 ký tự, bù 2 | `30° → 3000 → "0BB8"` · `-20° → -2000 → "F830"` |
| Phần trăm | `0..100` → hex 2 ký tự | `60 → "3C"` |
| Tốc độ đi tới góc | hằng `0x10` nối sau mỗi góc | `GAY = <góc 4 ký tự> + "10"` |

**Tốc độ thật là ±63.5 °/s**, không phải ±127 °/s. `skydroid-c12-protocol.md` sai
đúng 2 lần ở bảng này; bytecode có hằng `0.5f` nên bytecode thắng.

Giải mã tư thế `GAC` — 3 số int16 liên tiếp, chia 100:

```
#TPDGCwGAC0BB8F83000646E
           └yaw┘└pit┘└rol┘
   0BB8 = 3000 → yaw   =  30.00°
   F830 = -2000 → pitch = -20.00°
   0064 = 100  → roll  =   1.00°
```

---

## 4. Bảng lệnh

45 lệnh trong registry. **Allowlist, không phải blocklist**: lệnh không khai báo
trong `registry.py` thì không gửi được bằng bất kỳ đường nào.

### 🟢 SAFE — chỉ đọc, luôn cho phép

| Tên | cmd3 | Khung gửi | Trả về |
|---|---|---|---|
| `read.version` | VER | `#TPUD2rVER0051` | firmware camera |
| `read.hardware_version` | HWV | `#TPUD2rHWV0059` | version phần cứng |
| `read.model` | MOD | `#TPUD2rMOD0044` | model |
| `read.recording` | REC | `#TPUD2rREC003E` | đang quay hay không |
| `read.palette` | IMG | `#TPUD2rIMG0041` | palette nhiệt |
| `read.resolution` | VID | `#TPUD2rVID0047` | độ phân giải |
| `read.zoom` | DZM | `#TPUD2rDZM004F` | mức zoom số |
| `read.sdcard` | SDC | `#TPUD2rSDC013F` | dung lượng thẻ (0/0 = chưa cắm) |
| `read.sdcard_alt` | SDC | `#TPUD2rSDC003E` | biến thể data=00, chưa rõ khác gì |
| `read.thermal_spatial_nr` | TAR | `#TPUD2rTAR004B` | khử nhiễu không gian 0–100 |
| `read.thermal_shutter` | TAS | `#TPUD2rTAS004C` | chu kỳ shutter 5–100 |
| `read.thermal_detail` | TDI | `#TPUD2rTDI0045` | tăng chi tiết 0–100 |
| `read.thermal_gamma` | TGM | `#TPUD2rTGM004C` | gamma 0–100 |
| `read.thermal_brightness` | TIB | `#TPUD2rTIB0043` | độ sáng 0–100 |
| `read.thermal_contrast` | TIC | `#TPUD2rTIC0044` | tương phản 0–100 |
| `read.thermal_temporal_nr` | TTR | `#TPUD2rTTR005E` | khử nhiễu thời gian 0–100 |
| `read.thermal_scene` | TSM | `#TPUD2rTSM0058` | scene mode — C12 có thể không có |
| `read.ranging` | SLR | `#TPUD2rSLR0055` | đo xa laser — bytecode nói chỉ C13/C14 |
| `read.ext_config` | EXT | `#TPUD2rEXT0055` | LED / OSD / hiệu chuẩn |
| `read.video_config` | VOM | `#TPUD2rVOM0056` | flip, fps, GOP, bitrate |
| `read.image_quality` | IQE | `#TPUD2rIQE0043` | tinh chỉnh chất lượng ảnh |
| `read.ip_address` | IPV | `#TPUD2rIPV0053` | IP camera — **chỉ đọc** |
| `read.gateway` | GTW | `#TPUD2rGTW0056` | gateway — **chỉ đọc** |

Lệnh camera không hỗ trợ sẽ **im lặng** cho tới hết timeout. Im lặng chính là câu
trả lời, không phải lỗi.

### 🟡 REVERSIBLE — ghi cho camera, đổi được lại

| Tên | cmd3 | Khung ví dụ | |
|---|---|---|---|
| `camera.snap` | CAP | `#TPUD2wCAP013E` | chụp 1 ảnh vào thẻ |
| `camera.record_start` | REC | `#TPUD2wREC0144` | bắt đầu quay |
| `camera.record_stop` | REC | `#TPUD2wREC0043` | dừng quay |
| `camera.zoom_in` | DZM | `#TPUD2wDZM0A65` | zoom vào 1 bước |
| `camera.zoom_out` | DZM | `#TPUD2wDZM0B66` | zoom ra 1 bước |
| `camera.palette` | IMG | `#TPUD2wIMG044A` | palette = IRONBOW |
| `camera.resolution` | VID | `#TPUD2wVID014D` | độ phân giải = 1080P |
| `camera.thermal_brightness` | TIB | `#TPUD2wTIB3C5E` | độ sáng = 60 |
| `camera.thermal_contrast` | TIC | `#TPUD2wTIC3753` | tương phản = 55 |
| `camera.thermal_spatial_nr` | TAR | `#TPUD2wTAR3255` | khử nhiễu KG = 50 |
| `camera.thermal_shutter` | TAS | `#TPUD2wTAS1E67` | shutter = 30 |
| `camera.thermal_detail` | TDI | `#TPUD2wTDI324F` | chi tiết = 50 |
| `camera.thermal_gamma` | TGM | `#TPUD2wTGM3256` | gamma = 50 |
| `camera.thermal_temporal_nr` | TTR | `#TPUD2wTTR3268` | khử nhiễu TG = 50 |
| `telemetry.push_attitude` | GAA | `#TPUG2wGAA0A46` | đẩy GAC 10 Hz (0 = tắt) |

**Palette nằm ở `IMG`, không phải `TAR`.** `TAR` là khử nhiễu không gian — quét
nhầm vào đó là phá cấu hình cảm biến.

11 palette: `WHITE_HOT 01` · `SEPIA 03` · `IRONBOW 04` · `RAINBOW 05` ·
`NIGHT 06` · `AURORA 07` · `RED_HOT 08` · `JUNGLE 09` · `MEDICAL 0A` ·
`BLACK_HOT 0B` · `GLORY_HOT 0C` (giá trị `02` bỏ trống trong bytecode).

Độ phân giải: `720P 00` · `1080P 01` · `2K 02` · `4K 03`.

### 🟠 PHYSICAL — làm gimbal quay, **chỉ chạy khi đã ARM**

| Tên | cmd3 | Khung ví dụ | Tham số |
|---|---|---|---|
| `gimbal.yaw_speed` | GSY | `#TPUG2wGSY1464` | +10 °/s (âm = trái) |
| `gimbal.pitch_speed` | GSP | `#TPUG2wGSPF672` | −5 °/s (âm = xuống) |
| `gimbal.speed` | GSM | `#TPUG4wGSM14F6D6` | yaw+pitch 1 gói, **cần firmware ≥ 0.5** |
| `gimbal.goto_yaw` | GAY | `#TPUG6wGAY0BB8103E` | yaw tuyệt đối 30° |
| `gimbal.goto_pitch` | GAP | `#TPUG6wGAPF830102A` | pitch tuyệt đối −20° |
| `gimbal.goto` | GAM | `#TPUGCwGAM0BB810F8301081` | cả hai trục, 30° / −20° |
| `gimbal.akey` | PTZ | `#TPUG2wPTZ056F` | một phím: CENTER |

`PTZ` chỉ được dùng `01`–`05` (UP DOWN LEFT RIGHT CENTER). `06`–`08` đổi control
mode, `0A`/`0B` đổi mount mode, **`0C`/`0D` khởi động hiệu chuẩn gimbal** — tất cả
nằm trong danh sách chặn cứng.

**Khung DỪNG KHẨN** (dựng sẵn, không tra registry lúc cần):

```
#TPUG2wGSY005F      yaw  = 0
#TPUG2wGSP0056      pitch = 0
```

Gửi **3 lần mỗi khung** — UDP mất gói, gửi một lần là không đủ.

### 🔴 DANGEROUS — không có trong registry, không gửi được

| cmd3 | Vì sao cấm |
|---|---|
| `IPV` ghi | đổi IP camera. Sai là mất thiết bị vĩnh viễn: không UART, không nút reset, chỉ còn cách quét lại subnet và cầu may |
| `GTW` ghi | đổi gateway. Hậu quả như IPV |
| `VOM` ghi | đổi cấu hình luồng video. Hỏng RTSP là mất luôn video lẫn khả năng chẩn đoán |
| `IQE` ghi | đổi cấu hình encoder. Rủi ro như VOM |
| `RST` | reboot camera. Cần reboot thì rút điện |
| `RTF` | factory reset |
| `GAR` | góc roll — bytecode ghi "không khuyến nghị" |
| `TIM` | đặt giờ, định dạng chưa xác minh |
| `FCC` / `ZMC` | motor lấy nét / zoom quang — dành cho model ống kính cơ, C12 không có |

Đọc `IPV`/`GTW`/`VOM`/`IQE` thì an toàn — chỉ cấm **ghi**.

---

## 5. Cách gửi

### Đường 1 — socket thuần, khi cần chứng minh giao thức

```python
import socket
def checksum(b): return "%02X" % (sum(b.encode()) & 0xFF)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("", 0))                       # cổng 0 = để OS chọn, tránh đụng 5000
s.settimeout(1.0)

body  = "#TPUD2rMOD00"                # đọc model
frame = body + checksum(body)
s.sendto((frame + "\r\n").encode(), ("192.168.144.108", 5000))   # NHỚ CRLF
print(s.recvfrom(2048)[0])
```

### Đường 2 — CLI có sẵn (`c12_ctrl.py`)

```bash
python3 c12_ctrl.py version              # đọc firmware
python3 c12_ctrl.py snap                 # chụp ảnh
python3 c12_ctrl.py zoom-in
python3 c12_ctrl.py palette IRONBOW
python3 c12_ctrl.py yaw 20               # quay yaw 20 °/s — KHÔNG có watchdog
python3 c12_ctrl.py center               # PTZ 05
python3 c12_ctrl.py attitude 10          # bật đẩy GAC 10 Hz
```

> CLI này là script gốc, **không có ARM, không có watchdog, không có soft limit**.
> Nó tiện để thử một phát; điều khiển thật thì dùng web app.

### Đường 3 — web app (đường dùng thật)

```bash
curl localhost:8000/api/health
curl localhost:8000/api/camera
curl -X POST localhost:8000/api/camera/palette \
     -H 'content-type: application/json' -d '{"args":["IRONBOW"]}'
curl -X POST localhost:8000/api/arm          # mở khoá lệnh 🟠 PHYSICAL
curl -X POST localhost:8000/api/stop         # dừng khẩn, disarm luôn
```

| Endpoint | |
|---|---|
| `GET /api/health` | đích, dry-run, armed, thống kê link |
| `GET /api/commands` | cả registry, kèm mức rủi ro |
| `POST /api/cmd/<name>` | gửi thẳng một lệnh registry, ví dụ `read.model` |
| `GET /api/session` · `POST /api/session/start`·`/stop` | ghi phiên |
| `GET /api/camera` · `POST /api/camera/<action>` | trạng thái / ghi có xác nhận |
| `GET /api/gimbal` · `POST /api/gimbal/max-speed` | trạng thái vòng điều khiển |
| `POST /api/arm` · `/api/disarm` · `/api/stop` | chốt an toàn |
| `WS /ws/control` | điều khiển 20 Hz (xem dưới) |
| `GET /video/<name>` · `/video/<name>/snapshot.jpg` | MJPEG / 1 khung |
| `GET /api/diagnostics/preflight` · `POST /api/diagnostics/sweep` | chẩn đoán |
| `GET /api/debug` · `/api/debug/download` · `/api/debug/tail` | debug log |
| `POST /api/debug/mark` · `/api/debug/log` | đánh dấu / sự kiện trình duyệt |

**Ghi cho camera luôn trả về kết quả ĐỌC LẠI**, không trả "đã gửi":

```json
{"action":"palette","frame":"#TPUD2wIMG044A","kind":"direct","read":"read.palette",
 "expected":"IRONBOW","actual":"IRONBOW","ok":true,"attempts":1,"elapsed_ms":153.2}
```

`ok` có **ba** trạng thái: `true` đúng · `false` gói tới nơi nhưng không có tác
dụng · `null` không xác minh được (lệnh đọc im lặng, hoặc chưa cắm thẻ).

### Đường 4 — WebSocket, điều khiển liên tục

Trình duyệt **không** tick 20 Hz; nó chỉ báo khi *đổi trạng thái*, nhịp nằm ở
backend.

```json
→ {"type":"arm"}                              mở khoá
→ {"type":"state","yaw":10,"pitch":-5}        đặt tốc độ mong muốn
→ {"type":"ping"}                             nhịp tim, mỗi 100 ms khi đang ARM
→ {"type":"stop"}                             dừng khẩn
← {"type":"status", ...}                      backend đẩy về 10 Hz
← {"type":"rejected","detail":"..."}          bị từ chối (chưa ARM)
```

Bốn thứ chạy ngầm cho mọi lệnh gimbal, dù gửi từ đâu:

- **ARM** — lệnh 🟠 PHYSICAL bị từ chối **ở backend** khi phiên chưa arm
- **Watchdog 500 ms** — không có tin gì trong 500 ms là tự dừng và disarm
- **Soft limit ±85°** — chỉ chặn khi tư thế còn tươi; tư thế cũ thì **không**
  dùng để chặn (tin vào số liệu hết hạn còn nguy hiểm hơn là không chặn)
- **Năm ngả dừng khẩn** — nút STOP, phím `Space`/`Esc`, WebSocket đứt, tín hiệu
  `SIGINT`/`SIGTERM`, exception trong vòng điều khiển. Tất cả đi qua đúng một hàm
  `stop_all()`

---

## 6. Cách test

Thang 5 bậc, rủi ro tăng dần. Đừng nhảy bậc.

### Bậc 1 — pytest, không cần gì cả

```bash
.venv/bin/python -m pytest              # 549 test, ~45 giây
.venv/bin/python -m pytest -k gimbal    # chạy riêng một nhóm
```

Bộ test chạy simulator qua **socket UDP thật** và **server uvicorn thật**, nên
xanh nghĩa là cả stack chạy được trên máy đó, không chỉ import được.

### Bậc 2 — simulator, không cần camera

```bash
# terminal 1
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000
# terminal 2
.venv/bin/python -m c12ctl.web.app --host 127.0.0.1 --port 15000 \
    --local-port 0 --http-port 8000 --video synthetic
```

Simulator giả lập được đúng những kiểu hỏng phần cứng sẽ gây ra:

```bash
--chaos-loss 0.3      # mất 30% gói
--chaos-delay 0.5     # phản hồi trễ
--chaos-garbage 0.2   # byte rác
--no-gsm              # firmware gimbal < 0.5
--hold-speed          # gimbal giữ lệnh tốc độ thay vì tự dừng
```

`--hold-speed` đáng chú ý: hai tài liệu nguồn mâu thuẫn về việc gimbal có tự dừng
khi ngừng nhận gói hay không. Vòng điều khiển phải đúng ở **cả hai** kiểu.

Khung video tổng hợp có **vạch quét và đồng hồ** — so vạch trên trình duyệt với
vạch ở nguồn là ra độ trễ end-to-end, không cần dụng cụ gì.

### Bậc 3 — dry-run, in gói thay vì gửi

```bash
.venv/bin/python -m c12ctl.web.app --dry-run --video off
```

Xem khung lệnh sẽ đi ra mà không mở socket. Hợp để kiểm tra lại checksum và thứ
tự tham số trước khi cắm camera.

### Bậc 4 — camera thật, chỉ lệnh đọc

```bash
# 1. mạng đã thông chưa — không gửi một lệnh camera nào
.venv/bin/python -m c12ctl.diagnose --preflight-only

# 2. bản đồ năng lực — chỉ lệnh 2r, an toàn tuyệt đối
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md
```

`diagnose` chạy preflight trước và **dừng** nếu tầng link hỏng — quét lệnh khi cáp
chưa cắm chỉ tạo ra một loạt timeout vô nghĩa.

Kết quả ra `logs/findings.jsonl` (nối thêm mỗi lần, so được giữa các lần chạy) và
một bảng markdown. Lệnh nào trái với kỳ vọng của tài liệu bị đánh dấu **BẤT NGỜ**
ở đầu báo cáo.

### Bậc 5 — gimbal quay thật

**Trước khi ARM: kiểm tra khoảng trống quanh gimbal — dây cáp có thể bị quấn.**

```bash
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec --max-speed 10
```

Thứ tự nên theo:

1. Bấm **Record session** trước — lần đầu chạm phần cứng là lần không lặp lại
   được; có bản ghi thì tua lại xem đã gửi gì ngay trước lúc camera làm gì lạ.
2. Để `--max-speed 10` (thấp có chủ ý). Chỉ nâng sau khi đã **thấy** đường dừng
   chạy đúng.
3. Dùng mode **D-pad**, mỗi lần một trục: "chỉ yaw thôi có quay không?" — câu trả
   lời không bị trục thứ hai lẫn vào.
4. Thử dừng khẩn **trước** khi thử quay lâu: bấm `Space`, xem gimbal đứng lại.
5. Thử rút mạng / đóng tab khi đang quay — watchdog phải tự dừng trong 500 ms.
6. Chỉ khi cả 5 bước trên sạch mới nâng `--max-speed`.

Kiểm tra `GSM` (nếu muốn giảm nửa lưu lượng):

```bash
.venv/bin/python -m c12ctl.web.app --use-gsm --max-speed 5
```

`GSM` **không thăm dò được** — nó là lệnh ghi, không có phản hồi, nên cách duy
nhất để biết firmware có hỗ trợ là ra lệnh quay thật rồi nhìn. Không hỗ trợ thì
gimbal đứng im: bỏ `--use-gsm` là về lại hai gói `GSY`+`GSP` (luôn chạy).

---

## 7. Debug log — thứ để gửi đi khi hỏng

Mỗi lần chạy tự ghi `logs/debug/c12ctl.log`, bật sẵn.

```bash
# đang test, thấy lạ → bấm phím M ngay trên trang web (đóng mốc + chụp trạng thái)
# rồi lấy file:
curl -s http://127.0.0.1:8000/api/debug/download -o c12-debug.log
```

Trong file có 4 thứ:

| | |
|---|---|
| version, git commit, board, OS, backend cv2, **mọi** tham số khởi động | "lúc đó chạy cái gì?" |
| gói TX/RX (trừ luồng 20 Hz chỉ đếm, và gói lặp y hệt thì gộp) | "gửi gì ngay trước lúc đó?" |
| `snap[n]` mỗi 10 s: link, gimbal, telemetry, video, camera, CPU, RAM, nhiệt SoC | "hỏng dần hay hỏng đột ngột?" |
| sự kiện trình duyệt: ARM, đổi mode, từng vector đã lệnh, WS rớt, lỗi JS | "người vận hành bấm gì?" |

Đọc lại:

```bash
grep 'c12ctl.ui'      logs/debug/c12ctl.log   # người vận hành đã làm gì
grep 'snap\[.*video'  logs/debug/c12ctl.log   # video theo thời gian
grep -E 'MARK|WARNING|ERROR' logs/debug/c12ctl.log
```

Xoay vòng 8 MB × 3, không có mật khẩu gì trong đó. Tắt bằng `--no-debug-log`,
ghi đủ mọi gói bằng `--debug-packets`.

---

## 8. Cạm bẫy đã biết

| Triệu chứng | Nguyên nhân thật |
|---|---|
| Không thấy control trên ảnh video | đang ở nhánh `main` — phải `git checkout develop`, rồi Ctrl+Shift+R (cache `index.html`) |
| Lệnh đọc không bao giờ trả lời | thiếu `\r\n` cuối gói |
| `Could not bind UDP port 5000` | app trợ lý / ground station đang giữ cổng. Tắt nó, hoặc chạy `--local-port 0` |
| Ping được camera nhưng lệnh im lặng | firmware không hỗ trợ lệnh đó — im lặng là câu trả lời, xem bản đồ năng lực |
| Ghi xong nhưng không có tác dụng | lệnh ghi C12 **không có phản hồi**. "Đã gửi" ≠ "đã có tác dụng". Luôn đọc lại — đó là lý do `POST /api/camera/*` trả về giá trị đọc lại |
| Gimbal không quay dù đã gửi lệnh | chưa ARM (backend từ chối), hoặc dùng `--use-gsm` mà firmware < 0.5 |
| Gimbal tự dừng sau ~0.5 s | watchdog — client phải gửi `ping` mỗi 100 ms khi đang ARM |
| Gimbal dừng ở ~85° | soft limit, cố ý dừng trước giới hạn cơ ±90° |
| Tốc độ đặt 100 °/s mà chỉ quay ~63 | trần thật là ±63.5 °/s (raw ±127 × 0.5) |
| Khung video đen với `--video live` | sai `--host`, hoặc RTSP chưa lên. `--host` điều khiển **cả** đích UDP lẫn địa chỉ RTSP |
| Video lag dần rồi không dùng được | thiếu `drop=true max-buffers=1` trong pipeline GStreamer |
| Video giật trên Pi | thiếu `--decoder v4l2h265dec` → đang decode bằng phần mềm |
| Ảnh nhiệt bị sai màu | server đang tự tô màu. Mặc định nên **tắt** — C12 tự tô qua `IMG`, camera làm thì ảnh ghi ra thẻ giống hệt màn hình |
| pytest không collect được | máy có ROS Humble trên `PYTHONPATH`; `pytest.ini` đã tắt đích danh các plugin đó |
| `ModuleNotFoundError: cv2` | tạo venv thiếu `--system-site-packages` |

---

## 9. Đọc tiếp

| File | |
|---|---|
| `INSTALL.vi.md` | cài đặt và chạy, từng bước, kèm xử lý sự cố |
| `README.md` | lý do đằng sau từng quyết định thiết kế, theo 7 pha |
| `PHAN_TICH_SDK_C12.md` | phân tích bytecode RCSDK — nguồn của mọi hằng số ở đây |
| `skydroid-c12-protocol.md` | phân tích APK. Mâu thuẫn với bytecode thì **bytecode thắng** |
| `PLAN_WEBAPP_C12.md` | kiến trúc và lộ trình |
| `c12ctl/protocol/registry.py` | nguồn sự thật duy nhất của bảng lệnh |
