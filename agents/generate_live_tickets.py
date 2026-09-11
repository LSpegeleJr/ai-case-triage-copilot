"""
Addendum: Live Ticket Generator

Simulates real incoming tickets — creates NUM_TICKETS new Case records in
Salesforce (as if real people just submitted them), then immediately runs
the full four-agent pipeline (Triage -> Root Cause -> Safety -> Dispatch)
against them, reusing run_pipeline_live.py's own logic directly rather
than duplicating it. Refresh the Review Dashboard afterward to see these
tickets fully processed.

WHY CLAUDE WRITES EACH TICKET, RATHER THAN REUSING SPRINT 1'S FIXED
TEMPLATES: Sprint 1's synthetic data generator picks from a fixed list of
~15 phrasings — fine for bulk seed data, but a poor stand-in for "a real
person just submitted this," since it would just be replaying the same
wording every run. Generating fresh text each time is a better simulation
of genuinely new tickets arriving, and costs relatively little given this
only runs for a small batch (20), not hundreds.

Run with: python agents/generate_live_tickets.py
"""

import sys
import random
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from salesforce.client import get_connection

# Reuses the live pipeline's own functions directly — the exact same
# Triage -> Root Cause -> Safety -> Dispatch chain built for
# run_pipeline_live.py, not a second copy of that logic
from run_pipeline_live import process_one_case, get_untriaged_cases
from root_cause_agent import build_index
from safety_agent import load_safety_kb
from dispatch_agent import load_technicians

load_dotenv()
client = Anthropic()

NUM_TICKETS = 20
# Roughly the same ~10% ratio Sprint 1's seeded hazard cases used, so the
# Safety Agent has real, genuine hazards to catch in this live batch too
NUM_SAFETY_CRITICAL = 2

GENERATE_TICKET_TOOL = {
    "name": "generate_ticket",
    "description": "Write a realistic maintenance/incident report as a plant operator would actually type it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "A short subject line, under 80 characters."},
            "description": {"type": "string", "description": "1-2 sentences describing the issue, in a plant operator's own voice."},
        },
        "required": ["subject", "description"],
    },
}

NORMAL_PROMPT = """You are simulating a plant operator submitting a routine maintenance ticket \
about {equipment}. Write a realistic, varied report — a symptom like unusual vibration, a \
temperature or pressure drift, a minor leak, an intermittent trip, an overdue inspection, or \
similar. Keep it brief and technical, the way an operator would actually type it — not overly \
dramatic. Use the generate_ticket tool."""

SAFETY_PROMPT = """You are simulating a plant operator submitting an URGENT safety-related \
ticket about {equipment}. Write a realistic report describing an active hazard — a chemical \
odor, a visible release or spray, a pressure relief valve chattering, a loss of critical feed, \
or a toxic gas alarm. Keep it brief and urgent, the way an operator would actually type it under \
pressure. Use the generate_ticket tool."""


def generate_ticket_text(equipment_name: str, is_safety: bool) -> dict:
    # (SAFETY_PROMPT if is_safety else NORMAL_PROMPT).format(equipment=equipment_name)
        # A CONDITIONAL EXPRESSION picks which of the two system prompts to
        # use, based on whether this ticket should be a genuine hazard or a
        # routine issue; .format(equipment=...) then substitutes the real
        # equipment name into that prompt's {equipment} placeholder
    prompt = (SAFETY_PROMPT if is_safety else NORMAL_PROMPT).format(equipment=equipment_name)

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=200,
        system=prompt,
        tools=[GENERATE_TICKET_TOOL],
        tool_choice={"type": "tool", "name": "generate_ticket"},
        messages=[{"role": "user", "content": f"Write one ticket about {equipment_name}."}],
    )
    tool_use_block = next(b for b in message.content if b.type == "tool_use")
    return tool_use_block.input


def get_next_case_number(sf) -> int:
    # Finds the highest existing CS-##### number already in use, so new
    # tickets continue the sequence rather than colliding with it
    # sf.query("SELECT Case_Key__c FROM Case WHERE Case_Key__c != null ORDER BY Case_Key__c DESC LIMIT 1")
        # ORDER BY Case_Key__c DESC sorts text descending, which works
        # correctly here since every real Case_Key__c is the same
        # fixed-width format (CS-00001 through CS-00100) — LIMIT 1 then
        # takes just the highest one
    result = sf.query("SELECT Case_Key__c FROM Case WHERE Case_Key__c != null ORDER BY Case_Key__c DESC LIMIT 1")

    if result["totalSize"] == 0:
        return 1

    # last_key.split("-")[1]
        # "CS-00100".split("-") produces ["CS", "00100"]; [1] takes the
        # second piece, the number itself, still as text
    # int(...) + 1
        # converts that text to a real number and adds 1, so the very
        # next ticket continues the sequence rather than reusing a number
    last_key = result["records"][0]["Case_Key__c"]
    return int(last_key.split("-")[1]) + 1


def main():
    sf = get_connection()

    # Queries real Assets directly from Salesforce — local Assets.csv has
    # Asset_Key__c, but not the actual Salesforce Id a new Case's AssetId
    # lookup needs to point to
    assets = sf.query("SELECT Id, Name, Equipment_Type__c FROM Asset")["records"]

    start_number = get_next_case_number(sf)

    # random.sample(range(NUM_TICKETS), NUM_SAFETY_CRITICAL)
        # Picks NUM_SAFETY_CRITICAL DISTINCT random positions out of the
        # 20 tickets about to be generated, with no duplicates — those
        # positions are the ones that'll get the urgent safety prompt
        # instead of the routine one
    safety_indices = set(random.sample(range(NUM_TICKETS), NUM_SAFETY_CRITICAL))

    created_case_keys = []
    for i in range(NUM_TICKETS):
        asset = random.choice(assets)
        is_safety = i in safety_indices
        ticket = generate_ticket_text(asset["Name"], is_safety)
        case_key = f"CS-{start_number + i:05d}"

        # sf.Case.create({...})
            # Creates a brand new Case record directly — same mechanic as
            # create_work_order() in client.py, but called straight on the
            # standard Case object rather than through a shared helper,
            # since this is the only place in the whole project that ever
            # creates a new Case rather than updating an existing one
            # Deliberately leaves every AI_* field and Safety_Flag__c
            # unset — this is what marks these as genuinely untriaged,
            # exactly the condition get_untriaged_cases() looks for
        sf.Case.create({
            "Case_Key__c": case_key,
            "Subject": ticket["subject"],
            "Description": ticket["description"],
            "AssetId": asset["Id"],
            "Origin": random.choice(["Phone", "Web", "Email", "Mobile Field App"]),
            "Status": "New",
        })
        created_case_keys.append(case_key)
        tag = "SAFETY" if is_safety else "routine"
        print(f"[{i+1}/{NUM_TICKETS}] Created {case_key} ({tag}): {ticket['subject']}")

    print(f"\n{len(created_case_keys)} tickets created. Running the live pipeline against them now...\n")

    # Loads every reference index ONCE for this whole batch, same
    # reasoning as run_pipeline_live.py's own main() — not once per ticket
    kb_index = build_index()
    safety_kb_block = load_safety_kb()
    technicians = load_technicians()
    assignment_counts = {tech["Name"]: 0 for tech in technicians}

    # get_untriaged_cases(sf) finds every Case with AI_Priority__c still
    # blank — at this point in the project, that's exactly the tickets
    # just created above, and nothing else
    cases = get_untriaged_cases(sf)
    for case in cases:
        try:
            urgency, safety_flag, tech_label = process_one_case(sf, case, kb_index, safety_kb_block, technicians, assignment_counts)
            print(f"{case['Case_Key__c']}: {urgency}, safety={safety_flag}, dispatched to {tech_label}")
        except Exception as e:
            print(f"{case['Case_Key__c']}: FAILED — {e}")

    print(f"\nDone. Refresh the dashboard to see these {len(created_case_keys)} tickets fully processed.")


if __name__ == "__main__":
    main()
