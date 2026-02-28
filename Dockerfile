FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps

COPY . /app

ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0

CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0
