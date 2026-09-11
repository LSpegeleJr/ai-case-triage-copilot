"""
Addendum: Live Pipeline Orchestrator (example / starting point)

Demonstrates converting this project from four independent batch scripts
into ONE chained pipeline that processes newly-arrived Cases as they come
in — reusing the exact same agent functions built in Sprints 2-5, without
duplicating any of that logic.

WHAT THIS CHANGES, AND WHAT IT DOESN'T:
- The actual reasoning in each agent (classify_case, propose_root_cause,
  check_safety, match_technician, draft_dispatch_note) is UNCHANGED —
  imported directly from the real agent files, not rewritten. This is only
  possible because each agent was already built as clean, importable
  functions from Sprint 2 onward, rather than logic buried inside a single
  script with no reusable pieces.
- What changes is HOW a Case gets found (querying Salesforce directly for
  unprocessed Cases, instead of reading a fixed local CSV) and the
  PROCESSING UNIT (one Case flows through all four stages immediately,
  instead of four separate full-batch passes).

TRIGGER MECHANISM — scheduled polling, deliberately, not Change Data
Capture or a Salesforce Flow webhook. See BUILD_GUIDE.md's Sprint 7
addendum for the full reasoning: polling reuses everything already built,
needs no new infrastructure, and gets Cases processed within minutes of
arriving — genuine real-time (via Platform Events, or a Flow pushing to a
hosted endpoint) is the right next step once minutes of latency actually
matters, not the first problem worth solving at this stage.

Run this on a schedule instead of by hand — e.g. every 5 minutes via
Windows Task Scheduler or a cron job — rather than a person remembering to
invoke it. Each run only processes whatever Cases have arrived and haven't
been triaged yet, not a fixed batch of 100.
"""

import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from salesforce.client import get_connection, update_case_by_key, append_to_case_field, create_work_order, get_case_by_key

# Reuses the EXACT SAME functions built for each agent — nothing about the
# actual classification/reasoning logic changes for live processing
from triage_agent import classify_case
from root_cause_agent import build_index, retrieve_context, format_context, propose_root_cause
from safety_agent import load_safety_kb, check_safety
from dispatch_agent import load_technicians, match_technician, draft_dispatch_note

load_dotenv()


def get_untriaged_cases(sf) -> list:
    """
    Finds Cases that have arrived but haven't been through the Triage
    Agent yet — this is what replaces reading from a static Cases.csv.
    AI_Priority__c being blank is our signal that a Case is new and
    unprocessed; Case_Key__c != null still excludes Salesforce's own
    out-of-box demo Cases, same reasoning as get_all_cases() in client.py.

    Also selects Asset.Asset_Key__c specifically: retrieve_context() (in
    root_cause_agent.py) expects case["Asset_Key__c"] as a flat field,
    matching the shape of a row from the original Cases.csv it was built
    against. A live SOQL query returns AssetId plus a nested Asset object
    instead — this function bridges that gap below, rather than needing
    to change retrieve_context() itself.
    """
    result = sf.query("""
        SELECT Id, Case_Key__c, Subject, Description, AssetId,
               Asset.Asset_Key__c, Asset.Equipment_Type__c
        FROM Case
        WHERE AI_Priority__c = null AND Case_Key__c != null
    """)
    cases = result["records"]

    # Bridges the SOQL relationship shape (case["Asset"]["Asset_Key__c"])
    # back to the flat shape (case["Asset_Key__c"]) every batch agent
    # function already expects — a Case with no Asset at all gets None,
    # same as retrieve_context() already handles for the batch scripts
    for case in cases:
        case["Asset_Key__c"] = case["Asset"]["Asset_Key__c"] if case["Asset"] else None

    return cases


def process_one_case(sf, case: dict, kb_index: tuple, safety_kb_block: str, technicians: list, assignment_counts: dict):
    """
    Chains all four agents for ONE Case, in sequence — this is the actual
    behavior change from Sprints 2-5: instead of each agent processing a
    full batch independently, one Case flows through Triage, Root Cause,
    Safety, and Dispatch before the pipeline moves on to the next Case.
    """
    case_key = case["Case_Key__c"]
    case_text = f"Subject: {case['Subject']}\nDescription: {case['Description']}"
    equipment_type = case["Asset"]["Equipment_Type__c"] if case["Asset"] else None

    # --- Stage 1: Triage Agent ---
    triage_result = classify_case(case["Subject"], case["Description"])
    update_case_by_key(sf, case_key, {
        "AI_Priority__c": triage_result["urgency"],
        "AI_Category__c": triage_result["category"],
        "AI_Confidence__c": triage_result["confidence"],
    })

    # --- Stage 2: Root Cause Agent ---
    # kb_index and safety_kb_block are both built ONCE, outside this
    # function, and passed in — re-loading Knowledge Articles or the
    # Safety KB from scratch for every single Case would be wasteful,
    # same reasoning as the original batch scripts loading their indexes
    # once before their own loops
    assets_by_key, kb_by_equipment_type, wo_by_asset_key = kb_index
    kb_matches, wo_matches, _ = retrieve_context(case, assets_by_key, kb_by_equipment_type, wo_by_asset_key)
    context_block, trace = format_context(kb_matches, wo_matches)
    rc_result = propose_root_cause(case_text, context_block)
    update_case_by_key(sf, case_key, {
        "AI_Root_Cause__c": rc_result["root_cause_hypothesis"],
        "AI_Root_Cause_Confidence__c": rc_result["confidence"],
    })
    append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", f"[Root Cause Agent] {trace} Hypothesis: {rc_result['root_cause_hypothesis']}")

    # --- Stage 3: Safety Escalation Agent ---
    safety_result = check_safety(case_text, safety_kb_block)
    update_case_by_key(sf, case_key, {"Safety_Flag__c": safety_result["is_safety_critical"]})
    append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", f"[Safety Agent] Flagged: {safety_result['is_safety_critical']}. {safety_result['reasoning']}")

    # --- Stage 4: Dispatch Agent ---
    # Re-queries the Case fresh — draft_dispatch_note() needs the
    # AI_Priority__c / AI_Category__c / AI_Root_Cause__c / Safety_Flag__c
    # values Stages 1-3 JUST wrote, which aren't in the original `case`
    # dict this function was called with
    fresh_case = get_case_by_key(sf, case_key)
    technician = match_technician(equipment_type, technicians, assignment_counts) if equipment_type else None
    note = draft_dispatch_note(fresh_case, technician)

    wo_fields = {
        "WO_Key__c": f"WO-LIVE-{case_key}",
        "Case__c": fresh_case["Id"],
        "Status__c": "Open",
        "Priority__c": fresh_case.get("AI_Priority__c") or "Medium",
    }
    if fresh_case["AssetId"]:
        wo_fields["Asset__c"] = fresh_case["AssetId"]
    if technician:
        wo_fields["Assigned_Technician__c"] = technician["Name"]
        assignment_counts[technician["Name"]] += 1

    create_work_order(sf, wo_fields)
    append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", f"[Dispatch Agent] {note}")

    return triage_result["urgency"], safety_result["is_safety_critical"], technician["Name"] if technician else "UNASSIGNED"


def main():
    sf = get_connection()

    # Loads every reference index ONCE per run of this script — not once
    # per Case — same reasoning as every batch agent script before it
    kb_index = build_index()
    safety_kb_block = load_safety_kb()
    technicians = load_technicians()
    assignment_counts = {tech["Name"]: 0 for tech in technicians}

    cases = get_untriaged_cases(sf)
    print(f"Found {len(cases)} untriaged Case(s) this run.")

    for case in cases:
        try:
            urgency, safety_flag, tech_label = process_one_case(sf, case, kb_index, safety_kb_block, technicians, assignment_counts)
            print(f"{case['Case_Key__c']}: {urgency}, safety={safety_flag}, dispatched to {tech_label}")
        except Exception as e:
            print(f"{case['Case_Key__c']}: FAILED — {e}")


if __name__ == "__main__":
    main()
