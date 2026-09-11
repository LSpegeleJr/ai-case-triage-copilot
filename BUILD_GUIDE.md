# Build Guide — AI Case Triage & Dispatch Copilot

A sprint-by-sprint walkthrough for building this project yourself. Each section covers what
you're building, the decisions worth making deliberately rather than defaulting into, and the
real issues you're likely to hit along the way — with enough detail to actually recognize them
if they show up for you too, not just a summary after the fact.

Read `README.md` first for the overall map. This is the detailed path through it.

---

## Why this project builds in this order

Every sprint through Sprint 6 deliberately uses **fixed, known data** — the same 100 synthetic
Cases, the same 20 hand-labeled examples, the same 10 seeded safety hazards — never anything live
or unpredictable. That's not a simplification to skip past once you're comfortable with the
material; it's the actual reason each agent could be validated with any real confidence at all.

Against a fixed, known dataset, you can ask precise questions that a live, ever-changing stream of
tickets can't answer nearly as well. Does the Triage Agent's urgency call match your own
hand-labeled judgment, case by case, exactly? Did the Safety Agent catch all 10 of the specific
hazards you know you seeded — not "most of them, probably," but a checkable 10 out of 10? When
something fails, is it because your code is actually wrong, or because this one Case happened to
be an unusual edge case you've simply never seen before? A fixed dataset lets you answer these
with real certainty. A stream of freshly-generated tickets can't — every run is different, so a
failure this time might just be an unlucky roll, not a genuine regression, and you'd have no way
to tell the difference.

Only after every agent was individually validated this way — a known accuracy baseline, a known
safety recall rate, real bugs found and actually fixed against data you could inspect and reason
about precisely — does this project introduce a ticket generator that creates genuinely
unpredictable content (see the Addendum, at the very end). That ordering is deliberate: prove
correctness against something you can verify exactly, before testing against something you
can't. Build in this order yourself, rather than jumping straight to live or randomized input —
you'll have a much harder time telling a real bug apart from an unlucky test case if you do.

---

## Sprint 1 — Foundation & Data Model

**What you're building:** two Salesforce orgs, the full data schema across four objects, 157
synthetic records, and a GitHub repo with your backlog in it.

**Environment setup**
- Create a free Salesforce Developer Edition org for development
- Create a second one for staging — if you're reusing one email address, Gmail's plus-addressing
  trick (`you+staging@gmail.com`) gets you a second signup without needing a second real inbox
- Connect both orgs to **Copado Essentials**

**A decision to make early: what tracks your backlog.** It's tempting to assume Copado Essentials
handles this since it's already connected — it doesn't, for free. Pipelines and Work Items are
Essentials Plus (paid) features; the free tier only covers Quick Deployment (org-to-org metadata
promotion). Use **GitHub Projects** for the backlog instead — free, unlimited, and a more
broadly recognizable tool if this is going in a portfolio.

**Build the schema**, field by field, through Setup → Object Manager:
- **Case** (standard, extended): `Case_Key__c`, `AI_Priority__c`, `AI_Confidence__c`,
  `AI_Root_Cause__c`, `AI_Reasoning_Log__c`, `Safety_Flag__c`
  *(`Case_Key__c` is easy to forget on a first pass — it's your human-readable identifier, e.g.
  "CS-00098," that every later script uses to find the right record. Add it now.)*
- **Asset** (standard, extended): `Asset_Key__c` (mark it **External ID + Unique** — this is what
  lets a CSV import resolve lookups later), `Equipment_Type__c`, `Criticality__c`,
  `Last_Maintenance_Date__c`
- **Work_Order__c** (new custom object): `WO_Key__c`, `Case__c` (lookup), `Asset__c` (lookup),
  `Assigned_Technician__c`, `Status__c`, `Priority__c`, `Resolution_Notes__c`, `Closed_Date__c`
- **Knowledge_Article__c** (new custom object): `KA_Key__c`, `Title__c`, `Body__c`,
  `Equipment_Type__c`, `Category__c`

Custom objects are used for Work Order and Knowledge Article instead of Salesforce's standard
ones specifically to avoid needing Field Service Lightning or Salesforce Knowledge licensing in
a free Developer Edition org.

**Generate synthetic data**: 25 Assets, 12 Knowledge Articles, 20 historical (closed) Work
Orders, 100 Cases. Seed roughly 10% of your Cases as deliberate safety hazards (a chemical odor,
a pressure relief valve chattering, a caustic feed failure) — you'll need a known-answer test set
once you build the Safety Escalation Agent in Sprint 4, and it's much easier to seed this now than
retrofit it later.

**Load the data.** Two things to watch for here:
- The standard desktop **Data Loader now requires configuring your own OAuth External Client
  App** before it'll even log in — a real setup barrier for what should be a simple CSV import.
  Use **dataloader.io** instead (browser-based, same underlying Bulk API, nothing to install).
- The standard **Asset object requires an Account or Contact on every record** — an undocumented
  platform rule you'll hit as a `MALFORMED_ID`-adjacent error the first time you try to insert
  Assets without one. Create one placeholder Account representing "the facility," and include its
  Id on every Asset row.
- When mapping a lookup relationship in dataloader.io (Case → Asset, Work Order → Asset), use its
  **"Lookup via"** option to match on `Asset_Key__c` rather than a real Salesforce Id — this is
  what lets a CSV insert resolve relationships before any of the related records have real Ids
  you could reference directly.

**Set up your GitHub repo**: public (so it doubles as portfolio evidence), with a README, a
`.gitignore` (exclude `venv/`, `.env`, and generated output from version control), labels for
each Epic, milestones for each Sprint, and one Issue per backlog item.

---

## Sprint 2 — Triage Agent

**What you're building:** the first agent — reads a Case's raw text and classifies urgency and
category.

**Scaffold your project properly before writing agent code.** A flat folder of scripts works for
a first pass, but you'll add three more agents after this one; set up `data/`, `agents/`,
`scripts/`, and `output/` folders now, plus a `.env` file (never committed) for your API key,
managed through `python-dotenv`.

**Use Claude's tool-use (function calling), not free-text JSON.** The natural first instinct is
to ask Claude to "respond with only a JSON object" and parse the result yourself — this works
until it doesn't: any stray word, markdown fence, or truncated response breaks `json.loads()`.
Define a strict schema and force the model to respond through it instead. This isn't a
theoretical concern — expect roughly a 5% failure rate from free-text parsing at real scale, with
no visibility into why unless you're logging the actual exception.

**Add retry logic, and log the real error message**, not a bare "ERROR." When something fails,
you want to know *why* — a truncated response, a network blip, a malformed field — not just that
it happened.

**Build a cheap validation mode before you need it.** Every time you tweak a system prompt,
you'll want to re-test it — and re-running your full batch against a paid API every time adds up
fast. Hand-label a small sample (20 cases is plenty) and build a `--sample` flag that runs against
just that subset. Do this *before* you've burned money re-running full batches to test small
tweaks, not after.

**Validate against your own judgment, honestly.** Compare the model's output to your own
hand-labeled sample, and actually look at *where* it disagrees, not just the aggregate accuracy
number. Two things worth specifically checking:
- Is there a **systematic** pattern in the disagreements (the same case type, wrong the same way,
  repeatedly) — that's a real, fixable prompt issue, not noise
- Is your own hand-labeling internally **consistent** — near-identical cases labeled two different
  ways during a long manual task is a real risk, and it'll look like a model error if you don't
  catch it on your own side first

**A prompt-engineering trap worth knowing about ahead of time:** guidance you write for one field
can leak into a different field if the wording isn't scoped carefully. Adding urgency guidance
that says a trip "means the equipment left safe, reliable operation" can measurably bias the
model's *category* choice toward "Mechanical" too, purely because of the word choice — even
though category was never the intent. If you add a rule, state explicitly what it does and
doesn't govern.

---

## Sprint 3 — Root Cause Agent

**What you're building:** an agent that retrieves relevant reference material and proposes why a
Case is happening, with its own separate confidence score.

**Before building retrieval, decide: structured filtering or embeddings?** With a small,
already-tagged corpus (our project: ~32 documents, each carrying a clean `Equipment_Type__c`
field), embeddings-based semantic search adds real cost and a new dependency for no practical
gain. Filter by `Equipment_Type__c` for Knowledge Articles, and by the *specific* Asset (not just
its type) for historical Work Order resolutions — "what fixed this exact machine before" is a
stronger signal than generic same-type history. Revisit this call if your own corpus grows much
larger than a few dozen documents.

**Check for field collisions before you build.** If an earlier agent already claimed a
confidence-score field, a new agent needing its own confidence score will collide with it the
moment you wire up the write-back. Catch this at schema-design time, not after a failed write.

**Test your retrieval logic against real data before your first live run.** A structured filter
is easy to get subtly wrong — pulling in a Safety-tagged article for the wrong equipment type,
for instance, which reads as "relevant" but isn't. Check a few real cases by hand before trusting
the retrieved context blindly.

**Two failure modes worth testing for specifically, before you assume "0 failed" means it worked:**
- **An incomplete tool response.** Even with a strict schema, a response can get cut off before
  every required field finishes generating if `max_tokens` is too tight — and this surfaces as a
  confusing `KeyError` from a completely unrelated line, unless you explicitly check for it and
  raise a clear, retry-eligible error instead.
- **A Unicode crash on Windows.** Claude will eventually generate a character (a Greek letter used
  as engineering shorthand, for instance) that Windows' default file encoding can't represent.
  Set `encoding="utf-8"` explicitly on every file you open, in every script, before this bites
  you mid-run. If it does happen anyway: check whether your actual Salesforce writes already
  succeeded before the crash — a script can fail on its own *local backup file* after every real
  update already went through cleanly.

---

## Sprint 4 — Safety Escalation Agent

**What you're building:** a narrower, binary check — does this Case represent an active hazard —
independent of whatever urgency/category the Triage Agent already assigned.

**Keep this agent's design simpler than your Root Cause Agent's, on purpose.** A binary decision
doesn't need per-Case retrieval — load your small set of safety-specific reference material once,
and show all of it to every Case, since a hazard pattern is worth checking regardless of which
specific equipment is involved.

**Calibrate deliberately toward high recall.** State this as an explicit design principle in your
system prompt, not just an implicit hope: a missed hazard risks real harm, a false alarm costs a
few minutes of a reviewer's attention. Accept more false positives than you normally would for a
classification task.

**Write the flag explicitly for every Case, not just the positive ones.** Setting a boolean field
only when it's `True` leaves "checked, not a hazard" indistinguishable from "never examined" —
write `False` just as deliberately as `True`, so the absence of a flag actually means something.

**Validate against your known-answer set, precisely — don't eyeball it.** If you seeded specific
hazard cases back in Sprint 1, write a small script that cross-references your actual output
against that exact list. "Looks like it caught most of them" and "10 of 10, confirmed" are very
different claims to be able to make.

---

## Sprint 5 — Dispatch Agent

**What you're building:** the synthesis step — matches an available technician to a Case and
drafts a note pulling together everything the first three agents already decided.

**This is your first agent that needs to *read* Salesforce, not just write to it.** By this
point, the fields your earlier agents wrote (`AI_Priority__c`, `AI_Root_Cause__c`,
`Safety_Flag__c`) only exist in Salesforce — not in whatever local CSV you started from. Query
the Case fresh. If the Case has a related Asset, pull its fields in the *same* query via
relationship traversal (`Asset.Equipment_Type__c` in SOQL) rather than a second round-trip.

**Split deterministic logic from LLM judgment deliberately.** Matching a technician's skills
against an equipment type is a constraint check — plain code handles this correctly and for free.
Save the LLM call for the part that actually benefits from it: synthesizing several agents' prior
findings into one coherent, human-readable note.

**"Not auto-sent" doesn't require withholding the record — just withholding the notification.**
Create the real Work Order, with a status like "Open" that signals it's pending review, rather
than skipping creation entirely. The record existing is what makes it reviewable; nothing about
it needs to trigger an actual email or alert.

**Fix the shared-field overwrite problem before a third agent makes it worse.** If two agents
already write to the same audit-log-style field via a plain update, each one silently erases the
last agent's entry. By your third or fourth agent, this compounds. Build a proper
read-current-value-then-append helper *before* adding another writer to that field, and expect to
need to re-run your earlier agents once to restore any history already lost.

**Watch for schema mismatches the API will catch for you, immediately.** A lookup field expecting
a real record Id will reject a plain text name with a clear, specific error — that's usually a
sign the field was set up as the wrong type early on, not a bug in your write-back code. Salesforce
doesn't let you convert a Lookup field's type directly; expect to delete and recreate it as Text.

**If your matching logic always favors whoever's listed first, check whether that's actually true
at scale — not just in principle.** "First qualified match wins" can silently mean one otherwise-
qualified, available option never gets used at all, if everything they're skilled at is also
covered by someone listed above them. This is discoverable for free: pure matching logic (no LLM
call involved) can be tested directly against your real dataset before you decide whether load
balancing is worth adding.

---

## Sprint 6 — Review Dashboard

**What you're building:** a human-facing UI — a priority-sorted queue, a full reasoning trace per
Case, filters, and approve/override controls.

**Pick a tool that lets you skip a separate backend.** Streamlit (or an equivalent) can call your
existing Salesforce helper functions directly from the same script that renders the UI — no
separate API server needed for a project this size.

**Keep your data logic (sorting, filtering) separate from your UI framework's function calls.**
A plain Python function you can unit-test is worth far more than logic buried inside widget
callbacks you can only verify by clicking around by hand.

**Sort by meaning, not by the platform's default.** A picklist like Critical/High/Medium/Low
sorts alphabetically by default in most systems — the wrong order for an actual priority queue.
Map each value to an explicit rank number and sort by that instead.

**Add a schema field for "a human looked at this," if nothing already captures it.** Approve/
override controls need somewhere to record that a review actually happened, distinct from
whatever the AI itself decided.

**Cache your connection, but not forever.** A live session token has a real lifetime; caching a
connection object indefinitely means it'll eventually die mid-use during a long dashboard
session, surfacing as an expired-session error at an inconvenient moment. Give any cached
connection a time-to-live shorter than the underlying token's actual lifespan, so it refreshes
itself quietly before that happens.

**If your data count looks wrong, investigate systematically — don't assume it's a bug in your
own code.** A Developer Edition org comes pre-seeded with its own sample data the moment it's
provisioned, entirely unrelated to whatever schema you build afterward. A field your own records
always populate (in this project, `Case_Key__c`) being blank on the unexpected extras is usually
the fastest way to confirm this — followed by checking `Created By` and `Created Date`, which will
point to the org's own provisioning process rather than anything you built. Filter your queries
to exclude it once confirmed, rather than leaving it as confusing noise in front of anyone you
show this to later.

---

## Sprint 7 — Public Deployment & Narrative

*(To be filled in as this sprint happens.)*

---

## Addendum — From Batch Processing to Live, Incoming Tickets

Every agent in this project reads from a fixed local CSV and processes a set batch — deliberate
for a portfolio project (predictable, cheap to test against, fully reproducible), but not how a
real system would actually operate. A production dispatch system needs to respond to Cases as
they arrive — via web-to-case, email-to-case, phone, or a customer portal — not wait for someone
to run a script against a static file.

**What has to change, and what doesn't.** The actual reasoning in each agent — `classify_case()`,
`propose_root_cause()`, `check_safety()`, `match_technician()` + `draft_dispatch_note()` — doesn't
need to change at all. This is only true because each agent was already built as clean,
importable functions from Sprint 2 onward, rather than logic buried inside one script with
nothing reusable. What actually changes is **how a Case gets found** (querying Salesforce
directly for unprocessed Cases, instead of reading a fixed CSV) and **the processing unit** — one
Case flowing through all four stages immediately, instead of four independent full-batch passes.

**Three ways to trigger on new Cases, simplest to most involved:**

1. **Scheduled polling.** Query Salesforce directly for Cases where `AI_Priority__c = null`
   (nobody's triaged them yet), on a recurring schedule — every few minutes via Windows Task
   Scheduler or a cron job — instead of a person remembering to run a script. This reuses
   everything already built, needs no new infrastructure, and gets Cases processed within minutes
   of arriving.
2. **Salesforce Change Data Capture + a subscriber service.** Salesforce publishes an event the
   instant a Case is created; a long-running Python process subscribes via the Pub/Sub API and
   reacts immediately. Genuinely real-time (sub-second), but requires standing up and hosting a
   persistent listener service — meaningfully more infrastructure than a scheduled script.
3. **A Salesforce Flow with an outbound webhook.** A Flow fires on Case creation and makes an
   HTTP callout to an endpoint you host. This flips the direction (Salesforce pushes to you,
   rather than you polling or subscribing), but means your agent logic needs to become an actual
   hosted web service — a small FastAPI app with a public HTTPS endpoint — not a script.

**Recommendation: start with polling.** It gets you most of the practical benefit — Cases
processed within minutes, not whenever someone remembers to run something — with none of the new
hosting or subscription infrastructure the other two options require. Revisit Options 2 or 3 once
you're at a scale where minutes of latency genuinely matters; that's a real, valid concern for a
mature system, just not the first problem worth solving here.

**Chaining agents per Case, not just triggering them.** Right now, four separate scripts each
process all 100 Cases before the next script even starts. A live system needs one new Case to
flow through Triage → Root Cause → Safety → Dispatch immediately, not wait behind a full batch of
everything else. `agents/run_pipeline_live.py` is a working example of this: it imports each
agent's real function directly and chains all four for a single Case, using scheduled polling as
the trigger.

**A real integration detail worth knowing before you build this yourself:** the batch agent
functions expect `case["Asset_Key__c"]` as a flat field, matching the shape of a row from
`Cases.csv`. A live SOQL query naturally returns `AssetId` plus a nested `Asset` object instead
(via relationship traversal). These aren't the same shape — `run_pipeline_live.py` bridges this
with a small adapter after the query, rather than changing `retrieve_context()` itself. Expect a
handful of small shape mismatches like this any time you connect functions built against one data
source to a different one; it's a normal part of this kind of conversion, not a sign the earlier
code was wrong.

### A working demo: simulating tickets actually arriving

The clearest way to show this project handling live input isn't just documentation — it's an
agent that generates realistic tickets and watches the rest of the system actually process them,
in front of whoever you're showing this to.

**Why this comes last, deliberately, and not earlier.** Everything up through Sprint 6 was
validated against fixed, known data specifically so each agent's correctness could be checked
precisely — see "Why this project builds in this order," at the very top of this guide. A ticket
generator that creates unpredictable content is exactly the kind of live, ungrounded input that
principle argues against testing with *first*. It only belongs here, at the very end, because
every agent it now feeds has already been proven correct against something checkable. Build one
of these before your agents are validated, and a bad result becomes ambiguous — you can no longer
tell whether it's a real bug or just an unlucky randomly-generated Case.

**`agents/generate_live_tickets.py`** creates a batch of new Cases and immediately runs them
through the full pipeline — a genuine end-to-end simulation of tickets arriving and getting
handled, not just a description of how it would work:

- Uses Claude to **write each ticket's text fresh**, in a plant operator's voice, rather than
  replaying Sprint 1's fixed templates — a better simulation of tickets that have never been seen
  before, which is the entire point of testing this late rather than early.
- Deliberately seeds a couple of genuine safety hazards into each batch (the same ~10% ratio as
  the original seeded set), so the Safety Agent has something real to catch on live-generated
  data too, not just routine issues.
- Queries real Assets and your highest existing `Case_Key__c` directly from Salesforce, so every
  new batch continues your numbering correctly and links to real equipment — never a local CSV.
- Reuses `run_pipeline_live.py`'s own functions directly for the actual processing — no separate
  copy of that chaining logic.

**The dashboard needed one addition to make separate batches distinguishable: a `Created`
column, plus a date/time range filter in the sidebar.** Without it, tickets from a
`generate_live_tickets.py` run are indistinguishable from your original 100 once they're sitting
in the same queue. Two things worth knowing if you build this yourself:
- Salesforce's `CreatedDate` comes back as a plain string — parse it into a real `datetime`
  before trying to filter or sort on it, rather than comparing text.
- That parsed timestamp comes back **timezone-aware** (Salesforce includes a UTC offset), while a
  UI date/time picker's values are typically **timezone-naive** — comparing the two directly will
  raise an error. Convert to local time and strip the timezone marker before comparing, so both
  sides of the comparison agree.

**Validated for real, not just described:** this was run end to end — 20 tickets generated,
automatically processed through all four agents, and the dashboard's queue correctly showed 120
total Cases with the date/time filter cleanly isolating just the new batch from the original 100.

---

## Addendum — Actually Using Copado Essentials, Not Just Connecting It

Sprint 1 has you connect both orgs to Copado Essentials — but a connection that's never actually
used isn't a real deployment story, just two orgs sitting there. This addendum walks through
actually deploying something for real, so there's a genuine example to point to, not just an
implied one.

**Copado Essentials is not inside Salesforce at all — a real thing worth knowing before you go
looking for it.** It won't show up in Salesforce's own App Launcher, because nothing gets
installed inside your org; it's a completely separate web platform that connects to your orgs via
OAuth. Go to `essentials.copado.com` directly and log in with your Salesforce credentials — that
login *is* your Copado login, there's no separate password. Once in, an "Organizations" page
lists every org you've connected; confirm both Dev and Staging show up there with a real
"Last Authorized" date, which is itself a good, quick way to verify the connection was ever
actually completed rather than assumed.

**Deploying one specific field, end to end:**
1. **Deployments → New Deployment.** Set Source Org = your Dev org, Target Org = Staging.
2. **Add Components tab.** The Component Types dropdown needs "Custom Field" specifically — it's
   filed under an "Objects & Child Components" category, not sitting at the top level, and a
   similarly-named but unrelated type ("Custom Index," for database indexes, not fields) sits
   right next to it alphabetically. Search or browse for your field by name, filtered by its
   Parent object (e.g. `Case`), and select just that one component — not a wildcard.
3. **Validate before Deploy.** These are two separate buttons for a reason: Validate runs a
   dry-run that checks whether the deployment would succeed, without changing anything yet.
   Confirm it reports success before touching Deploy.
4. **Deploy**, once validation passes.
5. **Verify in the target org directly** — log into Staging, Setup → Object Manager (not the App
   Launcher; Object Manager lives under Setup) → the relevant object → Fields & Relationships,
   and confirm the field is actually there. Don't just trust Copado's own success message — the
   same "verify the real system state, not just the reported result" habit that mattered
   throughout every other sprint applies here too.

**What this deployment actually moves, worth being precise about in an interview:** only the
field's *definition* — its type, label, and configuration — not any data. Staging's Case records
(if it has any) don't gain any new values in this field; they'd all start blank. Metadata and
data are deployed through entirely separate mechanisms in Salesforce, and Copado Essentials'
Quick Deployment is specifically a metadata tool.

