# MARL-lib and MA-gym

1) Make sure you have docker installed. Then run

```
docker compose up -d
```
2) Find your container ID

```
docker ps
```
3) Get into the container

```
docker exec -it YOUR_CONTAINER_ID bash
```
4) Run test script

```
python test.py
```
