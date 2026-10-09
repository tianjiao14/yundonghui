FROM python:3.11-slim

WORKDIR /app

ENV TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# 1. 设置时区与阿里云源
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    (sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
     sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list)

# 2. 安装编译依赖 -> pip 安装 -> 立即卸载编译工具并清理缓存（合并在单层中完成）
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && pip install --no-cache-dir --default-timeout=100 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libffi-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# 3. 复制代码
COPY . .

RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 5000

CMD ["python", "app.py"]