FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 创建数据库目录并开放权限
RUN mkdir -p /app/data && chmod 777 /app/data
EXPOSE 5000
CMD ["python", "app.py"]