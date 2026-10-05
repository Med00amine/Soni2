FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY tests ./tests
COPY ejemplo_inicial ./ejemplo_inicial

CMD ["python", "-m", "pytest"]

