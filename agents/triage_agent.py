"""
Sprint 2, US-201: Triage Agent
Salesforce AI Case Triage & Dispatch Copilot

Reads data/Cases.csv, classifies each Case's urgency and category via the
Claude API, and writes results to output/triage_results.csv for review.

This is deliberately offline/CSV-based for now (US-201's acceptance criteria
is "returns urgency + category for a test batch") — wiring this up to write
directly into Salesforce via REST API is Sprint 2's next story, US-203.

Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example to .env and fill in your real ANTHROPIC_API_KEY
  3. Run from the project root: python agents/triage_agent.py

CHANGE FROM V1: uses Claude's tool-use (function calling) feature instead of
asking Claude to write JSON as free text. This eliminates the JSON-parsing
failures the original version hit (~5% of cases) by having the API enforce
the response shape directly, rather than hoping Claude's text output happens
to parse cleanly. Also adds retry logic for genuine transient failures
(network hiccups, rate limits) and logs the REAL error message per failed
Case instead of a bare "ERROR" placeholder.
"""

import csv          # reading/writing CSV files
import json          # parsing the JSON text Claude sends back
import time           # for the small pause between API calls
from pathlib import Path         # modern way to handle file paths
from dotenv import load_dotenv    # loads your .env file into the environment
from anthropic import Anthropic   # the SDK that talks to Claude's API

# from dotenv library
    # Looks for file named ".env" in current working directory
    # Folder you were in when you ran command
    # Reads .env file and goes line-by-line and parses text
    # splits on first "=" into a key and value
    # In our .env file "ANTHROPIC_API_KEY" became key and the API key code became value.  
    # This allows sensitive API keys to be listed in .env files & out of Python code
    # .env file is in the .gitignore so it will not be committed to GitHub
load_dotenv()

# Anthropic is not a function but a class.  
    # A blueprint for creating an object
    # Anthropic() builds one real object from the blueprint, instantiating the class.  
    # What gets stored in client is object you just built that can talk to Anthropic's API
    # It holds that ability for rest of script.  
    # client.messages.create(...) calls a capability inside that object.  
    # Anthropic() looks for "ANTHROPIC_API_KEY", which exists in environment due to load_dotenv()
client = Anthropic()  # picks up ANTHROPIC_API_KEY automatically

# Path is a standard-library class
    # Represents folder or file location.  
    # Works on both Windows and Mac/Linux
# __file__ special built-in variable
    # Always holds location of file currently running
    # agents/triage_agent.py in this case
# Path(__file__)
    # Takes file name and wraps it in a Path object
    # Get access to all  of Path's location-handling abilities
# .resolve()
    # Converts path into a full, unambiguous, absolute path
    # agents/triage_agent.py will display full folder path to it
    # Matters because __file__ isn't guarrenteed to already be absolute
    # .resolve() guarrantees that.  
# .parent
    # Steps up one folder level.  Called twice it goes up two folder levels
    # Steps to agents/ and then to project root folder.  
# PROJECT_ROOT
    # Becomes file path for project root folder
# DATA_FILE
    # / normally means division but in pathlib it means join
    # PROJECT ROOT is joined to become PROJECT ROOT/data/Cases.csv
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "Cases.csv"
OUTPUT_FILE = PROJECT_ROOT / "output" / "triage_results.csv"

# List must match picklis values vales for AI_Priority__c and AI_Category__c in Salesforce.  
URGENCY_LEVELS = ["Critical", "High", "Medium", "Low"]
CATEGORIES = ["Mechanical", "Electrical/Instrumentation", "Process", "Safety", "Routine/Preventive", "Other"]

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2

# \ before a line break tells Python to pretend new line not there, treats 2nd line as part of 1st.  
# {} in an f-string get replaced with actual Python value
    # {URGENCY_LEVELS} is seen as ["Critical", "High", "Medium", "Low"]
    # {{}} double curly braces is an esacape, get sent as single {}
    # Allows JSON formatting to get through instead of inputting a value  
SYSTEM_PROMPT = f"""You are a triage assistant for an industrial equipment maintenance team. \
You read incoming maintenance Case reports and classify each one.

Classify strictly into these values:
- urgency: one of {URGENCY_LEVELS}
- category: one of {CATEGORIES}
- confidence: an integer 0-100, how confident you are in this classification

Guidance:
- "Critical" means an active hazard, active release, or immediate safety risk — not just an unpleasant symptom.
- "High" means something that could become critical if ignored, or is actively impacting production.
- Equipment trips are "High" urgency, even ones that reset on their own — a trip represents a real process 
deviation, and depending on the equipment, a recurring trip can cascade into a full plant trip. Don't let "resets
 on its own" read as reassuring. This is an urgency rule only — it says nothing about category. A trip's category 
still depends entirely on its actual described cause (electrical/instrumentation, mechanical, process, etc.), 
not on the mere fact that a trip occurred. Don't let the word "trip" itself read as a signal toward "Mechanical."
- Any reported leak is "Safety" category, regardless of how the description downplays its visibility (e.g. "no 
visible spray" does not downgrade it) — a leak is a safety issue by its nature, not by how visible it currently 
looks.
- Recurring high-high alarms are "Safety" category, even when the description notes uncertainty about whether 
it's a real reading or an instrument fault — a high-high alarm represents real potential for an equipment or 
plant trip and should not be downgraded to routine instrumentation troubleshooting just because the cause is 
unconfirmed.
- Routine wear, scheduled inspections, and minor drift-type symptoms are usually "Medium" or "Low".
- If in doubt between Safety and another category, choose Safety and lower your confidence score rather than 
silently downgrading urgency."""

# The schema Claude MUST respond within — no free-text JSON to parse, no risk
# of extra prose breaking things, and the enum lists make it structurally
# impossible for Claude to return a value outside our Salesforce picklists.
CLASSIFY_TOOL = {
    "name": "classify_case",
    "description": "Classify a maintenance Case's urgency and category.",
    "input_schema": {
        "type": "object",
        "properties": {

            # "enum" here means Claude is only allowed to pick from these exac values
            # API enforces this
            "urgency": {"type": "string", "enum": URGENCY_LEVELS},
            "category": {"type": "string", "enum": CATEGORIES},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["urgency", "category", "confidence"],
    },
}

# classify_case() defined
    # Parameters "subject" and "description" are strings
    # -> dict is a hint this function produces a dictionary
# client.messages.create(...)
    # Actual network call where function reaches out to Anthropic's servers & waits for response
    # client is Anthropic object we built earlier
def classify_case(subject: str, description: str) -> dict:
    last_error = None # Set variable last_errpr to "no value"

    # range(1, MAX_RETRIES + 2) with MAX_RETRIES=2 produces [1, 2, 3]
    # Range(1, 4) means count from 1 to 4, stopping before 4
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            message = client.messages.create(
                model="claude-sonnet-5", # Sets Claude's model
                max_tokens=200, # Sets hard limit on token usage
                system=SYSTEM_PROMPT, # Our instructions

                # tools=[...] makes this tool available to Claude for this call.
                # tool_choice forces Claude to actually USE it (specifically
                # classify_case) rather than leaving it optional — without
                # this, Claude could still just reply with plain text instead.
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "classify_case"},

                # messages is the actual conversation
                    # A list containing one dictionary
                    # API expects conversation history in this shape
                    # A list, because a real back-and-forth convo could have many turns, here we only send one
                    # {"role": "user",...} part of required schema for Anthropics
                    # both "role" (the key) and "user" (the value) are dictated by Anthropic's API
                    # "user" means messages is coming from the human/system asking the question
                    # "assistant" means message is something Claude itself said earlier
                    # Typing "role":"customer" instead would cause the API to reject whole request
                        # Since not a value it recognizes.  
                    # {subject} and {description} hold real values at this point
                        # They were filled in main() below when it called classif_case()
                        # main() runs first in execution order, even thought it is written last in code
                messages=[{"role": "user", "content": f"Subject: {subject}\nDescription: {description}"}],
            )

            # message.content is a list of content blocks.
            # The block we want is identified by TYPE ("tool_use"). 
            # b for b in message.content if b.type == "tool_use"
                # "for b in message.content" declares loop variable b
                # "if b.type == "tool_use""" filter for b.type =="tool_use"
                # The first b represents the output from the loop filtering
            # next() gives first thing inner part produces, returns the first block 
            # matching that condition — a compact way of saying "find the tool_use block,
            # whichever position it's actually in."
            tool_use_block = next(b for b in message.content if b.type == "tool_use")

            # tool_use_block.input
                # tool_use_block is the one block we found for this iteration
                # .input pulls out its dictionary
                # {"urgency": ..., "category": ..., "confidence": ...}
                # return sends that dictionary back to what called it, classify_case()
            return tool_use_block.input

        # Exception as e
            # Captures error at iteration in e
        except Exception as e:

            # For every iteration in loop with an error last_error is reset as latest error e
            last_error = e
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    # We only reach this line if EVERY attempt failed. Raising last_error
    # last_error represents the last error in e
        # raise triggers an error on purpose, rethrowing error in last_error
        # This error becomes visible to main()'s own try/except
        # Remember main() runs first and calls calssify_case within its own try/except
        # Therefore, this error now runs through that try/except block
    raise last_error

def main():

    # Opens file at path we built
        # newline="" tells Python not to do usual automatic line-ending conversion
        # which can cause blank extra rows or corrupt values when reading CSV files.  
        # Open() returns the file object and "as f" stores it in f
    with open(DATA_FILE, newline="", encoding="utf-8") as f:

        # csv.DictReader(f) reads files and uses first row as column headers
            # turning every subsequent row into a dictionary keyed by those header names.  
            # Can reference case["Subject"] by name instead of remember description is 3rd column
        cases = list(csv.DictReader(f))

    results = []
    error_count = 0

    # enumerate(cases, start=1)
        # Walks through list 1 item at a time and hands back a counter alongside each item
        # start=1 tells Python to start at 1 instead of default 0
        # case catches actual dictionary for the row at present iteration
    for i, case in enumerate(cases, start=1):

        # try & except - Python try something and if error do except instead of crash
        try:
            result = classify_case(case["Subject"], case["Description"])

            # status column in CSV file may be blank for successful rows
            # Need to set a default status value to prevent errors
            status = "OK" 

        # Exception as e
            # Captures error at iteration in e
        except Exception as e:

            # Python shorthand for error_count = error_count + 1
                # Counts the errors entering the except block.  
            error_count += 1

            # result set to Error for urgency and category with confidence at 0
            result = {"urgency": "ERROR", "category": "ERROR", "confidence": 0}

            # status set to text string "ERROR: " and the value of error in e
            status = f"ERROR: {e}"

        # At this point dictionary result only knows what Claude gave back
            # urgency, category, and confidence
            # 3 lines below add 3 additional key-value pairs
            # For 2 lines key set in result with value coming from case
            # For status, key set in result with value coming from status
            # results.append(result) adds the now complete dictionary onto end of results
            # results is a list of dictionaries, that started blank above.  
            # results will have 100 dictionaries inside one list.  
            # A list is needed since we have 100 separate records
                # each with same set of fields but different values
                # List is natural shape for "many records, same structure"
        result["Case_Key__c"] = case["Case_Key__c"]
        result["Subject"] = case["Subject"]
        result["status"] = status
        results.append(result)

        # [{i}/{len(cases)}]
            # {len(cases)} counts total # of cases, 100 in our situation
            # {i} results in case #, starting at 1.  i will range 1 - 100 as loop iterates
        # {case['Case_key__c']}
            # Return the value of the key being referenced.  
        # Does this for other terms
        # {case['Case_Key__c']}, technically code be written with result instead of case
            # Due to result["Case_Key__c"] = case["Case_Key__c"] earlier
        print(
            f"[{i}/{len(cases)}] {case['Case_Key__c']}: {result['urgency']} / "
            f"{result['category']} ({result['confidence']}%) [{status}]"
        )
        time.sleep(0.3)  # light rate-limit courtesy, pauses API calls by three-tenths of a second

    # Once loop finishes all 100 cases this block of code writes everything out
    # OUTPUT_FILE.parent brings you to the parent folder of the file, output/
    # .mkdir(exist_ok=TRUE) creates that folder if doesn't exist and does nothing if it does
        # Prevents a potential error
    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    # Opens file at path we built, "w" designates to open as write
        # newline="" tells Python not to do usual automatic line-ending conversion
        # which can cause blank extra rows or corrupt values when reading CSV files.  
        # Open() returns the file object and "as f" stores it in f
        # NOTE this f is a different file object that f in Cases.csv
            # Other one closed once its with blocked ended
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:

        # csv.DictWriter
            # Mirror image of DictReader from top function
            # Instead of turning rows into dictionaries, turns dictionaries back into rows
            # Dictionary f is being converted into rows for variable writer
            # Uses fieldnames to decide which columns to include and what order
        # Variable writer, is an object built from DictWriter class
            # Knows how to do something (write dictionaries out as CSV rows)
        writer = csv.DictWriter(f, fieldnames=["Case_Key__c", "Subject", "urgency", "category", "confidence" , "status"])

        # .writeheader()
            # Uses fieldnames to creates header columns for variable writer
        writer.writeheader()

        # .writerows(results)
            # writes one row per dictionary in results
            # For each of the 100 dictionaries it looks up the values matching each fieldname
            # Writes those values across, one dictionary per row.  
        writer.writerows(results)

    print(f"\nDone. {len(results) - error_count}/{len(results)} succeeded. Wrote results to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()