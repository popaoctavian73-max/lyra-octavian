# LYRA Octavian (single-server prototype)

Monolit FastAPI: login + chat UI + upload + ingest + RAG (FAISS).

## Recomandat
Python 3.11 sau 3.12 (evită 3.14).

## Instalare (Windows)
```bat
cd LYRA_OCTAVIAN
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
```

## Rulare
```bat
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Login default: admin / admin
