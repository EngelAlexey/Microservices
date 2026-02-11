from sqlalchemy import Column, String, Float, Date, DateTime, Text, Integer, DECIMAL, Boolean
from database import Base
import datetime

class BcItem(Base):
    __tablename__ = "bcItems"
    ItemID = Column(String(20), primary_key=True)
    DatabaseID = Column(String(10))
    itCode = Column(String(50))
    CabysID = Column(String(20))
    itTitle = Column(String(300))

class FnDocument(Base):
    __tablename__ = "fnDocuments"
    
    DocumentID = Column(String(150), primary_key=True)
    DatabaseID = Column(String(2000), nullable=True)
    doFile = Column(String(256))        
    doDate = Column(Date)
    doType = Column(String(64))         
    doAccount = Column(String(64), nullable=True)
    doTitle = Column(Text, nullable=True)
    doConsecutive = Column(String(2000))
    
    doIssuer = Column(String(2000))     
    IssuerID = Column(String(10), nullable=True)
    doReceptor = Column(String(64))     
    ReceptorID = Column(String(10), nullable=True)
    
    CurrencyID = Column(String(64), default="CRC")
    doSubtotal = Column(DECIMAL(13, 2))
    doTaxes = Column(DECIMAL(13, 2))
    doTotal = Column(DECIMAL(13, 2))
    
    doStatus = Column(String(64), default="NEW")
    doCreatedBy = Column(String(150), default="AI_BOT")
    doCreatedAt = Column(DateTime, default=datetime.datetime.now)
    DriveID = Column(String(2000))      
    Bot = Column(Text, nullable=True)   

class FnDocumentLn(Base):
    __tablename__ = "fnDocumentsLns"
    
    DocumentLnID = Column(String(60), primary_key=True)
    DatabaseID = Column(String(10), nullable=True)
    DocumentID = Column(String(10))     
    dlNumber = Column(Integer, nullable=True)
    
    SupplyID = Column(Text)             
    CabysID = Column(String(50))
    dlDescription = Column(String(2000))
    
    dlQuantity = Column(DECIMAL(13, 2))
    dlUnit = Column(String(64), default="Unid")
    dlUnitPrice = Column(DECIMAL(13, 2))
    dlDiscount = Column(DECIMAL(13, 2), default=0)
    dlSubtotal = Column(DECIMAL(13, 2))
    dlTaxes = Column(DECIMAL(13, 2), default=0)
    dlTotal = Column(DECIMAL(13, 2))
    
    dlObservations = Column(String(2000), nullable=True)

class IcMovement(Base):
    __tablename__ = "icMovements"
    
    MovementID = Column(String(10), primary_key=True)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(10))
    OriginID = Column(String(10))     
    ProjectID = Column(String(10))    
    ItemID = Column(String(10))       
    DocumentLnID = Column(String(10)) 
    mvDate = Column(DateTime, default=datetime.datetime.now)
    mvAction = Column(String(10))     
    mvQuantity = Column(DECIMAL(13, 2))
    mvStatus = Column(String(45), default="Applied")
    mvNotes = Column(Text)
    mvCreatedby = Column(String(10), default="AI_BOT")
    mvCreateddate = Column(DateTime, default=datetime.datetime.now)

class IcPrice(Base):
    __tablename__ = "icPrices"
    
    PriceID = Column(String(10), primary_key=True)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(10))
    ItemID = Column(String(10))
    ProjectID = Column(String(10))
    MovementID = Column(String(10)) 
    prTitle = Column(String(150))   
    prDescription = Column(Text)
    prQuantity = Column(DECIMAL(13, 2))
    prPrice = Column(DECIMAL(13, 2)) 
    prTax = Column(DECIMAL(13, 2), default=0)
    prTotal = Column(DECIMAL(13, 2))
    prCreatedby = Column(String(10), default="AI_BOT")
    prCreateddate = Column(DateTime, default=datetime.datetime.now)