FROM python:3.13-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=18642
COPY bridge /app/bridge
COPY static /app/static
EXPOSE 18642
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18642/health', timeout=3)" || exit 1
CMD ["python", "-m", "bridge.server"]
