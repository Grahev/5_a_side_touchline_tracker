FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --home /app touchline \
 && mkdir -p /data && chown touchline /data
COPY app.py ./
COPY static ./static
ENV PORT=8080 DB_PATH=/data/touchline.db PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER touchline
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["python", "app.py"]
