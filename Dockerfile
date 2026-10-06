FROM python:3.12-slim

WORKDIR /app

COPY main.py strategy.py ./

CMD ["python", "-u", "main.py"]
