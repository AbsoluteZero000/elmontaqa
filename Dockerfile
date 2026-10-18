# Reconstructed from the observed Python 3.12 runtime; original Dockerfile unavailable.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html favicon.png ./
COPY static ./static
EXPOSE 8080
CMD ["python", "server.py"]
