# =========================
# Stage 1: 编译阶段 (构建轮子)
# =========================
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 替换软件源并安装编译工具
RUN (sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
     sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list) && \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 将依赖全部编译安装到 /install 目录下
RUN pip install --no-cache-dir --prefix=/install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt


# =========================
# Stage 2: 最终运行镜像 (极简轻量)
# =========================
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 使用软链接配置时区（0 体积，不装 tzdata）
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 从编译阶段仅拷贝安装好的 Python 依赖库
COPY --from=builder /install /usr/local

# 拷贝业务代码
COPY . .

# 创建持久化数据目录
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 5000

# 与 app.py 中的 eventlet 保持一致，单 Worker 保证 SQLite 与内存全局锁安全
CMD ["gunicorn", "-k", "eventlet", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]