"""
Sprint 2, US-203: Write Triage Agent results back to Salesforce

Reads output/triage_results.csv (produced by triage_agent.py) and updates
each real Case record in Salesforce with AI_Priority__c, AI_Category__c, and
AI_Confidence__c — closing the loop between this script and the actual
Salesforce data, rather than leaving results sitting only in a local CSV.

Rows with status != "OK" (i.e. ones that errored during classification) are
skipped — there's no real classification to write back for those.

Setup: this script authenticates via OAuth 2.0 Client Credentials flow, the
same as every other agent — see .env.example for the three variables it
actually needs (SF_INSTANCE_URL, SF_CONSUMER_KEY, SF_CONSUMER_SECRET), set
via your Dev org's own External Client App. (An earlier, since-abandoned
plan used a username/password/security-token approach instead — Salesforce
blocks that flow by default on newer orgs, which is exactly why Client
Credentials replaced it. This note used to describe that old approach and
was never updated after the switch.)

Run from the project root: python agents/write_results_to_salesforce.py
"""

import csv
import sys
from pathlib import Path
from dotenv import load_dotenv

# Makes the sibling `salesforce/` package importable when this script is run
# directly (python agents/write_results_to_salesforce.py) rather than as
# part of an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from salesforce.client import get_connection, update_case_by_key

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = PROJECT_ROOT / "output" / "triage_results.csv"


def main():
    # Reads the Triage Agent's local CSV output into a list of dictionaries
    # with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        # newline="" avoids extra blank rows CSV files can otherwise
        # produce on Windows; encoding="utf-8" ensures any special
        # character in a classification writes and reads correctly
        # regardless of the operating system's own default encoding
    # rows = list(csv.DictReader(f))
        # csv.DictReader(f) turns each CSV row into one dictionary keyed
        # by column header; list(...) collects all of them into one list
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Authenticates once, before the loop starts, and reuses the same
    # connection for every row processed below
    sf = get_connection()

    updated = 0
    skipped = 0
    failed = 0

    # for i, row in enumerate(rows, start=1):
        # enumerate(rows, start=1) walks through the list one item at a
        # time while also handing back a counter alongside each item;
        # start=1 makes that counter begin at 1 instead of Python's
        # normal default of 0, purely so the progress printout reads
        # "[1/100]" instead of "[0/100]"
    for i, row in enumerate(rows, start=1):
        # if row["status"] != "OK":
            # status was set by triage_agent.py — "OK" for a real
            # classification, or "ERROR: ..." if that Case failed during
            # triage. Anything other than exactly "OK" means there's
            # nothing valid to write back for this row
        if row["status"] != "OK":
            print(f"[{i}/{len(rows)}] {row['Case_Key__c']}: SKIPPED (no valid classification — {row['status']})")
            skipped += 1
            # continue
                # Immediately jumps to the NEXT iteration of the loop,
                # skipping every line below for this row entirely — there's
                # no fields to build or Salesforce call to make for a Case
                # that never got classified
            continue

        # fields
            # The three values this script is actually responsible for
            # writing back — built fresh for each row, from that row's own
            # triage_results.csv columns
            # "AI_Confidence__c": int(row["confidence"])
                # CSV values are always plain text, even for something
                # that's really a number — int(...) converts the text
                # (e.g. "85") into a real Python integer before sending it
                # to Salesforce's Percent field
        fields = {
            "AI_Priority__c": row["urgency"],
            "AI_Category__c": row["category"],
            "AI_Confidence__c": int(row["confidence"]),
        }

        try:
            # update_case_by_key(sf, row["Case_Key__c"], fields)
                # Imported from salesforce/client.py — looks up the real
                # Salesforce Id for this Case_Key__c, then updates the
                # three fields built above on that actual record
                # case_id gets back the real Salesforce Id, used only in
                # the printed confirmation line below
            case_id = update_case_by_key(sf, row["Case_Key__c"], fields)
            print(f"[{i}/{len(rows)}] {row['Case_Key__c']} -> {case_id}: updated ({row['urgency']} / {row['category']} / {row['confidence']}%)")
            updated += 1
        except Exception as e:
            # If update_case_by_key() raised for any reason (a network
            # issue, a bad field value Salesforce rejected), execution
            # jumps here instead of crashing the whole script — this one
            # Case gets logged as failed, and the loop continues on to
            # the next row
            print(f"[{i}/{len(rows)}] {row['Case_Key__c']}: FAILED — {e}")
            failed += 1

    # A final confirmation line, printed once after the loop finishes entirely
    print(f"\nDone. {updated} updated, {skipped} skipped (no valid classification), {failed} failed.")


if __name__ == "__main__":
    main()
