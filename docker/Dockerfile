# Dockerfile
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app

# Install system dependencies needed to build packages like psycopg2
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir qdrant-client pypdf openai

# Copy the application code
COPY . .

# Expose the port your FastAPI app runs on
EXPOSE 4500

# Start Uvicorn pointing to your app entrypoint
CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "4500"]


