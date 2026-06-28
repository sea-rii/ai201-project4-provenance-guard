"""
signals/image_consistency.py - Signal 4 (SF-4): Multi-modal image consistency.

When a creator submits text alongside an image_url, this signal asks Groq
to describe the image independently via URL, then scores how semantically
consistent the submitted description is with the AI-generated one.

Output:
    (score: float [0,1], ai_description: str | None)
    0.0 = very different descriptions (human-like observation)
    1.0 = nearly identical (AI-like)
    0.5 = abstain (any failure — graceful degradation)
"""

import os
import re
import json
import sys
from groq import Groq

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
TEXT_MODEL   = "llama-3.3-70b-versatile"


def _groq_describe_image(image_url: str) -> str:
    """Ask Groq to describe the image by URL."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this image in 2-3 sentences. Focus on: "
                            "the main subject, notable visual details, composition, "
                            "and overall mood or tone. Be specific and observational."
                        ),
                    },
                ],
            }
        ],
        max_tokens=200,
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def _consistency_score(submitted: str, generated: str) -> float:
    """Score semantic similarity 0.0 (different) to 1.0 (identical)."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    prompt = f"""Compare these two image descriptions and rate their semantic similarity.

Description A (submitted by creator):
{submitted}

Description B (independently AI-generated from the image):
{generated}

Rate 0.0-1.0:
- 1.0 = essentially the same content and focus
- 0.5 = some overlap, different emphasis or details  
- 0.0 = describing completely different things

High similarity suggests the submitted description may be AI-generated.
Low similarity suggests human observation with personal details a model wouldn't notice.

Respond ONLY with valid JSON, no preamble or markdown:
{{"similarity": <float 0.0-1.0>, "reasoning": "<one sentence>"}}"""

    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=150,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    return max(0.0, min(1.0, float(parsed["similarity"])))


def image_consistency_score(submitted_description: str, image_url: str):
    """
    Main entry point. Returns (score, ai_description) or (0.5, None) on failure.
    Failure is always graceful — the pipeline continues with other signals.
    """
    if not image_url or not submitted_description:
        return 0.5, None
    try:
        ai_description = _groq_describe_image(image_url)
        score = _consistency_score(submitted_description, ai_description)
        return score, ai_description
    except Exception as exc:
        print(f"[image_consistency] Error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 0.5, None


if __name__ == "__main__":
    # Standalone test with a publicly accessible image
    test_url = "https://images.unsplash.com/photo-1552053831-71594a27632d?w=800"
    test_desc = "A golden retriever dog sitting outdoors, looking alert and happy."
    print("Testing image consistency signal...")
    print(f"URL: {test_url}")
    score, desc = image_consistency_score(test_desc, test_url)
    print(f"AI description: {desc}")
    print(f"Consistency score: {score}")