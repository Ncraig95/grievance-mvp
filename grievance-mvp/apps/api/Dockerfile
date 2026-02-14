FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    libreoffice \
    fonts-dejavu-core \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY grievance_api /app/grievance_api
COPY templates /app/templates

EXPOSE 8080

CMD ["uvicorn", "grievance_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
