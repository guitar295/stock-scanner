# 🚀 HƯỚNG DẪN TRIỂN KHAI & QUẢN TRỊ BOT CHỨNG KHOÁN

## 📦 PHẦN I: CÀI ĐẶT BAN ĐẦU (CHỈ CHẠY 1 LẦN DUY NHẤT)
> [!IMPORTANT]
> **Chỉ chạy đúng 1 lần khi thiết lập máy mới.** Sau này không bao giờ phải chạy lại phần này.

### 🔹 KHỐI 1A: Cài đặt cho MÁY CHỦ VPS (Linux / Ubuntu)
Khối lệnh này tự động:
1. Cài đặt môi trường Python và toàn bộ thư viện cần thiết trên Linux.
2. Tải và kích hoạt dịch vụ **Cloudflare Tunnel (`cloudflared`)** chạy ngầm vĩnh viễn kết nối tên miền.
3. Tạo dịch vụ **Systemd (`scanner.service`)** tự động bật bot khi VPS khởi động lại (chạy ngầm 24/7, tự hồi sinh khi crash).

```bash
sudo apt-get update && \
sudo apt-get install -y python3-pip python3-dev libfreetype6-dev libpng-dev curl tmux && \
pip3 install --break-system-packages --ignore-installed pandas requests mplfinance pytz numpy matplotlib pillow flask && \
\
echo "🌐 Đang cài đặt và kích hoạt Cloudflare Tunnel..." && \
curl -s -L --output /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && \
sudo dpkg -i /tmp/cloudflared.deb && \
sudo cloudflared service install eyJhIjoiNjNjMmQxZmM5Y2Q0MzRiYWUwMzQzZDNhMDM0MDAxMTMiLCJ0IjoiMzJhMGE5NWEtYThiNi00MDk4LThjZWQtM2I1MTJiOTBiYjIyIiwicyI6Ik56UTBOR1V4TmpndE56STNPQzAwTUdZMkxXSTJaRGd0WkdGa01HSTROakV4WkdGaSJ9 2>/dev/null || true && \
sudo systemctl restart cloudflared 2>/dev/null || true && \
\
echo "⚙️ Đang thiết lập Systemd Service cho Bot..." && \
mkdir -p ~/scanner/static ~/scanner/static_data/charts ~/scanner/data/trade-journal && \
sudo bash -c "cat << 'EOF' > /etc/systemd/system/scanner.service
[Unit]
Description=Stock Scanner Bot & Dashboard 24/7
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$HOME/scanner
EnvironmentFile=-$HOME/scanner/.env
ExecStart=$(which python3) -u scanner_full.py
Restart=always
RestartSec=5s
StandardOutput=append:$HOME/scanner/scanner.log
StandardError=append:$HOME/scanner/scanner.log

[Install]
WantedBy=multi-user.target
EOF" && \
sudo systemctl daemon-reload && \
sudo systemctl enable scanner && \
echo "🎉🎉🎉 CÀI ĐẶT HỆ THỐNG VPS BAN ĐẦU HOÀN TẤT 100%!"
```

---

### 🔹 KHỐI 1B: Cài đặt cho MÁY MAC (macOS Native)
> [!NOTE]
> **Thư mục chuẩn trên Mac:** Toàn bộ code đặt tại `~/Desktop/Dashboard_mac`.

Khối lệnh này tự động:
1. Cài đặt toàn bộ thư viện Python cần thiết trên Mac.
2. Tạo sẵn cấu trúc thư mục, app điều khiển bot.

```bash
cd ~/Desktop/Dashboard_mac && python3 setup_mac.py
```

---

## 🚀 PHẦN II: LỆNH THƯỜNG DÙNG (CẬP NHẬT CODE & KHỞI ĐỘNG LẠI)
> [!TIP]
> **Sử dụng mỗi khi có thay đổi về code hoặc muốn khởi động lại Bot.**

### 🔹 KHỐI 2A: Cập nhật code & Khởi động lại trên VPS (Chạy trong 3 giây)
Tự động tải code mới nhất từ GitHub, đồng bộ thư viện và khởi động lại dịch vụ `scanner`:

```bash
cd ~/scanner && \
curl -s -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
curl -s -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/dashboard_server.py && \
[ -f static/lightweight-charts.min.js ] || curl -s -L "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js" -o static/lightweight-charts.min.js && \
sync && \
sudo systemctl restart scanner && \
echo "✅ CẬP NHẬT CODE MỚI & KHỞI ĐỘNG LẠI VPS HOÀN TẤT!" && \
sleep 3 && \
tail -n 25 ~/scanner/scanner.log
```

---

### 🔹 KHỐI 2B: Khởi động lại khi thay đổi code trên MÁY MAC

* **Cách 1 (Bấm 1 chạm từ thanh Dock - Tiện nhất):**
  - Chỉ cần click chuột vào biểu tượng **`Scanner Dashboard.app`** trên thanh Dock.

* **Cách 2 (Chạy trực tiếp có xem Log trong Terminal):**
  ```bash
  cd ~/Desktop/Dashboard_mac && \
  pkill -f "python3.*scanner_full.py" 2>/dev/null || true && \
  python3 scanner_full.py
  ```

* **Cách 3 (Chạy ngầm qua Terminal):**
  ```bash
  cd ~/Desktop/Dashboard_mac && \
  pkill -f "python3.*scanner_full.py" 2>/dev/null || true && \
  nohup python3 -u scanner_full.py > scanner.log 2>&1 &
  ```

---

## 🛠️ PHẦN III: CÁC LỆNH QUẢN LÝ & THEO DÕI THƯỜNG DÙNG

### 1. Xem Log Con Bot Đang Quét Trực Tiếp (Live Stream Realtime)
Theo dõi từng chu kỳ quét 5s, tín hiệu phát hiện và hoạt động gửi tin Telegram:
* **Trên VPS:**
  ```bash
  tail -f ~/scanner/scanner.log
  ```
* **Trên Mac:**
  ```bash
  tail -f ~/Desktop/Dashboard_mac/scanner.log
  ```
> *(Bấm tổ hợp phím `Ctrl + C` để đóng màn hình xem log).*

---

### 2. Xem Nhanh 50 Dòng Log Gần Nhất
* **Trên VPS:**
  ```bash
  tail -n 50 ~/scanner/scanner.log
  ```
* **Trên Mac:**
  ```bash
  tail -n 50 ~/Desktop/Dashboard_mac/scanner.log
  ```

---

### 3. Kiểm Tra Trạng Thái Hoạt Động Của Bot
* **Trên VPS (Kiểm tra qua Systemd):**
  ```bash
  sudo systemctl status scanner
  ```
* **Trên Mac (Kiểm tra tiến trình):**
  ```bash
  pgrep -fl "python3.*scanner_full.py"
  ```

---

### 4. Tắt Tạm Thời Hoặc Bật Lại Bot Thủ Công
* **Trên VPS:**
  ```bash
  # Tắt bot (Khi cần bảo trì)
  sudo systemctl stop scanner

  # Bật lại bot
  sudo systemctl start scanner
  ```
* **Trên Mac:**
  ```bash
  # Cách 1: Click đúp vào icon "Stop Scanner.app"
  # Cách 2 (Dòng lệnh):
  pkill -f "python3.*scanner_full.py"
  ```

---

## 🌐 CẤU HÌNH CLOUDFLARE ĐỒNG BỘ

| Thành phần | Địa chỉ cấu hình | Mục đích |
| :--- | :--- | :--- |
| **Cloudflare Pages** | `https://scanner.guitar295.xx.kg` | Giao diện web chính cho người dùng truy cập |
| **Cloudflare Tunnel** | `https://api.guitar295.xx.kg` $\rightarrow$ `localhost:8888` | Cổng truyền dữ liệu thời gian thực từ VPS |

