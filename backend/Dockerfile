FROM python:3.12-slim

# ffmpeg joins, re-encodes, masters and checks the narration audio. Without it
# the delivery falls back to byte-concatenation and says so in the job log.
# Tesseract with the Spanish model reads PDF pages that are scanned images;
# without it such pages are reported as missing content and block release.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8000

# Checked by Compose so the frontend only starts once the API answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

# --proxy-headers so the app sees the real client address through nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
