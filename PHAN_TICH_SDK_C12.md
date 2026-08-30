# Phân tích RCSDK v1.9.2 (Skydroid) — Camera C12

Nguồn phân tích (nằm ngoài thư mục project, ở `Skydroid-C12/rcsdk-demo/`):
- `../rcsdk-demo/README.md` (tài liệu chính thức của hãng)
- `../rcsdk-demo/app/libs/rcsdk-v1.9.2.aar` → `classes.jar` (dịch ngược bằng `javap -c`)
- `../rcsdk-demo/app/src/main/java/...` (demo Android)

---

## 1. Kết luận quan trọng nhất: SDK KHÔNG cấp video

Đã grep toàn bộ 1113 class trong `classes.jar`: **không có một tham chiếu nào tới `rtsp`, `rtmp`, `MediaCodec`, decoder hay bất kỳ URL luồng video nào.**

Các thư viện native đi kèm cũng không phải video:

```
libar8030_helper.so, libar8030_client.so, libar8030_ota.so   → chip radio AR8030 (link G-series)
libsky_serialport.so, libsky_ar8030_comm.so                  → serial / IPC
```

RCSDK chỉ làm 3 việc:

| Thành phần | Nhiệm vụ |
|---|---|
| `RCSDKManager` + `KeyManager` | Kết nối & cấu hình **tay điều khiển** (jostick, đối tần, tín hiệu) |
| `PipelineManager` | Ống dữ liệu trong suốt tới **flight controller** (MAVLink) |
| `PayloadManager` | **Điều khiển gimbal-camera** qua giao thức ASCII `#TP...` |

→ **Video là một luồng hoàn toàn tách biệt.** Camera C12 phát stream trên mạng `192.168.144.x`; ứng dụng phải tự mở stream bằng player riêng (GStreamer/FFmpeg/VLC/ExoPlayer). SDK chỉ dùng để *cấu hình* stream (độ phân giải, bitrate, GOP, lật ảnh) chứ không *tải* stream.

### Cách lấy video trên Rubik Pi 3

C12 nối bằng Ethernet/Wi-Fi, IP mặc định `192.168.144.108`, gateway `192.168.144.1` (xác nhận bởi các lệnh `IPV`/`GTW` trong SDK — xem §4.6).

Bước 1 — đặt IP tĩnh cùng subnet cho Rubik Pi:

```bash
sudo ip addr add 192.168.144.20/24 dev eth0
sudo ip link set eth0 up
ping -c3 192.168.144.108
```

Bước 2 — **dò cổng stream thực tế** (đừng đoán, SDK không ghi URL):

```bash
sudo nmap -Pn -p 554,8554,8000-8900 192.168.144.108
```

Bước 3 — thử các URL RTSP phổ biến của dòng gimbal này (**cần kiểm chứng trên thiết bị thật**, đây là các mẫu thường gặp chứ không lấy từ SDK):

```bash
ffprobe -rtsp_transport tcp rtsp://192.168.144.108:554/stream=0    # visible
ffprobe -rtsp_transport tcp rtsp://192.168.144.108:554/stream=1    # thermal
ffprobe -rtsp_transport tcp rtsp://192.168.144.108:8554/main
```

Nếu không có RTSP, camera có thể đẩy UDP/RTP thẳng tới ground station — bắt gói để xác định:

```bash
sudo tcpdump -i eth0 -n host 192.168.144.108 and udp
```

Bước 4 — phát/decode phần cứng trên QCS6490 (Rubik Pi 3):

```bash
# Hiển thị, decode bằng HW (v4l2)
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.108:554/stream=0 latency=100 \
  ! rtph265depay ! h265parse ! v4l2h265dec ! waylandsink

# Lưu file không re-encode
gst-launch-1.0 rtspsrc location=rtsp://192.168.144.108:554/stream=0 \
  ! rtph265depay ! h265parse ! mp4mux ! filesink location=cap.mp4
```

Lưu ý: có `VideoStreamType {Visible, Thermal}` trong SDK → camera có **2 luồng**, thường là 2 URL/2 cổng khác nhau.

---

## 2. Kiến trúc điều khiển camera trong SDK

```
C12  (com.skydroid.rcsdk.common.payload.C12)      ← API công khai
 ├── topCameraCore            → TopCameraCore           (lệnh CAMERA, tiền tố "#TPUD")
 └── skydroidGimbalControlCore → SkydroidGimbalControlCore (lệnh GIMBAL, tiền tố "#TPUG")
        ↓ writeData(String)
     BasePayload → Pipeline (UDP 5000 ↔ 192.168.144.108:5000)
```

`C12.setZoomRatios()` chỉ là wrapper một dòng gọi thẳng `TopCameraCore.setZoomRatios()`. **Toàn bộ giá trị thật nằm ở 2 class core** — và chúng chỉ sinh ra chuỗi ASCII. Nghĩa là bạn có thể bỏ hẳn SDK Android và nói chuyện trực tiếp với camera bằng UDP từ Rubik Pi.

Khởi tạo trong Android (tham chiếu):

```kotlin
val c12 = PayloadManager.getUDPPayload(PayloadType.C12, 5000, "192.168.144.108", 5000) as C12?
c12?.setCommListener(listener)
PayloadManager.connectPayload(c12)
```

`PayloadType.COMMON` (`CommonPayload`) là lớp gộp mọi lệnh cho C10/C11/C12/C13/C14 — dùng cái này nếu muốn code chung cho nhiều model.

---

## 3. Giao thức dây (wire protocol) — phần quan trọng để port sang Linux

### 3.1 Khung lệnh

```
#TP <SRC><DST> <LEN> <RW> <CMD3> <DATA...> <CRC2>
```

| Trường | Độ dài | Ý nghĩa |
|---|---|---|
| `#TP` | 3 | Ký tự bắt đầu cố định |
| `SRC` | 1 | `U` = nguồn (UAV/Ground unit) |
| `DST` | 1 | `D` = **camera**, `G` = **gimbal** |
| `LEN` | 1 | Số ký tự của `DATA`, viết dạng hex 1 ký tự (`2`, `4`, `6`, `B`=11, `C`=12, `F`=15) |
| `RW` | 1 | `w` = write/set, `r` = read/get |
| `CMD3` | 3 | Mã lệnh, ví dụ `DZM`, `CAP`, `REC` |
| `DATA` | LEN | Tham số dạng hex ASCII in hoa |
| `CRC2` | 2 | Checksum |

Mỗi gói được kết thúc bằng `\r\n` khi ghi xuống socket (thấy trong `sendCmdData$lambda-6`).

### 3.2 Thuật toán checksum (trích từ `TopCameraCore.genSendControlCmd`)

```python
def checksum(body: str) -> str:
    return body + "%02X" % (sum(body.encode("utf-8")) & 0xFF)
```

Cộng toàn bộ byte UTF-8 của phần thân (kể cả `#TP`), lấy 8 bit thấp, in hex **chữ hoa 2 ký tự**.

Đã kiểm chứng khớp 100% với các ví dụ trong README của hãng:
`#TPUD2wCAP01` → `3E` ✓ · `#TPUD2wREC01` → `44` ✓ · `#TPUD2rVER00` → `51` ✓

### 3.3 Đọc phản hồi

`getResultArgument()` cắt chuỗi trả về theo `\r` / `\n`, tìm gói có `CMD3` khớp rồi lấy phần `DATA`. Camera trả về cùng dạng khung, ví dụ đọc zoom trả `#TPDU2rDZM<xx><crc>`.

---

## 4. Bảng lệnh CAMERA (`#TPUD...`) — trích từ `TopCameraCore`

### 4.1 Zoom (mã `DZM`) ⭐

| Hàm SDK | Chuỗi gửi (đã có checksum) | Ghi chú |
|---|---|---|
| `addZoomRatios()` | `#TPUD2wDZM0A65` | Zoom **in** 1 bước (vô cấp, từ v1.5.2) |
| `subtractZoomRatios()` | `#TPUD2wDZM0B66` | Zoom **out** 1 bước |
| `setZoomRatios(0)` | `#TPUD2wDZM0054` | Ảnh gốc |
| `setZoomRatios(1)` | `#TPUD2wDZM0155` | |
| `setZoomRatios(4)` | `#TPUD2wDZM0458` | |
| `getZoomRatios()` | `#TPUD2rDZM004F` | Trả hệ số hiện tại |
| `setZoomForLens(TELE)` | `#TPUD2wDZM0C67` | Chỉ C14 |
| `setZoomForLens(WIDE)` | `#TPUD2wDZM0D68` | Chỉ C14 |

**Điểm cần chú ý (mâu thuẫn trong tài liệu hãng):**
- `setZoomRatios(int)` có kiểm tra cứng trong bytecode: `if (value < 0 || value > 4) → callback lỗi "ZoomRatios"`. Chuỗi gửi là `"#TPUD2wDZM0" + value`, tức **chỉ nhận 0–4**.
- Nhưng `getZoomRatios()` theo README trả về **0–67** cho C12/C13 (0–90 cho C11, 0–150 cho C14).
- → Muốn zoom mượt toàn dải, **dùng `addZoomRatios`/`subtractZoomRatios`** (mỗi lần 1 nấc) và đọc lại bằng `getZoomRatios`, đừng dùng `setZoomRatios`.

### 4.2 Chụp ảnh / quay phim

| Hàm | Chuỗi |
|---|---|
| `takePicture()` | `#TPUD2wCAP013E` |
| `startRecordVideo()` | `#TPUD2wREC0144` |
| `stopRecordVideo()` | `#TPUD2wREC0043` |
| `getRecordVideoState()` | `#TPUD2rREC003E` |
| `getSDCardCapacity()` | `#TPUD2rSDC013F` → `SDCardCapacity(total, remaining)`; cả hai = 0 nghĩa là **chưa cắm thẻ** |

### 4.3 Cấu hình luồng video ⭐

**Độ phân giải — `VID`** (`Resolution`: `R_720P`=0, `R_1080P`=1, `R_2K`=2, `R_4K`=3)

| Lệnh | Chuỗi |
|---|---|
| 720P | `#TPUD2wVID004C` |
| 1080P | `#TPUD2wVID014D` |
| 2K | `#TPUD2wVID024E` |
| 4K | `#TPUD2wVID034F` |
| đọc | `#TPUD2rVID0047` |

**Tham số stream — `VOM`** (`VideoConfig`), khung 11 ký tự data:

```
#TPUDBwVOM <hflip:1> <vflip:1> <frameRate:2hex> <gop:2hex> <bitRate:4hex> <1> <crc>
                                                              ↑ numToHex8
đọc: #TPUD2rVOM0056     (timeout 4000 ms khi set)
```

Cảnh báo của hãng: *"không khuyến nghị sửa các giá trị ngoài flip"* → quy trình an toàn là **get → sửa 1 field → set**.

**Hiệu chỉnh hình ảnh — `IQE`** (`VideoEffectConfig`), 12 ký tự data:

```
#TPUDCwIQE <id> <style> <tone> <brightness> <saturation> <contrastRatio> <sharpness> <crc>
đọc: #TPUD2rIQE0043
```

### 4.4 Ảnh nhiệt (thermal)

Pseudo-color — `IMG`, `#TPUD2wIMG<vv>`:

| Palette | vv | Palette | vv |
|---|---|---|---|
| WHITE_HOT | 01 | JUNGLE | 09 |
| SEPIA | 03 | MEDICAL | 0A |
| IRONBOW | 04 | BLACK_HOT | 0B |
| RAINBOW | 05 | GLORY_HOT | 0C |
| NIGHT | 06 | | |
| AURORA | 07 | RED_HOT | 08 |

Ví dụ: WHITE_HOT = `#TPUD2wIMG0147`, BLACK_HOT = `#TPUD2wIMG0B58`. Đọc: `#TPUD2rIMG0041`.

Các tham số nhiệt khác (đều dạng `#TPUD2w<CMD><vv>`, giá trị **0–100**, riêng `TAS` là **5–100**):

| Mã | Chức năng |
|---|---|
| `TSM` | Thermal scene mode |
| `TAS` | Chu kỳ shutter (5–100) |
| `TDI` | Detail enhancement |
| `TIB` | Brightness |
| `TIC` | Contrast |
| `TAR` | Khử nhiễu không gian |
| `TTR` | Khử nhiễu thời gian |
| `TGM` | Gamma |

### 4.5 Hệ thống

| Hàm | Chuỗi | Ghi chú |
|---|---|---|
| `getCameraVersion()` | `#TPUD2rVER0051` | |
| `getCameraModel()` | `#TPUD2rMOD0044` | |
| `reboot()` | `#TPUD2wRST0062` | |
| `reset()` | `#TPUD2wRTF0156` | Khôi phục mặc định |
| `getRanging()` | `#TPUD2rSLR0055` | Laser đo xa, chỉ C13/C14 |
| `setTime()` | `#TPUDFwTIM<HHmmss><ddMMyy>.00<crc>` | **Chỉ hiệu lực sau khi đã có hình** |
| `setExtConfig()` | `#tpUD4wEXT0<led><osd><calib><crc>` | Lưu ý tiền tố **chữ thường** `#tp` — đúng như trong bytecode |
| `getExtConfig()` | `#TPUD2rEXT0055` | |

### 4.6 Mạng

| Hàm | Chuỗi |
|---|---|
| `setIP()` / `getIP()` | `#TPUD<len>wIPV<ip>` / `#TPUD2rIPV00...` |
| `setGateway()` / `getGateway()` | `#TPUD<len>wGTW<gw>` / `#TPUD2rGTW00...` |

→ Đây là cách đổi IP camera nếu `192.168.144.108` xung đột với mạng của bạn.

---

## 5. Bảng lệnh GIMBAL (`#TPUG...`) — từ `SkydroidGimbalControlCore`

### 5.1 Một phím (`PTZ`)

| `AKey` | Chuỗi |
|---|---|
| `MID` (về giữa) | `#TPUG2wPTZ056F` |
| `TOP` | `#TPUG2wPTZ016B` |
| `DOWN` | `#TPUG2wPTZ026C` |
| `LEFT` | `#TPUG2wPTZ036D` |
| `RIGHT` | `#TPUG2wPTZ046E` |

Các `PTZ` khác: `0A/0B` = chế độ lắp (treo/lật ngược), `06/07/08` = chế độ điều khiển, `0C/0D` = hiệu chuẩn, `0E`–`14` = tinh chỉnh.

### 5.2 Điều khiển tốc độ

```
Yaw:        #TPUG2wGSY <speed_hex_1byte> <crc>
Pitch:      #TPUG2wGSP <speed_hex_1byte> <crc>
Yaw+Pitch:  #TPUG4wGSM <yaw_hex> <pitch_hex> <crc>     (cần firmware gimbal ≥ 0.5)
```

Công thức mã hoá tốc độ (lấy từ bytecode, hằng số `0.5f`, `+127`, `-127`):

```python
raw = max(-127, min(127, int(speed_deg_per_s / 0.5)))   # bù 2 → 1 byte hex
```

→ Dải hợp lệ **−63.5 … +63.5 °/s**, bước 0.5°/s. Yaw: âm = trái, dương = phải. Pitch: âm = xuống, dương = lên.

> ⚠️ Các ví dụ `#TPUG2wGSY1E75` (tốc độ 3°/s) trong README là **lỗi thời** — chúng dùng hệ số 0.1 của bản trước v1.2.1 (changelog v1.2.1 ghi rõ *"điều chỉnh tham số điều khiển tốc độ gimbal"*). Hãy tin bytecode: hệ số hiện tại là **0.5**.

**Quan trọng:** đây là lệnh tốc độ liên tục — gimbal chạy cho đến khi nhận lệnh mới. Muốn dừng phải gửi tốc độ 0 (`#TPUG2wGSY005F` / `#TPUG2wGSP0056`).

### 5.3 Điều khiển góc

```
#TPUG6wGAY <angle*100 : int16 hex> 10 <crc>     yaw
#TPUG6wGAP <angle*100 : int16 hex> 10 <crc>     pitch
#TPUG6wGAR ...                                   roll (không khuyến nghị)
#TPUGCwGAM <yaw> 10 <pitch> 10 <crc>             cả hai
```

Góc nhân 100, clamp `[-9000, +9000]` (±90.00°), int16 bù 2. Hậu tố `10` là tốc độ di chuyển.

Ví dụ: `gotoYaw(30)` → `#TPUG6wGAY0BB8103E`; `gotoPitch(-90)` → `#TPUG6wGAPDCD8104C`.

### 5.4 Đọc tư thế gimbal (`GAA` / `GAC`)

```
Bật push:  #TPUG2wGAA<rate_hex>     rate 0–100 Hz, 0 = tắt
           10 Hz → #TPUG2wGAA0A46 ;  tắt → #TPUG2wGAA0035
```

Sau đó camera tự đẩy gói `GAC` chứa yaw/pitch/roll — parser cắt từng 4 ký tự hex (int16, chia 100 ra độ) → `GimbalAttitud(yaw, pitch, roll)`.

Hãng lưu ý: chỉ set **sau khi gimbal đã kết nối và ra hình**, nên gửi lặp vài lần.

---

## 6. Khuyến nghị triển khai trên Rubik Pi 3

Rubik Pi 3 chạy Linux (không phải Android) → **file `.aar` không dùng được**. Nhưng như đã phân tích, không cần nó:

| Việc | Cách làm trên Rubik Pi |
|---|---|
| Video | GStreamer + `v4l2h264dec`/`v4l2h265dec` (HW decode của QCS6490) |
| Zoom / chụp / quay / cấu hình | Socket UDP thuần tới `192.168.144.108:5000`, sinh chuỗi `#TP...` theo §3–5 |
| Gimbal | Cùng socket đó, prefix `#TPUG` |
| MAVLink tới FC | Không qua SDK — nối serial/UDP trực tiếp tới flight controller |

Script mẫu chạy được ngay: **`c12_ctrl.py`** (cùng thư mục này).

```bash
python3 c12_ctrl.py zoom-in
python3 c12_ctrl.py zoom-get
python3 c12_ctrl.py snap
python3 c12_ctrl.py rec-start
python3 c12_ctrl.py res 1080p
python3 c12_ctrl.py palette BLACK_HOT
python3 c12_ctrl.py yaw 20        # 20 °/s sang phải
python3 c12_ctrl.py yaw 0         # dừng
python3 c12_ctrl.py goto 30 -45   # yaw 30°, pitch -45°
python3 c12_ctrl.py center
```

Nếu Rubik Pi 3 của bạn **chạy Android** thì vẫn nạp được `.aar` — nhưng lưu ý native lib chỉ có `arm64-v8a`/`armeabi-v7a` (đủ cho QCS6490), `minSdk 24`, và phần `RCSDKManager.connectToRC()` chỉ hoạt động khi thiết bị **chính là tay điều khiển Skydroid** (H12/H16/H30/G-series). Trên board thường, chỉ dùng được `PayloadManager` — mà phần đó thì như trên, chỉ là UDP + chuỗi ASCII.

---

## 7. Những cạm bẫy đã ghi trong SDK/README

1. **Xung đột cổng:** phải tắt app trợ lý (Assistant) và ground station trước, nếu không cổng 5000 bị chiếm và link dữ liệu chết.
2. `setTime()` và `setPushAttitudeEnable()` **chỉ hiệu lực sau khi camera đã ra hình**.
3. Lệnh tốc độ gimbal phải **gửi lặp lại** (kiểu deadman) hoặc gửi 0 để dừng.
4. `setZoomRatios` bị chặn ở 0–4 dù dải thật là 0–67 → dùng add/subtract.
5. Đọc cần sinh trắc: `getVideoConfig` timeout mặc định, `setVideoConfig` timeout **4000 ms** (dài hơn hẳn các lệnh khác).
6. `setExtConfig` dùng tiền tố chữ thường `#tpUD` — không phải typo của tôi, đúng như trong bytecode.
