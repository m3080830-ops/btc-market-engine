FROM python:3.12-slim

WORKDIR /app

COPY main.py strategy.py mexc_futures_live.py ./

CMD ["python", "-u", "main.py"]
