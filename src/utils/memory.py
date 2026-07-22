import os
import fcntl
import json
import re
from datetime import datetime

MEMORY_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../.agent_memory.json"))

def get_default_memory():
    return {
        "devices": [],
        "active_switch": None,
        "preferences": {"language": "ru", "default_device_type": "zyxel_os"},
        "diagnostic_history": [],
        "chat_summary": "",
        "command_policies": {
            "default": {
                "readonly_prefixes": ["show", "display", "get", "ping", "traceroute", "log", "system", "ip a", "ifconfig"],
                "blocked_patterns": ["erase", "delete", "format", "reload", "write erase", "write memory", "copy running-config startup-config", "reset", "shutdown", "clear logging", "clear log", "debug all", "undebug all", "no enable", "disable", "no ip", "no spanning", "no vlan", "no interface"]
            },
            "mikrotik": {
                "readonly_prefixes": ["/", "print", "export", "info", "resource", "interface", "ip", "route", "firewall", "address-list", "ntp", "user", "system", "log", "ppp", "radius", "certificate"],
                "blocked_patterns": ["remove", "disable", "enable", "set password", "change-password", "export password", "import", "flash", "format", "reset"]
            }
        },
        "chat_histories": {}
    }

def set_active_switch(name, ip, model, username, password, device_type, notes=""):
    mem = load_memory()
    mem["active_switch"] = {
        "name": name, "ip": ip, "model": model, "username": username,
        "password": password, "device_type": device_type, "notes": notes
    }
    save_memory(mem)
    return mem["active_switch"]

def get_active_switch():
    mem = load_memory()
    return mem.get("active_switch")

def get_command_policies():
    mem = load_memory()
    return mem.get("command_policies", {})

def set_command_policy(key, policy):
    mem = load_memory()
    mem.setdefault("command_policies", {})[key] = policy
    save_memory(mem)
    return True

def delete_command_policy(key):
    mem = load_memory()
    if "command_policies" in mem and key in mem["command_policies"]:
        del mem["command_policies"][key]
        save_memory(mem)
        return True
    return False

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=0-9A-Za-z]|\x1b\].*?\x07|\x1b[()][AB0]|\x07|\x1b7|\x1b8")

def _clean(text):
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()

def _normalize_device_type(device_type: str) -> str:
    device_type = (device_type or "").strip().lower()
    if not device_type or device_type in ("zyxel", "zyxelos", "zyxel_os"):
        return "zyxel_os"
    if device_type in ("cisco", "cisco_ios"):
        return "cisco_ios"
    if device_type in ("mikrotik", "routeros", "mikrotik_routeros"):
        return "mikrotik_routeros"
    if device_type == "mikrotik_routeros_v7":
        return "mikrotik_routeros"
    return device_type

def connect_to_switch(host, username, password, device_type="zyxel_os", command="show version", read_timeout=30):
    device_type = _normalize_device_type(device_type)
    """Подключается к коммутатору и выполняет команду. Универсально для ZyNOS/Zyxel/Cudy/Cisco:
    не шлёт заведомо неверные команды пейджинации и ждёт реальный промпт коммутатора."""
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
    import re

    device = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "timeout": 15,
        "global_delay_factor": 2,
    }

    try:
        with ConnectHandler(**device) as net_connect:
            prompt = net_connect.find_prompt().strip()
            expect = re.escape(prompt) if prompt else None
            probe = _clean(net_connect.send_command(command, expect_string=expect, read_timeout=read_timeout))
            version = _clean(net_connect.send_command("show version", expect_string=expect, read_timeout=read_timeout))
        return {
            "ok": True,
            "prompt": prompt,
            "probe": probe,
            "version": version,
            "error": None,
        }
    except NetmikoAuthenticationException:
        return {"ok": False, "error": "Ошибка аутентификации: неверный логин или пароль."}
    except NetmikoTimeoutException:
        return {"ok": False, "error": f"Таймаут: хост {host} недоступен или порт 22 закрыт."}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка подключения: {str(e)}"}

def get_switch_logs(host, username, password, device_type="zyxel_os", only_errors=False, read_timeout=40):
    device_type = _normalize_device_type(device_type)
    """Читает логи коммутатора. Для ZyNOS (Zyxel/Cudy) использует родные команды,
    а не 'show log', который там выдаёт 'Ambiguous command'."""
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
    import re

    device = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "timeout": 15,
        "global_delay_factor": 2,
    }

    if device_type == "zyxel_os":
        log_cmds = ["show log all", "show log buffered", "show error-log", "show logging"]
    elif device_type in ("mikrotik_routeros", "mikrotik_routeros_v7"):
        log_cmds = ["/log print", "/log print where topics~\"error,critical,warning\""]
    else:
        log_cmds = ["show logging", "show log", "show log buffered"]

    has_errors = False
    try:
        with ConnectHandler(**device) as net_connect:
            prompt = net_connect.find_prompt().strip()
            expect = re.escape(prompt) if prompt else None
            collected = []
            used = None
            for cmd in log_cmds:
                try:
                    out = net_connect.send_command(cmd, expect_string=expect, read_timeout=read_timeout)
                except Exception:
                    continue
                out = _clean(out)
                if out and "Ambiguous command" not in out and "Invalid command" not in out and "Incomplete command" not in out:
                    used = cmd
                    collected.append(f"=== {cmd} ===\n{out}")
            raw = "\n\n".join(collected) if collected else "Не удалось получить логи ни одной из команд."
            bad_lines = [ln.strip() for ln in raw.splitlines() if re.search(r"(error|warn|fail|down|link\s+down|timeout|deny|reset|exception|auth)", ln, re.I)]
            has_errors = bool(bad_lines)
            if only_errors and raw:
                if bad_lines:
                    link_down = [ln for ln in bad_lines if re.search(r"link\s+down", ln, re.I)]
                    auth = [ln for ln in bad_lines if "authentication" in ln.lower() or "auth" in ln.lower()]
                    other = [ln for ln in bad_lines if ln not in link_down and ln not in auth]
                    parts = ["🚨 ПЛОХИЕ ЛОГИ (всего {0}):".format(len(bad_lines))]
                    if link_down:
                        parts.append("\n🔌 Порты выключены (link down):\n" + "\n".join(link_down))
                    if auth:
                        parts.append("\n🔐 Ошибки аутентификации:\n" + "\n".join(auth))
                    if other:
                        parts.append("\n⚠️ Прочее:\n" + "\n".join(other))
                    if not (link_down or auth or other):
                        parts.append("\n" + "\n".join(bad_lines))
                    raw = "\n".join(parts)
                else:
                    raw = "✅ Критичных ошибок в логах не найдено.\n\nПолный вывод:\n" + raw
        return {"ok": True, "command": used, "logs": raw, "error": None, "has_errors": has_errors}
    except NetmikoAuthenticationException:
        return {"ok": False, "logs": None, "error": "Ошибка аутентификации: неверный логин или пароль.", "has_errors": True}
    except NetmikoTimeoutException:
        return {"ok": False, "logs": None, "error": f"Таймаут: хост {host} недоступен или порт 22 закрыт.", "has_errors": True}
    except Exception as e:
        return {"ok": False, "logs": None, "error": f"Ошибка подключения: {str(e)}", "has_errors": True}



def get_switch_config(host, username, password, device_type="zyxel_os", read_timeout=40):
    device_type = _normalize_device_type(device_type)
    """Собирает РЕАЛЬНУЮ текущую конфигурацию коммутатора родными командами ZyNOS.
    Возвращает ТОЛЬКО то, что реально ответил коммутатор (без выдумок)."""
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

    device = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "timeout": 15,
        "global_delay_factor": 2,
    }

    if device_type == "zyxel_os":
        cmds = ["show vlan", "show interface", "show interfaces status", "show ip route", "show config"]
    elif device_type in ("mikrotik_routeros", "mikrotik_routeros_v7"):
        cmds = [
            "/system resource print",
            "/system identity print",
            "/interface print",
            "/ip address print",
            "/ip route print",
            "/ip route print where dynamic=no",
            "/ip firewall filter print",
            "/ip nat print",
            "/ip dhcp-server print",
            "/ip dhcp-client print",
            "/ip dns print",
            "/ntp client print",
            "/user print",
            "/user-group print",
            "/log print limit=50",
            "/system package print",
            "/export",
        ]
    else:
        cmds = ["show vlan", "show ip interface brief", "show ip route", "show running-config"]

    try:
        with ConnectHandler(**device) as net_connect:
            prompt = net_connect.find_prompt().strip()
            expect = re.escape(prompt) if prompt else None
            sections = []
            for cmd in cmds:
                try:
                    out = net_connect.send_command(cmd, expect_string=expect, read_timeout=read_timeout)
                except Exception:
                    continue
                out = _clean(out)
                if out and "Invalid command" not in out and "Incomplete command" not in out and "Ambiguous command" not in out:
                    sections.append(f"=== {cmd} ===\n{out}")
                else:
                    sections.append(f"=== {cmd} ===\n(команда не вернула данных на этом коммутаторе)")
            if not sections:
                return {"ok": True, "config": "Коммутатор не вернул данных ни по одной из команд.", "error": None}
            return {"ok": True, "config": "\n\n".join(sections), "error": None}
    except NetmikoAuthenticationException:
        return {"ok": False, "config": None, "error": "Ошибка аутентификации: неверный логин или пароль."}
    except NetmikoTimeoutException:
        return {"ok": False, "config": None, "error": f"Таймаут: хост {host} недоступен или порт 22 закрыт."}
    except Exception as e:
        return {"ok": False, "config": None, "error": f"Ошибка подключения: {str(e)}"}


def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH)
                except Exception:
                    pass
                try:
                    data = json.load(f)
                    mem = get_default_memory()
                    mem.update(data) # Безопасное обновление новых полей
                    return mem
                finally:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except Exception:
                        pass
        except Exception:
            return get_default_memory()
    return get_default_memory()

def save_memory(data):
    try:
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except Exception:
                pass
            try:
                json.dump(data, f, ensure_ascii=False, indent=2)
            finally:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ Ошибка сохранения памяти: {e}", flush=True)

def add_device(name, ip, model, username, password, device_type, notes=""):
    mem = load_memory()
    # Обновляем, если устройство с таким IP или именем уже есть
    for dev in mem["devices"]:
        if dev["ip"] == ip or dev["name"] == name:
            dev.update({"name": name, "ip": ip, "model": model, "username": username, "password": password, "device_type": device_type, "notes": notes})
            save_memory(mem)
            return f"✅ Устройство {name} ({ip}) обновлено в памяти."
    
    mem["devices"].append({
        "name": name, "ip": ip, "model": model, "username": username, 
        "password": password, "device_type": device_type, "notes": notes, 
        "added": datetime.now().strftime("%Y-%m-%d")
    })
    save_memory(mem)
    return f"✅ Устройство {name} ({ip}) сохранено в памяти."

def get_devices():
    mem = load_memory()
    if not mem["devices"]:
        return "📭 В памяти нет сохраненных устройств."
    result = "📋 Сохраненные устройства:\n"
    for dev in mem["devices"]:
        result += f"- **{dev['name']}** ({dev['model']}): `{dev['ip']}` | Тип: `{dev['device_type']}` | Примечание: {dev.get('notes', 'Нет')}\n"
    return result

def add_diagnostic_log(device_name, issue, solution):
    mem = load_memory()
    entry = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "device": device_name,
        "issue": issue,
        "solution": solution
    }
    mem["diagnostic_history"].insert(0, entry) # Новые записи сверху
    mem["diagnostic_history"] = mem["diagnostic_history"][:50] # Храним только последние 50 записей
    save_memory(mem)
    return f"✅ Запись о диагностике '{device_name}: {issue}' сохранена в истории."

def search_history(query):
    mem = load_memory()
    query_lower = query.lower()
    matches = [
        entry for entry in mem["diagnostic_history"]
        if query_lower in entry["device"].lower() or query_lower in entry["issue"].lower() or query_lower in entry["solution"].lower()
    ]
    if not matches:
        return f"🔍 По запросу '{query}' в истории диагностик ничего не найдено."
    
    result = f"🔍 Найдено {len(matches)} записей по запросу '{query}':\n\n"
    for m in matches[:5]: # Показываем топ-5 совпадений
        result += f"📅 {m['date']} | 🖥️ {m['device']}\n🔴 Проблема: {m['issue']}\n🟢 Решение: {m['solution']}\n{'-'*40}\n"
    return result

def get_chat_history(device_id):
    mem = load_memory()
    return mem.get("chat_histories", {}).get(str(device_id), [])

def append_chat_history(device_id, role, content):
    mem = load_memory()
    key = str(device_id)
    mem.setdefault("chat_histories", {})
    mem["chat_histories"].setdefault(key, [])
    mem["chat_histories"][key].append({"role": role, "content": content, "time": datetime.now().isoformat()})
    if len(mem["chat_histories"][key]) > 40:
        mem["chat_histories"][key] = mem["chat_histories"][key][-40:]
    save_memory(mem)
