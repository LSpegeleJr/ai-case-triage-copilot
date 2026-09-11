"""
Sprint 2, US-204: Accuracy metrics log

Compares your hand-labeled sample (cases_to_label.csv) against the Triage
Agent's real output (output/triage_results.csv), and APPENDS a dated row to
output/accuracy_log.csv — a running history, rather than a one-off number
that goes stale the moment the prompt changes again.

Run this after every triage_agent.py re-run where you've changed the
system prompt, so you can see whether accuracy actually improved,
regressed, or stayed flat — not just assume it did.

Usage (from the project root):
  python scripts/validate_accuracy.py
  python scripts/validate_accuracy.py "after trip + leak/alarm fixes"
(the optional argument is a short note describing what changed, saved
alongside the numbers so the log is self-explanatory later)
"""

import csv
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LABELED_FILE = PROJECT_ROOT / "cases_to_label.csv"
RESULTS_FILE = PROJECT_ROOT / "output" / "triage_results.csv"
LOG_FILE = PROJECT_ROOT / "output" / "accuracy_log.csv"


def main():
    # note = sys.argv[1] if len(sys.argv) > 1 else ""
        # A CONDITIONAL EXPRESSION — sys.argv is Python's built-in list of
        # everything typed on the command line; sys.argv[0] is always the
        # script's own path, so sys.argv[1] would be an optional note
        # typed after it, e.g. "python scripts/validate_accuracy.py
        # my note" makes sys.argv[1] equal to "my note"
        # len(sys.argv) > 1 checks whether anything was actually typed
        # there at all — if not, note becomes an empty string instead of
        # crashing trying to read an index that doesn't exist
    note = sys.argv[1] if len(sys.argv) > 1 else ""

    # Reads your hand-labeled judgments into a list of dictionaries
    # with open(LABELED_FILE, newline="", encoding="utf-8") as f:
        # newline="" avoids extra blank rows CSV files can otherwise
        # produce on Windows; encoding="utf-8" ensures any special
        # character writes and reads correctly regardless of the
        # operating system's own default encoding
    with open(LABELED_FILE, newline="", encoding="utf-8") as f:
        labeled = list(csv.DictReader(f))

    # ai_results = {row["Case_Key__c"]: row for row in csv.DictReader(f)}
        # A DICT COMPREHENSION — builds a dictionary in one line, keyed by
        # each row's Case_Key__c, so a specific Case's AI result can be
        # looked up directly (ai_results["CS-00098"]) instead of looping
        # through the whole list every time one is needed
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        ai_results = {row["Case_Key__c"]: row for row in csv.DictReader(f)}

    urgency_matches = 0
    category_matches = 0
    compared = 0

    # for case in labeled:
        # Walks through every one of your hand-labeled Cases, one at a time
    for case in labeled:
        # ai = ai_results.get(case["Case_Key__c"])
            # Looks up this Case's real AI result by key; .get(...)
            # returns None instead of crashing if this specific Case
            # somehow isn't in the AI results at all
        ai = ai_results.get(case["Case_Key__c"])

        # if ai is None or ai.get("status", ai.get("urgency")) == "ERROR" or ai["urgency"] == "ERROR":
            # Excludes a Case from the comparison entirely if there's no
            # valid AI classification to compare your label against
            # ai is None
                # this Case's key wasn't found in ai_results at all
            # ai.get("status", ai.get("urgency")) == "ERROR"
                # checks the "status" column if present, falling back to
                # checking "urgency" itself if there's no "status" column
                # in this particular results file — handles both older
                # and newer triage_agent.py output shapes
            # ai["urgency"] == "ERROR"
                # a direct fallback check on urgency itself
            # continue
                # skips straight to the next Case in the loop, without
                # counting this one in urgency_matches, category_matches,
                # or compared at all
        if ai is None or ai.get("status", ai.get("urgency")) == "ERROR" or ai["urgency"] == "ERROR":
            continue  # excluded — no valid classification to compare against

        compared += 1

        # if case["My_Urgency"].strip().lower() == ai["urgency"].strip().lower():
            # .strip() removes any accidental leading/trailing whitespace;
            # .lower() makes the comparison case-insensitive, so "High"
            # and "high" still count as a match — guards against small
            # formatting differences between your typed label and the
            # AI's exact output without treating them as a real disagreement
        if case["My_Urgency"].strip().lower() == ai["urgency"].strip().lower():
            urgency_matches += 1
        if case["My_Category"].strip().lower() == ai["category"].strip().lower():
            category_matches += 1

    # total_cases = len(ai_results)
        # The full count of AI results, regardless of whether each one
        # happens to be in your smaller hand-labeled sample — used below
        # to calculate the error rate across the WHOLE run, not just the
        # sampled subset
    total_cases = len(ai_results)

    # error_count = sum(1 for r in ai_results.values() if r.get("status", "OK") != "OK")
        # sum(1 for r in ... if ...) is a common Python idiom for counting
        # how many items match a condition — for every result r whose
        # status ISN'T "OK", add 1; sum() then totals all those 1s
        # r.get("status", "OK") defaults to "OK" if this particular
        # results file has no "status" column at all, so older-format
        # files don't get miscounted as all-errors
    error_count = sum(1 for r in ai_results.values() if r.get("status", "OK") != "OK")

    # urgency_accuracy = round(100 * urgency_matches / compared, 1) if compared else 0
        # A CONDITIONAL EXPRESSION guarding against DIVIDING BY ZERO — if
        # compared is 0 (nothing was actually comparable), skip the
        # division entirely and use 0 instead of crashing
        # round(..., 1) rounds the resulting percentage to one decimal place
    urgency_accuracy = round(100 * urgency_matches / compared, 1) if compared else 0
    category_accuracy = round(100 * category_matches / compared, 1) if compared else 0
    error_rate = round(100 * error_count / total_cases, 1) if total_cases else 0

    # log_exists = LOG_FILE.exists()
        # Checks whether accuracy_log.csv already exists BEFORE opening it
        # — needed because opening a file in "a" (append) mode doesn't
        # tell you whether it was already there, and we only want to
        # write a header row the very first time this file is created
    log_exists = LOG_FILE.exists()

    # with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        # "a" mode: APPEND — adds new content to the end of the file
        # without erasing whatever's already there, unlike "w" mode
        # (write) used elsewhere in this project, which would erase the
        # entire run history every time this script executes
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "sample_size", "urgency_accuracy_pct",
            "category_accuracy_pct", "error_rate_pct", "notes",
        ])
        # if not log_exists: writer.writeheader()
            # Only writes the column-header row if the file DIDN'T already
            # exist a moment ago — otherwise, every single run would add
            # ANOTHER header row into the middle of the growing log
        if not log_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "sample_size": compared,
            "urgency_accuracy_pct": urgency_accuracy,
            "category_accuracy_pct": category_accuracy,
            "error_rate_pct": error_rate,
            "notes": note,
        })

    print(f"Urgency accuracy:  {urgency_matches}/{compared} = {urgency_accuracy}%")
    print(f"Category accuracy: {category_matches}/{compared} = {category_accuracy}%")
    print(f"Error rate (full {total_cases}-case run): {error_count}/{total_cases} = {error_rate}%")
    print(f"Logged to {LOG_FILE}")


if __name__ == "__main__":
    main()
