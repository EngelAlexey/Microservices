from sqlalchemy.orm import Session
from models import BcItem, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject
from thefuzz import process, fuzz
import uuid
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def _load_product_catalog(db: Session, database_id: str):
    """Carga el catálogo de productos por DatabaseID."""
    all_items = db.query(BcItem.ItemID, BcItem.itCode, BcItem.itTitle)\
                  .filter(BcItem.DatabaseID == database_id).all() 
    
    sku_map = {}
    choices_map = {}
    for item in all_items:
        if item.itCode:
            sku_map[item.itCode.strip().upper()] = item.ItemID
        if item.itTitle:
            choices_map[item.itTitle] = item.ItemID
    return sku_map, choices_map

def find_product_id(sku: str, description: str, sku_map: dict, choices_map: dict):
    """Busca el ItemID usando SKU y Fuzzy Match por descripción."""
    if sku:
        clean_sku = sku.strip().upper()
        if clean_sku in sku_map:
            return sku_map[clean_sku], "Exact SKU"
    
    if choices_map and description:
        best = process.extractOne(description, choices_map.keys(), scorer=fuzz.token_sort_ratio)
        if best and best[1] >= 80:
            return choices_map[best[0]], f"Fuzzy {best[1]}%"
    
    return sku or "UNKNOWN", "Raw SKU"

def _load_project_catalog(db: Session, database_id: str):
    projects = db.query(DrProject.ProjectID, DrProject.pjTitle, DrProject.pjAddress)\
                 .filter(DrProject.DatabaseID == database_id).all()
    project_choices = {}
    for pj in projects:
        # Usamos tanto el título como la dirección para el matching
        key = f"{pj.pjTitle or ''} {pj.pjAddress or ''}".strip()
        if key:
            project_choices[key] = pj.ProjectID
    return project_choices

def find_project_id(address_text: str, project_choices: dict):
    if not address_text or not project_choices:
        return None
    # Usamos partial_ratio para proyectos porque las direcciones suelen ser largas y contener el nombre
    best = process.extractOne(address_text, project_choices.keys(), scorer=fuzz.partial_ratio)
    if best and best[1] >= 75: 
        return project_choices[best[0]]
    return None

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = "BBJ"):
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map = _load_product_catalog(db, database_id)
    project_choices = _load_project_catalog(db, database_id)
    
    # Matching de proyecto basado en las direcciones extraídas
    address_to_match = f"{header.get('doIssuerAddress', '')} {header.get('doReceptorAddress', '')}"
    matched_project_id = find_project_id(address_to_match, project_choices)
    
    doc_obj = None
    if appsheet_doc_id:
        doc_obj = db.query(FnDocument).filter(FnDocument.DocumentID == appsheet_doc_id).first()
    
    if not doc_obj:
        doc_id = appsheet_doc_id if appsheet_doc_id else str(uuid.uuid4())[:8].upper()
        doc_obj = FnDocument(DocumentID=doc_id)
        db.add(doc_obj)
    
    try:
        doc_date = datetime.strptime(header.get("doDate"), "%Y-%m-%d").date()
    except:
        doc_date = datetime.now().date()

    doc_obj.DatabaseID = database_id  
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
    doc_obj.Bot = f"Procesado c/Proyecto: {matched_project_id or 'N/A'}. Uso IA: {data.get('usage', 'N/A')}"

    logs = []
    total_doc = 0
    line_number = 1
    
    for line in lines:
        clean_supply_id, match_type = find_product_id(
            sku=line.get("sku_candidate"), 
            description=line.get("description"),
            sku_map=sku_map,
            choices_map=choices_map
        )
        
        qty = float(line.get("quantity", 0))
        price_unit = float(line.get("unit_price", 0))
        total_line = float(line.get("total", (qty * price_unit))) 

        ln_uuid = str(uuid.uuid4())
        ln_id_short = ln_uuid[:8].upper() 
        
        new_ln = FnDocumentLn(
            DocumentLnID=ln_uuid,
            DocumentID=doc_obj.DocumentID,
            DatabaseID=database_id,
            dlNumber=line_number,
            SupplyID=clean_supply_id,
            dlDescription=line.get("description"),
            dlQuantity=qty,
            dlUnitPrice=price_unit,
            dlTotal=total_line,
            dlObservations=f"Match: {match_type}"
        )
        db.add(new_ln)

        if clean_supply_id and clean_supply_id != "UNKNOWN":
            
            mv_id = str(uuid.uuid4())[:8].upper()
            
            # Truncate values to fit varchar(10) in auxiliary tables
            truncated_origin = (doc_obj.doIssuer or "")[:10]
            truncated_item = (clean_supply_id or "")[:10]

            new_movement = IcMovement(
                MovementID=mv_id,
                DatabaseID=database_id,
                OriginID=truncated_origin,
                ProjectID=matched_project_id,
                ItemID=truncated_item,
                DocumentLnID=ln_id_short, 
                mvDate=doc_date,
                mvAction="IN",        
                mvQuantity=qty,
                mvStatus="Applied",
                mvNotes=f"Auto-generado por Factura {doc_obj.doConsecutive}",
                mvCreatedby="AI_BOT"
            )
            db.add(new_movement)
            
            pr_id = str(uuid.uuid4())[:8].upper()
            
            new_price = IcPrice(
                PriceID=pr_id,
                DatabaseID=database_id,
                ItemID=truncated_item,
                ProjectID=matched_project_id,
                MovementID=mv_id,         
                prTitle=f"Lote Fac {doc_obj.doConsecutive}",
                prDescription=line.get("description"),
                prQuantity=qty,
                prPrice=price_unit,
                prTotal=total_line,
                prCreatedby="AI_BOT"
            )
            db.add(new_price)
            
            logs.append(f"Línea {line_number}: {clean_supply_id} -> Movimiento {mv_id} Creado.")
        else:
            logs.append(f"Línea {line_number}: Producto NO identificado. No se generó movimiento.")

        total_doc += total_line
        line_number += 1

    doc_obj.doTotal = total_doc
    db.commit()
    
    return {
        "status": "success", 
        "document_id": doc_obj.DocumentID, 
        "logs": logs,
        "database_id": database_id,
        "matched_project": matched_project_id
    }