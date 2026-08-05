FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt .

RUN pip install \
    --no-cache-dir \
    -r requirements.txt

COPY main.py .
COPY history.py .

CMD ["python3", "main.py"]
