FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY config.json ./
COPY static ./static
RUN mkdir -p room_data

EXPOSE 8000
CMD ["python", "server.py"]
