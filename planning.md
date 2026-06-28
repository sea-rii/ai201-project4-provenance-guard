# Provenance Guard — Planning Document

---

## 1. Architecture Narrative

A piece of text enters the system via `POST /submit`. The request carries the raw text
and an optional creator ID. The first thing the system does is persist a submission record
to SQLite — this gives every piece of text a unique ID before any analysis runs, so
nothing is ever lost even if detection fails mid-flight.

The text is then passed simultaneously to two independent detectors:

**Signal 1 — LLM Classifier (Groq):** The raw text is sent to a Groq-hosted model with a
structured prompt asking it to assess the likelihood that the text was AI-generated.
The model returns a probability between 0 and 1. This signal captures semantic and
stylistic coherence holistically — things like suspiciously uniform hedging language,
over-explained transitions, and the absence of the small inconsistencies that human
writers leave behind.

**Signal 2 — Stylometric Heuristics:** The text is analysed in pure Python against three
measurable statistical properties: (a) sentence-length variance — AI text tends toward
uniformly medium-length sentences; (b) type-token ratio (TTR) — the ratio of unique words
to total words, a proxy for vocabulary diversity that AI text often scores higher on in a
suspiciously consistent way; (c) punctuation density — human writers use dashes, ellipses,
and irregular comma placement at rates that differ from model outputs. Each sub-measure
is normalised to [0, 1] and combined into a single heuristic score.

A **confidence scorer** takes the two raw signal scores and produces a combined score
(weighted average, with weights documented below) and a human-readable **transparency
label**: one of `LIKELY_HUMAN`, `UNCERTAIN`, or `LIKELY_AI`, each with a confidence
percentage attached.

The label, both raw scores, the combined score, and a timestamp are written to the audit
log (SQLite `submissions` table). The response returned to the caller contains the
submission ID, the label, the confidence score, and both individual signal scores.

If a creator believes their work was misclassified, they call `POST /appeal/:id`. The
system updates the submission record with an `appealed` flag and a free-text reason,
appends an appeal event to the audit log, and returns the updated record. No
re-analysis runs on appeal — the appeal is a human-review flag, not an automated
re-score.

---

## 2. Detection Signals

### Signal 1: LLM-Based Classification (Groq)

**What it measures:** Holistic semantic and stylistic plausibility of the text as
human-written. The model has been trained on vast human writing and can recognise
patterns — over-smooth transitions, hedging clusters, unnaturally balanced paragraph
lengths — that correlate with AI generation.

**Why it differs:** Human writing carries the author's cognitive fingerprints: topic
drift, uneven elaboration, idiosyncratic word choices, and rhetorical habits. AI models
generate token-by-token toward high-probability continuations, which produces a
different distributional signature even when the surface content looks identical.

**Blind spots:**
- Heavily edited AI text (a human polishes a GPT draft) can fool it.
- Very short texts give it too little signal to work with reliably.
- Domain-specific human writing (legal, technical) can look "AI-like" to the classifier.
- The model's own training distribution affects what it flags — it may be biased toward
  flagging text that resembles its own outputs.

---

### Signal 2: Stylometric Heuristics

**What it measures:** Three structural, statistically computable properties:

| Sub-measure | What it captures |
|---|---|
| Sentence-length variance | Standard deviation of sentence word-counts. AI text is more uniform. |
| Type-token ratio (TTR) | Unique words ÷ total words. AI text is often suspiciously high and consistent. |
| Punctuation density | Punctuation marks per 100 words. Human writing uses dashes, ellipses, and irregular commas at different rates. |

**Why it differs:** AI models sample from a learned distribution that smooths out the
variance humans introduce naturally through fatigue, enthusiasm, and stylistic habit.
Statistical measures can detect that smoothness even when the content is perfectly
coherent.

**Blind spots:**
- Academic or formal human writing is deliberately uniform — it will score as AI-like.
- A skilled prompt engineer can instruct a model to introduce artificial variance.
- Short texts (< ~100 words) have unreliable statistics — small-n variance estimates
  swing wildly.
- TTR is length-dependent: longer texts always have lower TTR regardless of authorship.

---

## 3. The False Positive Problem

**Scenario:** A novelist submits a chapter written in a deliberately controlled,
minimalist style (think Cormac McCarthy without punctuation, or a technical writer who
keeps sentences short and parallel). Both signals misfire:

- The LLM classifier sees the uniform hedging-free prose and the absence of casual
  asides and flags it as AI-like (score: 0.72).
- The stylometric analyser sees low sentence-length variance and high TTR and returns
  a heuristic score of 0.68.
- Combined score: ~0.70 → label: `LIKELY_AI` at 70% confidence.

**How the system reflects uncertainty:**
The combined score of 0.70 is above the `LIKELY_AI` threshold (>0.65) but not
overwhelmingly so. The label is `LIKELY_AI (70%)` — the percentage is surfaced to the
user precisely so a score of 70 reads differently from a score of 95. The response
also returns both individual signal scores, so a human reviewer can see that neither
signal was decisive on its own.

**How the creator appeals:**
The submission response contains a submission ID. The creator calls `POST /appeal/:id`
with a plain-text reason ("This is my own prose style; I can provide manuscript
drafts"). The system sets `appealed = true` in the audit log and records the reason.
The label does not change automatically — but the audit record now carries a flag that
a human reviewer must look at before any downstream action is taken.

**Architectural implication:** The system must never present a label as a binary
verdict. Every label is accompanied by a confidence score and both raw signal values.
The UI/API contract makes uncertainty legible, not hidden.

---

## 4. API Surface

### `POST /submit`
**Purpose:** Submit text for provenance analysis.
**Accepts:**
```json
{
  "text": "string (required)",
  "creator_id": "string (optional)"
}
```
**Returns:**
```json
{
  "submission_id": "integer",
  "label": "LIKELY_HUMAN | UNCERTAIN | LIKELY_AI",
  "confidence": "float (0–1)",
  "signals": {
    "llm_score": "float (0–1)",
    "stylometric_score": "float (0–1)"
  },
  "submitted_at": "ISO 8601 timestamp"
}
```

---

### `GET /submission/:id`
**Purpose:** Retrieve a previous submission and its analysis result.
**Returns:** Same shape as `/submit` response, plus `appealed` boolean and
`appeal_reason` string if applicable.

---

### `POST /appeal/:id`
**Purpose:** Flag a submission as misclassified and request human review.
**Accepts:**
```json
{
  "reason": "string (required)"
}
```
**Returns:**
```json
{
  "submission_id": "integer",
  "appealed": true,
  "appeal_reason": "string",
  "message": "Appeal recorded. A human reviewer will assess this submission."
}
```

---

### `GET /audit-log`
**Purpose:** Return the full audit log of submissions and appeal events.
**Returns:**
```json
{
  "entries": [
    {
      "submission_id": "integer",
      "label": "string",
      "confidence": "float",
      "llm_score": "float",
      "stylometric_score": "float",
      "appealed": "boolean",
      "appeal_reason": "string | null",
      "submitted_at": "timestamp",
      "appealed_at": "timestamp | null"
    }
  ]
}
```

---

## 5. Architecture Diagram

### Flow 1: Submission

```
Client
  │
  │  POST /submit  { text, creator_id }
  ▼
┌─────────────────────────────────┐
│         Flask Route Handler      │
│  • validates input               │
│  • creates submission record     │
└────────────┬────────────────────┘
             │ raw text
     ┌───────┴────────┐
     │                │
     ▼                ▼
┌─────────┐    ┌──────────────────┐
│  Groq   │    │  Stylometric     │
│  LLM    │    │  Analyser        │
│Classifier│   │  (pure Python)   │
└────┬────┘    └───────┬──────────┘
     │                 │
     │ llm_score (0–1) │ stylometric_score (0–1)
     └────────┬────────┘
              ▼
     ┌─────────────────┐
     │ Confidence      │
     │ Scorer          │
     │ weighted avg    │
     │ → combined score│
     │ → label text    │
     └────────┬────────┘
              │ label, confidence, both scores
              ▼
     ┌─────────────────┐
     │   Audit Log     │
     │   (SQLite)      │
     │ writes record   │
     └────────┬────────┘
              │
              ▼
         Response to Client
         { submission_id, label, confidence, signals, submitted_at }
```

---

### Flow 2: Appeal

```
Client
  │
  │  POST /appeal/:id  { reason }
  ▼
┌─────────────────────────────────┐
│       Flask Route Handler        │
│  • looks up submission by ID     │
│  • validates submission exists   │
└────────────┬────────────────────┘
             │
             ▼
     ┌─────────────────┐
     │   Audit Log     │
     │   (SQLite)      │
     │ sets appealed=1 │
     │ writes reason   │
     │ writes timestamp│
     └────────┬────────┘
              │
              ▼
         Response to Client
         { submission_id, appealed: true, appeal_reason, message }
```

---

## 6. Confidence Scoring Thresholds & Weights

| Combined Score | Label |
|---|---|
| < 0.35 | `LIKELY_HUMAN` |
| 0.35 – 0.65 | `UNCERTAIN` |
| > 0.65 | `LIKELY_AI` |

**Signal weights (initial, tunable):**
- LLM classifier: **0.6**
- Stylometric heuristics: **0.4**

Rationale: The LLM signal captures a richer set of features and has been pre-trained
on large corpora; the heuristic signal is transparent and auditable but has more
failure modes on short or specialised texts. The heuristic weight is intentionally
non-trivial so it can override a borderline LLM score — the combination is the point.

---

## 7. Stretch Features — Pre-Implementation Plans

These features are planned for implementation at the appropriate milestone. Each section
documents the design decision before any code is written, per the project requirement.

---

### SF-1: Ensemble Detection (3+ signals)

**Planned at:** Milestone 2 (alongside core signal implementation)

**Third signal — Perplexity Proxy (n-gram repetition rate):**
Measures how predictable the text is at the bigram/trigram level. AI-generated text
tends to avoid repeating n-grams (models are penalised for repetition during training),
producing a characteristic low-repetition-rate signature. Human writing — especially
informal or passionate writing — naturally repeats phrases, sentence openers, and
connective tissue.

What it measures: ratio of repeated bigrams to total bigrams in the text.
Why it differs: RLHF-trained models are explicitly penalised for repetition; human
writers are not.
Blind spot: Highly polished human prose (journalism, academic writing) also avoids
repetition deliberately.

**Updated weights (3-signal ensemble):**

| Signal | Weight | Rationale |
|---|---|---|
| LLM classifier (Groq) | 0.50 | Richest feature set, pre-trained |
| Stylometric heuristics | 0.30 | Structural, auditable, independent |
| N-gram repetition rate | 0.20 | Lightweight, captures training artifact |

**Voting approach:** Weighted average. If all three signals agree (all > 0.65 or all
< 0.35), confidence is boosted by 0.05. If signals split (one disagrees with the other
two), the label is pushed toward `UNCERTAIN` regardless of the weighted average, and
the response flags `"signals_disagree": true`.

---

### SF-2: Provenance Certificate ("Verified Human" credential)

**Planned at:** Milestone 3 (after core detection and appeal flow are stable)

**Verification step:** A creator can request a certificate for a submission that was
labelled `LIKELY_HUMAN` with confidence ≥ 0.80. The verification request triggers a
secondary Groq call with a stricter prompt that asks for a detailed stylistic
fingerprint of the text. If the secondary score also clears the threshold, a
certificate record is created in SQLite.

**Certificate contents:**
- `certificate_id`: UUID
- `submission_id`: FK
- `creator_id`: string
- `issued_at`: timestamp
- `fingerprint_summary`: 1–2 sentence human-readable description of the detected
  stylistic signature (generated by Groq)
- `confidence`: the combined score that earned the certificate

**Display:** `GET /certificate/:certificate_id` returns the certificate as JSON. The
certificate embeds a short `badge_text` field (e.g. "Verified Human — 91% confidence,
issued 2025-06-01") suitable for a creator to embed in their content's metadata or
display in a UI.

**Endpoint added:** `POST /certify/:submission_id`, `GET /certificate/:id`

---

### SF-3: Analytics Dashboard

**Planned at:** Milestone 4 (after all core routes exist and audit log is populated)

**Implementation:** A single HTML page served at `GET /dashboard` by Flask (no
separate frontend framework — plain HTML + inline Chart.js from CDN). The page
queries the SQLite audit log directly via a `GET /analytics` JSON endpoint and
renders three panels:

| Panel | Metric | Why useful |
|---|---|---|
| Detection distribution | % LIKELY_HUMAN / UNCERTAIN / LIKELY_AI over time | Shows system bias drift |
| Appeal rate | Appeals ÷ total submissions, rolling 7-day | Proxy for false positive rate |
| Signal disagreement rate | % of submissions where signals_disagree=true | Reveals cases needing threshold tuning |

The third metric (signal disagreement rate) is the "one additional metric of your
choosing" — it's operationally the most useful because persistent disagreement between
signals is an early warning that a threshold or weight needs recalibration.

**Endpoints added:** `GET /analytics` (JSON), `GET /dashboard` (HTML)

---

### SF-4: Multi-Modal Support (image descriptions)

**Planned at:** Milestone 4 (parallel to analytics, after core text pipeline is solid)

**Second content type:** Image descriptions / alt-text. A creator can submit a URL
to an image alongside a text description. The pipeline:

1. Accepts `{ "text": "...", "image_url": "..." }` at `POST /submit`
2. If `image_url` is present, a Groq vision call describes the image independently
3. The LLM classifier signal compares the submitted description against the
   Groq-generated description for semantic consistency — a human who actually looked
   at the image will describe things a model wouldn't notice (composition choices,
   emotional tone, background details)
4. The consistency score becomes a fourth signal (weight 0.15, other weights scaled
   down proportionally) when image_url is present; ignored otherwise

**Why this is genuinely distinct:** It's not "AI vs human wrote this description" — it's
"does the description reflect actual human observation of this image." That's a
different property from stylometry or LLM classification of the text alone.

**Endpoint change:** `POST /submit` accepts optional `image_url` field; response gains
optional `image_consistency_score` field when applicable.

---

## 8. Uncertainty Representation

### What a score means

The combined confidence score is a float in [0, 1] representing the system's estimated
probability that the text is AI-generated. It is **not** a binary flip at 0.5.

| Score range | Meaning | Label assigned |
|---|---|---|
| 0.00 – 0.34 | Evidence leans clearly human | `LIKELY_HUMAN` |
| 0.35 – 0.65 | Signals are inconclusive | `UNCERTAIN` |
| 0.66 – 1.00 | Evidence leans clearly AI | `LIKELY_AI` |

A score of 0.60 means: the weighted signal combination leans AI but does not cross the
threshold for a confident verdict. The system will return `UNCERTAIN` with 60% shown
to the user — not a positive AI detection. This is intentional: the cost of a false
positive (wrongly accusing a human) is higher than the cost of returning `UNCERTAIN`.

A score of 0.95 means all three signals agreed strongly and the ensemble boosted
confidence. A score of 0.35 is the minimum to leave `LIKELY_HUMAN` — it means at least
one signal gave a non-trivial AI score, which should be visible in the raw signal
breakdown the API always returns.

### How raw signals map to the combined score

Each signal returns a float in [0, 1] independently:
- `llm_score`: 0 = model is confident the text is human; 1 = model is confident it is AI
- `stylometric_score`: 0 = statistically human-like variance; 1 = statistically AI-like
  uniformity
- `ngram_score` (SF-1): 0 = high repetition rate (human-like); 1 = low repetition (AI-like)

Combined score (3-signal ensemble):
```
combined = (0.50 × llm_score) + (0.30 × stylometric_score) + (0.20 × ngram_score)
```

If all three signals agree (all > 0.65 or all < 0.35): `combined += 0.05` (capped at 1.0).
If signals disagree (one on opposite side of 0.5 from the other two): label is forced
to `UNCERTAIN` and `signals_disagree: true` is added to the response regardless of
the weighted average.

### Calibration note

These weights and thresholds are initial values. The audit log records every raw signal
score, making it possible to recalibrate thresholds by inspecting historical
distributions. The analytics dashboard (SF-3) will surface signal disagreement rate as
an early-warning metric for threshold drift.

---

## 9. Transparency Label Variants

These are the exact strings the system will produce. Designed before any UI is built.

### High-confidence AI result (combined ≥ 0.85)
```
Label:   LIKELY_AI
Headline: This content appears to be AI-generated.
Detail:  Our analysis found strong indicators of AI authorship
         (confidence: {score}%). If this is incorrect, you can
         appeal this result using your submission ID.
Badge:   🤖 AI-Generated ({score}% confidence)
```

### Moderate-confidence AI result (combined 0.66 – 0.84)
```
Label:   LIKELY_AI
Headline: This content may be AI-generated.
Detail:  Our analysis found moderate indicators of AI authorship
         (confidence: {score}%). Individual signal scores are
         available for review.
Badge:   🤖 Likely AI-Generated ({score}% confidence)
```

### Uncertain result (combined 0.35 – 0.65)
```
Label:   UNCERTAIN
Headline: We couldn't determine the origin of this content.
Detail:  Our signals produced inconclusive results (confidence: {score}%).
         This may indicate mixed authorship, a distinctive human style,
         or content that falls outside our detection range.
Badge:   ❓ Origin Uncertain ({score}% confidence)
```

### Moderate-confidence human result (combined 0.20 – 0.34)
```
Label:   LIKELY_HUMAN
Headline: This content appears to be human-written.
Detail:  Our analysis found moderate indicators of human authorship
         (confidence: {score}%). You may request a Verified Human
         certificate if confidence reaches 80%+.
Badge:   ✅ Likely Human-Written ({score}% confidence)
```

### High-confidence human result (combined < 0.20)
```
Label:   LIKELY_HUMAN
Headline: This content appears to be human-written.
Detail:  Our analysis found strong indicators of human authorship
         (confidence: {score}%). You are eligible to request a
         Verified Human certificate for this submission.
Badge:   ✅ Human-Written ({score}% confidence)
```

**Implementation note:** The `score` shown in labels is `round((1 - combined) * 100)`
for human labels and `round(combined * 100)` for AI labels — so the displayed
percentage always represents "confidence in the stated verdict", not a raw AI-probability
score. This is less confusing for non-technical users.

---

## 10. Appeals Workflow (Detailed)

### Who can appeal
Any caller who holds a valid `submission_id`. In this implementation there is no
authentication layer — any client with the ID can appeal. A production system would
gate this on creator identity.

### What they provide
```json
POST /appeal/:id
{
  "reason": "string (required, max 1000 chars)",
  "creator_id": "string (optional — for audit trail)"
}
```

### What the system does on receipt
1. Looks up the submission by ID — returns 404 if not found.
2. Checks that `appealed` is not already `true` — returns 409 if already appealed
   (one appeal per submission; re-appeals go through a human reviewer).
3. Sets `appealed = 1`, `appeal_reason = reason`, `appealed_at = now()` in SQLite.
4. Does **not** re-run detection. The original scores are preserved unchanged.
5. Returns the updated record with a human-readable message.

### What a human reviewer sees (appeal queue)
`GET /audit-log?appealed=true` returns only appealed submissions, sorted by
`appealed_at` ascending (oldest appeal first = FIFO review queue). Each entry shows:

- Submission ID and timestamp
- Original label and confidence score
- Both raw signal scores (so reviewer can spot borderline cases)
- `signals_disagree` flag (if true, this is a known ambiguous case)
- Appeal reason in the creator's own words
- Creator ID if provided

The reviewer has the information needed to make a human judgement without re-running
the system.

### Status transitions
```
submitted → [detection runs] → labelled
labelled  → [creator appeals] → appealed (label unchanged, human review flagged)
```
No further automated state changes occur after `appealed`. Overriding a label is a
manual operation (UPDATE in SQLite) outside the API surface in this implementation.

---

## 11. Anticipated Edge Cases

### Edge case 1: Short text (< 80 words)
**Problem:** The stylometric heuristics need a reasonable sample size. A 30-word
sentence has a sentence-length variance of 0 (one sentence), a TTR of ~1.0 (all words
unique), and almost no punctuation to measure. All three stylometric sub-scores will
produce unreliable values.
**Mitigation:** If `word_count < 80`, the stylometric signal weight is reduced to 0.10
and the LLM classifier weight is increased to 0.70. The response includes
`"low_confidence_reason": "text too short for reliable stylometric analysis"`.

### Edge case 2: Minimalist or highly formal human prose
**Problem:** A writer with a disciplined, uniform style (legal briefs, technical
documentation, minimalist fiction) will have low sentence-length variance, high TTR,
and sparse punctuation — the same pattern the heuristics associate with AI text. The
LLM classifier may also flag it, having been trained on more colloquial human writing.
**Mitigation:** This is the primary false-positive scenario documented in Section 3.
The system surfaces both raw scores and the `signals_disagree` flag. The appeal flow
exists precisely for this case.

### Edge case 3: AI text deliberately injected with variance
**Problem:** A sophisticated user prompts an LLM with "write this with varying sentence
lengths, unusual punctuation, and occasional typos." The stylometric signal and n-gram
signal may both return low AI-probability scores. If the LLM classifier also fails to
detect the manipulation, the system will under-call.
**Mitigation:** No current mitigation — this is a known hard limit of the approach.
The planning document acknowledges it so the system is not over-claimed. The LLM
classifier is the last line of defence here; its holistic assessment is harder to
fool than individual heuristics.

### Edge case 4: Mixed authorship (human draft, AI completion)
**Problem:** A human writes three paragraphs and uses AI to complete the last two. The
text is neither fully human nor fully AI. Signals will likely disagree, producing a
`signals_disagree: true` result and forcing `UNCERTAIN`.
**Mitigation:** `UNCERTAIN` is the correct label for this case. The system does not
attempt paragraph-level attribution in this implementation. The audit log preserves
the raw scores for future analysis.

---

## 12. Architecture Section (Milestone 2 Reference)

This section consolidates the submission and appeal flow diagrams from Section 5 for
use as AI prompting context in Milestones 3–5.

### Submission flow narrative
A `POST /submit` request is validated by the Flask route handler, which creates a
pending submission record in SQLite before any analysis runs. The raw text is then
passed to three independent detectors (LLM classifier via Groq, stylometric heuristic
analyser in pure Python, n-gram repetition analyser in pure Python). Their scores are
combined by the confidence scorer using a weighted average with an agreement bonus.
The scorer maps the combined score to a label variant and writes the full record —
both raw scores, combined score, label, and timestamp — to the SQLite audit log.
The response returns all of this to the client.

### Appeal flow narrative
A `POST /appeal/:id` request is validated (submission must exist, must not already be
appealed). The SQLite record is updated with the appeal reason and timestamp. No
re-scoring occurs. The audit log now shows the submission as flagged for human review.
`GET /audit-log?appealed=true` surfaces the review queue.

### Diagrams
(See Section 5 — diagrams are reproduced here for AI prompting context)

**Submission flow (3-signal version):**
```
Client
  │  POST /submit  { text, creator_id, image_url? }
  ▼
┌──────────────────────────────────────┐
│  Flask Route Handler                  │
│  • validate input                     │
│  • INSERT pending submission (SQLite) │
└──────────┬───────────────────────────┘
           │ raw text
   ┌───────┼───────────┐
   ▼       ▼           ▼
┌──────┐ ┌──────────┐ ┌──────────────┐
│ Groq │ │Stylometry│ │ N-gram       │
│  LLM │ │Heuristics│ │ Repetition   │
│  (1) │ │  (2)     │ │ Rate (3)     │
└──┬───┘ └────┬─────┘ └──────┬───────┘
   │          │              │
   │ 0–1      │ 0–1          │ 0–1
   └──────────┴──────┬───────┘
                     ▼
          ┌────────────────────┐
          │  Confidence Scorer  │
          │  weighted avg       │
          │  agreement bonus    │
          │  → combined score   │
          │  → label variant    │
          │  → signals_disagree │
          └──────────┬─────────┘
                     │
                     ▼
          ┌────────────────────┐
          │  SQLite Audit Log   │
          │  UPDATE submission  │
          └──────────┬─────────┘
                     │
                     ▼
               Response to Client
               { submission_id, label, headline, badge,
                 confidence, signals{}, submitted_at,
                 signals_disagree?, low_confidence_reason? }
```

**Appeal flow:**
```
Client
  │  POST /appeal/:id  { reason, creator_id? }
  ▼
┌──────────────────────────────────────┐
│  Flask Route Handler                  │
│  • SELECT submission (404 if missing) │
│  • check not already appealed (409)   │
└──────────┬───────────────────────────┘
           ▼
  ┌────────────────────┐
  │  SQLite Audit Log   │
  │  SET appealed=1     │
  │  SET appeal_reason  │
  │  SET appealed_at    │
  └──────────┬─────────┘
             ▼
       Response to Client
       { submission_id, appealed: true,
         appeal_reason, message }
```

---

## 13. AI Tool Plan

### Milestone 3 — Submission endpoint + Signal 1 (LLM Classifier)

**Spec sections to provide to AI tool:**
- Section 12 (Architecture, including submission flow diagram)
- Section 2 (Signal 1: LLM classifier — what it measures, output format)
- Section 8 (Uncertainty representation — score mapping)
- Section 4 (API surface — `POST /submit` request/response shape)

**What to ask the AI tool to generate:**
1. Flask app skeleton with SQLite initialisation (`init_db()`) and the `submissions`
   table schema (columns: id, creator_id, text, llm_score, stylometric_score,
   ngram_score, combined_score, label, signals_disagree, appealed, appeal_reason,
   submitted_at, appealed_at).
2. `groq_classifier(text) -> float` function: sends text to Groq with a structured
   prompt, parses the response to extract a 0–1 probability, handles API errors
   gracefully (returns 0.5 on failure so the system degrades to heuristics only).
3. `POST /submit` route stub that calls `groq_classifier`, stores the result, and
   returns a placeholder response (stylometric and n-gram scores hardcoded to 0.5
   until Milestone 4).

**Verification steps before wiring into the endpoint:**
- Call `groq_classifier("The mitochondria is the powerhouse of the cell.")` — expect
  a low score (human-like).
- Call `groq_classifier` with a pasted ChatGPT output — expect a high score.
- Check that API errors return 0.5, not an exception.
- Confirm SQLite table is created on first run and rows are inserted correctly.

---

### Milestone 4 — Signals 2 & 3 + Confidence Scoring

**Spec sections to provide to AI tool:**
- Section 12 (Architecture diagram — 3-signal version)
- Section 2 (Signal 2: stylometric heuristics — three sub-measures, normalisation)
- Section 7/SF-1 (Signal 3: n-gram repetition rate — what it measures, output)
- Section 8 (Uncertainty representation — weights, agreement bonus, disagree logic,
  thresholds)

**What to ask the AI tool to generate:**
1. `stylometric_score(text) -> float`: computes sentence-length variance, TTR, and
   punctuation density; normalises each to [0,1]; returns weighted average of the
   three sub-scores. Must handle texts < 80 words by returning `(score, low_sample=True)`.
2. `ngram_score(text) -> float`: tokenises text into bigrams; computes ratio of
   repeated bigrams to total bigrams; inverts so that high repetition → low score
   (human-like).
3. `confidence_scorer(llm, stylometric, ngram) -> (combined, label, signals_disagree)`:
   implements Section 8 formula exactly — weighted average, agreement bonus, disagree
   override, threshold mapping to label variant strings from Section 9.

**Verification steps:**
- Run all three scorers against a known AI paragraph (e.g., a GPT explanation of
  photosynthesis) and a known human paragraph (e.g., a personal blog post excerpt).
  Scores should differ meaningfully (> 0.2 gap) across all three signals.
- Verify the agreement bonus fires when all three signals are > 0.65.
- Verify `signals_disagree` is set when one signal contradicts the other two.
- Verify short-text flag triggers at < 80 words.

---

### Milestone 5 — Production Layer (Labels + Appeal endpoint)

**Spec sections to provide to AI tool:**
- Section 9 (Transparency label variants — exact headline, detail, badge text)
- Section 10 (Appeals workflow — status transitions, validation rules, queue format)
- Section 12 (Architecture — appeal flow diagram)
- Section 4 (API surface — `POST /appeal/:id`, `GET /audit-log`, `GET /submission/:id`)

**What to ask the AI tool to generate:**
1. `build_label_response(combined_score, signals) -> dict`: maps score ranges to the
   exact label variant strings from Section 9, including the inverted confidence
   percentage display logic.
2. `POST /appeal/:id` route: implements the four-step workflow from Section 10
   (lookup → 404, already-appealed → 409, UPDATE record, return response).
3. `GET /audit-log` route with optional `?appealed=true` filter for the review queue.
4. `GET /submission/:id` route.

**Verification steps:**
- Submit text that scores < 0.20 → verify `LIKELY_HUMAN` high-confidence label text.
- Submit text that scores 0.50 → verify `UNCERTAIN` label text.
- Submit text that scores > 0.85 → verify `LIKELY_AI` high-confidence label text.
- Appeal a submission → verify 200 response and `appealed=true` in audit log.
- Appeal the same submission again → verify 409.
- Appeal a nonexistent ID → verify 404.
- Call `GET /audit-log?appealed=true` → verify only appealed submissions appear.