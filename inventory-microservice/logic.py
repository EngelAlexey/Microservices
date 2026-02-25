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

def fetch_and_upload_image_task(query: str, filename: str, folder_id: str):
    """Tarea en background para descargar y subir a Drive sin bloquear el request"""
    try:
        print(f"\n[BACKGROUND_TASK] Iniciando para: {query}")
        logger.info(f"Hilo Background: Buscando imagen para [{query}]...")
        img_bytes, img_type, img_ext = search_product_image(query)
        
        if img_bytes:
            print(f"[BACKGROUND_TASK] Imagen obtenida ({len(img_bytes)} bytes). Subiendo a Drive...")
            # Subimos manteniendo el nombre 'filename' impuesto por la DB
            uploaded_url = upload_image_to_drive(img_bytes, filename, img_type, folder_id)
            if uploaded_url:
                print(f"[BACKGROUND_TASK] ¡EXITO! Imagen subida: {uploaded_url}")
                logger.info(f"Hilo Background: OK. Imagen guardada como {filename} en Drive.")
            else:
                print(f"[BACKGROUND_TASK] ERROR: La subida a Drive falló.")
                logger.error(f"Hilo Background: Falló la subida a Drive para {filename}")
        else:
            print(f"[BACKGROUND_TASK] ADVERTENCIA: No se encontró imagen para '{query}'.")
            logger.warning(f"Hilo Background: No se encontró imagen para {query}")
    except Exception as e:
        print(f"[BACKGROUND_TASK] CRASH CRITICO: {e}")
        import traceback
        traceback.print_exc()
        logger.error(f"Error en Hilo Background Imagen: {e}")

def _load_product_catalog(db: Session, database_id: str):
    """Carga el catálogo de productos por DatabaseID.
    
    Hace JOIN entre bcItems (producto padre) y bcItemsLns (variantes)
    para obtener lnCode (SKU) y nombres.
    Retorna ItemLnID (variante) para diferenciar presentaciones en inventario.
    """
    all_items = db.query(BcItemLn.ItemLnID, BcItemLn.lnCode, BcItemLn.lnTitle, BcItem.itTitle, BcItem.ItemID)\
                  .join(BcItem, BcItemLn.ItemID == BcItem.ItemID)\
                  .filter(BcItemLn.DatabaseID == database_id)\
                  .filter(BcItemLn.isDeleted.isnot(True))\
                  .filter(BcItem.isDeleted.isnot(True)).all()
    
    sku_map = {}
    choices_map = {}
    parent_map = {} # Mapeo de itTitle -> ItemID para productos maestros
    for item in all_items:
        if item.lnCode:
            sku_map[item.lnCode.strip().upper()] = item.ItemLnID
        # Se prioriza el nombre del producto padre, pero se puede usar el de la variante
        title = item.itTitle or item.lnTitle
        if title and title not in choices_map:
            choices_map[title] = item.ItemLnID
            
        # Mapear el producto padre para reuso de jerarquía
        if item.itTitle and item.itTitle not in parent_map:
            parent_map[item.itTitle] = item.ItemID
    return sku_map, choices_map, parent_map

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

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = "BBJ", image_folder_id: str = None):
    header = data.get("header", {})
    lines = data.get("lines", [])
    
    sku_map, choices_map, parent_map = _load_product_catalog(db, database_id)
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
        
        is_found = match_type != "Raw SKU"
        
        qty = float(line.get("quantity", 0))
        price_unit = float(line.get("unit_price", 0))
        total_line = float(line.get("total", (qty * price_unit))) 

        ln_uuid = str(uuid.uuid4()).replace('-', '')[:10].upper()
        
        if not is_found and clean_supply_id != "UNKNOWN":
            # 1. Intentar buscar un producto PADRE existente antes de crear uno nuevo
            product_name = str(line.get("product_name") or line.get("description") or "Producto Nuevo").strip()
            item_id = None
            
            if parent_map:
                p_keys = list(parent_map.keys())
                p_matches = difflib.get_close_matches(product_name, p_keys, n=1, cutoff=0.85)
                if p_matches:
                    item_id = parent_map[p_matches[0]]
                    logger.info(f"Línea {line_number}: Asociando a producto maestro existente: {p_matches[0]} ({item_id})")

            # 2. Si no hay padre, crearlo
            if not item_id:
                item_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
                
                image_path = None
                product_desc = str(line.get("description") or "")
                if image_folder_id and product_desc:
                    # AppSheet Format: "Kaizen/A14-Bodegas Benjamin/Items/0895E257F3.itImage.163806.png"
                    time_code = datetime.now().strftime("%H%M%S")
                    filename = f"{item_id}.itImage.{time_code}.png"
                    
                    # 3. Guardamos la ruta completa para AppSheet
                    # El usuario indicó que debe empezar desde la raíz: "03-Aplicaciones/..."
                    resolved_folder_path = get_folder_path_from_drive(image_folder_id)
                    
                    # Si el Service Account no ve la raíz del Shared Drive, la forzamos
                    if resolved_folder_path and not resolved_folder_path.startswith("03-Aplicaciones"):
                        resolved_folder_path = f"03-Aplicaciones/{resolved_folder_path}"
                    
                    image_path = f"{resolved_folder_path}{filename}"
                    
                    # Lanzar el hilo en background para descargar y subir a Drive
                    threading.Thread(
                        target=fetch_and_upload_image_task, 
                        args=(product_desc, filename, image_folder_id),
                        daemon=True
                    ).start()

                new_bc_item = BcItem(
                    ItemID=item_id,
                    DatabaseID=database_id,
                    itTitle=product_name[:300],
                    itDescription=product_desc,
                    CabysID=str(line.get("cabys_candidate") or "")[:20] if line.get("cabys_candidate") else None,
                    itImage=image_path,
                    itStatus=True,
                    itCreatedBy="AI_BOT",
                    Bot=f"Auto-creado por factura {doc_obj.doConsecutive}"
                )
                db.add(new_bc_item)
                # Agregamos al mapa local para evitar duplicados en la misma factura si vienen varias líneas del mismo padre
                parent_map[product_name] = item_id
            
            # 3. Crear la variante (Hijo)
            clean_supply_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
            
            new_bc_item_ln = BcItemLn(
                ItemLnID=clean_supply_id,
                ItemID=item_id,
                DatabaseID=database_id,
                lnCode=str(line.get("sku_candidate") or "SIN-CODIGO")[:50],
                lnTitle=str(line.get("variant_name") or line.get("description") or "Producto Nuevo")[:150],
                lnSize=str(line.get("size"))[:100] if line.get("size") else None,
                lnQuantity=qty,
                lnAvailable=qty,
                lnStatus=True,
                lnCreatedBy="AI_BOT",
                Bot=f"Auto-creado por factura {doc_obj.doConsecutive}"
            )
            db.add(new_bc_item_ln)
            
            if line.get("sku_candidate"):
                sku_map[str(line.get("sku_candidate")).strip().upper()] = clean_supply_id
                
            logs.append(f"Línea {line_number}: Producto nuevo {clean_supply_id} creado.")
            
        elif is_found:
            existing_ln = db.query(BcItemLn).filter(
                BcItemLn.ItemLnID == clean_supply_id,
                BcItemLn.DatabaseID == database_id
            ).first()
            if existing_ln:
                existing_ln.lnQuantity = (float(existing_ln.lnQuantity) if existing_ln.lnQuantity else 0.0) + qty
                existing_ln.lnAvailable = (float(existing_ln.lnAvailable) if existing_ln.lnAvailable else 0.0) + qty
                existing_ln.lnModifiedBy = "AI_BOT"
                existing_ln.lnModifiedAt = datetime.now()
            logs.append(f"Línea {line_number}: Stock de {clean_supply_id} actualizado.")
        
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
            
            mv_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
            
            # Truncate values to fit varchar(10) in auxiliary tables
            truncated_origin = (doc_obj.doIssuer or "")[:10]
            truncated_item = (clean_supply_id or "")[:10]

            new_movement = IcMovement(
                MovementID=mv_id,
                DatabaseID=database_id,
                OriginID=truncated_origin,
                ProjectID=None, 
                ItemID=truncated_item,
                DocumentLnID=(line.get("description") or ""), 
                mvDate=doc_date,
                mvAction="IN",        
                mvQuantity=qty,
                mvStatus="POSTED",
                mvNotes=f"Auto-generado por Factura {doc_obj.doConsecutive}",
                mvCreatedby="AI_BOT"
            )
            db.add(new_movement)
            
            pr_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
            
            new_price = IcPrice(
                PriceID=pr_id,
                DatabaseID=database_id,
                ItemID=truncated_item,
                ProjectID=None, 
                MovementID=mv_id,         
                prTitle="Ingreso",
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

def upsert_company_from_invoice_logic(db: Session, data: dict, source_file_id: str, database_id: str = "BBJ", target_company_id: str = None, update_if_exists: bool = True):
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