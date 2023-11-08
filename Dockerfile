FROM python:3.8-slim

WORKDIR /app

COPY requirements.txt requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY marllib/ marllib/
CMD ["python", "./marllib/patch/add_patch.py", "-y"]

RUN pip install marllib
RUN pip install ma-gym