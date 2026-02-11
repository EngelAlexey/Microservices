from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "inventory_db")
DB_PORT = os.getenv("DB_PORT", "3306")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

import ssl

# Check for certificate in Render secrets first, then local
ssl_cert_path = "/etc/secrets/server-ca.pem"
if not os.path.exists(ssl_cert_path):
    ssl_cert_path = os.path.join(os.path.dirname(__file__), "certs", "server-ca.pem")

if os.path.exists(ssl_cert_path):
    ssl_context = ssl.create_default_context(cafile=ssl_cert_path)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_REQUIRED
else:
    ssl_context = None
    print(f"WARNING: SSL certificate not found at {ssl_cert_path}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"ssl": ssl_context},
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
