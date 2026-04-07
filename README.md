# stock-scanner


```
docker logs -f scanner
```
Xem log real-time (Ctrl+C để thoát)

```docker logs --tail 50 scanner  # Xem 50 dòng log gần nhất```

```docker logs --tail 20 scanner  # Xem 20 dòng log gần nhất```



# PHẦN 1: Tạo file trên GitHub

## 1.1 — Tạo repo mới

Vào github.com → Đăng nhập
Nhấn nút "New" (góc trên bên trái)
Điền:

Repository name: ```stock-scanner```
Visibility: chọn Public


Nhấn "Create repository"


## 1.2 — Tạo file requirements.txt
Trong repo vừa tạo → nhấn "Add file" → "Create new file"

Tên file: ```requirements.txt```
Nội dung:
```
vnstock
pandas
requests
mplfinance
pytz
numpy
matplotlib
pillow
```
Nhấn "Commit changes" → "Commit changes" (xanh lá) để lưu.

## 1.3 — Tạo file Dockerfile
Nhấn "Add file" → "Create new file"

Tên file: ```Dockerfile```
Nội dung:
```
FROM python:3.11-slim
RUN apt-get update && apt-get install -y \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    gcc \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner_full.py .
ENV MPLBACKEND=Agg
CMD ["python", "-u", "scanner_full.py"]
```
Nhấn "Commit changes" để lưu.

## 1.4 — Tạo file scanner_full.py
Nhấn "Add file" → "Create new file"

Tên file: ```scanner_full.py```
Nội dung: 
Copy toàn bộ code trong phần bạn đã paste ở trên, bỏ dòng đầu tiên này đi (vì Docker không dùng lệnh pip kiểu Colab):
```
# Xoá dòng này trước khi paste lên GitHub:
!pip install -U vnstock pandas requests mplfinance pytz
```
Và thay phần cấu hình Bước 2 thành dùng biến môi trường (để API key không lộ trên GitHub):
```
=============================================================================
 BƯỚC 2: CẤU HÌNH
 =============================================================================
import os
VNSTOCK_API        = os.environ.get('VNSTOCK_API')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID')
MY_PERSONAL_CHAT_ID   = os.environ.get('MY_PERSONAL_CHAT_ID')

SCAN_INTERVAL_SEC  = 120
TZ_VN              = pytz.timezone('Asia/Ho_Chi_Minh')

register_user(VNSTOCK_API)
```
Phần còn lại giữ nguyên 100%. Nhấn **"Commit changes"** để lưu.

## 1.5 — Lấy raw link của từng file

Vào từng file → nhấn nút **"Raw"** → copy URL trên trình duyệt.

3 link sẽ có dạng:
```
https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/Dockerfile
https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/requirements.txt
https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/scanner_full.py
Thay TEN_BAN bằng username GitHub của bạn.
```

# PHẦN 2: Cài đặt trên VPS Oracle Ubuntu

## 2.1 — Cài Docker

bash
```
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io curl
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker
```
Kiểm tra cài thành công:

bash
```
docker --version
```
Output đúng: Docker version 24.x.x ...

## 2.2 — Tạo thư mục làm việc

bash
```
mkdir -p ~/scanner
cd ~/scanner
```
## 2.3 — Tải 3 file từ GitHub về VPS

Thay TEN_BAN bằng username GitHub của bạn:
Trong thư mục ~/scanner
bash
```
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/Dockerfile
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/requirements.txt
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/dashboard_server.py
```
Tạo file Tradingview Lightweight:
```
cd ~/scanner
mkdir -p static
curl -L "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js" \
     -o static/lightweight-charts.min.js
```

Kiểm tra đã có đủ 3 file:
bash
```
ls ~/scanner
```
Output đúng: Dockerfile  requirements.txt  scanner_full.py

## 2.4 — Tạo file .env chứa API key

File này chỉ nằm trên VPS, không bao giờ lên GitHub:
```
nano ~/scanner/.env
```
Paste nội dung sau (giữ nguyên thông tin thật của bạn):
```
VNSTOCK_API=vnstock_a9d67fdafadsfad...565
TELEGRAM_BOT_TOKEN=995266867:AAErWl......4MjlQV9Y8KWwfZoowPI
TELEGRAM_CHAT_ID=-100....
MY_PERSONAL_CHAT_ID = ...
```
Nhấn Ctrl+X → Y → Enter để lưu.
Nhấn Ctrl+S → Ctrl+X

Kiểm tra kết quả:
```
cat ~/scanner/.env
```

## 2.5 — Build Docker image

bash
```
cd ~/scanner
docker build -t stock-scanner . 
```
Chú ý: phải có dấu chấm để build từ thư mục hiện tại
Chờ 3–5 phút. Thành công khi thấy dòng cuối:

Successfully tagged stock-scanner:latest

## 2.6 — Chạy container
bash
```
docker run -d \
  --name scanner \
  --restart unless-stopped \
  --env-file ~/scanner/.env \
  stock-scanner
```

```
docker run -d \
  --name scanner \
  --restart unless-stopped \
  --env-file ~/scanner/.env \
  -p 8888:8888 \
  stock-scanner
```
## 2.7 — Kiểm tra đang chạy đúng

bash
```
docker logs -f scanner
```

Output đúng ngoài giờ giao dịch:
[22:10:05] ⏸  Ngoài giờ giao dịch → Đợi đến 09:00 ngày mai. Ngủ 120s...
Nhấn Ctrl+C để thoát xem log. Container vẫn chạy nền.

Sau khi chạy hoàn thành< NÊN xoá caches:
```
docker system prune -f
```

# PHẦN 3: Cập nhật khi sửa code trên GitHub

Quy trình mỗi lần thay đổi:
## Bước A — Trên GitHub (máy tính bất kỳ):

Vào repo → click vào file cần sửa → nhấn biểu tượng bút chì (Edit)
Sửa nội dung → nhấn "Commit changes"

## Bước B — Trên VPS, chạy đúng 1 lệnh này:

Để đảm bảo không còn "rác" hệ thống (các layer build lỗi hoặc volume không tên), hãy chạy lệnh dọn dẹp hệ thống Docker:
```
docker system prune -f
```
bash // Chú ý sửa link file tải trước khi chạy
```
cd ~/scanner && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
docker stop scanner && \
docker rm scanner && \
docker build --no-cache -t stock-scanner . && \
docker run -d --name scanner --restart unless-stopped --env-file ~/scanner/.env stock-scanner && \
echo "✅ Cập nhật hoàn tất!" && \
docker logs --tail 20 scanner
```
Đối với chạy cả dash.board_server.py, thì dùng đoạn dưới:
```
cd ~/scanner && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/dashboard_server.py && \
docker stop scanner 2>/dev/null || true && \
docker rm scanner 2>/dev/null || true && \
docker build --no-cache -t stock-scanner . && \
docker run -d --name scanner --restart unless-stopped --env-file ~/scanner/.env -p 8888:8888 stock-scanner && \
echo "✅ Cập nhật hoàn tất!" && \
docker logs --tail 20 scanner
```

```
cd ~/scanner && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/dashboard_server.py && \
sync && sleep 2 && \
docker stop scanner 2>/dev/null || true && \
docker rm scanner 2>/dev/null || true && \
docker build --no-cache -t stock-scanner . && \
docker run -d --name scanner --restart unless-stopped --env-file ~/scanner/.env -p 8888:8888 stock-scanner && \
echo "✅ Cập nhật hoàn tất!" && \
docker logs --tail 20 scanner

```
Lệnh này tự động làm 6 việc theo thứ tự:

Tải scanner_full.py mới nhất từ GitHub
Dừng container cũ
Xoá container cũ
Build lại image
Chạy container mới
In 20 dòng log cuối để xác nhận


Sau khi chạy hoàn thành< NÊN xoá caches:
```
docker system prune -f
```

## Lưu lệnh update thành shortcut (làm 1 lần, dùng mãi):

bash
```
echo 'alias update-scanner="cd ~/scanner && curl -O https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/scanner_full.py && docker stop scanner && docker rm scanner && docker build -t stock-scanner . && docker run -d --name scanner --restart unless-stopped --env-file ~/scanner/.env stock-scanner && docker logs --tail 20 scanner"' >> ~/.bashrc
source ~/.bashrc
```
Từ đó, mỗi lần update chỉ gõ:
bash
```
update-scanner
```
Tóm tắt 3 lệnh VPS cần nhớ
bashdocker logs -f scanner      # Xem log real-time
docker restart scanner      # Khởi động lại không cần rebuild
update-scanner              # Cập nhật code mới từ GitHub




# Xoá và làm lại :

Để đảm bảo môi trường trên VPS Oracle Ubuntu trở lại trạng thái "sạch" hoàn toàn như lúc chưa cài đặt dự án, giúp bạn thực hiện lại từ đầu mà không gặp xung đột dữ liệu cũ, tôi đề xuất quy trình 4 bước xử lý triệt để như sau:

Bước 1: Dừng và xóa bỏ Container

Việc này sẽ ngắt tiến trình đang chạy ngầm và giải phóng tên định danh scanner.

Bash
```
docker stop scanner && docker rm scanner
```
Bước 2: Xóa bỏ Docker Image

Xóa bản đóng gói (Image) để đảm bảo khi bạn làm lại, hệ thống sẽ phải build lại từ đầu thay vì dùng bản cache cũ.

Bash
```
docker rmi stock-scanner
```
(Nếu lệnh này báo lỗi do có nhiều image trùng tên, bạn có thể dùng docker rmi -f stock-scanner để cưỡng ép xóa).

Bước 3: Xóa thư mục dự án và dữ liệu nhạy cảm

Lệnh này sẽ xóa toàn bộ thư mục ~/scanner, bao gồm cả mã nguồn, Dockerfile và quan trọng nhất là tệp cấu hình bí mật .env.

Bash
```
rm -rf ~/scanner
```
Bước 4: Dọn dẹp tài nguyên Docker dư thừa (Tùy chọn nhưng khuyến nghị)

Để đảm bảo không còn "rác" hệ thống (các layer build lỗi hoặc volume không tên), hãy chạy lệnh dọn dẹp hệ thống Docker:

Bash
```
docker system prune -f
```
BÁO CÁO XÁC NHẬN TRẠNG THÁI SẠCH

Sau khi chạy xong các lệnh trên, bạn hãy chạy lệnh kiểm tra cuối cùng:

Kiểm tra Docker: ```docker ps -a``` (Danh sách phải trống hoặc không có mã scanner).

Kiểm tra Image: ```docker images``` (Không còn stock-scanner).

Kiểm tra Thư mục: ```ls ~/scanner``` (Hệ thống phải báo: No such file or directory).

Lưu ý tham mưu: Trước khi thực hiện Bước 3, hãy đảm bảo bạn đã lưu lại các API Key trong tệp .env ở một nơi an toàn (như Notepad trên máy tính) nếu bạn không còn bản lưu nào khác, vì sau khi xóa sẽ không thể khôi phục lại từ VPS.

Giờ đây, hệ thống của bạn đã sẵn sàng để thực hiện lại PHẦN 2 trong hướng dẫn của bạn. Khởi động lại bằng lệnh:

Bash
```
mkdir -p ~/scanner && cd ~/scanner
```

