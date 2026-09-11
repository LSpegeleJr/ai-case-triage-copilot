"""
Sprint 1 synthetic data generator
Salesforce AI Case Triage & Dispatch Copilot

Generates 4 CSVs sized and shaped for Salesforce Data Loader import:
  Assets.csv               -> load first  (Asset object, extended)
  Knowledge_Articles.csv   -> load anytime (Knowledge_Article__c, custom object)
  Historical_Work_Orders.csv -> load after Assets (Work_Order__c, custom object, Status=Completed)
  Cases.csv                -> load after Assets (Case object, extended)

Design note: Asset_Key__c is a text field you mark as "External ID" + "Unique"
on the Asset object in Setup. Cases.csv and Historical_Work_Orders.csv reference
assets by that key, which lets Data Loader resolve the lookup relationship
during insert without needing real Salesforce IDs up front.
"""

import csv
import random
from datetime import datetime, timedelta

random.seed(42)

EQUIPMENT_TYPES = ["Pump", "Valve", "Reactor", "Heat Exchanger", "Scrubber", "Compressor", "Storage Tank", "Sensor/Transmitter"]
CRITICALITY = ["Safety-Critical", "Production-Critical", "Standard"]

# ---------- Assets ----------
assets = []
for i in range(1, 26):
    eq = random.choice(EQUIPMENT_TYPES)
    crit = random.choices(CRITICALITY, weights=[0.25, 0.45, 0.30])[0]
    install_days_ago = random.randint(200, 3000)
    last_maint_days_ago = random.randint(5, 400)
    assets.append({
        "Asset_Key__c": f"AST-{i:04d}",
        "Name": f"{eq} {i:03d}",
        "Equipment_Type__c": eq,
        "Criticality__c": crit,
        "InstallDate": (datetime.now() - timedelta(days=install_days_ago)).strftime("%Y-%m-%d"),
        "Last_Maintenance_Date__c": (datetime.now() - timedelta(days=last_maint_days_ago)).strftime("%Y-%m-%d"),
        "SerialNumber": f"SN-{random.randint(100000,999999)}",
    })

with open("Assets.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(assets[0].keys()))
    w.writeheader()
    w.writerows(assets)

# ---------- Knowledge Articles ----------
kb_templates = [
    ("Diagnosing abnormal vibration on rotating equipment", "Pump", "Troubleshooting",
     "Check for cavitation, bearing wear, and misalignment before disassembly. Vibration above 7 mm/s RMS on a Safety-Critical asset should trigger immediate inspection."),
    ("Responding to high differential pressure across scrubber media", "Scrubber", "Troubleshooting",
     "Rising differential pressure usually indicates media fouling or plugging. Confirm inlet gas flow rate against design basis before recommending media replacement."),
    ("Valve stem leak first response", "Valve", "Safety",
     "Any reported leak on a valve handling regulated or hazardous media should be treated as a potential release event until confirmed otherwise. Escalate immediately."),
    ("Heat exchanger fouling and efficiency loss", "Heat Exchanger", "Troubleshooting",
     "A steady rise in outlet temperature differential over weeks points to fouling. A sudden shift in a single shift points to a tube leak instead."),
    ("Reactor temperature excursion response", "Reactor", "Safety",
     "Temperature excursions outside setpoint band require verifying whether the deviation is on .PV (actual process value) or .SP (setpoint) before dispatching a technician."),
    ("Compressor surge troubleshooting", "Compressor", "Troubleshooting",
     "Surge events are frequently caused by downstream valve position changes. Review recent control system logic changes before assuming a mechanical fault."),
    ("Storage tank level sensor drift", "Storage Tank", "Troubleshooting",
     "Level sensor drift is common after long maintenance intervals. Cross-check against a secondary level indication before dispatching."),
    ("Transmitter calibration failure patterns", "Sensor/Transmitter", "Troubleshooting",
     "Repeated calibration failures on the same transmitter model may indicate a batch defect rather than an installation issue."),
    ("Chemical odor near process equipment protocol", "Valve", "Safety",
     "Any reported chemical odor, regardless of equipment type, is a Safety-Critical case by default and must page the on-call safety engineer, not the standard dispatch queue."),
    ("Pump seal failure early indicators", "Pump", "Troubleshooting",
     "Minor seal weeping is a precursor to failure. Recommend scheduled seal replacement rather than emergency dispatch if flow rate is stable."),
    ("Scrubber caustic feed interruption", "Scrubber", "Safety",
     "Loss of caustic feed to a scrubber treating acid gas is a Safety-Critical condition and should escalate regardless of reported severity language."),
    ("Reactor pressure relief valve chatter", "Reactor", "Safety",
     "Audible chatter on a pressure relief valve indicates the valve is near its set pressure and should never be deferred to routine maintenance."),
]

with open("Knowledge_Articles.csv", "w", newline="") as f:
    fieldnames = ["KA_Key__c", "Title__c", "Equipment_Type__c", "Category__c", "Body__c"]
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    for i, (title, eq, cat, body) in enumerate(kb_templates, start=1):
        w.writerow({
            "KA_Key__c": f"KB-{i:04d}",
            "Title__c": title,
            "Equipment_Type__c": eq,
            "Category__c": cat,
            "Body__c": body,
        })

# ---------- Historical Work Orders (closed, feeds root-cause RAG) ----------
resolutions = [
    "Replaced worn bearing, realigned coupling, vibration returned to baseline.",
    "Cleaned scrubber media bed, differential pressure returned to design range.",
    "Tightened valve packing, no further leak reported after 30-day follow-up.",
    "Identified tube leak, isolated affected pass, scheduled bundle replacement.",
    "Confirmed deviation was on .SP due to a logic error; corrected DCS configuration.",
    "Adjusted downstream valve sequencing to eliminate surge condition.",
    "Replaced level transmitter, cross-checked against radar gauge.",
    "Recalibrated transmitter, flagged for batch review with vendor.",
    "Replaced mechanical seal proactively before failure occurred.",
    "Restored caustic feed pump, verified scrubber pH within 15 minutes.",
]

historical_wo = []
for i in range(1, 21):
    asset = random.choice(assets)
    historical_wo.append({
        "WO_Key__c": f"HWO-{i:04d}",
        "Asset_Key__c": asset["Asset_Key__c"],
        "Status__c": "Completed",
        "Priority__c": random.choice(["High", "Medium", "Low"]),
        "Resolution_Notes__c": random.choice(resolutions),
        "Closed_Date__c": (datetime.now() - timedelta(days=random.randint(10, 500))).strftime("%Y-%m-%d"),
    })

with open("Historical_Work_Orders.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(historical_wo[0].keys()))
    w.writeheader()
    w.writerows(historical_wo)

# ---------- Incoming Cases (to be triaged by the agent) ----------
normal_descriptions = [
    "Operator reports unusual vibration on {name} during routine rounds.",
    "Differential pressure trending up on {name} over the past week.",
    "Minor leak observed at {name} packing gland, no visible spray.",
    "Outlet temperature on {name} has drifted 8 degrees above normal over 3 shifts.",
    "Recurring high-high alarm on {name}, unclear if real or instrument fault.",
    "Noise complaint from operators near {name}, unclear source.",
    "Level indication on {name} disagrees with local gauge glass.",
    "Routine PM overdue on {name}, requesting inspection window.",
    "Intermittent trip on {name}, resets on its own after a few minutes.",
    "Corrosion visible on {name} housing, requesting assessment.",
]

safety_descriptions = [
    "Strong chemical odor reported near {name}, operators pulled back as precaution.",
    "Visible release/spray observed at {name}, area evacuated pending assessment.",
    "Pressure relief valve on {name} audibly chattering, high concern.",
    "Loss of caustic feed to {name}, downstream pH dropping fast.",
    "H2S alarm activated in the vicinity of {name}.",
]

origins = ["Phone", "Web", "Email", "Mobile Field App"]

cases = []
case_id = 1
for _ in range(90):
    asset = random.choice(assets)
    desc = random.choice(normal_descriptions).format(name=asset["Name"])
    cases.append({
        "Case_Key__c": f"CS-{case_id:05d}",
        "Subject": desc.split(",")[0][:80],
        "Description": desc,
        "Asset_Key__c": asset["Asset_Key__c"],
        "Origin": random.choice(origins),
        "Status": "New",
        "Safety_Flag__c": "FALSE",
        "CreatedDate": (datetime.now() - timedelta(days=random.randint(0, 30), hours=random.randint(0,23))).strftime("%Y-%m-%dT%H:%M:%S"),
    })
    case_id += 1

# sprinkle in safety-critical cases (~10%) to give the future Safety Escalation Agent real signal to catch
for _ in range(10):
    asset = random.choice(assets)
    desc = random.choice(safety_descriptions).format(name=asset["Name"])
    cases.append({
        "Case_Key__c": f"CS-{case_id:05d}",
        "Subject": desc.split(",")[0][:80],
        "Description": desc,
        "Asset_Key__c": asset["Asset_Key__c"],
        "Origin": random.choice(origins),
        "Status": "New",
        "Safety_Flag__c": "FALSE",  # intentionally left FALSE — this is what your Safety Agent should catch
        "CreatedDate": (datetime.now() - timedelta(days=random.randint(0, 30), hours=random.randint(0,23))).strftime("%Y-%m-%dT%H:%M:%S"),
    })
    case_id += 1

random.shuffle(cases)

with open("Cases.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(cases[0].keys()))
    w.writeheader()
    w.writerows(cases)

print(f"Assets: {len(assets)}")
print(f"Knowledge Articles: {len(kb_templates)}")
print(f"Historical Work Orders: {len(historical_wo)}")
print(f"Cases: {len(cases)} (10 are safety-critical, unflagged on purpose)")
