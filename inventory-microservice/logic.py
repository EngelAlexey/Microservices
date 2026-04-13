from sqlalchemy.orm import Session
from models import BcItem, BcItemLn, FnDocument, FnDocumentLn, IcMovement, IcPrice, DrProject, DrCompany, BcBrand, IcItemsStock
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

def get_now_ca():
    return datetime.now(timezone(timedelta(hours=-6))).replace(tzinfo=None)

def fetch_and_upload_image_task(query: str, filename: str, folder_id: str, item_id: str = None):
    try:
        img_bytes, img_type, _ = search_product_image(query)
        if img_bytes:
            drive_file_id = upload_image_to_drive(img_bytes, filename, img_type, folder_id)
            if drive_file_id and item_id:
                from database import SessionLocal
                db = SessionLocal()
                try:
                    item = db.query(BcItem).filter(BcItem.ItemID == item_id).first()
                    if item:
                        if not item.DriveID or item.itImage == filename:
                            item.DriveID = drive_file_id
                            item.itImage = filename
                            db.commit()
                except:
                    pass
                finally:
                    db.close()
    except:
        pass

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
                   origin_id: str, project_id: str, notes: str, now):
    m = IcMovement(
        MovementID=mv_id, DatabaseID=database_id, OriginID=(origin_id or "")[:10] or None,
        ProjectID=(project_id or "")[:10] or None, ItemID=(item_id or "")[:10],
        DocumentLnID=doc_ln_id, mvDate=mv_date or now, mvAction=action,
        mvQuantity=quantity, mvStatus="POSTED", mvNotes=notes,
        mvCreatedby="AI_BOT", mvCreateddate=now,
    )
    db.add(m)
    return m

def apply_valuation_bucket_logic(db: Session, db_id: str, item_id: str, loc_id: str, q_delta: float, 
                                 row_action: str, mv_id: str, supply_id: str, price: float, user: str, now):
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
            rec_in.prTotal = float(rec_in.prQuantity) * float(rec_in.prPrice or price)
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
        rec_bucket.prTotal = float(rec_bucket.prQuantity) * float(rec_bucket.prPrice or price)
        rec_bucket.prModifiedby = user
        rec_bucket.prModifieddate = now
    else:
        db.add(IcPrice(
            PriceID=str(uuid.uuid4()).replace('-', '')[:10].upper(),
            DatabaseID=db_id, ItemID=(item_id or "")[:10], ProjectID=(loc_id or "")[:10] or None,
            MovementID=mv_id, SupplyID=supply_id,
            prTitle=row_action, prDescription=f"Micro: {row_action}",
            prQuantity=q_delta, prPrice=price, prTotal=float(q_delta)*float(price),
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
        notes_base = f"Doc {doc.doConsecutive or document_id}"
        
        # Determine basic action
        action = _determine_action(doc.doAccount, doc.doType)

        if ln.OriginID and ln.DestinationID:
            out_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            # OUT from Origin
            _make_movement(db, mv_id=out_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action="OUT", quantity=qty, origin_id=ln.DestinationID, project_id=ln.OriginID,
                           notes=f"{notes_base} – Traslado salida", now=now)
            pr_out = str(uuid.uuid4()).replace('-', '')[:8].upper()
            _make_price(db, pr_id=pr_out, database_id=database_id, item_id=final_supply_id, mv_id=out_id,
                        project_id=ln.OriginID, title="Traslado Salida", ln=ln, now=now)
            
            in_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            # IN to Destination
            _make_movement(db, mv_id=in_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action="IN", quantity=qty, origin_id=ln.OriginID, project_id=ln.DestinationID,
                           notes=f"{notes_base} – Traslado entrada", now=now)
            pr_in = str(uuid.uuid4()).replace('-', '')[:8].upper()
            _make_price(db, pr_id=pr_in, database_id=database_id, item_id=final_supply_id, mv_id=in_id,
                        project_id=ln.DestinationID, title="Traslado Entrada", ln=ln, now=now)
            created_count += 2
        else:
            mv_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
            
            # Recinto logic: No fallback to IssuerID/ReceptorID
            # The 'ProjectID' column in IcMovement is the Facility (Recinto)
            if action == "OUT":
                loc_val = (ln.OriginID or project_id or "")[:10] or None
                origin_id_val = None # We don't use company IDs here
            else:
                loc_val = (ln.DestinationID or project_id or "")[:10] or None
                origin_id_val = None
                
            _make_movement(db, mv_id=mv_id, database_id=database_id, item_id=final_supply_id, doc_ln_id=ln.DocumentLnID,
                           mv_date=doc.doDate, action=action, quantity=qty, origin_id=origin_id_val, project_id=loc_val,
                           notes=notes_base, now=now)
            
            action_eval = "IN" if action == "IN" else "OUT"
            apply_valuation_bucket_logic(
                db=db, db_id=database_id, item_id=final_supply_id, loc_id=loc_val,
                q_delta=qty, row_action=action_eval, mv_id=mv_id,
                supply_id=ln.SupplyID, price=ln.dlUnitPrice, user="AI_BOT", now=now
            )
            variant = db.query(BcItemLn).filter(BcItemLn.ItemLnID == final_supply_id).first()
            if variant:
                delta = qty if action == "IN" else -qty
                variant.lnQuantity = float(variant.lnQuantity or 0) + delta
                variant.lnAvailable = float(variant.lnAvailable or 0) + delta
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
    final_title = raw_title or raw_name or raw_title
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

def create_item_from_url_logic(db: Session, data: dict, image_url: str, database_id: str, image_folder_id: str = None, item_id: str = None):
    database_id = (database_id or "")[:10]
    it_title = str(data.get("itTitle") or "").strip()[:300]
    it_brand_name = str(data.get("itBrand") or "").strip()
    it_category = str(data.get("itCategory") or "").strip()[:45]
    it_subcategory = str(data.get("itSubcategory") or "").strip()[:45]
    it_model = str(data.get("itModel") or "").strip()[:45]
    it_description = str(data.get("itDescription") or "").strip()
    it_observations = str(data.get("itObservations") or "").strip()
    now = get_now_ca()
    if not it_title: return {"status": "error", "reason": "No se pudo extraer el nombre del producto"}
    brand_id = None
    if it_brand_name: brand_id = upsert_brand_logic(db, it_brand_name, database_id)
    existing = None
    if item_id: existing = db.query(BcItem).filter(BcItem.ItemID == item_id).first()
    if not existing:
        existing = db.query(BcItem).filter(BcItem.itTitle == it_title, BcItem.DatabaseID == database_id, BcItem.isDeleted.isnot(True)).first()
    action = "found"
    if existing:
        item_id = existing.ItemID
        if it_title: existing.itTitle = it_title
        existing.itStatus = True
        existing.itModifiedBy = "AI_BOT"
        existing.itModifiedAt = now
        if brand_id and not existing.itBrand: existing.itBrand = brand_id
        if it_description and not existing.itDescription: existing.itDescription = it_description
        if it_category and not existing.itCategory: existing.itCategory = it_category
        if it_subcategory and not existing.itSubcategory: existing.itSubcategory = it_subcategory
        if it_model and not existing.itModel: existing.itModel = it_model
        if it_observations and not existing.itObservations: existing.itObservations = it_observations
        if data.get("itWebsite") and not existing.itWebsite: existing.itWebsite = str(data.get("itWebsite"))[:500]
        if image_url and not existing.itImage: existing.itImage = image_url[:255]
        db.commit()
    else:
        item_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_item = BcItem(
            ItemID=item_id, DatabaseID=database_id, itTitle=it_title, itDescription=it_description or "",
            itBrand=brand_id, itCategory=it_category or "", itSubcategory=it_subcategory or "", itModel=it_model or "",
            itWebsite=str(data.get("itWebsite"))[:500] if data.get("itWebsite") else "", itObservations=it_observations or "",
            CabysID="", itStatus=True, itImage=image_url[:255] if image_url else None,
            itCreatedBy="AI_BOT", itCreatedAt=now, itModifiedBy="AI_BOT", itModifiedAt=now, Bot="Importado desde URL"
        )
        db.add(new_item)
        db.commit()
        action = "inserted"
    ln_title = f"{it_title} {it_model}".strip() if it_model else it_title
    existing_ln = db.query(BcItemLn).filter(BcItemLn.ItemID == item_id, BcItemLn.lnTitle == ln_title[:150], BcItemLn.DatabaseID == database_id).first()
    if not existing_ln:
        ln_id = str(uuid.uuid4()).replace('-', '')[:8].upper()
        new_ln = BcItemLn(
            ItemLnID=ln_id, ItemID=item_id, DatabaseID=database_id, lnCode=ln_id, lnTitle=ln_title[:150],
            lnSize=it_model[:100] if it_model else "", lnBarcode="", lnSpecs="", UnitID="UND", inCertification="",
            lnWeight="", lnQuantity=0, lnAvailable=0, lnFeatures="", lnObservations="", lnStatus=True,
            lnCreatedBy="AI_BOT", lnCreatedAt=now, lnModifiedBy="AI_BOT", lnModifiedAt=now, Bot="ADDED"
        )
        db.add(new_ln)
        db.commit()
    else:
        existing_ln.lnModifiedBy = "AI_BOT"
        existing_ln.lnModifiedAt = now
        if not existing_ln.lnSize and it_model: existing_ln.lnSize = it_model[:100]
        db.commit()
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
                    finally: s.close()
        t = threading.Thread(target=upload_scraped_image, args=(image_url, img_filename, image_folder_id, item_id), daemon=True)
        t.start()
    return {"status": "success", "action": action, "item_id": item_id, "item_title": it_title, "brand_id": brand_id, "brand_name": it_brand_name or None, "database_id": database_id}

def process_single_movement_logic(db: Session, data: dict):
    mv_id = str(data.get("movement_id", "")).strip()
    db_id = str(data.get("database_id", "")).strip()
    item_id = str(data.get("item_id", "")).strip()
    origin_id = str(data.get("origin_id", "")).strip() if data.get("origin_id") else None
    dest_id = str(data.get("project_id", "")).strip() if data.get("project_id") else None
    qty = float(data.get("qty", 0.0))
    price = float(data.get("price", 0.0))
    supply_id = str(data.get("supply_id", "")).strip() if data.get("supply_id") else None
    action = str(data.get("action", "")).strip()
    user = str(data.get("created_by", "AI_BOT")).strip()
    now = get_now_ca()

    logger.info(f"Processing movement {mv_id} for item {item_id}. Action: {action}")

    # 1. Movimiento (Upsert)
    mv_obj = db.query(IcMovement).filter(IcMovement.MovementID == mv_id).first()
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
    mv_obj.mvDate = now
    mv_obj.mvStatus = "POSTED"
    mv_obj.mvCreatedby = user
    mv_obj.mvCreateddate = now if not mv_obj.mvCreateddate else mv_obj.mvCreateddate

    # --- HELPERS ---
    def update_net_stock(loc_id, q_delta):
        """ icItemsStock: Consolidate to 1 row per Project/Item (Net Total) """
        if not loc_id: return
        loc_id = loc_id.strip()
        rec = db.query(IcItemsStock).filter(
            IcItemsStock.ItemID == item_id,
            IcItemsStock.ProjectID == loc_id
        ).first()

        if rec:
            rec.stQuantity = float(rec.stQuantity or 0) + float(q_delta)
            movs = str(rec.stMovements or "")
            if mv_id not in movs:
                rec.stMovements = "{} , {}".format(movs, mv_id)
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

    # --- EXECUTION FLOW ---
    if action in ['Transfer', 'Reserved Transfer']:
        out_action = "OUT" if action == "Transfer" else "Reserved OUT"
        apply_valuation_bucket_logic(db, db_id, item_id, origin_id, qty, out_action, mv_id, supply_id, price, user, now)
        update_net_stock(origin_id, -qty)
        
        apply_valuation_bucket_logic(db, db_id, item_id, dest_id, qty, action, mv_id, supply_id, price, user, now)
        update_net_stock(dest_id, qty)
        update_global(0) # Touch bcItemsLns to trigger AppSheet sync
        
    elif action in ['IN', 'Reserved IN']:
        apply_valuation_bucket_logic(db, db_id, item_id, dest_id, qty, action, mv_id, supply_id, price, user, now)
        update_net_stock(dest_id, qty)
        update_global(qty)
        
    elif action in ['OUT', 'Reserved OUT']:
        apply_valuation_bucket_logic(db, db_id, item_id, origin_id, qty, action, mv_id, supply_id, price, user, now)
        update_net_stock(origin_id, -qty)
        update_global(-qty)

    db.commit()
    logger.info(f"Movement {mv_id} processed successfully for item {item_id}")
    return {"status": "success", "processed_action": action, "item_id": item_id}
