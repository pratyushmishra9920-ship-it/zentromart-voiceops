from __future__ import annotations

import os
import re
import hashlib
import requests
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from tools import create_ticket, lookup_order

ROOT = Path(__file__).resolve().parent
KB_DIR = ROOT / "kb"
DATA_DIR = ROOT / "data"
DATASET_DATE = "20 October 2026"

load_dotenv(ROOT / ".env")
def get_gnani_api_key():
    try:
        key = st.secrets.get("GNANI_API_KEY", "")
    except Exception:
        key = ""

    return str(key or os.getenv("GNANI_API_KEY", "")).strip()

st.set_page_config(page_title="ZentroMart VoiceOps", page_icon="🎧", layout="wide")
st.title("🎧 ZentroMart VoiceOps")
st.caption("Customer-support assistant · Synthetic capstone dataset · Dataset date: 20 October 2026")

with st.sidebar:
    st.subheader("Project status")
    st.write("Text support: active")
    st.write("Knowledge base: local Markdown files")
    st.write("Order lookup: identity-check gated")
    st.write("Ticket creation: local mock tool")
    st.write("Voice: Gnani STT/TTS integration enabled")
    st.info("Never share an order's status, items, address, amount or refund details until verification succeeds.")

def parse_front_matter(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if not text.startswith("---"):
        return metadata
    parts = text.split("---", 2)
    if len(parts) < 3:
        return metadata
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata

@st.cache_resource
def load_knowledge_base() -> tuple[list[dict[str, Any]], TfidfVectorizer | None, Any]:
    documents: list[dict[str, Any]] = []
    for path in sorted(KB_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        metadata = parse_front_matter(raw)
        status = metadata.get("status", "current").lower()
        if status == "superseded":
            continue
        # Preserve section IDs so retrieval can be evaluated.
        sections = re.split(r"(?m)(?=^#{1,4}\s+)", raw)
        for section in sections:
            section = section.strip()
            if not section or section.startswith("---"):
                continue
            heading_match = re.search(r"(?m)^#{1,4}\s+(.+)$", section)
            heading = heading_match.group(1).strip() if heading_match else metadata.get("title", path.stem)
            id_match = re.search(r"\[([A-Z0-9][A-Z0-9._-]*)\]", heading)
            section_id = id_match.group(1) if id_match else metadata.get("doc_id", path.stem)
            clean = re.sub(r"(?m)^#{1,4}\s+", "", section)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                documents.append({
                    "file": path.name,
                    "title": heading,
                    "section_id": section_id,
                    "doc_type": metadata.get("doc_type", "unknown"),
                    "status": status,
                    "text": clean,
                })
    if not documents:
        return [], None, None
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    matrix = vectorizer.fit_transform([doc["text"] for doc in documents])
    return documents, vectorizer, matrix

def retrieve(query: str, limit: int = 3) -> list[dict[str, Any]]:
    docs, vectorizer, matrix = load_knowledge_base()
    if not docs or vectorizer is None or matrix is None:
        return []
    query_vector = vectorizer.transform([query])
    scores = cosine_similarity(query_vector, matrix).ravel()
    ranked = scores.argsort()[::-1]
    results = []
    for idx in ranked:
        if scores[idx] <= 0:
            continue
        item = dict(docs[int(idx)])
        item["score"] = float(scores[idx])
        results.append(item)
        if len(results) >= limit:
            break
    # Current policy sections outrank other document types when scores are close.
    results.sort(key=lambda d: (d["doc_type"] == "policy", d["score"]), reverse=True)
    return results

def add_message(role: str, content: str) -> None:
    st.session_state.messages.append({"role": role, "content": content})

def is_order_request(text: str) -> bool:
    lower = text.lower()
    return bool(re.search(r"\b(order|delivery|delivered|refund|return|cancel|shipment|tracking|item bought|purchase)\b", lower)
                and re.search(r"\d{4,}", text)) or lower.strip() in {"check order", "order status", "track order"}

def get_order_id(text: str) -> str | None:
    # Preserve the numeric order ID; the mock tool normalizes spaces/punctuation.
    match = re.search(r"(?<!\d)(?:\d[\s-]*){8,14}(?!\d)", text)
    return re.sub(r"\D", "", match.group(0)) if match else None

def respond(user_text: str) -> str:
    text = user_text.strip()
    lower = text.lower()

    # A user who explicitly asks for a human can create a handover ticket.
    if any(phrase in lower for phrase in ("human agent", "human support", "speak to a person", "talk to a person", "escalate", "raise a ticket")):
        verified_order_id = st.session_state.get("verified_order_id")
        summary = f"Customer requested human support. Customer message: {text}. Handover requested for human review."
        ticket = create_ticket(summary=summary, order_id=verified_order_id, reason="customer_requested_human", intent="human_handover")
        if ticket.get("created") is True:
            return f"I've created a support handover ticket: **{ticket['ticket_id']}**. A human colleague can review your request."
        return "I couldn't create the handover ticket with the current details. Please try again or contact support."

    # If we are in an order verification flow, treat the next message as verification evidence.
    if st.session_state.get("pending_order_id"):
        if st.session_state.get("verification_attempts", 0) >= 2:
            st.session_state.pending_order_id = None
            st.session_state.verification_attempts = 0
            return "I can't share order details because verification attempts have been used. Please contact support from your registered phone number. I can still help with general policies."
        order_id = st.session_state.pending_order_id
        result = lookup_order(order_id, verification_value=text)
        st.session_state.verification_attempts += 1
        if result.get("found") and result.get("verified"):
            st.session_state.pending_order_id = None
            st.session_state.verification_attempts = 0
            st.session_state.verified_order_id = str(result.get("order_id", order_id))
            items = result.get("items", [])
            item_lines = []
            for item in items:
                name = item.get("product_name") or item.get("name") or item.get("sku") or "Item"
                qty = item.get("quantity", item.get("qty", ""))
                item_lines.append(f"- {name}" + (f" (quantity: {qty})" if qty != "" else ""))
            safe_status = result.get("status", "not available")
            total = result.get("total_inr")
            reply = f"Verification successful. Order **{st.session_state.verified_order_id}** status: **{safe_status}**."
            if item_lines:
                reply += "\n\nItems:\n" + "\n".join(item_lines)
            if total is not None:
                reply += f"\n\nOrder total: ₹{total}"
            reply += "\n\nWhat would you like help with next?"
            return reply
        if st.session_state.verification_attempts < 2:
            return "That verification value didn't match. You have one final attempt. Please provide either the registered phone number's last 4 digits or the delivery pincode. I won't reveal the expected value."
        st.session_state.pending_order_id = None
        st.session_state.verification_attempts = 0
        return "Verification failed again, so I can't share any order details. Please contact support from your registered phone number. I can still help with general policies."

    # Start an order lookup only when a plausible order ID is supplied.
    order_id = get_order_id(text)
    if order_id and is_order_request(text):
        st.session_state.pending_order_id = order_id
        st.session_state.verification_attempts = 0
        lookup_order(order_id)  # Deliberately returns no private order details.
        return f"I can check that order. For your privacy, please verify first: provide either the last 4 digits of the registered phone number or the delivery pincode for order {order_id}. Please provide only one value. I won't reveal the expected value."

    # General policy/document question: retrieve evidence and show section IDs.
    results = retrieve(text)
    if not results:
        return "I couldn't find a reliable answer in the current knowledge base. I don't want to guess. I can create a human-support handover if you'd like."

    lines = ["Here are the most relevant sections from the ZentroMart knowledge base:"]
    for result in results:
        lines.append(f"\n**[{result['section_id']}] {result['title']}** · `{result['file']}`")
        snippet = result["text"]
        if len(snippet) > 900:
            snippet = snippet[:900].rsplit(" ", 1)[0] + "…"
        lines.append(snippet)
    lines.append("\n*Please note: these are retrieved source excerpts, not a definitive answer if the sources conflict. Current policy documents take precedence over agent guides, help articles, FAQs and marketing pages. Superseded documents are excluded.*")
    return "\n\n".join(lines)


def gnani_transcribe(audio_bytes: bytes, language_code: str) -> str:
    """Transcribe one short recording using Gnani's REST STT endpoint."""
    api_key = get_gnani_api_key()
    if not api_key:
        raise RuntimeError("GNANI_API_KEY is missing from your .env file.")
    response = requests.post(
        "https://api.vachana.ai/stt/v3",
        headers={"X-API-Key-ID": api_key},
        data={"language_code": language_code, "format": "transcribe"},
        files={"audio_file": ("voice.wav", audio_bytes, "audio/wav")},
        timeout=60,
    )
    # Report Gnani's actual response body (never the API key) so setup/API
    # errors can be diagnosed rather than hidden behind a generic message.
    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.ok:
        detail = ""
        if isinstance(data, dict):
            err = data.get("error", data.get("detail", data.get("message", "")))
            detail = str(err)[:500] if err else ""
        else:
            detail = response.text[:300]
        raise RuntimeError(f"Gnani STT HTTP {response.status_code}. {detail or 'Check API access, audio format, and credits.'}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Gnani STT returned non-JSON output (HTTP {response.status_code}): {response.text[:250]}")
    transcript = data.get("transcript")

    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError(
            "Gnani response structure: "
            + json.dumps(data, ensure_ascii=False)[:1200]
        )

    return transcript.strip()

def gnani_speak(text: str, language_code: str) -> bytes:
    """Generate one WAV response using Gnani's REST TTS endpoint."""
    api_key = get_gnani_api_key()
    if not api_key:
        raise RuntimeError("GNANI_API_KEY is missing from your .env file.")
    # Keep voice output short and remove Markdown syntax before sending it to TTS.
    spoken = re.sub(r"`([^`]*)`", r"\1", text)
    spoken = re.sub(r"\*\*(.*?)\*\*", r"\1", spoken)
    spoken = re.sub(r"[*_#]", "", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    spoken = spoken[:700]
    response = requests.post(
        "https://api.vachana.ai/api/v1/tts/inference",
        headers={"X-API-Key-ID": api_key, "Content-Type": "application/json"},
        json={
            "text": spoken,
            "model": "timbre-v2.5",
            "voice": "Nalini",
            "language": language_code,
            "audio_config": {
                "encoding": "linear_pcm",
                "container": "wav",
                "num_channels": 1,
                "sample_rate": 22050,
                "sample_width": 2,
            },
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Gnani TTS returned HTTP {response.status_code}. Check API access and credits.")
    if "json" in response.headers.get("Content-Type", "").lower():
        raise RuntimeError("Gnani TTS returned an error instead of audio. Check API access and credits.")
    return response.content


if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_order_id" not in st.session_state:
    st.session_state.pending_order_id = None
if "verification_attempts" not in st.session_state:
    st.session_state.verification_attempts = 0
if "verified_order_id" not in st.session_state:
    st.session_state.verified_order_id = None
if "last_audio_hash" not in st.session_state:
    st.session_state.last_audio_hash = None

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


st.divider()
st.subheader("🎙️ Voice support (Gnani)")
st.caption("Record a short message (ideally under 30 seconds). Each new recording uses one STT request. Spoken replies are optional and use one additional TTS request.")
voice_col1, voice_col2 = st.columns([1, 1])
with voice_col1:
    voice_language_label = st.selectbox(
        "Spoken language",
        ["English (India)", "Hindi", "Hinglish"],
        key="voice_language",
    )
with voice_col2:
    speak_reply = st.checkbox(
        "Generate spoken reply (uses extra credits)",
        value=False,
        key="speak_reply",
    )

language_codes = {
    "English (India)": "en-IN",
    "Hindi": "hi-IN",
    "Hinglish": "hi-en",
}
recording = st.audio_input("Record your customer-support message", key="voice_recording")

if recording is not None:
    audio_bytes = recording.getvalue()
    recording_hash = hashlib.sha256(audio_bytes).hexdigest()
    if recording_hash != st.session_state.last_audio_hash:
        # Mark it before making the request so Streamlit reruns cannot charge twice.
        st.session_state.last_audio_hash = recording_hash
        if len(audio_bytes) > 15_000_000:
            st.error("This recording is too large. Please record a shorter message.")
        else:
            with st.spinner("Transcribing your recording with Gnani STT…"):
                try:
                    transcript = gnani_transcribe(audio_bytes, language_codes[voice_language_label])
                    st.success("Transcription complete")
                    st.markdown(f"**Recognized speech:** {transcript}")
                    add_message("user", transcript)
                    reply = respond(transcript)
                    add_message("assistant", reply)
                    st.markdown("**Support agent response**")
                    st.markdown(reply)

                    if speak_reply:
                        with st.spinner("Generating spoken response with Gnani TTS…"):
                            try:
                                audio_reply = gnani_speak(reply, language_codes[voice_language_label])
                                st.audio(audio_reply, format="audio/wav")
                            except Exception as exc:
                                st.warning(f"Text response is ready, but audio generation failed: {exc}")
                    st.info("This recording has been processed once. To submit another message, make a new recording.")
                except Exception as exc:
                    st.error(f"Voice processing failed: {exc}")
                    st.caption("Your existing text chat still works. Check that .env contains GNANI_API_KEY and that your Gnani key has STT access.")
    else:
        st.caption("This recording was already processed; it will not be submitted again on page reruns.")


prompt = st.chat_input("Ask about returns, refunds, delivery, or an order…")
if prompt:
    add_message("user", prompt)
    with st.chat_message("user"):
        st.markdown(prompt)
    reply = respond(prompt)
    add_message("assistant", reply)
    with st.chat_message("assistant"):
        st.markdown(reply)

with st.expander("Try these examples"):
    st.markdown(
        "- What is the return policy for electronics?\n"
        "- How are business days counted?\n"
        "- I want to check order 89782837\n"
        "- I need a human agent"
    )

st.caption("Capstone demo only. The supplied order tool uses synthetic data and is not production-grade authentication.")
