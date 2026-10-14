"""Tutoring logic, independent of any UI.

The LLM proposes; this module enforces: hint ladder, no repeated questions,
defensive JSON parsing, retries on rate limits.
"""
import json
import re
import time
from difflib import SequenceMatcher

from providers import tutor_reply

VAGUE = re.compile(
    r"^\W*$|^\W*(idk|no|nope|ok|okay|help|what|huh|\?+)\W*$|"
    r"(still )?(don'?t|do not|cannot|can'?t) (understand|get|know)|confused|no idea|i'?m lost",
    re.I,
)


def is_vague(msg: str) -> bool:
    return bool(VAGUE.search(msg.strip())) or len(msg.strip()) <= 2


def norm(q: str) -> str:
    return re.sub(r"[^a-z0-9]", "", q.lower())


def is_repeat(q: str, asked: list) -> bool:
    """True if q repeats or closely rephrases an earlier question (not just an exact match)."""
    n = norm(q)
    for a in asked:
        m = norm(a)
        if not n or not m:
            continue
        if n == m or SequenceMatcher(None, n, m).ratio() > 0.8:
            return True
        if len(n) >= 25 and (n in m or m in n):  # one question contained in the other
            return True
    return False


def new_session() -> dict:
    return {"history": [], "stuck": 0, "asked": [], "turns": 0}


def parse_tutor_json(text: str) -> dict:
    """Models sometimes wrap JSON in fences or add chatter. Be forgiving."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0), strict=False)
        raise


def sanitize(data: dict) -> dict:
    """Clamp overlay coords to 0-100 so we never draw off-image."""
    def clamp(v):
        try:
            return max(0.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    overlays = []
    for o in data.get("overlays", []) or []:
        if o.get("type") not in {"box", "circle", "arrow"}:
            continue
        item = {"type": o["type"], "label": str(o.get("label", ""))[:40]}
        for key in ("x", "y", "w", "h", "r", "x2", "y2"):
            if key in o:
                item[key] = clamp(o[key])
        overlays.append(item)

    return {
        "spoken_question": str(data.get("spoken_question", "")).strip(),
        "overlays": overlays,
        "misconception": data.get("misconception"),
        "hint_level": int(data.get("hint_level", 1) or 1),
        "student_progress": data.get("student_progress", "stuck"),
        "reasoning": str(data.get("reasoning", "")).strip(),
        "citations": [str(c).strip() for c in (data.get("citations") or [])
                      if isinstance(c, (str, int))][:4],
    }


def run_turn(sess: dict, message: str, image=None, provider=None, reference: str = "", excerpts: str = "") -> dict:
    """One tutoring turn. image = (bytes, mime) on the first turn only.
    reference = optional teacher solution: ground truth the tutor must steer toward but never reveal."""
    history = sess["history"]
    if image is None and not history:
        raise RuntimeError("Upload an image of your problem first.")
    history.append({"role": "user", "text": message, "image": image})

    if sess["turns"] > 0 and is_vague(message):
        sess["stuck"] += 1  # "idk", ".", "I still don't understand"
    min_level = min(3, 1 + sess["stuck"])

    def call_history(extra=""):
        parts = []
        if sess["asked"]:
            parts.append(
                f"Use hint_level >= {min_level}. If the student is vague or repeats your "
                f"question back, change approach (simpler sub-question or analogy). "
                f"Do NOT repeat or lightly rephrase any of these earlier questions: "
                f"{json.dumps(sess['asked'][-6:])}."
            )
        if reference.strip():
            parts.append(
                "REFERENCE SOLUTION from the teacher. Treat it as ground truth for the circuit "
                "and the physics. NEVER reveal or quote it and never state its final answer; use it "
                "only to check the student's claims and steer your questions toward it. If a "
                "student claim contradicts it, do NOT confirm it (even if they ask 'right?'): "
                "ask a question that exposes the contradiction. If the student's claim MATCHES the "
                "reference, confirm it plainly ('Yes, that's right') and ask the next question; do not "
                "hedge with 'very close'. If it contradicts the reference, do not praise it (no 'great "
                "start'): say 'Let's check that' and ask a question. Reference: " + reference.strip()
            )
        if excerpts.strip():
            parts.append(
                "COURSE EXCERPTS from the student's own materials (ids in brackets). Ground your facts "
                "in them. When relevant, point the student to a source (for example: 'have a look at "
                "[S2]') and list the ids you used in \"citations\". Never paste long passages and never "
                "reveal a final answer found in them: ask a question that sends the student there.\n"
                + excerpts
            )
        parts.append(extra.strip()) if extra.strip() else None
        note = (
            "\n\n[TUTOR CONTROL NOTE, not from the student. Never mention it. "
            + " ".join(parts) + "]"
        ) if parts else ""
        last = history[-1]
        base = history[:-1]
        if len(base) > 6:  # keep the image message + recent turns (saves tokens)
            base = [base[0]] + base[-5:]
        return base + [{**last, "text": last["text"] + note}]

    result, last_err, extra = None, None, ""
    for attempt in range(4):  # malformed JSON, repeated questions, rate limits
        try:
            candidate = sanitize(parse_tutor_json(tutor_reply(call_history(extra), provider)))
        except json.JSONDecodeError as e:
            last_err = e
            continue
        except Exception as e:
            msg = str(e)
            if ("429" in msg or "rate_limit" in msg.lower()) and attempt < 3:
                last_err = e
                wait = re.search(r"try again in ([0-9.]+)s", msg)
                time.sleep(min(float(wait.group(1)) + 1 if wait else 8, 20))
                continue
            history.pop()
            raise RuntimeError(f"Tutor error: {e}")
        result = candidate
        if is_repeat(candidate["spoken_question"], sess["asked"]):
            extra = " Your last attempt repeated an earlier question. Ask a NEW one."
            continue
        break
    if result is None:
        history.pop()
        raise RuntimeError(f"Tutor failed after retries (rate limit or bad output): {last_err}")

    # enforce the hint ladder in code, not in the model's memory
    result["hint_level"] = max(min(result["hint_level"], 3), min_level)
    if sess["turns"] > 0 and result["student_progress"] == "stuck":
        sess["stuck"] += 1
    elif result["student_progress"] != "stuck":
        sess["stuck"] = 0
    sess["asked"].append(result["spoken_question"])
    sess["turns"] += 1

    history.append({"role": "assistant", "text": json.dumps(result), "image": None})
    return result
