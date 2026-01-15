# 🛡️ PaySentry

### Real-Time Payment Intelligence & Compliance Platform

**PaySentry** is a production-grade fintech ecosystem capable of processing transactions at scale, detecting fraud in real-time (<100ms), and automating regulatory compliance.

The system evolves through three distinct phases: **Foundation (Core Payments)**, **Intelligence (ML Fraud Detection)**, and **Production (GenAI Compliance Agents)**.

---

## 🚀 Key Features

### ✅ Phase 1: Core Transaction Engine (Foundation)

* **High-Performance API:** Built on **FastAPI (ASGI)** for sub-millisecond response times.
* **Real-Time Ledger:** Atomic balance updates using **Redis Streams & Lua Scripts** to prevent race conditions.
* **Strict Financial Validation:** Uses Python’s `Decimal` and **Pydantic V2** for 100% type-safe financial data.
* **Merchant Portal:** React + Mantine UI dashboard for merchants to view balances and generate payment links.

### ✅ Phase 2: Fraud Intelligence (ML & Graph)

* **Hybrid Fraud Detection:** Ensemble model using **XGBoost** (Supervised) and **Isolation Forest** (Unsupervised).
* **Graph Network Analysis:** **NetworkX** integration to detect circular money laundering schemes and fraud rings.
* **Explainable AI (XAI):** Real-time **SHAP** value generation to explain *why* a transaction was blocked (Critical for RBI compliance).
* **Drift Monitoring:** **EvidentlyAI** integration to track model performance and data drift in production.

### ✅ Phase 3: Compliance Agent (GenAI)

* **ComplianceGPT:** A **LangGraph** autonomous agent that plans and executes compliance tasks.
* **RAG Architecture:** Vector search (**ChromaDB**) over RBI/NPCI Master Circulars for accurate regulatory answers.
* **Tool-Use Capabilities:** Agent can query SQL databases, analyze fraud graphs, and generate reports via natural language.

---

## 🚧 Development Roadmap

<<<<<<< HEAD
| Module | Status | Technology Stack |
|------|-------|------------------|
| Core Transaction Engine | 🟡 In Progress | FastAPI, Pydantic, Python 3.11+ |
| Persistence Layer | ⏳ Upcoming | PostgreSQL, SQLAlchemy (Async), Alembic |
| Real-Time Ledger | ⏳ Upcoming | Redis/Kafka (Atomic Updates), Webhooks |
| Fraud Detection Engine | ⏳ Upcoming | XGBoost, Isolation Forest, Scikit-Learn |
| Compliance Agent | ⏳ Upcoming | LangChain (RAG), LLMs, Vector DB |
| Analytics Dashboard | ⏳ Upcoming | Streamlit, Plotly |
=======
| Phase | Module | Status | Technology Stack |
| --- | --- | --- | --- |
| **1** | **Transaction Engine** | 🟡 In Progress | FastAPI, PostgreSQL (AsyncPG), Docker |
| **1** | **Real-Time Ledger** | ⏳ Upcoming | Redis Streams, Lua Scripts, Webhooks |
| **1** | **Merchant Portal** | ⏳ Upcoming | React, TypeScript, Mantine UI, TanStack Query |
| **2** | **Fraud Engine** | ⏳ Upcoming | XGBoost, Isolation Forest, Scikit-Learn |
| **2** | **Graph Brain** | ⏳ Upcoming | NetworkX (Louvain Algorithm), Polars |
| **2** | **Ops Dashboard** | ⏳ Upcoming | Streamlit, Plotly, EvidentlyAI |
| **3** | **Compliance Agent** | ⏳ Upcoming | LangGraph, OpenAI/Groq, RAGAS |
| **3** | **Knowledge Base** | ⏳ Upcoming | ChromaDB, HuggingFace Embeddings |
>>>>>>> d4a574ceb89cfaf13e4c18d26c1009ebe5b5cf5a

---

## 🛠️ Tech Stack

### Backend & Infrastructure

* **Language:** Python 3.11+
* **Framework:** FastAPI (ASGI)
* **Database:** PostgreSQL (Storage), Redis (State/Caching)
* **DevOps:** Docker, GitHub Actions, Railway/Vercel

### Machine Learning (Fraud)

* **Models:** XGBoost, Isolation Forest
* **Graph Analytics:** NetworkX
* **Data Processing:** Polars, Airflow
* **Explainability:** SHAP
* **Monitoring:** EvidentlyAI

### Generative AI (Compliance)

* **Orchestration:** LangChain, LangGraph
* **Vector DB:** ChromaDB / Qdrant
* **Evaluation:** RAGAS
* **LLM:** OpenAI GPT-4o / Groq (Llama 3)

---

## ⚡ Quick Start

### 1️⃣ Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/paysentry.git
cd paysentry

```

### 2️⃣ Set Up Environment

```bash
# Using uv (Recommended for speed)
uv venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt

```

### 3️⃣ Start Infrastructure

```bash
docker-compose up -d  # Starts PostgreSQL and Redis

```

### 4️⃣ Run the Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

```

### 5️⃣ Explore the Ecosystem

* **API Docs:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
* **Merchant Portal:** [http://127.0.0.1:3000](https://www.google.com/search?q=http://127.0.0.1:3000) (Requires React setup)
* **Fraud Dashboard:** [http://127.0.0.1:8501](https://www.google.com/search?q=http://127.0.0.1:8501) (Requires Streamlit setup)

---

### 📄 License

This project is licensed under the MIT License.
