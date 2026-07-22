import asyncio
import logging
import os
import sys
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Добавляем корень проекта в путь, чтобы импортировать utils.memory
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), "../..")))
from src.utils import memory
from src.utils.telegram import notify_telegram, is_telegram_enabled

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Server("memory-manager-server")

class ConnectSwitchArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname коммутатора")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    device_type: str = Field(default="zyxel_os", description="Тип устройства: 'zyxel_os', 'cisco_ios', 'mikrotik_routeros' или 'mikrotik_routeros_v7'")
    name: str = Field(default="", description="Имя устройства (необязательно, по умолчанию = host)")
    command: str = Field(default="show version", description="Команда для проверки связи")

class GetLogsArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname коммутатора")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    device_type: str = Field(default="zyxel_os", description="Тип устройства: 'zyxel_os', 'cisco_ios', 'mikrotik_routeros' или 'mikrotik_routeros_v7'")
    only_errors: bool = Field(default=False, description="Если True — вернуть только плохие строки (ошибки/варнинги/линки down)")

class GetConfigArgs(BaseModel):
    host: str = Field(description="IP-адрес или hostname коммутатора")
    username: str = Field(description="Имя пользователя для SSH")
    password: str = Field(description="Пароль для SSH")
    device_type: str = Field(default="zyxel_os", description="Тип устройства: 'zyxel_os', 'cisco_ios', 'mikrotik_routeros' или 'mikrotik_routeros_v7'")

class AddDeviceArgs(BaseModel):
    name: str = Field(description="Имя устройства (например, 'Zyxel-Office')")
    ip: str = Field(description="IP-адрес устройства")
    model: str = Field(description="Модель (например, 'GS1920-24' или 'RB4011')")
    username: str = Field(description="Имя пользователя")
    password: str = Field(description="Пароль")
    device_type: str = Field(description="Тип устройства: 'zyxel_os', 'cisco_ios', 'mikrotik_routeros' или 'mikrotik_routeros_v7'")
    notes: str = Field(default="", description="Дополнительные заметки")
    set_active: bool = Field(default=True, description="Сделать это устройство активным (текущим) коммутатором")

class AddLogArgs(BaseModel):
    device_name: str = Field(description="Имя или IP устройства")
    issue: str = Field(description="Краткое описание проблемы")
    solution: str = Field(description="Как проблема была решена или что выяснилось")

class SearchHistoryArgs(BaseModel):
    query: str = Field(description="Поисковый запрос (например, 'VLAN', 'порт 3', 'Zyxel')")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="add_network_device", description="Сохраняет или обновляет реквизиты коммутатора/MikroTik в долговременной памяти.", inputSchema=AddDeviceArgs.model_json_schema()),
        Tool(name="get_network_devices", description="Возвращает список всех сохраненных коммутаторов/MikroTik и их реквизиты.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="connect_switch", description="Подключается к коммутатору Zyxel/Cudy или MikroTik RouterOS по SSH, проверяет связь и делает его АКТИВНЫМ (текущим). Возвращает подтверждение подключения с выводом проверочной команды.", inputSchema=ConnectSwitchArgs.model_json_schema()),
        Tool(name="get_active_switch", description="Возвращает активный (текущий подключённый) коммутатор/MikroTik: имя, IP, тип, статус. Используй, чтобы подтвердить, к чему подключен агент.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_switch_logs", description="Подключается к коммутатору/MikroTik и читает его логи. Для ZyNOS использует родные команды, для MikroTik — `/log print`. only_errors=True возвращает только ошибки/варнинги/линки down.", inputSchema=GetLogsArgs.model_json_schema()),
        Tool(name="get_switch_config", description="Собирает текущую конфигурацию устройства. Для Zyxel/Cudy использует ZyNOS-команды (show config, show vlan, show interface, show ip). Для MikroTik RouterOS использует /export, /interface print, /ip route print, /vlan print. Возвращает реальный вывод без выдумок.", inputSchema=GetConfigArgs.model_json_schema()),
        Tool(name="add_diagnostic_log", description="Записывает итог диагностики (проблема и решение) в историю для будущего поиска.", inputSchema=AddLogArgs.model_json_schema()),
        Tool(name="search_diagnostic_history", description="Ищет в истории прошлые диагностики по ключевым словам.", inputSchema=SearchHistoryArgs.model_json_schema())
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "add_network_device":
            args = AddDeviceArgs(**arguments)
            res = memory.add_device(args.name, args.ip, args.model, args.username, args.password, args.device_type, args.notes)
            if args.set_active:
                memory.set_active_switch(args.name, args.ip, args.model, args.username, args.password, args.device_type, args.notes)
                res += "\n✅ Устройство сделано активным (текущим)."
            if is_telegram_enabled():
                await notify_telegram(f"✅ Добавлено устройство: {args.name} ({args.ip})\nМодель: {args.model}\nТип: {args.device_type}")
        elif name == "get_network_devices":
            res = memory.get_devices()
        elif name == "connect_switch":
            args = ConnectSwitchArgs(**arguments)
            out = memory.connect_to_switch(args.host, args.username, args.password, args.device_type, args.command)
            if out["ok"]:
                model = out["version"].strip().splitlines()[0] if out["version"].strip() else ""
                name = args.name or args.host
                memory.set_active_switch(name, args.host, model, args.username, args.password, args.device_type)
                res = (
                    f"✅ Подключено к коммутатору {args.host} ({args.device_type}). Устройство СДЕЛАНО АКТИВНЫМ.\n\n"
                    f"📡 Ответ проверочной команды `{args.command}`:\n{out['probe']}\n\n"
                    f"📋 Краткое описание устройства (show version):\n{out['version'][:600]}"
                )
                if is_telegram_enabled():
                    await notify_telegram(f"✅ Подключение к устройству: {args.host} ({args.device_type})\nМодель: {model}\nПользователь: {args.username}")
            else:
                res = f"❌ {out['error']} Коммутатор НЕ сделан активным."
                if is_telegram_enabled():
                    await notify_telegram(f"❌ Ошибка подключения: {args.host} ({args.device_type})\n{out['error']}")
        elif name == "get_active_switch":
            sw = memory.get_active_switch()
            if not sw:
                res = "⚠️ Активный коммутатор не выбран. Сначала выполни connect_switch или add_network_device."
            else:
                res = (
                    f"🟢 АКТИВНЫЙ КОММУТАТОР:\n"
                    f"• Имя: {sw['name']}\n• IP: {sw['ip']}\n• Тип: {sw['device_type']}\n• Модель: {sw.get('model', 'неизвестно')}\n"
                    f"• Пользователь: {sw['username']}\n• Примечание: {sw.get('notes', 'нет')}\n\n"
                    f"💡 Чтобы проверить, что агент точно к нему подключён, выполни connect_switch повторно или execute_switch_command с командой 'show version'."
                )
        elif name == "get_switch_logs":
            args = GetLogsArgs(**arguments)
            out = memory.get_switch_logs(args.host, args.username, args.password, args.device_type, args.only_errors)
            if out["ok"]:
                res = f"📜 Логи коммутатора {args.host} (команда: {out['command']}):\n\n{out['logs']}"
                if out.get("has_errors") and is_telegram_enabled():
                    short = (out.get("logs") or "").strip().replace("\n", " | ")
                    if len(short) > 200:
                        short = short[:200] + "..."
                    await notify_telegram(f"🚨 Плохие логи: {args.host} ({args.device_type})\n{short}")
            else:
                res = f"❌ {out['error']}"
                if is_telegram_enabled():
                    await notify_telegram(f"❌ Ошибка получения логов: {args.host} ({args.device_type})\n{out['error']}")
        
        elif name == "get_switch_config":
            args = GetConfigArgs(**arguments)
            out = memory.get_switch_config(args.host, args.username, args.password, args.device_type)
            if out["ok"]:
                res = f"⚙️ Конфигурация коммутатора {args.host}:\n\n{out['config']}"
            else:
                res = f"❌ {out['error']}"

        elif name == "add_diagnostic_log":
            args = AddLogArgs(**arguments)
            res = memory.add_diagnostic_log(args.device_name, args.issue, args.solution)
        elif name == "search_diagnostic_history":
            args = SearchHistoryArgs(**arguments)
            res = memory.search_history(args.query)
        else:
            res = f"❌ Неизвестный инструмент: {name}"
        return [TextContent(type="text", text=res)]
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Ошибка памяти: {str(e)}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())