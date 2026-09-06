# AI Case Triage & Dispatch Copilot
### An agentic AI system for Salesforce Service Cloud

Field service and equipment-heavy companies get flooded with Cases as free-text descriptions ("pump making noise," "line 3 tripped again"). Dispatchers manually read each one to guess urgency, likely cause, and which technician to send — and safety-critical issues don't always get flagged fast enough. This project builds a multi-agent AI system that triages, diagnoses, and drafts dispatch recommendations for those Cases, with a human always approving before anything writes back to Salesforce.

## Architecture (planned, Sprints 2-6)

1. **Triage Agent** — classifies urgency and category from Case text
2. **Root Cause Agent** — RAG over Knowledge Articles + Asset/Work Order history to hypothesize root cause
3. **Safety Escalation Agent** — flags hazard patterns for immediate human review, bypassing normal routing
4. **Dispatch Agent** — recommends technician, drafts a Work Order
5. **Review Dashboard** — human-in-the-loop queue with full reasoning trace, approve/override controls

## Tech stack

- **Salesforce Developer Edition** (Dev + Staging orgs)
- **Copado Essentials (Free tier)** — Quick Deployment for org-to-org metadata promotion
- **GitHub Projects** — Agile backlog (this repo)
- **Python + Claude API** — agent orchestration (Sprints 2+)
- **dataloader.io** — synthetic data loading

## Data model

| Object | Key fields | Purpose |
|---|---|---|
| Case (standard) | AI_Priority__c, AI_Confidence__c, AI_Root_Cause__c, AI_Reasoning_Log__c, Safety_Flag__c | Agent output lands here |
| Asset (standard) | Asset_Key__c (External ID), Equipment_Type__c, Criticality__c, Last_Maintenance_Date__c | Equipment being serviced |
| Work_Order__c (custom) | Case__c, Asset__c, Assigned_Technician__c, Status__c, Resolution_Notes__c | Dispatch output |
| Knowledge_Article__c (custom) | Title__c, Body__c, Equipment_Type__c, Category__c | RAG source for root-cause reasoning |

## Sprint 1 status: ✅ Complete

- Dev + Staging Developer Edition orgs created and connected via Copado Essentials
- Full schema built across all four objects
- 157 synthetic records loaded with zero errors: 25 Assets, 12 Knowledge Articles, 20 historical Work Orders, 100 Cases (10 deliberately safety-critical but unflagged, for Sprint 4 to catch)
- GitHub repo + Projects board established for backlog tracking

## Roadmap

| Sprint | Epic | Goal |
|---|---|---|
| 1 | Foundation & Data Model | ✅ Done |
| 2 | Triage Agent | Classify urgency/category, write back to Case |
| 3 | Root Cause Agent (RAG) | Retrieve KB + history, propose root cause |
| 4 | Safety Escalation Agent | Catch hazard patterns, bypass normal routing |
| 5 | Dispatch Agent | Recommend technician, draft Work Order |
| 6 | Review Dashboard | Queue, reasoning trace, approve/override |
| 7 | Public Deployment & Narrative | Ship it, record it, prep interview talking points |

## Notable decisions and debugging along the way

- **Copado Essentials' free tier doesn't include Pipelines or Work Items** (Plus-only) — the backlog moved to GitHub Projects instead, which also happens to be more broadly recognizable in interviews.
- **Standard Data Loader now requires configuring your own OAuth External Client App** before login — used dataloader.io (browser-based, same underlying Bulk API) instead, avoiding a Java install and OAuth app setup for what should be a simple CSV import.
- **Standard Asset object requires an Account or Contact** on every record, undocumented in the original schema plan — resolved by creating one placeholder Account representing the facility that owns the equipment.
