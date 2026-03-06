from database import SessionLocal
from models import BcItemLn, BcItem

db = SessionLocal()
try:
    total_items = db.query(BcItem).count()
    total_lns = db.query(BcItemLn).count()
    missing_urls = db.query(BcItem).filter(BcItem.itWebsite == "").count()
    print(f'Total Items: {total_items} | Total Lns: {total_lns} | Missing URLs: {missing_urls}')
finally:
    db.close()
