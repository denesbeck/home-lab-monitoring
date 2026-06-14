FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitoring_tools.py prompts.py webhook_app.py ./

# Run as an unprivileged user (only needs to read two mounted files + outbound HTTP).
RUN useradd -r -u 10001 appuser
USER appuser

EXPOSE 8080
CMD ["uvicorn", "webhook_app:app", "--host", "0.0.0.0", "--port", "8080"]
