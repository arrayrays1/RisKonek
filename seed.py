"""
Seed script — San Pedro, Laguna (demonstration data)
Run: python seed.py
To reseed: delete instance/riskonek.db first, then run again
"""
from app.database import SessionLocal, engine
from app.models import (
    Base,
    Barangay, Population, Incident, Resource, Equipment, User,
    RiskLevel, DisasterType, Severity, ResourceCategory,
    EquipmentType, EquipmentStatus, UserRole, Facility, FacilityType
)
from app.auth import hash_password
from datetime import date, timedelta
import random

# Ensure all tables exist before seeding (in case the DB file was deleted
# and main.py hasn't been booted yet).
Base.metadata.create_all(bind=engine)

db = SessionLocal()
print("Seeding RisKonek database...")
print("(Demonstration data — replace with actual CDRRMO records before deployment)\n")

# ─────────────────────────────────────────
# 1. BARANGAYS
# Real names and 2020 PSA census population
# Hazard types based on known San Pedro geography
# ─────────────────────────────────────────

barangay_data = [
    {"name": "Bagong Silang",       "population": 5736,  "hazard_types": "flood,earthquake"},
    {"name": "Calendola",           "population": 3797,  "hazard_types": "flood"},
    {"name": "Chrysanthemum",       "population": 12433, "hazard_types": "flood,earthquake"},
    {"name": "Cuyab",               "population": 21422, "hazard_types": "flood,landslide"},
    {"name": "Estrella",            "population": 8025,  "hazard_types": "flood,earthquake"},
    {"name": "Fatima",              "population": 6491,  "hazard_types": "flood"},
    {"name": "G.S.I.S.",            "population": 2828,  "hazard_types": "earthquake"},
    {"name": "Landayan",            "population": 33235, "hazard_types": "flood,earthquake"},
    {"name": "Langgam",             "population": 30946, "hazard_types": "flood"},
    {"name": "Laram",               "population": 6536,  "hazard_types": "earthquake"},
    {"name": "Magsaysay",           "population": 12793, "hazard_types": "flood,earthquake,landslide"},
    {"name": "Maharlika",           "population": 5580,  "hazard_types": "flood"},
    {"name": "Narra",               "population": 2297,  "hazard_types": "earthquake"},
    {"name": "Nueva",               "population": 4286,  "hazard_types": "flood,earthquake"},
    {"name": "Pacita 1",            "population": 22581, "hazard_types": "flood"},
    {"name": "Pacita 2",            "population": 11993, "hazard_types": "flood"},
    {"name": "Poblacion",           "population": 5771,  "hazard_types": "flood,earthquake"},
    {"name": "Riverside",           "population": 3028,  "hazard_types": "flood,landslide"},
    {"name": "Rosario",             "population": 5911,  "hazard_types": "flood"},
    {"name": "Sampaguita Village",  "population": 4941,  "hazard_types": "flood"},
    {"name": "San Antonio",         "population": 59368, "hazard_types": "flood,earthquake"},
    {"name": "San Lorenzo Ruiz",    "population": 5800,  "hazard_types": "flood,earthquake"},
    {"name": "San Roque",           "population": 7161,  "hazard_types": "flood,earthquake"},
    {"name": "San Vicente",         "population": 27561, "hazard_types": "flood,earthquake"},
    {"name": "Santo Niño",          "population": 3892,  "hazard_types": "flood"},
    {"name": "United Bayanihan",    "population": 5385,  "hazard_types": "flood,earthquake"},
    {"name": "United Better Living","population": 6204,  "hazard_types": "flood"},
]

pop_lookup = {b["name"]: b["population"] for b in barangay_data}

barangays_created = []
for b in barangay_data:
    existing = db.query(Barangay).filter(Barangay.name == b["name"]).first()
    if not existing:
        brgy = Barangay(
            name=b["name"],
            risk_level=RiskLevel.low,  # placeholder — computed at end of script
            hazard_types=b["hazard_types"],
            captain_name=f"Hon. [Captain Name]",
            captain_contact=f"09{random.randint(100000000, 999999999)}",
            chairperson_name=f"[BDRRMO Chairperson]",
            chairperson_contact=f"09{random.randint(100000000, 999999999)}",
            area_sqkm=round(random.uniform(0.5, 3.0), 2)
        )
        db.add(brgy)
        barangays_created.append(brgy)

db.commit()
print(f"✓ {len(barangays_created)} barangays seeded")

all_barangays = db.query(Barangay).all()

# ─────────────────────────────────────────
# 2. POPULATION
# Real 2020 PSA figures
# Vulnerable group estimates (replace with
# actual data when available from BDRRMO)
# ─────────────────────────────────────────

pop_created = 0
for brgy in all_barangays:
    existing = db.query(Population).filter(
        Population.barangay_id == brgy.id
    ).first()
    if not existing:
        total = pop_lookup.get(brgy.name, 5000)
        households = round(total / 4.45)  # San Pedro 2015 avg household size
        pop = Population(
            barangay_id=brgy.id,
            total_population=total,
            total_households=households,
            # Estimates only — update with actual BDRRMO census data
            pwd_count=round(total * 0.01),
            elderly_count=round(total * 0.04),
            children_count=round(total * 0.18),
        )
        db.add(pop)
        pop_created += 1

db.commit()
print(f"✓ {pop_created} population records seeded (2020 PSA figures)")
print("  Note: PWD/elderly/children counts are estimates — update with actual BDRRMO data")

# ─────────────────────────────────────────
# 3. INCIDENTS
# Demonstration data only
# Replace with actual CDRRMO incident records
# ─────────────────────────────────────────

disaster_descriptions = {
    DisasterType.flood: [
        "Rising floodwaters affected low-lying areas. Families were preemptively evacuated to designated centers.",
        "Continuous rainfall caused flooding along major streets. CDRRMO deployed rubber boats for rescue operations.",
        "Flash floods due to overflow of nearby waterways. Several households sustained property damage.",
    ],
    DisasterType.earthquake: [
        "Moderate tremor felt across the barangay. Minor structural cracks reported in older buildings.",
        "Light earthquake caused residents to evacuate buildings temporarily. No casualties reported.",
    ],
    DisasterType.fire: [
        "Fire broke out in a residential area. BFP contained the blaze within 2 hours. Affected families provided temporary shelter.",
        "Early morning fire damaged several homes. CDRRMO provided relief goods to affected families.",
    ],
    DisasterType.landslide: [
        "Heavy rains triggered a slope failure along the hillside. Road access temporarily blocked.",
        "Soil erosion caused minor landslide in an elevated area. Families relocated to evacuation center.",
    ],
    DisasterType.typhoon: [
        "Typhoon brought strong winds and heavy rainfall. Widespread flooding and power outages reported.",
        "Signal No. 2 typhoon caused significant damage to homes and infrastructure.",
    ],
}

incident_count = 0
for brgy in all_barangays:
    # 2–5 historical incidents per barangay
    num_incidents = random.randint(2, 5)

    # Weight disaster types by the barangay's known hazards
    hazards = brgy.hazard_types.split(",") if brgy.hazard_types else ["flood"]
    hazard_to_type = {
        "flood": DisasterType.flood,
        "earthquake": DisasterType.earthquake,
        "landslide": DisasterType.landslide,
        "typhoon": DisasterType.typhoon,
        "fire": DisasterType.fire,
    }
    # 70% chance of incident matching known hazard, 30% random
    weighted_types = [hazard_to_type[h] for h in hazards if h in hazard_to_type]
    if not weighted_types:
        weighted_types = [DisasterType.flood]

    for i in range(num_incidents):
        year = random.randint(2019, 2024)
        month = random.randint(1, 12)
        day = random.randint(1, 28)

        if random.random() < 0.7:
            dtype = random.choice(weighted_types)
        else:
            dtype = random.choice(list(DisasterType))

        description = random.choice(disaster_descriptions.get(dtype, ["Disaster event recorded."]))
        pop = pop_lookup.get(brgy.name, 5000)

        incident = Incident(
            barangay_id=brgy.id,
            disaster_type=dtype,
            date_occurred=date(year, month, day),
            severity=random.choice(list(Severity)),
            affected_families=random.randint(5, max(10, pop // 50)),
            casualties=random.randint(0, 3),
            description=description,
            source="Historical record (demonstration data)"
        )
        db.add(incident)
        incident_count += 1

db.commit()
print(f"✓ {incident_count} incident records seeded (demonstration data)")

# ─────────────────────────────────────────
# 4. RESOURCES
# ─────────────────────────────────────────

resources_data = [
    # Perishable
    {"name": "Family Food Pack",        "category": ResourceCategory.food,     "perishable": True,  "qty": 450,  "unit": "packs",   "threshold": 200, "expiry_days": 180},
    {"name": "Rice (25kg sack)",        "category": ResourceCategory.food,     "perishable": True,  "qty": 120,  "unit": "sacks",   "threshold": 50,  "expiry_days": 365},
    {"name": "Canned Goods (assorted)", "category": ResourceCategory.food,     "perishable": True,  "qty": 800,  "unit": "cans",    "threshold": 300, "expiry_days": 730},
    {"name": "Bottled Water (500ml)",   "category": ResourceCategory.water,    "perishable": True,  "qty": 2000, "unit": "bottles", "threshold": 500, "expiry_days": 365},
    {"name": "Oral Rehydration Salts",  "category": ResourceCategory.medicine, "perishable": True,  "qty": 300,  "unit": "sachets", "threshold": 100, "expiry_days": 540},
    {"name": "First Aid Kit",           "category": ResourceCategory.medicine, "perishable": True,  "qty": 45,   "unit": "kits",    "threshold": 20,  "expiry_days": 365},
    # Non-perishable
    {"name": "Tarpaulin (10x12 ft)",    "category": ResourceCategory.shelter,  "perishable": False, "qty": 180,  "unit": "pieces",  "threshold": 50,  "expiry_days": None},
    {"name": "Blanket",                 "category": ResourceCategory.shelter,  "perishable": False, "qty": 320,  "unit": "pieces",  "threshold": 100, "expiry_days": None},
    {"name": "Hygiene Kit",             "category": ResourceCategory.shelter,  "perishable": False, "qty": 150,  "unit": "kits",    "threshold": 60,  "expiry_days": None},
    {"name": "Life Vest",               "category": ResourceCategory.tools,    "perishable": False, "qty": 80,   "unit": "pieces",  "threshold": 30,  "expiry_days": None},
    {"name": "Rope (50m)",              "category": ResourceCategory.tools,    "perishable": False, "qty": 25,   "unit": "rolls",   "threshold": 10,  "expiry_days": None},
    {"name": "Flashlight",              "category": ResourceCategory.tools,    "perishable": False, "qty": 60,   "unit": "pieces",  "threshold": 20,  "expiry_days": None},
    {"name": "Generator (5kva)",        "category": ResourceCategory.tools,    "perishable": False, "qty": 4,    "unit": "units",   "threshold": 2,   "expiry_days": None},
    {"name": "Rubber Boat",             "category": ResourceCategory.tools,    "perishable": False, "qty": 6,    "unit": "units",   "threshold": 2,   "expiry_days": None},
]

resource_count = 0
for r in resources_data:
    existing = db.query(Resource).filter(Resource.name == r["name"]).first()
    if not existing:
        expiry = None
        if r["expiry_days"]:
            expiry = date.today() + timedelta(days=r["expiry_days"])
            # Make ~20% of items expire soon to demo the alert system
            if random.random() < 0.2:
                expiry = date.today() + timedelta(days=random.randint(5, 25))

        resource = Resource(
            name=r["name"],
            category=r["category"],
            is_perishable=r["perishable"],
            quantity=r["qty"],
            unit=r["unit"],
            storage_location=random.choice([
                "Warehouse A — Shelf 1",
                "Warehouse A — Shelf 2",
                "Warehouse B — Ground Floor",
                "City Hall Storage Room",
                "CDRRMO Main Stockroom"
            ]),
            restock_threshold=r["threshold"],
            expiry_date=expiry,
        )
        db.add(resource)
        resource_count += 1

db.commit()
print(f"✓ {resource_count} resource items seeded")

# ─────────────────────────────────────────
# 5. EQUIPMENT
# ─────────────────────────────────────────

equipment_data = [
    {"name": "Fire Truck 1",       "type": EquipmentType.fire_truck,     "status": EquipmentStatus.serviceable,     "serial": "FT-2019-001"},
    {"name": "Fire Truck 2",       "type": EquipmentType.fire_truck,     "status": EquipmentStatus.under_repair,    "serial": "FT-2020-002"},
    {"name": "Ambulance 1",        "type": EquipmentType.ambulance,      "status": EquipmentStatus.serviceable,     "serial": "AM-2021-001"},
    {"name": "Ambulance 2",        "type": EquipmentType.ambulance,      "status": EquipmentStatus.serviceable,     "serial": "AM-2022-002"},
    {"name": "Rescue Vehicle 1",   "type": EquipmentType.rescue_vehicle, "status": EquipmentStatus.serviceable,     "serial": "RV-2020-001"},
    {"name": "Rescue Vehicle 2",   "type": EquipmentType.rescue_vehicle, "status": EquipmentStatus.not_serviceable, "serial": "RV-2018-002"},
    {"name": "Generator Set 1",    "type": EquipmentType.generator,      "status": EquipmentStatus.serviceable,     "serial": "GN-2021-001"},
    {"name": "Generator Set 2",    "type": EquipmentType.generator,      "status": EquipmentStatus.serviceable,     "serial": "GN-2022-002"},
    {"name": "Chainsaw Unit 1",    "type": EquipmentType.chainsaw,       "status": EquipmentStatus.serviceable,     "serial": "CS-2020-001"},
    {"name": "Chainsaw Unit 2",    "type": EquipmentType.chainsaw,       "status": EquipmentStatus.not_serviceable, "serial": "CS-2019-002"},
]

equip_count = 0
for e in equipment_data:
    existing = db.query(Equipment).filter(
        Equipment.plate_or_serial == e["serial"]
    ).first()
    if not existing:
        equip = Equipment(
            name=e["name"],
            equipment_type=e["type"],
            status=e["status"],
            plate_or_serial=e["serial"],
            last_inspected=date(2024, random.randint(1, 12), random.randint(1, 28)),
        )
        db.add(equip)
        equip_count += 1

db.commit()
print(f"✓ {equip_count} equipment items seeded")

# ─────────────────────────────────────────
# 6. CRITICAL FACILITIES
# Count scales with population. Coordinates are jittered around each
# barangay's centroid (~50–200 m offset) so the Leaflet preview can plot them.
# Demonstration data — replace with actual BDRRMO facility records.
# ─────────────────────────────────────────

from app.utils.geo import BARANGAY_COORDS

def facility_quota(population: int) -> dict:
    """Return how many of each facility type to create for a given population.

    Larger barangays get more facilities, capped at small realistic numbers
    so the database isn't flooded with fake data.
    """
    if population >= 25000:      # very large (San Antonio, Landayan, etc.)
        return {"evac": 3, "clinic": 2, "school": 3, "hospital": 1, "staging": 1}
    elif population >= 12000:    # large
        return {"evac": 2, "clinic": 1, "school": 2, "hospital": 1, "staging": 1}
    elif population >= 6000:     # medium
        return {"evac": 2, "clinic": 1, "school": 1, "hospital": 0, "staging": 1}
    else:                        # small
        return {"evac": 1, "clinic": 1, "school": 1, "hospital": 0, "staging": 0}

FACILITY_NAME_TEMPLATES = {
    FacilityType.evacuation_center: [
        "{brgy} Evacuation Center",
        "{brgy} Covered Court",
        "{brgy} Barangay Hall Evac Site",
    ],
    FacilityType.health_clinic: [
        "{brgy} Health Center",
        "{brgy} Lying-in Clinic",
    ],
    FacilityType.school: [
        "{brgy} Elementary School",
        "{brgy} National High School",
        "{brgy} Day Care Center",
    ],
    FacilityType.hospital: [
        "{brgy} District Hospital",
    ],
    FacilityType.staging_area: [
        "{brgy} Staging Area",
    ],
}

FACILITY_CAPACITY = {
    FacilityType.evacuation_center: (200, 800),
    FacilityType.health_clinic:     (15, 60),
    FacilityType.school:            (300, 1500),
    FacilityType.hospital:          (40, 200),
    FacilityType.staging_area:      (0, 0),
}

facility_count = 0
for brgy in all_barangays:
    pop = pop_lookup.get(brgy.name, 5000)
    quota = facility_quota(pop)
    coords = BARANGAY_COORDS.get(brgy.name, {"lat": 14.350, "lng": 121.045})

    type_map = [
        (FacilityType.evacuation_center, quota["evac"]),
        (FacilityType.health_clinic,     quota["clinic"]),
        (FacilityType.school,            quota["school"]),
        (FacilityType.hospital,          quota["hospital"]),
        (FacilityType.staging_area,      quota["staging"]),
    ]

    for ftype, count in type_map:
        templates = FACILITY_NAME_TEMPLATES[ftype]
        for i in range(count):
            template = templates[i % len(templates)]
            # Disambiguate when we create multiple of the same type
            suffix = f" {i + 1}" if count > 1 and i >= len(templates) else ""
            name = template.format(brgy=brgy.name) + suffix

            existing = db.query(Facility).filter(
                Facility.barangay_id == brgy.id,
                Facility.name == name,
            ).first()
            if existing:
                continue

            # ~0.0015° ≈ 165 m jitter
            lat = coords["lat"] + random.uniform(-0.0015, 0.0015)
            lng = coords["lng"] + random.uniform(-0.0015, 0.0015)

            cap_lo, cap_hi = FACILITY_CAPACITY[ftype]
            capacity = random.randint(cap_lo, cap_hi) if cap_hi > 0 else None

            facility = Facility(
                barangay_id=brgy.id,
                name=name,
                facility_type=ftype,
                latitude=round(lat, 6),
                longitude=round(lng, 6),
                capacity=capacity,
                address=f"{brgy.name}, San Pedro, Laguna",
                is_active=random.random() > 0.05,  # ~5% under maintenance
            )
            db.add(facility)
            facility_count += 1

db.commit()
print(f"✓ {facility_count} critical facilities seeded (demonstration data)")

# ─────────────────────────────────────────
# 7. TEST USERS — one per role
# All use password: password123
# ─────────────────────────────────────────

test_users = [
    {"username": "admin",    "email": "admin@riskonek.com",   "role": UserRole.admin,        "barangay": None},
    {"username": "cfau_oic", "email": "cfau@riskonek.com",    "role": UserRole.cfau_oic,     "barangay": None},
    {"username": "bdrrmo1",  "email": "bdrrmo1@riskonek.com", "role": UserRole.bdrrmo,       "barangay": "Cuyab"},
    {"username": "staff1",   "email": "staff1@riskonek.com",  "role": UserRole.cdrrmo_staff, "barangay": None},
]

user_count = 0
for u in test_users:
    existing = db.query(User).filter(
    (User.username == u["username"]) | (User.email == u["email"])).first()
    if not existing:
        barangay_id = None
        if u["barangay"]:
            brgy = db.query(Barangay).filter(
                Barangay.name == u["barangay"]
            ).first()
            if brgy:
                barangay_id = brgy.id

        user = User(
            username=u["username"],
            email=u["email"],
            password_hash=hash_password("password123"),
            role=u["role"],
            is_active=True,
            barangay_id=barangay_id
        )
        db.add(user)
        user_count += 1

db.commit()
print(f"✓ {user_count} test users seeded")

# ─────────────────────────────────────────
# 8. COMPUTE RISK LEVELS FROM DATA
# Run after incidents and population are seeded
# ─────────────────────────────────────────

from app.analytics.simulator import update_all_risk_levels
update_all_risk_levels(db)

db.close()

print("\n✓ Database seeded successfully!")
print("\nTest accounts (password: password123)")
print("  admin      → /admin/dashboard")
print("  cfau_oic   → /cfau/dashboard")
print("  bdrrmo1    → /bdrrmo/dashboard  (Cuyab)")
print("  staff1     → /staff/dashboard")
print("\nNOTE: All data is for demonstration purposes.")
print("Replace with actual CDRRMO records before deployment.")