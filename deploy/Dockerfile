FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/culldrop
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY server server
COPY web web
ENV CULLDROP_DATA=/data CULLDROP_WEB=/opt/culldrop/web
VOLUME ["/data"]
EXPOSE 8000
WORKDIR /opt/culldrop/server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers"]
