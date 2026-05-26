# 默认走 DaoCloud 镜像，避免 Docker Hub 未登录 429 限流
# 可直连 Hub 时：docker build --build-arg BASE_IMAGE=python:3.8-slim .
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.8-slim
FROM ${BASE_IMAGE}

# .env 不打包进镜像，运行时由外部挂载到 /app/.env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/logs /app/data

EXPOSE 17686

CMD ["python", "app.py"]
