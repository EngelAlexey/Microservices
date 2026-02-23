from sqlalchemy.orm import Session
from models import BcItem, BcItemLn, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject, DrCompany
from thefuzz import process, fuzz
import uuid
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def _load_product_catalog(db: Session, database_id: str):
    """Carga el catálogo de productos por DatabaseID.
    
    Hace JOIN entre bcItems (producto padre) y bcItemsLns (variantes)
    para obtener lnCode (SKU) y nombres.
    Retorna ItemLnID (variante) para diferenciar presentaciones en inventario.
    """
    all_items = db.query(BcItemLn.ItemLnID, BcItemLn.lnCode, BcItemLn.lnTitle, BcItem.itTitle)\
                  .join(BcItem, BcItemLn.ItemID == BcItem.ItemID)\
                  .filter(BcItemLn.DatabaseID == database_id)\
                  .filter(BcItemLn.isDeleted == False)\
                  .filter(BcItem.isDeleted == False).all()
    
    sku_map = {}
    choices_map = {}
    for item in all_items:
        if item.lnCode:
            sku_map[item.lnCode.strip().upper()] = item.ItemLnID
        # Se prioriza el nombre del producto padre, pero se puede usar el de la variante
        title = item.itTitle or item.lnTitle
        if title and title not in choices_map:
            choices_map[title] = item.ItemLnID
    return sku_map, choices_map

def find_product_id(sku: str, description: str, sku_map: dict, choices_map: dict):
    """Busca el ItemLnID priorizando Fuzzy Match por descripción sobre SKU."""
    # 1. Intentar por descripción (Fuzzy Match) - Prioridad Alta
    if choices_map and description:
        best = process.extractOne(description, choices_map.keys(), scorer=fuzz.token_sort_ratio)
        if best and best[1] >= 80:
            return choices_map[best[0]], f"Fuzzy {best[1]}%"
    
    # 2. Fallback: Intentar por SKU exacto
    if sku:
        clean_sku = sku.strip().upper()
        if clean_sku in sku_map:
            return sku_map[clean_sku], "Exact SKU"
    
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
    
    issuer_data = header.get("issuer", {})
    receptor_data = header.get("receptor", {})

    issuer_id = header.get("doIssuerID")
    receptor_id = header.get("doReceptorID")
    
    # Upsert Issuer
    if issuer_data and any(issuer_data.values()):
        issuer_res = upsert_company_from_invoice_logic(db, issuer_data, source_file_id, database_id, update_if_exists=False)
        if issuer_res.get("status") == "success":
            issuer_id = issuer_res.get("company_id")
            
    # Upsert Receptor
    if receptor_data and any(receptor_data.values()):
        receptor_res = upsert_company_from_invoice_logic(db, receptor_data, source_file_id, database_id, update_if_exists=False)
        if receptor_res.get("status") == "success":
            receptor_id = receptor_res.get("company_id")
            
    # Matching de proyecto basado en las direcciones extraídas
    address_to_match = f"{issuer_data.get('cpAddress', '')} {receptor_data.get('cpAddress', '')}"
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
    doc_obj.doIssuer = issuer_id
    doc_obj.doReceptor = receptor_id
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
                ProjectID=None, 
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
                ProjectID=None, 
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

def upsert_company_from_invoice_logic(db: Session, data: dict, source_file_id: str, database_id: str, target_company_id: str = None, update_if_exists: bool = True):
    if not data:
        return {"status": "error", "message": "No data extraida"}
    
    identification = data.get("cpIdentification", "")
    if identification:
        identification = str(identification).strip()
    name = data.get("cpName", "Empresa Desconocida")
    if name:
        name = str(name).strip()
    
    company_obj = None
    
    # 1. Búsqueda prioritaria por ID provisto por AppSheet
    if target_company_id:
        company_obj = db.query(DrCompany).filter(
            DrCompany.CompanyID == target_company_id,
            DrCompany.DatabaseID == database_id
        ).first()

    # 2. Búsqueda por Cédula si no hay ID o no se encontró
    if not company_obj and identification:
        company_obj = db.query(DrCompany).filter(
            DrCompany.cpIdentification == identification,
            DrCompany.DatabaseID == database_id
        ).first()
        
    # 3. Búsqueda por Nombre como última opción
    if not company_obj:
        if name and name != "Empresa Desconocida":
            company_obj = db.query(DrCompany).filter(
                DrCompany.cpName.ilike(f"%{name}%"),
                DrCompany.DatabaseID == database_id
            ).first()

    is_new = False
    if not company_obj:
        company_id = str(uuid.uuid4())[:8].upper()
        company_obj = DrCompany(CompanyID=company_id)
        company_obj.cpCreatedBy = "AI_BOT"
        db.add(company_obj)
        is_new = True
    else:
        if not update_if_exists:
            return {
                "status": "success",
                "action": "found",
                "company_id": company_obj.CompanyID,
                "company_name": company_obj.cpName,
                "database_id": database_id
            }
            
        company_obj.cpModifiedby = "AI_BOT"
        company_obj.cpModifiedAt = datetime.now()

    company_obj.DatabaseID = database_id
    company_obj.cpFile = source_file_id
    
    if data.get("cpName"):
        company_obj.cpName = str(data.get("cpName"))[:200]
    if data.get("cpTitle"):
        company_obj.cpTitle = str(data.get("cpTitle"))[:150]
    if data.get("cpIdentification"):
        company_obj.cpIdentification = str(data.get("cpIdentification"))[:100]
    if data.get("cpAddress"):
        company_obj.cpAddress = str(data.get("cpAddress"))[:500]
    if data.get("cpEmail"):
        company_obj.cpEmail = str(data.get("cpEmail"))[:150]
    if data.get("cpPhone"):
        company_obj.cpPhone = str(data.get("cpPhone"))[:100]
        
    company_obj.cpBot = f"Procesado c/IA. Uso: {data.get('usage', 'N/A')}"
    
    db.commit()
    
    action = "inserted" if is_new else "updated"
    return {
        "status": "success",
        "action": action,
        "company_id": company_obj.CompanyID,
        "company_name": company_obj.cpName,
        "database_id": database_id
    }