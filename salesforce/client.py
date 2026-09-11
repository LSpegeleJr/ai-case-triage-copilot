"""
Reusable Salesforce connection helper.

Sprints 3-5 (Root Cause, Safety, Dispatch agents) will also need to read/write
Salesforce records — this module exists so the connection logic lives in one
place instead of being copy-pasted into every agent script.

Auth: OAuth 2.0 Client Credentials flow. (Username-Password flow, tried
first, turned out to be blocked by default for orgs created after Summer
'23 and unsupported by External Client Apps entirely — Salesforce's current
official recommendation for server-to-server integrations like this one is
Client Credentials instead.) This flow authenticates as whichever user is
configured as the "Run As" user on the External Client App itself, using
only the Consumer Key/Secret — no username, password, or security token
needed in this script at all.

ENVIRONMENT SWITCHING (Dev vs. Staging): the code here never changes
between environments — only which .env file gets loaded does. This is a
named, standard principle (Twelve-Factor App: "config varies across
environments, code does not"), not something specific to this project.
Every agent script gets this for free, automatically, without any changes
of its own — see get_connection() below for exactly how.
"""

import os
import sys
from dotenv import load_dotenv
import requests
from simple_salesforce import Salesforce


def get_connection() -> Salesforce:
    """
    Authenticate via OAuth 2.0 Client Credentials flow and return a live
    Salesforce connection object.

    Connects to Dev by default. Pass --staging on the command line of
    whichever script calls this to connect to Staging instead — e.g.
    `python agents/triage_agent.py --staging`. No individual agent script
    needs its own flag-handling code for this: every script already calls
    get_connection() to talk to Salesforce, so putting the environment
    switch here, once, makes it available everywhere at once.
    """
    # if "--staging" in sys.argv:
        # sys.argv is Python's own list of everything typed on the command
        # line, shared by whichever script actually imported this module —
        # e.g. running "python agents/triage_agent.py --staging" makes
        # sys.argv equal to ['agents/triage_agent.py', '--staging'], so
        # this check works no matter which script called get_connection()
    if "--staging" in sys.argv:
        # load_dotenv(".env.staging", override=True)
            # By the time this runs, the calling script's own top-level
            # load_dotenv() call has ALREADY loaded the default .env file
            # (Dev's credentials) into the environment — python-dotenv
            # normally REFUSES to overwrite a variable that's already set,
            # so without override=True, Staging's values would silently
            # never take effect; this forces the replacement
        load_dotenv(".env.staging", override=True)
        print("[client.py] --staging flag detected — connecting to Staging")

    print("[client.py] Using Client Credentials flow (v3)")  # confirms this exact file is what's running

    # Builds the token endpoint's full URL for this specific org
    # f"{os.environ['SF_INSTANCE_URL']}/services/oauth2/token"
        # os.environ['SF_INSTANCE_URL']
            # reads the org's own domain (e.g.
            # https://orgfarm-xxxxx.develop.my.salesforce.com) out of the
            # environment, loaded there by load_dotenv() in whichever
            # script imported this module
        # Client Credentials flow requires the ORG'S OWN domain as the
        # token endpoint — the generic login.salesforce.com used by other
        # OAuth flows isn't supported here
    token_url = f"{os.environ['SF_INSTANCE_URL']}/services/oauth2/token"

    # Sends the actual OAuth token request as an HTTP POST
    # requests.post(token_url, data={...})
        # requests is a third-party library for making HTTP calls;
        # .post(...) sends a POST request to token_url
        # data={...} is the request body — a dictionary of the exact
        # fields Salesforce's OAuth endpoint expects
        # "grant_type": "client_credentials"
            # tells Salesforce which OAuth flow this is — must be this
            # exact string for Client Credentials specifically
        # "client_id" / "client_secret"
            # the Consumer Key and Consumer Secret from the External
            # Client App, read out of the environment the same way as
            # SF_INSTANCE_URL above
    response = requests.post(token_url, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["SF_CONSUMER_KEY"],
        "client_secret": os.environ["SF_CONSUMER_SECRET"],
    })

    # if not response.ok:
        # response.ok is True for any normal successful HTTP response,
        # False for an error status — this only runs the block below when
        # something went wrong
        # print(f"OAuth token request failed ({response.status_code}): {response.text}")
            # Salesforce's OAuth errors include a specific reason in the
            # response body (e.g. "invalid_client_id", "invalid_grant")
            # that a bare HTTP status code alone won't tell you — printing
            # response.text here surfaces that real reason before the
            # script crashes, rather than losing it
    if not response.ok:
        print(f"OAuth token request failed ({response.status_code}): {response.text}")

    # response.raise_for_status()
        # Raises an exception immediately if the request failed — this
        # runs regardless of whether the print() above fired, so a failed
        # request always stops execution here rather than continuing on
        # with a broken response
    response.raise_for_status()

    # auth = response.json()
        # Parses the response body as JSON, turning Salesforce's reply
        # into a real Python dictionary — on success, this dictionary
        # contains "access_token" and "instance_url" among other fields
    auth = response.json()

    # return Salesforce(instance_url=auth["instance_url"], session_id=auth["access_token"])
        # Salesforce(...) is simple_salesforce's connection class —
        # building one here, rather than using its own built-in login
        # methods, is what lets us hand it an already-obtained OAuth
        # token directly
        # instance_url / session_id
            # instance_url tells simple_salesforce which org to talk to;
            # session_id is the actual access token proving we're
            # authenticated — together, these are everything
            # simple_salesforce needs to make real API calls from here on
    return Salesforce(instance_url=auth["instance_url"], session_id=auth["access_token"])


def update_case_by_key(sf: Salesforce, case_key: str, fields: dict) -> str:
    """
    Update a Case record identified by our own Case_Key__c (e.g. "CS-00047"),
    not Salesforce's internal record Id — since that's the identifier our
    CSVs and scripts have used all along.

    fields: a dict of {api_field_name: value} to update, e.g.
        {"AI_Priority__c": "High", "AI_Category__c": "Safety", "AI_Confidence__c": 85}

    Returns the real Salesforce Id of the Case that was updated, for logging.
    """
    # Case_Key__c is plain text, not a marked External ID field the way
    # Asset_Key__c was — so we look it up via a query first, then update by
    # the real Id. (Marking it as an External ID too would allow a shortcut
    # here, but isn't necessary for this volume of updates.)

    # safe_key = case_key.replace("'", "\\'")
        # A basic SOQL-injection guard — if case_key itself ever contained
        # a single quote, it could break out of the quoted string in the
        # query below; .replace("'", "\\'") escapes any quote character by
        # putting a backslash in front of it first
    safe_key = case_key.replace("'", "\\'")

    # sf.query(f"SELECT Id FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1")
        # sf.query(...) sends a SOQL query (Salesforce's query language,
        # similar in spirit to SQL) and returns the matching records
        # f"SELECT Id FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1"
            # asks for just the Id of the one Case whose Case_Key__c
            # matches safe_key — LIMIT 1 caps it at a single result, since
            # Case_Key__c values are expected to be unique
    result = sf.query(f"SELECT Id FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1")

    # if result["totalSize"] == 0:
        # totalSize is how many records the query actually found —
        # zero means no Case exists with this key at all
        # raise ValueError(...)
            # stops execution immediately with a clear, specific error,
            # rather than letting the next line crash with a confusing
            # "list index out of range" trying to read a record that was
            # never found
    if result["totalSize"] == 0:
        raise ValueError(f"No Case found with Case_Key__c = {case_key}")

    # case_id = result["records"][0]["Id"]
        # result["records"] is a list of matching records; [0] takes the
        # first (and, given LIMIT 1, only) one; ["Id"] reads its real
        # Salesforce Id out of that record
    case_id = result["records"][0]["Id"]

    # sf.Case.update(case_id, fields)
        # sf.Case is simple_salesforce's way of referring to the standard
        # Case object; .update(case_id, fields) sends the actual update —
        # case_id says WHICH record, fields says what to change on it
    sf.Case.update(case_id, fields)
    return case_id


def append_to_case_field(sf: Salesforce, case_key: str, field_name: str, new_text: str) -> str:
    """
    Appends new_text onto the END of a Case field's CURRENT value, rather
    than overwriting it — for fields multiple agents write to over time
    (AI_Reasoning_Log__c), where each agent's entry needs to survive the
    next agent's own write, not get erased by it.

    A plain update_case_by_key() call REPLACES a field's entire value —
    fine for a field only one agent ever touches (AI_Root_Cause__c), wrong
    for a field meant to accumulate entries from several agents over the
    life of a Case.

    Returns the real Salesforce Id of the Case that was updated.
    """
    safe_key = case_key.replace("'", "\\'")

    # sf.query(f"SELECT Id, {field_name} FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1")
        # Same query shape as update_case_by_key(), but ALSO selects
        # field_name itself — {field_name} is substituted directly into
        # the query text, so this same function works for appending to
        # ANY Case field, not just AI_Reasoning_Log__c specifically
        # We need the field's CURRENT value back from this query, which
        # is the whole reason this function queries first instead of
        # jumping straight to an update
    result = sf.query(f"SELECT Id, {field_name} FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1")
    if result["totalSize"] == 0:
        raise ValueError(f"No Case found with Case_Key__c = {case_key}")

    # record = result["records"][0]
        # The one matching record's full dictionary — includes both Id
        # and field_name's current value, since both were selected above
    record = result["records"][0]
    case_id = record["Id"]

    # existing_value = record.get(field_name) or ""
        # record.get(field_name) reads the field's current value — could
        # be None if nothing has ever written to it yet
        # "or \"\"" substitutes an empty string in that case, so the
        # f-string below always has a real string to work with instead of
        # risking an error trying to combine None with text
    existing_value = record.get(field_name) or ""

    # combined = f"{existing_value}\n{new_text}" if existing_value else new_text
        # A CONDITIONAL EXPRESSION — if existing_value is a non-empty
        # string, combine it with new_text separated by a line break;
        # otherwise (this is the very first entry) just use new_text
        # alone, with no leading blank line
    combined = f"{existing_value}\n{new_text}" if existing_value else new_text

    # sf.Case.update(case_id, {field_name: combined})
        # Writes the FULL combined text back — Salesforce has no
        # "append" operation of its own; this function's whole job is
        # simulating one by reading first, then writing the complete
        # result back as a normal update
    sf.Case.update(case_id, {field_name: combined})
    return case_id


def get_case_by_key(sf: Salesforce, case_key: str) -> dict:
    """
    Queries a Case by Case_Key__c, including the fields written by earlier
    agents (AI_Priority__c, AI_Category__c, AI_Root_Cause__c, Safety_Flag__c)
    AND its related Asset's Id and Equipment_Type__c, in a single SOQL query
    using relationship traversal (Asset.Equipment_Type__c) — rather than a
    separate query for the Asset, or falling back to the local Assets.csv,
    since Case already has a standard lookup to Asset (AssetId).

    Returns the full record dict. The related Asset's fields come back
    nested under record["Asset"] (e.g. record["Asset"]["Equipment_Type__c"]),
    or record["Asset"] is None if this Case's AssetId was never set.
    """
    safe_key = case_key.replace("'", "\\'")

    # sf.query(f"""...""")
        # A triple-quoted f-string used purely for readability here — lets
        # the SOQL query span multiple lines in the source rather than
        # being one very long single line
        # AssetId, Asset.Equipment_Type__c
            # AssetId is Case's own standard lookup field, holding the
            # related Asset's real Id directly
            # Asset.Equipment_Type__c uses DOT NOTATION to reach across
            # that relationship and pull a field from the related Asset
            # record in the SAME query, rather than needing a second
            # query afterward
    result = sf.query(f"""
        SELECT Id, Case_Key__c, Subject, Description, AI_Priority__c,
               AI_Category__c, AI_Root_Cause__c, Safety_Flag__c,
               AssetId, Asset.Equipment_Type__c
        FROM Case WHERE Case_Key__c = '{safe_key}' LIMIT 1
    """)
    if result["totalSize"] == 0:
        raise ValueError(f"No Case found with Case_Key__c = {case_key}")
    return result["records"][0]


def create_work_order(sf: Salesforce, fields: dict) -> str:
    """
    Creates a new Work_Order__c record (unlike update_case_by_key/
    append_to_case_field, which both update an EXISTING Case — this makes a
    brand new record instead, since each Case may get its own Work Order).

    fields: a dict of {api_field_name: value}, e.g.
        {"WO_Key__c": "WO-00001", "Case__c": "500...", "Asset__c": "02i...",
         "Assigned_Technician__c": "Maria Chen", "Status__c": "Open",
         "Priority__c": "High"}

    Returns the real Salesforce Id of the newly created Work_Order__c record.
    """
    # sf.Work_Order__c.create(fields)
        # sf.Work_Order__c refers to our custom Work_Order__c object, the
        # same way sf.Case refers to the standard Case object elsewhere in
        # this file
        # .create(fields) — unlike .update(), which needs an existing
        # record's Id, .create() makes a BRAND NEW record from scratch,
        # using fields as that new record's starting values
        # result["id"] reads the real Salesforce Id Salesforce assigned to
        # this new record, handed back in the create response
    result = sf.Work_Order__c.create(fields)
    return result["id"]


def get_all_cases(sf: Salesforce) -> list:
    """
    Queries every Case's key fields at once — used by the Review Dashboard,
    which needs to display and filter across the whole set, not one Case
    at a time the way the four agents do.

    Includes the related Asset's Equipment_Type__c and Criticality__c via
    relationship traversal (Asset.Equipment_Type__c), same pattern as
    get_case_by_key() — one query gets everything the dashboard needs to
    display and filter on, without a second query per Case.

    WHERE Case_Key__c != null excludes Salesforce's own out-of-box demo
    Case records, which every Developer Edition org is auto-seeded with the
    moment it's created — those never have a Case_Key__c at all, since they
    were never touched by any of our own data-loading or agent scripts.

    Returns a list of record dicts (Salesforce's raw query result shape).
    """
    # No filter beyond Case_Key__c != null — deliberately queries every
    # real Case OUR system created, not just a sample, since the dashboard
    # needs the whole set to display and filter across
    # WHERE Case_Key__c != null
        # Case_Key__c is blank on Salesforce's auto-seeded demo Cases,
        # since they were never loaded through dataloader.io or touched
        # by any agent — this excludes exactly those records, leaving
        # only the ones our own system actually built
    result = sf.query("""
        SELECT Id, Case_Key__c, Subject, Description, AI_Priority__c,
               AI_Category__c, AI_Confidence__c, AI_Root_Cause__c,
               AI_Root_Cause_Confidence__c, Safety_Flag__c,
               AI_Reasoning_Log__c, Human_Reviewed__c,
               Asset.Equipment_Type__c, Asset.Criticality__c
        FROM Case
        WHERE Case_Key__c != null
    """)
    return result["records"]
