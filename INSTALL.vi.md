# Cài đặt và chạy

> 🇬🇧 English version: [INSTALL.md](INSTALL.md)

Trạm mặt đất chạy trên web cho gimbal-camera Skydroid C12: hai luồng video trực
tiếp, lệnh camera có xác nhận bằng cách đọc lại, điều khiển gimbal, và ghi phiên
đồng bộ.

**Không cần camera vẫn chạy được.** Một simulator UDP và nguồn video tổng hợp
đứng thay phần cứng, nên mọi thứ dưới đây chạy được trên laptop không cắm gì cả.
Toàn bộ dự án cũng đã được phát triển theo đúng cách đó.

Đã kiểm chứng trên Ubuntu 22.04, Python 3.10.12, từ một bản `git clone` sạch.

---

## 1. Yêu cầu

| | |
|---|---|
| Hệ điều hành | Linux. Phát triển trên Ubuntu 22.04; đích triển khai là Rubik Pi 3 (arm64) |
| Python | 3.10 trở lên (mới chỉ kiểm chứng trên 3.10.12) |
| Gói hệ thống | **`python3-opencv`** — cung cấp `cv2` và `numpy` |
| Dùng thêm | `git`, và `ip` / `ping` từ `iproute2` / `iputils-ping` cho phần chẩn đoán mạng |

```bash
sudo apt update
sudo apt install -y git python3-venv python3-opencv
```

`python3-opencv` là gói quan trọng nhất. `cv2` ở đây **không** cài bằng pip — nó
lấy từ gói của bản phân phối, và bước tiếp theo được dựng dựa trên đúng điều đó.

## 2. Cài đặt

```bash
git clone <địa-chỉ-repo> WebAppControlC12
cd WebAppControlC12

python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

**`--system-site-packages` là bắt buộc, không phải tuỳ chọn.** Thiếu nó thì môi
trường ảo không nhìn thấy `cv2` và `numpy` của hệ thống, và tầng video sẽ không
import được. Xem [Xử lý sự cố](#7-xử-lý-sự-cố) để biết lỗi cụ thể.

Sáu gói pip đều nhẹ: FastAPI, uvicorn, pytest, pytest-asyncio, httpx và
websockets.

## 3. Kiểm chứng bản cài

```bash
.venv/bin/python -m pytest
```

Phải thấy **524 test xanh** trong khoảng 45 giây. Bộ test chạy simulator qua
socket UDP thật và một server uvicorn thật, nên xanh nghĩa là **cả stack chạy
được trên máy bạn**, chứ không chỉ là code import được.

## 4. Chạy khi không có phần cứng

Hai terminal, vì simulator là một tiến trình riêng.

```bash
# terminal 1 — camera giả lập
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000

# terminal 2 — app, trỏ vào simulator
.venv/bin/python -m c12ctl.web.app \
    --host 127.0.0.1 --port 15000 --local-port 0 \
    --http-port 8000 --video synthetic
```

Mở <http://localhost:8000>.

Ba tham số ở đó đều có việc thật:

- `--video synthetic` — sinh hai luồng đúng thông số C12 thật (1280×720@30 và
  384×288@25). Mặc định là `--video live`, tức là chờ camera ở địa chỉ RTSP.
- `--local-port 0` — để hệ điều hành tự chọn cổng UDP local. Mặc định là 5000,
  mà simulator chạy cùng máy có thể đang giữ cổng đó.
- `--host 127.0.0.1 --port 15000` — nói chuyện với simulator thay vì camera.

### Nhanh hơn nữa: không cần cả simulator

```bash
.venv/bin/python -m c12ctl.web.app --dry-run --video off
```

`--dry-run` in gói ra log thay vì mở socket. Hợp để xem giao diện và đọc registry
lệnh; không có ai trả lời nên đệm trạng thái camera sẽ rỗng — đúng như thiết kế.

### Thử được gì trên giao diện

- **Camera** — chụp ảnh, ghi hình, zoom, palette, độ phân giải. Mọi lệnh ghi đều
  được đọc lại trước khi hiện kết quả, và báo `confirmed` / `mismatch` /
  `unverified`.
- **Gimbal control** — bấm ARM, rồi kéo cần hoặc dùng WASD / phím mũi tên.
  `Space` và `Esc` là dừng khẩn, bất cứ lúc nào.
- **Record session** — ghi video, lưu lượng lệnh và tư thế vào
  `logs/sessions/<id>/` trên cùng một đồng hồ.
- **Preflight** và **Sweep reads** — chẩn đoán mạng và giao thức.

## 5. Chạy với C12 thật

Đọc hết mục này trước khi cắm bất cứ thứ gì.

> Phải làm xong bước 1–3 trước. Mọi lệnh bên dưới đều chạy bằng
> `.venv/bin/python`, thứ chưa tồn tại cho tới khi bạn tạo môi trường ảo.

### Đấu nối

- Camera cần **7.2–72 V qua đầu JST-2P**. RJ45 **không** cấp nguồn.
- Cáp Skydroid chỉ có 4 lõi (TX±/RX±) nên link sẽ chạy ở 100 Mb/s. Đó là bình
  thường, không phải lỗi.

### Mạng

Camera ở `192.168.144.108` và **không chạy DHCP server**, nên host phải tự gán IP
tĩnh cùng dải:

```bash
sudo ip addr add 192.168.144.20/24 dev enp8s0   # thay bằng tên interface của bạn
sudo ip link set enp8s0 up
```

### Trình tự khởi động, rủi ro tăng dần

```bash
# 1. chẩn đoán mạng — không gửi một lệnh camera nào
.venv/bin/python -m c12ctl.diagnose --preflight-only

# 2. bản đồ năng lực — chỉ lệnh đọc, an toàn tuyệt đối
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md

# 3. video thật
.venv/bin/python -m c12ctl.web.app --video live

# 4. đầy đủ, giữ tốc độ gimbal ở mức thấp
.venv/bin/python -m c12ctl.web.app --video live --max-speed 10
```

`diagnose` chạy preflight trước và **dừng** nếu tầng link hỏng: quét lệnh trong
khi cáp chưa cắm chỉ tạo ra một loạt timeout vô nghĩa.

Nên bấm ghi phiên trước khi làm bước 2. Lần đầu tiếp xúc phần cứng thật là lần
chạy không lặp lại được; nếu camera làm gì đó bất ngờ, bản ghi cho phép tua lại
xem đã gửi gì ngay trước đó.

### Hình ảnh camera hiện ra ở đâu

Đúng hai khung bạn đã thấy với nguồn tổng hợp — ở đầu trang, ngay dưới thanh nút.
Giao diện không đổi gì cả; chỉ nguồn khung hình đổi. Nút **Video** và **Layout**
vẫn hoạt động y hệt.

```
C12 ──RTSP :554 stream=1──┐
     (1280×720 @30, H.265) │
                           ├→ cv2 decode → FrameBus → encode JPEG → <img src="/video/visible">
C12 ──RTSP :555 stream=2──┘   (mỗi luồng    (giữ 1     (1 lần cho     <img src="/video/thermal">
     (384×288 @25, H.265)       1 thread)     khung)     mọi client)
```

Hai luồng đi **hai cổng RTSP khác nhau** và chạy độc lập hoàn toàn. Ảnh nhiệt
384×288 được server phóng ×2 trước khi gửi ra.

Lệnh chạy *đơn giản hơn* lệnh dùng simulator, vì `--video live` vốn đã là mặc
định:

```bash
.venv/bin/python -m c12ctl.web.app
```

App sẽ mở đúng hai URI này:

```
visible  rtsp://192.168.144.108:554/stream=1
thermal  rtsp://192.168.144.108:555/stream=2
```

Hai điều dễ vấp:

- **Đừng truyền `--video synthetic`.** Cắm camera vào mà vẫn để cờ đó thì bạn vẫn
  đang xem ảnh do máy sinh ra.
- **`--host` điều khiển cả hai đường** — nó vừa là đích lệnh UDP, vừa là địa chỉ
  RTSP. Trỏ vào simulator bằng `--host 127.0.0.1` thì không có video thật, vì ở
  đó không có RTSP server nào. Với camera thật, cứ để mặc định.

Trên Rubik Pi 3, thêm `--decoder v4l2h265dec` để decode bằng phần cứng.

### Phân biệt hình thật với hình giả lập

Nguồn tổng hợp nhìn là biết: dải màu SMPTE, đồng hồ và một vạch quét chạy ngang.
Chắc chắn hơn thì gọi `GET /api/video`, trường `uri` của mỗi luồng sẽ là
`rtsp://…` chứ không phải `synthetic:visible`.

Dòng chữ nhỏ trên mỗi khung là số đo đang chạy thật: fps vào, fps ra, độ trễ, ms
encode, KB mỗi khung. Số nhảy nghĩa là khung đang về.

### Nếu khung video vẫn đen

`frames=0` trong `/api/video`, và `/video/<name>/snapshot.jpg` trả 503, đều có
nghĩa là chưa nhận được gì. Kiểm tra theo thứ tự:

1. Bấm **Preflight** rồi đọc hai thẻ `RTSP 554` và `RTSP 555` — chúng kiểm tra
   trực tiếp cổng video.
2. **Camera đã khởi động xong chưa?** Luồng RTSP chỉ mở sau khi boot xong, không
   mở ngay lúc cấp nguồn.
3. IP của host đã nằm trong dải `192.168.144.x` chưa?
4. Kiểm tra ngoài app: `ffplay rtsp://192.168.144.108:554/stream=1`. Nếu ffplay
   cũng đen thì vấn đề ở camera hoặc mạng, không phải ở app này.

### Trước khi ARM gimbal

- Kiểm tra không gian quanh gimbal đã trống — dây cáp có thể bị quấn.
- Giữ `--max-speed` thấp cho lần chạy đầu. Mặc định 10 °/s là có chủ ý; trần phần
  cứng là 63.5 °/s.
- Đặt sẵn tay trên nút STOP.

## 6. Tham số hay dùng

```bash
.venv/bin/python -m c12ctl.web.app --help
```

| Tham số | Mặc định | |
|---|---|---|
| `--host` / `--port` | `192.168.144.108` / `5000` | đầu UDP của camera |
| `--local-port` | `5000` | cổng UDP local; dùng `0` khi 5000 bị chiếm |
| `--http-port` | `8000` | cổng giao diện web |
| `--bind` | `127.0.0.1` | địa chỉ nghe HTTP — xem cảnh báo bên dưới |
| `--video` | `live` | `live`, `synthetic` hoặc `off` |
| `--decoder` | — | `v4l2h265dec` trên Rubik Pi 3 để decode phần cứng |
| `--max-speed` | `10.0` | trần tốc độ gimbal, °/s |
| `--dry-run` | tắt | in gói ra log thay vì gửi |
| `--packet-log` | — | ghi mọi gói TX/RX ra file JSONL |
| `--record-fps` | `5` | số khung ghi mỗi giây, cho mỗi luồng |
| `--no-gimbal` / `--no-telemetry` / `--no-camera` / `--no-record` | bật | tắt từng phân hệ |

> ⚠️ **App không có xác thực.** Mặc định `--bind 127.0.0.1` chỉ nhận kết nối từ
> chính máy đó. Đổi thành `--bind 0.0.0.0` — ví dụ để mở giao diện từ máy tính
> bảng ngoài hiện trường — là mở quyền điều khiển gimbal cho mọi người trong mạng
> đó. Chỉ làm khi bạn kiểm soát được mạng.

Kết quả ghi xuống `logs/`: `logs/sessions/` cho các phiên,
`logs/findings.jsonl` cho bản đồ năng lực. Cả thư mục đã nằm trong `.gitignore`.

## 7. Xử lý sự cố

### `.venv/bin/python: No such file or directory`

`.venv/` **cố ý không nằm trong repo**. Môi trường ảo lưu đường dẫn tuyệt đối tới
máy đã tạo ra nó, nên bản sao chép sang máy khác hỏng ngay từ đầu — nó phải được
tạo lại trên từng máy. Vì vậy bản clone mới hoàn toàn chưa có `.venv` cho tới khi
bạn chạy [bước 2](#2-cài-đặt):

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Mọi lệnh trong hướng dẫn này đều mở đầu bằng `.venv/bin/python`, nên đây là lỗi
đầu tiên một bản clone mới gặp phải — kể cả khi bạn nhảy thẳng xuống trình tự
khởi động ở [bước 5](#5-chạy-với-c12-thật).

Hai dấu hiệu phân biệt lỗi này với lỗi ở tầng Python: thông báo do shell in ra
(có tiền tố `bash:`) chứ không phải một traceback, và mã thoát là 127.

Riêng `diagnose --preflight-only` **không cần** `python3-opencv` — nó không chạm
tới tầng video — nên chạy được cả trên máy tối giản chỉ mới cài `python3-venv`.

### `ModuleNotFoundError: No module named 'cv2'`

Môi trường ảo đã được tạo mà thiếu `--system-site-packages`. Tạo lại:

```bash
rm -rf .venv
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Nếu vẫn lỗi thì `cv2` chưa được cài ở mức hệ thống:
`sudo apt install python3-opencv`, rồi kiểm tra bằng
`python3 -c "import cv2; print(cv2.__version__)"`.

### `Could not bind UDP port 5000: [Errno 98] Address already in use`

Cổng 5000 rất hay bị app trợ lý hoặc một ground station khác chiếm. Tìm thủ phạm
bằng `ss -lunp | grep 5000` rồi tắt nó, hoặc đơn giản là chạy với
`--local-port 0`.

### Preflight báo hỏng và `diagnose` dừng lại

Đó là hành vi đúng khi chưa cắm camera. Phần in ra nêu rõ từng vấn đề kèm lệnh để
sửa — thường là gán IP tĩnh ở mục 5. Nếu vẫn muốn quét, thêm `--skip-preflight`.

### Khung video đen khi dùng `--video live`

Xem [Nếu khung video vẫn đen](#nếu-khung-video-vẫn-đen) để có thứ tự kiểm tra đầy
đủ. Lưu ý việc máy dev này thiếu các phần tử H.265 của GStreamer **không phải**
nguyên nhân: `cv2` được build kèm FFMPEG nên decode RTSP H.265 trực tiếp được.

### Máy có cài ROS thì pytest không collect được

`pytest.ini` đã tắt đích danh các plugin của ROS (`launch_testing` phụ thuộc
`lark`, thứ không có trong venv này). Đừng xoá khối `addopts` đó.

## 8. Ghi chú cho Rubik Pi 3

Rubik Pi 3 (Qualcomm QCS6490, arm64) là đích triển khai. Hướng dẫn giống hệt,
thêm đúng một điều — dùng decoder phần cứng:

```bash
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec
```

Hiệu năng video mới chỉ đo trên x86. Đo lại trên Pi chính là phép đo quyết định
việc phần WebRTC của pha 6 có đáng làm hay không; xem [NEXT.md](NEXT.md).

---

## Đọc tiếp ở đâu

| | |
|---|---|
| [README.md](README.md) | từng pha làm gì và vì sao, kèm số đo |
| [NEXT.md](NEXT.md) | trạng thái hiện tại và việc kế tiếp |
| [PLAN_WEBAPP_C12.md](PLAN_WEBAPP_C12.md) | phân tích giao thức, kiến trúc, lộ trình 7 pha |
| [PHAN_TICH_SDK_C12.md](PHAN_TICH_SDK_C12.md) | dịch ngược bytecode RCSDK — nguồn giao thức đáng tin nhất |

Các tài liệu đó viết bằng tiếng Việt. Code, log và giao diện thì bằng tiếng Anh.
