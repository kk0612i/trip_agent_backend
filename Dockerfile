FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    # 信任 Nginx 转发的 X-Forwarded-* 头，日志中才能拿到真实客户端 IP
    FORWARDED_ALLOW_IPS="*"

WORKDIR /app

# 先只装依赖，代码变动时可复用这一层缓存
COPY pyproject.toml uv.lock .python-version ./
# 本地 uv.lock 锁定的是清华镜像，海外/香港服务器访问会超时；构建时改回官方 PyPI（文件哈希相同）
RUN sed -i \
      -e 's#https://pypi.tuna.tsinghua.edu.cn/simple#https://pypi.org/simple#g' \
      -e 's#https://pypi.tuna.tsinghua.edu.cn/packages#https://files.pythonhosted.org/packages#g' \
      uv.lock \
 && uv sync --locked --no-dev --no-install-project

COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
