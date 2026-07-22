import uuid
import os
import json
import asyncio
import shutil
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
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.tools import tool


from src.agent.prompts import SYSTEM_PROMPT
from src.utils.memory import load_memory, save_memory, set_active_switch, get_active_switch, get_chat_history, append_chat_history, get_switch_config as _get_switch_config, get_switch_logs as _get_switch_logs, connect_to_switch
from src.utils.telegram import is_telegram_enabled

load_dotenv()

async def safe_ws_send(websocket: WebSocket, payload: dict):
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(payload)
    except Exception as e:
        print(f"❌ Ошибка отправки WS: {e}", flush=True)

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
    global agent, tools, mcp_client, llm, rag_db
    
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
    mcp_tools = await mcp_client.get_tools()
    
    tools = mcp_tools + [search_pdf_knowledge_base, get_switch_config, get_switch_logs_tool]
    print(f"✅ Загружено инструментов: {len(tools)}", flush=True)
    
    llm = ChatOpenAI(
        model=os.getenv("NVIDIA_MODEL_NAME", "nvidia/llama-3.3-nemotron-super-49b-v1"),
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.getenv("NVIDIA_API_KEY"),
        temperature=0.2,
        max_tokens=2000,
        timeout=300,
        max_retries=2
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
            print("🔄 Использую агента...", flush=True)
            try:
                memory = load_memory()
                context_mem = ""
                active = None
                if device_id:
                    devices = memory.get("devices", [])
                    for dev in devices:
                        if str(dev.get("id", "")) == str(device_id) or str(dev.get("ip", "")) == str(device_id):
                            active = dev
                            break
                if not active:
                    active = memory.get("active_switch")
                
                # Автоматически вызываем get_switch_config для запросов про конфигурацию
                config_fetched = False
                config_data = ""
                config_error = ""
                
                config_kw = ("конфигурац", "конфигура", "настройки", "текущие параметры", "настройка", "правила firewall", "firewall", "nat", "маршрут", "route", "interface", "интерфейс", "провер.*правильн", "анализ.*конфиг", "проверить.*конфиг", "проверить.*правила", "предложить.*улучш", "настроить vlan", "настрой vlan", "как настроить", "настрой.*vlan", "vlan", "bridge", "mstp", "stp", "port", "порт", "ip address", "ip.*адрес", "dhcp", "dns", "proxy", "ntp", "user", "роут", "маршрут", "qos", "traffic", "qos", "как настроить", "что настроить", "проверь настрой", "анализ.*настрой", "как.*настроить")
                is_config_request = any(k in user_msg_lower for k in config_kw)
                
                # Также проверяем — если пользователь НЕ просит "без подключения" / "только в базе"
                no_connect_kw = ("без подключения", "не подключайся", "не подключай", "только в базе", "только поиск", "pdf only", "search pdf", "в мануале", "в базе знаний", "поиск в базе", "найди в базе")
                is_pdf_only = any(k in user_msg_lower for k in no_connect_kw)
                
                # Вызываем get_switch_config если это не PDF-режим
                if active and is_config_request and not is_pdf_only:
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
                
                if active:
                    policy = memory.get("command_policies", {}).get("mikrotik", {})
                    policy_info = ""
                    if active.get("device_type", "").startswith("mikrotik"):
                        readonly = policy.get("readonly_prefixes", [])
                        blocked = policy.get("blocked_patterns", [])
                        policy_info = f"\n[ПОЛИТИКА КОМАНД ДЛЯ MikroTik]:\n  Разрешённые: {', '.join(readonly[:8])}...\n  Заблокировано: {', '.join(blocked[:8])}...\n"
                    
                    if config_fetched:
                        context_mem += (
                            f"\n[АКТИВНЫЙ КОММУТАТОР — РАБОТАЙ ТОЛЬКО С НИМ]:\n"
                            f"Имя: {active.get('name', active.get('ip'))} | IP: {active['ip']} | Тип: {active['device_type']} | "
                            f"Пользователь: {active['username']} | Пароль: {active['password']}\n"
                            f"{policy_info}\n"
                            f"=== РЕАЛЬНАЯ КОНФИГУРАЦИЯ (ПОЛУЧЕНА ПО SSH) ===\n{config_data}\n=== КОНЕЦ КОНФИГУРАЦИИ ===\n"
                            f"[ЗАДАЧА: АНАЛИЗИРУЙ ТОЛЬКО РЕАЛЬНУЮ КОНФИГУРАЦИЮ ВЫШЕ. НЕ СИМУЛИРУЙ, НЕ ПРИДУМЫВАЙ. ПРЕДЛАГАЙ КОНКРЕТНЫЕ УЛУЧШЕНИЯ НА ОСНОВЕ ДАННЫХ.]\n"
                        )
                    else:
                        dev_name = active.get('name', active.get('ip'))
                        dev_ip = active['ip']
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

                pdf_only_kw = ("без подключения", "не подключайся", "не подключай", "только в базе", "только поиск", "pdf only", "search pdf", "в мануале", "в базе знаний", "поиск в базе", "найди в базе")
                if any(k in user_msg_lower for k in pdf_only_kw):
                    ssh_tool_names = {"connect_switch", "execute_switch_command", "get_switch_logs", "get_switch_config", "get_switch_logs_tool"}
                    agent_tools = [t for t in agent_tools if (getattr(t, 'name', '') if not isinstance(t, dict) else t.get('name', '')) not in ssh_tool_names]
                    context_mem += "\n[РЕЖИМ: только база знаний / PDF. НЕ подключайся к коммутатору, НЕ вызывай SSH-инструменты.]\n"

                history = get_chat_history(device_id or "default")
                messages = [SystemMessage(content=SYSTEM_PROMPT)]
                for entry in history[-10:]:
                    if entry["role"] == "user":
                        messages.append(HumanMessage(content=entry["content"]))
                    elif entry["role"] == "assistant":
                        messages.append(AIMessage(content=entry["content"]))
                messages.append(HumanMessage(content=user_msg + context_mem))
                
                payload = {
                    "messages": messages,
                    "tools": [t if isinstance(t, dict) else _tool_to_dict(t) for t in agent_tools]
                }
                async with agent_lock:
                    try:
                        result = await asyncio.wait_for(agent.ainvoke(payload), timeout=240.0)
                    except asyncio.TimeoutError:
                        print("⚠️ Агент не ответил за 240с, повтор...", flush=True)
                        result = await agent.ainvoke(payload)

                def _msg_to_text(content):
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = []
                        for p in content:
                            if isinstance(p, dict):
                                if p.get("type") == "text" and p.get("text"):
                                    parts.append(p["text"])
                                elif "text" in p and p["text"]:
                                    parts.append(p["text"])
                            elif isinstance(p, str):
                                parts.append(p)
                        return "\n".join(parts)
                    return str(content)

                messages = result.get("messages", [])
                ai_text = None
                tool_error = None
                tool_text = ""
                for msg in reversed(messages):
                    msg_type = type(msg).__name__
                    if msg_type == "ToolMessage" and getattr(msg, "content", None):
                        txt = _msg_to_text(msg.content).strip()
                        if txt.startswith("❌") or txt.startswith("ОШИБКА"):
                            tool_error = txt
                            break
                        elif not tool_error:
                            tool_text = txt
                    elif isinstance(msg, AIMessage) and msg.content and not tool_error:
                        ai_text = _msg_to_text(msg.content).strip()
                        if ai_text:
                            break
                if tool_error:
                    final_response = tool_error
                elif ai_text:
                    final_response = ai_text
                else:
                    final_response = tool_text or "⚠️ Модель вернула пустой ответ."
            except Exception as e:
                print(f"❌ Ошибка агента: {e}", flush=True)
                final_response = f"❌ Ошибка: {str(e)}"

            # Отправляем финальный ответ пользователю
            response_payload = {"type": "thinking_end", "final": final_response, "request_id": request_id, "device_id": device_id}
            await safe_send(response_payload)
            
            # Clean up pending request
            if hasattr(websocket, 'pending_requests') and request_id in websocket.pending_requests:
                del websocket.pending_requests[request_id]
            
            # Сохраняем в per-device историю
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