import re
import logging
import time
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
import aiohttp
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import FnDocument 
from ai_services import extract_invoice_data, extract_company_data, extract_product_from_html, extract_product_from_barcode
from logic import insert_document_logic, upsert_company_from_invoice_logic, create_item_from_url_logic, create_inventory_movements_logic, process_single_movement_logic, backfill_movement_costs_logic, sync_rfq_lines_logic, backfill_categories_logic, backfill_units_logic, backfill_sizes_logic, backfill_clean_titles_logic
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

class BarcodePayload(BaseModel):
    barcode: str
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
    unit_tax: float | None = 0.0
    total_cost: float | None = 0.0
    supply_id: str | None = None
    action: str
    created_by: str | None = "AI_BOT"

class BackfillPayload(BaseModel):
    database_id: str
    limit: int | None = None

class BackfillCategoriesPayload(BaseModel):
    database_id: str
    dry_run: bool | None = False

class BackfillUnitsPayload(BaseModel):
    database_id: str
    dry_run: bool | None = False
    limit: int | None = None

class BackfillSizesPayload(BaseModel):
    database_id: str
    dry_run: bool | None = False
    limit: int | None = None

class CleanTitlesPayload(BaseModel):
    database_id: str
    dry_run: bool | None = False
    limit: int | None = None

class SyncRFQLinesPayload(BaseModel):
    rfq_id: str
    database_id: str
    selected_ids: str

class MessageContent(BaseModel):
    content: str
    mediaUrl: str

class BroadcastInput(BaseModel):
    messageData: MessageContent
    targetNumbers: str

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

@app.post("/webhook/extract-from-barcode")
async def extract_from_barcode(payload: BarcodePayload, db: Session = Depends(get_db)):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, extract_product_from_barcode, payload.barcode)
    if not data or not str(data.get("itTitle") or "").strip():
        raise HTTPException(status_code=422, detail="No se encontró información para este código de barras")
    try:
        img_folder = payload.image_folder_id or os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
        image_url = data.get("image_url")
        result = create_item_from_url_logic(
            db, data, image_url=image_url, database_id=payload.database_id,
            image_folder_id=img_folder, item_id=payload.item_id, barcode=payload.barcode
        )
        return {"status": "success", "source_barcode": payload.barcode, "data": result}
    except Exception as e:
        db.rollback()
        logger.exception(f"Error en extract-from-barcode (barcode={payload.barcode}, item_id={payload.item_id})")
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

@app.post("/admin/backfill-costs")
async def backfill_costs(payload: BackfillPayload, db: Session = Depends(get_db)):
    """
    Corrige retroactivamente mvUnitCost, mvTax, mvTotalCost en icMovements
    y prPrice, prTax, prTotal en icItemsPrices para todos los registros de la base de datos indicada.
    Usar una sola vez por database_id para sanear datos históricos.
    """
    try:
        result = backfill_movement_costs_logic(db, payload.database_id, payload.limit)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en backfill de costos: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/backfill-categories")
async def backfill_categories(payload: BackfillCategoriesPayload, db: Session = Depends(get_db)):
    """
    Herramienta de un solo uso: convierte itCategory de texto libre histórico a CategoryID
    (referencia a utCategories) para la base de datos indicada. NO es parte de la rutina.
    Usar dry_run=true para previsualizar.
    """
    try:
        result = backfill_categories_logic(db, payload.database_id, dry_run=bool(payload.dry_run))
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en backfill de categorías: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/backfill-units")
async def backfill_units(payload: BackfillUnitsPayload, db: Session = Depends(get_db)):
    """
    Herramienta de un solo uso: rellena UnitID vacío en bcItems (y sus variantes) infiriendo
    la unidad con IA desde itTitle/itModel. NO es parte de la rutina. Usar dry_run=true para
    previsualizar y limit para acotar.
    """
    try:
        result = backfill_units_logic(db, payload.database_id, dry_run=bool(payload.dry_run), limit=payload.limit)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en backfill de unidades: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/backfill-sizes")
async def backfill_sizes(payload: BackfillSizesPayload, db: Session = Depends(get_db)):
    """
    Herramienta de un solo uso: rellena itSize (Dimensión) vacío en bcItems extrayendo las medidas
    físicas con IA desde itTitle/itModel. Los productos sin medidas se saltan. NO es parte de la rutina.
    Usar dry_run=true para previsualizar y limit para acotar.
    """
    try:
        result = backfill_sizes_logic(db, payload.database_id, dry_run=bool(payload.dry_run), limit=payload.limit)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en backfill de dimensiones: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/clean-titles")
async def clean_titles(payload: CleanTitlesPayload, db: Session = Depends(get_db)):
    """
    Herramienta de un solo uso: limpia itTitle dejando el nombre genérico del padre (quita dimensión,
    marca, presentación y color de variante con IA). NO fusiona duplicados. NO es parte de la rutina.
    Usar dry_run=true para previsualizar.
    """
    try:
        result = backfill_clean_titles_logic(db, payload.database_id, dry_run=bool(payload.dry_run), limit=payload.limit)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en limpieza de títulos: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/sync-rfq-lines")
async def sync_rfq_lines(payload: SyncRFQLinesPayload, db: Session = Depends(get_db)):
    try:
        result = sync_rfq_lines_logic(db, payload.model_dump())
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error en sync-rfq-lines: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def execute_broadcast(payload: BroadcastInput):
    numbers_raw = payload.targetNumbers.split(",")
    numbers = [re.sub(r'\D', '', num) for num in numbers_raw if num.strip()]
    
    url = os.environ.get("BUILDERBOT_URL", os.environ.get("BUILDERBOT_URL"))
    api_key = os.environ.get("BUILDERBOT_API_KEY", os.environ.get("BUILDERBOT_API_KEY"))
    
    headers = {
        "Content-Type": "application/json",
        "x-api-builderbot": api_key
    }

    async with aiohttp.ClientSession() as session:
        for number in numbers:
            if not number:
                continue
                
            body = {
                "messages": {
                    "content": payload.messageData.content,
                    "mediaUrl": payload.messageData.mediaUrl
                },
                "number": number,
                "checkIfExists": False
            }
            try:
                async with session.post(url, headers=headers, json=body) as response:
                    resp_text = await response.text()
                    if response.status >= 400:
                        logging.error(f"BuilderBot Error {response.status}: {resp_text}")
            except Exception as e:
                logging.error(f"HTTP Request Error: {str(e)}")
            await asyncio.sleep(1.5)

@app.post("/webhook/broadcast")
async def trigger_broadcast(payload: BroadcastInput, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_broadcast, payload)
    return {"status": "queued"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), reload=True)
