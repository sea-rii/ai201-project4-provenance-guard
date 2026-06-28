"""
app.py - Provenance Guard: Flask application entry point.

All milestones + all four stretch features complete:
  SF-1: 3-signal ensemble with agreement/disagree logic
  SF-2: POST /certify/<id>, GET /certificate/<id>
  SF-3: GET /analytics (JSON), GET /dashboard (HTML)
  SF-4: optional image_url on /submit — 4th signal when present
"""

import traceback
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

import db
from signals.groq_classifier import groq_classifier
from signals.stylometric import stylometric_score
from signals.ngram import ngram_score
from signals.image_consistency import image_consistency_score

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder='dashboard')
app.config["PROPAGATE_EXCEPTIONS"] = False

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

with app.app_context():
    db.init_db()


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

# Base 3-signal weights
W_LLM, W_STYLO, W_NGRAM = 0.50, 0.30, 0.20
# Short-text weights (LLM boosted when stylometric is unreliable)
W_LLM_S, W_STYLO_S, W_NGRAM_S = 0.70, 0.10, 0.20
# 4-signal weights when image_url present (SF-4); scale base weights down
W_LLM_4, W_STYLO_4, W_NGRAM_4, W_IMG_4 = 0.43, 0.25, 0.17, 0.15

AGREEMENT_BONUS    = 0.05
AGREE_HIGH, AGREE_LOW = 0.65, 0.35
LIKELY_AI_THRESH   = 0.66
LIKELY_HUMAN_THRESH = 0.35


def compute_confidence(llm, stylo, ngram, img=None, low_sample=False):
    """
    Weighted ensemble + agreement bonus + disagree override.
    img is the image_consistency_score (float or None).
    Returns (combined, label, signals_disagree).
    """
    if img is not None and img != 0.5:
        # 4-signal mode (SF-4)
        combined = W_LLM_4*llm + W_STYLO_4*stylo + W_NGRAM_4*ngram + W_IMG_4*img
        active = [s for s in [llm, stylo, ngram, img] if s != 0.5]
    elif low_sample:
        combined = W_LLM_S*llm + W_STYLO_S*stylo + W_NGRAM_S*ngram
        active = [llm, stylo] + ([ngram] if ngram != 0.5 else [])
    else:
        combined = W_LLM*llm + W_STYLO*stylo + W_NGRAM*ngram
        active = [llm, stylo] + ([ngram] if ngram != 0.5 else [])

    all_ai    = all(s > AGREE_HIGH for s in active)
    all_human = all(s < AGREE_LOW  for s in active)
    signals_disagree = False

    if all_ai or all_human:
        combined = min(1.0, combined + AGREEMENT_BONUS)
    elif len(active) >= 3:
        ai_side = [s > 0.5 for s in active]
        minority = min(sum(ai_side), len(active) - sum(ai_side))
        if minority == 1:
            signals_disagree = True

    if combined >= LIKELY_AI_THRESH:
        label = "LIKELY_AI"
    elif combined >= LIKELY_HUMAN_THRESH:
        label = "UNCERTAIN"
    else:
        label = "LIKELY_HUMAN"

    if signals_disagree:
        label = "UNCERTAIN"

    return round(combined, 4), label, signals_disagree


# ---------------------------------------------------------------------------
# Transparency label builder  (planning.md §9)
# ---------------------------------------------------------------------------

def build_label(label, combined):
    if label == "LIKELY_AI":
        pct = round(combined * 100)
        if combined >= 0.85:
            return {
                "label": "LIKELY_AI",
                "headline": "This content appears to be AI-generated.",
                "detail": (f"Our analysis found strong indicators of AI authorship "
                           f"(confidence: {pct}%). If this is incorrect, you can "
                           f"appeal this result using your submission ID."),
                "badge": f"AI-Generated ({pct}% confidence)",
                "confidence_pct": pct,
            }
        return {
            "label": "LIKELY_AI",
            "headline": "This content may be AI-generated.",
            "detail": (f"Our analysis found moderate indicators of AI authorship "
                       f"(confidence: {pct}%). Individual signal scores are available for review."),
            "badge": f"Likely AI-Generated ({pct}% confidence)",
            "confidence_pct": pct,
        }

    if label == "UNCERTAIN":
        pct = round((1 - abs(combined - 0.5) * 2) * 100)
        return {
            "label": "UNCERTAIN",
            "headline": "We couldn't determine the origin of this content.",
            "detail": (f"Our signals produced inconclusive results (confidence: {pct}%). "
                       f"This may indicate mixed authorship, a distinctive human style, "
                       f"or content outside our detection range."),
            "badge": f"Origin Uncertain ({pct}% confidence)",
            "confidence_pct": pct,
        }

    # LIKELY_HUMAN
    pct = round((1 - combined) * 100)
    if combined < 0.20:
        return {
            "label": "LIKELY_HUMAN",
            "headline": "This content appears to be human-written.",
            "detail": (f"Our analysis found strong indicators of human authorship "
                       f"(confidence: {pct}%). You are eligible to request a "
                       f"Verified Human certificate for this submission."),
            "badge": f"Human-Written ({pct}% confidence)",
            "confidence_pct": pct,
        }
    return {
        "label": "LIKELY_HUMAN",
        "headline": "This content appears to be human-written.",
        "detail": (f"Our analysis found moderate indicators of human authorship "
                   f"(confidence: {pct}%). You may request a Verified Human "
                   f"certificate if confidence reaches 80%+."),
        "badge": f"Likely Human-Written ({pct}% confidence)",
        "confidence_pct": pct,
    }


# ---------------------------------------------------------------------------
# Routes — core
# ---------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 100 per day")
def submit():
    try:
        body = request.get_json(silent=True)
        if not body or not body.get("text", "").strip():
            return jsonify({"error": "Request body must include a non-empty 'text' field."}), 400

        text       = body["text"].strip()
        creator_id = body.get("creator_id", "anonymous")
        image_url  = body.get("image_url")          # SF-4

        submission_id = db.insert_submission(creator_id, text)

        # Signals 1-3
        llm               = groq_classifier(text)
        stylo, low_sample = stylometric_score(text)
        ngram             = ngram_score(text)

        # Signal 4: image consistency (SF-4) — only when image_url provided
        img_score      = None
        ai_description = None
        if image_url:
            img_score, ai_description = image_consistency_score(text, image_url)

        lc_reason = "text too short for reliable stylometric analysis" if low_sample else None
        combined, label, signals_disagree = compute_confidence(
            llm, stylo, ngram, img=img_score, low_sample=low_sample
        )

        db.update_submission_scores(
            submission_id, llm, stylo, ngram,
            combined, label, signals_disagree, lc_reason,
        )

        transparency = build_label(label, combined)

        signals_out = {
            "llm_score":         round(llm,   4),
            "stylometric_score": round(stylo, 4),
            "ngram_score":       round(ngram, 4),
        }
        if img_score is not None:
            signals_out["image_consistency_score"] = round(img_score, 4)

        resp = {
            "content_id":      submission_id,
            "attribution":     label.lower(),
            "label":           transparency["label"],
            "headline":        transparency["headline"],
            "detail":          transparency["detail"],
            "badge":           transparency["badge"],
            "confidence":      transparency["confidence_pct"],
            "combined_score":  combined,
            "signals":         signals_out,
            "signals_disagree": signals_disagree,
            "submitted_at":    datetime.now(timezone.utc).isoformat(),
            "status":          "classified",
        }

        if lc_reason:
            resp["low_confidence_reason"] = lc_reason
        if image_url:
            resp["image_url"] = image_url
        if ai_description:
            resp["ai_image_description"] = ai_description
        if label == "LIKELY_HUMAN" and (1 - combined) >= 0.80:
            resp["certificate_eligible"] = True

        return jsonify(resp), 201

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Internal server error.", "detail": str(e)}), 500


@app.route("/appeal", methods=["POST"])
def appeal():
    try:
        body = request.get_json(silent=True)
        if not body:
            return jsonify({"error": "JSON body required."}), 400

        content_id = body.get("content_id")
        reasoning  = body.get("creator_reasoning", "").strip()

        if content_id is None:
            return jsonify({"error": "'content_id' is required."}), 400
        if not reasoning:
            return jsonify({"error": "'creator_reasoning' is required."}), 400

        result = db.set_appeal(int(content_id), reasoning)

        if result == "not_found":
            return jsonify({"error": f"Submission {content_id} not found."}), 404
        if result == "already_appealed":
            return jsonify({"error": "Already appealed.", "content_id": content_id}), 409

        return jsonify({
            "content_id":    content_id,
            "status":        "under_review",
            "appeal_reason": reasoning,
            "message":       ("Appeal recorded. A human reviewer will assess this submission. "
                              "The original classification is preserved until a reviewer acts."),
            "appealed_at":   datetime.now(timezone.utc).isoformat(),
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/submission/<int:sid>", methods=["GET"])
def get_submission(sid):
    try:
        row = db.get_submission(sid)
        if not row:
            return jsonify({"error": f"Submission {sid} not found."}), 404
        if row["label"] and row["combined_score"] is not None:
            row.update(build_label(row["label"], row["combined_score"]))
        return jsonify(row), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/log", methods=["GET"])
@app.route("/audit-log", methods=["GET"])
def get_log():
    try:
        appealed_only = request.args.get("appealed", "").lower() == "true"
        entries = db.get_log(appealed_only=appealed_only)
        return jsonify({"count": len(entries), "entries": entries})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# SF-3: Analytics
# ---------------------------------------------------------------------------

@app.route("/analytics", methods=["GET"])
def analytics():
    """GET /analytics — aggregated detection metrics (SF-3)."""
    try:
        return jsonify(db.get_analytics())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """GET /dashboard — HTML analytics dashboard (SF-3)."""
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# SF-2: Provenance Certificate
# ---------------------------------------------------------------------------

@app.route("/certify/<int:sid>", methods=["POST"])
def certify(sid):
    try:
        row = db.get_submission(sid)
        if not row:
            return jsonify({"error": "Submission not found."}), 404
        if row["label"] != "LIKELY_HUMAN":
            return jsonify({"error": "Certificate only for LIKELY_HUMAN submissions.",
                            "label": row["label"]}), 422

        confidence_human = round((1 - row["combined_score"]) * 100)
        if confidence_human < 80:
            return jsonify({"error": f"Confidence too low ({confidence_human}%). Min 80%.",
                            "confidence": confidence_human}), 422

        fingerprint = _get_fingerprint(row.get("text", ""))
        cert_id     = str(uuid.uuid4())
        issued_at   = datetime.now(timezone.utc).isoformat()
        badge       = f"Verified Human — {confidence_human}% confidence, issued {issued_at[:10]}"

        db.insert_certificate(cert_id, sid, row["creator_id"],
                              issued_at, fingerprint, row["combined_score"], badge)

        return jsonify({
            "certificate_id":      cert_id,
            "submission_id":       sid,
            "creator_id":          row["creator_id"],
            "issued_at":           issued_at,
            "confidence":          confidence_human,
            "fingerprint_summary": fingerprint,
            "badge_text":          badge,
        }), 201

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/certificate/<cert_id>", methods=["GET"])
def get_certificate(cert_id):
    try:
        cert = db.get_certificate(cert_id)
        if not cert:
            return jsonify({"error": "Certificate not found."}), 404
        return jsonify(cert), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _get_fingerprint(text):
    if not text:
        return "Stylistic fingerprint unavailable."
    try:
        import os, re
        from groq import Groq
        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": (
                "In 1-2 sentences, describe the distinctive stylistic signature of this "
                "text that identifies it as human-written. Focus on specific measurable "
                f"properties.\n\nText:\n{text[:1500]}"
            )}],
            temperature=0.3, max_tokens=150,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return "Stylistic fingerprint generation failed."


# ---------------------------------------------------------------------------
# Health + error handlers
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "milestone": 5, "stretch": ["SF1","SF2","SF3","SF4"]})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found."}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed."}), 405

@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Rate limit exceeded.",
                    "detail": "Max 10/min, 100/day on /submit."}), 429


if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False)