# 🚀 HƯỚNG DẪN TRIỂN KHAI & QUẢN TRỊ BOT CHỨNG KHOÁN
> **Kiến trúc Tối tân:** VPS Native Systemd + Cloudflare Tunnel + Cloudflare Pages (Jamstack Siêu Tốc 0.01s)

---

## 🌟 TỔNG QUAN KIẾN TRÚC VẬN HÀNH

* **Frontend (Cloudflare Pages):** Phân phối giao diện HTML/JS và thư viện vẽ Chart TradingView từ 300+ máy chủ CDN tại Việt Nam.
* **Backend (VPS Native Systemd):** Con bot quét 5s liên tục, tính toán VPA, Breakout và nạp sẵn file JSON tĩnh vào bộ đệm.
* **Đường truyền (Cloudflare Tunnel):** Kết nối mã hóa HTTP/3, chống DDoS, bảo mật 100% ẩn IP máy chủ.

---

## 📦 KHỐI 1: CÀI ĐẶT HỆ THỐNG BAN ĐẦU
> [!IMPORTANT]
> **Chỉ chạy đúng 1 lần duy nhất khi thiết lập VPS mới.** Sau này không bao giờ phải chạy lại khối này.

Khối lệnh này tự động:
1. Cài đặt toàn bộ môi trường Python và các thư viện cần thiết.
2. Tải và kích hoạt dịch vụ **Cloudflare Tunnel (`cloudflared`)** chạy ngầm vĩnh viễn.
3. Tạo dịch vụ **Systemd (`scanner.service`)** tự động bật bot khi VPS khởi động lại (tương đương `--restart unless-stopped`).

```bash
sudo apt-get update && \
sudo apt-get install -y python3-pip python3-dev libfreetype6-dev libpng-dev curl tmux && \
pip3 install --break-system-packages pandas requests mplfinance pytz numpy matplotlib pillow flask && \
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
echo "🎉🎉🎉 CÀI ĐẶT HỆ THỐNG BAN ĐẦU HOÀN TẤT 100%!"
```

---

## 🚀 KHỐI 2: KHỞI ĐỘNG & CẬP NHẬT CODE (DÙNG THƯỜNG XUYÊN)
> [!TIP]
> **Dùng mỗi khi cập nhật code mới từ GitHub hoặc muốn Restart bot.** Lệnh chạy xong trong **đúng 3 giây**!

Khối lệnh này tự động:
1. Tải bản mới nhất của `scanner_full.py` và `dashboard_server.py` từ GitHub.
2. Tải thư viện biểu đồ Lightweight Charts nếu chưa có.
3. Khởi động lại dịch vụ bot và in ngay 25 dòng log đầu tiên ra màn hình.

```bash
cd ~/scanner && \
curl -s -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/scanner_full.py && \
curl -s -O https://raw.githubusercontent.com/guitar295/stock-scanner/refs/heads/main/dashboard_server.py && \
[ -f static/lightweight-charts.min.js ] || curl -s -L "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js" -o static/lightweight-charts.min.js && \
sync && \
sudo systemctl restart scanner && \
echo "✅ CẬP NHẬT CODE MỚI & KHỞI ĐỘNG LẠI HOÀN TẤT TRONG 3 GIÂY!" && \
sleep 3 && \
tail -n 25 ~/scanner/scanner.log
```

---

## 🛠️ KHỐI 3: CÁC LỆNH QUẢN LÝ THƯỜNG DÙNG

### 1. Xem Log Con Bot Đang Quét Trực Tiếp (Live Stream Realtime)
Theo dõi từng chu kỳ quét 5s, tín hiệu phát hiện và hoạt động gửi tin Telegram:
```bash
tail -f ~/scanner/scanner.log
```
> *(Bấm tổ hợp phím `Ctrl + C` để đóng màn hình xem log).*

---

### 2. Xem Nhanh 50 Dòng Log Gần Nhất
Xem nhanh các thông báo gần nhất rồi thoát ngay ra dòng lệnh:
```bash
tail -n 50 ~/scanner/scanner.log
```

---

### 3. Kiểm Tra Trạng Thái Hoạt Động Của Bot
Kiểm tra xem bot có đang chạy ổn định 24/7 hay không (`active (running)` màu xanh):
```bash
sudo systemctl status scanner
```

---

### 4. Tắt Tạm Thời Hoặc Bật Lại Bot Thủ Công
* **Tắt bot (Khi cần bảo trì):**
  ```bash
  sudo systemctl stop scanner
  ```
* **Bật lại bot:**
  ```bash
  sudo systemctl start scanner
  ```

---

