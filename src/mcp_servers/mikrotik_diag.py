import asyncio
import logging
import os
import sys
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from dotenv import load_dotenv

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv()
from src.utils.telegram import notify_telegram, is_telegram_enabled

logging.basicConfig(level=logging.INFO)

# Отключаем спам от httpx (Telegram) и paramiko
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger("paramiko.transport").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Дедупликация: не выполнять ту же команду дважды (для apply_routeros_config)
_command_cache = {}

def _clean_routeros_command(cmd: str) -> str:
    """Чистит команду RouterOS от мусора: убирает src-port=0-65535, dst-port=0-65535, protocol=all."""
    import re
    # Убираем src-port=0-65535 и dst-port=0-65535 (бессмысленные — все порты)
    cmd = re.sub(r'\s+src-port=0-65535', '', cmd, flags=re.IGNORECASE)
    cmd = re.sub(r'\s+dst-port=0-65535', '', cmd, flags=re.IGNORECASE)
    # Убираем protocol=all (по умолчанию и так все протоколы)
    cmd = re.sub(r'\s+protocol=all', '', cmd, flags=re.IGNORECASE)
    # Нормализуем пробелы
    cmd = ' '.join(cmd.split())
    # Добавляем / в начале если нет
    if not cmd.startswith('/'):
        cmd = '/' + cmd
    return cmd

def _dedup_check(host: str, command: str) -> bool:
    """Проверяет, выполнялась ли уже эта команда. Если да — возвращает False.
    Нормализует ключ: убирает comment, так как RouterOS может добавлять разные комментарии."""
    import re
    # Нормализуем: убираем comment="..." для сравнения
    normalized = re.sub(r'comment="[^"]*"', 'comment=', command.strip())
    key = f"{host}::{normalized}"
    if key in _command_cache:
        return False
    _command_cache[key] = True
    if len(_command_cache) > 500:
        _command_cache.clear()
    return True

class RouterOSCommandArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname MikroTik роутера")
    username: str = Field(description="Имя пользователя для SSH (обычно 'admin')")
    password: str = Field(description="Пароль для SSH")
    command: str = Field(description="Команда RouterOS (например, '/interface ethernet print')")
    device_type: str = Field(default="mikrotik_routeros", description="Тип устройства: всегда 'mikrotik_routeros'")

class RouterOSConfigArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname MikroTik роутера")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    command: str = Field(description="Одна или несколько команд конфигурации. Несколько команд можно разделять символом ; или переводом строки. Пример: '/ip firewall filter add action=accept chain=input connection-state=established,related comment=\"Accept established\"; /ip firewall filter add action=accept chain=input protocol=tcp dst-port=22 comment=\"Allow SSH\"'")
    device_type: str = Field(default="mikrotik_routeros", description="Тип устройства: всегда 'mikrotik_routeros'")
    confirm: str = Field(default="false", description="Подтверждение опасной операции. Установите 'true' только если пользователь явно подтвердил (сказал 'да', 'подтверждаю', 'выполняй')")

    def is_confirmed(self) -> bool:
        return str(self.confirm).lower().strip() in ('true', '1', 'yes', 'да', 'подтверждаю', 'выполняй')

app = Server("mikrotik-diag-server")

def _load_policy(device_ip: str = ""):
    from src.utils.memory import get_command_policies
    policies = get_command_policies()
    policy = policies.get(device_ip, policies.get("default", {}))
    return policy

def _is_safe_command(command: str, device_ip: str = "") -> bool:
    cmd = command.lower().strip()
    policy = _load_policy(device_ip)
    blocked = policy.get("blocked_patterns", [])
    for pattern in blocked:
        if pattern.lower() in cmd:
            return False
    return True

def _is_readonly_command(command: str, device_ip: str = "") -> bool:
    cmd = command.lower().strip()
    policy = _load_policy(device_ip)
    prefixes = policy.get("readonly_prefixes", [])
    if any(cmd.startswith(p) for p in prefixes):
        return True
    if cmd.startswith("/"):
        for p in prefixes:
            if cmd.startswith("/" + p):
                return True
    return False

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="execute_routeros_command",
            description="Подключается к MikroTik RouterOS по SSH и выполняет ТОЛЬКО диагностические команды чтения (print, show, ping, traceroute, /interface print, /ip route print, /system resource print и т.д.). НЕ выполняет команды изменения конфигурации.",
            inputSchema=RouterOSCommandArgs.model_json_schema(),
        ),
        Tool(
            name="apply_routeros_config",
            description="Выполняет ОДНУ безопасную команду конфигурации на MikroTik RouterOS v7. ИСПОЛЬЗУЙ ТОЛЬКО когда пользователь явно просит внести правки/изменения. Запрещены опасные команды (reset, reboot, fetch, bandwidth-test, export/import и т.п.). Возвращает результат применения.",
            inputSchema=RouterOSConfigArgs.model_json_schema(),
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "execute_routeros_command":
        args = RouterOSCommandArgs(**arguments)
        args.device_type = "mikrotik_routeros"  # Принудительно
        cmd_lower = args.command.lower().strip()
        policy = _load_policy(args.host)
        blocked = policy.get("blocked_patterns", [])
        for pattern in blocked:
            if pattern.lower() in cmd_lower:
                return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Команда заблокирована: {pattern}")]
        readonly_prefixes = list(policy.get("readonly_prefixes", ["/", ":", "get", "print", "show", "ping", "traceroute", "resolve", "fetch"]))
        allowed = any(cmd_lower.startswith(p) for p in readonly_prefixes)
        if not allowed:
            for p in readonly_prefixes:
                if not p.startswith("/") and cmd_lower.startswith("/" + p):
                    allowed = True
                    break
        if not allowed:
            return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Разрешены только команды: {', '.join(readonly_prefixes)}.")]
        return await _run_routeros_command(args)

    if name == "apply_routeros_config":
        args = RouterOSConfigArgs(**arguments)
        # Принудительно mikrotik_routeros (mikrotik_routeros_v7 не поддерживается netmiko)
        args.device_type = "mikrotik_routeros"
        if not args.is_confirmed():
            return [TextContent(type="text", text="⚠️ Для выполнения команды конфигурации требуется явное подтверждение пользователя. Попросите пользователя подтвердить операцию (он должен сказать 'да', 'подтверждаю', 'выполняй').")]
        # Чистим команду от мусора (src-port=0-65535, protocol=all и т.п.)
        args.command = _clean_routeros_command(args.command)
        if not _is_safe_command(args.command, args.host):
            policy = _load_policy(args.host)
            blocked = policy.get("blocked_patterns", [])
            return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Команда заблокирована. Запрещены: {', '.join(blocked[:5])}...")]
        # Дедупликация: проверяем каждую команду из пачки
        raw = args.command.replace('\n', '\n').replace(';', '\n')
        individual_cmds = [c.strip() for c in raw.split('\n') if c.strip()]
        already_done = [c for c in individual_cmds if not _dedup_check(args.host, c)]
        if already_done:
            done_list = "\n".join(f"  ✅ {c}" for c in already_done)
            remaining = [c for c in individual_cmds if c not in already_done]
            if not remaining:
                return [TextContent(type="text", text=f"✅ Все команды уже были выполнены ранее:\n\n{done_list}\n\nПовторное выполнение отменено.")]
            # Выполняем только оставшиеся
            args.command = "; ".join(remaining)
            print(f"✅ {len(already_done)} команд пропущено (дубли), выполняю {len(remaining)} новых", flush=True)
        result = await _run_routeros_command(args)
        await _notify_routeros_config(args.host, args.command, result[0].text)
        return result

    raise ValueError(f"Unknown tool: {name}")


async def _run_routeros_command(args) -> list[TextContent]:
    log_file = os.path.join(os.getcwd(), "switch_debug_session.log")
    device = {
        "device_type": args.device_type,
        "host": args.host,
        "username": args.username,
        "password": args.password,
        "session_log": log_file,
        "timeout": 15,
        "global_delay_factor": 2,
    }

    try:
        logger.info(f"Подключение к {args.host} (RouterOS)...")
        with ConnectHandler(**device) as net_connect:
            prompt = net_connect.find_prompt().strip()
            # Разделяем команды по ; и переводам строк
            raw = args.command.replace('\n', '\n')
            parts = [c.strip() for c in raw.replace(';', '\n').split('\n') if c.strip()]
            results = []
            for cmd in parts:
                logger.info(f"Выполнение: {cmd}")
                output = net_connect.send_command(cmd, read_timeout=40)
                output = output.strip() if output else ""
                if not output:
                    output = "Команда выполнена успешно (RouterOS не возвращает вывод для команд изменения конфигурации)."
                results.append(f"  {cmd}\n  → {output}")
            return [TextContent(type="text", text=f"✅ Выполнено на {args.host} ({args.device_type}):\n\n" + "\n\n".join(results))]

    except NetmikoAuthenticationException:
        await _notify_routeros_error(args.host, "Ошибка аутентификации: неверный логин или пароль.")
        return [TextContent(type="text", text="❌ Ошибка аутентификации: проверьте логин и пароль.")]
    except NetmikoTimeoutException:
        await _notify_routeros_error(args.host, f"Таймаут: хост {args.host} недоступен или порт 22 закрыт.")
        return [TextContent(type="text", text=f"❌ Таймаут: хост {args.host} недоступен или порт 22 закрыт.")]
    except Exception as e:
        await _notify_routeros_error(args.host, str(e))
        return [TextContent(type="text", text=f"❌ Ошибка: {str(e)}")]


async def _notify_routeros_error(host: str, error_text: str):
    if not is_telegram_enabled():
        return
    message = (
        f"🚨 Ошибка диагностики MikroTik\n"
        f"🖥️ Хост: {host}\n"
        f"❌ {error_text}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await notify_telegram(message)

async def _notify_routeros_config(host: str, command: str, result_text: str):
    if not is_telegram_enabled():
        return
    ok = result_text.startswith("✅")
    status = "✅ Успешно" if ok else "❌ Ошибка"
    short = result_text.strip().replace("\n", " | ")
    if len(short) > 200:
        short = short[:200] + "..."
    message = (
        f"🔧 Изменение конфигурации MikroTik RouterOS\n"
        f"🖥️ Хост: {host}\n"
        f"📝 Команда: {command}\n"
        f"{status}: {short}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await notify_telegram(message)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
