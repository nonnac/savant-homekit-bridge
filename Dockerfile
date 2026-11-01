FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

COPY bridge ./bridge
COPY tools ./tools

VOLUME /data
# 8099 web UI, 8091 Savant profiles (TCP), 51827 Apple Home (HAP)
EXPOSE 8099 8091 51827
CMD ["python", "-m", "bridge"]
