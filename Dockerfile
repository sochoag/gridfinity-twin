FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TWIN_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY geometry geometry
COPY vision vision
COPY server server
RUN useradd -m -u 1000 app && mkdir -p /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
