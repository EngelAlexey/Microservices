from sqlalchemy.orm import Session
from models import BcItem, FnDocument, FnDocumentLn
from thefuzz import process, fuzz
import uuid
from datetime import datetime

def _load_product_catalog(db: Session):
    """Carga el catálogo de productos UNA sola vez por request.
    
    Returns:
        tuple: (sku_map, choices_map)
            - sku_map: dict {itCode: ItemID} para búsqueda exacta por SKU
            - choices_map: dict {itTitle: ItemID} para fuzzy fallback
    """
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
    """Busca el ItemID usando 2 métodos (en lugar de 3):
    
    1. Búsqueda exacta por SKU (en memoria, O(1))
    2. Fuzzy match por descripción (fallback, en memoria)
    
    Se eliminó la búsqueda por CABYS porque agrega latencia sin
    aportar precisión significativa.
    """
    if sku:
        clean_sku = sku.strip().upper()
        if clean_sku in sku_map:
            return sku_map[clean_sku], "Exact SKU"
    
    if choices_map and description:
        best = process.extractOne(description, choices_map.keys(), scorer=fuzz.token_sort_ratio)
        if best and best[1] >= 80:
            return choices_map[best[0]], f"Fuzzy {best[1]}%"
    
    return sku or "UNKNOWN", "Raw SKU"


def insert_document_logic(db: Session, data: dict, source_file_id: str):
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map = _load_product_catalog(db)
    
    doc_id = str(uuid.uuid4())[:8].upper()
    
    try:
        doc_date = datetime.strptime(header.get("doDate"), "%Y-%m-%d").date()
    except:
        doc_date = datetime.now().date()

    current_db_id = "BBJ"
    
    new_doc = FnDocument(
        DocumentID=doc_id,
        DatabaseID=current_db_id,
        doDate=doc_date,
        doConsecutive=header.get("doConsecutive"),
        doType=header.get("doType"),
        doIssuer=header.get("doIssuerID"),
        doReceptor=header.get("doReceptorID"),
        doFile=source_file_id,
        DriveID=source_file_id,
        doStatus="READY_FOR_BOT",
        doCreatedBy="AI_MICROSERVICE",
        doTotal=0, 
        Bot=f"Procesado por Microservicio Python. Uso IA: {data.get('usage', 'N/A')}"
    )
    db.add(new_doc)
    
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
            DocumentID=doc_id,
            DatabaseID=current_db_id,
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

    new_doc.doSubtotal = total_subtotal
    new_doc.doTaxes = total_taxes
    new_doc.doTotal = total_doc
    
    db.commit()
    
    return {
        "status": "success", 
        "document_id": doc_id, 
        "lines_count": line_number - 1,
        "logs": logs
    }