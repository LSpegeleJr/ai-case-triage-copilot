# Use "streamlit run agents/dashboard.py" to run the dashboard
"""
Sprint 6/7, US-601/602/604: Review Dashboard

A Streamlit app for a human dispatcher to review every Case's AI-generated
decisions in one place: urgency/category (Triage Agent), root cause
hypothesis (Root Cause Agent), safety flag (Safety Escalation Agent), and
the full accumulated reasoning trace from all three.

THIS DASHBOARD NEVER CALLS CLAUDE AND NEVER WRITES TO SALESFORCE — it is
entirely read-only. Approving or overriding a Case's priority happens by
opening that Case directly in Salesforce (a link is provided), not from a
button here. This matters specifically because this dashboard may be
deployed publicly: a read-only app can't rack up API costs or modify real
data just by being opened or refreshed, regardless of who's viewing it.

DATA LOGIC KEPT SEPARATE FROM UI CODE ON PURPOSE: sort_by_priority() and
apply_filters() are plain functions with no Streamlit dependency, so they
can be tested directly rather than only being verifiable by clicking
around a running app.

Run with: streamlit run agents/dashboard.py
"""

import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
import pandas as pd
import streamlit as st
from dateutil import parser as date_parser
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from salesforce.client import get_connection, get_all_cases

# Loads .env into the environment so get_connection() below can find the
# Salesforce credentials — has to run before get_connection() is ever called.
# This is what makes LOCAL runs work; it does nothing on Streamlit Cloud,
# since .env is gitignored and never actually gets deployed there.
load_dotenv()

# Bridges Streamlit Cloud's own secrets manager into the SAME os.environ
# variables client.py already reads — so client.py itself never needs to
# know or care whether it's running locally or deployed
# try: ... except Exception: pass
    # st.secrets raises an error locally if no secrets.toml file exists,
    # which is the normal, expected case for local development — this
    # silently does nothing in that situation, relying on load_dotenv()
    # above instead
# for key, value in st.secrets.items():
    # Streamlit Cloud's secrets are configured through its own dashboard,
    # not a file in this repo — st.secrets.items() reads whatever was
    # configured there
# os.environ.setdefault(key, str(value))
    # .setdefault(...) only sets a value if that key ISN'T already present
    # — so if .env already supplied something locally, this never
    # overwrites it; on Streamlit Cloud, where .env doesn't exist at all,
    # this is what actually populates the environment client.py reads from
try:
    for key, value in st.secrets.items():
        os.environ.setdefault(key, str(value))
except Exception:
    pass

# Maps each urgency value to a sort-order number, since Salesforce would
# otherwise sort these alphabetically (Critical, High, Low, Medium) —
# wrong order for a real priority queue. Lower number = more urgent = sorts first.
PRIORITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def sort_by_priority(cases: list) -> list:
    # Sorts Cases so the most urgent ones come first, using our own defined
    # order (Critical, High, Medium, Low) instead of alphabetical order
    # sorted(...)
        # returns a NEW sorted list, rather than reordering cases in place
        # key=lambda c: ...
            # a LAMBDA is Python's syntax for a small, unnamed function
            # defined inline — "lambda c: EXPRESSION" behaves the same as
            # writing a separate "def some_name(c): return EXPRESSION",
            # just without giving it a name, since it's only used once
            # here, this lambda takes one Case dictionary (c) and returns
            # whatever value sorted() should actually compare for it
        # c.get("AI_Priority__c")
            # reads this Case's urgency value; could be None if the Triage
            # Agent hasn't run on it yet
        # PRIORITY_RANK.get(..., 99)
            # looks up that value's rank number; the second argument, 99,
            # is the DEFAULT returned if the value isn't a key in
            # PRIORITY_RANK at all (e.g. None) — pushes never-triaged
            # Cases to the very end, rather than crashing on a missing key
    return sorted(cases, key=lambda c: PRIORITY_RANK.get(c.get("AI_Priority__c"), 99))


def apply_filters(cases: list, safety_only: bool, equipment_types: list, criticalities: list) -> list:
    # Narrows the full Case list down step by step, one filter at a time —
    # each variable below only gets smaller than the one before it, rather
    # than building one giant combined condition in a single line
    filtered = cases

    # Only keeps Cases flagged as safety-critical, and only runs at all if
    # the dispatcher actually checked the "Safety flagged only" box
    # if safety_only:
        # safety_only is a plain True/False from a Streamlit checkbox
        # filtered = [c for c in filtered if c.get("Safety_Flag__c")]
            # c.get("Safety_Flag__c") is falsy for both False and None, so
            # a Case that's explicitly cleared OR was never checked at all
            # both get filtered out here — only True survives
    if safety_only:
        filtered = [c for c in filtered if c.get("Safety_Flag__c")]

    # Only keeps Cases whose equipment type is one the dispatcher selected
    # if equipment_types:
        # equipment_types is a list from a Streamlit multiselect — if the
        # dispatcher hasn't picked anything, this list is empty, and an
        # empty list is "falsy," so this whole filter step is skipped
        # (showing every equipment type) rather than matching zero Cases
        # filtered = [c for c in filtered if (c.get("Asset") or {}).get("Equipment_Type__c") in equipment_types]
            # (c.get("Asset") or {})
                # handles a Case with no Asset at all — c.get("Asset")
                # returns None in that case, and "or {}" substitutes an
                # empty dictionary so the following .get(...) doesn't
                # crash trying to read a key off of None
            # .get("Equipment_Type__c") in equipment_types
                # checks whether this Case's actual equipment type is one
                # of the ones the dispatcher selected
    if equipment_types:
        filtered = [c for c in filtered if (c.get("Asset") or {}).get("Equipment_Type__c") in equipment_types]

    # Same pattern as the equipment-type filter just above, applied to
    # Criticality__c instead
    if criticalities:
        filtered = [c for c in filtered if (c.get("Asset") or {}).get("Criticality__c") in criticalities]

    return filtered


def parse_created_date(case: dict) -> datetime | None:
    # Salesforce returns CreatedDate as a text string, e.g.
    # "2026-09-05T20:05:00.000+0000" — turns it into a real Python
    # datetime object that can actually be compared and formatted, rather
    # than compared as plain text
    # case.get("CreatedDate")
        # could be None if this Case somehow has no CreatedDate at all —
        # date_parser.parse(None) would raise, so this is checked first
    raw = case.get("CreatedDate")
    if not raw:
        return None

    # date_parser.parse(raw, default=datetime(1900, 1, 1))
        # from python-dateutil — reliably handles Salesforce's exact
        # timestamp format (including the +0000 offset) across Python
        # versions, unlike the standard library's datetime.fromisoformat(),
        # which is pickier about the exact format in older Python versions
        # default=datetime(1900, 1, 1)
            # A REAL, sharp-edged gotcha this guards against: if
            # date_parser.parse() can't determine some piece of the date
            # from the input string, it doesn't raise an error — it
            # silently FILLS IN the missing piece using this "default"
            # value. If you never pass one, dateutil's own built-in
            # default is the CURRENT moment — meaning a partially-broken
            # or unexpectedly-shaped input string would silently become
            # "today," with no error or warning anywhere, for every
            # single Case at once. An obviously-wrong sentinel date
            # (1900) instead makes that failure mode immediately visible
            # (Cases would show as from 1900, clearly wrong) rather than
            # silently masquerading as legitimate, valid data
    parsed = date_parser.parse(raw, default=datetime(1900, 1, 1))

    # .astimezone().replace(tzinfo=None)
        # Salesforce's timestamp comes back timezone-AWARE (it includes a
        # UTC offset); Streamlit's date_input/time_input widgets produce
        # plain, timezone-NAIVE values with no offset concept at all —
        # Python refuses to compare an aware and a naive datetime
        # directly, so this converts the timestamp to the local system's
        # own timezone first (.astimezone(), with no argument, means
        # "convert to local time"), then strips the timezone marker
        # entirely (.replace(tzinfo=None)), producing a naive datetime
        # that correctly represents local wall-clock time — matching
        # what the sidebar's pickers actually produce
    return parsed.astimezone().replace(tzinfo=None)


def filter_by_date_range(cases: list, start_dt: datetime, end_dt: datetime) -> list:
    # Keeps only Cases whose CreatedDate falls within [start_dt, end_dt] —
    # this is what actually lets a specific batch of tickets (e.g. one run
    # of generate_live_tickets.py) be isolated from every other Case
    filtered = []
    for c in cases:
        created = parse_created_date(c)
        # A Case with no parseable CreatedDate at all is excluded from a
        # date-filtered view entirely, rather than crashing trying to
        # compare None against a real datetime
        if created and start_dt <= created <= end_dt:
            filtered.append(c)
    return filtered


def build_table_rows(cases: list) -> list:
    # Turns the raw Salesforce record dicts (with their nested "Asset"
    # sub-dictionary from the relationship query) into a flatter shape
    # convenient for displaying as a table — one plain dictionary per row
    rows = []
    for c in cases:
        # Ensures asset is always a real dictionary to read from, even for
        # a Case with no linked Asset — same None-safety pattern as
        # apply_filters() above
        # asset = c.get("Asset") or {}
        asset = c.get("Asset") or {}
        # created = parse_created_date(c)
            # Reuses the same parsing function the date filter itself
            # relies on, so the displayed value and the filtered value
            # can never disagree with each other
        # created.strftime("%Y-%m-%d %H:%M") if created else "—"
            # .strftime(...) formats a real datetime into readable text —
            # "2026-09-05 20:05" rather than the raw ISO string Salesforce
            # returns; the conditional expression falls back to a plain
            # dash if this Case somehow has no parseable CreatedDate at all
        created = parse_created_date(c)
        rows.append({
            "Case Key": c.get("Case_Key__c"),
            "Subject": c.get("Subject"),
            "Priority": c.get("AI_Priority__c"),
            "Category": c.get("AI_Category__c"),
            "Safety Flag": c.get("Safety_Flag__c"),
            "Equipment Type": asset.get("Equipment_Type__c"),
            "Criticality": asset.get("Criticality__c"),
            "Reviewed": c.get("Human_Reviewed__c"),
            "Created": created.strftime("%Y-%m-%d %H:%M") if created else "—",
        })
    return rows


def main():
    # Sets the browser tab title and uses the full page width rather than
    # Streamlit's default narrower centered layout — must be the first
    # Streamlit command in the whole script
    st.set_page_config(page_title="Case Review Dashboard", layout="wide")
    st.title("AI Case Triage & Dispatch — Review Dashboard")

    # Authenticates to Salesforce and reuses the same connection across
    # reruns, but only for a LIMITED time — not forever
    # @st.cache_resource(ttl=1800)
        # Same caching behavior as before (run once, reuse the result on
        # every rerun), but ttl=1800 tells Streamlit to automatically
        # throw away the cached result after 1800 seconds (30 minutes) and
        # run the function fresh the next time it's needed
        # Without a ttl, the SAME connection object gets reused forever,
        # even after its underlying OAuth token expires — a real dispatcher
        # keeping this dashboard open for hours would eventually hit a
        # SalesforceExpiredSession error, which is exactly what happened
        # here. 30 minutes is comfortably shorter than Salesforce's typical
        # token lifetime, so a fresh connection gets built before the old
        # one has a chance to actually expire.
    @st.cache_resource(ttl=1800)
    def _get_connection():
        return get_connection()

    sf = _get_connection()

    # Caches the full Case list so re-fetching all 100 records doesn't
    # happen on every single click, only when explicitly refreshed
    # @st.cache_data
        # same idea as @st.cache_resource, but for actual DATA rather than
        # a live connection object
    @st.cache_data
    def _get_all_cases():
        return get_all_cases(sf)

    if st.button("Refresh from Salesforce"):
        # Wipes the cached result, so the NEXT call to _get_all_cases()
        # actually re-queries Salesforce instead of returning the same
        # cached data again
        # _get_all_cases.clear()
        _get_all_cases.clear()

    cases = _get_all_cases()

    # st.sidebar puts these controls in the left-hand panel rather than
    # the main page — standard Streamlit convention for filters/settings
    st.sidebar.header("Filters")
    safety_only = st.sidebar.checkbox("Safety flagged only")

    # Builds the two dropdown option lists for the sidebar filters, pulling
    # only values that actually appear somewhere in the real data
    # set(... for c in cases if ...)
        # a SET COMPREHENSION — collects every distinct value present
        # across all Cases (a set automatically drops duplicates)
        # (c.get("Asset") or {}).get("Equipment_Type__c")
            # same None-safety pattern as apply_filters() above
        # if (c.get("Asset") or {}).get("Equipment_Type__c")
            # discards any None result (a Case with no Asset) before it'd
            # show up as a confusing blank option in the dropdown
    # sorted(...)
        # orders the resulting set alphabetically, for a predictable
        # dropdown order rather than whatever random order a set produces
    equipment_options = sorted(set((c.get("Asset") or {}).get("Equipment_Type__c") for c in cases if (c.get("Asset") or {}).get("Equipment_Type__c")))
    criticality_options = sorted(set((c.get("Asset") or {}).get("Criticality__c") for c in cases if (c.get("Asset") or {}).get("Criticality__c")))

    equipment_types = st.sidebar.multiselect("Equipment type", equipment_options)
    criticalities = st.sidebar.multiselect("Criticality", criticality_options)

    st.sidebar.subheader("Created date/time")

    # all_dates = [parse_created_date(c) for c in cases]
        # Parses every Case's CreatedDate up front, once, so min()/max()
        # below don't need to re-parse anything
    # all_dates = [d for d in all_dates if d]
        # Drops any None results (a Case with no parseable CreatedDate) —
        # min()/max() would crash comparing a real datetime against None
    all_dates = [parse_created_date(c) for c in cases]
    all_dates = [d for d in all_dates if d]

    # min(all_dates).date() / max(all_dates).date()
        # min()/max() find the earliest and latest actual timestamps
        # across every Case; .date() drops the time-of-day portion, since
        # st.date_input works with plain dates, not full timestamps —
        # time-of-day precision is added back separately, below
    # if all_dates else datetime.now().date()
        # a fallback for the edge case of an empty Case list entirely,
        # so this line can't crash trying to call min() on nothing
    earliest = min(all_dates).date() if all_dates else datetime.now().date()
    latest = max(all_dates).date() if all_dates else datetime.now().date()

    # if earliest == latest: latest = earliest + timedelta(days=1)
        # A SECOND, independent safety net — even if every Case genuinely
        # were created on the exact same calendar day, min_value equal to
        # max_value below would leave the widget with no valid range to
        # actually pick within, which can make a date-range picker appear
        # entirely locked/uninteractive rather than just narrow. Forcing
        # at least a one-day spread guarantees the widget always stays
        # genuinely usable, regardless of what the underlying data looks
        # like or what caused it.
    if earliest == latest:
        latest = earliest + timedelta(days=1)

    # st.sidebar.date_input(..., value=(earliest, latest))
        # passing a TUPLE as the value makes this a single RANGE picker —
        # the dispatcher can pick a start and end date in one widget,
        # rather than two separate ones
        # date_range starts as (earliest, latest) — the widest possible
        # range — showing every Case by default, until narrowed
    date_range = st.sidebar.date_input("Date range", value=(earliest, latest), min_value=earliest, max_value=latest)

    # st.sidebar.time_input(...)
        # Adds time-of-day precision on top of the date range above — two
        # same-day batches (e.g. two separate generate_live_tickets.py
        # runs) would otherwise be indistinguishable using dates alone
        # time(0, 0) / time(23, 59)
            # default to covering the ENTIRE day (midnight to 11:59 PM),
            # so picking a date range alone, with no further narrowing,
            # still shows every Case from those days — not an empty result
    start_time = st.sidebar.time_input("From time", value=time(0, 0))
    end_time = st.sidebar.time_input("To time", value=time(23, 59))

    filtered = apply_filters(cases, safety_only, equipment_types, criticalities)

    # if len(date_range) == 2:
        # While the dispatcher is actively picking a range in the UI,
        # date_range can briefly be a ONE-item tuple (only the start date
        # clicked so far) — this guards against trying to filter on an
        # incomplete range mid-click
        # datetime.combine(date_range[0], start_time)
            # combines the separately-picked DATE and TIME OF DAY into one
            # real datetime — date_range[0] alone has no time-of-day
            # information; start_time alone has no calendar date
    if len(date_range) == 2:
        start_dt = datetime.combine(date_range[0], start_time)
        end_dt = datetime.combine(date_range[1], end_time)
        filtered = filter_by_date_range(filtered, start_dt, end_dt)

    sorted_cases = sort_by_priority(filtered)

    st.subheader(f"Queue ({len(sorted_cases)} of {len(cases)} Cases)")

    # Renders the queue as an actual interactive table (sortable columns, scrollable)
    # pd.DataFrame(build_table_rows(sorted_cases))
        # build_table_rows() (defined above) turns the list of Case
        # dictionaries into flat table-row dictionaries; pd.DataFrame(...)
        # then turns that list into a real pandas table structure
    # st.dataframe(...)
        # the Streamlit widget that actually displays a DataFrame as a
        # visible, interactive table on the page
    table_df = pd.DataFrame(build_table_rows(sorted_cases))
    st.dataframe(table_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Case detail")

    # Builds a plain list of every Case Key, in the same priority order as
    # the table above, for the dropdown below to choose from
    # [c["Case_Key__c"] for c in sorted_cases]
    case_keys = [c["Case_Key__c"] for c in sorted_cases]
    if not case_keys:
        st.info("No Cases match the current filters.")
        return

    selected_key = st.selectbox("Select a Case to review", case_keys)

    # Finds the one full Case dictionary matching whichever key the
    # dispatcher just picked from the dropdown
    # next(c for c in sorted_cases if c["Case_Key__c"] == selected_key)
        # "c for c in sorted_cases if ..." is a GENERATOR EXPRESSION —
        # scans through sorted_cases looking for a match
        # next(...) returns just the first match found
    selected_case = next(c for c in sorted_cases if c["Case_Key__c"] == selected_key)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Subject:** {selected_case.get('Subject')}")
        st.markdown(f"**Description:** {selected_case.get('Description')}")
        st.markdown(f"**AI Priority:** {selected_case.get('AI_Priority__c')} (confidence: {selected_case.get('AI_Confidence__c')}%)")
        st.markdown(f"**AI Category:** {selected_case.get('AI_Category__c')}")
        st.markdown(f"**Root Cause:** {selected_case.get('AI_Root_Cause__c')} (confidence: {selected_case.get('AI_Root_Cause_Confidence__c')}%)")
        st.markdown(f"**Safety Flag:** {selected_case.get('Safety_Flag__c')}")
        st.markdown(f"**Human Reviewed:** {selected_case.get('Human_Reviewed__c')}")
    with col2:
        st.markdown("**Full reasoning trace (all agents):**")
        # Displays the full accumulated log as a large, read-only text box
        # st.text_area(..., height=300, disabled=True)
            # disabled=True prevents the dispatcher from accidentally
            # editing it, since this is meant to display the log, not
            # create a new editable copy of it
        st.text_area("AI_Reasoning_Log__c", value=selected_case.get("AI_Reasoning_Log__c") or "(empty)", height=300, disabled=True, label_visibility="collapsed")

    st.divider()

    # Rather than write anything from the dashboard itself, this links
    # straight to the Case's real record in Salesforce — approvals and
    # priority overrides happen there instead
    # SF_INSTANCE_URL is the same org domain already used for
    # authentication in salesforce/client.py, read the same way — via
    # os.environ, populated by load_dotenv() locally or the secrets
    # bridge above when deployed
    # f"{sf_instance_url}/lightning/r/Case/{selected_case['Id']}/view"
        # Salesforce's standard Lightning URL pattern for opening any
        # record directly by its real Id — /lightning/r/OBJECT/ID/view
    sf_instance_url = os.environ.get("SF_INSTANCE_URL", "").rstrip("/")
    case_url = f"{sf_instance_url}/lightning/r/Case/{selected_case['Id']}/view"
    st.markdown(f"**To approve or change this Case's priority, open it directly in Salesforce:** [{selected_key} →]({case_url})")
    st.caption("Approvals and overrides are made in Salesforce itself, not from this dashboard — this keeps the dashboard safely read-only, including in any deployment shared publicly, and every change goes through Salesforce's own permissions and field history tracking rather than a separate path.")


if __name__ == "__main__":
    main()
