# Skydroid C12 — Đặc tả giao thức điều khiển & video

Tài liệu dùng làm input cho Claude Code để phát triển app C++ điều khiển camera gimbal Skydroid C12 qua Ethernet.

> **Về độ tin cậy**: mỗi mục được gắn nhãn `[VERIFIED]` (đã kiểm chứng thực tế),
> `[DERIVED]` (rút từ chuỗi trong APK, chưa chạy thử), hoặc `[HYPOTHESIS]`
> (suy luận, **bắt buộc phải verify** trước khi tin). Đừng coi `[HYPOTHESIS]`
> là sự thật khi sinh code — hãy viết code có đường thoát nếu giả định sai.

---

## 1. Kết nối vật lý & mạng

| Mục | Giá trị | Nhãn |
|---|---|---|
| IP camera | `192.168.144.108` (tĩnh, **không** có DHCP server) | `[VERIFIED]` |
| Subnet | `192.168.144.0/24` | `[VERIFIED]` |
| Cổng điều khiển | UDP `5000` | `[VERIFIED]` |
| RTSP khả kiến | `rtsp://192.168.144.108:554/stream=1` | `[VERIFIED]` |
| RTSP nhiệt | `rtsp://192.168.144.108:555/stream=2` | `[VERIFIED]` |
| Nguồn camera | 7.2–72 V qua JST-2P (RJ45 **không** có PoE) | `[VERIFIED]` |
| Tốc độ link | 100 Mb/s (cáp Skydroid chỉ có 4 lõi TX±/RX±) | `[VERIFIED]` |

Host phải tự gán IP tĩnh cùng dải:

```bash
sudo ip addr add 192.168.144.20/24 dev <iface>
```

---

## 2. Thông số luồng video `[VERIFIED]`

Đo bằng `ffprobe`:

| | Khả kiến (554) | Nhiệt (555) |
|---|---|---|
| Codec | HEVC (H.265) Main | HEVC (H.265) Main |
| Pixel format | `yuvj420p` (full-range) | `yuvj420p` (full-range) |
| Độ phân giải | 1280×720 | 384×288 (native sensor, không upscale) |
| Frame rate | 30 fps | 25 fps |

**Ràng buộc thiết kế quan trọng:**

- Hai luồng chạy **lệch frame rate** (30 vs 25) → cần hai pipeline/thread độc lập,
  không dùng chung một vòng lặp lấy frame đồng bộ.
- Luồng nhiệt đã bị nén 8-bit YUV → **dữ liệu radiometric (°C tuyệt đối) đã mất**.
  Không thể đo nhiệt độ từ RTSP. Chỉ có ảnh xám tương đối sau AGC của camera.
- Ảnh nhiệt mặc định là **grayscale**. Tô màu (pseudo-color) làm được ở phía client
  bằng `cv::applyColorMap()`, không cần lệnh gì từ camera.

---

## 3. Định dạng khung lệnh `[VERIFIED]`

Giao thức họ **Topotek**, text thuần ASCII, một lệnh một gói UDP.

```
#TP U D 2 w REC 01 44
│   │ │ │ │  │   │  └── checksum (2 hex hoa)
│   │ │ │ │  │   └───── data (số ký tự = trường length)
│   │ │ │ │  └───────── command word (3 ký tự, hoa)
│   │ │ │ └──────────── 'r' = read, 'w' = write
│   │ │ └────────────── length: số ký tự của data (hex, 1 ký tự)
│   │ └──────────────── địa chỉ đích
│   └────────────────── địa chỉ nguồn ('U' = host/controller)
└────────────────────── header cố định "#TP"
```

### Thuật toán checksum `[VERIFIED]`

Tổng byte ASCII của **toàn bộ phần trước checksum**, lấy `& 0xFF`, in hoa 2 hex.

```cpp
std::string frame(const std::string& body) {
    unsigned sum = 0;
    for (unsigned char c : body) sum += c;
    char cs[3];
    std::snprintf(cs, sizeof cs, "%02X", sum & 0xFF);
    return body + cs;
}
```

Kiểm chứng ngược trên literal lấy nguyên từ APK:

| Literal trong APK | body | checksum tính được | Khớp |
|---|---|---|---|
| `#TPUD2rVER0051` | `#TPUD2rVER00` | `51` | ✅ |
| `#TPUD2wRST0163` | `#TPUD2wRST01` | `63` | ✅ |

> ⚠️ Nhiều chuỗi trong APK là **template chưa có checksum** (app tự cộng lúc runtime).
> Ví dụ `#TPUG6wGAY000063` có length=6 nên data là `000063` (6 ký tự), *không phải*
> data `0000` + checksum `63`. Luôn tự tính checksum, đừng copy đuôi chuỗi trong APK.

### Địa chỉ đích `[DERIVED]`

| Ký tự | Khối | Command word thấy trong APK |
|---|---|---|
| `D` | Camera / xử lý ảnh | REC CAP DZM IMG VID VER HWV MOD SDC IQE VOM GTW IPV EXT SLR RTF RST TAR TAS TDI TGM TIB TIC TSM TTR |
| `G` | Gimbal | PTZ GSY GSP GAA GAY GAP GAR GSM PGM GAM |
| `M` | Motor ống kính | FCC (focus) ZMC (zoom) |

---

## 4. Bảng lệnh — checksum đã tính sẵn

### 4.1 Gimbal, điều khiển hướng `[DERIVED]`

`#TPUG2wPTZxx` — APK có `PTZ00`..`PTZ14` (21 action, khớp enum `PTZAction`).
Ánh xạ dưới đây là `[HYPOTHESIS]` ngoài `00` và `05`:

| Hành động | Gói đầy đủ |
|---|---|
| stop | `#TPUG2wPTZ006A` |
| up | `#TPUG2wPTZ016B` |
| down | `#TPUG2wPTZ026C` |
| left | `#TPUG2wPTZ036D` |
| right | `#TPUG2wPTZ046E` |
| home / center | `#TPUG2wPTZ056F` |

### 4.2 Gimbal, điều khiển tốc độ `[VERIFIED]`

`GSY` = yaw speed, `GSP` = pitch speed. Data là **signed byte**, dải `-99..+99`
(`0x9D`..`0x63`).

| Speed | GSY (yaw) | GSP (pitch) |
|---|---|---|
| +99 | `#TPUG2wGSY6368` | `#TPUG2wGSP635F` |
| +50 | `#TPUG2wGSY3264` | `#TPUG2wGSP325B` |
| +25 | `#TPUG2wGSY1969` | `#TPUG2wGSP1960` |
| 0 | `#TPUG2wGSY005F` | `#TPUG2wGSP0056` |
| −25 | `#TPUG2wGSYE77B` | `#TPUG2wGSPE772` |
| −50 | `#TPUG2wGSYCE87` | `#TPUG2wGSPCE7E` |
| −99 | `#TPUG2wGSY9D7C` | `#TPUG2wGSP9D73` |

> 🔑 **Gimbal KHÔNG giữ lệnh tốc độ.** Nó dừng sau vài chục ms nếu không nhận gói mới.
> Phải **refresh ~20 Hz** (chu kỳ 50 ms) suốt thời gian muốn chuyển động, rồi gửi
> speed 0 để dừng. Đây là ràng buộc kiến trúc: cần một thread điều khiển chạy nền.

### 4.3 Gimbal, góc tuyệt đối `[HYPOTHESIS]`

`GAY` / `GAP` / `GAR` = angle yaw / pitch / roll, length = 6.
Suy đoán cấu trúc: **4 ký tự góc (signed 16-bit hex) + 2 ký tự tốc độ**.

| Mục | Trạng thái |
|---|---|
| Cấu trúc 4+2 | `[HYPOTHESIS]` — dựa trên template `#TPUG6wGAY000063` |
| Đơn vị góc (0.1° hay 0.01°) | **CHƯA BIẾT — phải thử nghiệm** |

Gói để dò đơn vị (gửi rồi đo góc quay thực tế):

```
#TPUG6wGAY0000631A   góc raw 0,     speed 99
#TPUG6wGAY01C26330   góc raw 450    → 45.0° nếu đơn vị 0.1°
#TPUG6wGAY11946329   góc raw 4500   → 45.0° nếu đơn vị 0.01°
#TPUG6wGAYEE6C635D   góc raw -4500
```

Nếu xác nhận được, **ưu tiên dùng góc tuyệt đối** thay vì speed: không cần vòng lặp
20 Hz, và gimbal tự giữ vị trí.

`GSM` (length 4) `[HYPOTHESIS]`: có thể là speed cả yaw+pitch trong một gói
(2 ký tự mỗi trục) — tiện cho joystick. Cần verify.

### 4.4 Camera — lệnh đọc `[DERIVED]`

Toàn bộ lệnh `r` là **read-only, an toàn tuyệt đối**. Dùng để enumerate xem C12
thực sự hỗ trợ lệnh nào (lệnh không hỗ trợ sẽ không phản hồi).

```
#TPUD2rVER0051   version          #TPUD2rTAR004B   thermal ? (ứng viên palette)
#TPUD2rHWV0059   hardware ver     #TPUD2rTAS004C   thermal spatial NR
#TPUD2rMOD0044   mode             #TPUD2rTDI0045   thermal detail enhance
#TPUD2rSDC003E   SD card          #TPUD2rTGM004C   thermal gamma
#TPUD2rREC003E   recording        #TPUD2rTIB0043   thermal brightness
#TPUD2rIMG0041   image            #TPUD2rTIC0044   thermal contrast
#TPUD2rVID0047   video            #TPUD2rTSM0058   thermal scene mode
#TPUD2rDZM004F   digital zoom     #TPUD2rTTR005E   thermal temporal NR
#TPUD2rIQE0043   image quality    #TPUD2rGTW0056   gateway
#TPUD2rVOM0056   ?                #TPUD2rIPV0053   IP address
#TPUD2rEXT0055   ?                #TPUD2rSLR0055   ?
```

### 4.5 Camera — lệnh ghi `[DERIVED]`

| Hành động | Gói |
|---|---|
| Chụp ảnh | `#TPUD2wCAP013E` |
| Dừng ghi | `#TPUD2wREC0043` |
| Bắt đầu ghi | `#TPUD2wREC0144` |
| Toggle ghi | `#TPUD2wREC0A54` |
| Reset | `#TPUD2wRST0163` |

### 4.6 Ống kính `[DERIVED]`

| Hành động | Gói |
|---|---|
| focus stop / + / − | `#TPUM2wFCC003E` / `#TPUM2wFCC013F` / `#TPUM2wFCC0240` |
| zoom stop / in / out | `#TPUM2wZMC005C` / `#TPUM2wZMC015D` / `#TPUM2wZMC025E` |

---

## 5. Thermal palette — phần cần dò

### Danh sách palette `[DERIVED]`

Rút từ resource string của APK, **đúng 11 mục**, khớp con số trong manual C12:

```
0? white_hot    black_hot   ironbow    rainbow    red_hot
   glory_hot    aurora      jungle     medical    sepia      night
```

Cross-check: `showThermalPalettePop$lambda-34` và `$lambda-45` cách nhau 11.

### Điều chưa biết

| Câu hỏi | Trạng thái |
|---|---|
| Command word nào set palette? | `[HYPOTHESIS]` — `TAR` là ứng viên số 1, `TDI` số 2 |
| Index nào ứng với palette nào? | **CHƯA BIẾT** |
| Có đi qua UDP 5000 không? | **CHƯA CHẮC** — xem cảnh báo dưới |

### Gói để sweep `TAR`

```
#TPUD2wTAR0050   #TPUD2wTAR0151   #TPUD2wTAR0252   #TPUD2wTAR0353
#TPUD2wTAR0454   #TPUD2wTAR0555   #TPUD2wTAR0656   #TPUD2wTAR0757
#TPUD2wTAR0858   #TPUD2wTAR0959   #TPUD2wTAR0A61
```

Mở `ffplay rtsp://192.168.144.108:555/stream=2` rồi gửi từng gói, ghi lại màu tương ứng.

### ⚠️ Hai cảnh báo

1. **C12 có thể không hỗ trợ scene mode.** APK có `menu_c12_thermal_palette` nhưng
   `menu_c13_thermal_scene` → 16 scene mode nhiều khả năng là tính năng của C13,
   không phải C12. Đừng đầu tư thời gian vào `TSM`.
2. **Class `ThermalPalette` nằm trong `com/skydroid/rcsdk/`** — đây là SDK cho tay
   điều khiển H12/H16, có thể đi qua kênh khác (không phải Topotek/UDP 5000).
   Nếu sweep `TAR` không có tác dụng, palette có thể **không điều khiển được qua
   Ethernet**, và giải pháp là tô màu ở client (mục 2).

---

## 6. Hạn chế đã biết `[VERIFIED]`

- **Không có telemetry.** Không quan sát thấy phản hồi encoder/IMU trên UDP 5000,
  kể cả khi bind source port 5000. API Ethernet chỉ ra lệnh chuyển động, không trả
  trạng thái góc. → Nếu cần closed-loop theo góc thật, phải gắn IMU ngoài lên thân camera.
- Không đo được nhiệt độ tuyệt đối qua RTSP (xem mục 2).

---

## 7. Yêu cầu cho app C++

### Chức năng tối thiểu

1. Hiển thị **đồng thời** hai luồng RTSP (khả kiến 30fps + nhiệt 25fps).
2. Tô màu ảnh nhiệt phía client, cho phép đổi colormap runtime.
3. Điều khiển gimbal (yaw/pitch) bằng bàn phím hoặc joystick.
4. Chụp ảnh / bắt đầu–dừng ghi.

### Ràng buộc kiến trúc

- Hai decoder độc lập, **không** đồng bộ frame giữa hai luồng.
- Thread điều khiển gimbal riêng, tick 50 ms, gửi lại lệnh speed khi phím còn giữ,
  gửi speed 0 khi nhả phím.
- Socket UDP dùng chung cho mọi lệnh; đọc phản hồi non-blocking (không phải lệnh
  nào cũng trả lời).
- HEVC decode 720p30 bằng CPU khá nặng → dùng hardware decode khi có.

### Pipeline GStreamer tham chiếu

```bash
# Desktop (software decode)
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.108:554/stream=1 latency=200 protocols=tcp ! \
    rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! autovideosink
```

Trên NVIDIA Jetson: thay `avdec_h265` → `nvv4l2decoder` (dùng NVDEC).
Lưu ý codec là **H.265**, không phải H.264 → dùng `rtph265depay` / `h265parse`.

### Tô màu ảnh nhiệt

```cpp
cv::Mat gray, colored;
cv::cvtColor(frame, gray, cv::COLOR_BGR2GRAY);
cv::applyColorMap(gray, colored, cv::COLORMAP_INFERNO);   // hoặc JET, TURBO, HOT
cv::resize(colored, display, cv::Size(768, 576), 0, 0, cv::INTER_NEAREST);
```

`INTER_NEAREST` giữ pixel sắc nét — hợp với nguồn 384×288. Dùng `cv::applyColorMap`
thay vì filter `pseudocolor` của ffmpeg: LUT 256 bậc liên tục, không bị banding.

### Ghi chú triển khai

- Mọi giá trị `[HYPOTHESIS]` phải nằm sau một lớp abstraction để đổi được dễ dàng
  khi verify xong (đặc biệt: ánh xạ PTZ action, cấu trúc/đơn vị của GAY/GAP/GAR,
  command word của palette).
- Viết unit test cho hàm checksum trước tiên — kiểm chứng bằng 2 literal ở mục 3.
- Nên có chế độ `--dry-run` in gói ra stdout thay vì gửi, để debug.

---

## 8. Phân loại mức độ nguy hiểm & lệnh cấu hình

### 8.1 Bảng phân loại rủi ro

Mọi lệnh phải được xếp vào một trong bốn mức. **Web app không được để lẫn các mức
này trong cùng một UI.**

| Mức | Ý nghĩa | Hành vi UI bắt buộc |
|---|---|---|
| 🟢 **SAFE** | Read-only, không đổi trạng thái | Gọi tự do, không cần xác nhận |
| 🟡 **REVERSIBLE** | Đổi trạng thái nhưng khôi phục dễ | Gọi tự do, hiển thị trạng thái hiện tại |
| 🟠 **PHYSICAL** | Gây chuyển động cơ khí | Cần watchdog + nút dừng khẩn |
| 🔴 **DANGEROUS** | Có thể mất kết nối vĩnh viễn | **KHÔNG implement** trong bản đầu |

### 8.2 🟢 SAFE — read-only, dùng tự do

Toàn bộ lệnh `2r`. Không đổi bất kỳ trạng thái nào. Đây là nhóm nên dùng để
enumerate xem C12 hỗ trợ gì.

```
#TPUD2rVER0051   #TPUD2rHWV0059   #TPUD2rMOD0044   #TPUD2rSDC003E
#TPUD2rREC003E   #TPUD2rIMG0041   #TPUD2rVID0047   #TPUD2rDZM004F
#TPUD2rIQE0043   #TPUD2rVOM0056   #TPUD2rGTW0056   #TPUD2rIPV0053
#TPUD2rEXT0055   #TPUD2rSLR0055   #TPUD2rTAR004B   #TPUD2rTAS004C
#TPUD2rTDI0045   #TPUD2rTGM004C   #TPUD2rTIB0043   #TPUD2rTIC0044
#TPUD2rTSM0058   #TPUD2rTTR005E   #TPUD2rSDC013F
```

> `SDC` xuất hiện với cả data `00` lẫn `01` trong APK — có thể là hai sub-query khác nhau.

**Web app nên có một trang "Diagnostics" gọi toàn bộ nhóm này.** Lệnh không được
hỗ trợ sẽ không phản hồi (timeout), nên đây là cách an toàn để lập bản đồ khả năng
thực tế của C12.

### 8.3 🟡 REVERSIBLE — đổi được, khôi phục dễ

| Lệnh | Chức năng | Ghi chú |
|---|---|---|
| `#TPUD2wCAP013E` | Chụp ảnh | Chỉ ghi thêm file vào SD |
| `#TPUD2wREC0144` / `#TPUD2wREC0043` | Bắt đầu / dừng ghi | Luôn có nút dừng |
| `#TPUD2wTAR00..0A` | Palette nhiệt (giả định) | Chỉ đổi hiển thị |
| `#TPUM2wZMC015D` / `#TPUM2wZMC025E` / `#TPUM2wZMC005C` | Zoom in/out/stop | |
| `#TPUM2wFCC013F` / `#TPUM2wFCC0240` / `#TPUM2wFCC003E` | Focus +/−/stop | |
| `#TPUD2wDZM…` | Digital zoom | Data chưa rõ format |

Sweep palette an toàn — xem mục 5.

### 8.4 🟠 PHYSICAL — gây chuyển động cơ khí

| Lệnh | Rủi ro |
|---|---|
| `GSY` / `GSP` (speed) | Gimbal quay liên tục nếu control loop không dừng |
| `PTZ01`..`PTZ14` | Ánh xạ action là `[HYPOTHESIS]` — có thể quay hướng bất ngờ |
| `GAY` / `GAP` / `GAR` (góc) | Sai đơn vị → quay quá biên |
| `GAM` (length 12) | Set cả 3 trục cùng lúc, hậu quả nhân ba |
| `GSM` (length 4) | Chưa xác minh |

**Yêu cầu bắt buộc với web app:**

- Nút **STOP khẩn** luôn hiển thị, gửi `#TPUG2wGSY005F` + `#TPUG2wGSP0056`.
- Watchdog: WS đứt hoặc không có state update trong 500 ms → dừng ngay (mục 9.3).
- Giới hạn speed trong UI ở mức thấp (±30) khi test lần đầu, chỉ mở tới ±99 sau
  khi đã tin tưởng control loop.
- **Không** implement `GAM` cho tới khi `GAY`/`GAP`/`GAR` đã verify xong đơn vị góc.
- Kiểm tra không gian vật lý quanh gimbal trước khi test — dây cáp có thể bị quấn.

### 8.5 🔴 DANGEROUS — có thể mất kết nối vĩnh viễn

**Không đưa các lệnh này vào web app ở giai đoạn đầu.**

| Lệnh | Length | Hậu quả nếu sai |
|---|---|---|
| `#TPUDCwGTW…` | 12 | Đổi gateway → camera có thể không còn liên lạc được |
| `#TPUDCwIQE…` | 12 | Đổi encode config → luồng RTSP có thể hỏng |
| `#TPUDBwIQE…` | 11 | như trên |
| `#TPUDBwVOM…` | 11 | Đổi video output mode |
| `#TPUD2wRST0163` | 2 | Reset — chưa rõ là soft reset hay factory reset |
| `#TPUD2wRTF01` | 2 | Chưa rõ chức năng |

**Vì sao đặc biệt nguy hiểm:** camera chỉ có một đường vào duy nhất là Ethernet.
Không có UART, không có nút reset dễ tiếp cận. Nếu đổi IP/gateway sai, **không có
đường khôi phục** ngoài việc quét lại toàn bộ dải mạng và hy vọng tìm thấy.

Chuỗi `cxc-SettingModel-IP-C01(EPTZ)-applyIPAndGateway-` trong APK xác nhận đây
đúng là chức năng đổi IP. Length 12 gợi ý format IPv4 dạng 12 chữ số liền
(`192168144108`), nhưng **đây là `[HYPOTHESIS]`, chưa verify.**

Nếu về sau vẫn muốn làm, quy trình bắt buộc:

1. Đọc trạng thái hiện tại trước: `#TPUD2rGTW0056`, `#TPUD2rIPV0053`.
2. Đọc code trong jadx để biết chính xác format, **không đoán**:
   ```bash
   grep -rn 'applyIPAndGateway' src_out/sources/
   grep -rn '"GTW"\|TPUDCwGTW' src_out/sources/
   ```
3. Chỉ thử khi đã có phương án khôi phục đã kiểm chứng.

### 8.6 Lệnh cấu hình đáng khám phá (read trước, write sau)

Nhóm này có tiềm năng hữu ích nhưng phải tiếp cận qua đường đọc:

| Lệnh | Giả thuyết | Vì sao đáng quan tâm |
|---|---|---|
| `IQE` (11–12) | Image Quality Encoding | Có thể chỉnh độ phân giải/bitrate của RTSP |
| `VOM` (11) | Video Output Mode | Có thể đổi cấu hình luồng |
| `TIM` (15) | Set thời gian | APK nhúng `NTPUDPClient`. 15 ký tự ≈ `YYYYMMDDHHMMSS` + 1 |
| `MOD` | Mode | Chưa rõ |
| `EXT`, `SLR` | Chưa rõ | |

Cách tiếp cận đúng: gọi lệnh read tương ứng, **quan sát format của phản hồi**, rồi
mới suy ra cách đóng gói data cho lệnh write. An toàn hơn nhiều so với đoán mò.

### 8.7 Yêu cầu implementation cho web app

```
UI phân tầng bắt buộc:
  Tab "Control"      → 🟠 PHYSICAL   (kèm nút STOP luôn hiển thị)
  Tab "Camera"       → 🟡 REVERSIBLE
  Tab "Diagnostics"  → 🟢 SAFE       (enumerate toàn bộ lệnh read)
  🔴 DANGEROUS       → KHÔNG có UI trong bản đầu
```

- Backend phải có **allowlist** command word. Lệnh không nằm trong allowlist bị từ
  chối ở tầng service, không phụ thuộc vào việc frontend có gọi hay không.
- Chế độ `--dry-run` in gói ra stdout thay vì gửi UDP — dùng khi phát triển.
- Log mọi gói đã gửi kèm timestamp, để truy vết khi camera vào trạng thái lạ.

---

## 9. Web app local (tuỳ chọn) — LƯU Ý BẮT BUỘC ĐỌC

Nếu triển khai dưới dạng web app chạy local thay vì app desktop native, có **hai
cạm bẫy kiến trúc** mà nếu bỏ qua sẽ sinh ra code không chạy được.

### 9.1 ⛔ Browser KHÔNG phát được RTSP

Không có trình duyệt nào hỗ trợ RTSP. Đừng sinh code kiểu:

```html
<!-- SAI — sẽ không bao giờ chạy -->
<video src="rtsp://192.168.144.108:554/stream=1"></video>
```

Bắt buộc phải có **lớp bridge ở backend** chuyển RTSP sang định dạng browser hiểu.

| Phương án | Độ trễ | Nhận xét |
|---|---|---|
| **WebRTC** (qua `go2rtc` hoặc `mediamtx`) | ~200 ms | Tốt nhất cho luồng khả kiến 720p30 |
| **MJPEG** (`multipart/x-mixed-replace`) | ~300 ms | Đơn giản nhất, tự code được. Hợp luồng nhiệt 384×288 |
| ~~HLS / DASH~~ | 2–6 s | **KHÔNG dùng** — điều khiển gimbal trễ 3 s là vô dụng |

**Khuyến nghị lai:**
- Luồng khả kiến (1280×720) → WebRTC, vì MJPEG ở độ phân giải này tốn băng thông.
- Luồng nhiệt (384×288) → MJPEG. Độ phân giải nhỏ nên chi phí không đáng kể, và
  tiện vì đằng nào cũng phải tô màu bằng OpenCV ở server (xem 8.4).

### 9.2 ⛔ Điều khiển gimbal KHÔNG dùng REST

Gimbal cần refresh 20 Hz (mục 4.2). Nếu mỗi tick là một HTTP request thì sẽ có
20 req/s với độ trễ thất thường → gimbal giật, không mượt.

**Đúng:** WebSocket + control thread ở backend.

```
Browser                          Backend
   │                                │
   │ ── WS: {yaw:50, pitch:0} ──►   │  (chỉ gửi khi state ĐỔI)
   │    (nhấn phím)                 │
   │                                │  control thread tick 50ms:
   │                                │    while state.yaw != 0:
   │                                │      send_udp(GSY, state.yaw)
   │                                │
   │ ── WS: {yaw:0, pitch:0} ───►   │
   │    (nhả phím)                  │  → send_udp(GSY, 0), dừng lặp
```

Browser **không** tick 20 Hz. Nó chỉ báo state thay đổi; backend giữ nhịp.

### 9.3 🛡️ Watchdog an toàn

Nếu WebSocket đứt (tab đóng, mạng rớt, browser crash) trong lúc gimbal đang quay,
gimbal sẽ tự dừng sau timeout của chính nó — nhưng **không nên dựa vào đó**.
Backend phải:

- Gửi `GSY 0` + `GSP 0` ngay khi WS đóng hoặc lỗi.
- Timeout ở control thread: nếu không nhận state update nào trong 500 ms, tự dừng.

### 9.4 Tô màu ảnh nhiệt — server hay client?

| | Server (OpenCV) | Client (canvas JS) |
|---|---|---|
| Độ phức tạp | Thấp — `cv::applyColorMap` một dòng | Cao — tự viết LUT |
| Băng thông | Gửi ảnh màu (lớn hơn) | Gửi ảnh xám (nhỏ hơn) |
| Đổi colormap | Cần round-trip | Tức thì |

**Khuyến nghị: server.** Ở 384×288 chênh lệch băng thông không đáng kể, và tái sử
dụng được đúng code sẽ dùng cho bản native sau này. Đổi colormap qua WebSocket
message, độ trễ vài trăm ms là chấp nhận được cho thao tác không thường xuyên.

### 9.5 Kiến trúc tổng thể

```
Browser ── WebSocket ────► control state + colormap
        ── WebRTC ───────► luồng khả kiến
        ── MJPEG /thermal► luồng nhiệt (đã tô màu)
                 │
                 ▼
        Backend (FastAPI / aiohttp)
          ├─ control thread, tick 50 ms ──► UDP 5000
          ├─ thermal worker: RTSP 555 → OpenCV → colormap → JPEG
          └─ go2rtc / mediamtx: RTSP 554 → WebRTC
                 │
                 ▼
          C12 @ 192.168.144.108
```

### 9.6 Gợi ý stack

**Backend Python** (khuyến nghị cho web app — nhanh hơn C++ đáng kể ở mảng này):
- `FastAPI` + `uvicorn` — HTTP + WebSocket
- `opencv-python` — đọc RTSP nhiệt, tô màu, encode JPEG
- `socket` stdlib — UDP điều khiển (tái dùng logic từ `c12_probe.py`)
- `go2rtc` — binary riêng, config YAML vài dòng, lo phần WebRTC

Nếu bắt buộc C++: `drogon` hoặc `crow` cho HTTP/WS, `libdatachannel` cho WebRTC.
Nhưng cân nhắc kỹ — chi phí phát triển cao hơn nhiều mà lợi ích không rõ ràng cho
lớp web.

**Frontend:** HTML + JS thuần là đủ. Không cần framework cho một trang điều khiển.
Bắt `keydown`/`keyup` để gửi state, `<img src="/thermal">` cho MJPEG,
`<video>` + WebRTC peer connection cho luồng khả kiến.

### 9.7 ⚠️ Web app không thay thế bản native

Web app hợp làm **prototype để dò protocol** và test nhanh: dễ debug, không cần
build, chạy được từ máy khác trong LAN.

Nhưng nếu đích cuối là app C++ trên Jetson thì bản native vẫn cần thiết:
- Web app thêm độ trễ ở lớp bridge (transcode + network hop).
- Không tận dụng trực tiếp được NVDEC / zero-copy trên Jetson.
- Thêm một tầng phụ thuộc runtime (browser, go2rtc).

Nên coi hai bản là bổ trợ, không phải thay thế nhau.

---

## 10. Tham chiếu ngoài

- Repo dịch ngược C10 (cùng họ Topotek, phần lớn lệnh tương thích):
  `https://github.com/aatonovaz/skydroid-c10-gimbal-control`
  Xem `docs/app_derived_feature_commands.md`.
- APK nguồn: `SkydroidCameraFPV.apk` — package `com.skydroid.camerafpv`,
  SDK nội bộ `com.skydroid.rcsdk`, `com.skydroid.fpvlibrary`.
