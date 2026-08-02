FROM python:3.11-slim

# poppler + tesseract enable the OCR fallback for scanned filings
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN pip install --no-cache-dir -e .

ENV FINSCAN_WORK_DIR=/data
VOLUME ["/data"]
EXPOSE 8080

CMD ["uvicorn", "finscan.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
