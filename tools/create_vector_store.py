import os
import time
from pathlib import Path

from openai import OpenAI

DOCS_DIR = Path("DOCS")
ALLOWED_EXT = {".md", ".txt", ".pdf"}

REBUILD = True  # set False if you want only initial creation

def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in environment")

    client = OpenAI()

    existing_vs_id = os.getenv("OPENAI_VECTOR_STORE_ID")

    # ---------------------------------------------------
    # 1. Determine vector store
    # ---------------------------------------------------
    if existing_vs_id:
        vs_id = existing_vs_id
        print("Using existing vector store:", vs_id)

        if REBUILD:
            print("REBUILD MODE: clearing existing files...")
            files = client.vector_stores.files.list(vector_store_id=vs_id)
            for f in files.data:
                client.vector_stores.files.delete(
                    vector_store_id=vs_id,
                    file_id=f.id
                )
                print("Deleted file:", f.id)
    else:
        print("Creating new vector store...")
        vs = client.vector_stores.create(name="LYRA_OCTAVIAN_DOCS")
        vs_id = vs.id
        print("Created new VECTOR_STORE_ID:", vs_id)
        print("\nPut this into .env:")
        print("OPENAI_VECTOR_STORE_ID=" + vs_id)

    # ---------------------------------------------------
    # 2. Collect DOCS
    # ---------------------------------------------------
    docs_files = []
    for p in DOCS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT:
            docs_files.append(p)

    if not docs_files:
        raise SystemExit(f"No DOCS files found in {DOCS_DIR.resolve()}")

    # ---------------------------------------------------
    # 3. Upload & attach
    # ---------------------------------------------------
    for p in docs_files:
        with p.open("rb") as f:
            up = client.files.create(file=f, purpose="assistants")
        client.vector_stores.files.create(
            vector_store_id=vs_id,
            file_id=up.id
        )
        print("Added:", p.as_posix())

    # ---------------------------------------------------
    # 4. Poll until ready
    # ---------------------------------------------------
    while True:
        vs = client.vector_stores.retrieve(vs_id)
        status = vs.status
        counts = vs.file_counts
        print("Status:", status, "file_counts:", counts)

        if status == "completed":
            break
        if status == "expired":
            raise SystemExit("Vector store expired unexpectedly")

        time.sleep(5)

    print("\nDONE. Vector store ready.")
    print("Active VECTOR_STORE_ID:", vs_id)


if __name__ == "__main__":
    main()
