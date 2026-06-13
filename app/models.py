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
    earthquake = "earthquake"
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

class FacilityStatus(str, enum.Enum):
    """Week 9 — operational status tag for a critical facility, as
    maintained by the BDRRMO Chairperson. Distinct from the structural
    `Facility.status` field (Permanent / Temporary / Under Construction)
    imported in Week 5."""
    available = "available"
    under_maintenance = "under_maintenance"
    unavailable = "unavailable"

class EquipmentType(str, enum.Enum):
    fire_truck = "fire_truck"
    ambulance = "ambulance"
    rescue_vehicle = "rescue_vehicle"
    generator = "generator"
    chainsaw = "chainsaw"
    # Week 7 — additive: client-requested equipment categories.
    rescue_boat = "rescue_boat"
    radio = "radio"
    flashlight = "flashlight"
    life_vest = "life_vest"
    other = "other"

class EquipmentStatus(str, enum.Enum):
    # Legacy values kept for backward compatibility with existing rows.
    serviceable = "serviceable"
    not_serviceable = "not_serviceable"
    under_repair = "under_repair"
    # Week 7 — client-aligned statuses for operational monitoring.
    available = "available"
    deployed = "deployed"
    unserviceable = "unserviceable"

class Urgency(str, enum.Enum):
    low = "low"
    moderate = "moderate"
    high = "high"
    critical = "critical"

class ServiceabilityStatus(str, enum.Enum):
    """Week 8 — workflow status for CFAU operational reports.

    Tracks where a report sits in its review lifecycle, kept separate
    from the EquipmentStatus serviceability *finding*. Used by both
    EquipmentReport (full draft → submitted → reviewed → resolved flow)
    and IncidentReport (only draft → submitted are used)."""
    draft = "draft"
    submitted = "submitted"
    reviewed = "reviewed"
    resolved = "resolved"

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

class LifecycleStatus(str, enum.Enum):
    """Upload lifecycle, kept separate from the extraction-oriented
    ReportStatus. An upload starts as a draft on creation, becomes
    confirmed once saved to Gold, and can then be archived (and restored).
    Drafts may be discarded; discarded uploads are terminal."""
    draft = "draft"
    confirmed = "confirmed"
    archived = "archived"
    discarded = "discarded"

class UploadEvent(str, enum.Enum):
    """Event types recorded in the append-only UploadHistory trail."""
    created = "created"
    extracted = "extracted"
    confirmed = "confirmed"
    edited = "edited"
    archived = "archived"
    unarchived = "unarchived"
    discarded = "discarded"

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
    # Week 9 (TR-BDR-07) — free-text list of emergency responders the
    # BDRRMO Chairperson maintains (names / roles / contact numbers).
    # Officials are captured by captain_*/chairperson_* above.
    emergency_contacts = Column(Text)
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
    email = Column(String(100), nullable=True, unique=True)
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
    equipment_reports = relationship(
        "EquipmentReport",
        back_populates="reported_by_user",
        foreign_keys="EquipmentReport.reported_by",
    )
    incident_reports = relationship("IncidentReport", back_populates="submitted_by_user")
    uploaded_reports = relationship(
        "UploadedReport",
        back_populates="uploaded_by_user",
        foreign_keys="UploadedReport.uploaded_by",
    )
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
    capacity = Column(Integer)  # legacy: kept for backward compatibility
    address = Column(String(255))
    is_active = Column(Boolean, default=True)
    # Week 9 — three-state operational tag managed by the BDRRMO
    # Chairperson. `is_active` is kept in sync (available -> True) so the
    # admin map/profile continue to work unchanged.
    operational_status = Column(
        Enum(FacilityStatus), default=FacilityStatus.available, nullable=False
    )
    # Week 9 — soft delete / archive, matching the Resource/Equipment
    # convention. Archived facilities are hidden from the default list.
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    # Week 5 — fields sourced from san_pedro_critical_facilities.xlsx.
    # Capacity fields are String because the source file uses ranges
    # (e.g. "40-80", "200-400") that cannot be coerced to Integer.
    status = Column(String(30))                       # Permanent / Temporary / Under Construction
    floor_area_sqm = Column(Float)
    capacity_families = Column(String(30))
    capacity_individuals = Column(String(30))
    ereid_capacity_families = Column(String(30))
    ereid_capacity_individuals = Column(String(30))
    supports_tropical_cyclone = Column(Boolean, default=False)
    supports_flooding = Column(Boolean, default=False)
    supports_landslide = Column(Boolean, default=False)
    supports_fire = Column(Boolean, default=False)
    vulnerability_risk = Column(Text)
    eo_moa_mou = Column(Text)
    is_approximate_location = Column(Boolean, default=False)
    is_city_level = Column(Boolean, default=False)

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
    is_archived = Column(Boolean, default=False)
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
    # `status` is the serviceability FINDING (serviceable / under_repair /
    # unserviceable), not the workflow state — those are tracked separately.
    status = Column(Enum(EquipmentStatus), default=EquipmentStatus.not_serviceable)
    admin_remarks = Column(Text)
    reported_at = Column(DateTime, server_default=func.now())  # created timestamp

    # ── Week 8 — CFAU Serviceability Reporting (additive) ─────────────
    title = Column(String(200))
    report_type = Column(String(30))   # inspection / maintenance / serviceability
    report_status = Column(
        Enum(ServiceabilityStatus),
        default=ServiceabilityStatus.draft,
        nullable=False,
    )
    submitted_at = Column(DateTime, nullable=True)   # set when submitted
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # Set when an admin applies this report's finding to the live
    # Equipment.status (admin-assisted sync). NULL = not yet applied.
    finding_applied_at = Column(DateTime, nullable=True)

    # relationships
    equipment = relationship("Equipment", back_populates="equipment_reports")
    reported_by_user = relationship(
        "User", back_populates="equipment_reports", foreign_keys=[reported_by]
    )
    reviewed_by_user = relationship("User", foreign_keys=[reviewed_by])

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
    submitted_at = Column(DateTime, nullable=True)   # set when submitted (NULL = draft)

    # ── Week 8 — CFAU Post-Incident Reporting (additive) ──────────────
    operations_summary = Column(Text)
    actions_taken = Column(Text)
    challenges_encountered = Column(Text)
    personnel_count = Column(Integer, default=0)
    personnel_notes = Column(Text)
    report_status = Column(
        Enum(ServiceabilityStatus),
        default=ServiceabilityStatus.draft,
        nullable=False,
    )
    created_at = Column(DateTime, server_default=func.now())

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

    # ── Upload Lifecycle & Auditability Sprint (additive) ─────────────
    # Lifecycle is tracked separately from `status` (which tracks
    # extraction). New uploads start as draft; confirm → confirmed;
    # confirmed ↔ archived; draft → discarded (terminal).
    lifecycle_status = Column(
        Enum(LifecycleStatus), default=LifecycleStatus.draft, nullable=False
    )
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    discarded_at = Column(DateTime, nullable=True)
    discarded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    #relationships
    uploaded_by_user = relationship(
        "User", back_populates="uploaded_reports", foreign_keys=[uploaded_by]
    )
    barangay = relationship("Barangay", back_populates="uploaded_reports")
    history = relationship(
        "UploadHistory",
        back_populates="report",
        order_by="UploadHistory.timestamp",
        cascade="all, delete-orphan",
    )

# ==========================
# TABLE 10b: Upload History (append-only edit/lifecycle trail)
# ==========================

class UploadHistory(Base):
    """Append-only, immutable history of an upload's lifecycle and edits.

    One row per event. Edit events record a single field change
    (field_changed / old_value / new_value); lifecycle events
    (created, extracted, confirmed, archived, unarchived, discarded)
    use event_type with an optional human-readable note in new_value.

    Rows are never updated or deleted in normal operation — corrections
    are made by appending new rows.
    """
    __tablename__ = "upload_history"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("uploaded_reports.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    event_type = Column(Enum(UploadEvent), nullable=False)
    field_changed = Column(String(100))   # only for edit events
    old_value = Column(Text)
    new_value = Column(Text)
    reason = Column(Text)                 # optional free-text reason
    timestamp = Column(DateTime, server_default=func.now())

    # relationships
    report = relationship("UploadedReport", back_populates="history")
    user = relationship("User")


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


# *******************************
# Upload history helper — append-only. Never updates existing rows.
# Caller is responsible for committing (so several edit rows can be
# batched into one transaction).
# *******************************

def add_upload_history(
    db,
    report_id: int,
    user_id,
    event_type: "UploadEvent",
    field_changed: str = None,
    old_value=None,
    new_value=None,
    reason: str = None,
):
    entry = UploadHistory(
        report_id=report_id,
        user_id=user_id,
        event_type=event_type,
        field_changed=field_changed,
        old_value=None if old_value is None else str(old_value),
        new_value=None if new_value is None else str(new_value),
        reason=(reason or None),
    )
    db.add(entry)
    return entry