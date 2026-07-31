import uuid
import os
import json
import re
import asyncio
import shutil
import time
from datetime import datetime
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from pydantic import BaseModel

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.tools import tool


from src.agent.prompts import SYSTEM_PROMPT
from src.utils.memory import load_memory, save_memory, set_active_switch, get_active_switch, get_chat_history, append_chat_history, get_switch_config as _get_switch_config, get_switch_logs as _get_switch_logs, connect_to_switch
from src.utils.telegram import is_telegram_enabled

load_dotenv()

class _SkipAgent(Exception):
    """Пропустить обработку агента, final_response уже установлен."""
    pass

def _is_write_request(user_msg_lower: str) -> bool:
    write_keywords = [
        "настрой", "настроить", "измени", "изменить", "добавь", "добавить",
        "удали", "удалить", "включи", "включить", "выключи", "выключить",
        "запусти", "запустить", "останови", "остановить", "примени",
        "применить", "установи", "установить", "создай", "создать",
        "раздач", "wifi", "wireless", "firewall", "vlan", "dhcp", "nat",
        "bridge", "interface", "порт", "port", "ssid", "ip address",
        "восстанови", "восстановить", "upload", "apply", "commit", "backup", "бэкап"
    ]
    return any(k in user_msg_lower for k in write_keywords)


def _is_simple_read_request(user_msg_lower: str) -> bool:
    simple_keywords = [
        "статистик", "статус", "uptime", "нагрузка", "трафик", "порт",
        "port", "соединен", "connection", "established", "запущен",
        "running", "интерфейс", "interface", "ip address", "ip addr",
        "маршрут", "route", "vlan", "показать", "покажи", "отобрази",
        "выведи", "напиши", "расскажи", "сколько", "какой", "какая",
        "какие", "что", "где", "когда", "айпи", "ip ", "мак",
        "mac", "arp", "таблица", "таблиц", "лог", "log", "журнал",
        "ошибк", "error", "warn", "fail", "down", "включен",
        "выключен", "disable", "enable", "link", "статус порта",
        "порт", "port status", "sfp", "оптик", " fiber"
    ]
    return any(k in user_msg_lower for k in simple_keywords)


async def safe_ws_send(websocket: WebSocket, payload: dict):
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(payload)
    except Exception as e:
        print(f"❌ Ошибка отправки WS: {e}", flush=True)

def _is_tool_call_error(e: Exception) -> bool:
    err_str = str(e)
    return "400" in err_str or "DEGRADED" in err_str or "function" in err_str.lower()



def _get_tokenizer():
    try:
        import tiktoken
        return tiktoken.encoding_for_model("gpt-4o")
    except Exception:
        return None


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_tokenizer()
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _truncate_text(text: str, max_tokens: int) -> str:
    if _count_tokens(text) <= max_tokens:
        return text
    enc = _get_tokenizer()
    if enc:
        try:
            tokens = enc.encode(text)
            truncated = enc.decode(tokens[:max_tokens])
            return truncated + "\n\n... (обрезано по токенам)"
        except Exception:
            pass
    approx_chars = max_tokens * 4
    return text[:approx_chars] + "\n\n... (обрезано по токенам)"


def _extract_text(msg) -> str:
    content = getattr(msg, "content", None)
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("text"):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        text = "\n".join(parts)
    else:
        text = str(content) if content else ""

    text = text.strip()
    if text:
        return text

    extra = getattr(msg, "additional_kwargs", {}) or {}
    reasoning = extra.get("reasoning_content") or extra.get("reasoning")
    if reasoning and isinstance(reasoning, str):
        return reasoning.strip()

    meta = getattr(msg, "response_metadata", {}) or {}
    meta_text = meta.get("content") or meta.get("text") or meta.get("message")
    if meta_text and isinstance(meta_text, str):
        return meta_text.strip()

    return ""


async def _retry_call(coro_factory, max_retries=3, timeout=180.0, fallback=None, is_write=False):
    for attempt in range(max_retries):
        try:
            return await asyncio.wait_for(coro_factory(), timeout=timeout)
        except GraphRecursionError as e:
            print(f"🔄 Агент зациклился на вызовах инструментов (recursion limit), переключаюсь...", flush=True)
            if fallback is not None:
                try:
                    return await asyncio.wait_for(fallback(), timeout=timeout)
                except Exception as e2:
                    raise Exception(f"Резервный LLM тоже не смог ответить: {e2}")
            raise
        except asyncio.TimeoutError as e:
            if fallback is not None:
                print(f"🔄 Таймаут Groq ({timeout}с), переключаюсь на NVIDIA...", flush=True)
                try:
                    return await asyncio.wait_for(fallback(), timeout=timeout)
                except Exception as e2:
                    raise Exception(f"Резервный LLM тоже не ответил за {timeout}с. Первый: {e}. Резервный: {e2}")
            raise
        except Exception as e:
            err_str = str(e)
            is_rate_limit = (
                "429" in err_str or
                "rate_limit" in err_str.lower() or
                "tokens per minute" in err_str.lower() or
                "tokens per day" in err_str.lower() or
                "Request too large" in err_str or
                "TPD" in err_str or
                "TPM" in err_str or
                ("503" in err_str and "ResourceExhausted" in err_str) or
                "resource_exhausted" in err_str.lower() or
                "total request limit reached" in err_str.lower()
            )
            if is_rate_limit:
                is_daily_limit = "TPD" in err_str or "tokens per day" in err_str.lower()
                is_nvidia_limit = "503" in err_str or "resource_exhausted" in err_str.lower() or "total request limit reached" in err_str.lower()
                if is_daily_limit and fallback is not None:
                    print(f"🔄 Дневной лимит Groq исчерпан, переключаюсь на NVIDIA...", flush=True)
                    try:
                        return await asyncio.wait_for(fallback(), timeout=timeout)
                    except Exception as e2:
                        raise Exception(f"Оба LLM недоступны. Groq: дневной лимит исчерпан. NVIDIA: {e2}")
                if is_daily_limit and fallback is None:
                    raise Exception(f"Groq дневной лимит исчерпан ({err_str}). Укажите NVIDIA API ключ.")
                if is_nvidia_limit and fallback is None and attempt < max_retries - 1:
                    wait = min(2 ** attempt * 2, 15)
                    print(f"⏳ NVIDIA лимит исчерпан (попытка {attempt+1}/{max_retries}), жду {wait}с...", flush=True)
                    await asyncio.sleep(wait)
                    continue
                if is_nvidia_limit and fallback is None:
                    raise Exception(f"NVIDIA API временно недоступен (превышен лимит запросов). Попробуйте позже.")
                if attempt < max_retries - 1:
                    wait = min(2 ** attempt, 10)
                    print(f"⏳ Rate limit/размер запроса (попытка {attempt+1}/{max_retries}), жду {wait}с...", flush=True)
                    await asyncio.sleep(wait)
                    continue
                if fallback is not None:
                    print(f"🔄 Переключаюсь на резервный LLM (NVIDIA)...", flush=True)
                    try:
                        return await asyncio.wait_for(fallback(), timeout=timeout)
                    except Exception as e2:
                        err_str2 = str(e2)
                        is_rate_limit2 = (
                            "429" in err_str2 or
                            "rate_limit" in err_str2.lower() or
                            "tokens per minute" in err_str2.lower() or
                            "tokens per day" in err_str2.lower() or
                            "Request too large" in err_str2 or
                            "TPD" in err_str2 or
                            "TPM" in err_str2 or
                            ("503" in err_str2 and "ResourceExhausted" in err_str2) or
                            "resource_exhausted" in err_str2.lower() or
                            "total request limit reached" in err_str2.lower()
                        )
                        if is_rate_limit2:
                            raise Exception(f"Оба LLM исчерпали лимиты. Первый: {err_str}. NVIDIA: {err_str2}")
                        raise e2
            raise


async def _execute_routeros_commands(active: dict, commands: list[str]) -> str:
    try:
        from src.mcp_servers.mikrotik_diag import call_tool
    except Exception as e:
        return f"❌ Ошибка загрузки SSH: {e}"
    
    host = active.get("ip")
    username = active.get("username")
    password = active.get("password")
    
    # Фильтруем пустые команды
    filtered_commands = [cmd.strip() for cmd in commands if cmd.strip()]
    if not filtered_commands:
        return "⚠️ Нет команд для выполнения"
    
    # Для отладки - выводим команды
    print(f"🔧 Команды для выполнения ({len(filtered_commands)}):", flush=True)
    for i, cmd in enumerate(filtered_commands, 1):
        print(f"  {i}. {cmd}", flush=True)
    
    # Объединяем все команды в одну строку с разделителем новой строки
    # MCP сервер умеет обрабатывать несколько команд за одно подключение
    combined_command = "\n".join(filtered_commands)
    
    # Определяем, нужен ли confirm (если есть команды изменения конфигурации)
    has_config_commands = any(k in combined_command.lower() for k in 
                            ["add", "set", "enable", "disable", "create", "remove", "delete"])
    
    args = {
        "host": host,
        "username": username,
        "password": password,
        "command": combined_command,
        "confirm": "true" if has_config_commands else "false",
        "device_type": active.get("device_type", "mikrotik_routeros"),
    }
    
    try:
        r = await call_tool("apply_routeros_config", args)
        text = r[0].text if r else "Нет результата"
        
        # Анализируем результат
        if "failure:" in text.lower() or "syntax error" in text.lower() or "invalid value" in text.lower():
            # Есть ошибки RouterOS
            return f"⚠️ На {host} есть ошибки RouterOS:\n\n{text}"
        elif "❌" in text:
            # Ошибка подключения или выполнения
            return text
        else:
            # Успешное выполнение
            return text
            
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Исключение при выполнении команд: {error_msg}", flush=True)
        return f"❌ Ошибка выполнения команд: {error_msg}"


def _is_mikrotik_wifi_write(user_msg_lower: str, active: dict | None, is_write: bool) -> bool:
    if not is_write or not active:
        return False
    if not str(active.get("device_type", "")).startswith("mikrotik"):
        return False
    return any(k in user_msg_lower for k in ("wifi", "wi-fi", "вай", "wireless", "ssid", "раздач"))


def _extract_wifi_params(user_msg: str) -> dict:
    params = {}
    msg_lower = user_msg.lower()

    password_patterns = [
        r"пароль\s+(\S+)",
        r"password\s+(\S+)",
        r"psk\s+(\S+)",
        r"ключ\s+(\S+)",
        r"wpa-pre-shared-key\s+(\S+)",
        r"пароль:\s*(\S+)",
        r"password:\s*(\S+)",
        r"пароль\s+([^\s,;]+)",  # Более гибкий паттерн
        r"password\s+([^\s,;]+)",
        r"wpa.*key[=:\s]+(\S+)",
    ]
    for pattern in password_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            params["password"] = m.group(1)
            # Убираем возможные кавычки
            params["password"] = params["password"].strip('"\'').strip()
            break

    ssid_patterns = [
        r"название\s+(?:сети|ssid|wifi)\s+(\S+)",
        r"ssid\s+(\S+)",
        r"сеть\s+(\S+)",
        r"имя\s+(?:сети|ssid|wifi)\s+(\S+)",
        r"название[:\s]+(\S+)",
        r"названи[ея]\s+сет[ии]\s+(\S+)",
        r"сеть[:\s]+(\S+)",
        r"название\s+сети\s+([^\s,;]+)",
        r"сеть\s+([^\s,;]+)",
        r"ssid[=:\s]+(\S+)",
    ]
    for pattern in ssid_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            params["ssid"] = m.group(1)
            # Убираем возможные кавычки
            params["ssid"] = params["ssid"].strip('"\'').strip()
            break

    profile_patterns = [
        r"profile\s+(\S+)",
        r"профиль\s+(\S+)",
        r"security-profile\s+(\S+)",
        r"профиль[:\s]+(\S+)",
    ]
    for pattern in profile_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            params["profile"] = m.group(1)
            params["profile"] = params["profile"].strip('"\'').strip()
            break
    
    # Если не указан профиль, используем дефолтный
    if "profile" not in params:
        params["profile"] = "default"

    mode_patterns = [
        r"режим\s+(\S+)",
        r"mode\s+(\S+)",
        r"режим[:\s]+(\S+)",
    ]
    for pattern in mode_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            mode_val = m.group(1).lower()
            # Нормализуем режим
            if "dynamic" in mode_val:
                params["mode"] = "dynamic-keys"
            elif "static" in mode_val:
                params["mode"] = "static-keys"
            else:
                params["mode"] = "dynamic-keys"  # default
            break
    
    if "mode" not in params:
        params["mode"] = "dynamic-keys"

    auth_patterns = [
        r"аунтетификации?\s+(\S+)",
        r"аутентификации?\s+(\S+)",
        r"authentication\s+(\S+)",
        r"тип\s+аутентификации?\s+(\S+)",
        r"тип[:\s]+(\S+)",
        r"authentication[-\s]types?\s+(\S+)",
    ]
    for pattern in auth_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            val = m.group(1).lower()
            if "wpa2" in val or "wpa2-psk" in val:
                params["auth_type"] = "wpa2-psk"
            elif "wpa" in val:
                params["auth_type"] = "wpa-psk"
            else:
                params["auth_type"] = "wpa2-psk"  # default
            break
    
    if "auth_type" not in params:
        params["auth_type"] = "wpa2-psk"

    antenna_patterns = [
        r"антенна\s+(\d+)",
        r"antenna-gain\s+(\d+)",
        r"gain\s+(\d+)",
        r"мощность\s+(\d+)",
        r"сигнал\s+(\d+)",
        r"antenna[-\s]gain[:\s]+(\d+)",
    ]
    for pattern in antenna_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            gain = m.group(1)
            # Проверяем, что это число
            if gain.isdigit():
                gain_int = int(gain)
                # Ограничиваем разумными значениями
                if gain_int < 0:
                    gain_int = 0
                elif gain_int > 30:
                    gain_int = 30
                params["antenna_gain"] = str(gain_int)
            break

    if "antenna_gain" not in params:
        params["antenna_gain"] = "14"

    return params


def _extract_routeros_commands(text: str) -> list[str]:
    commands = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("/") and not line.startswith("//") and not line.startswith("#"):
            clean = line.strip()
            if clean:
                commands.append(clean)
    if not commands:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and len(line) > 3:
                commands.append(line)
    return commands


async def _configure_mikrotik_wifi_directly(active: dict, wifi_params: dict) -> str:
    """Прямая настройка WiFi на MikroTik без использования LLM"""
    import asyncio
    
    # Извлекаем параметры с дефолтными значениями
    password = wifi_params.get("password", "")
    ssid = wifi_params.get("ssid", "Switch")
    profile = wifi_params.get("profile", "default")
    mode = wifi_params.get("mode", "dynamic-keys")
    auth_type = wifi_params.get("auth_type", "wpa2-psk")
    antenna_gain = wifi_params.get("antenna_gain", "14")
    
    # Проверяем обязательные параметры
    if not password:
        return "❌ Ошибка: не указан пароль для WiFi"
    
    # Проверяем длину пароля для WPA2-PSK
    if len(password) < 9:
        return f"❌ Ошибка: пароль для WPA2-PSK должен быть минимум 9 символов (RouterOS v7 требует >8, получено: {len(password)}). Используйте пароль длиной 9-64 символа."
    if len(password) > 64:  # RouterOS ограничение 64 символа
        return f"❌ Ошибка: пароль слишком длинный (максимум 64 символа, получено: {len(password)})"
    
    # Проверяем специальные символы в пароле - RouterOS может быть чувствителен
    # Лучше использовать только буквы, цифры и базовые символы
    invalid_chars = ['"', "'", ';', '\\', '`', '$', '&', '|', '<', '>']
    for char in invalid_chars:
        if char in password:
            return f"❌ Ошибка: пароль содержит недопустимый символ '{char}'"
    
    # Проверяем, что пароль состоит из печатных ASCII символов
    try:
        password.encode('ascii')
    except UnicodeEncodeError:
        return "❌ Ошибка: пароль должен содержать только ASCII символы"
    
    print(f"🔧 Прямая настройка WiFi с параметрами:", flush=True)
    print(f"  SSID: {ssid}", flush=True)
    print(f"  Пароль: {password}", flush=True)
    print(f"  Профиль: {profile}", flush=True)
    print(f"  Режим: {mode}", flush=True)
    print(f"  Тип аутентификации: {auth_type}", flush=True)
    print(f"  Мощность антенны: {antenna_gain}", flush=True)
    
    # Сначала получим текущую конфигурацию, чтобы понять состояние
    try:
        check_cmds = [
            "/interface wireless security-profiles print detail",
            "/interface wireless print detail"
        ]
        
        print(f"🔧 Проверяю текущую конфигурацию...", flush=True)
        current_config = await _execute_routeros_commands(active, check_cmds)
        
        # Анализируем текущую конфигурацию
        if "mode=none" in current_config.lower() and "authentication-types=wpa2-psk" in current_config.lower():
            print(f"⚠️ Обнаружен профиль с mode=none но с wpa2-psk", flush=True)
            # Нужно пересоздать профиль или исправить режим
            commands = []
            
            # Вариант 1: Удалить и создать новый профиль
            # /interface wireless security-profiles remove [find name=default]
            # /interface wireless security-profiles add name=default mode=dynamic-keys authentication-types=wpa2-psk wpa-pre-shared-key=пароль
            
            # Вариант 2: Исправить существующий профиль правильно
            # Для WPA2-PSK нужно использовать wpa2-pre-shared-key
            commands.append("/interface wireless security-profiles set [find name=default] mode=dynamic-keys authentication-types=wpa2-psk")
            commands.append(f"/interface wireless security-profiles set [find name=default] wpa2-pre-shared-key={password}")
            
        else:
            # Обычная настройка
            commands = []
            
            # 1. Настраиваем security profile
            # Для WPA2-PSK используем wpa2-pre-shared-key
            commands.append(
                f"/interface wireless security-profiles set [find name=default] "
                f"mode={mode} authentication-types={auth_type} wpa2-pre-shared-key={password}"
            )
        
        # 2. Настраиваем беспроводной интерфейс
        commands.append(
            f"/interface wireless set [find] disabled=no mode=ap-bridge ssid={ssid} "
            f"security-profile={profile} antenna-gain={antenna_gain}"
        )
        
        # 3. Включаем интерфейс
        commands.append("/interface wireless enable [find]")
        
        print(f"🔧 Выполняю {len(commands)} команд настройки WiFi", flush=True)
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. {cmd}", flush=True)
        
        exec_result = await _execute_routeros_commands(active, commands)
        
        # Проверяем наличие ошибок
        if "❌" in exec_result or "failure:" in exec_result.lower():
            # Пробуем альтернативный подход - удалить и создать заново
            print(f"⚠️ Первый подход не сработал, пробую альтернативный...", flush=True)
            
            alt_commands = [
                f"/interface wireless security-profiles remove [find name=default]",
                f"/interface wireless security-profiles add name=default mode={mode} authentication-types={auth_type} wpa2-pre-shared-key={password}",
                f"/interface wireless set [find] disabled=no mode=ap-bridge ssid={ssid} security-profile=default antenna-gain={antenna_gain}",
                f"/interface wireless enable [find]"
            ]
            
            print(f"🔧 Выполняю альтернативные команды ({len(alt_commands)})", flush=True)
            alt_result = await _execute_routeros_commands(active, alt_commands)
            
            if "❌" in alt_result or "failure:" in alt_result.lower():
                return f"❌ Ошибка при настройке WiFi (оба подхода не сработали):\n\nПервый подход:\n{exec_result}\n\nВторой подход:\n{alt_result}"
            
            exec_result = alt_result
        
        # Ждем немного для применения настроек
        await asyncio.sleep(1)
        
        # Проверяем результат
        check_result = await _execute_routeros_commands(active, [
            "/interface wireless security-profiles print detail",
            "/interface wireless print detail"
        ])
        
        # Формируем отчет
        summary = f"🔧 Настройка WiFi завершена:\n"
        summary += f"- SSID: {ssid}\n"
        summary += f"- Пароль: {'установлен' if password else 'не установлен'}\n"
        summary += f"- Мощность антенны: {antenna_gain}\n\n"
        
        summary += f"Результат выполнения:\n{exec_result}\n\n"
        summary += f"Проверка конфигурации:\n{check_result}"
        
        # Проверяем, есть ли пароль в security profile
        if password and password.lower() not in check_result.lower():
            summary += f"\n⚠️ Внимание: пароль не найден в конфигурации security profile"
        
        if "disabled=yes" in check_result.lower():
            summary += f"\n⚠️ Внимание: беспроводной интерфейс отключен"
        
        return summary
        
    except Exception as e:
        return f"❌ Исключение при настройке WiFi:\nОшибка: {str(e)}\n\nТип ошибки: {type(e).__name__}"

# --- НАЧАЛО: Прямое подключение RAG (без MCP) ---
DB_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../chroma_db"))
print("⏳ Загрузка локальной базы знаний...")
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
rag_db = Chroma(persist_directory=DB_PATH, embedding_function=embeddings, collection_name="network_docs")
print("✅ База знаний загружена.")

@tool
async def search_pdf_knowledge_base(query: str) -> str:
    """Ищет информацию в мануалах Zyxel/Cudy и возвращает ПЕРЕВЕДЁННЫЙ на русский ответ.
    Используй при запросах про настройку/инструкции (VLAN, IP Setup, Interface VLAN и т.п.)."""
    print(f"\n🔧 [TOOL START] Выполняю поиск по запросу: '{query}'", flush=True)
    try:
        results = rag_db.similarity_search(query, k=3)
        print(f"✅ [TOOL END] Найдено {len(results)} фрагментов.", flush=True)

        if not results:
            return f"❌ В базе знаний ничего не найдено по запросу: '{query}'. Попробуй ключевые слова: 'VLAN 16 IP address setup' или 'Interface VLAN'."

        parts = []
        for doc in results:
            page = doc.metadata.get("page", "?")
            source = doc.metadata.get("source", "Неизвестный файл")
            text = " ".join(doc.page_content.split())
            if len(text) > 1500:
                text = text[:1500] + "…"
            parts.append(f"[Источник: {source}, Стр. {page}]\n{text}")
        context_str = "\n\n---\n\n".join(parts)

        if llm is None:
            return f"📚 Найдено в мануале (оригинал):\n\n{context_str}"

        prompt = f"""Ты — старший сетевой инженер, эксперт по коммутаторам Zyxel и Cudy.
Дай пользователю чёткий ответ СТРОГО на русском языке по его вопросу, опираясь ТОЛЬКО на фрагменты мануала.

ВОПРОС ПОЛЬЗОВАТЕЛЯ: "{query}"

ФРАГМЕНТЫ ИЗ МАНУАЛА (могут быть на английском):
{context_str}

ПРАВИЛА:
1. Отвечай только по-русски, переведи нужные места из мануала.
2. Структура: 📌 Кратко · 🛠️ Пошагово (точные названия полей/кнопок) · ⚠️ Важно (про сохранение) · 📄 Источник (страница).
3. Если в мануале нет ответа — честно напиши об этом.
4. Без лишней воды."""

        response = await llm.ainvoke([
            SystemMessage(content="Ты — краткий и точный сетевой инженер. Всегда отвечай по-русски."),
            HumanMessage(content=prompt)
        ])
        return response.content
    except Exception as e:
        print(f"❌ [TOOL ERROR] {e}", flush=True)
        return f"❌ Ошибка поиска: {str(e)}"

@tool
def get_switch_config(host: str, username: str, password: str, device_type: str = "zyxel_os") -> str:
    """Собирает РЕАЛЬНУЮ текущую конфигурацию коммутатора по SSH. Возвращает полный вывод всех команд.
    Для MikroTik: /system resource print, /interface print, /ip address print, /ip route print, /ip firewall filter print, /ip nat print, /export.
    Для Zyxel: show vlan, show interface, show config и т.д.
    ДОВЕРЕННЫЙ ИНСТРУМЕНТ — /export разрешён для MikroTik. Вызывай при запросах 'конфигурация', 'настройки', 'текущие параметры'."""
    result = _get_switch_config(host, username, password, device_type)
    if isinstance(result, dict):
        if result.get("ok"):
            return result.get("config", "Нет данных")
        else:
            return f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}"
    return str(result)

@tool
def get_switch_logs_tool(host: str, username: str, password: str, device_type: str = "zyxel_os", only_errors: bool = False) -> str:
    """Читает логи коммутатора по SSH. Для ZyNOS: show log all, show log buffered. Для MikroTik: /log print."""
    result = _get_switch_logs(host, username, password, device_type, only_errors)
    if isinstance(result, dict):
        if result.get("ok"):
            return result.get("logs", "Нет логов")
        else:
            return f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}"
    return str(result)

def _tool_to_dict(t):
    if isinstance(t, dict):
        return t
    schema = getattr(t, "args_schema", None) or getattr(t, "input_schema", None)
    if schema is not None and hasattr(schema, 'model_json_schema'):
        schema = schema.model_json_schema()
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": schema or {},
    }

def _build_connect_tool(active):
    @tool
    def connect_switch(host: str, username: str, password: str, device_type: str = "zyxel_os", name: str = "", command: str = "show version") -> str:
        """Подключается к коммутатору по SSH, проверяет связь и делает его АКТИВНЫМ. Обязательно вызывай при запросах 'подключись', 'проверь подключение', 'покажи что подключён'. Возвращает подтверждение и вывод проверочной команды."""
        out = connect_to_switch(host, username, password, device_type, command)
        if out["ok"]:
            model = out["version"].strip().splitlines()[0] if out["version"].strip() else ""
            dev_name = name or host
            set_active_switch(dev_name, host, model, username, password, device_type)
            return (
                f"✅ Подключено к {host} ({device_type}). Устройство СДЕЛАНО АКТИВНЫМ.\n\n"
                f"📡 Ответ `{command}`:\n{out['probe']}\n\n"
                f"📋 Описание (show version):\n{out['version'][:600]}"
            )
        return f"❌ {out['error']} Коммутатор НЕ сделан активным."

    return connect_switch

agent = None
tools = []
mcp_client = None
llm = None
agent_lock = asyncio.Lock()
MAX_AGENT_RECURSION_READ = 15
MAX_AGENT_RECURSION_WRITE = 20
MAX_AGENT_RECURSION_READ_FALLBACK = 15
MAX_AGENT_RECURSION_WRITE_FALLBACK = 20

def index_pdf_file(file_path: str):
    if not os.path.exists(file_path):
        return {"ok": False, "error": "Файл не найден"}
    if rag_db is None:
        return {"ok": False, "error": "База знаний не инициализирована"}
    
    try:
        loader = PyMuPDFLoader(file_path)
        docs = loader.load()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        chunks = text_splitter.split_documents(docs)
        file_name = os.path.basename(file_path)
        for chunk in chunks:
            chunk.metadata["source"] = file_name
        
        rag_db.add_documents(chunks)
        return {"ok": True, "chunks": len(chunks)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, tools, mcp_client, llm, llm_write, rag_db
    
    print("🚀 Запуск агента сетевой диагностики...", flush=True)
    config_path = os.path.join(os.path.dirname(__file__), "../../mcp_config.json")
    config_path = os.path.normpath(config_path)
    with open(config_path) as f:
        config = json.load(f)
    
    mcp_servers = {
        name: {k: v for k, v in s.items() if k in ["command", "args", "transport"]}
        for name, s in config["mcp_servers"].items()
        if s.get("enabled", True)
    }

    # Используем venv python для MCP-серверов, если они запускаются через python
    import sys as _sys
    _venv_python = _sys.executable
    for name, server in mcp_servers.items():
        if server.get("command", "").strip() == "python":
            server["command"] = _venv_python
            print(f"  🔧 MCP '{name}': python → {_venv_python}", flush=True)

    default_docs_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../docs"))
    docs_dir = os.getenv("DOCS_DIR", default_docs_dir)
    if "filesystem" in mcp_servers:
        fs_args = list(mcp_servers["filesystem"].get("args", []))
        if fs_args:
            fs_args[-1] = docs_dir
        mcp_servers["filesystem"]["args"] = fs_args
    
    print(f" Инициализация MCP серверов: {list(mcp_servers.keys())} (docs: {docs_dir})", flush=True)
    
    global rag_db
    if rag_db is None:
        print("⏳ Переинициализация базы знаний...", flush=True)
        embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        rag_db = Chroma(persist_directory=DB_PATH, embedding_function=embeddings, collection_name="network_docs")
        print("✅ База знаний переинициализирована.", flush=True)
    
    mcp_client = MultiServerMCPClient(mcp_servers)
    mcp_tools = []
    try:
        mcp_tools = await mcp_client.get_tools()
        print(f"✅ MCP инструментов загружено: {len(mcp_tools)}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка загрузки MCP инструментов: {e}", flush=True)
        print("⚠️ Продолжаем без MCP-инструментов.", flush=True)
        # Создаём пустой клиент, чтобы close не упал
        mcp_client = MultiServerMCPClient({})
    
    tools = mcp_tools + [search_pdf_knowledge_base, get_switch_config, get_switch_logs_tool]
    print(f"✅ Загружено инструментов: {len(tools)}", flush=True)
    
    llm = ChatOpenAI(
        model=os.getenv("NVIDIA_MODEL_NAME", "meta/llama-3.3-70b-instruct"),
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.getenv("NVIDIA_API_KEY"),
        temperature=0.2,
        max_tokens=2000,
        timeout=180,
        max_retries=1
    )
    
    llm_write = ChatOpenAI(
        model=os.getenv("NVIDIA_MODEL_NAME2", "meta/llama-3.3-70b-instruct"),
        base_url=os.getenv("NVIDIA_BASE_URL2", "https://integrate.api.nvidia.com/v1"),
        api_key=os.getenv("NVIDIA_API_KEY2") or os.getenv("NVIDIA_API_KEY"),
        temperature=0.2,
        max_tokens=4000,
        timeout=120,
        max_retries=1
    )
    
    agent = create_agent(llm, tools)
    print("🎉 АГЕНТ ГОТОВ К ДИАГНОСТИКЕ!", flush=True)
    
    if is_telegram_enabled():
        try:
            import threading
            from src.monitor_service import main as monitor_main
            def _run():
                import asyncio
                asyncio.run(monitor_main())
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            print("🤖 Фоновый мониторинг логов запущен в отдельном потоке.", flush=True)
        except Exception as e:
            print(f"⚠️ Не удалось запустить фоновый мониторинг: {e}", flush=True)
    
    yield
    
    if mcp_client and hasattr(mcp_client, 'close'):
        await mcp_client.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    from fastapi.responses import Response
    content = open("static/index.html", "r", encoding="utf-8").read()
    return Response(content=content, media_type="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

class ChatReq(BaseModel):
    message: str
    device_id: str = ""
    request_id: str = ""

@app.get("/api/health")
async def health():
    return {"status": "ok" if agent else "loading", "tools_count": len(tools)}

@app.get("/api/devices")
async def list_devices():
    mem = load_memory()
    devices = mem.get("devices", [])
    for i, d in enumerate(devices):
        d["id"] = str(i)
    return devices

@app.post("/api/devices")
async def create_device(device: dict):
    mem = load_memory()
    devices = mem.setdefault("devices", [])
    device["id"] = device.get("ip") or str(uuid.uuid4())
    device["added"] = datetime.now().strftime("%Y-%m-%d")
    devices.append(device)
    save_memory(mem)
    return device

@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    mem = load_memory()
    devices = mem.get("devices", [])
    mem["devices"] = [d for d in devices if d.get("id") != device_id]
    save_memory(mem)
    return {"ok": True}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Разрешены только PDF файлы")
    
    docs_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../docs"))
    os.makedirs(docs_dir, exist_ok=True)
    
    file_path = os.path.join(docs_dir, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    if rag_db is not None:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, index_pdf_file, file_path)
    
    return {"message": f"Файл {file.filename} загружен и отправлен на индексацию в базу знаний.", "path": file_path}

@app.get("/api/documents")
async def list_documents():
    if rag_db is None:
        return {"documents": []}
    try:
        result = rag_db.get()
        counts = {}
        if result and result.get("metadatas"):
            for meta in result["metadatas"]:
                if isinstance(meta, dict):
                    src = meta.get("source")
                    if src:
                        counts[src] = counts.get(src, 0) + 1
        final = [{"name": k, "chunks": v} for k, v in counts.items()]
        final.sort(key=lambda x: x["name"])
        return {"documents": final}
    except Exception as e:
        return {"documents": [], "error": str(e)}

@app.get("/api/policies")
async def list_policies():
    from src.utils.memory import get_command_policies
    return get_command_policies()

@app.get("/api/policies/{key}")
async def get_policy(key: str):
    from src.utils.memory import get_command_policies
    policies = get_command_policies()
    if key not in policies:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"key": key, **policies[key]}

@app.post("/api/policies/{key}")
async def set_policy(key: str, policy: dict):
    from src.utils.memory import set_command_policy
    set_command_policy(key, policy)
    return {"ok": True, "key": key}

@app.delete("/api/policies/{key}")
async def delete_policy(key: str):
    from src.utils.memory import delete_command_policy
    if not delete_command_policy(key):
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"ok": True}

@app.get("/api/chat_history/{device_id}")
async def get_chat_history_api(device_id: str):
    from src.utils.memory import get_chat_history
    return {"messages": get_chat_history(device_id)}

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    if not llm:
        await safe_ws_send(websocket, {"type": "error", "message": "LLM не инициализирован"})
        return
    
    # Глобальный lock для обработки запросов (один за раз)
    global_processing_lock = asyncio.Lock()
    # Очередь сообщений
    message_queue = asyncio.Queue()
    queue_running = False
    
    await safe_ws_send(websocket, {"type": "connected", "tools": len(tools)})
    
    async def safe_send(payload: dict):
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(payload)
        except Exception as e:
            print(f"⚠️ WS send error: {e}", flush=True)
    
    async def process_one_message():
        nonlocal queue_running
        try:
            raw_data = await message_queue.get()
        except asyncio.CancelledError:
            return

        if websocket.client_state != WebSocketState.CONNECTED:
            print("⚠️ WS disconnected, dropping message", flush=True)
            return

        try:
            try:
                req = ChatReq(**json.loads(raw_data))
            except:
                req = ChatReq(message=raw_data)
            
            user_msg = req.message.strip()
            device_id = getattr(req, 'device_id', '') or ''
            request_id = getattr(req, 'request_id', '') or ''
            print(f"\n📤 Запрос: {user_msg} [device={device_id}] [req={request_id}]", flush=True)
            
            await safe_send({"type": "thinking_start", "message": "🧠 Анализирую...", "request_id": request_id, "device_id": device_id})
            
            if not hasattr(websocket, 'pending_requests'):
                websocket.pending_requests = {}
            websocket.pending_requests[request_id] = device_id
            
            final_response = "⚠️ Произошла неизвестная ошибка при обработке запроса."
            user_msg_lower = user_msg.lower()
            is_write = _is_write_request(user_msg_lower)
            is_simple_read = not is_write and _is_simple_read_request(user_msg_lower)
            current_llm = llm_write if is_write else llm
            print(f"🔄 Использую агента... (тип: {'write' if is_write else 'simple_read' if is_simple_read else 'read'}, LLM: {'NVIDIA' if is_write else 'Groq'})", flush=True)
            
            show_config_kw = ("покажи конфигураци", "покажи настройк", "текущая конфигураци", "покажи конфиг", "текущие настройк", "текущие параметры", "покажи конфигурацию", "покажи все настройки", "покажи текущую конфигураци", "покажи текущие настройк", "расскажи про конфигураци", "расскажи конфигураци", "прочитай конфигураци", "покажи конфигурацию устройства", "покажи конфиг устройства", "отобрази конфигураци", "выведи конфигураци", "напиши конфигураци", "напиши конфиг", "конфигураци", "кофигураци", "конфиг")
            is_show_config = any(k in user_msg_lower for k in show_config_kw)

            # Прямая обработка WiFi запросов для MikroTik
            try:
                memory = load_memory()
                active = None
                if device_id:
                    devices = memory.get("devices", [])
                    for idx, dev in enumerate(devices):
                        dev_id = str(dev.get("id", ""))
                        dev_ip = str(dev.get("ip", ""))
                        if dev_id == str(device_id) or dev_ip == str(device_id) or str(idx) == str(device_id):
                            active = dev
                            break
                if not active:
                    active = memory.get("active_switch")
                
                if active and is_write and _is_mikrotik_wifi_write(user_msg_lower, active, is_write):
                    wifi_params = _extract_wifi_params(user_msg)
                    if "password" in wifi_params:
                        print(f"🔧 ПРЯМАЯ НАСТРОЙКА WiFi для {active.get('name', active.get('ip'))}", flush=True)
                        print(f"🔧 Использую улучшенную логику для MikroTik WiFi", flush=True)
                        try:
                            result = await _configure_mikrotik_wifi_directly(active, wifi_params)
                            raise _SkipAgent(result)
                        except _SkipAgent:
                            raise
                        except Exception as e:
                            print(f"❌ Ошибка прямой настройки WiFi: {e}, продолжаем с LLM", flush=True)
                            # Продолжаем с обычной обработкой через LLM
            except Exception as e:
                print(f"⚠️ Ошибка при проверке прямой обработки WiFi: {e}", flush=True)
            
            try:
                memory = load_memory()
                context_mem = ""
                active = None
                if device_id:
                    devices = memory.get("devices", [])
                    for idx, dev in enumerate(devices):
                        dev_id = str(dev.get("id", ""))
                        dev_ip = str(dev.get("ip", ""))
                        if dev_id == str(device_id) or dev_ip == str(device_id) or str(idx) == str(device_id):
                            active = dev
                            break
                if not active:
                    active = memory.get("active_switch")
                
                no_connect_kw = ("без подключения", "не подключайся", "не подключай", "только в базе", "только поиск", "pdf only", "search pdf", "в мануале", "в базе знаний", "поиск в базе", "найди в базе")
                is_pdf_only = any(k in user_msg_lower for k in no_connect_kw)

                config_fetched = False
                config_data = ""
                config_error = ""

                if (
                    active
                    and not is_pdf_only
                    and _is_mikrotik_wifi_write(user_msg_lower, active, is_write)
                    and not config_fetched
                ):
                    print(
                        f"🔧 АВТО-ЗАПРОС get_switch_config (wifi) для {active.get('name', active.get('ip'))}",
                        flush=True,
                    )
                    config_data = _get_switch_config(
                        active["ip"],
                        active["username"],
                        active["password"],
                        active.get("device_type", "mikrotik_routeros"),
                        topic="wifi",
                    )
                    if isinstance(config_data, dict):
                        if config_data.get("ok"):
                            config_fetched = True
                            config_data = config_data.get("config", "Нет данных")
                        else:
                            config_error = config_data.get("error", "Неизвестная ошибка")
                    else:
                        config_fetched = True
                        config_data = str(config_data)

                if active and is_show_config and not is_pdf_only:
                    print(f"🔧 АВТО-ЗАПРОС get_switch_config для {active.get('name', active.get('ip'))}", flush=True)
                    config_data = _get_switch_config(
                        active['ip'], active['username'], active['password'],
                        active.get('device_type', 'zyxel_os')
                    )
                    if isinstance(config_data, dict):
                        if config_data.get("ok"):
                            config_fetched = True
                            config_data = config_data.get("config", "Нет данных")
                            print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                        else:
                            config_error = config_data.get("error", "Неизвестная ошибка")
                            print(f"❌ get_switch_config ошибка: {config_error}", flush=True)
                    else:
                        config_fetched = True
                        config_data = str(config_data)
                        print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                elif active and is_simple_read and not is_pdf_only and not config_fetched:
                    print(f"🔧 АВТО-ЗАПРОС get_switch_config для simple_read {active.get('name', active.get('ip'))}", flush=True)
                    config_data = _get_switch_config(
                        active['ip'], active['username'], active['password'],
                        active.get('device_type', 'zyxel_os')
                    )
                    if isinstance(config_data, dict):
                        if config_data.get("ok"):
                            config_fetched = True
                            config_data = config_data.get("config", "Нет данных")
                            print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                        else:
                            config_error = config_data.get("error", "Неизвестная ошибка")
                            print(f"❌ get_switch_config ошибка: {config_error}", flush=True)
                    else:
                        config_fetched = True
                        config_data = str(config_data)
                        print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                elif active and not is_write and not is_simple_read and not is_pdf_only and not config_fetched:
                    print(f"🔧 АВТО-ЗАПРОС get_switch_config для read-запроса {active.get('name', active.get('ip'))}", flush=True)
                    config_data = _get_switch_config(
                        active['ip'], active['username'], active['password'],
                        active.get('device_type', 'zyxel_os')
                    )
                    if isinstance(config_data, dict):
                        if config_data.get("ok"):
                            config_fetched = True
                            config_data = config_data.get("config", "Нет данных")
                            print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                        else:
                            config_error = config_data.get("error", "Неизвестная ошибка")
                            print(f"❌ get_switch_config ошибка: {config_error}", flush=True)
                    else:
                        config_fetched = True
                        config_data = str(config_data)
                        print(f"✅ get_switch_config вернул данные (len={len(config_data)})", flush=True)
                
                if active:
                    if is_simple_read:
                        dev_name = active.get('name', active.get('ip'))
                        dev_ip = active['ip']
                        context_mem += (
                            f"\n[АКТИВНЫЙ КОММУТАТОР — РАБОТАЙ ТОЛЬКО С НИМ]:\n"
                            f"Имя: {dev_name} | IP: {dev_ip} | Тип: {active['device_type']} | "
                            f"Пользователь: {active['username']} | Пароль: {active['password']}\n"
                            f"У тебя есть инструменты для SSH: execute_routeros_command, get_switch_config, get_switch_logs. "
                            f"Используй их для реальных данных. НЕ генерируй симуляции.\n"
                        )
                    else:
                        policy = memory.get("command_policies", {}).get("mikrotik", {})
                        policy_info = ""
                        if active.get("device_type", "").startswith("mikrotik"):
                            readonly = policy.get("readonly_prefixes", [])
                            blocked = policy.get("blocked_patterns", [])
                            policy_info = f"\n[ПОЛИТИКА КОМАНД ДЛЯ MikroTik]:\n  Разрешённые: {', '.join(readonly[:8])}...\n  Заблокировано: {', '.join(blocked[:8])}...\n"
                        
                        if config_fetched:
                            wifi_write_hint = ""
                            if is_write and _is_mikrotik_wifi_write(user_msg_lower, active, is_write):
                                wifi_params = _extract_wifi_params(user_msg)
                                param_lines = []
                                if "password" in wifi_params:
                                    param_lines.append(f"wpa-pre-shared-key={wifi_params['password']}")
                                if "ssid" in wifi_params:
                                    param_lines.append(f"ssid={wifi_params['ssid']}")
                                if "profile" in wifi_params:
                                    param_lines.append(f"security-profile={wifi_params['profile']}")
                                if "mode" in wifi_params:
                                    param_lines.append(f"mode={wifi_params['mode']}")
                                if "auth_type" in wifi_params:
                                    param_lines.append(f"authentication-types={wifi_params['auth_type']}")
                                if "antenna_gain" in wifi_params:
                                    param_lines.append(f"antenna-gain={wifi_params['antenna_gain']}")
                                params_hint = " ".join(param_lines)
                                wifi_write_hint = (
                                    f"[ЗАДАЧА WiFi]:\n"
                                    f"ВНИМАНИЕ: ПАРАМЕТРЫ ИЗ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ — НЕ ИЗМЕНЯЙ ИХ:\n"
                                    f"  {params_hint}\n"
                                    f"План действий:\n"
                                    f"  1) Прочитай текущий конфиг выше и запомни текущие значения wlan и security-profile.\n"
                                    f"  2) Составь ПОЛНЫЙ план всех команд ДО их выполнения.\n"
                                    f"  3) Примени через apply_routeros_config (confirm=true) — ОТДЕЛЬНАЯ команда на каждую строку, не склеивай.\n"
                                    f"  4) Порядок: (a) security-profile: mode=dynamic-keys, authentication-types=wpa2-psk, wpa-pre-shared-key={wifi_params.get('password', '...')} (b) wireless: disabled=no, mode=ap-bridge, ssid={wifi_params.get('ssid', '...')}, security-profile={wifi_params.get('profile', 'default')}, antenna-gain={wifi_params.get('antenna_gain', '14')}.\n"
                                    f"  5) Проверь: /interface wireless security-profiles print detail — если authentication-types пустой или нет wpa2-psk, сеть будет БЕЗ пароля; исправь и проверь снова.\n"
                                    f"  6) Проверь: /interface wireless print detail — убедись что antenna-gain не 0 (0 = максимальное приглушение, 14 = минимальное затухание = максимальный сигнал).\n"
                                    f"  7) Кратко объясни пользователю что изменил и что показала проверка.\n"
                                    f"НЕ ИСПОЛЬЗУЙ ДРУГИЕ ЗНАЧЕНИЯ ПАРАМЕТРОВ КРОМЕ УКАЗАННЫХ ВЫШЕ!\n"
                                )
                            elif is_write:
                                wifi_write_hint = (
                                    "[ЗАДАЧА: проанализируй конфиг, составь план, примени через apply_routeros_config (confirm=true). "
                                    "После изменений проверь результат read-командами.]\n"
                                )
                            else:
                                wifi_write_hint = (
                                    "[ЗАДАЧА: АНАЛИЗИРУЙ ТОЛЬКО РЕАЛЬНУЮ КОНФИГУРАЦИЮ ВЫШЕ. НЕ СИМУЛИРУЙ.]\n"
                                )
                            context_mem += (
                                f"\n[АКТИВНЫЙ КОММУТАТОР — РАБОТАЙ ТОЛЬКО С НИМ]:\n"
                                f"Имя: {active.get('name', active.get('ip'))} | IP: {active['ip']} | Тип: {active['device_type']} | "
                                f"Пользователь: {active['username']} | Пароль: {active['password']}\n"
                                f"{policy_info}\n"
                                f"=== РЕАЛЬНАЯ КОНФИГУРАЦИЯ (ПОЛУЧЕНА ПО SSH) ===\n{config_data}\n=== КОНЕЦ КОНФИГУРАЦИИ ===\n"
                                f"{wifi_write_hint}"
                            )
                        else:
                            dev_name = active.get('name', active.get('ip'))
                            dev_ip = active['ip']
                            if is_write:
                                wifi_params_hint = ""
                                if _is_mikrotik_wifi_write(user_msg_lower, active, is_write):
                                    wifi_params = _extract_wifi_params(user_msg)
                                    param_parts = []
                                    if "password" in wifi_params:
                                        param_parts.append(f"wpa-pre-shared-key={wifi_params['password']}")
                                    if "ssid" in wifi_params:
                                        param_parts.append(f"ssid={wifi_params['ssid']}")
                                    if "profile" in wifi_params:
                                        param_parts.append(f"security-profile={wifi_params['profile']}")
                                    if "mode" in wifi_params:
                                        param_parts.append(f"mode={wifi_params['mode']}")
                                    if "auth_type" in wifi_params:
                                        param_parts.append(f"authentication-types={wifi_params['auth_type']}")
                                    if "antenna_gain" in wifi_params:
                                        param_parts.append(f"antenna-gain={wifi_params['antenna_gain']}")
                                    if param_parts:
                                        wifi_params_hint = (
                                            f"\n[ПАРАМЕТРЫ WiFi ИЗ СООБЩЕНИЯ — НЕ ИЗМЕНЯЙ ИХ]:\n"
                                            f"  {' '.join(param_parts)}\n"
                                            f"План: 1) get_switch_config (wireless) 2) Составь план команд 3) Примени через apply_routeros_config (confirm=true) "
                                            f"4) Проверь результат. НЕ выполняй команды без плана!"
                                        )
                                context_mem += (
                                    f"\n[АКТИВНЫЙ КОММУТАТОР — РАБОТАЙ ТОЛЬКО С НИМ]:\n"
                                    f"Имя: {dev_name} | IP: {dev_ip} | Тип: {active['device_type']} | "
                                    f"Пользователь: {active['username']} | Пароль: {active['password']}\n"
                                    f"{policy_info}\n"
                                    f"[ЗАДАЧА: сначала execute_routeros_command или get_switch_config (wireless), "
                                    f"затем apply_routeros_config с confirm=true. Не выполняй вслепую — смотри текущий конфиг.{wifi_params_hint}]\n"
                                )
                            else:
                                context_mem += (
                                    f"\n[АКТИВНЫЙ КОММУТАТОР — РАБОТАЙ ТОЛЬКО С НИМ]:\n"
                                    f"Имя: {dev_name} | IP: {dev_ip} | Тип: {active['device_type']} | "
                                    f"Пользователь: {active['username']} | Пароль: {active['password']}\n"
                                    f"{policy_info}\n"
                                    f"❌❌❌ КРИТИЧЕСКАЯ ОШИБКА: get_switch_config НЕ УДАЛОСЬ — {config_error}\n"
                                    f"ТВОЙ ЕДИНСТВЕННЫЙ ОТВЕТ: напиши пользователю 'Не удалось подключиться к коммутатору {dev_name} ({dev_ip}): {config_error}'\n"
                                    f"НЕ генерируй команды, НЕ предлагай действия, НЕ предлагай policy, НЕ генерируй 'ручную настройку'.\n"
                                    f"БОЛЬШЕ НИЧЕГО НЕ ОТВЕЧАЙ.\n"
                                )
                else:
                    context_mem += "\n[АКТИВНЫЙ КОММУТАТОР: не выбран. Если user просит подключиться — используй connect_switch с данными из сообщения.]\n"

                agent_tools = tools
                ssh_tool_names = {"connect_switch", "execute_switch_command", "get_switch_logs", "get_switch_config", "get_switch_logs_tool"}

                def _tool_name(t):
                    return t.get('name', '') if isinstance(t, dict) else getattr(t, 'name', '')

                pdf_only_kw = ("без подключения", "не подключайся", "не подключай", "только в базе", "только поиск", "pdf only", "search pdf", "в мануале", "в базе знаний", "поиск в базе", "найди в базе")
                if any(k in user_msg_lower for k in pdf_only_kw):
                    agent_tools = [t for t in agent_tools if _tool_name(t) not in ssh_tool_names]
                    context_mem += "\n[РЕЖИМ: только база знаний / PDF. НЕ подключайся к коммутатору, НЕ вызывай SSH-инструменты.]\n"
                elif config_fetched and not is_write:
                    # Конфиг уже получен авто-запросом для read/теории — убираем SSH-инструменты,
                    # чтобы reasoning-модель не зациклилась на повторных вызовах get_switch_config.
                    agent_tools = [t for t in agent_tools if _tool_name(t) not in ssh_tool_names]
                    context_mem += "\n[ВАЖНО: конфигурация УЖЕ приложена выше. НЕ вызывай SSH-инструменты повторно — просто анализируй данные и дай ответ текстом.]\n"

                history = get_chat_history(device_id or "default")
                messages = [SystemMessage(content=SYSTEM_PROMPT)]
                skip_history_prefixes = ("⚠️ Модель вернула пустой ответ", "⚠️ Произошла неизвестная ошибка", "❌ Ошибка:")
                for entry in history[-10:]:
                    role = entry.get("role", "")
                    content = (entry.get("content") or "").strip()
                    if not content:
                        continue
                    if role == "assistant" and content.startswith(skip_history_prefixes):
                        continue
                    if role == "user":
                        messages.append(HumanMessage(content=content))
                    elif role == "assistant":
                        messages.append(AIMessage(content=content))
                messages.append(HumanMessage(content=user_msg + context_mem))
                input_messages_count = len(messages)
                
                # Ограничиваем общее количество токенов во входных данных
                MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "8000"))
                total_input_tokens = sum(_count_tokens(getattr(m, "content", "") or "") for m in messages)
                if total_input_tokens > MAX_INPUT_TOKENS:
                    print(f"⚠️ Вход слишком большой ({total_input_tokens} токенов), обрезаю до {MAX_INPUT_TOKENS}...", flush=True)
                    budget = MAX_INPUT_TOKENS
                    for i in range(len(messages) - 1, -1, -1):
                        content = getattr(messages[i], "content", "") or ""
                        tokens = _count_tokens(content)
                        if tokens > budget:
                            new_content = _truncate_text(content, max(1, budget))
                            if isinstance(messages[i], HumanMessage):
                                messages[i] = HumanMessage(content=new_content)
                            elif isinstance(messages[i], SystemMessage):
                                messages[i] = SystemMessage(content=new_content)
                            elif isinstance(messages[i], AIMessage):
                                messages[i] = AIMessage(content=new_content)
                            budget = 0
                            break
                        else:
                            budget -= tokens
                    # Если всё ещё не влезло — обрезаем историю с начала
                    total_input_tokens = sum(_count_tokens(getattr(m, "content", "") or "") for m in messages)
                    while total_input_tokens > MAX_INPUT_TOKENS and len(messages) > 2:
                        removed = messages.pop(1)
                        total_input_tokens = sum(_count_tokens(getattr(m, "content", "") or "") for m in messages)
                    print(f"✂️ После обрезки: {total_input_tokens} токенов, {len(messages)} сообщений", flush=True)
                
                # Обрезаем конфиг если он слишком большой (чтобы не висеть на 70B модели)
                MAX_CONFIG_CHARS = 4000
                if config_fetched and len(config_data) > MAX_CONFIG_CHARS:
                    config_data = config_data[:MAX_CONFIG_CHARS] + f"\n\n... (обрезано, полный размер: {len(config_data)} символов)"
                    print(f"✂️ Конфиг обрезан до {MAX_CONFIG_CHARS} символов", flush=True)

                # Если конфиг уже получен авто-запросом — формат через прямой LLM вызов,
                # без агента (быстро, один запрос вместо 2-3)
                if config_fetched and is_show_config:
                    print("⚡ Прямой LLM вызов для форматирования конфига (без агента)...", flush=True)
                    dev_name = active.get('name', active.get('ip')) if active else "устройства"
                    format_prompt = (
                        f"Пользователь спросил: '{user_msg}'. "
                        f"Вот РЕАЛЬНАЯ конфигурация устройства {dev_name}, полученная по SSH:\n\n"
                        f"{config_data}\n\n"
                        f"ОТВЕТЬ ПО-РУССКИ. Кратко опиши конфигурацию: интерфейсы, IP, маршруты, VLAN. "
                        f"Используй таблицы для наглядности. Не выдумывай данные."
                    )
                    try:
                        direct_result = await _retry_call(
                            lambda: current_llm.ainvoke([
                                SystemMessage(content="Ты — старший сетевой инженер. Отвечай по-русски, используй таблицы где уместно."),
                                HumanMessage(content=format_prompt)
                            ]),
                            max_retries=3,
                            timeout=120.0,
                            is_write=is_write,
                            fallback=lambda: llm_write.ainvoke([
                                SystemMessage(content="Ты — старший сетевой инженер. Отвечай по-русски, используй таблицы где уместно."),
                                HumanMessage(content=format_prompt)
                            ])
                        )
                        direct_text = direct_result.content.strip() if isinstance(direct_result.content, str) else str(direct_result.content)
                        if direct_text and len(direct_text) > 20:
                            final_response = direct_text
                            print(f"✅ Прямой LLM ответ: {len(final_response)} символов", flush=True)
                        else:
                            final_response = f"📋 Конфигурация устройства {dev_name}:\n\n{config_data}"
                    except asyncio.TimeoutError:
                        print("⚠️ Прямой LLM таймаут, возвращаю сырые данные", flush=True)
                        final_response = f"📋 Конфигурация устройства {dev_name}:\n\n{config_data}"
                    raise _SkipAgent(final_response)

                if config_error:
                    if is_simple_read:
                        final_response = f"❌ Не удалось подключиться к коммутатору {active.get('name', active.get('ip'))}: {config_error}\n\nПопробуй позже или проверь доступность устройства по SSH."
                        raise _SkipAgent(final_response)

                # Для простых read-запросов с уже полученным конфигом — пропускаем агент
                # и сразу форматируем ответ через прямой LLM вызов (быстрее, без цикла инструментов)
                if config_fetched and is_simple_read:
                    dev_name = active.get('name', active.get('ip')) if active else "устройства"
                    # Авто-фetch данных подключений для запросов про connections/established
                    extra_data = ""
                    if any(k in user_msg_lower for k in ("established", "подключен", "connection", "conntrack", "активн", "текущ")):
                        print(f"🔧 Авто-запрос /ip firewall connection print для simple_read", flush=True)
                        conn_result = await _execute_routeros_commands(active, ["/ip firewall connection print"])
                        if conn_result and "bad command" not in conn_result.lower():
                            extra_data = f"\n\n=== ДАННЫЕ АКТИВНЫХ ПОДКЛЮЧЕНИЙ (/ip firewall connection print) ===\n{conn_result}\n=== КОНЕЦ ДАННЫХ ==="
                            print(f"✅ /ip firewall connection print вернул данные (len={len(conn_result)})", flush=True)
                    direct_prompt = (
                        f"Пользователь спрашивает про подключения (established). "
                        f"Вот конфигурация устройства {dev_name}, полученная по SSH:\n\n"
                        f"{config_data}{extra_data}\n\n"
                        f"Если данные подключений есть выше — дай краткий ответ на русском: сколько активных (established) подключений, "
                        f"к каким IP-адресам они подключены, по каким портам/протоколам. Оформи ответ таблицей если данных много. "
                        f"Если данных подключений нет — сообщи пользователю команду для их получения: /ip firewall connection print detail\n"
                        f"Не выдумывай данные, отвечай только на основе реальных данных выше."
                    )
                    try:
                        direct_result = await _retry_call(
                            lambda: current_llm.ainvoke([
                                SystemMessage(content="Ты — старший сетевой инженер. Отвечай по-русски, кратко и по делу.").content,
                                HumanMessage(content=direct_prompt)
                            ]),
                            max_retries=2,
                            timeout=60.0,
                            is_write=False,
                            fallback=lambda: llm_write.ainvoke([
                                SystemMessage(content="Ты — старший сетевой инженер. Отвечай по-русски, кратко.").content,
                                HumanMessage(content=direct_prompt)
                            ])
                        )
                        direct_text = direct_result.content.strip() if isinstance(direct_result.content, str) else str(direct_result.content)
                        if direct_text and len(direct_text) > 5:
                            final_response = direct_text
                        else:
                            final_response = f"📋 Конфигурация устройства {dev_name}:\n\n{config_data}"
                    except Exception as e:
                        print(f"⚠️ Прямой LLM вызов для simple_read не удался: {e}", flush=True)
                        final_response = f"📋 Конфигурация устройства {dev_name}:\n\n{config_data}"
                    raise _SkipAgent(final_response)

                # Создаём агента с актуальным набором инструментов для этого запроса
                if len(agent_tools) == len(tools) and current_llm is llm:
                    active_agent = agent
                else:
                    active_agent = create_agent(current_llm, agent_tools)
                    print(f"🔧 Агент пересоздан с {len(agent_tools)} инструментами (из {len(tools)})", flush=True)

                async with agent_lock:
                    try:
                        agent_timeout = 180.0 if is_write else 90.0
                        recursion_limit = MAX_AGENT_RECURSION_WRITE if is_write else MAX_AGENT_RECURSION_READ
                        agent_with_config = active_agent.with_config({"recursion_limit": recursion_limit})
                        fallback_recursion = MAX_AGENT_RECURSION_WRITE_FALLBACK if is_write else MAX_AGENT_RECURSION_READ_FALLBACK
                        if is_write:
                            agent_fallback = None
                        elif is_simple_read and config_fetched:
                            # For simple_read with config already fetched, NVIDIA fallback uses direct LLM (no agent loop)
                            agent_fallback = lambda: llm_write.ainvoke([
                                SystemMessage(content="Ты — старший сетевой инженер. Отвечай по-русски, кратко и по делу на основе данных ниже.").content,
                                HumanMessage(content=(
                                    f"Пользователь спрашивает про подключения (established). "
                                    f"Вот конфигурация устройства, полученная по SSH:\n\n"
                                    f"{config_data}\n\n"
                                    f"Дай краткий ответ на русском. Не выдумывай данные. "
                                    f"Если данные подключений отсутствуют — скажи об этом."
                                ))
                            ])
                        else:
                            # For read requests (non-simple_read), NVIDIA fallback also uses direct LLM since no write tools needed
                            system_msg = "Ты — старший сетевой инженер. Проанализируй предоставленные данные и дай краткий ответ по-русски."
                            human_msg = (
                                f"Пользователь задал вопрос: '{user_msg}'.\n\n"
                                f"Вот данные, полученные по SSH от устройства {active.get('name', active.get('ip')) if active else 'устройства'}:\n\n"
                                f"{config_data if config_fetched else 'Данные устройства не получены.'}\n\n"
                                f"Проанализируй и дай краткий ответ. Не выдумывай данные. "
                                f"Если данных недостаточно для ответа — скажи об этом."
                            )
                            agent_fallback = lambda: llm_write.ainvoke([
                                SystemMessage(content=system_msg),
                                HumanMessage(content=human_msg)
                            ])
                        result = await _retry_call(
                            lambda: agent_with_config.ainvoke({"messages": messages}),
                            max_retries=3,
                            timeout=agent_timeout,
                            is_write=is_write,
                            fallback=agent_fallback
                        )
                    except asyncio.TimeoutError:
                        timeout_str = "180с" if is_write else "90с"
                        print(f"⚠️ Агент не ответил за {timeout_str}, возвращаю сырые данные", flush=True)
                        if config_fetched and config_data:
                            final_response = f"📋 Конфигурация устройства {active.get('name', active.get('ip'))}:\n\n{config_data}"
                        else:
                            final_response = f"⚠️ Агент не ответил за {timeout_str}. Попробуй ещё раз."
                        raise _SkipAgent(final_response)
                    except Exception as e:
                        err_str = str(e)
                        if is_write and active and active.get("device_type", "").startswith("mikrotik") and _is_tool_call_error(e):
                            print(f"⚠️ Write-агент упал на tool-calling: {err_str[:200]}, переключаюсь на прямой LLM...", flush=True)
                            try:
                                response = await _retry_call(
                                    lambda: current_llm.ainvoke([HumanMessage(content=(
                                        "Сгенерируй краткий план настройки MikroTik WiFi без лишнего текста. "
                                        "Только RouterOS команды, по одной на строку, начиная с /. "
                                        "Не добавляй пояснения."
                                    ))]),
                                    max_retries=3,
                                    timeout=180.0,
                                    is_write=True
                                )
                                text = _extract_text(response)
                                commands = _extract_routeros_commands(text)
                                if commands:
                                    exec_result = await _execute_routeros_commands(active, commands)
                                    final_response = f"⚡ Выполнено через прямой LLM:\n\n{exec_result}"
                                else:
                                    final_response = f"⚠️ LLM не сгенерировал команды. Ответ:\n{text}"
                            except Exception as e2:
                                final_response = f"❌ Fallback LLM ошибка: {_format_api_error(e2)}"
                            raise _SkipAgent(final_response)
                        if isinstance(e, GraphRecursionError):
                            print(f"🔄 Агент (основной и fallback) зациклился, возвращаю конфиг напрямую", flush=True)
                            if config_fetched and config_data:
                                final_response = f"📋 Конфигурация устройства {active.get('name', active.get('ip'))}:\n\n{config_data}"
                            elif is_simple_read:
                                final_response = f"⚠️ Модель зациклилась на вызовах инструментов. Устройство {active.get('name', active.get('ip'))} недоступно или не отвечает корректно."
                            else:
                                final_response = f"⚠️ Агент зациклился на вызовах инструментов и не смог ответить."
                        else:
                            raise
                        raise _SkipAgent(final_response)

                # NVIDIA fallback вернул AIMessage напрямую — использовать его текст как финальный ответ
                if isinstance(result, AIMessage):
                    ai_text = _extract_text(result)
                    if not ai_text or len(ai_text) < 5:
                        ai_text = None
                    if ai_text:
                        final_response = ai_text.strip()
                        print(f"📤 Финальный ответ (NVIDIA fallback): {len(final_response)} символов", flush=True)
                        raise _SkipAgent(final_response)
                    # Если NVIDIA вернул пустой ответ — показать raw данные
                    final_response = f"📋 Конфигурация устройства {active.get('name', active.get('ip'))}:\n\n{config_data}" if config_fetched and config_data else "⚠️ Модель не смогла ответить."
                    raise _SkipAgent(final_response)

                result_messages = result.get("messages", [])
                new_messages = result_messages[input_messages_count:] or result_messages[-1:]
                print(f"🔍 Получено {len(result_messages)} сообщений (новых: {len(new_messages)})", flush=True)

                ai_text = None
                tool_error = None
                tool_text = ""
                for msg in reversed(new_messages):
                    msg_type = type(msg).__name__
                    txt = _extract_text(msg)
                    if msg_type == "ToolMessage":
                        if txt.startswith("❌") or txt.startswith("ОШИБКА"):
                            if not tool_error:
                                tool_error = txt
                        elif txt and not tool_text:
                            tool_text = txt
                    elif isinstance(msg, AIMessage):
                        if txt and not ai_text:
                            ai_text = txt

                if tool_error:
                    final_response = tool_error
                elif tool_text:
                    final_response = tool_text
                elif ai_text:
                    final_response = ai_text
                elif config_fetched and config_data:
                    final_response = f"📋 Конфигурация устройства {active.get('name', active.get('ip'))}:\n\n{config_data}"
                elif config_error:
                    final_response = f"❌ Не удалось получить данные с устройства: {config_error}"
                else:
                    final_response = "⚠️ Модель вернула пустой ответ."

                print(f"📤 Финальный ответ: {len(final_response)} символов", flush=True)
            except _SkipAgent as skip:
                final_response = str(skip)
                print(f"📤 Финальный ответ (skip): {len(final_response)} символов", flush=True)
            except Exception as e:
                print(f"❌ Ошибка агента: {e}", flush=True)
                final_response = f"❌ Ошибка: {str(e)}"

            # Отправляем финальный ответ пользователю
            response_payload = {"type": "thinking_end", "final": final_response, "request_id": request_id, "device_id": device_id}
            await safe_send(response_payload)
            
            # Clean up pending request
            if hasattr(websocket, 'pending_requests') and request_id in websocket.pending_requests:
                del websocket.pending_requests[request_id]
            
            # Сохраняем в per-device историю (кроме служебных фолбэков/ошибок)
            _skip_prefixes = ("⚠️ Модель вернула пустой ответ", "⚠️ Произошла неизвестная ошибка", "❌ Ошибка:")
            if not final_response.startswith(_skip_prefixes):
                append_chat_history(device_id or "default", "user", user_msg)
                append_chat_history(device_id or "default", "assistant", final_response)
                
        except Exception as e:
            print(f"❌ Ошибка обработки сообщения: {e}", flush=True)
        finally:
            queue_running = False
    
    try:
        while True:
            try:
                data = await websocket.receive_text()
                # Ставим сообщение в очередь
                await message_queue.put(data)
                # Запускаем обработку очереди если не идёт
                if not queue_running:
                    queue_running = True
                    await process_one_message()
            except Exception:
                print("WS receive closed, exiting loop", flush=True)
                break
    except Exception as e:
        print(f"WS err: {e}", flush=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.agent.main:app", host="0.0.0.0", port=8000, reload=True)