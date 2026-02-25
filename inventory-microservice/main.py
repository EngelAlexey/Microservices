import logging
import time
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, engine, Base, SessionLocal
from models import FnDocument 
from ai_services import extract_invoice_data, extract_company_data
from logic import insert_document_logic, upsert_company_from_invoice_logic
from drive_services import download_with_validation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

_executor = ThreadPoolExecutor(max_workers=4)

class FilePayload(BaseModel):
    file_id: str
    file_name: str = ""
    doc_id: str = None
    database_id: str
    company_id: str = None
    image_folder_id: str = None

@app.get("/")
def read_root():
    return {"status": "System Online", "version": "3.1.1 (Production Ready)"}

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
    logger.info(f"Procesando archivo ID: {file_id}")

    t0 = time.time()
    loop = asyncio.get_event_loop()
    
    dup_future = loop.run_in_executor(_executor, _check_duplicate, file_id)
    drive_future = loop.run_in_executor(_executor, download_with_validation, file_id)
    
    exists, (content, meta) = await asyncio.gather(dup_future, drive_future)
    
    if exists:
        return {
            "status": "skipped", 
            "reason": "Already processed", 
            "document_id": exists.DocumentID
        }

    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible o no existe en Drive")

    t_ai = time.time()
    data = extract_invoice_data(content)
    
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")

    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        
        t_db = time.time()
        result = insert_document_logic(
            db, 
            data, 
            source_file_id=file_id, 
            appsheet_doc_id=payload.doc_id, 
            database_id=payload.database_id,
            image_folder_id=img_folder
        )
        
        total_time = time.time() - request_start
        logger.info(f"Procesado exitoso: {file_id} en {total_time:.2f}s")
        
        result["processing_time_seconds"] = round(total_time, 2)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error DB: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/extract-company")
async def extract_company(payload: FilePayload, db: Session = Depends(get_db)):
    file_id = payload.file_id
    request_start = time.time()
    logger.info(f"Extrayendo empresa para archivo: {file_id}")

    t0 = time.time()
    loop = asyncio.get_event_loop()
    
    drive_future = loop.run_in_executor(_executor, download_with_validation, file_id)
    content, meta = await drive_future
    
    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible o no existe en Drive")

    data = extract_company_data(content)
    
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA para empresa")

    try:
        result = upsert_company_from_invoice_logic(
            db, 
            data, 
            source_file_id=file_id, 
            database_id=payload.database_id,
            target_company_id=payload.company_id
        )
        
        total_time = time.time() - request_start
        logger.info(f"Extracción empresa exitosa: {file_id} en {total_time:.2f}s")
        
        result["processing_time_seconds"] = round(total_time, 2)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error DB: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
