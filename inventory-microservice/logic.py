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

def find_product_id(description: str, choices_map: dict):
    """Busca el ItemLnID usando match EXACTO de nombre para evitar falsos positivos."""
    if not description or not choices_map:
        return "UNKNOWN", "Raw Name"
    
    clean_desc = str(description).strip().lower()
    
    # 1. Búsqueda por match exacto (case-insensitive)
    for title, ln_id in choices_map.items():
        if str(title).strip().lower() == clean_desc:
            return ln_id, f"Exact Name ({title[:20]})"
    
    return "UNKNOWN", "Raw Name"

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

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = None):
    """PASO 1: Digitalización y Registro de Filas.
    Guarda el documento y las líneas. No afecta inventario.
    """
    database_id = (database_id or "")[:10]
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map, parent_map, variant_map = _load_product_catalog(db, database_id)
    project_choices = _load_project_catalog(db, database_id)
    
    issuer_data = header.get("issuer", {})
    receptor_data = header.get("receptor", {})

    issuer_id = header.get("doIssuerID")
    receptor_id = header.get("doReceptorID")
    
    if issuer_data and any(issuer_data.values()):
        # Issuer is usually Supplier/Partner
        issuer_data["cpCategory"] = "Supplier" 
        issuer_res = upsert_company_from_invoice_logic(db, issuer_data, source_file_id, database_id, update_if_exists=False)
        if issuer_res.get("status") == "success":
            issuer_id = issuer_res.get("company_id")
            
    if receptor_data and any(receptor_data.values()):
        # Receptor is usually Client/Company
        receptor_data["cpCategory"] = "Client"
        receptor_res = upsert_company_from_invoice_logic(db, receptor_data, source_file_id, database_id, update_if_exists=False)
        if receptor_res.get("status") == "success":
            receptor_id = receptor_res.get("company_id")
            
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
        doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else datetime.now().date()
    except:
        doc_date = datetime.now().date()

    doc_obj.DatabaseID = database_id  
    doc_obj.doDate = doc_date
    doc_obj.doConsecutive = header.get("doConsecutive")
    doc_obj.doType = header.get("doType")
    
    # Mapeo de Compañías
    doc_obj.IssuerID = (issuer_id or "")[:10]
    doc_obj.ReceptorID = (receptor_id or "")[:10]
    
    doc_obj.doAccount = header.get("doAccount")
    doc_obj.doCredit = header.get("doCredit")
    doc_obj.CurrencyID = header.get("CurrencyID", "CRC")
    doc_obj.doSubtotal = header.get("SubtotalAmount", 0.0)
    doc_obj.doTaxes = header.get("TaxAmount", 0.0)
    doc_obj.doTotal = header.get("TotalAmount", 0.0)
    doc_obj.doFile = source_file_id
    doc_obj.DriveID = source_file_id
    # Se eliminó el estado manual DRAFT a petición del usuario
    doc_obj.Bot = f"Digitalizado. Proyecto: {matched_project_id or 'N/A'}. IA: {data.get('usage', 'N/A')}"

    db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == doc_obj.DocumentID).delete()

    line_number = 1
    for line in lines:
        manual_desc = str(line.get("description") or "Sin descripción").strip()
        product_hint = str(line.get("product_name") or "").strip()
        
        # Match EXACTO inicial para sugerencia
        supply_id, _ = find_product_id(manual_desc, choices_map)
        
        qty = float(line.get("quantity", 0))
        price_unit = float(line.get("unit_price", 0))
        discount_ln = float(line.get("discount_amount", 0))
        subtotal_ln = float(line.get("subtotal_line", 0) or ((qty * price_unit) - discount_ln))
        tax_ln = float(line.get("tax_amount", 0))
        total_ln = float(line.get("total_line", 0) or (subtotal_ln + tax_ln))
        
        ln_uuid = str(uuid.uuid4()).replace('-', '')[:8].upper()
        
        new_ln = FnDocumentLn(
            DocumentLnID=ln_uuid,
            DocumentID=doc_obj.DocumentID,
            DatabaseID=database_id,
            dlNumber=line_number,
            SupplyID=supply_id if supply_id != "UNKNOWN" else None,
            CabysID=str(line.get("cabys_candidate") or "")[:50],
            dlDescription=manual_desc,
            dlQuantity=qty,
            dlUnitPrice=price_unit,
            dlDiscount=discount_ln,
            dlSubtotal=subtotal_ln,
            dlTaxes=tax_ln,
            dlTotal=total_ln,
            # Guardamos el nombre "Maestro" sugerido por la IA para usarlo en el Paso 2
            dlObservations=f"HINT:{product_hint}" if product_hint else None
        )
        db.add(new_ln)
        line_number += 1

    db.commit()
    return {"status": "success", "document_id": doc_obj.DocumentID}

def create_inventory_movements_logic(db: Session, document_id: str, database_id: str, image_folder_id: str = None, project_id: str = None):
    """PASO 2: Procesamiento de Inventario con Jerarquía Maestro-Variante.
    1. Busca variante exacta.
    2. Si no hay variante, busca artículo maestro (BcItem).
    3. Crea variante bajo maestro existente o crea ambos.
    """
    doc = db.query(FnDocument).filter(FnDocument.DocumentID == document_id).first()
    if not doc:
        return {"error": "Documento no encontrado"}
    
    # Reload catalog to get latest items
    sku_map, choices_map, parent_map, variant_map = _load_product_catalog(db, database_id)
    lines = db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == document_id).all()
    created_count = 0
    
    for ln in lines:
        final_supply_id = ln.SupplyID
        
        # Si no tiene SupplyID, intentamos match estricto de variante
        if not final_supply_id:
            final_supply_id, _ = find_product_id(ln.dlDescription, choices_map)
            
        # Si sigue siendo desconocido, aplicamos Lógica de Jerarquía
        if final_supply_id == "UNKNOWN" or not final_supply_id:
            product_hint = ""
            if ln.dlObservations and ln.dlObservations.startswith("HINT:"):
                product_hint = ln.dlObservations.replace("HINT:", "").strip()

            # 1. Buscar Maestro (BcItem)
            # Intentamos match exacto del hint contra itTitle
            existing_master_id = None
            if product_hint:
                master = db.query(BcItem).filter(BcItem.itTitle == product_hint, BcItem.DatabaseID == database_id).first()
                if master:
                    existing_master_id = master.ItemID
            
            # Si no hay hint o no hubo match, intentamos buscar si el nombre del padre está contenido en la descripción
            if not existing_master_id:
                # Verificamos si algún itTitle existente es prefijo de la descripción de la línea
                all_masters = db.query(BcItem).filter(BcItem.DatabaseID == database_id).all()
                for m in all_masters:
                    if m.itTitle.strip().lower() in ln.dlDescription.strip().lower():
                        existing_master_id = m.ItemID
                        break

            # 2. Crear o Reutilizar Maestro
            if existing_master_id:
                item_id = existing_master_id
                logger.info(f"Reutilizando Maestro Identificado: {item_id} para {ln.dlDescription}")
            else:
                item_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
                new_bc_item = BcItem(
                    ItemID=item_id,
                    DatabaseID=database_id,
                    itTitle=product_hint if product_hint else ln.dlDescription[:300],
                    itCreatedBy="AI_BOT"
                )
                db.add(new_bc_item)
                logger.info(f"Nuevo Maestro Creado: {item_id} ({product_hint or ln.dlDescription[:30]})")
                
                # Disparar descarga de imagen en background
                if image_folder_id:
                    img_query = product_hint or ln.dlDescription[:60]
                    img_filename = f"{item_id}.jpg"
                    t = threading.Thread(
                        target=fetch_and_upload_image_task,
                        args=(img_query, img_filename, image_folder_id, item_id),
                        daemon=True
                    )
                    t.start()

            # 3. Crear Variante (BcItemLn)
            final_supply_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            new_bc_item_ln = BcItemLn(
                ItemLnID=final_supply_id,
                ItemID=item_id,
                DatabaseID=database_id,
                lnCode=final_supply_id,  # Unique code per variant to avoid duplicate key constraint
                lnTitle=ln.dlDescription[:150],
                lnQuantity=0, 
                lnAvailable=0,
                lnCreatedBy="AI_BOT"
            )
            db.add(new_bc_item_ln)
            ln.SupplyID = final_supply_id
            
        # 4. Generar Movimientos y Precios
        mv_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_movement = IcMovement(
            MovementID=mv_id,
            DatabaseID=database_id,
            OriginID=(doc.IssuerID or "")[:10],
            ProjectID=(project_id or "")[:10] or None,
            ItemID=(final_supply_id or "")[:10],
            DocumentLnID=ln.DocumentLnID,
            mvDate=doc.doDate or datetime.now(),
            mvAction="IN",        
            mvQuantity=ln.dlQuantity,
            mvStatus="POSTED",
            mvNotes=f"Fulfillment fact {doc.doConsecutive}",
            mvCreatedby="AI_BOT"
        )
        db.add(new_movement)
        
        pr_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_price = IcPrice(
            PriceID=pr_id,
            DatabaseID=database_id,
            ItemID=(final_supply_id or "")[:10],
            MovementID=mv_id,
            ProjectID=(project_id or "")[:10] or None,
            prTitle="Ingreso",
            prDescription=(ln.dlDescription or "")[:255],
            prQuantity=ln.dlQuantity,
            prPrice=ln.dlUnitPrice,
            prTax=ln.dlTaxes,
            prTotal=ln.dlTotal,
            prCreatedby="AI_BOT"
        )
        db.add(new_price)
        
        variant = db.query(BcItemLn).filter(BcItemLn.ItemLnID == final_supply_id).first()
        if variant:
            variant.lnQuantity = (float(variant.lnQuantity or 0)) + float(ln.dlQuantity)
            variant.lnAvailable = (float(variant.lnAvailable or 0)) + float(ln.dlQuantity)
        
        created_count += 1
        
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
        company_obj.cpName = str(data.get("cpName"))[:300]
    if data.get("cpTitle"):
        company_obj.cpTitle = str(data.get("cpTitle"))[:300]
    if data.get("cpCategory"):
        company_obj.cpCategory = str(data.get("cpCategory"))[:150]
    if data.get("cpIdentification"):
        company_obj.cpIdentification = str(data.get("cpIdentification"))[:300]
    if data.get("cpAddress"):
        company_obj.cpAddress = str(data.get("cpAddress"))[:150]
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