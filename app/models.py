from sqlalchemy import (
    Column, Integer, String, Float, Boolean,
    DateTime, Date, Text, Enum, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum

class UserRole(str, enum.Enum):
    admin = "admin"
    cfau_oic = "cfau_oic"
    bdrrmo = "bdrrmo"
    cdrrmo_staff = "cdrrmo_staff"

class RiskLevel(str, enum.Enum):
    low = "low"
    moderate = "moderate"
    high = "high"
    critical = "critical"

class DisasterType(str, enum.Enum):
    flood = "flood"
    earthquake = "earthwuake"
    fire = "fire"
    landslide = "landslide"
    typhoon = "typhoon"
    other = "other"

class Severity(str, enum.Enum):
    minor = "minor"
    moderate = "moderate"
    major = "major"
    catastrophic = "catastrophic"

class FacilityType(str, enum.Enum):
    evacuation_center = "evacuation_center"
    health_clinic = "health_clinic"
    school = "school"
    hospital = "hospital"
    staging_area = "staging_area"

class EquipmentType(str, enum.Enum):
    fire_truck = "fire_truck"
    ambulance = "ambulance"
    rescue_vehicle = "rescue_vehicle"
    generator = "generator"
    chainsaw = "chainsaw"
    other = "other"

class EquipmentStatus(str, enum.Enum):
    serviceable = "serviceable"
    not_serviceable = "not_serviceable"
    under_repair = "under_repair"

class Urgency(str, enum.Enum):
    low = "low"
    moderate = "moderate"
    high = "high"
    critical = "critical"

class ReportStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    reviewed = "reviewed"
    confirmed = "confirmed"
    failed = "failed"

class FileType(str, enum.Enum):
    pdf = "pdf"
    excel = "excel"
    csv = "csv"

class ResourceCategory(str, enum.Enum):
    food = "food"
    medicine = "medicine"
    shelter = "shelter"
    water = "water"
    tools = "tools"
    clothing = "clothing"
    other = "other"

# ==========================
# TABLE 1: Barangay
# ==========================

class Barangay(Base):
    __tablename__ = "barangays"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    risk_level = Column(Enum(RiskLevel), default=RiskLevel.low)
    captain_name = Column(String(100))
    captain_contact = Column(String(20))
    chairperson_name = Column(String(100))
    chairperson_contact = Column(String(20))
    area_sqkm = Column(Float)
    hazard_types = Column(String(200))  # comma-separated: "flood,earthquake"
    created_at = Column(DateTime, server_default=func.now())

    # Relationships — one barangay has many of these
    users = relationship("User", back_populates="barangay")
    populations = relationship("Population", back_populates="barangay")
    facilities = relationship("Facility", back_populates="barangay")
    incidents = relationship("Incident", back_populates="barangay")
    uploaded_reports = relationship("UploadedReport", back_populates="barangay")

# ==========================
# TABLE 2: Users (4 roles)
# ==========================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False, unique=True)
    email = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    is_active = Column(Boolean, default=True)
    contact_number = Column(String(20))
    # nullable=True — admin/cfau don't belong to a specific barangay
    barangay_id = Column(Integer, ForeignKey("barangays.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    last_login = Column(DateTime)

    # relationships
    barangay = relationship("Barangay", back_populates="users")
    audit_logs = relationship("AuditLog", back_populates="user")
    reported_incidents = relationship("Incident", back_populates="reported_by_user")
    equipment_reports = relationship("EquipmentReport", back_populates="reported_by_user")
    incident_reports = relationship("IncidentReport", back_populates="submitted_by_user")
    uploaded_reports = relationship("UploadedReport", back_populates="uploaded_by_user")
    updated_resources = relationship("Resource", back_populates="updated_by_user")

# ==========================
# TABLE 3: Population - census data per brgy
# ==========================
class Population(Base):
    __tablename__ = "populations"

    id = Column(Integer, primary_key=True, index=True)
    barangay_id = Column(Integer, ForeignKey("barangays.id"), nullable=False)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    total_population = Column(Integer, default=0)
    total_households = Column(Integer, default=0)
    pwd_count = Column(Integer, default=0)
    elderly_count = Column(Integer, default=0)
    children_count = Column(Integer, default=0)
    recorded_at = Column(DateTime, server_default=func.now())

    # Relationships
    barangay = relationship("Barangay", back_populates="populations")

# ==========================
# TABLE 4: Critical facility points w/ map coordinates
# ==========================

class Facility(Base):
    __tablename__ = "facilities"

    id = Column(Integer, primary_key=True, index=True)
    barangay_id = Column(Integer, ForeignKey("barangays.id"), nullable=False)
    name = Column(String(150), nullable=False)
    facility_type = Column(Enum(FacilityType), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    capacity = Column(Integer)  # for evacuation centers
    address = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    # relationships
    barangay = relationship("Barangay", back_populates="facilities")

# ==========================
# TABLE 5: Incident reporting
# ==========================

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    barangay_id = Column(Integer, ForeignKey("barangays.id"), nullable=False)
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    disaster_type = Column(Enum(DisasterType), nullable=False)
    date_occurred = Column(Date, nullable=False)
    severity = Column(Enum(Severity), default=Severity.moderate)
    affected_families = Column(Integer, default=0)
    casualties = Column(Integer, default=0)
    description = Column(Text)
    source = Column(String(100))  # manual entry, uploaded report, etc.
    created_at = Column(DateTime, server_default=func.now())

    #relationships
    barangay = relationship("Barangay", back_populates="incidents")
    reported_by_user = relationship("User", back_populates="reported_incidents")
    incident_reports = relationship("IncidentReport", back_populates="incident")

# ==========================
# TABLE 6: Resource Inventory
# ==========================

class Resource(Base):
    __tablename__ = "resources"

    id = Column(Integer, primary_key=True, index=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(150), nullable=False)
    category = Column(Enum(ResourceCategory), nullable=False)
    is_perishable = Column(Boolean, default=False)
    quantity = Column(Integer, default=0)
    unit = Column(String(30))  # packs, liters, kg, pieces
    storage_location = Column(String(150))  # warehouse/shelf
    restock_threshold = Column(Integer, default=0)
    expiry_date = Column(Date, nullable=True)  # only for perishables
    is_archived = Column(Boolean, default=False)
    last_updated = Column(DateTime, server_default=func.now(), onupdate=func.now())

    #relationships
    updated_by_user = relationship("User", back_populates="updated_resources")

# ==========================
# TABLE 7: Equipments
# ==========================

class Equipment(Base):
    __tablename__ = "equipment"

    id = Column(Integer, primary_key=True, index=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(150), nullable=False)
    equipment_type = Column(Enum(EquipmentType), nullable=False)
    status = Column(Enum(EquipmentStatus), default=EquipmentStatus.serviceable)
    plate_or_serial = Column(String(50))
    last_inspected = Column(Date)
    created_at = Column(DateTime, server_default=func.now())

    #relationships
    assigned_to_user = relationship("User")
    equipment_reports = relationship("EquipmentReport", back_populates="equipment")

# ==========================
# TABLE 8: Equipment Reports
# ==========================

class EquipmentReport(Base):
    __tablename__ = "equipment_reports"

    id = Column(Integer, primary_key=True, index=True)
    equipment_id = Column(Integer, ForeignKey("equipment.id"), nullable=False)
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    issue_description = Column(Text)
    urgency = Column(Enum(Urgency), default=Urgency.moderate)
    status = Column(Enum(EquipmentStatus), default=EquipmentStatus.not_serviceable)
    admin_remarks = Column(Text)
    reported_at = Column(DateTime, server_default=func.now())

    # relationships
    equipment = relationship("Equipment", back_populates="equipment_reports")
    reported_by_user = relationship("User", back_populates="equipment_reports")

# ==========================
# TABLE 9: Incident Reprot
# ==========================

class IncidentReport(Base):
    __tablename__ = "incident_reports"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    equipment_used = Column(Text)
    field_risks = Column(Text)
    recommendations = Column(Text)
    submitted_at = Column(DateTime, server_default=func.now())

    #relationships
    incident = relationship("Incident", back_populates="incident_reports")
    submitted_by_user = relationship("User", back_populates="incident_reports")

# ==========================
# TABLE 10: Report Uploads
# ==========================

class UploadedReport(Base):
    __tablename__ = "uploaded_reports"

    id = Column(Integer, primary_key=True, index=True)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    barangay_id = Column(Integer, ForeignKey("barangays.id"), nullable=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)  # path to raw file in /uploads
    file_type = Column(Enum(FileType), nullable=False)
    ai_summary = Column(Text)           # plain-English summary from Groq
    extracted_data = Column(JSON)       # structured JSON fields from Groq
    status = Column(Enum(ReportStatus), default=ReportStatus.pending)
    uploaded_at = Column(DateTime, server_default=func.now())

    #relationships
    uploaded_by_user = relationship("User", back_populates="uploaded_reports")
    barangay = relationship("Barangay", back_populates="uploaded_reports")

# ==========================
# TABLE 11: Audit Log
# ==========================

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(100), nullable=False)  # e.g. "created", "updated", "deleted"
    target_table = Column(String(50))             # e.g. "resources", "incidents"
    target_id = Column(Integer)                   # the ID of the affected record
    description = Column(Text)                    # human-readable description
    timestamp = Column(DateTime, server_default=func.now())

    # relationships
    user = relationship("User", back_populates="audit_logs")




# *******************************
# Audit log helper (called from every route that changes data)
# *******************************

def log_action(db, user_id: int, action: str, target_table: str, target_id: int, description: str):
    log = AuditLog(
        user_id=user_id,
        action=action,
        target_table=target_table,
        target_id=target_id,
        description=description
    )
    db.add(log)
    db.commit()