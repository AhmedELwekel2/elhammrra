FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8020

# The Chroma store under data/processed must already be built (run the ingest +
# embed steps once, e.g. python -m src.ingest.azure_ocr && python -m src.embed.embed).
# Mount data/ as a volume in compose so the index persists.
CMD ["python", "-m", "uvicorn", "src.generate.api:app", "--host", "0.0.0.0", "--port", "8020"]
