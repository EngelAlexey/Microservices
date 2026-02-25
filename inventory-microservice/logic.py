from sqlalchemy.orm import Session
from models import BcItem, BcItemLn, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject, DrCompany
import difflib
import uuid
from datetime import datetime
import logging
from image_services import search_product_image
from drive_services import upload_image_to_drive, get_folder_path_from_drive
import threading

logger = logging.getLogger(__name__)

def fetch_and_upload_image_task(query: str, filename: str, folder_id: str, item_id: str = None):
    """Tarea en background para descargar y subir a Drive sin bloquear el request"""
    try:
        logger.info(f"Hilo Background: Iniciando búsqueda para [{query}] (Item: {item_id})...")
        img_bytes, img_type, _ = search_product_image(query)
        
        if img_bytes:
            drive_file_id = upload_image_to_drive(img_bytes, filename, img_type, folder_id)
            
            if drive_file_id:
                if item_id:
                    from database import SessionLocal
                    db = SessionLocal()
                    try:
                        item = db.query(BcItem).filter(BcItem.ItemID == item_id).first()
                        if item:
                            if not item.DriveID or item.itImage == filename:
                                item.DriveID = drive_file_id
                                item.itImage = filename
                                db.commit()
                                logger.info(f"DB Actualizada: Item {item_id} -> DriveID {drive_file_id}")
                    except Exception as de:
                        logger.error(f"Error al actualizar DB en background: {de}")
                    finally:
                        db.close()
            else:
                logger.error(f"La subida a Drive falló para el item {item_id}")
        else:
            logger.warning(f"No se encontró imagen para '{query}'")
    except Exception as e:
        logger.error(f"Error crítico en hilo de imagen: {e}")

def _load_product_catalog(db: Session, database_id: str):
    """Carga el catálogo de productos por DatabaseID.
    
    Hace JOIN entre bcItems (producto padre) y bcItemsLns (variantes)
    para obtener lnCode (SKU) y nombres.
    Retorna ItemLnID (variante) para diferenciar presentaciones en inventario.
    """
    all_items = db.query(BcItemLn.ItemLnID, BcItemLn.lnCode, BcItemLn.lnTitle, BcItem.itTitle, BcItem.ItemID, BcItem.DriveID)\
                  .join(BcItem, BcItemLn.ItemID == BcItem.ItemID)\
                  .filter(BcItemLn.DatabaseID == database_id)\
                  .filter(BcItemLn.isDeleted.isnot(True))\
                  .filter(BcItem.isDeleted.isnot(True)).all()
    
    sku_map = {}
    choices_map = {}
    parent_map = {}   # itTitle -> {"parent_id": ItemID, "drive_id": DriveID}
    variant_map = {}  # ItemLnID -> {"parent_id": ItemID, "drive_id": DriveID}
    
    for item in all_items:
        if item.lnCode:
            sku_map[item.lnCode.strip().upper()] = item.ItemLnID
            
        # Almacenar metadatos del variante/hijo
        variant_map[item.ItemLnID] = {"parent_id": item.ItemID, "drive_id": item.DriveID}
            
        # Se prioriza el nombre del producto padre, pero se puede usar el de la variante
        title = item.itTitle or item.lnTitle
        if title and title not in choices_map:
            choices_map[title] = item.ItemLnID
            
        # Mapear el producto padre para reuso de jerarquía
        if item.itTitle and item.itTitle not in parent_map:
            parent_map[item.itTitle] = {"parent_id": item.ItemID, "drive_id": item.DriveID}
            
    return sku_map, choices_map, parent_map, variant_map

def find_product_id(sku: str, description: str, sku_map: dict, choices_map: dict):
    """Busca el ItemLnID priorizando Fuzzy Match por descripción sobre SKU."""
    # 1. Intentar por descripción (Fuzzy Match) - Prioridad Alta
    if choices_map and description:
        keys = list(choices_map.keys())
        matches = difflib.get_close_matches(str(description).strip(), keys, n=1, cutoff=0.8)
        if matches:
            return choices_map[matches[0]], f"Fuzzy Name ({matches[0][:20]})"
            
    # 2. Intentar buscar por SKU si la descripción no dió un match seguro:
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
    if not address_text or not str(address_text).strip() or not project_choices:
        return None
    # Usamos difflib nativo para evitar crashes de Rust en Windows
    keys = list(project_choices.keys())
    matches = difflib.get_close_matches(str(address_text).strip(), keys, n=1, cutoff=0.75)
    if matches:
        return project_choices[matches[0]]
    return None

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = None, image_folder_id: str = None, create_movements: bool = True):
    # Truncate database_id to fit varchar(10)
    database_id = (database_id or "")[:10]
    
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map, parent_map, variant_map = _load_product_catalog(db, database_id)
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
        doc_id = (str(appsheet_doc_id)[:150]) if appsheet_doc_id else str(uuid.uuid4())[:8].upper()
        doc_obj = FnDocument(DocumentID=doc_id)
        db.add(doc_obj)
    
    try:
        doc_date_str = header.get("doDate")
        if doc_date_str:
            doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date()
        else:
            doc_date = datetime.now().date()
    except:
        doc_date = datetime.now().date()

    doc_obj.DatabaseID = database_id  
    doc_obj.doDate = doc_date
    doc_obj.doConsecutive = header.get("doConsecutive")
    doc_obj.doType = header.get("doType")
    doc_obj.doIssuer = issuer_id
    doc_obj.IssuerID = issuer_id
    doc_obj.doReceptor = receptor_id
    doc_obj.ReceptorID = receptor_id
    doc_obj.doAccount = header.get("doAccount")
    doc_obj.CurrencyID = header.get("CurrencyID", "CRC")
    doc_obj.doSubtotal = header.get("SubtotalAmount", 0.0)
    doc_obj.doTaxes = header.get("TaxAmount", 0.0)
    doc_obj.doTotal = header.get("TotalAmount", 0.0)
    doc_obj.doFile = source_file_id
    doc_obj.DriveID = source_file_id
    doc_obj.doStatus = "PROCESSED_BY_AI"
    doc_obj.Bot = f"Step: Digitized. Project: {matched_project_id or 'N/A'}. IA: {data.get('usage', 'N/A')}"

    logs = []
    line_number = 1
    
    # Clean previous lines if re-processing
    if appsheet_doc_id:
        db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == appsheet_doc_id).delete()

    for line in lines:
        clean_supply_id, match_type = find_product_id(
            sku=line.get("sku_candidate"), 
            description=line.get("description"),
            sku_map=sku_map,
            choices_map=choices_map
        )
        
        is_found = match_type != "Raw SKU"
        qty = float(line.get("quantity", 0))
        price_unit = float(line.get("unit_price", 0))
        subtotal_ln = float(line.get("subtotal_line", 0) or (qty * price_unit))
        tax_ln = float(line.get("tax_amount", 0))
        total_ln = float(line.get("total_line", 0) or (subtotal_ln + tax_ln))
        
        ln_uuid = str(uuid.uuid4()).replace('-', '')[:8].upper()

        product_name = str(line.get("product_name") or line.get("description") or "Producto").strip()
        product_desc = str(line.get("description") or "")
        item_id = None
        existing_drive_id = None

        if is_found:
            meta = variant_map.get(clean_supply_id)
            if meta:
                item_id = meta.get("parent_id")
                existing_drive_id = meta.get("drive_id")
        else:
            if parent_map:
                p_keys = list(parent_map.keys())
                p_matches = difflib.get_close_matches(product_name, p_keys, n=1, cutoff=0.85)
                if p_matches:
                    meta = parent_map[p_matches[0]]
                    item_id = meta["parent_id"]
                    existing_drive_id = meta["drive_id"]

        if not item_id:
            item_id = str(uuid.uuid4()).replace('-', '')[:8].upper()

        image_path = None
        if not existing_drive_id and image_folder_id:
            time_code = datetime.now().strftime("%H%M%S")
            filename = f"{item_id}.itImage.{time_code}.png"
            resolved_folder_path = get_folder_path_from_drive(image_folder_id)
            if resolved_folder_path and not resolved_folder_path.startswith("03-Aplicaciones"):
                resolved_folder_path = f"03-Aplicaciones/{resolved_folder_path}"
            image_path = f"{resolved_folder_path}{filename}"
            threading.Thread(
                target=fetch_and_upload_image_task, 
                args=(f"{product_name} {product_desc}", filename, image_folder_id, item_id),
                daemon=True
            ).start()

        if not is_found:
            if item_id not in [v.get("parent_id") for v in variant_map.values() if v.get("parent_id")]:
                new_bc_item = BcItem(
                    ItemID=item_id,
                    DatabaseID=database_id,
                    itTitle=product_name[:300],
                    itDescription=product_desc,
                    CabysID=str(line.get("cabys_candidate") or "")[:20] if line.get("cabys_candidate") else None,
                    itImage=image_path,
                    itCreatedBy="AI_BOT"
                )
                db.add(new_bc_item)
                parent_map[product_name] = {"parent_id": item_id, "drive_id": image_path}

            clean_supply_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            new_bc_item_ln = BcItemLn(
                ItemLnID=clean_supply_id,
                ItemID=item_id,
                DatabaseID=database_id,
                lnCode=str(line.get("sku_candidate") or "SIN-CODIGO")[:50],
                lnTitle=str(line.get("variant_name") or line.get("description") or "Producto Nuevo")[:150],
                lnSize=str(line.get("size"))[:100] if line.get("size") else None,
                lnQuantity=0, # Initial stock 0 (movements will increase it)
                lnAvailable=0,
                lnCreatedBy="AI_BOT"
            )
            db.add(new_bc_item_ln)
        
        new_ln = FnDocumentLn(
            DocumentLnID=ln_uuid,
            DocumentID=doc_obj.DocumentID,
            DatabaseID=database_id,
            dlNumber=line_number,
            SupplyID=clean_supply_id,
            CabysID=str(line.get("cabys_candidate") or "")[:50],
            dlDescription=line.get("description"),
            dlQuantity=qty,
            dlUnitPrice=price_unit,
            dlSubtotal=subtotal_ln,
            dlTaxes=tax_ln,
            dlTotal=total_ln,
            dlObservations=f"Match: {match_type}"
        )
        db.add(new_ln)
        line_number += 1

    db.commit()
    
    if create_movements:
        create_inventory_movements_logic(db, doc_obj.DocumentID, database_id)
        doc_obj.doStatus = "COMPLETED"
        db.commit()

    return {
        "status": "success", 
        "document_id": doc_obj.DocumentID, 
        "logs": logs,
        "database_id": database_id
    }

def create_inventory_movements_logic(db: Session, document_id: str, database_id: str):
    """Genera movimientos de inventario basados en las líneas del documento confirmado."""
    doc = db.query(FnDocument).filter(FnDocument.DocumentID == document_id).first()
    if not doc:
        return {"error": "Documento no encontrado"}
    
    lines = db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == document_id).all()
    created_count = 0
    
    for ln in lines:
        if not ln.SupplyID or ln.SupplyID == "UNKNOWN":
            continue
            
        mv_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        
        new_movement = IcMovement(
            MovementID=mv_id,
            DatabaseID=database_id,
            OriginID=(doc.IssuerID or doc.doIssuer or "")[:10],
            ItemID=(ln.SupplyID or "")[:10],
            DocumentLnID=ln.DocumentLnID, 
            mvDate=doc.doDate or datetime.now(),
            mvAction="IN",        
            mvQuantity=ln.dlQuantity,
            mvStatus="POSTED",
            mvNotes=f"Auto-generado por Factura {doc.doConsecutive}",
            mvCreatedby="AI_BOT"
        )
        db.add(new_movement)
        
        pr_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_price = IcPrice(
            PriceID=pr_id,
            DatabaseID=database_id,
            ItemID=(ln.SupplyID or "")[:10],
            MovementID=mv_id,         
            prTitle="Ingreso",
            prDescription=(ln.dlDescription or "")[:255],
            prQuantity=ln.dlQuantity,
            prPrice=ln.dlUnitPrice,
            prTax=ln.dlTaxes,
            prTotal=ln.dlTotal,
            prCreatedby="AI_BOT"
        )
        db.add(new_price)
        
        # Update stock in bcItemsLns
        variant = db.query(BcItemLn).filter(BcItemLn.ItemLnID == ln.SupplyID).first()
        if variant:
            variant.lnQuantity = (float(variant.lnQuantity or 0)) + float(ln.dlQuantity)
            variant.lnAvailable = (float(variant.lnAvailable or 0)) + float(ln.dlQuantity)
        
        created_count += 1
        
    doc.doStatus = "COMPLETED"
    db.commit()
    return {"status": "success", "movements_created": created_count}

def upsert_company_from_invoice_logic(db: Session, data: dict, source_file_id: str, database_id: str = None, target_company_id: str = None, update_if_exists: bool = True):
    """
    Extracted logic to process company details (Issuer or Receptor).
    Checks for duplicates in DrCompanies by tax ID.
    If target_company_id is provided, updates that exact record.
    Returns a dict with status & company_id.
    """
    
    cp_identification = str(data.get("cpIdentification", "")).strip()
    cp_name = str(data.get("cpName") or "").strip()
    
    if not cp_name and not cp_identification:
        return {"status": "skipped", "reason": "No name or ID provided"}
        
    company_obj = None
    
    # 1. Búsqueda prioritaria por ID provisto por AppSheet
    if target_company_id:
        company_obj = db.query(DrCompany).filter(
            DrCompany.CompanyID == target_company_id,
            DrCompany.DatabaseID == database_id
        ).first()

    # 2. Búsqueda por Cédula si no hay ID o no se encontró
    if not company_obj and cp_identification:
        company_obj = db.query(DrCompany).filter(
            DrCompany.cpIdentification == cp_identification,
            DrCompany.DatabaseID == database_id
        ).first()
        
    # If strictly needed, fallbacks to exact textual match on cpName
    if not company_obj and cp_name:
        company_obj = db.query(DrCompany).filter(
            DrCompany.cpName == cp_name,
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