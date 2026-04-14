# openGauss 部署指南

本文档介绍如何使用 openGauss 7.0+ 作为 ContextHub 的存储后端。

## 前置条件

- Docker 已安装
- openGauss 7.0+ (内置 DataVec 向量能力)

## 1. 启动 openGauss 容器

```bash
# 使用项目提供的 compose 文件
docker compose -f docker-compose.opengauss.yml up -d

# 或者手动启动
docker pull opengauss/opengauss-server:latest
docker run --name opengauss --privileged=true -d \
  -e GS_PASSWORD=Huawei@123 \
  -p 15432:5432 \
  opengauss/opengauss-server:latest
```

## 2. 初始化数据库

```bash
docker exec -it opengauss bash
su omm
gsql -d postgres -p 5432
```

在 gsql 中执行：

```sql
CREATE USER contexthub WITH PASSWORD 'ContextHub@123' SYSADMIN;
CREATE DATABASE contexthub OWNER contexthub DBCOMPATIBILITY = 'PG';
```

> **注意：**
> - openGauss 7.0+ 内置 DataVec，无需创建 vector 扩展
> - 使用自定义`uuid_generate_v4()` (`alembic/versions/001_initial_schema.py`)，替代 PostgreSQL 的 `pgcrypto` + `gen_random_uuid()`
> - openGauss 密码有强度约束，须包含大小写字母、数字和特殊字符
> - 使用 `SYSADMIN` 而非 `SUPERUSER` 关键字
> - 使用DBCOMPATIBILITY = 'PG'模式，保证空字符串/NULL值处理等与postgres统一。

## 3. 配置 ContextHub

编辑 `.env` 文件：

```env
DATABASE_URL=postgresql://contexthub:ContextHub%40123@<host>:15432/contexthub
DB_BACKEND=opengauss
```

其中 `<host>` 替换为 openGauss 服务器的实际 IP 地址。

## 4. 运行数据库迁移

```bash
DB_BACKEND=opengauss alembic upgrade head
```

> 迁移脚本会根据 `DB_BACKEND` 环境变量自动选择：
> - `opengauss`: 使用 `uuid_generate_v4()` 作为 UUID 默认值
> - `postgres` (默认): 创建 `vector` + `pgcrypto` 扩展，使用 `gen_random_uuid()`

## 5. 启动服务

```bash
DB_BACKEND=opengauss uvicorn contexthub.main:app --host 0.0.0.0 --port 8000
```

## 6. 安装 Python 依赖说明

使用 openGauss 后端时，不需要安装 `pgvector` Python 包：

```bash
# openGauss 后端
pip install .

# PostgreSQL + pgvector 后端
pip install ".[postgres]"
```

## 与 PostgreSQL 后端的差异

| 特性 | PostgreSQL 16 | openGauss 7.0+ |
|------|--------------|----------------|
| 向量扩展 | pgvector (需安装) | DataVec (内置) |
| UUID 函数 | `gen_random_uuid()` (pgcrypto) | `uuid_generate_v4()` (自定义) |
| 向量类型 `vector(N)` | 兼容 | 兼容 |
| 向量距离 `<=>` | 兼容 | 兼容 |
| HNSW 索引 | 兼容 | 兼容 |
| RLS | 兼容 | 兼容 |
| `pg_notify`/`LISTEN` | 兼容 | 不兼容 |
| asyncpg 驱动 | 兼容 | 部分兼容 |
| 连接 URL 格式 | `postgresql://` | `postgresql://` |
