FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

COPY requirements.txt .

RUN pip install \
    --no-cache-dir \
    -r requirements.txt

COPY main.py .
COPY history.py .
COPY chart.py .

CMD ["python3", "main.py"]
