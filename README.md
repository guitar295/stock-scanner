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
bashnano ~/scanner/.env
```
Paste nội dung sau (giữ nguyên thông tin thật của bạn):
```
VNSTOCK_API=vnstock_a9d67fdafadsfad...565
TELEGRAM_BOT_TOKEN=995266867:AAErWl......4MjlQV9Y8KWwfZoowPI
TELEGRAM_CHAT_ID=-100....
```
Nhấn Ctrl+X → Y → Enter để lưu.

## 2.5 — Build Docker image

bash
```
cd ~/scanner
docker build -t stock-scanner .  // phải có dấu chấm để build từ thư mục hiện tại
```
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
## 2.7 — Kiểm tra đang chạy đúng

bash
```
docker logs -f scanner
```
Output đúng trong giờ giao dịch:

🚀 Sẵn sàng quét 98 mã: AAA, ACB, ANV...
⚙️  AUTO-SCANNER ĐÃ KÍCH HOẠT
🔄 [09:02:15] BẮT ĐẦU CHU KỲ QUÉT

Output đúng ngoài giờ giao dịch:
[22:10:05] ⏸  Ngoài giờ giao dịch → Đợi đến 09:00 ngày mai. Ngủ 120s...
Nhấn Ctrl+C để thoát xem log. Container vẫn chạy nền.


# PHẦN 3: Cập nhật khi sửa code trên GitHub

Quy trình mỗi lần thay đổi:
## Bước A — Trên GitHub (máy tính bất kỳ):

Vào repo → click vào file cần sửa → nhấn biểu tượng bút chì (Edit)
Sửa nội dung → nhấn "Commit changes"

## Bước B — Trên VPS, chạy đúng 1 lệnh này:

bash // Chú ý sửa link file tải trước khi chạy
```
cd ~/scanner && \
curl -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
docker stop scanner && \
docker rm scanner && \
docker build -t stock-scanner . && \
docker run -d --name scanner --restart unless-stopped --env-file ~/scanner/.env stock-scanner && \
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
