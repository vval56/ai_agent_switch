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

sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv()
from src.utils.telegram import notify_telegram, is_telegram_enabled

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

class RouterOSCommandArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname MikroTik роутера")
    username: str = Field(description="Имя пользователя для SSH (обычно 'admin')")
    password: str = Field(description="Пароль для SSH")
    command: str = Field(description="Команда RouterOS (например, '/interface ethernet print')")
    device_type: str = Field(default="mikrotik_routeros", description="Тип устройства: 'mikrotik_routeros' или 'mikrotik_routeros_v7'")

class RouterOSConfigArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname MikroTik роутера")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    command: str = Field(description="Одна команда конфигурации (например, '/interface ethernet enable 1')")
    device_type: str = Field(default="mikrotik_routeros", description="Тип устройства: 'mikrotik_routeros' или 'mikrotik_routeros_v7'")
    confirm: bool = Field(default=False, description="Подтверждение опасной операции (установите True только если пользователь явно подтвердил)")

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
        if not args.confirm:
            return [TextContent(type="text", text="⚠️ Для выполнения команды конфигурации требуется явное подтверждение пользователя. Попросите пользователя подтвердить операцию (он должен сказать 'да', 'подтверждаю', 'выполняй').")]
        if not _is_safe_command(args.command, args.host):
            policy = _load_policy(args.host)
            blocked = policy.get("blocked_patterns", [])
            return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Команда заблокирована. Запрещены: {', '.join(blocked[:5])}...")]
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
            logger.info(f"Выполнение: {args.command}")
            output = net_connect.send_command(args.command, read_timeout=40)
            return [TextContent(type="text", text=f"✅ Успешно на {args.host} ({args.device_type}):\n\n{output}")]

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
