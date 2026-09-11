# AI Case Triage & Dispatch Copilot
### Build an agentic AI system for Salesforce Service Cloud

Field service and equipment-heavy companies get flooded with Cases as free-text descriptions ("pump making noise," "line 3 tripped again"). Dispatchers manually read each one to guess urgency, likely cause, and which technician to send — and safety-critical issues don't always get flagged fast enough.

This repo walks you through building a multi-agent AI system that triages, diagnoses, escalates, and dispatches those Cases — with a human always able to review before anything proceeds. **Start with `BUILD_GUIDE.md`** for the full sprint-by-sprint walkthrough; this README is the map of what you're building and why.

## What you'll build

1. **Triage Agent** — classifies urgency and category from Case text
2. **Root Cause Agent** — retrieves Knowledge Articles + Asset/Work Order history to hypothesize root cause
3. **Safety Escalation Agent** — flags hazard patterns for immediate human review
4. **Dispatch Agent** — matches an available technician, creates a Work Order, drafts a dispatch note
5. **Review Dashboard** — a human-in-the-loop queue with full reasoning trace and approve/override controls

## What you'll need

- A **Salesforce Developer Edition org** (free) — two of them, actually: one for development, one for staging
- **Copado Essentials (Free tier)** — for org-to-org metadata deployment
- A **GitHub account** — for your backlog and the repo itself
- **Python 3.10+**, and an **Anthropic API key** (separate from any Claude.ai subscription — this is billed pay-as-you-go)
- Familiarity with basic command-line use (you'll spend time in PowerShell or a terminal)

No prior Salesforce admin experience is required — every schema change is spelled out click by click in `BUILD_GUIDE.md`.

## Data model you'll set up

| Object | Key fields | Purpose |
|---|---|---|
| Case (standard) | Case_Key__c, AI_Priority__c, AI_Category__c, AI_Confidence__c, AI_Root_Cause__c, AI_Root_Cause_Confidence__c, AI_Reasoning_Log__c, Safety_Flag__c, Human_Reviewed__c | All four agents' output lands here |
| Asset (standard) | Asset_Key__c (External ID), Equipment_Type__c, Criticality__c, Last_Maintenance_Date__c | Equipment being serviced |
| Work_Order__c (custom) | WO_Key__c, Case__c, Asset__c, Assigned_Technician__c, Status__c, Priority__c, Resolution_Notes__c, Closed_Date__c | Dispatch Agent output |
| Knowledge_Article__c (custom) | Title__c, Body__c, Equipment_Type__c, Category__c | Reference material for Root Cause and Safety agents |

You won't build this all at once — `BUILD_GUIDE.md` introduces each field exactly when the sprint that needs it comes up, which is closer to how this schema actually gets built in practice: a few fields at a time, driven by what the next piece of working code actually requires.

## Roadmap

| Sprint | What you'll build |
|---|---|
| 1 | Foundation: orgs, schema, synthetic data, GitHub backlog |
| 2 | Triage Agent |
| 3 | Root Cause Agent |
| 4 | Safety Escalation Agent |
| 5 | Dispatch Agent |
| 6 | Review Dashboard |
| 7 | Public deployment, a live-ticket simulation, and your own interview narrative |

## Tech stack, and why each piece was chosen

- **Salesforce Developer Edition** — free, full-featured enough for this project
- **Copado Essentials (Free tier)** — free tier only covers Quick Deployment (org-to-org metadata promotion), not Pipelines/Work Items — plan your backlog tool accordingly (see below)
- **GitHub Projects**, not a paid backlog tool — free, unlimited, and more broadly recognizable than a niche tool if you're building this as a portfolio piece
- **Python + Claude API**, using tool-use (function calling) throughout rather than asking the model to write JSON as free text — this eliminates an entire class of parsing failure at the root, rather than something you patch around later
- **simple-salesforce**, authenticating via OAuth 2.0 Client Credentials flow through an External Client App — Salesforce's current recommendation for this kind of server-to-server integration; older flows (username/password, SOAP login) are being retired
- **Streamlit**, not a separate React/Flask stack, for the dashboard — a single script can call your Salesforce helper functions directly, no separate backend needed
- **dataloader.io**, not the desktop Data Loader — newer versions of Data Loader require configuring your own OAuth app; dataloader.io is browser-based and needs nothing installed

## Design decisions worth understanding before you start

- **Validate every agent against fixed, known data before ever introducing live or unpredictable input.** This governs the whole build order, not just one sprint — Sprints 1 through 6 all run against the same 100 synthetic Cases, the same 20 hand-labeled examples, the same 10 seeded safety hazards. A fixed dataset lets you check correctness precisely (did the Safety Agent catch all 10 seeded hazards — exactly, not approximately); a stream of freshly-generated tickets can't give you that same certainty, since every run differs and a bad result could just be an unlucky Case rather than a real bug. Only introduce something like a live ticket generator once every agent it feeds has already been proven correct this way — see `BUILD_GUIDE.md`'s Addendum for exactly this, built last, on purpose.
- **Use structured retrieval (filter by Equipment_Type__c and Asset_Key__c), not embeddings.** With a corpus this size (~30 reference documents), embeddings add cost and a dependency for no real retrieval-quality gain. Revisit this if your own corpus grows much larger.
- **Match technicians to jobs with plain deterministic code, not an LLM call.** Skill-and-availability matching is a constraint check, not a reasoning task — save the LLM for the parts that actually need judgment, like drafting a synthesized summary.
- **Calibrate your Safety Agent toward high recall on purpose.** A missed hazard is far worse than a false alarm costing a reviewer thirty seconds. Validate this against a real, known set of seeded hazard cases — don't just trust the aggregate flag count.
- **Give every agent that writes to a shared field an append-not-overwrite helper**, from the start. If two agents ever write to the same field via a plain update, the second one silently erases the first — a bug that's easy to introduce and easy to miss until you actually read the data back.
- **Use `--sample` mode everywhere an LLM is involved**, so you can validate a prompt change against a small hand-labeled set before paying for a full run. This alone will save you real money over the course of the project.
- **Set `encoding="utf-8"` explicitly on every file you open**, on every platform. Windows' default encoding can't represent every character an LLM might generate, and this exact bug will eventually crash a run at the worst possible moment if you don't.

For the full story behind each of these — the actual errors hit, what they looked like, and how each was diagnosed — see `BUILD_GUIDE.md`.
