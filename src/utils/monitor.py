import asyncio
import logging
import os
import sys
import json
import hashlib
from datetime import datetime
from dotenv import load_dotenv

sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv()

from src.utils.telegram import notify_telegram, is_telegram_enabled
from src.utils.memory import load_memory, save_memory, _normalize_device_type

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = int(os.getenv("TELEGRAM_CHECK_INTERVAL", "300"))
MAX_HISTORY = 500

def _short(text: str, limit: int = 200) -> str:
    text = (text or "").strip().replace("\n", " | ")
    return text[:limit] + "..." if len(text) > limit else text

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

async def check_once():
    mem = load_memory()
    devices = mem.get("devices", [])
    if not devices:
        return []

    seen = mem.setdefault("telegram_notifications", [])
    new_alerts = []
    loop = asyncio.get_event_loop()

    for dev in devices:
        host = dev.get("ip")
        username = dev.get("username")
        password = dev.get("password")
        device_type = dev.get("device_type", "zyxel_os")
        name = dev.get("name", host)

        if not host or not username or not password:
            continue

        try:
            from src.utils.memory import get_switch_logs
            out = await loop.run_in_executor(None, get_switch_logs, host, username, password, _normalize_device_type(device_type), False)
        except Exception as e:
            logger.warning("Monitor error getting logs for %s: %s", host, e)
            continue

        if not out.get("ok"):
            text = _short(out.get("error", ""))
            h = _hash(f"ERR:{host}:{text}")
            if h not in seen:
                seen.append(h)
                if len(seen) > MAX_HISTORY:
                    seen[:] = seen[-MAX_HISTORY:]
                new_alerts.append((host, f"❌ Ошибка доступа: {name}\n{text}"))
            continue

        logs = out.get("logs") or ""
        if not logs or "Не удалось получить логи" in logs:
            continue

        bad = []
        for line in logs.splitlines():
            s = line.strip()
            if not s:
                continue
            if any(k in s.lower() for k in ["error", "warn", "fail", "down", "link down", "timeout", "deny", "reset", "exception", "auth"]):
                bad.append(s)

        if not bad:
            continue

        if len(bad) > 8:
            bad = bad[:8] + [f"... и еще {len(bad) - 8} строк"]

        text = "\n".join(bad)
        h = _hash(f"LOG:{host}:{text}")
        if h not in seen:
            seen.append(h)
            if len(seen) > MAX_HISTORY:
                seen[:] = seen[-MAX_HISTORY:]
            new_alerts.append((host, f"🚨 Плохие логи: {name} ({host})\n{_short(text, 300)}"))

    if new_alerts:
        save_memory(mem)

    return new_alerts

async def monitor_loop():
    if not is_telegram_enabled():
        logger.info("Telegram отключен в .env, мониторинг не запущен.")
        return

    logger.info("Запущен фоновый мониторинг логов, интервал=%s сек.", CHECK_INTERVAL)
    while True:
        try:
            alerts = await check_once()
            for host, text in alerts:
                logger.info("Отправка уведомления для %s", host)
                await notify_telegram(text)
        except Exception as e:
            logger.warning("Monitor loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

def start_monitor():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(monitor_loop())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
