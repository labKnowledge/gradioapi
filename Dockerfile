FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ca-certificates \
    build-essential \
    software-properties-common \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y openssl libssl-dev

COPY . .
    
RUN pip install fastapi==0.104.1 uvicorn==0.24.0 gradio-client==0.7.0 pydantic==2.4.2 requests==2.31.0 websockets aiohttp


EXPOSE 8000
EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "300"]