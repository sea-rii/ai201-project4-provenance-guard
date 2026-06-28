# ai201-project4-provenance-guard

# Provenance Guard

A Flask API that detects whether text is AI-generated or human-written, using a
three-signal ensemble and a full production layer: transparency labels, an appeals
workflow, rate limiting, a structured audit log, and an analytics dashboard.

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/ai201-project4-provenance-guard.git
cd ai201-project4-provenance-guard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project root:
```
GROQ_API_KEY=your_key_here
```

Run the server:
```bash
python app.py
# Serving on http://127.0.0.1:5001
```

> **macOS note:** Port 5000 is used by AirPlay Receiver. The server runs on 5001.

---

## Architecture

A submission travels through seven components before a response is returned.

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
   └──────────┴──────┬───────┘
                     ▼
          ┌────────────────────┐
          │  Confidence Scorer  │
          │  weighted average   │
          │  agreement bonus    │
          │  disagree override  │
          │  → combined score   │
          │  → label variant    │
          └──────────┬─────────┘
                     ▼
          ┌────────────────────┐
          │  SQLite Audit Log   │
          │  UPDATE submission  │
          └──────────┬─────────┘
                     ▼
               Response to Client
```

**Submission flow:** The submission is written to SQLite _before_ any detection runs,
so every piece of text has a permanent ID even if Groq times out. Three signals run
independently, their scores are combined by the confidence scorer, and the full result
— raw signal scores, combined score, label, and timestamp — is written back to the
audit log before the response is returned.

**Appeal flow:** `POST /appeal` looks up the submission, rejects if already appealed
(409), sets `appealed=1` and `status='appealed'` in SQLite, and returns a
confirmation. No re-scoring occurs — the appeal is a human-review flag.

---

## Detection Signals

### Signal 1: LLM Classifier (Groq) — weight 0.50

**What it measures:** Holistic semantic and stylistic plausibility of the text as
human-written. The model assesses whether the writing exhibits uniform hedging
language, over-explained transitions, unnaturally balanced paragraph lengths, or the
absence of personal idiosyncrasies.

**Why these differ:** Human writing carries cognitive fingerprints — topic drift,
uneven elaboration, idiosyncratic word choices, rhetorical habits. AI models generate
toward high-probability token continuations, which produces a different distributional
signature even when surface content looks identical.

**Why this signal:** It's the most information-dense signal available without
additional infrastructure. A pre-trained model has already internalized what
human writing looks like across a vast range of styles and domains.

**Blind spots:** Heavily edited AI text fools it. Very short texts (< ~30 words) give
it too little to work with. Domain-specific human writing (legal briefs, technical
documentation) can look AI-like to the classifier.

**Output:** Float in [0, 1]. 0 = confident it's human, 1 = confident it's AI.
Falls back to 0.5 on any API error so the system degrades gracefully.

---

### Signal 2: Stylometric Heuristics (pure Python) — weight 0.30

**What it measures:** Three statistically computable properties:

| Sub-measure | AI signature | Human signature |
|---|---|---|
| Sentence-length variance | Low std dev (uniform) | High std dev (variable) |
| Type-token ratio (TTR) | High (avoids repetition) | Lower (natural recurrence) |
| Punctuation density | Low expressive punctuation | Dashes, ellipses, exclamation marks |

Each sub-measure is normalised to [0, 1] and combined with weights (0.45 / 0.35 / 0.20).

**Why this signal:** It's independent of any LLM — it measures structural properties
that are computable without an API call, making it auditable and deterministic. When
the LLM classifier returns a borderline score, the stylometric signal either
corroborates or challenges it.

**Blind spots:** Academic or formal human writing is deliberately uniform — it will
score as AI-like (the monetary policy example in testing). A skilled adversary can
prompt a model to introduce artificial variance. TTR is length-dependent: texts under
~80 words have unreliable statistics, so the system flags `low_confidence_reason` and
down-weights this signal when word count is low.

**Output:** Float in [0, 1]. Also returns `low_sample: bool`.

---

### Signal 3: N-gram Repetition Rate (pure Python) — weight 0.20

**What it measures:** Ratio of repeated bigrams to total bigrams. AI models are
penalised for repetition during RLHF training, producing a characteristic
low-repetition-rate signature. Human writing — especially informal or conversational —
naturally repeats phrases, sentence openers, and connective tissue.

**Why this signal:** It captures a property of how models are trained, not just what
they produce. The RLHF repetition penalty is a training artifact that leaves a
measurable signature in output distributions.

**Blind spots:** Polished human prose (journalism, academic writing) also avoids
repetition deliberately. Requires 100+ tokens for reliable statistics; returns 0.5
(abstain) on shorter texts.

**Output:** Float in [0, 1]. Returns 0.5 (abstain) for texts under 100 tokens.

---

## Confidence Scoring

### Formula

```
combined = 0.50 × llm + 0.30 × stylometric + 0.20 × ngram
```

When the ngram signal abstains (returns exactly 0.5), it is treated as non-voting
and does not trigger the disagreement check. Short texts use redistributed weights:
LLM 0.70 / stylometric 0.10 / ngram 0.20.

**Agreement bonus:** If all active signals agree (all > 0.65 or all < 0.35),
`combined += 0.05` (capped at 1.0).

**Disagreement override:** If one signal contradicts the other two (one on the
opposite side of 0.5), `signals_disagree: true` is set and the label is forced to
`UNCERTAIN` regardless of the weighted average. This prevents a high combined score
from masking genuine signal conflict.

### Thresholds

| Combined score | Label |
|---|---|
| ≥ 0.66 | `LIKELY_AI` |
| 0.35 – 0.65 | `UNCERTAIN` |
| < 0.35 | `LIKELY_HUMAN` |

The displayed `confidence` percentage always represents confidence _in the stated
verdict_, not a raw AI-probability. For `LIKELY_AI`, it's `combined × 100`. For
`LIKELY_HUMAN`, it's `(1 - combined) × 100`. For `UNCERTAIN`, it reflects distance
from the centre.

### Example submissions with different scores

**High-confidence AI (combined: 0.86)**
```json
{
  "text": "Artificial intelligence represents a transformative paradigm shift...",
  "signals": { "llm_score": 0.9, "stylometric_score": 0.84, "ngram_score": 0.5 },
  "combined_score": 0.8643,
  "label": "LIKELY_AI",
  "confidence": 86
}
```

**Lower-confidence human (combined: 0.32)**
```json
{
  "text": "ok so i finally tried that new ramen place downtown and honestly?...",
  "signals": { "llm_score": 0.2, "stylometric_score": 0.76, "ngram_score": 0.5 },
  "combined_score": 0.3158,
  "label": "LIKELY_HUMAN",
  "confidence": 68
}
```

The 18-point gap in combined score (0.86 vs 0.32) produces meaningfully different
labels. The 68% confidence on the human example is honest — the stylometric signal
is legitimately uncertain about casual prose, but LLM at 0.2 dominates the
weighted average.

### Why this scoring approach

The weighted average was chosen over a voting scheme because it preserves gradient
information — a score of 0.82 is treated differently from 0.68, rather than both
being a single "AI" vote. The disagree override adds a safety valve: when signals
genuinely conflict, the system admits uncertainty rather than letting a high combined
score mask the conflict.

**What I'd change for production:** These weights were set by reasoning about signal
reliability, not empirical calibration. A deployed system would tune them against a
labelled dataset and recalibrate thresholds monthly using the audit log's historical
distribution. The analytics dashboard's signal disagreement rate metric was built
specifically to surface when recalibration is needed.

---

## Transparency Label Variants

The system produces five label variants. The exact text each one displays:

### High-confidence AI (combined ≥ 0.85)
```
label:    LIKELY_AI
headline: This content appears to be AI-generated.
detail:   Our analysis found strong indicators of AI authorship (confidence: {N}%).
          If this is incorrect, you can appeal this result using your submission ID.
badge:    AI-Generated ({N}% confidence)
```

### Moderate-confidence AI (combined 0.66–0.84)
```
label:    LIKELY_AI
headline: This content may be AI-generated.
detail:   Our analysis found moderate indicators of AI authorship (confidence: {N}%).
          Individual signal scores are available for review.
badge:    Likely AI-Generated ({N}% confidence)
```

### Uncertain (combined 0.35–0.65)
```
label:    UNCERTAIN
headline: We couldn't determine the origin of this content.
detail:   Our signals produced inconclusive results (confidence: {N}%). This may
          indicate mixed authorship, a distinctive human style, or content that
          falls outside our detection range.
badge:    Origin Uncertain ({N}% confidence)
```

### Moderate-confidence human (combined 0.20–0.34)
```
label:    LIKELY_HUMAN
headline: This content appears to be human-written.
detail:   Our analysis found moderate indicators of human authorship (confidence: {N}%).
          You may request a Verified Human certificate if confidence reaches 80%+.
badge:    Likely Human-Written ({N}% confidence)
```

### High-confidence human (combined < 0.20)
```
label:    LIKELY_HUMAN
headline: This content appears to be human-written.
detail:   Our analysis found strong indicators of human authorship (confidence: {N}%).
          You are eligible to request a Verified Human certificate for this submission.
badge:    Human-Written ({N}% confidence)
```

---

## API Reference

### `POST /submit`
Submit text for provenance analysis.

```bash
curl -s -X POST http://127.0.0.1:5001/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "your text here", "creator_id": "optional-id"}'
```

Optional: include `"image_url": "https://..."` to activate the image consistency
signal (SF-4).

**Response:**
```json
{
  "content_id": 14,
  "label": "LIKELY_AI",
  "headline": "This content may be AI-generated.",
  "detail": "Our analysis found moderate indicators...",
  "badge": "Likely AI-Generated (80% confidence)",
  "confidence": 80,
  "combined_score": 0.801,
  "signals": {
    "llm_score": 0.8,
    "stylometric_score": 0.91,
    "ngram_score": 0.5
  },
  "signals_disagree": false,
  "submitted_at": "2026-06-28T22:51:41Z",
  "status": "classified"
}
```

---

### `POST /appeal`
Flag a submission as misclassified and request human review.

```bash
curl -s -X POST http://127.0.0.1:5001/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": 1, "creator_reasoning": "I wrote this myself..."}'
```

Returns 409 if already appealed. Does not re-score.

---

### `GET /submission/<id>`
Retrieve a single submission with full label text.

---

### `GET /log` / `GET /audit-log`
Return structured audit log entries. Add `?appealed=true` for the review queue.

---

### `GET /analytics`
Aggregated detection metrics (SF-3).

---

### `GET /dashboard`
HTML analytics dashboard (SF-3).

---

### `POST /certify/<id>`
Issue a Verified Human certificate (SF-2). Requires `LIKELY_HUMAN` label with
≥ 80% confidence.

---

### `GET /certificate/<cert_id>`
Retrieve a certificate by UUID.

---

## Rate Limiting

Applied to `POST /submit`: **10 requests per minute, 100 per day**.

**Reasoning:** A writer submitting their own work for review would rarely send more
than a few requests per minute — even batch-checking a novel chapter-by-chapter would
stay under 10/min. The 10/min cap blocks automated scripts flooding the system while
remaining invisible to legitimate use. The 100/day cap prevents sustained abuse from
a single IP while being generous for any real workflow.

**Evidence (rate limit test — 12 rapid requests):**
```
201  ← request 1
201  ← request 2
201  ← request 3
201  ← request 4
201  ← request 5
201  ← request 6
201  ← request 7
201  ← request 8
429  ← rate limit exceeded
429
429
429
```

---

## Appeals Workflow

When a submission is misclassified, the creator calls `POST /appeal` with their
`content_id` and a plain-text explanation. The system:

1. Looks up the submission — returns 404 if not found
2. Rejects with 409 if already appealed (one appeal per submission)
3. Sets `appealed=1`, `status='appealed'`, records reason and timestamp
4. Returns confirmation — original label is preserved until a human reviewer acts

**Example appeal response:**
```json
{
  "content_id": 1,
  "status": "under_review",
  "appeal_reason": "I wrote this myself from personal experience...",
  "message": "Appeal recorded. A human reviewer will assess this submission.",
  "appealed_at": "2026-06-28T22:51:56Z"
}
```

**Review queue:** `GET /log?appealed=true` returns only appealed submissions, sorted
by submission date, showing original scores alongside the appeal reason so a reviewer
has everything needed to make a judgment.

---

## Audit Log

Every submission writes a structured JSON entry with: timestamp, content ID, creator
ID, label, combined score, all three individual signal scores, signals_disagree flag,
appeal status, and appeal reason.

**Sample entries (3 of 25 total):**
```json
{
  "id": 14,
  "creator_id": "label-test-ai",
  "submitted_at": "2026-06-28T22:51:41Z",
  "status": "classified",
  "label": "LIKELY_AI",
  "combined_score": 0.801,
  "llm_score": 0.8,
  "stylometric_score": 0.91,
  "ngram_score": 0.5,
  "signals_disagree": false,
  "appealed": false,
  "appeal_reason": null
}
```
```json
{
  "id": 15,
  "creator_id": "label-test-human",
  "submitted_at": "2026-06-28T22:51:47Z",
  "status": "classified",
  "label": "LIKELY_HUMAN",
  "combined_score": 0.3228,
  "llm_score": 0.2,
  "stylometric_score": 0.8279,
  "ngram_score": 0.5,
  "signals_disagree": false,
  "appealed": false,
  "appeal_reason": null
}
```
```json
{
  "id": 1,
  "creator_id": "test-user-1",
  "submitted_at": "2026-06-28T22:29:13Z",
  "status": "appealed",
  "label": "UNCERTAIN",
  "combined_score": 0.35,
  "llm_score": 0.2,
  "stylometric_score": 0.5,
  "ngram_score": 0.5,
  "signals_disagree": false,
  "appealed": true,
  "appeal_reason": "I wrote this myself from personal experience. I am a non-native English speaker..."
}
```

---

## Stretch Features

### SF-1: Ensemble Detection (3+ signals) ✅

Three independent signals with documented weights (0.50 / 0.30 / 0.20). Agreement
bonus (+0.05) when all signals agree strongly. Disagree override forces `UNCERTAIN`
when one signal contradicts the other two. `signals_disagree` flag always returned
in the API response so callers can see when the ensemble is uncertain internally.

### SF-2: Provenance Certificate ✅

`POST /certify/<id>` issues a Verified Human certificate for `LIKELY_HUMAN`
submissions with ≥ 80% confidence. Triggers a secondary Groq call to generate a
stylistic fingerprint summary. Certificate includes a badge string suitable for
embedding in content metadata:

```
Verified Human — 85% confidence, issued 2026-06-28
```

`GET /certificate/<uuid>` retrieves the full certificate including fingerprint.

### SF-3: Analytics Dashboard ✅

`GET /dashboard` — live HTML dashboard with three panels:
- **Detection distribution** (doughnut chart): % LIKELY_HUMAN / UNCERTAIN / LIKELY_AI
- **Daily submission volume** (bar chart): last 14 days
- **Appeals vs Disagreements** (bar chart): operational health at a glance

`GET /analytics` — the underlying JSON. Auto-refreshes every 30 seconds.

The **signal disagreement rate** (28% in testing) is the custom metric — it's an
early warning for threshold drift. When one signal persistently contradicts the other
two, the weights or thresholds need recalibration, not more data.

### SF-4: Multi-Modal Support ✅

Include `"image_url": "https://..."` in any `/submit` request to activate a fourth
signal. The system asks Groq Vision to describe the image independently, then scores
how semantically consistent the submitted description is with the AI-generated one.

- High consistency (score near 1.0) → submitted description may be AI-generated
- Low consistency (score near 0.0) → submitted description reflects human observation

The image signal uses weight 0.15 (other weights scaled down). When it fires, the
response includes both `image_consistency_score` and `ai_image_description`.

**Example (golden retriever + human description):**
```json
{
  "signals": {
    "llm_score": 0.8,
    "stylometric_score": 0.775,
    "ngram_score": 0.5,
    "image_consistency_score": 0.2
  },
  "signals_disagree": true,
  "label": "UNCERTAIN"
}
```
The human description mentioned "tongue out" — the image showed the dog holding a
tulip. The 0.2 consistency score captured that genuine observational divergence,
correctly triggering signal disagreement.

---

## Known Limitations

### 1. Formal human prose scores as AI-like

The stylometric signal measures uniformity — low sentence-length variance, high
type-token ratio, sparse expressive punctuation. These properties are also
characteristic of careful, formal human writing: legal briefs, academic papers,
technical documentation. A human writing in this register will score high on the
stylometric signal regardless of authorship.

This is a property of the signal design, not a calibration error. The stylometric
signal cannot distinguish between "uniform because it's a model" and "uniform because
the author is disciplined." The LLM classifier helps here but also tends to flag
formal prose. In testing, a monetary policy paragraph scored `LIKELY_AI` at 0.90
combined — a false positive for a type of content the system will frequently encounter.

**Mitigation:** The appeal workflow exists for this case. The system always returns
both raw signal scores, so a human reviewer can see that the high combined score came
from stylometric uniformity rather than a confident LLM classification.

### 2. Short texts are structurally unreliable

Texts under 80 words produce unreliable stylometric statistics. A single sentence
has sentence-length variance of 0 and TTR near 1.0 regardless of authorship. The
system flags this with `low_confidence_reason` and shifts weights toward the LLM
signal, but the LLM classifier also struggles with very short texts. A 20-word
sentence gives the system very little to work with — both signals are essentially
guessing at that length.

**Mitigation:** The system is honest about this — it flags the low-confidence reason
in every response where word count < 80. A production system would enforce a minimum
text length before allowing submission.

---

## Spec Reflection

### Where the spec helped

The requirement to write out all three label variants in plain English _before_
building the UI forced a design decision I would have deferred: what does a score of
0.62 actually say to a user? Writing "We couldn't determine the origin" before coding
made it clear the label needed to be non-accusatory and action-oriented (directing
users to appeal), not just a probability. The exact wording in planning.md §9
translated directly into the `build_label()` function with no ambiguity.

### Where the implementation diverged

The spec (planning.md §8) specified that `signals_disagree` should force the label to
`UNCERTAIN` unconditionally. In implementation, this created a problem: the n-gram
signal returns exactly 0.5 for any text under 100 tokens (abstain), and 0.5 sits on
the "human" side of the 0.5 boundary. When the LLM and stylometric signals both
scored > 0.65 (AI-like), the n-gram abstain value triggered the minority-disagree
logic and forced `UNCERTAIN` on clearly AI-generated text.

The fix was to treat `ngram == 0.5` as abstain rather than as a signal with an
opinion — it's excluded from the disagree check entirely. This is a better design than
the spec: a signal that can't form an opinion shouldn't be able to override two signals
that can. The spec was updated in planning.md to reflect this.

---

## AI Usage

### Instance 1: Flask app skeleton and db.py

**Directed:** "Generate a Flask app skeleton with SQLite initialisation and a
`submissions` table matching this schema [pasted schema from planning.md §13]. Include
a `POST /submit` route stub and a `groq_classifier` function that returns a 0–1
probability score from the Groq API."

**Produced:** A working Flask skeleton, a `groq_classifier` function with a
structured JSON prompt, and a db module with `init_db()` and `insert_submission()`.

**Revised:** The generated `insert_submission()` had a parameter count mismatch — 3
column placeholders but only 2 values in the tuple (`submitted_at` was in the column
list but missing from the values). This caused a SQLite binding error on the first
real request. Fixed by adding `now` to the tuple. Also revised the Groq prompt to
explicitly strip markdown fences from the response, which the original didn't handle.

### Instance 2: Stylometric signal implementation

**Directed:** "Implement a `stylometric_score(text)` function computing
sentence-length variance, type-token ratio, and punctuation density. Normalise each
to [0, 1] with these calibration anchors [pasted from planning.md]. Return
`(score, low_sample)` where `low_sample=True` when word count < 80."

**Produced:** A working three-sub-measure implementation with the correct function
signature and calibration structure.

**Revised:** The initial TTR calibration anchors (`lo=0.40, hi=0.85`) caused the
signal to score short informal human texts as highly AI-like — a 55-word ramen review
scored 0.83 (AI-like) because short texts naturally have near-100% unique word
ratios. Recalibrated to `lo=0.55, hi=0.92` after testing against the four milestone
inputs. Also raised the n-gram short-text threshold from 20 tokens to 100 tokens
after observing that 55-token texts with zero bigram repetition were scoring 1.0
(AI-like) purely due to text length, not authorship.