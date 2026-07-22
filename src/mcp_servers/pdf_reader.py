import asyncio
import logging
import os
import tempfile
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field
import fitz  # PyMuPDF
import pytesseract
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("pdf-reader-server")

class PDFExtractArgs(BaseModel):
    file_path: str = Field(description="Путь к PDF файлу")
    extract_images: bool = Field(default=True, description="Извлекать ли картинки и делать OCR")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="extract_pdf_content",
            description="Извлекает текст и картинки из PDF файла. Делает OCR картинок для чтения схем и скриншотов.",
            inputSchema=PDFExtractArgs.model_json_schema(),
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "extract_pdf_content":
        raise ValueError(f"Unknown tool: {name}")

    args = PDFExtractArgs(**arguments)
    
    if not os.path.exists(args.file_path):
        return [TextContent(type="text", text=f"❌ Файл не найден: {args.file_path}")]
    
    try:
        logger.info(f"Открытие PDF: {args.file_path}")
        doc = fitz.open(args.file_path)
        
        full_text = []
        image_count = 0
        ocr_texts = []
        
        # Создаем временную папку для картинок
        temp_dir = tempfile.mkdtemp(prefix="pdf_images_")
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # 1. Извлекаем текст страницы
            page_text = page.get_text()
            if page_text.strip():
                full_text.append(f"\n--- Страница {page_num + 1} ---\n{page_text}")
            
            # 2. Извлекаем картинки и делаем OCR
            if args.extract_images:
                image_list = page.get_images(full=True)
                for img_index, img in enumerate(image_list):
                    xref = img[0]
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    
                    # Сохраняем картинку
                    image_filename = f"page{page_num + 1}_img{img_index + 1}.png"
                    image_path = os.path.join(temp_dir, image_filename)
                    with open(image_path, "wb") as img_file:
                        img_file.write(image_bytes)
                    
                    image_count += 1
                    
                    # Делаем OCR картинки
                    try:
                        pil_image = Image.open(image_path)
                        ocr_text = pytesseract.image_to_string(pil_image, lang='eng+rus')
                        if ocr_text.strip():
                            ocr_texts.append(f"\n[OCR из картинки {image_filename}]:\n{ocr_text}")
                    except Exception as e:
                        logger.warning(f"OCR ошибка для {image_filename}: {e}")
        
        doc.close()
        
        # Формируем итоговый ответ
        result = []
        result.append(f"✅ Извлечено из PDF: {len(doc)} страниц, {image_count} картинок\n")
        result.append("📄 ТЕКСТ ИЗ PDF:")
        result.append("\n".join(full_text))
        
        if ocr_texts:
            result.append("\n\n🖼️ ТЕКСТ ИЗ КАРТИНОК (OCR):")
            result.append("\n".join(ocr_texts))
        
        result.append(f"\n\n📁 Картинки сохранены в: {temp_dir}")
        
        return [TextContent(type="text", text="\n".join(result))]
        
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Ошибка обработки PDF: {str(e)}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())