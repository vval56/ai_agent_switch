import asyncio
import logging
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Server("rag-search-server")

# Папка для хранения "памяти" агента (векторная база)
DB_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../chroma_db"))
os.makedirs(DB_PATH, exist_ok=True)

def get_embeddings():
    # Легкая и быстрая модель для эмбеддингов, отлично работает на Mac M3
    return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

class IndexPDFArgs(BaseModel):
    file_path: str = Field(description="Полный путь к PDF файлу для индексации в базу знаний")

class SearchPDFArgs(BaseModel):
    query: str = Field(description="Конкретный поисковый запрос (например, 'настройка VLAN 10' или 'индикация портов')")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="index_pdf_document",
            description="Разбивает большой PDF на фрагменты и сохраняет в локальную базу знаний. ИСПОЛЬЗОВАТЬ ПЕРВЫМ при добавлении нового мануала.",
            inputSchema=IndexPDFArgs.model_json_schema(),
        ),
        Tool(
            name="search_pdf_knowledge_base",
            description="Мгновенно ищет ответ на конкретный вопрос в ранее проиндексированных PDF документах. Возвращает только релевантные фрагменты с номерами страниц.",
            inputSchema=SearchPDFArgs.model_json_schema(),
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "index_pdf_document":
        args = IndexPDFArgs(**arguments)
        if not os.path.exists(args.file_path):
            return [TextContent(type="text", text=f"❌ Файл не найден: {args.file_path}")]
        try:
            logger.info(f"Начинаю индексацию {args.file_path}...")
            loader = PyMuPDFLoader(args.file_path)
            docs = loader.load()
            
            # Разбиваем на chunks по 1000 символов с перекрытием
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
            chunks = text_splitter.split_documents(docs)
            
            embeddings = get_embeddings()
            collection_name = "network_docs" # Единая база для всех мануалов
            
            # 🛠️ ИСПРАВЛЕНИЕ: Выносим тяжелую операцию в отдельный поток, чтобы не блокировать asyncio и не ловить таймаут
            def _build_db():
                return Chroma.from_documents(
                    documents=chunks,
                    embedding=embeddings,
                    persist_directory=DB_PATH,
                    collection_name=collection_name,
                    metadata={"source": os.path.basename(args.file_path)}
                )
            
            # Запускаем синхронную функцию создания БД в асинхронном режиме
            await asyncio.to_thread(_build_db)
            
            return [TextContent(type="text", text=f"✅ Успешно проиндексировано {len(chunks)} фрагментов из '{os.path.basename(args.file_path)}'. База знаний обновлена. Теперь задавай вопросы по этому документу!")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ Ошибка индексации: {str(e)}")]

    elif name == "search_pdf_knowledge_base":
        args = SearchPDFArgs(**arguments)
        try:
            embeddings = get_embeddings()
            db = Chroma(
                persist_directory=DB_PATH, 
                embedding_function=embeddings, 
                collection_name="network_docs"
            )
            # Ищем 3 самых релевантных фрагмента
            results = db.similarity_search(args.query, k=3)
            
            if not results:
                return [TextContent(type="text", text="❌ В базе знаний ничего не найдено по этому запросу. Убедись, что файл был проиндексирован.")]
                
            context = f"🔍 Найденная информация по запросу '{args.query}':\n\n"
            for i, doc in enumerate(results):
                page = doc.metadata.get("page", "?")
                source = doc.metadata.get("source", "Неизвестный файл")
                context += f"📄 [Файл: {source}, Страница: {page}]\n{doc.page_content}\n{'-'*40}\n"
            
            return [TextContent(type="text", text=context)]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ Ошибка поиска: {str(e)}")]
            
    return [TextContent(type="text", text=f"❌ Неизвестный инструмент: {name}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())