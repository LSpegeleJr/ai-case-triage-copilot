"""
Sprint 4, US-401/402/403/404: Safety Escalation Agent

For each Case, checks whether it represents an ACTIVE safety hazard requiring
immediate escalation — a narrower, more focused question than the Triage
Agent's multi-category classification. Sets Safety_Flag__c (True or False,
for every Case — not just the ones that get flagged) and appends an audit
entry to AI_Reasoning_Log__c recording the decision and why, so every check
is auditable, not just the positive hits.

DELIBERATELY CALIBRATED TOWARD HIGH RECALL: the acceptance criteria targets
catching all 10 deliberately-seeded safety-critical Cases from Sprint 1,
accepting some false positives as an acceptable trade-off — a missed hazard
risks real harm, a false alarm costs a few minutes of a safety engineer's
attention checking something that turns out fine.

RETRIEVAL: unlike the Root Cause Agent, this doesn't need equipment-type or
asset-specific retrieval — it loads the small, fixed set of Safety-category
Knowledge Articles ONCE and shows all of them to every Case, since a hazard
pattern (a leak, a chemical odor, a relief valve chattering) is worth
checking against regardless of which specific equipment is involved.

Usage:
  python agents/safety_agent.py            # full 100-case run
  python agents/safety_agent.py --sample    # only the 20 hand-labeled cases,
                                             # cheap way to test a prompt tweak
"""

import csv
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from salesforce.client import get_connection, update_case_by_key, append_to_case_field

load_dotenv()
client = Anthropic()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KB_FILE = PROJECT_ROOT / "data" / "Knowledge_Articles.csv"
CASES_FILE = PROJECT_ROOT / "data" / "Cases.csv"
SAMPLE_FILE = PROJECT_ROOT / "cases_to_label.csv"
OUTPUT_FILE = PROJECT_ROOT / "output" / "safety_results.csv"

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2

# Defines the exact structure Claude must respond within — no free-text to parse,
# nothing that can come back almost-but-not-quite valid. "required" lists which
# fields must always be present; "type": "boolean" for is_safety_critical means
# the API itself enforces a true/false answer, not a string that happens to say
# "true". This schema is a plain yes/no decision plus reasoning, not a graded
# score — the question here is binary by design: does this Case need immediate
# escalation or not.
FLAG_SAFETY_CASE_TOOL = {
    "name": "flag_safety_case",
    "description": "Decide whether a maintenance Case represents an active safety hazard requiring immediate escalation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_safety_critical": {
                "type": "boolean",
                "description": "True if this Case should be flagged for immediate safety escalation, False otherwise.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentences explaining the decision, citing a specific hazard pattern by name if one applies.",
            },
        },
        "required": ["is_safety_critical", "reasoning"],
    },
}

SYSTEM_PROMPT = """You are a safety escalation reviewer for an industrial equipment maintenance \
team. Your ONLY job is deciding whether a Case represents an ACTIVE safety hazard requiring \
immediate escalation outside normal maintenance queues — not diagnosing root cause, not \
assigning routine priority, just this one yes/no call.

Err deliberately toward flagging when uncertain: a false alarm costs a few minutes of a safety \
engineer's attention: a missed hazard risks real harm. If a Case is genuinely ambiguous between \
"probably fine" and "possible hazard," flag it.

Known hazard patterns from this team's own safety knowledge base:
{safety_kb_block}

Use the flag_safety_case tool to record your decision."""


def load_safety_kb() -> str:
    # Runs ONCE, at the start of the script — not once per Case, since this set of
    # articles never changes between Cases.

    # Reads the KB CSV into a list of dictionaries
    # with open(KB_FILE, newline="", encoding="utf-8") as f:
        # newline="" avoids extra blank rows CSV files can otherwise produce on
        # Windows; encoding="utf-8" ensures any special character in an
        # article's text writes and reads correctly regardless of the
        # operating system's own default encoding
    # articles = list(csv.DictReader(f))
        # csv.DictReader(f) turns each CSV row into one dictionary keyed by
        # column header; list(...) collects all of them into one list
    with open(KB_FILE, newline="", encoding="utf-8") as f:
        articles = list(csv.DictReader(f))

    # a
        # This is the OUTPUT EXPRESSION — the whole article dictionary itself gets
        # included in the resulting list for each match, unchanged
    # for a in articles
        # Loops through every article dictionary in the articles list, one at a time
    # if a["Category__c"] == "Safety"
        # Filter: only keep this article if its Category__c value is exactly
        # "Safety" — Troubleshooting-category articles get discarded here, since
        # they're not relevant to this agent's specific job
    safety_articles = [a for a in articles if a["Category__c"] == "Safety"]

    # A LIST COMPREHENSION — builds one formatted text line per safety article
    # f"- \"{a['Title__c']}\": {a['Body__c']}"
        # An f-string — combines each article's title and body with a leading
        # "- " and a colon between them
    # for a in safety_articles
        # Loops through the filtered safety_articles list built above, one
        # article at a time
    lines = [f"- \"{a['Title__c']}\": {a['Body__c']}" for a in safety_articles]

    # "\n"
        # The join SEPARATOR — a real line-break character
    # .join(lines)
        # Glues every string in the lines list together, with "\n" inserted
        # between each one, producing one multi-line block of text
    # return
        # Sends that combined text back to whatever called load_safety_kb() —
        # that's main(), which stores it for use in every Case's system prompt
    return "\n".join(lines)


def check_safety(case_text: str, safety_kb_block: str) -> dict:
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
            # A .format() SUBSTITUTION — different mechanism than an f-string
            # SYSTEM_PROMPT
                # Defined above, contains a literal placeholder written with
                # SINGLE curly braces, {safety_kb_block} — since SYSTEM_PROMPT
                # is a plain string, not an f-string, single braces don't
                # trigger any special Python behavior on their own
            # .format(safety_kb_block=safety_kb_block)
                # Looks for {safety_kb_block} specifically inside the string
                # and replaces it with the real KB text passed in here as a
                # named argument
                # Why .format() instead of an f-string here: SYSTEM_PROMPT is
                # defined once, above, before load_safety_kb() has even run
                # yet — an f-string bakes in its substitutions the instant
                # it's written, so it couldn't wait to be filled in later the
                # way .format() can
            message = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=400,
                system=SYSTEM_PROMPT.format(safety_kb_block=safety_kb_block),
                # tools=[FLAG_SAFETY_CASE_TOOL]
                    # Makes the FLAG_SAFETY_CASE_TOOL schema (defined near the
                    # top of this file) available to Claude for this call. A
                    # list, since the API supports offering multiple tools at
                    # once — here there's only ever the one
                # tool_choice={"type": "tool", "name": "flag_safety_case"}
                    # Forces Claude to actually USE this tool rather than
                    # leaving it optional — without this, Claude could still
                    # reply with plain text instead
                    # "name": "flag_safety_case" must match the "name" field
                    # inside FLAG_SAFETY_CASE_TOOL exactly, or the API rejects
                    # the request
                # messages=[{"role": "user", "content": f"Case:\n{case_text}"}]
                    # "role" and "content" are required keys dictated by the
                    # API's own schema — "user" specifically means this message
                    # is coming from the human/system asking the question, as
                    # opposed to "assistant", which would be Claude's own prior
                    # reply in a longer conversation
                    # f"Case:\n{case_text}"
                        # case_text was passed into this function as a
                        # parameter — this just adds a plain "Case:" label and
                        # a line break in front of it
                tools=[FLAG_SAFETY_CASE_TOOL],
                tool_choice={"type": "tool", "name": "flag_safety_case"},
                messages=[{"role": "user", "content": f"Case:\n{case_text}"}],
            )
            tool_use_block = next(b for b in message.content if b.type == "tool_use")
            result = tool_use_block.input

            # Even though the tool schema marks both fields "required," that's
            # a hint to the model, not something enforced mid-generation — if
            # the response gets cut off before both fields finish (e.g. a long
            # explanation pushing against the max_tokens limit), result could
            # come back missing one. Checking here, with the real result dict
            # still in hand, means the error shows exactly what WAS returned.
            # Raising inside this try block means it's caught by the except
            # below like any other failure, so an incomplete response gets a
            # real retry attempt too, rather than crashing immediately with an
            # unhelpful KeyError from somewhere else entirely.
            if "is_safety_critical" not in result or "reasoning" not in result:
                raise ValueError(f"Incomplete tool response, missing required field(s): {result}")

            return result
        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def main():
    safety_kb_block = load_safety_kb()

    # Always reads the FULL Case record from CASES_FILE, not from
    # cases_to_label.csv directly — cases_to_label.csv only carries the fields
    # needed for hand-labeling (Case_Key__c, Subject, Description, etc.), not
    # every field a Case has, so reading it directly as the case data could
    # crash later if this agent ever needed a field it doesn't include
    with open(CASES_FILE, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))

    # if "--sample" in sys.argv:
        # Checks whether the exact text "--sample" was typed on the command
        # line — sys.argv is Python's built-in list of everything typed, e.g.
        # running "python agents/safety_agent.py --sample" makes sys.argv equal
        # to ['agents/safety_agent.py', '--sample']
    if "--sample" in sys.argv:
        # Opens cases_to_label.csv specifically
        with open(SAMPLE_FILE, newline="", encoding="utf-8") as f:
            # A SET COMPREHENSION — builds a set (an unordered collection with
            # no duplicates) in one line, using curly braces instead of square
            # brackets
            # row["Case_Key__c"]
                # Reads just the Case_Key__c value out of each row
            # for row in csv.DictReader(f)
                # Loops through every row of cases_to_label.csv
            # Result: sample_keys ends up holding every Case_Key__c value from
            # cases_to_label.csv, collected into one set. A set (not a list) is
            # used here specifically because checking "is this key in the set"
            # (next line) is faster than checking "is this key in a list", and
            # we only care about membership here, not order or duplicates
            sample_keys = {row["Case_Key__c"] for row in csv.DictReader(f)}

        # A LIST COMPREHENSION — filters the full 100-Case list down to just
        # the ones in the labeled sample
        # c for c in cases
            # Walks through every full Case dictionary already loaded above
        # if c["Case_Key__c"] in sample_keys
            # Keeps only the ones whose Case_Key__c appears in sample_keys
        # Result: cases still holds FULL Case dictionaries (every field
        # intact), just narrowed down to the labeled subset instead of all 100
        cases = [c for c in cases if c["Case_Key__c"] in sample_keys]

        # An f-string confirming --sample mode is active and how many Cases
        # actually matched — len(cases) reflects the now-filtered list, not
        # the original 100
        print(f"--sample mode: running only {len(cases)} labeled cases (cheaper than the full 100)\n")

    sf = get_connection()

    results = []
    flagged_count = 0
    failed = 0

    for i, case in enumerate(cases, start=1):
        case_key = case["Case_Key__c"]
        case_text = f"Subject: {case['Subject']}\nDescription: {case['Description']}"

        try:
            result = check_safety(case_text, safety_kb_block)

            # An f-string building the audit-log text for THIS Case
            # {result['is_safety_critical']}
                # Substitutes in the True/False decision
            # {result['reasoning']}
                # Substitutes in Claude's own explanation for that decision
            # Runs the same way regardless of what is_safety_critical came back
            # as — US-404 asks for every decision to be auditable, not just the
            # positive hits
            reasoning_entry = f"[Safety Agent] Flagged: {result['is_safety_critical']}. {result['reasoning']}"

            # Writes Safety_Flag__c back to this Case's real Salesforce record
            # "Safety_Flag__c": result["is_safety_critical"]
                # Safety_Flag__c is a Checkbox field in Salesforce —
                # simple_salesforce accepts a plain Python True/False for a
                # Checkbox field directly, no conversion needed
                # Writes to EVERY Case processed, setting Safety_Flag__c to
                # False explicitly (not just leaving it untouched) when the
                # answer is "no" — confirms the Case was actually checked, not
                # simply never examined
                # This field is only ever written by this agent, so a plain
                # overwrite is correct here
            case_id = update_case_by_key(sf, case_key, {
                "Safety_Flag__c": result["is_safety_critical"],
            })
            # append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", reasoning_entry)
                # AI_Reasoning_Log__c is different — the Root Cause Agent (and
                # eventually the Dispatch Agent) also write to this same
                # field. A plain overwrite here would erase whatever those
                # agents already wrote for this Case. append_to_case_field
                # queries the field's current value first, then writes back
                # the existing content plus this new entry combined, so every
                # agent's reasoning survives together instead of only the
                # most recent write winning
            append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", reasoning_entry)

            # A CONDITIONAL EXPRESSION — an if/else written on one line
            # "FLAGGED" if result["is_safety_critical"] else "clear"
                # If is_safety_critical is True: flag_text becomes "FLAGGED"
                # If it's False: flag_text becomes "clear" instead
            flag_text = "FLAGGED" if result["is_safety_critical"] else "clear"

            # An f-string building the live progress line printed for this Case
            # [{i}/{len(cases)}]
                # i is this loop's current position (from enumerate, starting
                # at 1); len(cases) is the total count — together showing
                # progress like "[3/20]"
            # {case_key} -> {case_id}
                # Which Case, and the real Salesforce Id it was just updated on
            # {flag_text}
                # "FLAGGED" or "clear", from the line just above
            # {result['reasoning'][:80]}
                # [:80] is a SLICE — takes just the first 80 characters of the
                # reasoning text, so a long explanation doesn't make this one
                # printed line unreadably long
            print(f"[{i}/{len(cases)}] {case_key} -> {case_id}: {flag_text} — {result['reasoning'][:80]}...")

            # if result["is_safety_critical"]:
                # A boolean used directly as the if-condition — True runs the
                # indented line below, False skips it entirely
            # flagged_count += 1
                # Shorthand for flagged_count = flagged_count + 1 — increments
                # the running tally by 1 each time a Case actually gets
                # flagged, used only for the final summary line
            if result["is_safety_critical"]:
                flagged_count += 1

            # Marks this Case as having completed successfully — read again a
            # few lines below when building the results dict for this Case
            status = "OK"
        except Exception as e:
            # If check_safety() raised an error (after exhausting its own
            # retries), execution jumps here instead of continuing normally,
            # with e capturing whatever the actual error was
            print(f"[{i}/{len(cases)}] {case_key}: FAILED — {e}")
            failed += 1

            # A FALLBACK placeholder — since check_safety() never successfully
            # returned anything for this Case, there's no real decision to work
            # with. This manufactured dict keeps the same two keys
            # (is_safety_critical, reasoning) that a real result would have, so
            # the results.append(...) block below doesn't need special-case
            # handling for "no data"
            # None specifically (not True or False) marks this as "never
            # determined," distinct from a real "clear" decision
            result = {"is_safety_critical": None, "reasoning": ""}

            # f"ERROR: {e}"
                # Captures the ACTUAL exception message here, not a bare
                # "ERROR" placeholder — so reviewing the output later shows
                # the real reason a Case failed, not just that it did
            status = f"ERROR: {e}"

        # Builds one more dictionary and adds it onto the end of the results
        # list (started empty, above the loop) — one dictionary per Case,
        # collected into one list
        # is_safety_critical / reasoning
            # Pulled from result — either the real decision (success path) or
            # the None/empty placeholder set in the except block (failure
            # path) — either way, result always has both keys by this point
        results.append({
            "Case_Key__c": case_key,
            "is_safety_critical": result["is_safety_critical"],
            "reasoning": result["reasoning"],
            "status": status,
        })

    # OUTPUT_FILE.parent.mkdir(exist_ok=True)
        # .parent is the output/ folder itself (steps up one level from the
        # file path to its containing folder)
        # .mkdir(exist_ok=True) creates that folder if it doesn't already
        # exist, and does nothing (rather than erroring) if it does — a small
        # safety net in case someone deletes the output/ folder later
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
        writer = csv.DictWriter(f, fieldnames=["Case_Key__c", "is_safety_critical", "reasoning", "status"])
        writer.writeheader()
        writer.writerows(results)

    # A final confirmation line, printed once after the loop finishes entirely
    # len(results) - flagged_count - failed
        # Everything that isn't flagged and isn't failed — i.e. the "clear"
        # count — calculated rather than tracked with its own separate counter
    print(f"\nDone. {flagged_count} flagged, {len(results) - flagged_count - failed} clear, {failed} failed. Wrote local copy to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
