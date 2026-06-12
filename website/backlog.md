# Take Five · Product Backlog

Last updated: June 2026  
Scoring method: RICE (Reach × Impact × Confidence / Effort)  
Eden Alternative domains: Identity · Connectedness · Security · Autonomy · Meaning · Growth · Joy

---

## In Progress / Next Build
> Items sorted by RICE score (descending).

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

### Safety & Health Signal Tracker
**Eden domains:** Security  
**RICE: 1.43** · Reach 100% · Impact 3 · Confidence 0.95 · Effort 2 wks  
**Description:** T5 reads every message looking for safety and health signals — falls, injuries, recurring symptoms, behavioral changes — and saves them as structured records. Surfaces patterns in the weekly digest that wouldn't be visible week-to-week. Timeline view in the app for showing Dr. Yu.  
**Note:** T5 is not a first responder. This is retrospective pattern detection, not emergency alerting.  
**Pilot finding:** Mom's fall came through a side conversation with Jennifer Smith, never hit the family chat. Dad's fall was an afterthought at the end of a longer message. Neither was logged.  
**Open questions:**
- When T5 spots a fall or incident, announce in chat or quietly log and surface in digest?
- How to handle deduplication when the same event is mentioned across multiple messages?
- Severity tiers: how to treat soft signals (seemed confused) vs. hard events (fell, firemen called)?
- Should the timeline feed into the appointment prep packet automatically?

---

## Backlog — Prioritized
> Items sorted by RICE score (descending).

### Document Registry
**Eden domains:** Security, Autonomy  
**RICE: 0.75** · Reach 100% · Impact 2 · Confidence 0.75 · Effort 2 wks  
**Description:** Shared registry of where important documents live — will, power of attorney, advance directive, insurance, financial records. Phase 1: location registry only (where is it, who to call). Phase 2: actual file storage with access controls.  
**Pilot finding:** Dad keeps account passwords on a barely legible printed page. Most adult children don't know where legal documents are until something goes wrong.  
**Open questions:**
- Registry only vs. actual file storage — which is useful enough to build first?
- Should access be tiered — some visible to all, sensitive items to certain family members only?
- Does a registry entry fit as a new `resource_type` in `clinical_records` or does it need its own table?

### Family Call Transcript Ingestion
**Eden domains:** Connectedness, Security  
**RICE: 0.70** · Reach 100% · Impact 2 · Confidence 0.7 · Effort 2 wks  
**Description:** T5 processes transcripts from the weekly family call, extracting the same care-relevant signals it picks up from group chat. Phase 1: manual upload. Phase 2: automated once recording tool is in place.  
**Pilot finding:** Both recent falls were first mentioned on family calls, not in chat. Dr. Yu July 13 appointment and preference to see Dr. Yu vs. PA both came from June 8 call. None of it would be in T5 without Eric manually posting it.  
**Open questions:**
- What tool generates the transcript — Tactiq, Otter.ai, manual notes?
- Does transcript content go into `messages` table with a distinct `source` type, or separate table?
- Upload surface for phase 1: paste into GroupMe, upload via app, or dedicated endpoint?

### Activity Calendar Photo Ingestion
**Eden domains:** Joy, Connectedness, Growth (anti-boredom)  
**RICE: 0.52** · Reach 100% · Impact 2 · Confidence 0.65 · Effort 2.5 wks  
**Description:** Family member photographs the retirement community's monthly activity calendar. T5 ingests it via vision pipeline (same as prescription label flow), extracts events, stores them in `calendar_items`. Enables: surfacing relevant activities in digest, noticing when senior hasn't attended anything in two weeks, feeding Life Log automatically, generating conversation starters for family visits.  
**Pilot relevance:** Pilot family is in a retirement community — immediately testable, not theoretical.  
**Dependency:** Calendar items table, vision pipeline (already partially built for prescription labels).

### Calendar & Appointments Table
**Eden domains:** Security, Connectedness  
**RICE: 0.45** · Reach 100% · Impact 1 · Confidence 0.9 · Effort 2 wks  
**Description:** Structured storage for calendar items extracted from chat or ingested via other means. Foundation for automated appointment prep packet triggering and future calendar features. Infrastructure item — no direct user value on its own but required by several higher-scored features.  
**Proposed schema:** Single `calendar_items` table with `item_type` (medical / care_visit / activity), `source` (chat_extracted / photo_ingested / manual), `scheduled_at`, `person_id`, `circle_id`, `metadata` JSONB for type-specific fields.  
**Dependency for:** Automated appointment prep packet trigger, activity calendar ingestion.  
**Decision deferred:** Build on-demand prep packet first; instrument appointments table in parallel.

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
