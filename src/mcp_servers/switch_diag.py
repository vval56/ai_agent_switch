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

app = Server("switch-diag-server")

class SwitchCommandArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname коммутатора")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    command: str = Field(description="Команда (например, 'show running-config' или 'vlan database')")
    device_type: str = Field(default="zyxel_os", description="Тип устройства: 'zyxel_os' или 'cisco_ios'")
    confirm: bool = Field(default=False, description="Подтверждение опасной операции (установите True только если пользователь явно подтвердил)")

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
    return any(cmd.startswith(p) for p in prefixes)

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="execute_switch_command",
            description="Подключается к коммутатору Zyxel или Cudy по SSH и выполняет ТОЛЬКО диагностические команды чтения. НЕ выполняет команды записи.",
            inputSchema=SwitchCommandArgs.model_json_schema(),
        ),
        Tool(
            name="apply_switch_config",
            description="Выполняет ОДНУ команду конфигурации на коммутаторе Zyxel/Cudy. ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ (confirm=true). ИСПОЛЬЗУЙ ТОЛЬКО когда пользователь явно сказал 'да', 'подтверждаю', 'выполняй'. Запрещены опасные команды (erase, delete, reload, write erase и т.д.). Возвращает результат применения.",
            inputSchema=SwitchCommandArgs.model_json_schema(),
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    args = SwitchCommandArgs(**arguments)
    
    if name == "execute_switch_command":
        cmd_lower = args.command.lower().strip()
        policy = _load_policy(args.host)
        safe_prefixes = tuple(policy.get("readonly_prefixes", ["show", "display", "cat", "grep", "ping", "traceroute", "log", "system", "get", "ip a", "ifconfig"]))
        if not any(cmd_lower.startswith(prefix) for prefix in safe_prefixes):
            return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Разрешены только команды: {', '.join(safe_prefixes)}.")]
        return await _run_command(args)

    if name == "apply_switch_config":
        if not args.confirm:
            return [TextContent(type="text", text="⚠️ Для выполнения команды конфигурации требуется явное подтверждение пользователя. Попросите пользователя подтвердить операцию.")]
        if not _is_safe_command(args.command, args.host):
            policy = _load_policy(args.host)
            blocked = policy.get("blocked_patterns", [])
            return [TextContent(type="text", text=f"❌ ОШИБКА БЕЗОПАСНОСТИ: Команда заблокирована. Запрещены: {', '.join(blocked[:5])}...")]
        result = await _run_command(args)
        await _notify_switch_config(args.host, args.command, result[0].text)
        return result

    raise ValueError(f"Unknown tool: {name}")


async def _run_command(args: SwitchCommandArgs) -> list[TextContent]:
    log_file = os.path.join(os.getcwd(), "switch_debug_session.log")
    device = {
        "device_type": args.device_type,
        "host": args.host,
        "username": args.username,
        "password": args.password,
        "session_log": log_file,
        "timeout": 15,
        "global_delay_factor": 1.5,
    }

    try:
        logger.info(f"Подключение к {args.host} ({args.device_type})...")
        with ConnectHandler(**device) as net_connect:
            logger.info(f"Выполнение: {args.command}")
            output = net_connect.send_command(args.command, read_timeout=30)
            return [TextContent(type="text", text=f"✅ Успешно на {args.host}:\n\n{output}")]

    except NetmikoAuthenticationException:
        await _notify_switch_error(args.host, "Ошибка аутентификации: неверный логин или пароль.")
        return [TextContent(type="text", text="❌ Ошибка аутентификации: проверьте логин и пароль.")]
    except NetmikoTimeoutException:
        await _notify_switch_error(args.host, f"Таймаут: хост {args.host} недоступен или порт 22 закрыт.")
        return [TextContent(type="text", text=f"❌ Таймаут: хост {args.host} недоступен или порт 22 закрыт.")]
    except Exception as e:
        await _notify_switch_error(args.host, str(e))
        return [TextContent(type="text", text=f"❌ Ошибка: {str(e)}")]

async def _notify_switch_error(host: str, error_text: str):
    if not is_telegram_enabled():
        return
    message = (
        f"🚨 Ошибка диагностики коммутатора\n"
        f"🖥️ Хост: {host}\n"
        f"❌ {error_text}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await notify_telegram(message)

async def _notify_switch_config(host: str, command: str, result_text: str):
    if not is_telegram_enabled():
        return
    ok = result_text.startswith("✅")
    status = "✅ Успешно" if ok else "❌ Ошибка"
    short = result_text.strip().replace("\n", " | ")
    if len(short) > 200:
        short = short[:200] + "..."
    message = (
        f"🔧 Изменение конфигурации коммутатора\n"
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