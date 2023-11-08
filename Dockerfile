FROM python:3.8-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /usr/src/app
COPY requirements.txt requirements.txt

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Run app.py when the container launches
COPY MARLlib/marllib/ marllib/.
CMD ["python", "./marllib/patch/add_patch.py", "-y"]

RUN pip install marllib
RUN pip install ma-gym

COPY test.py test.py