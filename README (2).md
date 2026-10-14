# 🧠 Socratic Study Companion

An AI study companion for **Track D: Personalized Tutoring & Adaptive Learning**.

It turns a student's own **slides, textbook chapters and lecture transcripts** into a
**source-cited knowledge base**, then uses it to run **Socratic tutoring** on the student's own
problem (a photo of a diagram, plus a voice note) and **adaptive quizzes** that target what the
student actually got wrong. The tutor never gives the answer; it asks one guiding question,
points to the right source, and draws on the student's diagram.

## Demo
- Live app: _add link_
- Demo video: _add link_

## How it covers the track
| Track requirement | What this project does |
|---|---|
| Unify lecture videos, textbooks and slides | Upload `.pdf`, `.pptx`, and lecture transcripts (`.srt`, `.vtt`, `.txt`, `.md`) into one knowledge base |
| Source-cited | Every passage keeps its file and location (`slide 4`, `p. 2`, `01:20`); tutor replies and quiz answers show where each idea came from |
| Adaptive assessments | Quiz difficulty rises or falls per answer; mastery is tracked per concept; the quiz targets misconceptions detected while tutoring |
| Personalized tutoring | Socratic questions, a 3-level hint ladder, misconception tracking, reference-solution grounding |
| Multimodal | Photo/diagram understanding with overlays drawn on the image, voice input (Whisper), spoken replies |

## How it works
```
 slides / textbook / transcripts --> [kb.py] chunks tagged with source + location, BM25 search
                                          |
 student's problem photo + voice --> [tutor_core.py] --> vision LLM --> question, overlays, citations
                                          |                 ^
                                          |                 +-- top passages from the knowledge base
                                          v
                               misconceptions detected --> [quiz.py] pool of cited questions
                                                              adaptive difficulty + mastery tracking
```

## Design decisions (what I learned)
- **The LLM proposes, the code enforces.** The model alone repeated questions and mis-tracked
  hint levels. Hint level, stuck-count and "never repeat a question" (including close rephrasings)
  now live in code, and a repeat triggers an automatic retry.
- **Adaptivity is code, not vibes.** One LLM call writes a pool of questions at three
  difficulty levels; the code picks the next question from the student's results and tracks
  mastery per concept.
- **A confident tutor can be wrong, and agreeable.** An early version said an ideal voltmeter acts
  "like a wire", and confirmed a student's wrong guess just because they asked "right?". Fixes:
  a physics-facts guardrail, rules against confirming unverified claims, and an optional
  teacher-supplied **reference solution** that the tutor steers toward but never reveals.
- **Ground answers in the student's own materials.** Retrieval-augmented answers with citations
  make guidance verifiable.
- **LLM output is untrusted input.** Responses are parsed defensively, validated, clamped, and
  retried on malformed JSON or rate limits.
- **Simple retrieval on purpose.** BM25 in pure Python instead of a vector database: no extra
  infrastructure, easy to explain, good enough for course-sized material.
- **Vision coordinates are approximate,** so overlays use generous shapes.

## Try it
Sample course material (written for this demo) is in `samples/`: slides, a chapter PDF, a lecture
transcript and a cheat sheet on DC circuits. Upload them in the **Course materials** tab, then
upload a circuit problem in the **Tutor** tab.

## Limitations / next steps
- Lecture videos are supported as transcripts; automatic speech-to-text from video files is next
- Dense or blurry diagrams reduce reading accuracy (a tap-to-confirm step would help)
- Mastery is per session; persisting it across sessions would enable spaced repetition
- Scanned PDFs need OCR

## Run locally
```bash
pip install -r requirements.txt
# put keys in .streamlit/secrets.toml (see secrets.toml.example) or a .env file
streamlit run app.py
```

## Deploy (Streamlit Community Cloud)
1. Push this repo to GitHub
2. share.streamlit.io -> New app -> repo, branch `main`, main file `app.py`
3. Advanced settings -> Secrets -> paste `.streamlit/secrets.toml.example` with your real key
