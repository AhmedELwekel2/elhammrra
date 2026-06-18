FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8030

# Run the API. Ingestion (doc_ingestion_pipeline.py) must be run once beforehand
# (or as a one-off) so the pgvector collection is populated.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8030"]
