import hashlib
import io
import json
import math
import os

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()
try:  # Streamlit Cloud secrets -> environment variables (providers.py reads os.environ)
    for _k in st.secrets:
        if isinstance(st.secrets[_k], str):
            os.environ.setdefault(_k, st.secrets[_k])
except Exception:
    pass

from kb import KnowledgeBase  # noqa: E402
from providers import transcribe  # noqa: E402
from quiz import AdaptiveQuiz, generate_pool  # noqa: E402
from tutor_core import new_session, run_turn  # noqa: E402

st.set_page_config(page_title="Socratic Study Companion", page_icon="🧠", layout="wide")

YELLOW = (255, 211, 61)


# ---------------- helpers ----------------
def prepare_image(file) -> bytes:
    """Normalize any upload to a reasonably small RGB JPEG (keeps API calls cheap)."""
    img = Image.open(file).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def draw_overlays(img_bytes: bytes, overlays: list) -> Image.Image:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    if not overlays:
        return img
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    lw = max(3, W // 200)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(14, W // 45))
    except Exception:
        font = ImageFont.load_default()
    for o in overlays:
        x, y = o.get("x", 0) / 100 * W, o.get("y", 0) / 100 * H
        lx, ly = x, y + lw * 3
        if o["type"] == "box":
            w, h = o.get("w", 10) / 100 * W, o.get("h", 10) / 100 * H
            d.rectangle([x, y, x + w, y + h], outline=YELLOW, width=lw)
            ly = y + h + 4
        elif o["type"] == "circle":
            r = o.get("r", 6) / 100 * min(W, H)
            d.ellipse([x - r, y - r, x + r, y + r], outline=YELLOW, width=lw)
            lx, ly = x - r, y + r + 4
        elif o["type"] == "arrow":
            x2, y2 = o.get("x2", 0) / 100 * W, o.get("y2", 0) / 100 * H
            d.line([x, y, x2, y2], fill=YELLOW, width=lw)
            a, hd = math.atan2(y2 - y, x2 - x), lw * 5
            d.polygon(
                [(x2, y2),
                 (x2 - hd * math.cos(a - 0.4), y2 - hd * math.sin(a - 0.4)),
                 (x2 - hd * math.cos(a + 0.4), y2 - hd * math.sin(a + 0.4))],
                fill=YELLOW,
            )
        if o.get("label"):
            bb = d.textbbox((lx, ly), o["label"], font=font)
            d.rectangle([bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2], fill=(0, 0, 0, 200))
            d.text((lx, ly), o["label"], fill=YELLOW, font=font)
    return img


def speak(text: str):
    """Browser text-to-speech (best effort; some browsers block autoplay)."""
    components.html(
        f"<script>try{{speechSynthesis.cancel();"
        f"speechSynthesis.speak(new SpeechSynthesisUtterance({json.dumps(text)}));}}catch(e){{}}</script>",
        height=0,
    )


# ---------------- session state ----------------
S = st.session_state
S.setdefault("sess", new_session())
S.setdefault("img", None)
S.setdefault("img_key", None)
S.setdefault("chat", [])
S.setdefault("overlays", [])
S.setdefault("meta", None)
S.setdefault("last_audio", None)
S.setdefault("kb", KnowledgeBase())
S.setdefault("misc", [])      # misconceptions detected while tutoring (feeds the quiz)
S.setdefault("quiz", None)


def reset():
    S.sess, S.chat, S.overlays, S.meta, S.last_audio = new_session(), [], [], None, None
    S.misc, S.quiz = [], None


def render_chat(chat):
    for item in chat:
        who, text = item[0], item[1]
        if who == "you":
            st.chat_message("user").write(text)
        elif who == "tutor":
            with st.chat_message("assistant"):
                st.write(text)
                if len(item) > 2 and item[2]:
                    labels = " · ".join(S.kb.label(c) for c in item[2])
                    st.caption(("📚 Related in your materials: " if item[3] else "📚 Sources: ") + labels)
        else:
            st.error(text)


st.title("🧠 Socratic Study Companion")
st.caption(
    "Your course materials + your own diagrams → source-cited Socratic tutoring and adaptive quizzes."
)
tab_tutor, tab_kb, tab_quiz = st.tabs(["🧠 Tutor", "📚 Course materials", "📝 Adaptive quiz"])

# =============== TAB 1: TUTOR ===============
with tab_tutor:
    left, right = st.columns([1.3, 1], gap="large")

    with left:
        up = st.file_uploader("Photo or diagram of your problem", type=["png", "jpg", "jpeg", "webp"])
        if up is not None:
            key = f"{up.name}-{up.size}"
            if S.img_key != key:
                reset()
                S.img, S.img_key = prepare_image(up), key
                S.chat.append(("tutor", "Got your problem. Tell me (type or use the mic) where you're stuck."))
        with st.expander("🔑 Teacher notes (optional): reference solution"):
            st.caption("Hidden ground truth for the tutor. It steers toward this but never reveals it.")
            st.text_area("Reference solution", key="ref", height=150, label_visibility="collapsed")

    with right:
        ai = st.selectbox("AI provider", ["default", "groq", "gemini"], index=0)
        use_tts = st.checkbox("Speak replies", value=True)
        audio = st.audio_input("🎤 Record a voice note")

    typed = st.chat_input("Where are you stuck? Or answer the tutor...")

    # ---- handle new input ----
    message = typed
    if audio is not None:
        data = audio.getvalue()
        h = hashlib.md5(data).hexdigest()
        if h != S.last_audio:
            S.last_audio = h
            try:
                with st.spinner("Transcribing..."):
                    message = transcribe(data, "voice.wav")
            except Exception as e:
                S.chat.append(("err", f"Voice failed: {e}"))

    spoke = None
    if message and message.strip():
        if S.img is None:
            st.warning("Upload an image of your problem first.")
        else:
            S.chat.append(("you", message))
            first = S.sess["turns"] == 0 and not S.sess["history"]
            hits = []
            if S.kb.chunks:  # retrieve from the student's own materials
                last_q = S.sess["asked"][-1] if S.sess["asked"] else ""
                last_m = S.misc[-1] if S.misc else ""
                hits = S.kb.search(f"{message} {last_m} {last_q}", k=3)
            try:
                with st.spinner("Thinking..."):
                    r = run_turn(
                        S.sess, message,
                        image=(S.img, "image/jpeg") if first else None,
                        provider=None if ai == "default" else ai,
                        reference=S.get("ref", "") or "",
                        excerpts=S.kb.format(hits),
                    )
                cites = [c for c in r.get("citations", []) if c in S.kb.by_id]
                related = False
                if not cites and hits:
                    cites, related = [hits[0]["id"]], True
                S.chat.append(("tutor", r["spoken_question"], cites, related))
                S.overlays, S.meta, spoke = r["overlays"], r, r["spoken_question"]
                if r["misconception"] and r["misconception"] not in S.misc:
                    S.misc.append(r["misconception"])
            except Exception as e:
                S.chat.append(("err", str(e)))

    # ---- render ----
    with left:
        if S.img:
            st.image(draw_overlays(S.img, S.overlays), use_container_width=True)
            if st.button("Start over"):
                reset()
                S.img, S.img_key = None, None
                st.rerun()

    with right:
        if S.meta:
            st.caption(
                f"hint level {S.meta['hint_level']}/3  ·  {S.meta['student_progress']}  ·  "
                f"{S.meta['misconception'] or '-'}"
            )
        render_chat(S.chat)
        if S.meta:
            with st.expander("Tutor's reasoning"):
                st.write(S.meta["reasoning"])

    if spoke and use_tts:
        speak(spoke)

# =============== TAB 2: COURSE MATERIALS ===============
with tab_kb:
    st.subheader("Source-cited knowledge base")
    st.caption(
        "Upload slides (.pptx), textbook chapters (.pdf), and lecture videos as transcripts "
        "(.srt, .vtt, .txt, .md). The tutor and the quiz draw on these and cite where each idea comes from."
    )
    files = st.file_uploader(
        "Course materials", type=["pdf", "pptx", "srt", "vtt", "txt", "md"],
        accept_multiple_files=True, key="kb_files", label_visibility="collapsed",
    )
    for f in files or []:
        if f.name not in S.kb.files:
            try:
                with st.spinner(f"Indexing {f.name}..."):
                    S.kb.add_file(f.name, f.getvalue())
            except Exception as e:
                st.error(f"{f.name}: {e}")
    if S.kb.files:
        st.write(f"**{len(S.kb.chunks)} indexed passages** from {len(S.kb.files)} file(s):")
        for name, n in S.kb.files.items():
            st.write(f"- {name}: {n} passages")
        probe = st.text_input("🔎 Try a search", placeholder="e.g. does an ideal voltmeter draw current?")
        if probe:
            for hit in S.kb.search(probe, k=3):
                st.markdown(f"**{S.kb.label(hit['id'])}** (relevance {hit['score']})")
                st.write(hit["text"][:400])
    else:
        st.info("No materials yet. Try the files in the repo's samples/ folder.")

# =============== TAB 3: ADAPTIVE QUIZ ===============
def quiz_material(focus: str) -> str:
    parts = []
    if S.kb.chunks:
        first_msg = next((m[1] for m in S.chat if m[0] == "you"), "")
        hits = S.kb.search(f"{focus} {first_msg}", k=6)
        seen = {h["id"] for h in hits}
        for c in S.kb.sample(6):
            if len(hits) >= 6:
                break
            if c["id"] not in seen:
                hits.append(c)
        parts.append(S.kb.format(hits, limit=600))
    ref = str(S.get("ref", "") or "").strip()
    if ref:
        parts.append(f"[R1] (Teacher notes) {ref[:900]}")
    return "\n".join(parts)


def render_quiz():
    quiz = S.quiz
    if quiz is None:
        if not (S.kb.chunks or str(S.get("ref", "") or "").strip()):
            st.info("Upload course materials in the 📚 tab first (or add teacher notes in the Tutor tab).")
            return
        focus = ", ".join(S.misc[-3:])
        st.write(
            "I'll write a short quiz from your materials. Difficulty adapts to your answers, "
            "and every question points back to its source."
        )
        if focus:
            st.write(f"🎯 Targeting what came up while tutoring: **{focus}**")
        if st.button("Generate my adaptive quiz", type="primary"):
            try:
                with st.spinner("Writing questions from your materials..."):
                    pool = generate_pool(
                        quiz_material(focus), focus, provider=None if ai == "default" else ai
                    )
                    start = 1 if (S.meta and S.meta["hint_level"] >= 3) else 2
                    S.quiz = AdaptiveQuiz(pool, start_level=start)
                    S.quiz.advance()
                st.rerun()
            except Exception as e:
                st.error(str(e))
        return

    if quiz.phase == "answering":
        q = quiz.current
        done = len(quiz.results)
        st.progress(done / quiz.max_q, text=f"Question {done + 1} of {quiz.max_q} · difficulty {q['difficulty']}/3 · {q['concept']}")
        st.markdown(f"### {q['question']}")
        choice = st.radio("Pick one", q["options"], index=None, key=f"quiz_q{done}")
        if st.button("Submit answer", disabled=choice is None):
            quiz.submit(q["options"].index(choice))
            st.rerun()
    elif quiz.phase == "feedback":
        fb = quiz.feedback
        if fb["correct"]:
            st.success("Correct!")
        else:
            st.error(f"Not quite. The answer was: {fb['right']}")
        if fb["explanation"]:
            st.write(fb["explanation"])
        if fb["source_id"]:
            st.caption(f"📚 Source: {S.kb.label(fb['source_id'])}")
        st.caption(f"Next question difficulty: {fb['next_level']}/3 (adapts to your answers)")
        if st.button("Next question", type="primary"):
            quiz.advance()
            st.rerun()
    else:
        right_n, total = quiz.score()
        st.subheader(f"Your result: {right_n}/{total}")
        st.write("**Mastery by concept**")
        for concept, (ok, n) in quiz.mastery().items():
            st.progress(ok / n, text=f"{concept}: {ok}/{n}")
        weak = quiz.weakest()
        if weak and quiz.score()[0] < total:
            st.write(f"🎯 Focus next on: **{weak}**")
        missed = quiz.missed_sources()
        if missed:
            st.write("📚 Review these sources: " + " · ".join(S.kb.label(c) for c in missed))
        if st.button("New quiz"):
            S.quiz = None
            st.rerun()


with tab_quiz:
    render_quiz()
