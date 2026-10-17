# OWcoach 지인 베타 서비스: 웹 서버 + 분석 작업자 1개 (service/README.md)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY analyzer/requirements.txt analyzer/requirements.txt
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r analyzer/requirements.txt -r service/requirements.txt
COPY analyzer analyzer
COPY service service
COPY docs/demo docs/demo
COPY docs/demo2 docs/demo2
ENV OWCOACH_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
WORKDIR /app/service
EXPOSE 8000
CMD ["uvicorn", "owcoach_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
