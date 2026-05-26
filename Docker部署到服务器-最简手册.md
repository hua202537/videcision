# Docker 部署到服务器（最简手册）

## 1. 准备

在服务器安装：

- Docker
- Docker Compose（`docker compose version` 可用即可）

将项目代码拷到服务器目录，例如 `/opt/decision-service`。

## 2. 配置环境变量

复制模板并按实际环境修改：

```bash
cp .env.example .env
```

重点改这几个：

- `HOST_PORT`：服务器对外暴露端口（默认 `17686`）
- `SOURCE_BROKER`：MQTT 地址（本机/内网 IP/域名）
- `SOURCE_PORT`
- `SOURCE_USERNAME`
- `SOURCE_PASSWORD`

## 3. 构建并启动

```bash
docker compose build --no-cache
docker compose up -d
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f
```

## 4. 验证服务

服务器本机验证：

```bash
curl http://127.0.0.1:${HOST_PORT:-17686}/api/health
curl http://127.0.0.1:${HOST_PORT:-17686}/api/status
```

外部机器验证：

```bash
curl http://<服务器IP>:<HOST_PORT>/api/health
curl http://<服务器IP>:<HOST_PORT>/api/status
```

## 5. 运维常用命令

重启：

```bash
docker compose restart
```

停止：

```bash
docker compose down
```

更新代码后重发版：

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

## 6. 防火墙与网络

确保服务器已放行 `HOST_PORT` 对应端口（默认 `17686`）。

如果 HTTP 正常但业务不触发，优先检查 `.env` 中 MQTT 配置是否可达。
