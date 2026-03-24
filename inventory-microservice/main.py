import logging
import time
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import FnDocument 
from ai_services import extract_invoice_data, extract_company_data, extract_product_from_html
from logic import insert_document_logic, upsert_company_from_invoice_logic, create_item_from_url_logic, create_inventory_movements_logic, process_single_movement_logic
from drive_services import download_with_validation, resolve_file_id
from scrape_services import scrape_product_page

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
_executor = ThreadPoolExecutor(max_workers=4)

class FilePayload(BaseModel):
    file_id: str | None = None
    database_id: str
    doc_id: str | None = None
    image_folder_id: str | None = None

class MovementPayload(BaseModel):
    document_id: str | None = None
    database_id: str | None = None
    DocumentID: str | None = None
    DatabaseID: str | None = None
    image_folder_id: str | None = None
    project_id: str | None = None

class UrlPayload(BaseModel):
    url: str
    database_id: str
    image_folder_id: str | None = None
    item_id: str | None = None

class SingleMovementPayload(BaseModel):
    movement_id: str
    database_id: str
    item_id: str
    origin_id: str | None = None
    project_id: str | None = None
    qty: float
    price: float | None = 0.0
    supply_id: str | None = None
    action: str
    created_by: str | None = "AI_BOT"

@app.get("/")
def read_root():
    return {"status": "System Online", "version": "5.1.0"}

def _check_duplicate(file_id: str, database_id: str):
    db = SessionLocal()
    try:
        return db.query(FnDocument).filter(
            FnDocument.doFile == file_id,
            FnDocument.DatabaseID == database_id,
            FnDocument.doConsecutive.isnot(None)
        ).first()
    finally:
        db.close()

@app.post("/webhook/process-drive-file")
async def process_drive_file(payload: FilePayload, db: Session = Depends(get_db)):
    logger.info(f"Received payload: {payload}")
    file_id = payload.file_id
    db_id = payload.database_id
    # Normalize database id
    database_id = (db_id or "")[:10]
    
    # Save the original path/id for later use in database record 'doFile'
    original_path = file_id
    
    # 1. Prioritize looking up by doc_id (DocumentID) in the database
    if payload.doc_id:
        db_doc = db.query(FnDocument).filter(
            FnDocument.DocumentID == payload.doc_id,
            FnDocument.DatabaseID == database_id
        ).first()
        if db_doc and db_doc.DriveID:
            logger.info(f"Resolved DriveID from database record using DocID: {payload.doc_id}")
            file_id = db_doc.DriveID
    
    # DriveID must be present at this point
    if not file_id:
        logger.error(f"Missing DriveID for DocID: {payload.doc_id} in Database: {database_id}")
        raise HTTPException(
            status_code=422, 
            detail=f"DriveID (file_id) is null. Verify the source of digitalization (fnDocuments vs utEmailsAtt) for DocID: {payload.doc_id}"
        )

    loop = asyncio.get_event_loop()
    # 2. If it's a path or URL, resolve it to an ID (if not already resolved by DB lookup)
    if '/' in file_id:
        file_id = await loop.run_in_executor(_executor, resolve_file_id, file_id)
        
    exists = await loop.run_in_executor(_executor, _check_duplicate, file_id, database_id)
    if exists:
        return {"status": "skipped", "reason": "Expediente ya digitalizado", "document_id": exists.DocumentID}
        
    content, meta = await loop.run_in_executor(_executor, download_with_validation, file_id)
    if not content:
        raise HTTPException(status_code=404, detail="Archivo no accesible")
        
    data = extract_invoice_data(content)
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")
    try:
        # Use database_id normalized, not the one from payload raw
        result = insert_document_logic(
            db, data, source_file_id=original_path, appsheet_doc_id=payload.doc_id, 
            database_id=database_id, drive_id=file_id
        )
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/create-movements")
async def create_movements(payload: MovementPayload, db: Session = Depends(get_db)):
    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        doc_id = payload.document_id or payload.DocumentID
        db_id = payload.database_id or payload.DatabaseID
        
        if not doc_id or not db_id:
            raise HTTPException(status_code=422, detail="Missing DocumentID or DatabaseID in payload")
            
        result = create_inventory_movements_logic(
            db, doc_id, db_id, 
            image_folder_id=img_folder, project_id=payload.project_id
        )
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/extract-company")
async def extract_company(payload: FilePayload, db: Session = Depends(get_db)):
    file_id = payload.file_id
    loop = asyncio.get_event_loop()
    content, meta = await loop.run_in_executor(_executor, download_with_validation, file_id)
    if not content: raise HTTPException(status_code=404)
    data = extract_company_data(content)
    if not data: raise HTTPException(status_code=422)
    result = upsert_company_from_invoice_logic(db, data, file_id, payload.database_id, payload.doc_id)
    return {"status": "success", "data": result}

@app.post("/webhook/extract-from-url")
async def extract_from_url(payload: UrlPayload, db: Session = Depends(get_db)):
    loop = asyncio.get_event_loop()
    html, image_url = await loop.run_in_executor(_executor, scrape_product_page, payload.url)
    if not html:
        raise HTTPException(status_code=422, detail="No se pudo acceder a la URL")
    data = extract_product_from_html(html)
    if not data:
        raise HTTPException(status_code=422, detail="Fallo extracción IA")
    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        result = create_item_from_url_logic(
            db, data, image_url=image_url, database_id=payload.database_id,
            image_folder_id=img_folder, item_id=payload.item_id
        )
        return {"status": "success", "source_url": payload.url, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/process-movement")
async def process_movement_endpoint(payload: SingleMovementPayload, db: Session = Depends(get_db)):
    try:
        # Ejecutamos la lógica en el mismo hilo de la petición HTTP para bloquear a AppSheet
        result = process_single_movement_logic(db, payload.model_dump())
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error procesando movimiento: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), reload=True)
