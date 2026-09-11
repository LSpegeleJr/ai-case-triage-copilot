"""
Sprint 3, US-301/302/303/304: Root Cause Agent

For each Case, retrieves relevant Knowledge Articles and historical Work
Order resolutions for that Case's Asset, then asks Claude to propose a root
cause hypothesis with its own confidence score — separate from the Triage
Agent's urgency/category confidence. Writes AI_Root_Cause__c,
AI_Root_Cause_Confidence__c, and an appended entry in AI_Reasoning_Log__c
(showing what was retrieved, for the human reviewer to see WHY the agent
concluded what it did) back to the real Case record in Salesforce.

RETRIEVAL APPROACH: structured filtering by Equipment_Type__c and
Asset_Key__c, not embeddings-based semantic search. With only ~32 total
documents (12 Knowledge Articles + 20 historical Work Orders), and given
both already carry a clean Equipment_Type__c field to filter on, a full
embeddings pipeline would add real cost, a new dependency, and complexity
for no practical retrieval-quality gain at this corpus size.

Usage:
  python agents/root_cause_agent.py            # full 100-case run
  python agents/root_cause_agent.py --sample    # only the 20 hand-labeled cases,
                                                 # cheap way to test a prompt tweak
"""

import csv
import sys
import time
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

# Makes the sibling salesforce/ package importable when this script is run
# directly, since it lives in a different folder (agents/) than salesforce/
# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Path(__file__).resolve().parent.parent
        # __file__ is this script's own location; .resolve() makes it a
        # full absolute path; .parent.parent steps up two folders
        # (agents/ -> the project root)
    # str(...) converts that Path object into a plain text string, since
    # sys.path expects plain strings, not Path objects
    # .insert(0, ...) adds that folder at POSITION 0, the very front of
    # the list, so Python checks the project root before anywhere else
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pulls three specific functions out of salesforce/client.py, the shared
# module every agent in this project uses for its actual Salesforce calls
# from salesforce.client import get_connection, update_case_by_key, append_to_case_field
from salesforce.client import get_connection, update_case_by_key, append_to_case_field

load_dotenv()
client = Anthropic()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_FILE = PROJECT_ROOT / "data" / "Assets.csv"
KB_FILE = PROJECT_ROOT / "data" / "Knowledge_Articles.csv"
HISTORICAL_WO_FILE = PROJECT_ROOT / "data" / "Historical_Work_Orders.csv"
CASES_FILE = PROJECT_ROOT / "data" / "Cases.csv"
SAMPLE_FILE = PROJECT_ROOT / "cases_to_label.csv"
OUTPUT_FILE = PROJECT_ROOT / "output" / "root_cause_results.csv"

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2

# Caps on how much reference material gets sent to Claude per Case — keeps
# the prompt focused and token cost predictable, rather than sending every
# match regardless of how many exist
MAX_KB_ARTICLES = 3
MAX_HISTORICAL_WO = 3

# Defines the exact structure Claude must respond within — no free-text to
# parse, nothing that can come back almost-but-not-quite valid. "enum"-style
# constraints and "required" fields mean the API itself enforces the shape,
# rather than a text instruction the model could technically ignore.
PROPOSE_ROOT_CAUSE_TOOL = {
    "name": "propose_root_cause",
    "description": "Propose a root cause hypothesis for a maintenance Case, grounded in the provided reference material.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause_hypothesis": {
                "type": "string",
                "description": "A concise (1-3 sentence) root cause hypothesis, citing which reference material (if any) it draws on.",
            },
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["root_cause_hypothesis", "confidence"],
    },
}

SYSTEM_PROMPT = """You are a root-cause analysis assistant for an industrial equipment \
maintenance team. You're given a Case's description, along with relevant Knowledge Base \
articles and historical Work Order resolutions for the same asset or equipment type.

Propose the most likely root cause using the propose_root_cause tool. Ground your hypothesis \
in the provided reference material where it genuinely applies — cite it by name if you use it. \
If none of the provided material is a good match for this specific Case, say so plainly and give \
your best general hypothesis instead, with a correspondingly lower confidence score. Never \
invent a reference to a Knowledge Article or historical Work Order that wasn't actually provided \
to you."""


def build_index():
    # Runs ONCE at the very start of the script, not once per Case — loads
    # all three reference CSVs and reorganizes them into dictionaries that
    # retrieve_context() can look things up in instantly for each of the
    # 100 Cases, instead of re-reading and re-scanning every CSV 100 times

    # Reads Assets.csv and builds a dictionary keyed by Asset_Key__c, so a
    # specific asset can be looked up directly instead of scanning a list
    # assets_by_key = {row["Asset_Key__c"]: row for row in csv.DictReader(f)}
        # A DICT COMPREHENSION — builds a dictionary in one line
        # row["Asset_Key__c"]: row
            # for each CSV row, its Asset_Key__c value becomes the new
            # dict's key, and the whole row (every field) becomes the value
        # Result: assets_by_key["AST-0014"] returns that one asset's full
        # row directly, rather than looping through every asset to find it
    with open(ASSETS_FILE, newline="", encoding="utf-8") as f:
        assets_by_key = {row["Asset_Key__c"]: row for row in csv.DictReader(f)}

    # Reads Knowledge_Articles.csv into a plain list of dictionaries
    # with open(KB_FILE, newline="", encoding="utf-8") as f:
        # newline="" avoids extra blank rows CSV files can otherwise
        # produce on Windows; encoding="utf-8" ensures any special
        # character in an article's text writes and reads correctly
    with open(KB_FILE, newline="", encoding="utf-8") as f:
        kb_articles = list(csv.DictReader(f))

    # Groups the KB articles into a dictionary keyed by equipment type, so
    # every Pump article (for example) can be retrieved together in one lookup
    # kb_by_equipment_type = defaultdict(list)
        # defaultdict(list) is a dictionary that automatically creates an
        # empty list [] the first time a new key is used — a plain dict
        # would raise a KeyError instead
    kb_by_equipment_type = defaultdict(list)
    for article in kb_articles:
        # kb_by_equipment_type[article["Equipment_Type__c"]].append(article)
            # article["Equipment_Type__c"] reads this article's equipment
            # type — that value becomes the KEY on kb_by_equipment_type
            # .append(article) adds this article onto the end of whichever
            # list is stored under that key (or a fresh one, via defaultdict)
        kb_by_equipment_type[article["Equipment_Type__c"]].append(article)

    # Reads Historical_Work_Orders.csv into a plain list of dictionaries —
    # same open()/DictReader()/list() pattern as kb_articles above
    with open(HISTORICAL_WO_FILE, newline="", encoding="utf-8") as f:
        historical_wo = list(csv.DictReader(f))

    # Groups historical Work Orders by the SPECIFIC asset they belong to
    # (not just equipment type), since "what fixed this exact machine
    # before" is a stronger signal than generic same-type history
    wo_by_asset_key = defaultdict(list)
    for wo in historical_wo:
        # wo_by_asset_key[wo["Asset_Key__c"]].append(wo)
            # wo["Asset_Key__c"] is used ONLY to decide which list to add
            # to — .append(wo) then adds the ENTIRE row, every field
            # included, not just the Asset_Key__c value used to pick it
        wo_by_asset_key[wo["Asset_Key__c"]].append(wo)

    # Hands all three lookup dictionaries back to main() as a tuple, which
    # unpacks them into three separate variable names in one line
    return assets_by_key, kb_by_equipment_type, wo_by_asset_key


def retrieve_context(case, assets_by_key, kb_by_equipment_type, wo_by_asset_key):
    # Runs ONCE PER CASE, using the lookup dictionaries build_index()
    # already built. case is one dictionary — one row from Cases.csv.

    asset_key = case["Asset_Key__c"]

    # Looks up this Case's actual Asset record by its key
    # asset = assets_by_key.get(asset_key)
        # .get(...) is used instead of assets_by_key[asset_key] so a
        # missing key returns None instead of crashing the script
    asset = assets_by_key.get(asset_key)

    # Reads the equipment type off that Asset, or None if no Asset was found
    # equipment_type = asset["Equipment_Type__c"] if asset else None
        # A CONDITIONAL EXPRESSION — if asset is a real dictionary (not
        # None), read its Equipment_Type__c; otherwise, equipment_type
        # becomes None instead of crashing trying to read a key off of None
    equipment_type = asset["Equipment_Type__c"] if asset else None

    # Finds up to MAX_KB_ARTICLES Knowledge Articles matching this equipment type
    # kb_matches = list(kb_by_equipment_type.get(equipment_type, []))[:MAX_KB_ARTICLES]
        # kb_by_equipment_type.get(equipment_type, [])
            # looks up this equipment type's article list; [] is the
            # default if equipment_type isn't a key at all (e.g. it's
            # None), producing a usable empty list instead of crashing
        # list(...)
            # makes a fresh, independent copy, so slicing it below can't
            # accidentally modify the original data stored in
            # kb_by_equipment_type
        # [:MAX_KB_ARTICLES]
            # a SLICE — keeps just the first few items, discarding the rest
    kb_matches = list(kb_by_equipment_type.get(equipment_type, []))[:MAX_KB_ARTICLES]

    # Same .get(key, default) and slicing pattern as kb_matches above, but
    # looked up by this SPECIFIC asset instead of by equipment type
    wo_matches = wo_by_asset_key.get(asset_key, [])[:MAX_HISTORICAL_WO]

    return kb_matches, wo_matches, equipment_type


def format_context(kb_matches, wo_matches):
    # Turns the raw retrieved rows into two pieces of text: context_block
    # (human-readable text sent to Claude) and trace (a short summary of
    # what was retrieved, saved into AI_Reasoning_Log__c so a human
    # reviewer can see what evidence the agent had, not just its conclusion)

    lines = []
    trace_parts = []

    # A list is "truthy" when it has at least one item, "falsy" when
    # empty — so this reads as "if there's at least one KB article"
    if kb_matches:
        lines.append("Relevant Knowledge Articles:")
        for a in kb_matches:
            # \" is an ESCAPED double-quote — needed since this whole
            # string is wrapped in double quotes already, and a literal "
            # inside it would otherwise be mistaken for the closing quote
            lines.append(f"- \"{a['Title__c']}\" ({a['Category__c']}): {a['Body__c']}")

        # Builds one summary line naming every retrieved article's title
        # trace_parts.append(f"Retrieved {len(kb_matches)} KB article(s): " + ", ".join(f'"{a["Title__c"]}"' for a in kb_matches))
            # f'"{a["Title__c"]}"' for a in kb_matches
                # a GENERATOR EXPRESSION — for each article, produces its
                # title wrapped in literal quote marks. Uses SINGLE quotes
                # on the outside (f'...') so the DOUBLE quotes inside
                # don't need escaping — a different way of avoiding the
                # same quote-collision problem as the \" above
            # ", ".join(...)
                # glues all those quoted titles into one string, with
                # ", " inserted between each one
            # + "Retrieved ... KB article(s): " + ...
                # glues that joined string onto the end of this separate
                # piece of text, making one final combined string
        trace_parts.append(f"Retrieved {len(kb_matches)} KB article(s): " + ", ".join(f'"{a["Title__c"]}"' for a in kb_matches))
    else:
        lines.append("Relevant Knowledge Articles: none found for this equipment type.")
        trace_parts.append("No matching KB articles found.")

    # Same truthy-list if/else pattern as kb_matches above, applied to the
    # historical Work Order section instead
    if wo_matches:
        lines.append("\nHistorical Work Order resolutions for this asset:")
        for wo in wo_matches:
            lines.append(f"- {wo['Resolution_Notes__c']}")
        trace_parts.append(f"Retrieved {len(wo_matches)} historical Work Order(s) for this asset.")
    else:
        lines.append("\nHistorical Work Order resolutions for this asset: none found.")
        trace_parts.append("No historical Work Orders found for this asset.")

    # Combines both lists into the two final strings this function returns
    # "\n".join(lines)
        # glues every line in the list together with a real line break
        # between each one, producing one multi-line block of text
    # " ".join(trace_parts)
        # same .join() mechanic, but with a plain space as the separator,
        # producing one single-line summary
    # return ..., ...
        # sends both strings back together as a tuple — main() below
        # unpacks this as context_block, trace = format_context(...)
    return "\n".join(lines), " ".join(trace_parts)


def propose_root_cause(case_text: str, context_block: str) -> dict:
    # last_error starts as None, then gets overwritten with the real
    # exception every time an attempt fails, so there's something real to
    # raise at the very end if every attempt fails
    last_error = None

    # range(1, MAX_RETRIES + 2) with MAX_RETRIES=2 produces [1, 2, 3] —
    # three total attempts (the first try plus two retries). range()'s
    # second number is exclusive, which is why "+2" is needed to land on
    # 3 actual attempts, not "+1"
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            message = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=500,
                system=SYSTEM_PROMPT,
                tools=[PROPOSE_ROOT_CAUSE_TOOL],
                tool_choice={"type": "tool", "name": "propose_root_cause"},
                # "role" and "content" are required keys dictated by the
                # API's own schema — "user" specifically means this
                # message is coming from the human/system asking the
                # question, not a prior reply from Claude itself
                # f"Case:\n{case_text}\n\n{context_block}"
                    # case_text (this Case's own Subject/Description) and
                    # context_block (the retrieved KB/Work Order text from
                    # format_context()) both get combined into one string
                    # \n\n is a full blank line, visually separating the
                    # Case text from the reference material underneath it
                messages=[{"role": "user", "content": f"Case:\n{case_text}\n\n{context_block}"}],
            )
            # message.content is a list of content blocks; this scans for
            # the one whose type is "tool_use" and returns just that one
            # tool_use_block = next(b for b in message.content if b.type == "tool_use")
                # "b for b in message.content if b.type == 'tool_use'" is
                # a generator expression; next(...) returns the first match
            tool_use_block = next(b for b in message.content if b.type == "tool_use")
            result = tool_use_block.input

            # Even though the tool schema marks both fields "required,"
            # that's a hint to the model, not something enforced
            # mid-generation — if the response gets cut off before both
            # fields finish, result could come back missing one. Checking
            # here, with the real result dict still in hand, means the
            # error shows exactly what WAS returned, and gets caught by
            # the except block below as a normal retry-eligible failure.
            if "root_cause_hypothesis" not in result or "confidence" not in result:
                raise ValueError(f"Incomplete tool response, missing required field(s): {result}")

            return result
        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def main():
    assets_by_key, kb_by_equipment_type, wo_by_asset_key = build_index()

    # Always reads the FULL Case record from CASES_FILE, not from
    # cases_to_label.csv directly — cases_to_label.csv only carries the
    # fields needed for hand-labeling (Case_Key__c, Subject, Description,
    # etc.), not every field a Case has, so reading it directly here would
    # crash later trying to read Asset_Key__c off a row that doesn't have it
    with open(CASES_FILE, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))

    if "--sample" in sys.argv:
        # sample_keys = {row["Case_Key__c"] for row in csv.DictReader(f)}
            # A SET COMPREHENSION — collects every Case_Key__c value out
            # of cases_to_label.csv into one set. A set (not a list) is
            # used because checking "is this key in the set" is faster
            # than checking a list, and only membership matters here
        with open(SAMPLE_FILE, newline="", encoding="utf-8") as f:
            sample_keys = {row["Case_Key__c"] for row in csv.DictReader(f)}
        # Filters the full 100-Case list down to just the labeled subset —
        # cases still holds FULL Case dictionaries afterward, every field intact
        cases = [c for c in cases if c["Case_Key__c"] in sample_keys]
        print(f"--sample mode: running only {len(cases)} labeled cases (cheaper than the full 100)\n")

    # Authenticates to Salesforce once, before the loop starts, and reuses
    # the same connection for every Case processed below
    sf = get_connection()

    results = []
    updated = 0
    failed = 0

    for i, case in enumerate(cases, start=1):
        case_key = case["Case_Key__c"]

        # Unpacks retrieve_context()'s three-item tuple return by POSITION
        # (not by name — it's coincidental that the names match on both sides)
        kb_matches, wo_matches, equipment_type = retrieve_context(case, assets_by_key, kb_by_equipment_type, wo_by_asset_key)

        # Same by-position tuple-unpacking as above, for format_context()'s
        # two-item return
        context_block, trace = format_context(kb_matches, wo_matches)

        # Combines this Case's Subject and Description into one string,
        # with a line break between them, for propose_root_cause() to use
        case_text = f"Subject: {case['Subject']}\nDescription: {case['Description']}"

        try:
            result = propose_root_cause(case_text, context_block)

            # Combines trace (what evidence was retrieved) with the
            # hypothesis itself into one text string — this becomes the
            # AI_Reasoning_Log__c entry, so a reviewer sees both what
            # evidence was available AND what was concluded from it
            reasoning_entry = f"[Root Cause Agent] {trace} Hypothesis: {result['root_cause_hypothesis']}"

            # Writes AI_Root_Cause__c and AI_Root_Cause_Confidence__c —
            # both fields only this agent ever writes to, so a plain
            # overwrite is correct here: no other agent's data to protect
            case_id = update_case_by_key(sf, case_key, {
                "AI_Root_Cause__c": result["root_cause_hypothesis"],
                "AI_Root_Cause_Confidence__c": result["confidence"],
            })

            # AI_Reasoning_Log__c is different — the Safety and Dispatch
            # Agents also write to this same field. A plain overwrite
            # would erase whatever they already wrote for this Case.
            # append_to_case_field() queries the field's current value
            # first, then writes back the combination, so every agent's
            # reasoning survives together instead of only the most recent
            # write winning.
            append_to_case_field(sf, case_key, "AI_Reasoning_Log__c", reasoning_entry)

            # [:80] is a SLICE — takes just the first 80 characters of the
            # hypothesis so a long one doesn't make this printed line
            # unreadably long
            print(f"[{i}/{len(cases)}] {case_key} -> {case_id}: {result['confidence']}% confidence — {result['root_cause_hypothesis'][:80]}...")
            updated += 1
            status = "OK"
        except Exception as e:
            print(f"[{i}/{len(cases)}] {case_key}: FAILED — {e}")
            failed += 1
            result = {"root_cause_hypothesis": "", "confidence": 0}
            status = f"ERROR: {e}"

        # Builds one more dictionary and adds it onto the end of the
        # results list — one dictionary per Case, collected into one list
        # root_cause_hypothesis / confidence
            # Pulled from result — either Claude's real answer (success
            # path) or the blank placeholder set in the except block
            # (failure path); either way, result always has both keys by
            # this point, so this line doesn't need to know which path ran
        results.append({
            "Case_Key__c": case_key,
            "equipment_type": equipment_type,
            "root_cause_hypothesis": result["root_cause_hypothesis"],
            "confidence": result["confidence"],
            "status": status,
        })

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Case_Key__c", "equipment_type", "root_cause_hypothesis", "confidence", "status"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. {updated} updated, {failed} failed. Wrote local copy to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
