FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# WeasyPrint (Pango/Harfbuzz), OpenCV runtime libs, fonts for PDF rendering, Ghostscript for PDF/A.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libffi8 libgl1 libglib2.0-0 \
      fonts-dejavu-core ghostscript curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install --with-deps chromium

COPY . /app/backend
COPY --from=scripts . /app/scripts
RUN useradd -m -u 10001 tendersync && mkdir -p /data/storage && chown -R tendersync /data /app
USER tendersync
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
