FROM python:3.12-slim
WORKDIR /app
COPY app.py ./
COPY static ./static
ENV DB_PATH=/data/touchline.db PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["python", "app.py"]
