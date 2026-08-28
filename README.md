# Telegram Verification & Anti-Link Bot (PTB v20+ Async)

## 🚀 Quick Setup Guide

1. Create a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate # (On Windows: venv\Scripts\activate)
   pip install -r requirements.txt
   ```

2. Setup Environment Variables:
   ```bash
   cp .env.example .env
   # Edit .env with your BOT_TOKEN and Telegram channel IDs
   ```

3. Run Bot:
   ```bash
   python main.py
   ```

## 🐳 Docker Setup
```bash
docker build -t telegram-bot .
docker run -d --name tg-bot --env-file .env --restart unless-stopped telegram-bot
```
