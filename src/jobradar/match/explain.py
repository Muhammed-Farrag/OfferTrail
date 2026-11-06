"""Grounded match explanation: strengths, missing requirements and unknowns, each tied to IDs.

Requirements come from the stored job description (or only the title when no description is
available — then requirements are reported as unknown). A requirement counts as a strength
only if an *approved* candidate fact mentions it; the cited chunk ID lets the user verify it.
"""

from __future__ import annotations

import re
import sqlite3

from jobradar.rag.embed import Embedder
from jobradar.rag.store import search

SKILLS: dict[str, str] = {
    # canonical name -> regex
    "Python": r"python",
    "PyTorch": r"pytorch|torch",
    "TensorFlow": r"tensorflow|keras",
    "scikit-learn": r"scikit[- ]learn|sklearn",
    "Hugging Face Transformers": r"hugging\s?face|transformers",
    "LLMs": r"llms?|large language models?|gpt|generative ai|genai",
    "RAG": r"rag|retrieval[- ]augmented",
    "LangChain": r"langchain",
    "LlamaIndex": r"llama[- ]?index",
    "Vector databases": r"vector (?:db|database|store)s?|faiss|pinecone|chroma|qdrant|weaviate|pgvector",
    "Prompt engineering": r"prompt engineering|prompting",
    "Fine-tuning": r"fine[- ]tun(?:e|ing)|lora|peft",
    "NLP": r"nlp|natural language processing",
    "Computer vision": r"computer vision|opencv|image (?:classification|segmentation)|yolo",
    "Deep learning": r"deep learning|neural networks?|cnn|rnn",
    "Machine learning": r"machine learning|\bml\b",
    "MLOps": r"mlops|ml ops|model deployment|mlflow|kubeflow",
    "Docker": r"docker|containers?",
    "Kubernetes": r"kubernetes|k8s",
    "AWS": r"aws|amazon web services|sagemaker",
    "GCP": r"gcp|google cloud|vertex ai",
    "Azure": r"azure",
    "SQL": r"sql|postgres(?:ql)?|mysql",
    "FastAPI": r"fastapi",
    "Flask": r"flask",
    "pandas / NumPy": r"pandas|numpy",
    "Spark": r"spark|pyspark",
    "Git": r"git|github|gitlab",
    "Statistics": r"statistics|statistical",
    "Arabic": r"arabic",
    "English": r"english",
}
_SKILL_RX = {name: re.compile(rf"(?<![a-z0-9])(?:{rx})(?![a-z0-9])", re.I)
             for name, rx in SKILLS.items()}


def skills_in(text: str | None) -> list[str]:
    if not text:
        return []
    return [name for name, rx in _SKILL_RX.items() if rx.search(text)]


def _approved_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, section, text FROM candidate_facts WHERE approved = 1"
                        ).fetchall()


def explain(conn: sqlite3.Connection, embedder: Embedder | None, job: sqlite3.Row,
            description: str | None, job_id: int) -> dict:
    facts = _approved_facts(conn)
    unknowns: list[str] = []
    if not facts:
        return {"strengths": [], "missing": [], "related": [], "job_evidence": [],
                "unknowns": ["no approved candidate facts yet — run onboarding (/profile)"],
                "requirements_source": "none"}

    if description:
        requirements = skills_in(description)
        source = "description"
        if not requirements:
            unknowns.append("no recognizable skill requirements in the description")
    else:
        requirements = skills_in(job["title"])
        source = "title_only"
        unknowns.append("no job description stored — requirements unknown (Paste description)")

    strengths, missing = [], []
    for req in requirements:
        rx = _SKILL_RX[req]
        hit = next((f for f in facts if rx.search(f["text"])), None)
        if hit:
            strengths.append({"requirement": req, "chunk_id": f"cand:f{hit['id']}",
                              "text": hit["text"]})
        else:
            missing.append(req)

    related, job_evidence = [], []
    if embedder is not None:
        query = description or job["title"] or ""
        related = [{"chunk_id": h.chunk_id, "score": round(h.score, 3), "text": h.text}
                   for h in search(conn, embedder, "candidate", query, k=3,
                                       min_score=embedder.min_relevance)]
        if description:
            req_query = " ".join(requirements) or (job["title"] or "")
            job_evidence = [{"chunk_id": h.chunk_id, "text": h.text}
                            for h in search(conn, embedder, "job", req_query, k=2, job_id=job_id)]

    return {"strengths": strengths, "missing": missing, "related": related,
            "job_evidence": job_evidence, "unknowns": unknowns, "requirements_source": source}
