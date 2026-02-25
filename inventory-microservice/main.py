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

class DigitizePayload(BaseModel):
    file_id: str
    database_id: str

class ProductUpsertPayload(BaseModel):
    file_id: str
    database_id: str
    extraction_data: dict
    appsheet_doc_id: str = None
    image_folder_id: str = None

class MovementPayload(BaseModel):
    document_id: str
    database_id: str

@app.get("/")
def read_root():
    return {"status": "System Online", "version": "4.0.0 (Multi-Step Flow)"}

def _check_duplicate(file_id: str, database_id: str):
    """Verifica duplicados filtrando por archivo y base de datos."""
    db = SessionLocal()
    try:
        result = db.query(FnDocument).filter(
            FnDocument.doFile == file_id,
            FnDocument.DatabaseID == database_id
        ).first()
        return result
    finally:
        db.close()

@app.post("/webhook/digitize")
async def digitize_invoice(payload: DigitizePayload):
    """Paso 1: Solo digitalización vía IA."""
    file_id = payload.file_id
    db_id = payload.database_id
    logger.info(f"Digitalizando archivo: {file_id} (Client: {db_id})")

    loop = asyncio.get_event_loop()
    content, meta = await loop.run_in_executor(_executor, download_with_validation, file_id)
    
    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible en Drive")

    data = extract_invoice_data(content)
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")

    return {"status": "success", "extraction": data}

@app.post("/webhook/upsert-products")
async def upsert_products(payload: ProductUpsertPayload, db: Session = Depends(get_db)):
    """Paso 2: Inserción de productos y documento (Manual o Automático)."""
    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        
        result = insert_document_logic(
            db, 
            payload.extraction_data, 
            source_file_id=payload.file_id, 
            appsheet_doc_id=payload.appsheet_doc_id, 
            database_id=payload.database_id,
            image_folder_id=img_folder,
            create_movements=False # Nuevo flag para separar pasos
        )
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en Upsert: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/create-movements")
async def create_movements(payload: MovementPayload, db: Session = Depends(get_db)):
    """Paso 3: Generación de movimientos de inventario."""
    try:
        from logic import create_inventory_movements_logic
        result = create_inventory_movements_logic(db, payload.document_id, payload.database_id)
        return {"status": "success", "result": result}
    except Exception as e:
        logger.error(f"Error en Movimientos: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# El endpoint original se mantiene para compatibilidad si es necesario, 
# pero el flujo recomendado es el de 3 pasos.
@app.post("/webhook/process-drive-file")
async def process_drive_file(payload: FilePayload, db: Session = Depends(get_db)):
    file_id = payload.file_id
    db_id = payload.database_id
    logger.info(f"Procesando archivo ID: {file_id} (Client: {db_id}) - Legacy Flow")

    loop = asyncio.get_event_loop()
    dup_future = loop.run_in_executor(_executor, _check_duplicate, file_id, db_id)
    drive_future = loop.run_in_executor(_executor, download_with_validation, file_id)
    
    exists, (content, meta) = await asyncio.gather(dup_future, drive_future)
    
    if exists:
        return {"status": "skipped", "reason": "Already processed", "document_id": exists.DocumentID}

    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible")

    data = extract_invoice_data(content)
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")

    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        result = insert_document_logic(
            db, data, source_file_id=file_id, appsheet_doc_id=payload.doc_id, 
            database_id=payload.database_id, image_folder_id=img_folder, create_movements=True
        )
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/extract-company")
async def extract_company(payload: FilePayload, db: Session = Depends(get_db)):
    # ... (Misma lógica de antes)
    file_id = payload.file_id
    loop = asyncio.get_event_loop()
    content, meta = await loop.run_in_executor(_executor, download_with_validation, file_id)
    if not content: raise HTTPException(status_code=404)
    data = extract_company_data(content)
    if not data: raise HTTPException(status_code=422)
    result = upsert_company_from_invoice_logic(db, data, file_id, payload.database_id, payload.company_id)
    return {"status": "success", "data": result}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), reload=True)
