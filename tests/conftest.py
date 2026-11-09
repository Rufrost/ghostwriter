import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Настройки по умолчанию для тестов: никаких реальных ключей
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("THREADS_APP_ID", "1234567890")
os.environ.setdefault("THREADS_APP_SECRET", "secret")
os.environ.setdefault("TIMEZONE", "Europe/Moscow")
