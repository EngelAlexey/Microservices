from sqlalchemy.orm import Session
from models import BcItem, BcItemLn, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject, DrCompany, BcBrand, IcItemsStock, UtCategory, BcUnit
import difflib
import uuid
from datetime import datetime, timedelta, timezone
import logging
import re
from sqlalchemy import func, text, or_
from drive_services import upload_image_to_drive, get_folder_path_from_drive
from appsheet_services import appsheet_mutate, is_configured as appsheet_is_configured
import threading

logger = logging.getLogger(__name__)


def _appsheet_write_or_db(db: Session, table: str, action: str, row: dict, db_fallback, database_id: str = None):
    """Escribe la fila vía la API de AppSheet (Add/Edit) para que el cambio aparezca sin sync.

    Pasa `UserSettings={"DatabaseID": database_id}` (imprescindible: los filtros de seguridad
    de la app de Bayco se basan en él; sin él AppSheet no ve las filas y Edit/Refs fallan).

    Si AppSheet no está configurado o la llamada falla, cae a la escritura directa en BD
    (`db_fallback`), de modo que el dato nunca se pierde: aparecerá tras una sync manual,
    igual que en el comportamiento original. Devuelve "appsheet" o "db" según el origen.
    """
    if appsheet_is_configured():
        try:
            user_settings = {"DatabaseID": database_id} if database_id else None
            appsheet_mutate(table, action, [row], user_settings=user_settings)
            return "appsheet"
        except Exception as e:
            db.rollback()
            logger.warning(f"AppSheet {action} en {table} falló; cae a escritura directa en BD: {e}")
    db_fallback()
    return "db"

def safe_float(val):
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    
    # Limpiamos letras, símbolos, comas Y espacios en blanco
    cleaned = str(val).replace(',', '').replace('₡', '').replace('¢', '').replace('C', '').replace('c', '').replace('$', '').replace(' ', '').strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def strip_html_tags(text):
    if not text:
        return ""
    clean = re.compile('<.*?>')
    return re.sub(clean, '', str(text)).strip()

def get_now_ca():
    return datetime.now(timezone(timedelta(hours=-6))).replace(tzinfo=None)

def _is_manual_image(v: str) -> bool:
    """True si `itImage` es una imagen subida manualmente vía AppSheet: una ruta relativa de su
    carpeta (p.ej. 'bcItems_Images/…' o 'A00-Kaizen/Items/…'). El bot NO debe pisar estas.
    Los valores del bot —vacío, nombre pelado sin '/', o una URL http(s)— devuelven False."""
    v = (v or "").strip()
    return bool(v) and "/" in v and not v.lower().startswith("http")

def _load_product_catalog(db: Session, database_id: str):
    all_items = db.query(BcItemLn.ItemLnID, BcItemLn.lnCode, BcItemLn.lnTitle, BcItem.itTitle, BcItem.ItemID, BcItem.DriveID)\
                  .join(BcItem, BcItemLn.ItemID == BcItem.ItemID)\
                  .filter(BcItemLn.DatabaseID == database_id)\
                  .filter(BcItemLn.isDeleted.isnot(True))\
                  .filter(BcItem.isDeleted.isnot(True)).all()
    
    sku_map = {}
    choices_map = {}
    parent_map = {}   
    variant_map = {}  
    
    for item in all_items:
        if item.lnCode:
            sku_map[item.lnCode.strip().upper()] = item.ItemLnID
        variant_map[item.ItemLnID] = {"parent_id": item.ItemID, "drive_id": item.DriveID}
        title = item.itTitle or item.lnTitle
        if title and title not in choices_map:
            choices_map[title] = item.ItemLnID
        if item.itTitle and item.itTitle not in parent_map:
            parent_map[item.itTitle] = {"parent_id": item.ItemID, "drive_id": item.DriveID}
            
    return sku_map, choices_map, parent_map, variant_map

def find_product_id(description: str, choices_map: dict, product_hint: str = "", brand_hint: str = "", model_hint: str = ""):
    if not choices_map:
        return "UNKNOWN", "Raw Name", None
    clean_desc = str(description).strip().lower()
    for title, ln_id in choices_map.items():
        if title and str(title).strip().lower() == clean_desc:
            return ln_id, "Exact Name", title
    hints_combined = f"{brand_hint} {product_hint} {model_hint}".strip().lower()
    if hints_combined:
        for title, ln_id in choices_map.items():
            if title and str(title).strip().lower() == hints_combined:
                return ln_id, "Exact Hint", title
    import re
    def get_jaccard(s1, s2):
        w1 = set(re.findall(r'\w+', s1))
        w2 = set(re.findall(r'\w+', s2))
        if not w1 or not w2: return 0.0
        return len(w1.intersection(w2)) / len(w1.union(w2))
    best_score = 0.0
    best_id = None
    best_title = None
    best_source = "None"
    for title, ln_id in choices_map.items():
        if not title: continue
        title_lower = str(title).strip().lower()
        if (clean_desc in title_lower or title_lower in clean_desc) and len(clean_desc) > 10:
            return ln_id, "Substring", title
        score = get_jaccard(clean_desc, title_lower)
        if hints_combined:
            score_hint = get_jaccard(hints_combined, title_lower)
            if score_hint > score: score = score_hint
        if score > best_score:
            best_score = score
            best_id = ln_id
            best_title = title
            best_source = "Jaccard"
    if best_score >= 0.85:
        return best_id, f"{best_source} (High)", best_title
    if best_score >= 0.50:
        return "UNKNOWN", f"{best_source} (Maybe)", best_title
    return "UNKNOWN", "Not Found", None

def _load_project_catalog(db: Session, database_id: str):
    projects = db.query(DrProject.ProjectID, DrProject.pjTitle, DrProject.pjAddress)\
                 .filter(DrProject.DatabaseID == database_id).all()
    project_choices = {}
    for pj in projects:
        key = f"{pj.pjTitle or ''} {pj.pjAddress or ''}".strip()
        if key:
            project_choices[key] = pj.ProjectID
    return project_choices

def find_project_id(address_text: str, project_choices: dict):
    if not address_text or not str(address_text).strip() or not project_choices:
        return None
    keys = list(project_choices.keys())
    matches = difflib.get_close_matches(str(address_text).strip(), keys, n=1, cutoff=0.75)
    if matches:
        return project_choices[matches[0]]
    return None

def insert_document_logic(db: Session, data: dict, source_file_id: str, appsheet_doc_id: str = None, database_id: str = None, drive_id: str = None):
    database_id = (database_id or "")[:10]
    
    # Blindaje contra "null" (None) de la IA
    header = data.get("header") or {}
    lines = data.get("lines") or []
    
    sku_map, choices_map, parent_map, variant_map = _load_product_catalog(db, database_id)
    project_choices = _load_project_catalog(db, database_id)
    
    # Blindaje contra "null" (None) dentro del header
    issuer_data = header.get("issuer") or {}
    receptor_data = header.get("receptor") or {}
    issuer_id = header.get("doIssuerID")
    receptor_id = header.get("doReceptorID")
    if issuer_data and any(issuer_data.values()):
        issuer_data["cpCategory"] = "Supplier" 
        issuer_res = upsert_company_from_invoice_logic(db, issuer_data, source_file_id, database_id, update_if_exists=False)
        if issuer_res.get("status") == "success":
            issuer_id = issuer_res.get("company_id")
    if receptor_data and any(receptor_data.values()):
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
    doc_date_str = header.get("doDate")
    try:
        doc_date = datetime.strptime(doc_date_str, "%Y-%m-%d").date() if doc_date_str else now.date()
    except (ValueError, TypeError):
        logger.warning(f"Fecha de documento inválida '{doc_date_str}'; se usa la fecha actual.")
        doc_date = now.date()
    doc_obj.doCreatedAt = now
    doc_obj.doCreatedBy = "AI_BOT"
    doc_obj.DatabaseID = database_id  
    doc_obj.doDate = doc_date
    doc_obj.doConsecutive = header.get("doConsecutive")
    doc_obj.doType = header.get("doType")
    doc_obj.IssuerID = (issuer_id or "")[:10]
    doc_obj.ReceptorID = (receptor_id or "")[:10]
    doc_obj.doAccount = header.get("doAccount")
    doc_obj.doCredit = header.get("doCredit")
    doc_obj.CurrencyID = header.get("CurrencyID", "CRC")
    doc_obj.doSubtotal = safe_float(header.get("SubtotalAmount", 0.0))
    doc_obj.doTaxes = safe_float(header.get("TaxAmount", 0.0))
    doc_obj.doTotal = safe_float(header.get("TotalAmount", 0.0))
    doc_obj.doFile = source_file_id
    doc_obj.DriveID = str(drive_id or source_file_id).strip()
    num_lines = len(lines)
    issuer_name = issuer_data.get('cpName', 'Desconocido')
    project_info = f". Proyecto: {matched_project_id}" if matched_project_id else ""
    doc_obj.doAIComment = f"Digitalización completa. Emisor: {issuer_name}. Líneas procesadas: {num_lines}{project_info}."
    line_number = 1
    for line in lines:
        manual_desc = str(line.get("description") or "Sin descripción").strip()
        product_hint = str(line.get("product_name") or "").strip()
        brand_hint = str(line.get("brand") or "").strip()
        model_hint = str(line.get("model") or "").strip()
        supply_id, match_source, candidate = find_product_id(manual_desc, choices_map, product_hint, brand_hint, model_hint)
        qty = safe_float(line.get("quantity", 0))
        price_unit = safe_float(line.get("unit_price", 0))
        discount_ln = safe_float(line.get("discount_amount", 0))
        subtotal_raw = safe_float(line.get("subtotal_line", 0))
        subtotal_ln = subtotal_raw if subtotal_raw else ((qty * price_unit) - discount_ln)
        tax_ln = safe_float(line.get("tax_amount", 0))
        total_raw = safe_float(line.get("total_line", 0))
        total_ln = total_raw if total_raw else (subtotal_ln + tax_ln)
        ln_uuid = str(uuid.uuid4()).replace('-', '')[:8].upper()
        obs_parts = []
        if supply_id == "UNKNOWN":
            if candidate:
                obs_parts.append(f"Posible coincidencia: {candidate}")
            else:
                obs_parts.append("Articulo no encontrado en catalogo.")
        hint_str = f"HINT:{product_hint}|BRAND:{line.get('brand') or ''}|MODEL:{line.get('model') or ''}"
        if product_hint or line.get('brand') or line.get('model'):
            obs_parts.append(hint_str)
        new_ln = FnDocumentLn(
            DocumentLnID=ln_uuid, DocumentID=doc_obj.DocumentID, DatabaseID=database_id,
            dlNumber=line_number, SupplyID=supply_id if supply_id != "UNKNOWN" else None,
            CabysID=str(line.get("cabys_candidate") or "")[:50], dlDescription=manual_desc,
            dlQuantity=qty, dlUnitPrice=price_unit, dlDiscount=discount_ln,
            dlSubtotal=subtotal_ln, dlTaxes=tax_ln, dlTotal=total_ln,
            dlObservations=" ".join(obs_parts) if obs_parts else None,
            OriginID=(str(line.get("origin_id") or "")[:10]) or None,
            DestinationID=(str(line.get("destination_id") or "")[:10]) or None,
        )
        db.add(new_ln)
        line_number += 1
    db.commit()
    return {"status": "success", "document_id": doc_obj.DocumentID}

def _determine_action(doc_account: str, doc_type: str) -> str:
    acc = (doc_account or "").upper()
    dtype = (doc_type or "").upper()
    
    if acc == "CXC":
        # Sale
        if dtype == "NC":
            return "IN"  # Customer return
        return "OUT"     # Standard sale
    
    if acc == "CXP":
        # Purchase
        if dtype == "NC":
            return "OUT" # Return to supplier
        return "IN"      # Standard purchase
        
    return "IN"

def _make_movement(db: Session, *, mv_id: str, database_id: str, item_id: str,
                   doc_ln_id: str, mv_date, action: str, quantity: float,
                   origin_id: str, project_id: str, notes: str, now,
                   unit_cost: float = 0, unit_tax: float = 0, total_cost: float = 0):
    m = IcMovement(
        MovementID=mv_id, DatabaseID=database_id, OriginID=(origin_id or "")[:10] or None,
        ProjectID=(project_id or "")[:10] or None, ItemID=(item_id or "")[:10],
        DocumentLnID=doc_ln_id, mvDate=now,
        mvAction=action, mvQuantity=quantity,
        mvUnitCost=unit_cost, mvTax=unit_tax, mvTotalCost=total_cost,
        mvStatus="POSTED", mvNotes=notes,
        mvCreatedby="AI_BOT", mvCreateddate=now,
        mvModifiedby="AI_BOT", mvModifieddate=now
    )
    db.add(m)
    return m

def apply_valuation_bucket_logic(db: Session, db_id: str, item_id: str, loc_id: str, q_delta: float,
                                 row_action: str, mv_id: str, supply_id: str, price: float, user: str, now,
                                 unit_tax: float = 0):
    """ icItemsPrices: Replicando logica de AppSheet (ISBLANK / TOP 1 FILTER LIST) """
    if not loc_id: return
    loc_id = str(loc_id).strip()

    inward_titles = ["IN", "Reserved IN", "Transfer", "Reserved Transfer"]
    outward_titles = ["OUT", "Reserved OUT"]

    is_outward = row_action in outward_titles

    if is_outward:
        # Se resta por el tipo (Busca el TOP 1 origin "IN" y resta)
        rec_in = db.query(IcPrice).filter(
            IcPrice.ItemID == item_id,
            IcPrice.ProjectID == loc_id,
            IcPrice.prTitle.in_(inward_titles),
            IcPrice.isDeleted == False
        ).first()
        if rec_in:
            rec_in.prQuantity = float(rec_in.prQuantity or 0) - float(q_delta)
            _unit_price = float(rec_in.prPrice or price)
            _unit_tax = float(rec_in.prTax or unit_tax)
            rec_in.prTotal = float(rec_in.prQuantity) * (_unit_price + _unit_tax)
            rec_in.prModifiedby = user
            rec_in.prModifieddate = now

    # ISBLANK / TOP 1 para sumar o crear fila de la action respectiva
    search_list = outward_titles if is_outward else inward_titles

    rec_bucket = db.query(IcPrice).filter(
        IcPrice.ItemID == item_id,
        IcPrice.ProjectID == loc_id,
        IcPrice.prTitle.in_(search_list),
        IcPrice.isDeleted == False
    ).first()

    if rec_bucket:
        rec_bucket.prQuantity = float(rec_bucket.prQuantity or 0) + float(q_delta)
        _unit_price = float(rec_bucket.prPrice or price)
        _unit_tax = float(rec_bucket.prTax or unit_tax)
        rec_bucket.prTotal = float(rec_bucket.prQuantity) * (_unit_price + _unit_tax)
        rec_bucket.prModifiedby = user
        rec_bucket.prModifieddate = now
    else:
        db.add(IcPrice(
            PriceID=str(uuid.uuid4()).replace('-', '')[:10].upper(),
            DatabaseID=db_id, ItemID=(item_id or "")[:10], ProjectID=(loc_id or "")[:10] or None,
            MovementID=mv_id, SupplyID=supply_id,
            prTitle=row_action, prDescription=f"Micro: {row_action}",
            prQuantity=q_delta, prPrice=price, prTax=unit_tax,
            prTotal=float(q_delta) * (float(price) + float(unit_tax)),
            prCreatedby=user, prCreateddate=now,
            prModifiedby=user, prModifieddate=now, isDeleted=False
        ))
def create_inventory_movements_logic(db: Session, document_id: str, database_id: str,
                                     image_folder_id: str = None, project_id: str = None):
    doc = db.query(FnDocument).filter(FnDocument.DocumentID == document_id).first()
    if not doc:
        return {"error": "Documento no encontrado"}
    
    sku_map, choices_map, parent_map, variant_map = _load_product_catalog(db, database_id)
    lines = db.query(FnDocumentLn).filter(FnDocumentLn.DocumentID == document_id).all()
    created_count = 0
    for ln in lines:
        final_supply_id = ln.SupplyID
        if not final_supply_id or final_supply_id == "UNKNOWN":
            final_supply_id, _, _ = find_product_id(ln.dlDescription, choices_map)
        if final_supply_id == "UNKNOWN" or not final_supply_id:
            continue
        now = get_now_ca()
        qty = float(ln.dlQuantity or 0)
        if qty == 0:
            continue
        net_unit_price = float(ln.dlSubtotal or 0) / qty  # precio neto unitario (con descuento)
        unit_tax = float(ln.dlTaxes or 0) / qty           # impuesto unitario
        total_cost = float(ln.dlTotal or 0)               # total de la línea
        notes_base = f"Doc {doc.doConsecutive or document_id}"

        # Determine basic action
        action = _determine_action(doc.doAccount, doc.doType)

        if ln.OriginID and ln.DestinationID:
            out_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            m_out = _make_movement(db, mv_id=out_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action="OUT", quantity=qty, origin_id=ln.OriginID, project_id=ln.DestinationID,
                           notes=f"{notes_base} – Traslado salida", now=now,
                           unit_cost=net_unit_price, unit_tax=unit_tax, total_cost=total_cost)
            _perform_inventory_update(db, m_out, database_id, final_supply_id, ln.SupplyID, net_unit_price, "AI_BOT", now, unit_tax)

            in_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            m_in = _make_movement(db, mv_id=in_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action="IN", quantity=qty, origin_id=ln.OriginID, project_id=ln.DestinationID,
                           notes=f"{notes_base} – Traslado entrada", now=now,
                           unit_cost=net_unit_price, unit_tax=unit_tax, total_cost=total_cost)
            _perform_inventory_update(db, m_in, database_id, final_supply_id, ln.SupplyID, net_unit_price, "AI_BOT", now, unit_tax)
            created_count += 2
        else:
            mv_id = str(uuid.uuid4()).replace('-', '')[:8].upper()

            if action == "OUT":
                origin_val = (project_id or "")[:10] or None
                dest_val = None
            else:
                origin_val = None
                dest_val = (project_id or "")[:10] or None

            m = _make_movement(db, mv_id=mv_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action=action, quantity=qty, origin_id=origin_val, project_id=dest_val,
                           notes=notes_base, now=now,
                           unit_cost=net_unit_price, unit_tax=unit_tax, total_cost=total_cost)

            _perform_inventory_update(db, m, database_id, final_supply_id, ln.SupplyID, net_unit_price, "AI_BOT", now, unit_tax)
            created_count += 1
    db.commit()
    return {"status": "success", "movements_created": created_count}

def upsert_company_from_invoice_logic(db: Session, data: dict, source_file_id: str, database_id: str = None, target_company_id: str = None, update_if_exists: bool = True):
    cp_identification = str(data.get("cpIdentification", "")).strip()
    cp_name = str(data.get("cpName") or "").strip()
    if not cp_name and not cp_identification:
        return {"status": "skipped", "reason": "No name or ID provided"}
    company_obj = None
    if target_company_id:
        company_obj = db.query(DrCompany).filter(DrCompany.CompanyID == target_company_id, DrCompany.DatabaseID == database_id).first()
    if not company_obj and cp_identification:
        company_obj = db.query(DrCompany).filter(DrCompany.cpIdentification == cp_identification, DrCompany.DatabaseID == database_id).first()
    if not company_obj and cp_name:
        company_obj = db.query(DrCompany).filter(DrCompany.cpName == cp_name, DrCompany.DatabaseID == database_id).first()
    is_new = False
    if not company_obj:
        company_id = str(uuid.uuid4())[:8].upper()
        company_obj = DrCompany(CompanyID=company_id)
        company_obj.cpCreatedBy = "AI_BOT"
        db.add(company_obj)
        is_new = True
    else:
        if not update_if_exists:
            return {"status": "success", "action": "found", "company_id": company_obj.CompanyID, "company_name": company_obj.cpName, "database_id": database_id}
        company_obj.cpModifiedby = "AI_BOT"
        company_obj.cpModifiedAt = get_now_ca()
    company_obj.DatabaseID = (database_id or "")[:50]
    company_obj.cpFile = (source_file_id or "")[:255]
    raw_name = str(data.get("cpName") or "").strip()
    raw_title = str(data.get("cpTitle") or "").strip()
    if "(" in raw_name and ")" in raw_name:
        match = re.search(r"^(.*?)\s*\((.*?)\)", raw_name)
        if match:
            extracted_legal = match.group(1).strip()
            extracted_fantasy = match.group(2).strip()
            if extracted_legal: raw_name = extracted_legal
            if extracted_fantasy and not raw_title: raw_title = extracted_fantasy
    final_name = raw_name or raw_title
    final_title = raw_title or raw_name
    if final_name: company_obj.cpName = final_name[:200]
    if final_title: company_obj.cpTitle = final_title[:150]
    if data.get("cpCategory"): company_obj.cpCategory = str(data.get("cpCategory"))[:100]
    if data.get("cpIdentification"): company_obj.cpIdentification = str(data.get("cpIdentification"))[:100]
    if data.get("cpAddress"): company_obj.cpAddress = str(data.get("cpAddress"))[:500]
    if data.get("cpEmail"): company_obj.cpEmail = str(data.get("cpEmail"))[:150]
    if data.get("cpPhone"): company_obj.cpPhone = str(data.get("cpPhone"))[:100]
    now = get_now_ca()
    if is_new: company_obj.cpCreatedAt = now
    else:
        company_obj.cpModifiedby = "AI_BOT"
        company_obj.cpModifiedAt = now
    db.commit()
    action = "inserted" if is_new else "updated"
    return {"status": "success", "action": action, "company_id": company_obj.CompanyID, "company_name": company_obj.cpName, "database_id": database_id}

def upsert_brand_logic(db: Session, brand_name: str, database_id: str):
    if not brand_name: return None
    brand_name = brand_name.strip()
    database_id = (database_id or "")[:150]
    brand_obj = db.query(BcBrand).filter(func.lower(BcBrand.brTitle) == brand_name.lower(), BcBrand.DatabaseID == database_id, BcBrand.isDeleted.isnot(True)).first()
    if brand_obj: return brand_obj.BrandID
    brand_id = str(uuid.uuid4())[:10].upper()
    new_brand = BcBrand(BrandID=brand_id, DatabaseID=database_id, brTitle=brand_name, isDeleted=False)
    db.add(new_brand)
    db.commit()
    return brand_id

def upsert_category_logic(db: Session, category_name: str, database_id: str):
    """Resuelve un nombre de categoría a un CategoryID de utCategories (crea si no existe).
    Espeja a upsert_brand_logic: itCategory en bcItems guarda el ID, no el texto."""
    if not category_name:
        return None
    category_name = str(category_name).strip()
    if not category_name:
        return None
    database_id = (database_id or "")[:10]
    cat_obj = db.query(UtCategory).filter(
        func.lower(UtCategory.ctTitle) == category_name.lower(),
        UtCategory.DatabaseID == database_id,
        UtCategory.isDeleted.isnot(True)
    ).first()
    if cat_obj:
        return cat_obj.CategoryID
    now = get_now_ca()
    category_id = str(uuid.uuid4()).replace('-', '')[:8]
    db.add(UtCategory(
        CategoryID=category_id, DatabaseID=database_id, ctTitle=category_name[:150],
        ctCreatedby="AI_BOT", ctCreateddate=now, ctModifiedby="AI_BOT", ctModifieddate=now,
        isDeleted=False
    ))
    db.commit()
    return category_id

def upsert_unit_logic(db: Session, unit_name: str, unit_symbol: str, database_id: str):
    """Resuelve una unidad de medida a un UnitID de bcUnits (crea si no existe).
    Hace match por unTitle o unSymbol (case-insensitive) y, si no encuentra, crea la unidad."""
    unit_name = str(unit_name or "").strip()
    unit_symbol = str(unit_symbol or "").strip()
    if not unit_name and not unit_symbol:
        return None
    database_id = (database_id or "")[:150]
    candidates = {v.lower() for v in (unit_name, unit_symbol) if v}
    units = db.query(BcUnit).filter(
        BcUnit.DatabaseID == database_id,
        BcUnit.isDeleted.isnot(True)
    ).all()
    for u in units:
        title = (u.unTitle or "").strip().lower()
        symbol = (u.unSymbol or "").strip().lower()
        if (title and title in candidates) or (symbol and symbol in candidates):
            return u.UnitID
    unit_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
    db.add(BcUnit(
        UnitID=unit_id, DatabaseID=database_id,
        unTitle=(unit_name or unit_symbol)[:300], unSymbol=(unit_symbol or unit_name)[:300],
        isDeleted=False
    ))
    db.commit()
    return unit_id

def create_item_from_url_logic(db: Session, data: dict, image_url: str, database_id: str, image_folder_id: str = None, item_id: str = None, barcode: str = None):
    database_id = (database_id or "")[:10]
    it_title = str(data.get("itTitle") or "").strip()[:300]
    it_brand_name = str(data.get("itBrand") or "").strip()
    it_category_name = str(data.get("itCategory") or "").strip()
    it_subcategory = str(data.get("itSubcategory") or "").strip()[:45]
    it_model = str(data.get("itModel") or "").strip()[:45]
    it_size = str(data.get("itSize") or "").strip()[:100]
    it_unit_name = str(data.get("itUnit") or "").strip()
    it_unit_symbol = str(data.get("itUnitSymbol") or "").strip()
    it_description = strip_html_tags(data.get("itDescription"))
    it_observations = strip_html_tags(data.get("itObservations"))
    it_cabys = str(data.get("cabys") or data.get("CabysID") or "").strip()[:50]
    now = get_now_ca()
    if not it_title: return {"status": "error", "reason": "No se pudo extraer el nombre del producto"}
    brand_id = None
    if it_brand_name: brand_id = upsert_brand_logic(db, it_brand_name, database_id)
    # itCategory guarda el CategoryID (referencia a utCategories), no el texto libre.
    # Tolerante a permisos: si el usuario de BD no puede leer/escribir utCategories (p.ej. bayco_api
    # en producción), se omite el campo en vez de abortar la creación del ítem.
    category_id = None
    if it_category_name:
        try:
            category_id = upsert_category_logic(db, it_category_name, database_id)
        except Exception as e:
            db.rollback()
            logger.warning(f"No se pudo resolver itCategory (¿permisos en utCategories?): {e}")
    # itUnit/itUnitSymbol se resuelven a un UnitID de bcUnits (crea la unidad si no existe).
    unit_id = None
    if it_unit_name or it_unit_symbol:
        try:
            unit_id = upsert_unit_logic(db, it_unit_name, it_unit_symbol, database_id)
        except Exception as e:
            db.rollback()
            logger.warning(f"No se pudo resolver UnitID (¿permisos en bcUnits?): {e}")
    existing = None
    if item_id: existing = db.query(BcItem).filter(BcItem.ItemID == item_id).first()
    if not existing:
        existing = db.query(BcItem).filter(BcItem.itTitle == it_title, BcItem.DatabaseID == database_id, BcItem.isDeleted.isnot(True)).first()
    action = "found"
    if existing:
        item_id = existing.ItemID
        # Upsert parcial: solo rellenamos campos vacíos (igual que antes). Calculamos el set de
        # cambios y lo aplicamos vía AppSheet (Edit); el fallback a BD los escribe en el ORM.
        changes = {"itStatus": True, "itModifiedBy": "AI_BOT", "itModifiedAt": now}
        if it_title: changes["itTitle"] = it_title
        if brand_id and not existing.itBrand: changes["itBrand"] = brand_id
        if it_description and not existing.itDescription: changes["itDescription"] = it_description
        if category_id and not existing.itCategory: changes["itCategory"] = category_id
        if it_subcategory and not existing.itSubcategory: changes["itSubcategory"] = it_subcategory
        if it_model and not existing.itModel: changes["itModel"] = it_model
        if it_size and not existing.itSize: changes["itSize"] = it_size
        if unit_id and not existing.UnitID: changes["UnitID"] = unit_id
        if it_observations and not existing.itObservations: changes["itObservations"] = it_observations
        if data.get("itWebsite") and not existing.itWebsite: changes["itWebsite"] = str(data.get("itWebsite"))[:500]
        if it_cabys and not existing.CabysID: changes["CabysID"] = it_cabys
        if image_url and not existing.itImage: changes["itImage"] = image_url[:255]
        def _db_update_item():
            for k, v in changes.items(): setattr(existing, k, v)
            db.commit()
        _appsheet_write_or_db(db, "bcItems", "Edit", {"ItemID": item_id, **changes}, _db_update_item, database_id)
    else:
        item_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        it_website = str(data.get("itWebsite"))[:500] if data.get("itWebsite") else ""
        item_row = {
            "ItemID": item_id, "DatabaseID": database_id, "itTitle": it_title,
            "itDescription": it_description or "", "itBrand": brand_id or "", "itCategory": category_id or "",
            "itSubcategory": it_subcategory or "", "itModel": it_model or "", "itSize": it_size or "",
            "UnitID": unit_id or "", "itCatalog": False, "itWebsite": it_website,
            "itObservations": it_observations or "", "CabysID": it_cabys, "itStatus": True,
            "itImage": image_url[:255] if image_url else "",
            "itCreatedBy": "AI_BOT", "itCreatedAt": now, "itModifiedBy": "AI_BOT", "itModifiedAt": now,
            "Bot": "Importado desde URL",
        }
        def _db_add_item():
            new_item = BcItem(
                ItemID=item_id, DatabaseID=database_id, itTitle=it_title, itDescription=it_description or "",
                itBrand=brand_id, itCategory=category_id or "", itSubcategory=it_subcategory or "", itModel=it_model or "",
                itSize=it_size or "", UnitID=unit_id, itCatalog=False,
                itWebsite=it_website, itObservations=it_observations or "",
                CabysID=it_cabys, itStatus=True, itImage=image_url[:255] if image_url else None,
                itCreatedBy="AI_BOT", itCreatedAt=now, itModifiedBy="AI_BOT", itModifiedAt=now, Bot="Importado desde URL"
            )
            db.add(new_item)
            db.commit()
        _appsheet_write_or_db(db, "bcItems", "Add", item_row, _db_add_item, database_id)
        action = "inserted"
    ln_title = f"{it_title} {it_model}".strip() if it_model else it_title
    existing_ln = db.query(BcItemLn).filter(BcItemLn.ItemID == item_id, BcItemLn.lnTitle == ln_title[:150], BcItemLn.DatabaseID == database_id).first()
    if not existing_ln:
        ln_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        ln_row = {
            "ItemLnID": ln_id, "ItemID": item_id, "DatabaseID": database_id, "lnCode": ln_id,
            "lnTitle": ln_title[:150], "lnSpecs": it_model[:100] if it_model else "",
            "lnBarcode": (barcode or "")[:50], "lnPresentation": "", "UnitID": unit_id or "UND",
            "inCertification": "", "lnWeight": "", "lnQuantity": 0, "lnAvailable": 0,
            "lnFeatures": brand_id or "", "lnObservations": "", "lnStatus": True,
            "lnCreatedBy": "AI_BOT", "lnCreatedAt": now, "lnModifiedBy": "AI_BOT", "lnModifiedAt": now, "Bot": "ADDED",
        }
        def _db_add_ln():
            new_ln = BcItemLn(
                ItemLnID=ln_id, ItemID=item_id, DatabaseID=database_id, lnCode=ln_id, lnTitle=ln_title[:150],
                lnSpecs=it_model[:100] if it_model else "", lnBarcode=(barcode or "")[:50], lnPresentation="", UnitID=unit_id or "UND", inCertification="",
                lnWeight="", lnQuantity=0, lnAvailable=0, lnFeatures=brand_id or "", lnObservations="", lnStatus=True,
                lnCreatedBy="AI_BOT", lnCreatedAt=now, lnModifiedBy="AI_BOT", lnModifiedAt=now, Bot="ADDED"
            )
            db.add(new_ln)
            db.commit()
        _appsheet_write_or_db(db, "bcItemsLns", "Add", ln_row, _db_add_ln, database_id)
    else:
        ln_changes = {"lnModifiedBy": "AI_BOT", "lnModifiedAt": now}
        if not existing_ln.lnSpecs and it_model: ln_changes["lnSpecs"] = it_model[:100]
        if barcode and not existing_ln.lnBarcode: ln_changes["lnBarcode"] = barcode[:50]
        if unit_id and (not existing_ln.UnitID or existing_ln.UnitID == "UND"): ln_changes["UnitID"] = unit_id
        if brand_id: ln_changes["lnFeatures"] = brand_id
        def _db_update_ln():
            for k, v in ln_changes.items(): setattr(existing_ln, k, v)
            db.commit()
        _appsheet_write_or_db(db, "bcItemsLns", "Edit", {"ItemLnID": existing_ln.ItemLnID, **ln_changes}, _db_update_ln, database_id)
    if image_url and image_folder_id:
        # AppSheet renderiza una columna de imagen cuyo valor es una URL pública. El bot sube la
        # imagen scrapeada a Drive (permiso anyone:reader) y guarda en `itImage` la URL thumbnail
        # del archivo, que AppSheet muestra directo —sin columna virtual ni carpeta nativa. (La SA
        # no puede escribir en la carpeta de la app, que vive en "Mi unidad"; ver memoria
        # drive-auth-methods. Y la AppSheet API no ingiere la imagen desde la URL: solo guarda el
        # string —probado—, pero como la columna es de tipo imagen, la URL basta para que renderice.)
        # Antes se guardaba el nombre pelado `<ItemID>.jpg`, que AppSheet no resolvía por ruta.
        # Se escribe vía AppSheet API (Edit) con fallback a BD, igual que el resto del catálogo.
        img_filename = f"{item_id}.jpg"
        def upload_scraped_image(img_url, filename, folder_id, iid, db_id):
            from scrape_services import download_image_from_url
            from database import SessionLocal
            s = SessionLocal()
            try:
                item = s.query(BcItem).filter(BcItem.ItemID == iid).first()
                # Guard no-destructivo: no pisar una imagen subida manualmente (ruta de AppSheet).
                # Se chequea ANTES de descargar/subir para no gastar Drive en vano.
                if item is None or _is_manual_image(item.itImage):
                    return
                img_bytes, content_type = download_image_from_url(img_url)
                if not img_bytes:
                    return
                drive_file_id = upload_image_to_drive(img_bytes, filename, content_type, folder_id)
                if not drive_file_id:
                    return
                it_url = f"https://drive.google.com/thumbnail?authuser=0&sz=w1024&id={drive_file_id}"
                def _db_update():
                    item.itImage = it_url
                    item.DriveID = drive_file_id
                    s.commit()
                _appsheet_write_or_db(s, "bcItems", "Edit", {"ItemID": iid, "itImage": it_url, "DriveID": drive_file_id}, _db_update, db_id)
            finally:
                s.close()
        t = threading.Thread(target=upload_scraped_image, args=(image_url, img_filename, image_folder_id, item_id, database_id), daemon=True)
        t.start()
    return {"status": "success", "action": action, "item_id": item_id, "item_title": it_title, "brand_id": brand_id, "brand_name": it_brand_name or None, "database_id": database_id}

def _perform_inventory_update(db: Session, mv_obj: IcMovement, db_id: str, item_id: str, supply_id: str, price: float, user: str, now,
                              unit_tax: float = 0):
    """ Single source of truth for inventory changes. Marks movement as POSTED. """
    action = mv_obj.mvAction
    qty = float(mv_obj.mvQuantity or 0)
    origin_id = mv_obj.OriginID
    dest_id = mv_obj.ProjectID
    mv_id = mv_obj.MovementID

    def update_net_stock(loc_id, q_delta):
        if not loc_id: return
        loc_id = loc_id.strip()
        rec = db.query(IcItemsStock).filter(IcItemsStock.ItemID == item_id, IcItemsStock.ProjectID == loc_id).first()
        if rec:
            rec.stQuantity = float(rec.stQuantity or 0) + float(q_delta)
            movs = str(rec.stMovements or "")
            if mv_id not in movs: rec.stMovements = "{} , {}".format(movs, mv_id)
            rec.stModifiedBy = user
            rec.stModifiedAt = now
            rec.isDeleted = False
        else:
            db.add(IcItemsStock(
                StockID=str(uuid.uuid4()).replace('-', '')[:10].upper(),
                DatabaseID=db_id, ItemID=item_id, ProjectID=loc_id,
                stQuantity=q_delta, stMovements=mv_id,
                stCreatedBy=user, stCreatedAt=now,
                stModifiedBy=user, stModifiedAt=now, isDeleted=False
            ))

    def update_global(q_delta):
        variant = db.query(BcItemLn).filter(BcItemLn.ItemLnID == item_id).first()
        if variant:
            variant.lnQuantity = float(variant.lnQuantity or 0) + float(q_delta)
            variant.lnAvailable = float(variant.lnAvailable or 0) + float(q_delta)
            variant.lnModifiedBy = user
            variant.lnModifiedAt = now

    # Execution based on action mapping
    if action in ['Transfer', 'Reserved Transfer']:
        out_action = "OUT" if action == "Transfer" else "Reserved OUT"
        apply_valuation_bucket_logic(db, db_id, item_id, origin_id, qty, out_action, mv_id, supply_id, price, user, now, unit_tax)
        update_net_stock(origin_id, -qty)
        apply_valuation_bucket_logic(db, db_id, item_id, dest_id, qty, action, mv_id, supply_id, price, user, now, unit_tax)
        update_net_stock(dest_id, qty)
        update_global(0)
    elif action in ['IN', 'Reserved IN']:
        apply_valuation_bucket_logic(db, db_id, item_id, dest_id, qty, action, mv_id, supply_id, price, user, now, unit_tax)
        update_net_stock(dest_id, qty)
        update_global(qty)
    elif action in ['OUT', 'Reserved OUT']:
        apply_valuation_bucket_logic(db, db_id, item_id, origin_id, qty, action, mv_id, supply_id, price, user, now, unit_tax)
        update_net_stock(origin_id, -qty)
        update_global(-qty)

    mv_obj.mvStatus = "POSTED"
    mv_obj.mvModifiedby = user
    mv_obj.mvModifieddate = now

def process_single_movement_logic(db: Session, data: dict):
    mv_id = str(data.get("movement_id", "")).strip()
    db_id = str(data.get("database_id", "")).strip()
    item_id = str(data.get("item_id", "")).strip()
    origin_id = str(data.get("origin_id", "")).strip() if data.get("origin_id") else None
    dest_id = str(data.get("project_id", "")).strip() if data.get("project_id") else None
    qty = float(data.get("qty", 0.0))
    price = float(data.get("price", 0.0))
    unit_tax = float(data.get("unit_tax", 0.0))
    total_cost = float(data.get("total_cost", 0.0)) or (qty * (price + unit_tax))
    supply_id = str(data.get("supply_id", "")).strip() if data.get("supply_id") else None
    action = str(data.get("action", "")).strip()
    user = str(data.get("created_by", "AI_BOT")).strip()
    now = get_now_ca()

    logger.info(f"Processing movement {mv_id} for item {item_id}. Action: {action}")

    # Row-level lock to prevent concurrent webhook execution
    mv_obj = db.query(IcMovement).filter(IcMovement.MovementID == mv_id).with_for_update().first()
    
    if mv_obj and mv_obj.mvStatus == "POSTED":
        return {"status": "skipped", "reason": "POSTED", "movement_id": mv_id}

    if not mv_obj:
        mv_obj = IcMovement(MovementID=mv_id)
        db.add(mv_obj)

    mv_obj.DatabaseID = db_id
    mv_obj.ItemID = item_id
    mv_obj.OriginID = origin_id
    mv_obj.ProjectID = dest_id
    mv_obj.mvAction = action
    mv_obj.mvQuantity = qty
    mv_obj.mvUnitCost = price
    mv_obj.mvTax = unit_tax
    mv_obj.mvTotalCost = total_cost
    mv_obj.mvDate = now
    mv_obj.mvCreatedby = user
    mv_obj.mvCreateddate = now if not mv_obj.mvCreateddate else mv_obj.mvCreateddate

    _perform_inventory_update(db, mv_obj, db_id, item_id, supply_id, price, user, now, unit_tax)

    db.commit()
    logger.info(f"Movement {mv_id} processed successfully for item {item_id}")
    return {"status": "success", "processed_action": action, "item_id": item_id}

def backfill_movement_costs_logic(db: Session, database_id: str, limit: int = None):
    """
    Corrige retroactivamente mvUnitCost/mvTax/mvTotalCost en icMovements
    y prPrice/prTax/prTotal en icItemsPrices para todos los registros que
    provienen de una línea de documento (DocumentLnID presente).
    Crea registros faltantes en icItemsPrices para movimientos que nunca
    generaron su bucket (e.g. movimientos pre-refactor con recinto en campo erróneo).
    Usar limit=N para verificar antes de correr completo.
    """
    inward_actions  = ["IN", "Reserved IN", "Transfer", "Reserved Transfer"]
    outward_actions = ["OUT", "Reserved OUT"]

    q = (
        db.query(IcMovement)
        .join(FnDocumentLn, IcMovement.DocumentLnID == FnDocumentLn.DocumentLnID)
        .join(FnDocument, FnDocumentLn.DocumentID == FnDocument.DocumentID)
        .filter(
            IcMovement.DatabaseID == database_id,
            IcMovement.DocumentLnID.isnot(None),
            FnDocument.Bot == "ADDED"
        )
    )
    if limit:
        q = q.limit(limit)
    movements = q.all()

    updated_mv = 0
    updated_price = 0
    created_price = 0
    skipped = 0

    for mv in movements:
        ln = db.query(FnDocumentLn).filter(FnDocumentLn.DocumentLnID == mv.DocumentLnID).first()
        if not ln:
            skipped += 1
            continue

        qty = float(ln.dlQuantity or 0)
        if qty == 0:
            skipped += 1
            continue

        net_unit_price = float(ln.dlSubtotal or 0) / qty
        unit_tax = float(ln.dlTaxes or 0) / qty
        total_cost = float(ln.dlTotal or 0)

        mv.mvUnitCost = net_unit_price
        mv.mvTax = unit_tax
        mv.mvTotalCost = total_cost
        updated_mv += 1

        # Actualizar buckets existentes vinculados a este movimiento
        price_recs = db.query(IcPrice).filter(IcPrice.MovementID == mv.MovementID).all()
        for pr in price_recs:
            pr.prPrice = net_unit_price
            pr.prTax = unit_tax
            pr.prTotal = float(pr.prQuantity or 0) * (net_unit_price + unit_tax)
            updated_price += 1

        # Si no existe ningún bucket para este movimiento, crearlo.
        # Compatibilidad pre-refactor: IN guardaba recinto en OriginID, OUT en ProjectID.
        if not price_recs:
            action = mv.mvAction or ""
            if action in inward_actions:
                loc_id = mv.ProjectID or mv.OriginID
            elif action in outward_actions:
                loc_id = mv.OriginID or mv.ProjectID
            else:
                loc_id = None

            if loc_id:
                existing_bucket = db.query(IcPrice).filter(
                    IcPrice.ItemID == mv.ItemID,
                    IcPrice.ProjectID == loc_id,
                    IcPrice.prTitle == action,
                    IcPrice.isDeleted == False
                ).first()

                if existing_bucket:
                    existing_bucket.prQuantity = float(existing_bucket.prQuantity or 0) + qty
                    existing_bucket.prPrice = net_unit_price
                    existing_bucket.prTax = unit_tax
                    existing_bucket.prTotal = float(existing_bucket.prQuantity) * (net_unit_price + unit_tax)
                    updated_price += 1
                else:
                    db.add(IcPrice(
                        PriceID=str(uuid.uuid4()).replace('-', '')[:10].upper(),
                        DatabaseID=database_id,
                        ItemID=(mv.ItemID or "")[:10],
                        ProjectID=str(loc_id)[:10],
                        MovementID=mv.MovementID,
                        SupplyID=mv.ItemID,
                        prTitle=action,
                        prDescription=f"Backfill: {action}",
                        prQuantity=qty,
                        prPrice=net_unit_price,
                        prTax=unit_tax,
                        prTotal=qty * (net_unit_price + unit_tax),
                        prCreatedby=mv.mvCreatedby,
                        prCreateddate=mv.mvCreateddate,
                        prModifiedby=mv.mvModifiedby,
                        prModifieddate=mv.mvModifieddate,
                        isDeleted=False
                    ))
                    created_price += 1

    db.commit()
    return {
        "status": "success",
        "movements_updated": updated_mv,
        "price_records_updated": updated_price,
        "price_records_created": created_price,
        "skipped": skipped
    }

def backfill_units_logic(db: Session, database_id: str, dry_run: bool = False, limit: int = None):
    """HERRAMIENTA DE UN SOLO USO (no es parte de la rutina).

    Rellena bcItems.UnitID (y la UnitID de sus variantes) cuando está vacío, infiriendo la
    unidad de venta con IA a partir de itTitle + itModel (sin re-scrapear). Resuelve/crea la
    unidad en bcUnits vía upsert_unit_logic. Usar dry_run=True para previsualizar las unidades
    que sugiere la IA sin escribir, y limit=N para acotar la corrida.
    """
    from ai_services import infer_unit_from_text
    database_id = (database_id or "")[:10]
    q = db.query(BcItem).filter(
        BcItem.DatabaseID == database_id,
        BcItem.isDeleted.isnot(True),
        or_(BcItem.UnitID.is_(None), BcItem.UnitID == "")
    )
    if limit:
        q = q.limit(limit)
    items = q.all()

    now = get_now_ca()
    updated = 0
    skipped = 0
    preview = []
    for it in items:
        data = infer_unit_from_text(it.itTitle, it.itModel)
        unit_name = str((data or {}).get("itUnit") or "").strip()
        unit_symbol = str((data or {}).get("itUnitSymbol") or "").strip()
        if not unit_name and not unit_symbol:
            skipped += 1
            continue
        if dry_run:
            preview.append({"item": it.ItemID, "title": it.itTitle, "model": it.itModel,
                            "unit": unit_name, "symbol": unit_symbol})
            updated += 1
            continue
        unit_id = upsert_unit_logic(db, unit_name, unit_symbol, database_id)
        if not unit_id:
            skipped += 1
            continue
        it.UnitID = unit_id
        it.itModifiedBy = "AI_BOT"
        it.itModifiedAt = now
        for ln in db.query(BcItemLn).filter(BcItemLn.ItemID == it.ItemID,
                                            BcItemLn.DatabaseID == database_id).all():
            if not ln.UnitID or ln.UnitID == "UND":
                ln.UnitID = unit_id
                ln.lnModifiedBy = "AI_BOT"
                ln.lnModifiedAt = now
        updated += 1

    if not dry_run:
        db.commit()
    return {
        "status": "success",
        "database_id": database_id,
        "dry_run": dry_run,
        "candidates": len(items),
        "updated": updated,
        "skipped": skipped,
        "preview": (preview if dry_run else None),
    }

def backfill_sizes_logic(db: Session, database_id: str, dry_run: bool = False, limit: int = None):
    """HERRAMIENTA DE UN SOLO USO (no es parte de la rutina).

    Rellena bcItems.itSize (Dimensión) cuando está vacío, extrayendo las medidas físicas con IA
    a partir de itTitle + itModel (sin re-scrapear). No todos los productos tienen dimensión en su
    nombre: los que no la tienen se saltan (la IA devuelve null y no se escribe nada).
    Usar dry_run=True para previsualizar y limit=N para acotar la corrida.
    """
    from ai_services import infer_size_from_text
    database_id = (database_id or "")[:10]
    q = db.query(BcItem).filter(
        BcItem.DatabaseID == database_id,
        BcItem.isDeleted.isnot(True),
        or_(BcItem.itSize.is_(None), BcItem.itSize == "")
    )
    if limit:
        q = q.limit(limit)
    items = q.all()

    now = get_now_ca()
    updated = 0
    skipped = 0
    preview = []
    for it in items:
        data = infer_size_from_text(it.itTitle, it.itModel)
        size_val = str((data or {}).get("itSize") or "").strip()
        if not size_val or size_val.lower() in ("null", "none", "n/a"):
            skipped += 1
            continue
        size_val = size_val[:100]
        if dry_run:
            preview.append({"item": it.ItemID, "title": it.itTitle, "model": it.itModel, "size": size_val})
            updated += 1
            continue
        it.itSize = size_val
        it.itModifiedBy = "AI_BOT"
        it.itModifiedAt = now
        updated += 1

    if not dry_run:
        db.commit()
    return {
        "status": "success",
        "database_id": database_id,
        "dry_run": dry_run,
        "candidates": len(items),
        "updated": updated,
        "skipped": skipped,
        "preview": (preview if dry_run else None),
    }

def backfill_clean_titles_logic(db: Session, database_id: str, dry_run: bool = False, limit: int = None):
    """HERRAMIENTA DE UN SOLO USO (no es parte de la rutina).

    Limpia bcItems.itTitle dejando el nombre genérico del padre, quitando dimensión, marca,
    presentación y color/acabado de variante (esos datos ya viven en itSize/itBrand/itModel).
    NO fusiona padres duplicados; solo limpia el nombre. Devuelve un resumen de qué nombres
    quedarían duplicados tras la limpieza. Usar dry_run=True para previsualizar.
    """
    from ai_services import clean_parent_title
    database_id = (database_id or "")[:10]
    brand_map = {
        b.BrandID: b.brTitle
        for b in db.query(BcBrand.BrandID, BcBrand.brTitle).filter(BcBrand.DatabaseID == database_id).all()
    }
    q = db.query(BcItem).filter(
        BcItem.DatabaseID == database_id,
        BcItem.isDeleted.isnot(True)
    )
    if limit:
        q = q.limit(limit)
    items = q.all()

    now = get_now_ca()
    changed = 0
    unchanged = 0
    preview = []
    resulting_titles = {}
    for it in items:
        brand_name = brand_map.get(it.itBrand, "") if it.itBrand else ""
        data = clean_parent_title(it.itTitle, brand_name, it.itSize, it.itModel)
        new_title = str((data or {}).get("itTitle") or "").strip()[:300]
        if not new_title:
            new_title = it.itTitle  # nunca vaciar
        resulting_titles[new_title] = resulting_titles.get(new_title, 0) + 1
        if new_title == (it.itTitle or "").strip():
            unchanged += 1
            continue
        if dry_run:
            preview.append({"item": it.ItemID, "old": it.itTitle, "new": new_title})
        else:
            it.itTitle = new_title
            it.itModifiedBy = "AI_BOT"
            it.itModifiedAt = now
        changed += 1

    if not dry_run:
        db.commit()

    duplicates = {t: n for t, n in resulting_titles.items() if n > 1}
    return {
        "status": "success",
        "database_id": database_id,
        "dry_run": dry_run,
        "scanned": len(items),
        "changed": changed,
        "unchanged": unchanged,
        "resulting_distinct_titles": len(resulting_titles),
        "duplicate_titles_after": len(duplicates),
        "duplicates_detail": duplicates,
        "preview": (preview if dry_run else None),
    }

def backfill_categories_logic(db: Session, database_id: str, dry_run: bool = False):
    """HERRAMIENTA DE UN SOLO USO (no es parte de la rutina).

    Convierte el campo itCategory de bcItems que aún tiene texto libre histórico
    a un CategoryID de utCategories (resolviendo/creando la categoría). Los items
    cuyo itCategory ya es un CategoryID válido no se tocan. Usar dry_run=True para
    previsualizar sin escribir.
    """
    database_id = (database_id or "")[:10]
    existing_ids = {
        row[0] for row in db.query(UtCategory.CategoryID)
        .filter(UtCategory.DatabaseID == database_id).all()
    }
    items = db.query(BcItem).filter(
        BcItem.DatabaseID == database_id,
        BcItem.itCategory.isnot(None),
        BcItem.itCategory != "",
        BcItem.isDeleted.isnot(True)
    ).all()

    now = get_now_ca()
    converted = 0
    already_id = 0
    mapping = {}
    for it in items:
        cur = (it.itCategory or "").strip()
        if not cur or cur in existing_ids:
            already_id += 1
            continue
        if dry_run:
            mapping[cur] = mapping.get(cur, 0) + 1
            converted += 1
            continue
        cat_id = upsert_category_logic(db, cur, database_id)
        if cat_id:
            it.itCategory = cat_id
            it.itModifiedBy = "AI_BOT"
            it.itModifiedAt = now
            existing_ids.add(cat_id)
            converted += 1

    if not dry_run:
        db.commit()
    return {
        "status": "success",
        "database_id": database_id,
        "dry_run": dry_run,
        "total_with_category": len(items),
        "already_category_id": already_id,
        "converted": converted,
        "distinct_free_text": (mapping if dry_run else None),
    }

def sync_rfq_lines_logic(db: Session, data: dict):
    rfq_id = str(data.get("rfq_id", "")).strip()
    db_id = str(data.get("database_id", "")).strip()

    raw_ids = str(data.get("selected_ids", "")).strip()
    if raw_ids:
        selected_ids = [sid.strip() for sid in raw_ids.split(",") if sid.strip()]
    else:
        selected_ids = []

    if not rfq_id:
        return {"status": "error", "reason": "No RFQID provided"}

    now = get_now_ca()

    try:
        if not selected_ids:
            db.execute(
                text("DELETE FROM bcRFQLns WHERE RFQID = :rfq_id"),
                {"rfq_id": rfq_id}
            )
            db.commit()
            return {"status": "success", "action": "cleared_all"}

        format_strings = ','.join([f':id_{i}' for i in range(len(selected_ids))])
        delete_params = {f'id_{i}': sid for i, sid in enumerate(selected_ids)}
        delete_params['rfq_id'] = rfq_id

        delete_query = f"DELETE FROM bcRFQLns WHERE RFQID = :rfq_id AND OriginalRequestLnID NOT IN ({format_strings})"
        db.execute(text(delete_query), delete_params)

        existing_recs = db.execute(
            text("SELECT OriginalRequestLnID FROM bcRFQLns WHERE RFQID = :rfq_id"),
            {"rfq_id": rfq_id}
        ).fetchall()
        existing_ids = {row[0] for row in existing_recs}

        to_insert = [sid for sid in selected_ids if sid not in existing_ids]

        if to_insert:
            insert_format = ','.join([f':ins_{i}' for i in range(len(to_insert))])
            ins_params = {f'ins_{i}': sid for i, sid in enumerate(to_insert)}

            max_line_query = "SELECT MAX(rfLineNumber) FROM bcRFQLns WHERE RFQID = :rfq_id"
            current_max = db.execute(text(max_line_query), {"rfq_id": rfq_id}).scalar()
            next_line_num = (current_max or 0) + 1

            select_query = f"""
                SELECT RequestLnID, ItemID, rlQuality, rlObservations, UnitID
                FROM bcRequestsLns
                WHERE RequestLnID IN ({insert_format}) AND (isDeleted = 0 OR isDeleted IS NULL)
            """
            source_lines = db.execute(text(select_query), ins_params).fetchall()

            for row in source_lines:
                new_id = str(uuid.uuid4()).replace('-', '')[:10].upper()
                db.execute(
                    text("""
                        INSERT INTO bcRFQLns (
                            RFQLnID, DatabaseID, RFQID, OriginalRequestLnID,
                            ItemID, rfQuality, rfObservations, UnitID, rfTimestamp, rfLineNumber
                        ) VALUES (
                            :new_id, :db_id, :rfq_id, :req_id,
                            :item_id, :qty, :obs, :unit_id, :now, :line_num
                        )
                    """),
                    {
                        "new_id": new_id,
                        "db_id": db_id,
                        "rfq_id": rfq_id,
                        "req_id": row[0],
                        "item_id": row[1],
                        "qty": row[2] if row[2] is not None else 0.0,
                        "obs": row[3],
                        "unit_id": row[4],
                        "now": now,
                        "line_num": next_line_num
                    }
                )
                next_line_num += 1

        db.commit()
        return {"status": "success", "inserted": len(to_insert), "deleted_eval": True}

    except Exception as e:
        db.rollback()
        logger.error(f"Failed sync RFQ lines: {str(e)}")
        raise
