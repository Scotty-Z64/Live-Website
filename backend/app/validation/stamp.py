"""Commissioner of Oaths stamp verification: OpenCV candidate detection + Vision-LLM confirmation.

Decision rule (deterministic):
* OpenCV must find at least one ink-stamp candidate region (coloured, compact, stamp-sized).
* If ANTHROPIC_API_KEY is configured, Claude must independently confirm a Commissioner of Oaths
  stamp AND a signature, and the region it reports must overlap an OpenCV candidate.
* Without a vision model, the OpenCV candidate plus the literal "Commissioner of Oaths" text on
  the page is accepted, and the result is marked ``vision_verified = false`` for audit.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date

import cv2
import numpy as np

from ..config import get_settings
from .parsing import parse_date

log = logging.getLogger(__name__)


@dataclass
class StampCandidate:
    x: int
    y: int
    w: int
    h: int
    score: float
    shape: str


@dataclass
class StampVerdict:
    stamp_present: bool
    signature_present: bool | None
    stamp_date: date | None
    method: str
    vision_verified: bool
    candidates: list[StampCandidate] = field(default_factory=list)
    vision: dict | None = None
    reason: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["stamp_date"] = self.stamp_date.isoformat() if self.stamp_date else None
        return d


def detect_candidates(png: bytes) -> list[StampCandidate]:
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h_img, w_img = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # Stamp inks: blue/violet and red. Saturation threshold excludes black body text.
    blue = cv2.inRange(hsv, (95, 60, 40), (150, 255, 255))
    red1 = cv2.inRange(hsv, (0, 70, 50), (10, 255, 255))
    red2 = cv2.inRange(hsv, (165, 70, 50), (180, 255, 255))
    mask = cv2.morphologyEx(blue | red1 | red2, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_side = 0.06 * min(h_img, w_img)
    max_side = 0.6 * min(h_img, w_img)
    out: list[StampCandidate] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        aspect = w / h
        if not 0.4 <= aspect <= 2.5:
            continue
        area = cv2.contourArea(c)
        perim = cv2.arcLength(c, True) or 1
        circularity = 4 * np.pi * area / (perim * perim)
        shape = "circular" if circularity > 0.6 and 0.8 <= aspect <= 1.25 else "rectangular"
        ink_density = float(mask[y : y + h, x : x + w].mean() / 255.0)
        score = round(min(1.0, 0.5 * ink_density + 0.5 * min(1.0, circularity + 0.3)), 3)
        out.append(StampCandidate(int(x), int(y), int(w), int(h), score, shape))
    # Also look for dark circular stamps (black-ink rubber stamps) with Hough circles.
    gray = cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 5)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=int(min_side), param1=120, param2=60,
        minRadius=int(min_side / 2), maxRadius=int(max_side / 2),
    )
    if circles is not None:
        dark = cv2.dilate((gray < 110).astype(np.uint8), np.ones((5, 5), np.uint8))
        angles = np.linspace(0, 2 * np.pi, 240, endpoint=False)
        for cx, cy, r in np.round(circles[0, :8]).astype(int):
            xs = np.clip((cx + r * np.cos(angles)).astype(int), 0, w_img - 1)
            ys = np.clip((cy + r * np.sin(angles)).astype(int), 0, h_img - 1)
            coverage = float(dark[ys, xs].mean())
            # A rubber stamp has a continuous inked ring; text blocks do not.
            if coverage >= 0.85:
                out.append(StampCandidate(int(cx - r), int(cy - r), int(2 * r), int(2 * r), round(0.5 * coverage, 3), "circular-dark"))
    return sorted(out, key=lambda c: -c.score)[:5]


VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "commissioner_stamp_present": {"type": "boolean"},
        "signature_present": {"type": "boolean"},
        "stamp_text": {"type": "string"},
        "stamp_date": {"type": "string", "description": "Date written in or beside the stamp, ISO YYYY-MM-DD, or empty"},
        "stamp_bbox_pct": {
            "type": "object",
            "description": "Stamp bounding box as percentages of page width/height",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "w": {"type": "number"}, "h": {"type": "number"},
            },
            "required": ["x", "y", "w", "h"],
            "additionalProperties": False,
        },
    },
    "required": ["commissioner_stamp_present", "signature_present", "stamp_text", "stamp_date", "stamp_bbox_pct"],
    "additionalProperties": False,
}

VISION_PROMPT = (
    "You are verifying a South African sworn affidavit page. Report only what is visibly present. "
    "commissioner_stamp_present is true only if there is an inked or embossed stamp identifying a "
    "Commissioner of Oaths (or SAPS / attorney ex officio commissioner). signature_present is true "
    "if a handwritten signature appears near the stamp or deponent line. Copy the stamp's legible text "
    "verbatim into stamp_text. If no stamp exists, return a zero bounding box."
)


def vision_check(png: bytes) -> dict | None:
    s = get_settings()
    if not s.anthropic_api_key:
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    try:
        resp = client.beta.messages.create(
            model=s.anthropic_model,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": VISION_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.standard_b64encode(png).decode(),
                            },
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        log.warning("vision stamp check failed: %s", exc)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _overlaps(bbox_pct: dict, cands: list[StampCandidate], w: int, h: int) -> bool:
    bx, by = bbox_pct["x"] / 100 * w, bbox_pct["y"] / 100 * h
    bw, bh = bbox_pct["w"] / 100 * w, bbox_pct["h"] / 100 * h
    for c in cands:
        ix = max(0, min(bx + bw, c.x + c.w) - max(bx, c.x))
        iy = max(0, min(by + bh, c.y + c.h) - max(by, c.y))
        if ix * iy > 0:
            return True
    return False


def verify_commissioner_stamp(page_png: bytes, page_text: str) -> StampVerdict:
    cands = detect_candidates(page_png)
    img = cv2.imdecode(np.frombuffer(page_png, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2] if img is not None else (1, 1)
    text_cue = bool(re.search(r"commissioner\s+of\s+oaths", page_text, re.I))
    sworn = re.search(r"(?:sworn|commissioned|signed)[^\n]{0,80}?(?:on|this)\s+([^\n]{4,40})", page_text, re.I)
    text_date = parse_date(sworn.group(1)) if sworn else None

    vision = vision_check(page_png)
    if vision is not None:
        vdate = parse_date(vision.get("stamp_date") or "") or text_date
        present = bool(vision["commissioner_stamp_present"]) and bool(cands) and _overlaps(vision["stamp_bbox_pct"], cands, w, h)
        reason = "" if present else (
            "Vision model did not confirm a Commissioner of Oaths stamp"
            if not vision["commissioner_stamp_present"]
            else "Vision stamp region not corroborated by OpenCV ink detection"
        )
        return StampVerdict(present, bool(vision["signature_present"]), vdate, "opencv+vision-llm", True, cands, vision, reason)

    present = bool(cands) and text_cue
    reason = "" if present else ("No ink-stamp region detected" if not cands else "No 'Commissioner of Oaths' text near stamp")
    return StampVerdict(present, None, text_date, "opencv+text", False, cands, None, reason)
