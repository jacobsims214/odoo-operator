FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Create non-root user (use adduser for better compatibility)
RUN adduser --disabled-password --gecos "" --uid 1000 appuser
USER appuser

# Start the operator
CMD ["kopf", "run", "--standalone", "--liveness=http://0.0.0.0:8080/healthz", "src/main.py"]
