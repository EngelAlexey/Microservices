from database import SessionLocal
from models import FnDocument
import json

def verify():
    db = SessionLocal()
    try:
        doc_id = "B89AA9F185"
        doc = db.query(FnDocument).filter(FnDocument.DocumentID == doc_id).first()
        if doc:
            print(f"Found Document: {doc.DocumentID}")
            print(f"doFile: '{doc.doFile}'")
            print(f"DriveID: '{doc.DriveID}'")
            print(f"doAIComment: '{doc.doAIComment}'")
            print(f"doConsecutive: '{doc.doConsecutive}'")
        else:
            print(f"Document {doc_id} NOT found in DB.")
            # Let's see if there are ANY documents
            count = db.query(FnDocument).count()
            print(f"Total documents in table: {count}")
    finally:
        db.close()

if __name__ == "__main__":
    verify()
