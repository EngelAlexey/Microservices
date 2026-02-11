from sqlalchemy.orm import Session
from models import BcItem, FnDocument, FnDocumentLn
from thefuzz import process, fuzz
import uuid
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def _load_product_catalog(db: Session):
    all_items = db.query(BcItem.ItemID, BcItem.itCode, BcItem.itTitle).all()
    sku_map = {}
    choices_map = {}
    for item in all_items:
        if item.itCode:
            sku_map[item.itCode.strip().upper()] = item.ItemID
        if item.itTitle:
            choices_map[item.itTitle] = item.ItemID
    return sku_map, choices_map

def find_product_id(sku: str, description: str, sku_map: dict, choices_map: dict):
    if sku:
        clean_sku = sku.strip().upper()
        if clean_sku in sku_map:
            return sku_map[clean_sku], "Exact SKU"
    
    if choices_map and description:
        best = process.extractOne(description, choices_map.keys(), scorer=fuzz.token_sort_ratio)
        if best and best[1] >= 80:
            return choices_map[best[0]], f"Fuzzy {best[1]}%"
    
    return sku or "UNKNOWN", "Raw SKU"

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None):
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map = _load_product_catalog(db)
    
    doc_obj = None
    
    if appsheet_doc_id:
        doc_obj = db.query(FnDocument).filter(FnDocument.DocumentID == appsheet_doc_id).first()
    
    if not doc_obj:
        doc_id = appsheet_doc_id if appsheet_doc_id else str(uuid.uuid4())[:8].upper()
        doc_obj = FnDocument(DocumentID=doc_id)
        db.add(doc_obj)
        logger.info(f"Creado nuevo documento: {doc_id}")
    else:
        logger.info(f"Actualizando documento existente: {appsheet_doc_id}")

    try:
        doc_date = datetime.strptime(header.get("doDate"), "%Y-%m-%d").date()
    except:
        doc_date = datetime.now().date()

    doc_obj.DatabaseID = "BBJ"
    doc_obj.doDate = doc_date
    doc_obj.doConsecutive = header.get("doConsecutive")
    doc_obj.doType = header.get("doType")
    doc_obj.doIssuer = header.get("doIssuerID")
    doc_obj.doReceptor = header.get("doReceptorID")
    doc_obj.doAccount = header.get("doAccount")
    doc_obj.CurrencyID = header.get("CurrencyID", "CRC")
    
    doc_obj.doFile = source_file_id
    doc_obj.DriveID = source_file_id
    doc_obj.doStatus = "PROCESSED_BY_AI"
    doc_obj.doCreatedBy = "AI_MICROSERVICE"
    doc_obj.Bot = f"Procesado. Uso IA: {data.get('usage', 'N/A')}"

    logs = []
    total_doc = 0
    line_number = 1
    
    total_subtotal = 0
    total_taxes = 0
    
    for line in lines:
        clean_supply_id, match_type = find_product_id(
            sku=line.get("sku_candidate"), 
            description=line.get("description"),
            sku_map=sku_map,
            choices_map=choices_map
        )
        
        ln_id = str(uuid.uuid4())
        
        qty = float(line.get("quantity", 0))
        price = float(line.get("unit_price", 0))
        discount = float(line.get("discount_amount", 0))
        taxes = float(line.get("tax_amount", 0))
        
        gross_amount = qty * price
        subtotal = gross_amount - discount
        line_total = subtotal + taxes
        
        new_ln = FnDocumentLn(
            DocumentLnID=ln_id,
            DocumentID=doc_obj.DocumentID, 
            DatabaseID="BBJ",
            dlNumber=line_number,
            SupplyID=clean_supply_id,
            CabysID=line.get("cabys_candidate"),
            dlDescription=line.get("description"),
            dlQuantity=qty,
            dlUnitPrice=price,
            dlDiscount=discount,
            dlSubtotal=subtotal,
            dlTaxes=taxes,
            dlTotal=line_total,
            dlObservations=f"Match: {match_type}"
        )
        db.add(new_ln)
        
        total_subtotal += subtotal
        total_taxes += taxes
        total_doc += line_total
        line_number += 1
        logs.append(f"Línea {line_number-1}: {clean_supply_id} ({match_type})")

    doc_obj.doSubtotal = total_subtotal
    doc_obj.doTaxes = total_taxes
    doc_obj.doTotal = total_doc
    
    db.commit()
    
    return {
        "status": "success", 
        "document_id": doc_obj.DocumentID, 
        "lines_count": line_number - 1,
        "logs": logs,
        "mode": "UPDATE_APPEND"
    }