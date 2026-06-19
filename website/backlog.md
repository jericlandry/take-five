# Take Five · Product Backlog

Last updated: June 2026  
Scoring method: RICE (Reach × Impact × Confidence / Effort)  
Eden Alternative domains: Identity · Connectedness · Security · Autonomy · Meaning · Growth · Joy

---

## Backlog — Prioritized
> All items, sorted by RICE score (descending). Dependency notes flag where build order should deviate from pure score ranking.

### Appointment Prep Packet
**Eden domains:** Security, Autonomy  
**RICE: 1.8** · Reach 100% · Impact 3 · Confidence 0.9 · Effort 1.5 wks  
**Description:** A structured briefing generated before a medical appointment, covering recent symptoms mentioned in chat, mood patterns, current medications, and open family questions. Designed for the family member attending the visit — readable the night before, scannable on a phone in the exam room.  
**Trigger:** On-demand first (`@T5 prep for Dad's appointment Thursday`). Automate once appointments table exists.  
**Output:** App view (mobile-optimized, checklist-style) + GroupMe post summarizing what was sent.  
**Pilot test:** Dad's neck pain (mentioned 4x since April across multiple people). Dr. Yu appointment July 13.  
**Open questions:**
- How far back should T5 look — from last visit with same doctor, or fixed 30–60 day window?
- Should family members be able to add their own questions to the packet before the visit?
- Should the app view have more detail than the GroupMe post?

---

### Circle-Level Context Field
**Eden domains:** Identity, Autonomy  
**RICE: 1.8** · Reach 100% · Impact 1 · Confidence 0.9 · Effort 0.5 wks  
**Description:** Admin-editable free-text field on `care_circles` (e.g. `agent_context`) injected into the system prompt at runtime — e.g. "both parents have medical backgrounds, comfortable with clinical terminology" or communication preferences (hearing, pacing). Per-circle, so family circle and elder circle can carry different context. Surfaced in existing Circles panel config tab.  
**Dependency:** None — can be built independently.  
**Context:** Part of the Elder Circle feature set (new care circle type) — see below for related items.

---

### Clinical Signal Detection
**Eden domains:** Security  
**RICE: 1.43** · Reach 100% · Impact 3 · Confidence 0.95 · Effort 2 wks  
**Description:** A general clinical signal detection layer — a separate async agent within the same service that reads every stored message and extracts structured health and safety observations. Runs post-message-storage, never blocks conversation flow. Returns an array of signals per message (one message can produce multiple records). Pilot phase: log silently to calibrate detection quality before any surfacing behavior is enabled.

**Signal taxonomy (prompt-configured in LangSmith):**
- Safety — falls, injuries, emergencies, near-misses
- Functional — mobility shifts, eating changes, sleep changes, confusion, withdrawal from activities
- Symptom — pain, nausea, dizziness, shortness of breath
- Mood/affect — good days, bad days, tearfulness, agitation, flat affect
- Medication — refusals, side effect mentions, missed doses

**Corroboration mechanic:** For soft/oblique signals below a confidence threshold, T5 composes a single grouped question covering the most important signals from that message and posts it during the next conversation lull (see: Lull Detection & Proactive Check-in). Does not ask immediately — waits for quiet. For high-confidence hard incidents (a fall, a 911 call), T5 logs silently — asking for corroboration of a fall feels tone-deaf. Corroboration responses are logged as child signal records via `parent_id` and serve as human-labeled training data.

**Cross-channel corroboration:** SMS senders (aides, nurses) are outside the GroupMe thread. When T5 posts a corroboration question to GroupMe, it also sends an SMS acknowledgment back to the original reporter: "Thanks for flagging that — I've shared it with the family." If the family confirms, T5 closes the loop via SMS: "The family confirmed they've noticed similar changes — flagged for the weekly digest." This makes SMS feel like a connected channel, not a dead drop.

**Schema:**
```sql
clinical_signals
- id
- parent_id              -- null = original detection; not null = corroboration response
- message_id             -- FK to messages (source of detection or response)
- circle_id
- subject_id             -- person being described (not the sender)
- signal_category        -- safety / functional / symptom / mood / medication
- signal_type            -- fall / confusion / pain / refused_meds / etc.
- raw_excerpt            -- exact text that triggered detection
- mention_style          -- direct / oblique
- confidence             -- float
- channel                -- groupme / sms
- response_type          -- null (original) / confirmed / contradicted / added_detail
- corroboration_requested
- corroboration_requested_at
- superseded_by_id       -- FK to clinical_signals; handles deduplication across channels
- detected_at
```

**Key design decisions:**
- Single table: corroboration responses are signals with a `parent_id`, not a separate table
- `subject_id` is who is being described, not who sent the message — critical for multi-senior circles
- `superseded_by_id` handles cross-channel deduplication (Rosa mentions fall via SMS, Jennifer mentions same fall in GroupMe next day)
- Severity is deferred — it's a property of patterns across signals, not of individual signals
- Pattern detection is deferred — derives from this table when ready; no schema changes needed

**Prompt management:** Detection prompt in LangSmith (`t5-signal-detection`). Corroboration grouping prompt in LangSmith (`t5-signal-corroboration`). Taxonomy and confidence thresholds are prompt configuration, not code.

**Note:** T5 is not a first responder. This is retrospective pattern detection, not emergency alerting.

**Pilot finding:** Autumn's May 1 message contained five distinct signals in one update — fall risk, sleep, medication timing, wheelchair refusal, energy/stamina. Dad's nipple symptom raised by Autumn was never followed up on and disappeared into the chat. Neither would have been logged or surfaced without this feature.

**Deferred:**
- Pattern detection across signals
- Severity tiers (emerges from patterns)
- Timeline view in app
- FHIR translation

---

### Lull Detection & Proactive Check-in
**Eden domains:** Connectedness, Security  
**RICE: 1.20** · Reach 100% · Impact 2 · Confidence 0.9 · Effort 1 wk  
**Description:** A daily cron (`take-five-checkin`, runs 7pm CT) that checks each active care circle for conversation lulls and posts a proactive T5 message when the circle has been quiet long enough to warrant one. This is the mechanism by which T5 speaks up unprompted — a meaningful behavioral shift from the current pull-only model where T5 only responds when asked.

**Lull threshold:** 48 hours of no inbound messages. Start here and tune based on pilot observation — some circles will be naturally quieter than others.

**Priority order when T5 has something to say:**
1. Pending clinical signal corroboration — signals detected but corroboration not yet requested
2. Life Log elicitation — personalized question based on recent Life Log gaps for the subject
3. General engagement prompt — fallback if nothing else is pending

The lull is the trigger; the content is prioritized. T5 doesn't ask two questions in the same lull — one message per check-in.

**Per-circle state:** `last_checkin_at` timestamp on `care_circles` prevents duplicate check-ins during extended lulls. If T5 already posted a check-in within the past 48 hours, skip even if the circle is still quiet.

**Infrastructure:**
```
take-five-summary    weekly         → digest generation (existing)
take-five-checkin    daily 7pm CT   → lull detection + proactive messaging (new)
```
Render cron syntax for 7pm CT (CDT = UTC-5): `0 0 * * *` — verify UTC offset before deploying.

**Behavioral note:** Fixed daily time is appropriate for pilot — simplicity while learning lull patterns. Future state: introduce per-circle time variance (randomized delay 0–4 hours post-cron) so check-ins don't feel synchronized across circles or robotic in timing.

**Dependency:** Clinical Signal Detection (for corroboration content). Mid-week Life Log elicitation (for fallback content). Both should be built or at least designed before this cron goes live, so the priority logic has something to work with.

---

### Circle Typing
**Eden domains:** Connectedness, Identity  
**RICE: 0.95** · Reach 100% · Impact 1 · Confidence 0.95 · Effort 1 wk  
**Description:** Adds `circle_type` (`family` | `elder`) to `care_circles`. Each family can have a second, optional elder circle alongside their family circle — distinct purpose: connection and story, not coordination. Digest/prep-packet generation gated to `circle_type='family'`. Senior gets a `circle_memberships` row in both circles.  
**Note:** Foundational — prerequisite for Elder-Circle Agent Prompts, Cross-Circle Digest Surfacing, and Email Channel below, despite the modest score. **Build before those three regardless of ranking.**  
**Context:** Part of the Elder Circle feature set (new care circle type) — see below for related items.

---

### Document Registry
**Eden domains:** Security, Autonomy  
**RICE: 0.75** · Reach 100% · Impact 2 · Confidence 0.75 · Effort 2 wks  
**Description:** Shared registry of where important documents live — will, power of attorney, advance directive, insurance, financial records. Phase 1: location registry only (where is it, who to call). Phase 2: actual file storage with access controls.  
**Pilot finding:** Dad keeps account passwords on a barely legible printed page. Most adult children don't know where legal documents are until something goes wrong.  
**Open questions:**
- Registry only vs. actual file storage — which is useful enough to build first?
- Should access be tiered — some visible to all, sensitive items to certain family members only?
- Does a registry entry fit as a new `resource_type` in `clinical_records` or does it need its own table?

---

### Family Call Transcript Ingestion
**Eden domains:** Connectedness, Security  
**RICE: 0.70** · Reach 100% · Impact 2 · Confidence 0.7 · Effort 2 wks  
**Description:** T5 processes transcripts from the weekly family call, extracting the same care-relevant signals it picks up from group chat. Phase 1: manual upload. Phase 2: automated once recording tool is in place.  
**Pilot finding:** Both recent falls were first mentioned on family calls, not in chat. Dr. Yu July 13 appointment and preference to see Dr. Yu vs. PA both came from June 8 call. None of it would be in T5 without Eric manually posting it.  
**Open questions:**
- What tool generates the transcript — Tactiq, Otter.ai, manual notes?
- Does transcript content go into `messages` table with a distinct `source` type, or separate table?
- Upload surface for phase 1: paste into GroupMe, upload via app, or dedicated endpoint?

---

### Activity Calendar Photo Ingestion
**Eden domains:** Joy, Connectedness, Growth (anti-boredom)  
**RICE: 0.52** · Reach 100% · Impact 2 · Confidence 0.65 · Effort 2.5 wks  
**Description:** Family member photographs the retirement community's monthly activity calendar. T5 ingests it via vision pipeline (same as prescription label flow), extracts events, stores them in `calendar_items`. Enables: surfacing relevant activities in digest, noticing when senior hasn't attended anything in two weeks, feeding Life Log automatically, generating conversation starters for family visits.  
**Pilot relevance:** Pilot family is in a retirement community — immediately testable, not theoretical.  
**Dependency:** Calendar items table, vision pipeline (already partially built for prescription labels).

---

### Calendar & Appointments Table
**Eden domains:** Security, Connectedness  
**RICE: 0.45** · Reach 100% · Impact 1 · Confidence 0.9 · Effort 2 wks  
**Description:** Structured storage for calendar items extracted from chat or ingested via other means. Foundation for automated appointment prep packet triggering and future calendar features. Infrastructure item — no direct user value on its own but required by several higher-scored features.  
**Proposed schema:** Single `calendar_items` table with `item_type` (medical / care_visit / activity), `source` (chat_extracted / photo_ingested / manual), `scheduled_at`, `person_id`, `circle_id`, `metadata` JSONB for type-specific fields.  
**Dependency for:** Automated appointment prep packet trigger, activity calendar ingestion.  
**Decision deferred:** Build on-demand prep packet first; instrument appointments table in parallel.

---

### Elder-Circle Agent Prompts
**Eden domains:** Identity, Connectedness, Meaning  
**RICE: 0.28** · Reach 20% · Impact 2 · Confidence 0.7 · Effort 1 wk  
**Description:** New LangSmith prompt family (`t5-elder-system-prompt`, `t5-elder-story`) for a warm, conversational, Story-oriented tone — distinct from the task/coordination tone of `t5-system-prompt`. `circle_type` determines which prompt family loads at runtime. Versioned independently.  
**Dependency:** Circle Typing.  
**Context:** Part of the Elder Circle feature set (new care circle type) — see below for related items.

---

### Cross-Circle Digest & Prep Packet Surfacing
**Eden domains:** Connectedness, Identity  
**RICE: 0.24** · Reach 30% · Impact 2 · Confidence 0.6 · Effort 1.5 wks  
**Description:** Elder-circle conversations and Story answers inform the family circle's weekly digest and appointment prep packets, without the elder circle producing its own artifacts. Digest generation (`summaries.py`, `t5-week-summary`) widens its message query to pull from both circles for the same family, filtered by shared `person_id`. Query/aggregation change only — no schema change beyond `circle_type`.  
**Open question:** Does elder-circle content get its own labeled digest section ("From your conversations with Mom this week...") or blend into the general narrative?  
**Dependency:** Circle Typing.  
**Context:** Part of the Elder Circle feature set (new care circle type) — see below for related items.

---

### Email Channel for Elder Circle
**Eden domains:** Identity, Connectedness, Meaning  
**RICE: 0.10** · Reach 20% · Impact 3 · Confidence 0.5 · Effort 3 wks  
**Description:** Senior interacts via email rather than phone/voice or GroupMe — better fit for reflective Story answers, lower-pressure than a call. New `source = 'email'` (already supported by v2 `source`/`author_type` schema). Inbound via webhook/IMAP -> `messages` row (`circle_id` = elder circle, `person_id` = senior, `author_type = 'senior'`). Outbound: agent-initiated emails (Story questions, periodic check-ins) via transactional email — ties into pending `takefive.care` email setup decision.  
**Cadence:** Weekly to start, aligned with Story rhythm — daily risks feeling like spam over email.  
**Dependency:** Circle Typing. Highest impact of the elder-circle items but largest lift and lowest confidence — new integration territory.  
**Context:** Part of the Elder Circle feature set (new care circle type) — see below for related items.

---

## Elder Circle — Feature Set Overview

A second, optional care circle per family ("elder circle") alongside the existing family circle. The senior participates directly via email. Produces no artifacts of its own (no weekly digest, no prep packet) — its content can be surfaced into the family circle's digest/prep packet instead.

Four items above belong to this feature set: **Circle-Level Context Field** (1.8), **Circle Typing** (0.95), **Elder-Circle Agent Prompts** (0.28), **Cross-Circle Digest & Prep Packet Surfacing** (0.24), and **Email Channel for Elder Circle** (0.10).

**Suggested build sequence within this set** (respects the Circle Typing dependency, which the raw RICE ranking alone doesn't capture):  
1. Circle-Level Context Field (no dependency, quick win)  
2. Circle Typing (prerequisite for the rest)  
3. Cross-Circle Digest & Prep Packet Surfacing  
4. Elder-Circle Agent Prompts  
5. Email Channel for Elder Circle

---

## Philosophy & Framework Notes

### Eden Alternative Alignment
Take Five was derived from lived experience independently of the Eden Alternative framework. The alignment is validation, not inspiration — both arrived at the same conclusions through different paths.

**Eden's three enemies of well-being:** Loneliness · Helplessness · Boredom  
**Eden's three antidotes:** Companionship · Purpose · Variety/Spontaneity

**Current Take Five coverage by Eden domain:**
- Identity ✅ — The Story (memoir)
- Connectedness ✅ — Weekly digest, care circle
- Security ✅ — Safety tracker, appointment prep, document registry
- Autonomy ⚠️ — Underserved. Opportunity: surfacing senior's expressed preferences and desires
- Meaning ⚠️ — Underserved. Opportunity: giving the senior ways to contribute, not just receive
- Growth ⚠️ — Partially served by Life Log. More opportunity here.
- Joy ✅ — Life Log (books, shows, activities)

**Future feature directions from Eden gaps (not yet in backlog):**
- Voice of Mom/Dad in the digest — surfacing expressed desires and preferences, not just observations
- "Give, not just receive" prompts — opportunities for the senior to contribute to the family (share a recipe, weigh in on a decision)
- Routine variety signals — flag when the senior's week has been identical for too long
- Preference capture — structured record of favorites, rhythms, what kind of visitor she likes; briefing layer for new aides

**Partnership note:** Eden membership is a future credibility and sales channel consideration, particularly relevant for TXCCC/person-centered care ecosystem conversations. Not an immediate priority.

---

## How We Prioritize — RICE Scoring

### RICE Score
Every feature is scored using the RICE method: **Reach × Impact × Confidence / Effort**. The resulting number isn't meaningful in absolute terms — what matters is the relative ranking. A higher score means more value delivered with less effort and more certainty.

### The Four Factors

**Reach** — What percentage of the care circle does this affect, and how often? Scored as a percentage (0–100%). A feature that touches every family member every week scores 100%. One that only applies occasionally scores lower.

**Impact** — How much does it actually help when it does reach someone? Scored 1–3:  
- 3 = meaningfully changes behavior, prevents a real problem, or reduces significant burden  
- 2 = clearly useful, noticeable improvement  
- 1 = nice to have, marginal improvement

**Confidence** — How sure are we the feature will work as expected? Scored 0–1:  
- 0.9–1.0 = validated by pilot data or direct user feedback  
- 0.7–0.8 = reasonable assumption, some evidence  
- 0.5–0.6 = educated guess, higher uncertainty

**Effort** — How long will it take to build, in weeks? Lower effort scores better. A two-week build scores half what a one-week build scores at the same reach, impact, and confidence.

### Reading the Scores
Scores above 1.0 represent high-priority features with strong evidence and manageable effort. Scores below 0.5 are either lower-confidence, higher-effort, or infrastructure work that enables other features rather than delivering direct user value.
