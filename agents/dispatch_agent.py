"""
Sprint 5, US-501/502/503: Dispatch Agent

For each Case, reads its current AI-enriched fields from Salesforce (written
by the Triage, Root Cause, and Safety Escalation Agents), matches an
available technician whose skills cover the Case's equipment type, creates a
new Work_Order__c record, and appends a synthesized dispatch note to the
Case's AI_Reasoning_Log__c.

HYBRID DESIGN — deterministic matching + LLM narrative: technician matching
(US-501) is a plain constraint-satisfaction problem (does this technician's
skill list include the needed equipment type, and are they available) —
solved with ordinary Python, not an API call, since there's no ambiguity for
an LLM to usefully reason about here. Claude is used only for US-503's
actual value-add: synthesizing the Case's urgency, category, root cause
hypothesis, safety flag, and the matched technician into one coherent,
readable note for a human dispatcher to review.

"NOT AUTO-SENT" (US-503): the Work Order is created with Status__c="Open" —
a real record, but one sitting in an unstarted state pending a human
dispatcher's own review and action, not an automatic notification or
kickoff triggered by this script.

Usage:
  python agents/dispatch_agent.py            # full 100-case run
  python agents/dispatch_agent.py --sample    # only the 20 hand-labeled cases,
                                               # cheap way to test a prompt tweak
"""

import csv
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # sys.path is a list of folder paths — every time Python sees "import" or
    # "from", it searches these folders in order looking for a matching file
    # Path(__file__).resolve().parent.parent
        # __file__ is this script's own location; .resolve() makes it a full
        # absolute path; .parent.parent steps up two folders (agents/ -> the
        # project root)
    # str(...) converts that Path object into a plain text string, since
    # sys.path expects plain strings, not Path objects
    # .insert(0, ...) adds that folder at POSITION 0, the very front of the
    # list, so Python checks the project root before anywhere else
    # Why needed: this script lives in agents/, but salesforce/client.py
    # lives one folder up in a sibling folder — without this, the import
    # below would fail with "No module named 'salesforce'"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# from salesforce.client import get_connection, get_case_by_key, create_work_order, append_to_case_field
    # Pulls four specific functions out of salesforce/client.py — the same
    # shared module the other two agents use, so the actual
    # connection/query/create/append logic exists in one place, not
    # copy-pasted into every agent script
from salesforce.client import get_connection, get_case_by_key, create_work_order, append_to_case_field

load_dotenv()  # reads .env in the project root and loads ANTHROPIC_API_KEY (and the Salesforce credentials) into the environment
client = Anthropic()  # builds the object this script uses to talk to Claude; picks up ANTHROPIC_API_KEY automatically

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Each of these builds a full file path by joining PROJECT_ROOT with a
# subfolder and filename, using pathlib's "/" operator — same pattern as
# every other agent in this project
TECHNICIANS_FILE = PROJECT_ROOT / "data" / "Technicians.csv"
CASES_FILE = PROJECT_ROOT / "data" / "Cases.csv"
SAMPLE_FILE = PROJECT_ROOT / "cases_to_label.csv"
OUTPUT_FILE = PROJECT_ROOT / "output" / "dispatch_results.csv"

# MAX_RETRIES / RETRY_DELAY_SECONDS
    # Sizes the retry loop in draft_dispatch_note() below: MAX_RETRIES=2 means
    # 2 EXTRA attempts after the first try (3 total), with a 2-second pause
    # between attempts
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2

# DRAFT_NOTE_TOOL
    # Defines the exact structure Claude must respond within — no free-text
    # to parse, nothing that can come back almost-but-not-quite valid
    # "required": ["note"]
        # The one field Claude's response must always include
    # "type": "string"
        # The API enforces that "note" comes back as text, not some other
        # data type
DRAFT_NOTE_TOOL = {
    "name": "draft_dispatch_note",
    "description": "Draft a short dispatch note for a human dispatcher summarizing why this Case is being routed as it is.",
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "2-4 sentences: what's wrong, how urgent, who it's going to and why, and any safety concern.",
            },
        },
        "required": ["note"],
    },
}

# SYSTEM_PROMPT
    # The instructions handed to Claude before it sees any Case — sets the
    # task (draft a note for a human, not a message sent to the technician),
    # the tone (readable in five seconds), and what must be called out
    # (safety flag, no-technician situations)
    # Triple-quoted so the text can span multiple lines exactly as typed; the
    # backslash at the end of a line tells Python to ignore that line break,
    # so a sentence can wrap in the source without a literal line break
    # landing inside the string Claude receives
SYSTEM_PROMPT = """You are drafting a dispatch note for a human dispatcher reviewing a maintenance \
Case before it goes out. You're given the Case's details, the AI triage/root-cause findings \
already on file, and which technician has been matched to it (or that none was available).

Write a short, practical note a dispatcher could read in five seconds: what's wrong, how urgent, \
who's assigned and why they're a reasonable match, and call out plainly if this is safety-flagged \
or if no technician was available. This note is for human review before dispatch — not a message \
sent to the technician directly.

Use the draft_dispatch_note tool to record it."""


def load_technicians() -> list:
    # Reads the technician roster into a list of dictionaries
    # with open(TECHNICIANS_FILE, newline="", encoding="utf-8") as f:
        # newline="" avoids extra blank rows CSV files can otherwise produce
        # on Windows; encoding="utf-8" ensures any special character writes
        # and reads correctly regardless of the operating system's own
        # default encoding
    # techs = list(csv.DictReader(f))
        # csv.DictReader(f) turns each CSV row into one dictionary keyed by
        # column header; list(...) collects all of them into one list
    with open(TECHNICIANS_FILE, newline="", encoding="utf-8") as f:
        techs = list(csv.DictReader(f))

    # tech["Skills"].split(";")
        # Skills is stored as one semicolon-separated string per technician
        # (e.g. "Pump;Valve;Compressor") — .split(";") breaks that single
        # string into a real Python list (["Pump", "Valve", "Compressor"])
    # tech["Available"] == "True"
        # CSV values are always plain text, even for something that reads
        # like a boolean — the literal text "True" has to be compared
        # against the string "True", not the real Python value True
    # This loop MODIFIES each technician dictionary in place, replacing the
    # raw Skills string and Available string with a real list and real
    # boolean, so the rest of this file can work with proper Python types
    # instead of re-parsing these two fields every time they're needed
    for tech in techs:
        tech["Skills"] = tech["Skills"].split(";")
        tech["Available"] = tech["Available"] == "True"

    return techs


# def match_technician(equipment_type: str, technicians: list, assignment_counts: dict) -> dict | None:
    # dict | None is a UNION TYPE HINT — says this function returns either a
    # real dictionary (a matched technician) or Python's None (no match
    # found), preparing the caller to expect both possibilities
    # assignment_counts: a dictionary tracking how many Cases each
    # technician has already been assigned so far THIS RUN, keyed by
    # technician Name — passed in from main() so the count persists across
    # every call to this function, rather than resetting each time
def match_technician(equipment_type: str, technicians: list, assignment_counts: dict) -> dict | None:
    # A plain Python function — no API call, no LLM involved. Matching a
    # technician to an equipment type is a constraint check (does their
    # skill list include it, are they available), not something requiring
    # judgment an LLM would meaningfully improve on.

    # Finds every qualified, available technician for this equipment
    # type — not just the first one, so a later-listed but equally
    # qualified technician has a real chance of being picked
    # qualified = [tech for tech in technicians if equipment_type in tech["Skills"] and tech["Available"]]
        # A LIST COMPREHENSION — collects EVERY matching technician into a
        # list, rather than stopping at the first one found
        # This is the actual load-balancing fix: the earlier version
        # returned the first match immediately, which meant a technician
        # listed later in Technicians.csv could be fully qualified and
        # available, yet NEVER get picked, if everything they're skilled
        # at was also covered by someone listed above them — discovered
        # by noticing one technician (Alicia Fontaine) never appeared
        # once across a full 100-Case run
    qualified = [tech for tech in technicians if equipment_type in tech["Skills"] and tech["Available"]]

    # if not qualified: return None
        # An empty list is "falsy" in Python — "not qualified" is True when
        # the list has zero items in it. No qualified, available technician
        # exists for this equipment type at all, so there's nothing to pick
        # among — return None explicitly, same as the previous version did
    if not qualified:
        return None

    # Picks whichever qualified technician has been assigned the FEWEST
    # jobs so far this run — this is the actual load-balancing step
    # min(qualified, key=...)
        # finds whichever item in qualified produces the SMALLEST value
        # when run through the key function — here, each technician's
        # current assignment count
    # key=lambda tech: assignment_counts[tech["Name"]]
        # a LAMBDA is a small, unnamed function defined inline — this one
        # takes one technician dictionary and looks up their current
        # count in assignment_counts, using their Name as the key
        # this is what min() actually compares across every candidate,
        # rather than comparing the technician dictionaries themselves
        # (which have no obvious "smaller" or "larger")
    # If multiple technicians are tied for the fewest assignments so far,
    # min() returns the FIRST one it encounters in qualified — preserving
    # Technicians.csv's own ordering as a tiebreaker, only when there's an
    # actual tie to break rather than every single time
    return min(qualified, key=lambda tech: assignment_counts[tech["Name"]])


def draft_dispatch_note(case: dict, technician: dict | None) -> str:
    # Builds a readable technician name for the prompt, or a plain
    # fallback string if no technician was matched at all
    # technician_text = f"{technician['Name']}" if technician else "No qualified technician currently available"
        # A CONDITIONAL EXPRESSION — if technician is a real dict (not
        # None), use its Name; otherwise substitute the fallback string,
        # so the prompt always has something readable here regardless of
        # whether a match was found
    technician_text = f"{technician['Name']}" if technician else "No qualified technician currently available"

    # case.get('AI_Priority__c') or 'Not yet triaged'
        # .get(...) returns None if the key is missing or its value is
        # already None — "or 'Not yet triaged'" substitutes readable
        # fallback text in either case, so the prompt never shows a bare
        # "None" if an earlier agent hasn't run on this Case yet
    # This whole block is a triple-quoted f-string — every {...} gets
    # replaced with the real value from the case dictionary or the
    # technician_text built just above, producing one multi-line block of
    # readable text to send to Claude
    case_summary = f"""Case: {case['Subject']}
Description: {case['Description']}
AI Priority: {case.get('AI_Priority__c') or 'Not yet triaged'}
AI Category: {case.get('AI_Category__c') or 'Not yet triaged'}
AI Root Cause: {case.get('AI_Root_Cause__c') or 'Not yet analyzed'}
Safety Flagged: {case.get('Safety_Flag__c')}
Matched Technician: {technician_text}"""

    # last_error = None
        # Starts as Python's "nothing here yet" value. Gets overwritten with
        # the real exception every time an attempt below fails, so there's
        # something real to raise at the very end if every attempt fails
    last_error = None
    # for attempt in range(1, MAX_RETRIES + 2):
        # range(1, MAX_RETRIES + 2) with MAX_RETRIES=2 produces [1, 2, 3] —
        # three total attempts (the first try plus two retries). range()'s
        # second number is exclusive (stops before it), which is why "+2" is
        # needed to land on 3 actual attempts, not "+1"
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            # client.messages.create(...) — the actual network call to Claude
            # model: which Claude model answers
            # max_tokens: a hard ceiling on how long the reply is allowed to be
            # system: the instructions block defined above
            # tools=[DRAFT_NOTE_TOOL]: makes that schema available to Claude
            # for this call — a list, since the API supports offering
            # multiple tools at once, though here there's only ever the one
            # tool_choice={"type": "tool", "name": "draft_dispatch_note"}:
            # forces Claude to actually USE this tool rather than leaving it
            # optional — without this, Claude could reply with plain text
            # instead
            # messages=[{"role": "user", "content": case_summary}]: "role"
            # and "content" are required keys dictated by the API's own
            # schema — "user" means this message is coming from the
            # human/system asking the question; case_summary (built above)
            # is the actual text Claude sees
            message = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=300,
                system=SYSTEM_PROMPT,
                tools=[DRAFT_NOTE_TOOL],
                tool_choice={"type": "tool", "name": "draft_dispatch_note"},
                messages=[{"role": "user", "content": case_summary}],
            )
            # tool_use_block = next(b for b in message.content if b.type == "tool_use")
                # message.content is a list of content blocks; "b for b in
                # message.content if b.type == 'tool_use'" is a generator
                # expression scanning for the one whose type is "tool_use";
                # next(...) returns just the first match, rather than
                # collecting every match into a list
            tool_use_block = next(b for b in message.content if b.type == "tool_use")
            # .input pulls out that block's dictionary — here, {"note": "..."}
                # already a real Python dict, no json.loads() needed, since
                # tool-use hands back structured data directly
            result = tool_use_block.input

            # Even though the tool schema marks "note" required, that's a
            # hint to the model, not something enforced mid-generation — if
            # the response gets cut off before it finishes, result could
            # come back missing it. Checking here, with the real result dict
            # still in hand, means the error shows exactly what WAS
            # returned. Raising inside this try block means it's caught by
            # the except below like any other failure, so an incomplete
            # response gets a real retry attempt too.
            if "note" not in result:
                raise ValueError(f"Incomplete tool response, missing required field(s): {result}")

            # Sends just the note text back to whatever called
            # draft_dispatch_note() — that's main(), further down in this file
            return result["note"]
        except Exception as e:
            # Remembers the real error, then either waits and loops back
            # around to try again, or (once out of attempts) falls through
            # to raising it below
            last_error = e
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    # Only reached if EVERY attempt failed — raises the real, most recent
    # error rather than swallowing it, so main()'s own try/except is what
    # decides what happens next
    raise last_error


def main():
    technicians = load_technicians()

    # Builds a running tally of how many Cases each technician has been
    # assigned so far this run, starting everyone at zero
    # assignment_counts = {tech["Name"]: 0 for tech in technicians}
        # A DICT COMPREHENSION — for every technician in the roster,
        # creates an entry keyed by their Name, starting at 0 — including
        # Dave Kowalski (currently unavailable), so every technician has a
        # real entry here even before any assignments happen
        # This dictionary persists for the WHOLE run — created once here,
        # then passed into match_technician() on every Case, so the counts
        # keep accumulating across the entire loop rather than resetting
    assignment_counts = {tech["Name"]: 0 for tech in technicians}

    # Always reads the FULL Case record from CASES_FILE, not from
    # cases_to_label.csv directly — cases_to_label.csv only carries the
    # fields needed for hand-labeling (Case_Key__c, Subject, Description,
    # etc.), not every field a Case has, so reading it directly as the case
    # data could crash later if this agent ever needed a field it doesn't
    # include
    with open(CASES_FILE, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))

    # if "--sample" in sys.argv:
        # Checks whether the exact text "--sample" was typed on the command
        # line — sys.argv is Python's built-in list of everything typed,
        # e.g. running "python agents/dispatch_agent.py --sample" makes
        # sys.argv equal to ['agents/dispatch_agent.py', '--sample']
    if "--sample" in sys.argv:
        # Opens cases_to_label.csv and builds a SET of every Case_Key__c
        # value in it — a set (not a list) is used because checking "is
        # this key in the set" a few lines below is faster than checking
        # against a list, and only membership matters here, not order
        with open(SAMPLE_FILE, newline="", encoding="utf-8") as f:
            sample_keys = {row["Case_Key__c"] for row in csv.DictReader(f)}
        # Filters the full 100-Case list down to just the ones whose
        # Case_Key__c appears in sample_keys — cases still holds FULL Case
        # dictionaries afterward, just narrowed down to the labeled subset
        cases = [c for c in cases if c["Case_Key__c"] in sample_keys]
        print(f"--sample mode: running only {len(cases)} labeled cases (cheaper than the full 100)\n")

    # Authenticates to Salesforce once, before the loop starts, and reuses
    # this same connection object for every Case processed below
    sf = get_connection()

    # results / dispatched / unassigned / failed
        # results: an empty list, built up one dictionary per Case as the
        # loop runs, eventually written out to the local CSV at the end
        # dispatched / unassigned / failed: running counters, each starting
        # at 0, incremented as the loop progresses — used only for the
        # final summary line printed after the loop finishes
    results = []
    dispatched = 0
    unassigned = 0
    failed = 0

    # for i, case_row in enumerate(cases, start=1):
        # enumerate(cases, start=1) walks through the list one item at a
        # time while also handing back a counter alongside each item;
        # start=1 makes that counter begin at 1 instead of Python's normal
        # default of 0, purely so the progress printout reads "[1/100]"
        # instead of "[0/100]"
        # i catches that counter; case_row catches the actual dictionary for
        # that row
    for i, case_row in enumerate(cases, start=1):
        # Reads just this row's Case_Key__c value — used to query Salesforce
        # for the real, current record a few lines below
        case_key = case_row["Case_Key__c"]

        try:
            # get_case_by_key(sf, case_key)
                # Queries Salesforce directly for this Case's CURRENT state —
                # including AI_Priority__c, AI_Category__c, AI_Root_Cause__c,
                # and Safety_Flag__c, none of which exist in the local
                # Cases.csv, since those fields only got written by the
                # earlier agents' runs against the real Salesforce records
            case = get_case_by_key(sf, case_key)

            # case["Asset"]["Equipment_Type__c"] if case["Asset"] else None
                # case["Asset"] is None if this Case's AssetId was never
                # set — the conditional expression avoids crashing trying to
                # read a field off of nothing in that situation
            equipment_type = case["Asset"]["Equipment_Type__c"] if case["Asset"] else None

            # match_technician(equipment_type, technicians, assignment_counts) if equipment_type else None
                # Another conditional expression — only attempts to match a
                # technician if equipment_type actually has a real value; if
                # it's None (no Asset on this Case at all), technician is
                # set to None directly, skipping the match attempt entirely
                # rather than calling match_technician() with a meaningless
                # None argument
                # assignment_counts is the SAME dictionary built once at the
                # top of main() — passed in fresh on every Case so
                # match_technician() always sees the running totals from
                # every Case processed so far, not a blank dictionary
            technician = match_technician(equipment_type, technicians, assignment_counts) if equipment_type else None

            note = draft_dispatch_note(case, technician)

            # f"WO-{i:05d}"
                # An f-string with a FORMAT SPEC — :05d means "format this
                # number as at least 5 digits, padding with leading zeros if
                # shorter", so i=1 becomes "00001", i=100 becomes "00100"
                # A "WO-" prefix (not "HWO-", already used by the
                # historical/closed Work Orders from Sprint 1) so the two
                # sets of keys never collide
            wo_key = f"WO-{i:05d}"

            # wo_fields
                # The base set of fields every Work Order gets, regardless of
                # whether a technician or asset were found
                # "Priority__c": case.get("AI_Priority__c") or "Medium"
                    # Mirrors the Case's own urgency onto its Work Order; "or
                    # 'Medium'" substitutes a reasonable default only if the
                    # Case was somehow never triaged at all
            wo_fields = {
                "WO_Key__c": wo_key,
                "Case__c": case["Id"],
                "Status__c": "Open",
                "Priority__c": case.get("AI_Priority__c") or "Medium",
            }
            # Only include Asset__c and Assigned_Technician__c if they
            # actually have a real value — omitting a key entirely (rather
            # than setting it to None) avoids sending a lookup field an
            # explicit null when there's nothing meaningful to link
            if case["AssetId"]:
                wo_fields["Asset__c"] = case["AssetId"]
            if technician:
                wo_fields["Assigned_Technician__c"] = technician["Name"]

            # create_work_order(sf, wo_fields)
                # create_work_order is imported from salesforce/client.py —
                # sends wo_fields to Salesforce as a brand new Work_Order__c
                # record (not an update to an existing one, since this
                # object never existed for this Case before)
                # wo_id gets back the real Salesforce Id of the newly
                # created record, used in the printed confirmation line below
            wo_id = create_work_order(sf, wo_fields)

            # reasoning_entry = f"[Dispatch Agent] {note}"
                # Builds the audit-log text for this Case, tagging it with
                # which agent wrote it, same convention as the other two
                # agents' entries
            reasoning_entry = f"[Dispatch Agent] {note}"
            # append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", reasoning_entry)
                # AI_Reasoning_Log__c is written to by all three agents that
                # run before this one too — append_to_case_field queries the
                # field's current value first, then writes back the existing
                # content plus this new entry combined, so every agent's
                # reasoning survives together instead of only the most
                # recent write winning
            append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", reasoning_entry)

            # tech_label = technician["Name"] if technician else "UNASSIGNED"
                # A conditional expression, same pattern as technician_text
                # above — real technician name if one was matched, a plain
                # "UNASSIGNED" label otherwise, used for both the printed
                # line below and the row saved to results
            tech_label = technician["Name"] if technician else "UNASSIGNED"
            # An f-string building the live progress line printed for this
            # Case — [i/len(cases)] shows progress like "[3/20]",
            # note[:80] is a SLICE taking just the first 80 characters so a
            # long note doesn't make this one printed line unreadably long
            print(f"[{i}/{len(cases)}] {case_key} -> WO {wo_id} ({wo_key}): {tech_label} — {note[:80]}...")

            # if technician: dispatched += 1 else: unassigned += 1
                # A boolean used directly as the if-condition; += 1 is
                # shorthand for "add 1 to the current value" — increments
                # whichever counter applies, used only for the final
                # summary line
                # assignment_counts[technician["Name"]] += 1
                    # This is the actual load-balancing update — increments
                    # THIS specific technician's running count in the SAME
                    # dictionary match_technician() reads from, so the very
                    # next Case needing this equipment type sees an
                    # up-to-date count and can route to whoever's least busy
                    # so far, rather than always favoring the same person
            if technician:
                dispatched += 1
                assignment_counts[technician["Name"]] += 1
            else:
                unassigned += 1
            # Marks this Case as having completed successfully — read again
            # a few lines below when building the results dict for this Case
            status = "OK"
        except Exception as e:
            # If anything above raised (a failed Salesforce query, a
            # permanently-failed draft_dispatch_note() call, a failed
            # Work Order creation), execution jumps here instead of
            # continuing normally, with e capturing whatever the actual
            # error was
            print(f"[{i}/{len(cases)}] {case_key}: FAILED — {e}")
            failed += 1
            # A FALLBACK — since something failed partway through, there's
            # no guarantee wo_key/tech_label/note ever got assigned a real
            # value this iteration; setting all three to empty strings here
            # means the results.append(...) block below always has
            # something to write, regardless of which line failed
            wo_key, tech_label, note = "", "", ""
            # Captures the ACTUAL exception message here, not a bare "ERROR"
            # placeholder — so reviewing the output later shows the real
            # reason a Case failed, not just that it did
            status = f"ERROR: {e}"

        # Builds one more dictionary and adds it onto the end of the results
        # list (started empty, above the loop) — one dictionary per Case,
        # collected into one list
        results.append({
            "Case_Key__c": case_key,
            "WO_Key__c": wo_key,
            "Assigned_Technician__c": tech_label,
            "note": note,
            "status": status,
        })

    # OUTPUT_FILE.parent.mkdir(exist_ok=True)
        # .parent is the output/ folder itself; .mkdir(exist_ok=True) creates
        # that folder if it doesn't already exist, and does nothing (rather
        # than erroring) if it does — a small safety net in case someone
        # deletes the output/ folder later
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    # with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        # "w" mode: create the file if it doesn't exist, or erase existing
        # content if it does, so this run's results write in fresh
    # writer = csv.DictWriter(f, fieldnames=[...])
        # Turns dictionaries back into CSV rows, using fieldnames to decide
        # which columns to include and what order they appear in
    # writer.writeheader() / writer.writerows(results)
        # .writeheader() writes the column-name row first; .writerows(results)
        # then writes one row per dictionary in the results list
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Case_Key__c", "WO_Key__c", "Assigned_Technician__c", "note", "status"])
        writer.writeheader()
        writer.writerows(results)

    # A final confirmation line, printed once after the loop finishes entirely
    print(f"\nDone. {dispatched} dispatched, {unassigned} unassigned (no qualified technician), {failed} failed. Wrote local copy to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
