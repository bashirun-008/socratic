"""Adaptive assessment: one LLM call writes a pool of source-cited questions at 3 difficulty
levels; the code then adapts difficulty per answer and tracks mastery per concept."""
import re
import time

from providers import llm_json
from tutor_core import parse_tutor_json

QUIZ_SYSTEM = """You write multiple-choice questions for a student using ONLY the course excerpts provided.
Return ONLY one JSON object, no prose, no markdown fences:
{"questions":[{"concept":"2-4 word label","difficulty":1,"question":"...","options":["...","...","...","..."],"answer_index":0,"explanation":"1-2 sentences","source_id":"S3"}]}
Rules:
- Write exactly the requested number of questions, spread evenly over difficulty 1, 2 and 3
  (1 = recall a fact, 2 = apply one idea, 3 = multi-step reasoning).
- Every question must be answerable from the excerpts. source_id is the excerpt id it comes from (S1, S2... or R1).
- Exactly 4 options, one correct. Put the correct option at a varied position. Options are plain text without A/B/C/D.
- Make wrong options plausible, based on common misconceptions (especially the FOCUS ones).
- Never use double quotes or line breaks inside string values."""


def _clean(q: dict):
    try:
        opts = [str(o).strip() for o in q["options"]]
        ans = int(q["answer_index"])
        if len(opts) != 4 or not 0 <= ans <= 3 or not str(q["question"]).strip():
            return None
        return {
            "concept": str(q.get("concept", "general"))[:40] or "general",
            "difficulty": max(1, min(3, int(q.get("difficulty", 2)))),
            "question": str(q["question"]).strip(),
            "options": opts,
            "answer_index": ans,
            "explanation": str(q.get("explanation", "")).strip(),
            "source_id": str(q.get("source_id", "")).strip(),
        }
    except (KeyError, TypeError, ValueError):
        return None


def generate_pool(excerpts: str, focus: str = "", n: int = 6, provider=None) -> list[dict]:
    user = (
        f"FOCUS (misconceptions this student showed, target them): {focus or 'none'}\n\n"
        f"EXCERPTS:\n{excerpts}\n\nWrite {n} questions."
    )
    last = None
    for attempt in range(3):
        try:
            data = parse_tutor_json(llm_json(QUIZ_SYSTEM, user, provider))
            pool = [c for c in (_clean(q) for q in data.get("questions", [])) if c]
            if len(pool) >= 3:
                return pool
            last = ValueError("model returned too few valid questions")
        except Exception as e:
            msg = str(e)
            last = e
            if ("429" in msg or "rate_limit" in msg.lower()) and attempt < 2:
                wait = re.search(r"try again in ([0-9.]+)s", msg)
                time.sleep(min(float(wait.group(1)) + 1 if wait else 8, 20))
                continue
            if not isinstance(e, ValueError):  # real API error (key, model name): don't retry
                raise RuntimeError(f"Quiz error: {e}")
    raise RuntimeError(f"Could not build a quiz (try again): {last}")


class AdaptiveQuiz:
    """The code, not the model, decides what comes next: right answers raise the
    difficulty, wrong answers lower it, and mastery is tracked per concept."""

    def __init__(self, pool: list[dict], start_level: int = 2, max_q: int = 5):
        self.pool = pool
        self.level = max(1, min(3, start_level))
        self.max_q = min(max_q, len(pool))
        self.used: set[int] = set()
        self.results: list[dict] = []
        self.current = None
        self.feedback = None
        self.phase = "answering"

    def advance(self):
        remaining = [i for i in range(len(self.pool)) if i not in self.used]
        if len(self.results) >= self.max_q or not remaining:
            self.phase, self.current = "done", None
            return None
        i = min(remaining, key=lambda j: (abs(self.pool[j]["difficulty"] - self.level), j))
        self.used.add(i)
        self.current, self.phase, self.feedback = self.pool[i], "answering", None
        return self.current

    def submit(self, choice: int) -> bool:
        q = self.current
        correct = choice == q["answer_index"]
        self.results.append({
            "concept": q["concept"], "difficulty": q["difficulty"], "correct": correct,
            "source_id": q["source_id"], "question": q["question"],
        })
        self.level = max(1, min(3, self.level + (1 if correct else -1)))
        self.feedback = {"correct": correct, "explanation": q["explanation"],
                         "source_id": q["source_id"], "right": q["options"][q["answer_index"]],
                         "next_level": self.level}
        self.phase = "feedback"
        return correct

    def mastery(self) -> dict:
        out: dict[str, list[int]] = {}
        for r in self.results:
            c = out.setdefault(r["concept"], [0, 0])
            c[0] += r["correct"]
            c[1] += 1
        return out

    def score(self) -> tuple[int, int]:
        return sum(r["correct"] for r in self.results), len(self.results)

    def weakest(self):
        m = self.mastery()
        return min(m, key=lambda c: m[c][0] / m[c][1]) if m else None

    def missed_sources(self) -> list[str]:
        seen, out = set(), []
        for r in self.results:
            if not r["correct"] and r["source_id"] and r["source_id"] not in seen:
                seen.add(r["source_id"]); out.append(r["source_id"])
        return out
