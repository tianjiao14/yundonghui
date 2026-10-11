FROM python:3.11-slim

WORKDIR /app

# 设置时区与环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装编译所需的依赖（Gevent 依赖部分 C 扩展）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖配置并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 拷贝项目源代码
COPY . .

# 暴露端口
EXPOSE 5000

# 启动命令（单 Worker 保证内存单例广播无需额外 Redis 依赖）
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "1", "-b", "0.0.0.0:5000", "--threads", "1", "app:app"]