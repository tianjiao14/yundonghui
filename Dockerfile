FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 1. 0体积配置上海时区与数据目录（合并为一层）
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    mkdir -p /app/data && chmod 777 /app/data

# 2. 先单独拷贝依赖文件，利用官方高速 CDN 直接安装预编译二进制包 (.whl)
# 只要不改 requirements.txt，后续改任何代码都会 100% 瞬间跳过此步骤！
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt || \
    pip install --no-cache-dir -r requirements.txt

# 3. 最后拷贝经常变动的业务代码与 HTML 模板（秒级完成）
COPY . .

EXPOSE 5000

# 4. 直接使用 app.py 内置的 gevent WSGI 服务器（或 gunicorn gevent 模式）
# 直接运行 python app.py 还能正常启动你在代码底部写的 auto_db_backup_task 自动热备份协程！
CMD ["python", "app.py"]