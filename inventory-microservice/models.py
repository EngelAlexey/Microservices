from sqlalchemy import Column, String, Float, Date, DateTime, Text, Integer, DECIMAL, Boolean, ForeignKey
from database import Base
import datetime

class BcItem(Base):
    __tablename__ = "bcItems"
    ItemID = Column(String(20), primary_key=True)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(10))
    itImage = Column(String(255))
    itCategory = Column(String(45))
    itSubcategory = Column(String(45))
    itTitle = Column(String(300))
    itDescription = Column(Text)
    itBrand = Column(String(20))
    CabysID = Column(String(20))
    itObservations = Column(Text)
    itStatus = Column(Boolean, default=True)
    itCreatedBy = Column(String(20))
    itCreatedAt = Column(DateTime, default=datetime.datetime.now)
    itModifiedBy = Column(String(20))
    itModifiedAt = Column(DateTime, default=datetime.datetime.now)
    itEnabled = Column(Text)
    DriveID = Column(String(100), nullable=True)
    Bot = Column(String(100))

class BcItemLn(Base):
    __tablename__ = "bcItemsLns"
    ItemLnID = Column(String(20), primary_key=True)
    ItemID = Column(String(20), ForeignKey("bcItems.ItemID"), nullable=False)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(10))
    lnCode = Column(String(50), nullable=False)
    lnBarcode = Column(String(50))
    lnTitle = Column(String(150))
    lnSpecs = Column(String(100))
    lnSize = Column(String(100))
    UnitID = Column(String(45))
    inCertification = Column(String(45))
    lnWeight = Column(Text)
    lnQuantity = Column(DECIMAL(13, 2), default=0)
    lnReserved = Column(DECIMAL(13, 2), default=0)
    lnAvailable = Column(DECIMAL(13, 2))
    lnMin = Column(DECIMAL(13, 2))
    lnMax = Column(DECIMAL(13, 2))
    lnReorder = Column(DECIMAL(13, 2))
    lnFeatures = Column(Text)
    lnObservations = Column(Text)
    lnStatus = Column(Boolean, default=True)
    lnCreatedBy = Column(String(20))
    lnCreatedAt = Column(DateTime, default=datetime.datetime.now)
    lnModifiedBy = Column(String(20))
    lnModifiedAt = Column(DateTime, default=datetime.datetime.now)
    Bot = Column(String(100))

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
    doCredit = Column(String(64), nullable=True) # Cash, 15 days, 30 days
    doCreatedBy = Column(String(150), default="AI_BOT")
    doCreatedAt = Column(DateTime, default=datetime.datetime.now)
    DriveID = Column(String(2000))      
    Bot = Column(Text, nullable=True)   

class FnDocumentLn(Base):
    __tablename__ = "fnDocumentsLns"
    
    DocumentLnID = Column(String(60), primary_key=True)
    DatabaseID = Column(String(10), nullable=True)
    DocumentID = Column(String(150))     
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
    ProjectID = Column(String(10), nullable=True)
    ItemID = Column(String(10))       
    DocumentLnID = Column(String(100))
    mvDate = Column(DateTime, default=datetime.datetime.now)
    mvAction = Column(String(10))     
    mvQuantity = Column(DECIMAL(13, 2))
    mvStatus = Column(String(45), default="Applied")
    mvNotes = Column(Text)
    mvCreatedby = Column(String(10), default="AI_BOT")
    mvCreateddate = Column(DateTime, default=datetime.datetime.now)

class IcPrice(Base):
    __tablename__ = "icItemsPrices"
    
    PriceID = Column(String(10), primary_key=True)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(10))
    ItemID = Column(String(10))
    ProjectID = Column(String(10), nullable=True)
    MovementID = Column(String(10)) 
    prTitle = Column(String(150))   
    prDescription = Column(Text)
    prQuantity = Column(DECIMAL(13, 2))
    prPrice = Column(DECIMAL(13, 2)) 
    prTax = Column(DECIMAL(13, 2), default=0)
    prTotal = Column(DECIMAL(13, 2))
    prCreatedby = Column(String(10), default="AI_BOT")
    prCreateddate = Column(DateTime, default=datetime.datetime.now)
    prModifiedby = Column(String(10))
    prModifieddate = Column(DateTime)

class DrProject(Base):
    __tablename__ = "drProjects"
    ProjectID = Column(String(10), primary_key=True)
    DatabaseID = Column(String(30))
    pjTitle = Column(String(100))
    pjAddress = Column(String(200))

class DrCompany(Base):
    __tablename__ = "drCompanies"
    
    CompanyID = Column(String(50), primary_key=True)
    isDeleted = Column(Boolean, default=False)
    DatabaseID = Column(String(50))
    cpFile = Column(String(255))
    cpCategory = Column(String(100))
    cpTitle = Column(String(150))
    cpName = Column(String(200))
    cpDescription = Column(Text)
    cpIdentification = Column(String(100))
    cpLegalrepresentative = Column(String(150))
    cpRepresentativeID = Column(String(50))
    cpRepresentativeSignature = Column(String(255))
    cpCountry = Column(String(100))
    cpAddress = Column(String(500))
    cpWebpage = Column(String(255))
    cpEmail = Column(String(150))
    cpPhone = Column(String(100))
    cpServices = Column(String(255))
    cpCredit = Column(String(50))
    cpObservations = Column(Text)
    cpStatus = Column(Boolean, default=True)
    cpImage = Column(String(255))
    cpCreatedBy = Column(String(50))
    cpCreatedAt = Column(DateTime, default=datetime.datetime.now)
    cpModifiedby = Column(String(50))
    cpModifiedAt = Column(DateTime)
    cpBot = Column(String(255))
