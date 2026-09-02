FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapter ./adapter
EXPOSE 8000
# Synchronous WSGI server; the adapter's TMS calls are blocking I/O.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "adapter.main:app"]
