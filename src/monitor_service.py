import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import sys
from datetime import datetime
from collections import deque
from dotenv import load_dotenv

sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv()

from src.utils.telegram import notify_telegram, is_telegram_enabled
from src.utils.memory import load_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

BAD_PATTERNS = re.compile(
    r"(error|warn|fail|down|link\s+down|timeout|deny|reset|exception|auth)",
    re.IGNORECASE,
)

MAX_HISTORY = 2000
DEFAULT_PORT = 1514

_seen: set = set()
_seen_initialized = False


def _clean(text: str) -> str:
    return text.strip()


def _normalize(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^\d+\s+", "", text)
    text = re.sub(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\d{2}:\d{2}:\d{2}",
        "DATE TIME",
        text,
    )
    text = re.sub(r"\d{2}:\d{2}:\d{2}", "TIME", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()


def _hash_line(line: str) -> str:
    normalized = _normalize(line)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _is_bad(message: str) -> bool:
    return bool(BAD_PATTERNS.search(message))


def _is_own_ssh_log(line: str, dev_username: str) -> bool:
    normalized = _normalize(line).lower()
    if "ssh" not in normalized:
        return False
    if "authentication failure" in normalized:
        return False
    if "user" not in normalized:
        return False
    if dev_username and f"user {dev_username.lower()}" in normalized:
        return True
    if "ssh user" in normalized and ("login" in normalized or "logout" in normalized):
        return True
    return False


class SyslogServer:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.transport = None
        self.protocol = None
        self.recent: deque = deque(maxlen=MAX_HISTORY)

    def connection_made(self, transport):
        self.transport = transport
        logger.info("Syslog server listening on %s:%s", self.host, self.port)

    def connection_lost(self, exc):
        logger.warning("Syslog server connection lost: %s", exc)

    def datagram_received(self, data, addr):
        try:
            message = data.decode("utf-8", errors="replace")
            message = _clean(message)
            if not message:
                return
            self.recent.append(message)
            if _is_bad(message):
                asyncio.get_event_loop().create_task(self._alert(addr, message))
        except Exception as e:
            logger.warning("Syslog parse error: %s", e)

    async def _alert(self, addr, message: str):
        if not is_telegram_enabled():
            return
        key = f"{addr[0]}:{_hash_line(message)}"
        if key in _seen:
            logger.debug("Syslog duplicate skipped: %s", key)
            return
        _seen.add(key)
        if len(_seen) > MAX_HISTORY:
            _seen.clear()
        short = message.replace("\n", " | ")
        if len(short) > 280:
            short = short[:280] + "..."
        text = (
            f"🚨 Плохой syslog от {addr[0]}\n"
            f"{short}\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await notify_telegram(text)
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    async def start(self):
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=(self.host, self.port),
        )
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            transport.close()


async def _ssh_poll_once(interval: int):
    global _seen_initialized
    mem = load_memory()
    devices = mem.get("devices", [])
    if not devices:
        logger.info("No devices in memory, skipping poll")
        return

    if not _seen_initialized:
        _seen.clear()
        _seen_initialized = True
        logger.info("Seen cleared on first poll")

    new_alerts = []
    loop = asyncio.get_event_loop()

    for dev in devices:
        host = dev.get("ip")
        username = dev.get("username")
        password = dev.get("password")
        device_type = dev.get("device_type", "zyxel_os")
        name = dev.get("name", host)

        if not host or not username or not password:
            logger.warning("Device %s missing credentials, skipping", name)
            continue

        logger.info("Polling %s (%s)", name, host)
        try:
            from src.utils.memory import get_switch_logs
            out = await loop.run_in_executor(
                None, get_switch_logs, host, username, password, device_type, False
            )
        except Exception as e:
            logger.warning("Monitor error getting logs for %s: %s", host, e)
            continue

        if not out.get("ok"):
            text = _clean(out.get("error", ""))
            h = f"ERR:{host}:{_hash_line(text)}"
            if h not in _seen:
                _seen.add(h)
                new_alerts.append((host, f"❌ Ошибка доступа: {name}\n{text}"))
            else:
                logger.info("Error already seen: %s", h)
            continue

        logs = out.get("logs") or ""
        if not logs or "Не удалось получить логи" in logs:
            logger.info("No logs returned for %s", host)
            continue

        bad_lines = []
        for ln in logs.splitlines():
            s = ln.strip()
            if not s:
                continue
            if _is_own_ssh_log(s, username):
                continue
            if _is_bad(s):
                bad_lines.append(s)

        logger.info("Found %d bad lines in logs for %s", len(bad_lines), host)
        if not bad_lines:
            continue

        new_bad = []
        for line in bad_lines:
            h = f"LINE:{host}:{_hash_line(line)}"
            if h not in _seen:
                _seen.add(h)
                new_bad.append(line)

        logger.info("New bad lines for %s: %d", host, len(new_bad))
        if not new_bad:
            continue

        if len(new_bad) > 8:
            new_bad = new_bad[:8] + [f"... и еще {len(new_bad) - 8} строк"]

        text = "\n".join(new_bad)
        new_alerts.append((host, f"🚨 Плохие логи: {name} ({host})\n{text[:300]}"))

    logger.info("Total seen hashes in memory: %d", len(_seen))

    for host, text in new_alerts:
        logger.info("SENDING ALERT for %s: %s", host, text.replace('\n', ' | ')[:120])
        loop.create_task(notify_telegram(text))


async def _poll_loop(interval: int):
    logger.info("SSH poll monitor started, interval=%s sec", interval)
    while True:
        try:
            await _ssh_poll_once(interval)
        except Exception as e:
            logger.warning("Poll loop error: %s", e)
        await asyncio.sleep(interval)


async def main():
    port = int(os.getenv("SYSLOG_PORT", str(DEFAULT_PORT)))
    poll_interval = int(os.getenv("MONITOR_POLL_INTERVAL", "60"))

    if not is_telegram_enabled():
        logger.info("Telegram disabled, monitor service exiting.")
        return

    tasks = []
    if os.getenv("SYSLOG_ENABLED", "true").lower() in ("1", "true", "yes"):
        server = SyslogServer("0.0.0.0", port)
        tasks.append(asyncio.create_task(server.start()))
        logger.info("Syslog receiver enabled on UDP %s", port)

    if os.getenv("SSH_POLL_ENABLED", "true").lower() in ("1", "true", "yes"):
        tasks.append(asyncio.create_task(_poll_loop(poll_interval)))
        logger.info("SSH poll enabled, interval=%s sec", poll_interval)

    if not tasks:
        logger.info("No monitors enabled.")
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
