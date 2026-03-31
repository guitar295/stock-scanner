# stock-scanner

2.1 — Cài Docker

bash

sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io curl
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker

Kiểm tra cài thành công:

bash

docker --version
Output đúng: Docker version 24.x.x ...

2.2 — Tạo thư mục làm việc

bash

mkdir -p ~/scanner
cd ~/scanner

2.3 — Tải 3 file từ GitHub về VPS

Thay TEN_BAN bằng username GitHub của bạn:

bash
curl -O https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/Dockerfile
curl -O https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/requirements.txt
curl -O https://raw.githubusercontent.com/TEN_BAN/stock-scanner/main/scanner_full.py

Kiểm tra đã có đủ 3 file:
bash

ls ~/scanner
Output đúng: Dockerfile  requirements.txt  scanner_full.py

2.4 — Tạo file .env chứa API key

File này chỉ nằm trên VPS, không bao giờ lên GitHub:

bashnano ~/scanner/.env

Paste nội dung sau (giữ nguyên thông tin thật của bạn):

VNSTOCK_API=vnstock_a9d67fdafadsfad...565
TELEGRAM_BOT_TOKEN=995266867:AAErWl......4MjlQV9Y8KWwfZoowPI
TELEGRAM_CHAT_ID=-100....

Nhấn Ctrl+X → Y → Enter để lưu.

2.5 — Build Docker image

bash

cd ~/scanner
docker build -t stock-scanner .  // phải có dấu chấm để build từ thư mục hiện tại

Chờ 3–5 phút. Thành công khi thấy dòng cuối:
```
Successfully tagged stock-scanner:latest

2.6 — Chạy container
bash

docker run -d \
  --name scanner \
  --restart unless-stopped \
  --env-file ~/scanner/.env \
  stock-scanner

2.7 — Kiểm tra đang chạy đúng
bash
docker logs -f scanner

Output đúng trong giờ giao dịch:

🚀 Sẵn sàng quét 98 mã: AAA, ACB, ANV...
⚙️  AUTO-SCANNER ĐÃ KÍCH HOẠT
🔄 [09:02:15] BẮT ĐẦU CHU KỲ QUÉT
```

Output đúng ngoài giờ giao dịch:
```
[22:10:05] ⏸  Ngoài giờ giao dịch → Đợi đến 09:00 ngày mai. Ngủ 120s...
Nhấn Ctrl+C để thoát xem log. Container vẫn chạy nền.
