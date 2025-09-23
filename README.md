# Downloader Bot (Standalone)

Bot Telegram tối giản chỉ để tải và upload media.

## Chạy local

```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN="<your_token>"
python app.py
```

## Lệnh hỗ trợ
- /start
- /download <url>
- /downloadlist <url1> <url2> ... (hoặc reply danh sách URL)
- Gửi tin nhắn có URL: bot tự động tải
- Reply tới tin nhắn có URL: bot tự động tải

## Triển khai (Railway/Heroku)
- Procfile: `worker: python app.py`
- Biến môi trường: `TELEGRAM_BOT_TOKEN`

## Cookies YouTube
Đặt `data/cookies.txt` để vượt qua xác thực khi tải từ YouTube.
# hunziibot2-railway
