#!/bin/bash

# ========== 物料管理系统 Docker Compose 部署脚本 ==========
# 功能：通过 Docker Compose 重启 assets-api，支持源码备份与回滚保护
# 用法：
#   1) 上传项目 zip 包到部署根目录并命名为 assetsmangment.zip
#      （默认 ${ASSETS_ROOT}/assetsmangment.zip）
#   2) chmod +x restart.sh && ./restart.sh
# 说明：restart.sh / docker-compose.yml 手动上传到 ASSETS_ROOT，
#       zip 只包含源码（Dockerfile + app/ + frontend/）。
# 可用环境变量覆盖：
#   ASSETS_ROOT            部署根目录（默认 /software/project/python/assets）
# ========================================================

set -euo pipefail

# 脚本所在目录即 ASSETS_ROOT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 配置区域
export ASSETS_ROOT="${ASSETS_ROOT:-/software/project/python/assets}"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
PROJECT_NAME="assetsmangment"
# 上传 zip 落点（把新包放这里）
DROP_ZIP_PATH="${ASSETS_ROOT}/assetsmangment.zip"
# 实际运行源码目录（compose build context 就是它）
RUN_APP_DIR="${ASSETS_ROOT}/${PROJECT_NAME}"
# 备份目录
BACKUP_DIR="${ASSETS_ROOT}/backups"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "❌ 未找到 Docker Compose 文件: $COMPOSE_FILE"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

backup_run_app() {
    if [ -d "$RUN_APP_DIR" ]; then
        local timestamp
        timestamp=$(date +"%Y%m%d%H%M%S")
        tar -czf "${BACKUP_DIR}/${PROJECT_NAME}_${timestamp}.tar.gz" -C "$RUN_APP_DIR" . 2>/dev/null || true
        echo "→ 备份旧源码为: ${BACKUP_DIR}/${PROJECT_NAME}_${timestamp}.tar.gz"
    fi
}

echo "=========================================="
echo "停止 ${PROJECT_NAME} 服务..."
echo "=========================================="

if docker compose -f "$COMPOSE_FILE" ps | grep -q "Up"; then
    docker compose -f "$COMPOSE_FILE" down --remove-orphans
    echo "→ 删除旧备份..."
    find "$BACKUP_DIR" -name "${PROJECT_NAME}_*.tar.gz" -exec rm -f {} +
    backup_run_app
else
    echo "→ 服务未启动"
fi

echo "=========================================="
echo "部署 ${PROJECT_NAME} 服务..."
echo "=========================================="

if [ -f "$DROP_ZIP_PATH" ]; then
    if [ -d "$RUN_APP_DIR" ]; then
        echo "→ 删除旧备份..."
        find "$BACKUP_DIR" -name "${PROJECT_NAME}_*.tar.gz" -exec rm -f {} +
        backup_run_app
        echo "→ 清理旧源码..."
        rm -rf "$RUN_APP_DIR"
    fi
    echo "→ 解压新包到 ${RUN_APP_DIR}..."
    mkdir -p "$RUN_APP_DIR"
    unzip -q -o "$DROP_ZIP_PATH" -d "$RUN_APP_DIR" 2>/dev/null || true
    echo "→ 删除上传包: ${DROP_ZIP_PATH}"
    rm -f "$DROP_ZIP_PATH"
elif [ ! -d "$RUN_APP_DIR" ]; then
    echo "❌ 未找到可部署的源码。请将新包放到: ${DROP_ZIP_PATH}"
    echo "   或确保运行目录已存在: ${RUN_APP_DIR}"
    exit 1
else
    echo "→ 未上传新包，使用已有源码: ${RUN_APP_DIR}"
fi

if [ ! -f "${RUN_APP_DIR}/Dockerfile" ]; then
    echo "❌ 未找到 Dockerfile: ${RUN_APP_DIR}/Dockerfile"
    exit 1
fi

echo "→ 运行目录已就绪: $(ls -ld "$RUN_APP_DIR")"

mkdir -p "${ASSETS_ROOT}/data"
mkdir -p "${ASSETS_ROOT}/logs/${PROJECT_NAME}"

echo "→ 构建镜像..."
docker compose -f "$COMPOSE_FILE" build

echo "→ 启动服务..."
docker compose -f "$COMPOSE_FILE" up -d

echo "→ 服务启动中，请稍候..."
sleep 5
docker compose -f "$COMPOSE_FILE" ps
