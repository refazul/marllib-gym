FROM python:3.8-slim

WORKDIR /app

COPY requirements.txt requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY marllib/ marllib/
CMD ["python", "./marllib/patch/add_patch.py", "-y"]
RUN pip install marllib

COPY ma_gym/ ma_gym/
RUN pip install ma-gym