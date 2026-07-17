#!/usr/bin/env bash
# 在 ECS 上【只跑一次】:装 Docker + 准备线上 .env。
# 用法(SSH 进 ECS 后):sudo bash ecs-setup.sh
set -e

# 1) 装 Docker(优先用 Alibaba Cloud Linux 的包管理器,回退官方脚本)
if ! command -v docker >/dev/null 2>&1; then
  echo "安装 Docker…"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y docker || curl -fsSL https://get.docker.com | bash
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker || curl -fsSL https://get.docker.com | bash
  else
    curl -fsSL https://get.docker.com | bash
  fi
  systemctl enable --now docke
fi
docker --version

# 2) 线上密钥目录(容器 --env-file 读取,绝不进代码仓库)
mkdir -p /opt/assets
if [ ! -f /opt/assets/.env ]; then
  cat > /opt/assets/.env <<'EOF'
# 线上密钥:填真值。OSS 已可用;其余接真后填。
AM_OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com
AM_OSS_BUCKET=assets009
AM_OSS_ACCESS_KEY_ID=
AM_OSS_ACCESS_KEY_SECRET=
AM_DASHSCOPE_API_KEY=
AM_CONTENT_SAFETY_ACCESS_KEY_ID=
AM_CONTENT_SAFETY_ACCESS_KEY_SECRET=
AM_DATABASE_URL=
EOF
  echo "已创建 /opt/assets/.env 模板 —— 请编辑填入真密钥。"
fi

echo "✅ ECS 就绪。还需:①编辑 /opt/assets/.env 填密钥 ②在 ECS 安全组放行 80 端口(和 443 若上 https)。"
