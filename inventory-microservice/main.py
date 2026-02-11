from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, engine, Base, SessionLocal
from models import FnDocument 
from ai_services import extract_invoice_data
from logic import insert_document_logic 
from drive_services import download_with_validation
import logging
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

_executor = ThreadPoolExecutor(max_workers=4)

class FilePayload(BaseModel):
    file_id: str
    file_name: str = ""

@app.get("/")
def read_root():
    return {"status": "System Online", "version": "3.1.0 (Performance Optimized v2)"}

def _check_duplicate(file_id: str):
    """Verifica duplicados en un hilo separado con su propia sesión DB."""
    db = SessionLocal()
    try:
        result = db.query(FnDocument).filter(FnDocument.doFile == file_id).first()
        return result
    finally:
        db.close()

@app.post("/webhook/process-drive-file")
async def process_drive_file(payload: FilePayload, db: Session = Depends(get_db)):
    file_id = payload.file_id
    request_start = time.time()
    logger.info(f"⏱️ Procesando archivo ID: {file_id}")

    t0 = time.time()
    loop = asyncio.get_event_loop()
    
    dup_future = loop.run_in_executor(_executor, _check_duplicate, file_id)
    drive_future = loop.run_in_executor(_executor, download_with_validation, file_id)
    
    exists, (content, meta) = await asyncio.gather(dup_future, drive_future)
    logger.info(f"⏱️ Paso 1+2 - Duplicados + Drive (paralelo): {time.time() - t0:.2f}s")
    
    if exists:
        return {
            "status": "skipped", 
            "reason": "Already processed", 
            "document_id": exists.DocumentID
        }

    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible o no existe en Drive")

    t2 = time.time()
    data = extract_invoice_data(content)
    logger.info(f"⏱️ Paso 3 - Gemini AI: {time.time() - t2:.2f}s")
    
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")

    t3 = time.time()
    try:
        result = insert_document_logic(db, data, source_file_id=file_id)
        logger.info(f"⏱️ Paso 4 - DB Insert: {time.time() - t3:.2f}s")
        
        total_time = time.time() - request_start
        logger.info(f"✅ TOTAL: {total_time:.2f}s para archivo {file_id}")
        
        result["processing_time_seconds"] = round(total_time, 2)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error DB: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)