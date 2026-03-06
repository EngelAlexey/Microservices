from sqlalchemy.orm import Session
from models import BcItem, BcItemLn, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject, DrCompany, BcBrand
import difflib
import uuid
from datetime import datetime, timedelta, timezone
import logging
import re
from sqlalchemy import func
from image_services import search_product_image
from drive_services import upload_image_to_drive, get_folder_path_from_drive
import threading

logger = logging.getLogger(__name__)

def get_now_ca():
    """Returns current datetime in Central America timezone (GMT-6)."""
    return datetime.now(timezone(timedelta(hours=-6))).replace(tzinfo=None)

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

def find_product_id(description: str, choices_map: dict, product_hint: str = "", brand_hint: str = "", model_hint: str = ""):
    """Busca el ItemLnID usando match EXACTO y luego FUZZY para encontrar el producto correcto."""
    if not choices_map:
        return "UNKNOWN", "Raw Name"
    
    clean_desc = str(description).strip().lower()
    
    # 1. Búsqueda por match exacto (case-insensitive)
    for title, ln_id in choices_map.items():
        if title and str(title).strip().lower() == clean_desc:
            return ln_id, f"Exact Name ({title[:20]})"
            
    # 2. Búsqueda combinada usando hints de IA (Marca + Modelo/Producto)
    hints_combined = f"{brand_hint} {product_hint} {model_hint}".strip().lower()
    if hints_combined:
        for title, ln_id in choices_map.items():
            if title and str(title).strip().lower() == hints_combined:
                return ln_id, f"Exact Hint ({title[:20]})"
                
    # 3. Búsqueda difusa (Fuzzy Match) usando descripción o hints
    import difflib
    keys = list(choices_map.keys())
    
    # Intentar con la descripción original
    if clean_desc:
        matches = difflib.get_close_matches(clean_desc, keys, n=1, cutoff=0.75)
        if matches:
            return choices_map[matches[0]], f"Fuzzy Desc ({matches[0][:20]})"
            
    # Intentar con los hints combinados
    if hints_combined:
        matches = difflib.get_close_matches(hints_combined, keys, n=1, cutoff=0.75)
        if matches:
            return choices_map[matches[0]], f"Fuzzy Hint ({matches[0][:20]})"
    
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

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = None, drive_id: str = None):
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
    
    now = get_now_ca()
    try:
        doc_date_str = header.get("doDate")
        doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else now.date()
    except:
        doc_date = now.date()

    doc_obj.doCreatedAt = now
    doc_obj.doCreatedBy = "AI_BOT"
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
    doc_obj.DriveID = drive_id or source_file_id
    # Se eliminó el estado manual DRAFT a petición del usuario
    doc_obj.doAIComment = f"Digitalizado. Proyecto: {matched_project_id or 'N/A'}. IA: {data.get('usage', 'N/A')}"

    # Se evita llamar a .delete() si no hay líneas para prevenir error de permisos (DELETE command denied)
    if db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == doc_obj.DocumentID).count() > 0:
        import logging
        logging.getLogger(__name__).warning(f"Document {doc_obj.DocumentID} already has lines. Cannot delete due to DB permissions.")
        # db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == doc_obj.DocumentID).delete()

    line_number = 1
    for line in lines:
        manual_desc = str(line.get("description") or "Sin descripción").strip()
        product_hint = str(line.get("product_name") or "").strip()
        brand_hint = str(line.get("brand") or "").strip()
        model_hint = str(line.get("model") or "").strip()
        
        # Match EXACTO inicial para sugerencia
        supply_id, _ = find_product_id(manual_desc, choices_map, product_hint, brand_hint, model_hint)
        
        qty = float(line.get("quantity", 0))
        price_unit = float(line.get("unit_price", 0))
        discount_ln = float(line.get("discount_amount", 0))
        subtotal_ln = float(line.get("subtotal_line", 0) or ((qty * price_unit) - discount_ln))
        tax_ln = float(line.get("tax_amount", 0))
        total_ln = float(line.get("total_line", 0) or (subtotal_ln + tax_ln))
        
        ln_uuid = str(uuid.uuid4()).replace('-', '')[:8].upper()
        
        obs_parts = []
        if supply_id == "UNKNOWN":
            obs_parts.append("⚠️ Artículo no existe en catálogo, debe registrarse.")
        
        hint_str = f"HINT:{product_hint}|BRAND:{line.get('brand') or ''}|MODEL:{line.get('model') or ''}"
        if product_hint or line.get('brand') or line.get('model'):
            obs_parts.append(hint_str)
            
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
            dlObservations=" ".join(obs_parts) if obs_parts else None
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
            
        # Si sigue siendo desconocido, evitamos crearlo automáticamente
        if final_supply_id == "UNKNOWN" or not final_supply_id:
            logger.warning(f"Línea ignorada por artículo faltante (no se auto-creará): {ln.dlDescription}")
            continue # Saltamos la creación de inventario para esta línea

            
        # 4. Generar Movimientos y Precios
        now = get_now_ca()
        mv_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_movement = IcMovement(
            MovementID=mv_id,
            DatabaseID=database_id,
            OriginID=(doc.IssuerID or "")[:10],
            ProjectID=(project_id or "")[:10] or None,
            ItemID=(final_supply_id or "")[:10],
            DocumentLnID=ln.DocumentLnID,
            mvDate=doc.doDate or now,
            mvAction="IN",        
            mvQuantity=ln.dlQuantity,
            mvStatus="POSTED",
            mvNotes=f"Fulfillment fact {doc.doConsecutive}",
            mvCreatedby="AI_BOT",
            mvCreateddate=now
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
            prCreatedby="AI_BOT",
            prCreateddate=now,
            prModifiedby="AI_BOT",
            prModifieddate=now
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
        company_obj.cpModifiedAt = get_now_ca()

    company_obj.DatabaseID = (database_id or "")[:50]
    company_obj.cpFile = (source_file_id or "")[:255]
    
    # Extraer valores y fallbacks
    raw_name = str(data.get("cpName") or "").strip()
    raw_title = str(data.get("cpTitle") or "").strip()
    
    # Regex fallback: si cpName contiene paréntesis, intentar separar
    if "(" in raw_name and ")" in raw_name:
        match = re.search(r"^(.*?)\s*\((.*?)\)", raw_name)
        if match:
            extracted_legal = match.group(1).strip()
            extracted_fantasy = match.group(2).strip()
            if extracted_legal:
                raw_name = extracted_legal
            if extracted_fantasy and not raw_title:
                raw_title = extracted_fantasy

    # Fallback: si falta uno, usar el otro
    final_name = raw_name or raw_title
    final_title = raw_title or raw_name or raw_title
    
    if final_name:
        company_obj.cpName = final_name[:200]
    if final_title:
        company_obj.cpTitle = final_title[:150]
        
    if data.get("cpCategory"):
        company_obj.cpCategory = str(data.get("cpCategory"))[:100]
    if data.get("cpIdentification"):
        company_obj.cpIdentification = str(data.get("cpIdentification"))[:100]
    if data.get("cpAddress"):
        company_obj.cpAddress = str(data.get("cpAddress"))[:500]
    if data.get("cpEmail"):
        company_obj.cpEmail = str(data.get("cpEmail"))[:150]
    if data.get("cpPhone"):
        company_obj.cpPhone = str(data.get("cpPhone"))[:100]
        
    company_obj.cpBot = "Procesado c/IA"
    
    now = get_now_ca()
    if is_new:
        company_obj.cpCreatedAt = now
    else:
        company_obj.cpModifiedby = "AI_BOT"
        company_obj.cpModifiedAt = now
        
    db.commit()
    
    action = "inserted" if is_new else "updated"
    return {
        "status": "success",
        "action": action,
        "company_id": company_obj.CompanyID,
        "company_name": company_obj.cpName,
        "database_id": database_id
    }

def upsert_brand_logic(db: Session, brand_name: str, database_id: str):
    """Busca una marca por nombre, si no existe la crea."""
    if not brand_name:
        return None
        
    brand_name = brand_name.strip()
    database_id = (database_id or "")[:150]
    # Buscar por título exacto (case insensitive)
    brand_obj = db.query(BcBrand).filter(
        func.lower(BcBrand.brTitle) == brand_name.lower(),
        BcBrand.DatabaseID == database_id,
        BcBrand.isDeleted.isnot(True)
    ).first()
    
    if brand_obj:
        return brand_obj.BrandID
        
    # Si no existe, crear con datos mínimos
    brand_id = str(uuid.uuid4())[:10].upper()
    new_brand = BcBrand(
        BrandID=brand_id,
        DatabaseID=database_id,
        brTitle=brand_name,
        isDeleted=False
    )
    db.add(new_brand)
    db.commit()
    logger.info(f"Nueva Marca Creada: {brand_id} ({brand_name})")
    return brand_id

def create_item_from_url_logic(db: Session, data: dict, image_url: str, database_id: str, image_folder_id: str = None, item_id: str = None):
    """
    Creates or updates a BcItem (product master) from data extracted from a product URL.
    - If item_id is provided, updates that specific record (AppSheet pre-created row).
    - Otherwise, checks by title to avoid duplicates.
    - Upserts brand.
    - Spawns a background thread to download and upload the product image.
    Returns a dict with status and item details.
    """
    database_id = (database_id or "")[:10]
    
    it_title = str(data.get("itTitle") or "").strip()[:300]
    it_brand_name = str(data.get("itBrand") or "").strip()
    it_category = str(data.get("itCategory") or "").strip()[:45]
    it_subcategory = str(data.get("itSubcategory") or "").strip()[:45]
    it_model = str(data.get("itModel") or "").strip()[:45]
    it_description = str(data.get("itDescription") or "").strip()
    it_observations = str(data.get("itObservations") or "").strip()
    
    now = get_now_ca()

    if not it_title:
        return {"status": "error", "reason": "No se pudo extraer el nombre del producto"}
    
    # 1. Upsert de marca
    brand_id = None
    if it_brand_name:
        brand_id = upsert_brand_logic(db, it_brand_name, database_id)
    
    # 2. Buscar maestro: primero por item_id provisto (fila pre-creada por AppSheet),
    # luego por título exacto para evitar duplicados
    existing = None
    if item_id:
        existing = db.query(BcItem).filter(BcItem.ItemID == item_id).first()
    
    if not existing:
        existing = db.query(BcItem).filter(
            BcItem.itTitle == it_title,
            BcItem.DatabaseID == database_id,
            BcItem.isDeleted.isnot(True)
        ).first()
    
    action = "found"
    if existing:
        item_id = existing.ItemID
        logger.info(f"Maestro existente encontrado: {item_id} ({it_title})")
        # Siempre actualizamos itTitle e itStatus (la fila de AppSheet viene vacía)
        if it_title:
            existing.itTitle = it_title
        existing.itStatus = True
        # Auditoría
        existing.itModifiedBy = "AI_BOT"
        existing.itModifiedAt = now
        # Completar los demás campos si estaban vacíos
        if brand_id and not existing.itBrand:
            existing.itBrand = brand_id
        if it_description and not existing.itDescription:
            existing.itDescription = it_description
        if it_category and not existing.itCategory:
            existing.itCategory = it_category
        if it_subcategory and not existing.itSubcategory:
            existing.itSubcategory = it_subcategory
        if it_model and not existing.itModel:
            existing.itModel = it_model
        if it_observations and not existing.itObservations:
            existing.itObservations = it_observations
        if data.get("itWebsite") and not existing.itWebsite:
            existing.itWebsite = str(data.get("itWebsite"))[:500]
        # itImage: asignar la URL de la imagen de inmediato para que AppSheet la vea al instante.
        # El hilo de background la actualizará con el nombre del archivo Drive cuando termine.
        if image_url and not existing.itImage:
            existing.itImage = image_url[:255]
        db.commit()
    else:
        item_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_item = BcItem(
            ItemID=item_id,
            DatabaseID=database_id,
            itTitle=it_title,
            itDescription=it_description or "",
            itBrand=brand_id,
            itCategory=it_category or "",
            itSubcategory=it_subcategory or "",
            itModel=it_model or "",
            itWebsite=str(data.get("itWebsite"))[:500] if data.get("itWebsite") else "",
            itObservations=it_observations or "",
            CabysID="",
            itStatus=True,
            # itImage: URL directa como placeholder inmediato; el hilo de background
            # la reemplaza con el nombre del archivo Drive cuando termina la subida.
            itImage=image_url[:255] if image_url else None,
            itCreatedBy="AI_BOT",
            itCreatedAt=now,
            itModifiedBy="AI_BOT",
            itModifiedAt=now,
            Bot="Importado desde URL"
        )
        db.add(new_item)
        db.commit()
        action = "inserted"
        logger.info(f"Nuevo Maestro Creado desde URL: {item_id} ({it_title})")

    # 3. Asegurar la Variante (BcItemLn) para esta presentación específica
    # Si tenemos it_model (e.g. '700ml'), creamos una variante que lo contenga.
    ln_title = f"{it_title} {it_model}".strip() if it_model else it_title
    existing_ln = db.query(BcItemLn).filter(
        BcItemLn.ItemID == item_id,
        BcItemLn.lnTitle == ln_title[:150],
        BcItemLn.DatabaseID == database_id
    ).first()
    
    if not existing_ln:
        ln_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_ln = BcItemLn(
            ItemLnID=ln_id,
            ItemID=item_id,
            DatabaseID=database_id,
            lnCode=ln_id,
            lnTitle=ln_title[:150],
            lnSize=it_model[:100] if it_model else "",
            lnBarcode="",
            lnSpecs="",
            UnitID="UND",
            inCertification="",
            lnWeight="",
            lnQuantity=0,
            lnAvailable=0,
            lnFeatures="",
            lnObservations="",
            lnStatus=True,
            lnCreatedBy="AI_BOT",
            lnCreatedAt=now,
            lnModifiedBy="AI_BOT",
            lnModifiedAt=now
        )
        db.add(new_ln)
        db.commit()
        logger.info(f"Nueva Variante Creada: {ln_id} ({ln_title})")
    else:
        # Actualizar variante existente si fuera necesario
        existing_ln.lnModifiedBy = "AI_BOT"
        existing_ln.lnModifiedAt = now
        if not existing_ln.lnSize and it_model:
            existing_ln.lnSize = it_model[:100]
        db.commit()

    # 4. Descargar y subir imagen en background
    if image_url and image_folder_id:
        img_filename = f"{item_id}.jpg"
        
        def upload_scraped_image(img_url, filename, folder_id, iid):
            from scrape_services import download_image_from_url
            img_bytes, content_type = download_image_from_url(img_url)
            if img_bytes:
                drive_file_id = upload_image_to_drive(img_bytes, filename, content_type, folder_id)
                if drive_file_id:
                    from database import SessionLocal
                    s = SessionLocal()
                    try:
                        item = s.query(BcItem).filter(BcItem.ItemID == iid).first()
                        if item:
                            item.DriveID = drive_file_id
                            item.itImage = filename
                            s.commit()
                            logger.info(f"Imagen URL subida a Drive: {iid} -> {drive_file_id}")
                    finally:
                        s.close()
            else:
                logger.warning(f"No se pudo descargar imagen desde URL: {img_url}")
        
        t = threading.Thread(
            target=upload_scraped_image,
            args=(image_url, img_filename, image_folder_id, item_id),
            daemon=True
        )
        t.start()
    
    return {
        "status": "success",
        "action": action,
        "item_id": item_id,
        "item_title": it_title,
        "brand_id": brand_id,
        "brand_name": it_brand_name or None,
        "database_id": database_id
    }