FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ARG INSTALL_DEV_DEPS=0

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    libreoffice \
    poppler-utils \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-liberation2 \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create venv so we never pip-install into system python (PEP 668 safe)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    if [ "$INSTALL_DEV_DEPS" = "1" ]; then pip install --no-cache-dir -r /app/requirements-dev.txt; fi

COPY grievance_api /app/grievance_api
COPY templates /app/templates

EXPOSE 8080

CMD ["uvicorn", "grievance_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
