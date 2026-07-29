import os
import sys
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

# Путь к базе данных
DB_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "chroma_db"))
os.makedirs(DB_PATH, exist_ok=True)

def index_file(file_path):
    if not os.path.exists(file_path):
        print(f"❌ Файл не найден: {file_path}")
        return

    print(f"⏳ Загрузка PDF: {os.path.basename(file_path)}...")
    loader = PyMuPDFLoader(file_path)
    docs = loader.load()
    
    print("⏳ Разбиение на фрагменты...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = text_splitter.split_documents(docs)
    
    # Гарантируем, что у каждого фрагмента правильное имя файла в метаданных
    file_name = os.path.basename(file_path)
    for chunk in chunks:
        chunk.metadata["source"] = file_name
        
    print(f"✅ Разбито на {len(chunks)} фрагментов.")
    
    print("⏳ Запуск эмбеддинга (это займет 1-2 минуты, жди)...")
    # Игнорируем ворнинг HF Hub, он не критичен для локальной работы
    embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    
    print("⏳ Сохранение в локальную базу ChromaDB...")
    Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=DB_PATH,
        collection_name="network_docs"
        # Аргумент metadata отсюда убран, так как он уже есть в chunks
    )
    
    print(f"\n🎉 УСПЕХ! Файл '{file_name}' проиндексирован.")
    print("Теперь ты можешь спросить агента в чате о содержимом этого файла.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python index_pdf.py <путь_к_файлу.pdf>")
    else:
        index_file(sys.argv[1])