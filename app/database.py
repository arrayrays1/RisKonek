from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# connect_args only needed for SQLite
engine = create_engine(
    DATABASE_URL.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1
    ),
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=5
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# dependency, used in every route that needs database
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()