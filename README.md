# 🛡️ PaySentry Core  
### Real-Time Payment Intelligence & Processing Platform

![Status](https://img.shields.io/badge/status-active%20development-yellow)
![License](https://img.shields.io/badge/license-MIT-blue)

**PaySentry Core** is a high-performance fintech backend designed to process transactions at scale while delivering **real-time fraud detection** and **compliance intelligence**.

Currently in the **API Development Phase**, the system simulates modern banking architecture with:
- **Sub-millisecond response times**
- **Strict financial data validation**
- **An extensible foundation for future ML integration**

---

## 🚀 Key Features

### ✅ Currently Implemented (Foundation Layer)

- **High-Performance API**  
  Built on **FastAPI (ASGI)**, optimized for high concurrency and low latency.

- **Financial Precision**  
  Uses Python’s `Decimal` type and **strictly typed Pydantic V2 models** to prevent floating-point errors and ensure data integrity.

- **Type Safety**  
  100% type-hinted codebase for reliability and auto-generated documentation.

- **Interactive Documentation**  
  Automatic **Swagger UI** available at `/docs` for instant API testing.

- **Professional Workflow**  
  Feature-branch Git workflow with **Conventional Commits**.

---

## 🚧 Roadmap & Upcoming Modules

| Module | Status | Technology Stack |
|------|-------|------------------|
| Core Transaction Engine | 🟡 In Progress | FastAPI, Pydantic, Python 3.11+ |
| Persistence Layer | ⏳ Upcoming | PostgreSQL, SQLAlchemy (Async), Alembic |
| Real-Time Ledger | ⏳ Upcoming | Redis/Kafka (Atomic Updates), Webhooks |
| Fraud Detection Engine | ⏳ Upcoming | XGBoost, Isolation Forest, Scikit-Learn |
| Compliance Agent | ⏳ Upcoming | LangChain (RAG), LLMs, Vector DB |
| Analytics Dashboard | ⏳ Upcoming | Streamlit, Plotly |

---

## 🛠️ Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI
- **Server:** Uvicorn (ASGI)
- **Validation:** Pydantic V2
- **Environment:** Docker, WSL2
- **Package Manager:** `uv` (Fast Python package installer)

---

## ⚡ Quick Start

### 1️⃣ Clone the Repository
```bash
git clone https://github.com/YOUR_USERNAME/paysentry-core.git
cd paysentry-core
```

### 2️⃣ Set Up Environment
```bash
# Using uv (Recommended)
uv venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install fastapi uvicorn pydantic[email]
```

### 3️⃣ Run the Server
```bash
uvicorn app.main:app --reload # Start the FastAPI development server with hot reload
```

### 4️⃣ Explore the API
Open your browser and navigate to: http://127.0.0.1:8000/docs

This launches the interactive Swagger UI for testing and exploration.

---

### 📄 License
This project is licensed under the MIT License.