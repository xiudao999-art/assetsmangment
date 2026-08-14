#!/bin/bash

# ========== 物料管理系统 Docker Compose 部署脚本 ==========
# 功能：构建期间保持旧服务运行，构建成功后替换 assets-api；
#       新容器健康检查失败时自动回滚到上一镜像。
# 用法：
#   1) 上传项目 zip 包到部署根目录并命名为 assetsmangment.zip
#      （默认 ${ASSETS_ROOT}/assetsmangment.zip）
#   2) chmod +x restart.sh && ./restart.sh
# 说明：restart.sh / docker-compose.yml 手动上传到 ASSETS_ROOT，
#       zip 只包含源码（Dockerfile + app/ + frontend/）。
# 可用环境变量覆盖：
#   ASSETS_ROOT             部署根目录（默认 /software/project/python/assets）
#   HEALTH_RETRIES         健康检查次数（默认 30）
#   HEALTH_INTERVAL_SECONDS 每次健康检查间隔秒数（默认 2）
# ========================================================

set -Eeuo pipefail

# 脚本所在目录用于定位 Compose 文件；应用目录仍由 ASSETS_ROOT 决定。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 配置区域
export ASSETS_ROOT="${ASSETS_ROOT:-/software/project/python/assets}"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
PROJECT_NAME="assetsmangment"
SERVICE_NAME="assets-api"
CONTAINER_NAME="assets-api"
IMAGE_NAME="assets-api:latest"
ROLLBACK_IMAGE="assets-api:rollback"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-2}"

# 上传 zip 落点（把新包放这里）
DROP_ZIP_PATH="${ASSETS_ROOT}/assetsmangment.zip"
# 实际运行源码目录（compose build context 就是它）
RUN_APP_DIR="${ASSETS_ROOT}/${PROJECT_NAME}"
# 备份目录
BACKUP_DIR="${ASSETS_ROOT}/backups"
DEPLOYED_FROM_ZIP=false
SOURCE_BACKUP_PATH=""

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "❌ 未找到 Docker Compose 文件: $COMPOSE_FILE"
    exit 1
fi

for command_name in docker unzip tar; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "❌ 缺少部署命令: $command_name"
        exit 1
    fi
done

COMPOSE=(docker compose -f "$COMPOSE_FILE")
mkdir -p "$BACKUP_DIR"

backup_run_app() {
    if [ -d "$RUN_APP_DIR" ]; then
        local timestamp backup_path
        timestamp=$(date +"%Y%m%d%H%M%S")
        backup_path="${BACKUP_DIR}/${PROJECT_NAME}_${timestamp}.tar.gz"
        tar -czf "$backup_path" -C "$RUN_APP_DIR" .
        SOURCE_BACKUP_PATH="$backup_path"
        echo "→ 备份旧源码为: $backup_path"
    fi
}

restore_source() {
    if [ "$DEPLOYED_FROM_ZIP" != true ]; then
        return 0
    fi
    if [ -z "$SOURCE_BACKUP_PATH" ] || [ ! -f "$SOURCE_BACKUP_PATH" ]; then
        echo "⚠️ 没有可恢复的旧源码备份"
        return 1
    fi

    echo "→ 恢复旧源码: $SOURCE_BACKUP_PATH"
    rm -rf -- "$RUN_APP_DIR"
    mkdir -p "$RUN_APP_DIR"
    tar -xzf "$SOURCE_BACKUP_PATH" -C "$RUN_APP_DIR"
}

wait_for_health() {
    local attempt
    for ((attempt = 1; attempt <= HEALTH_RETRIES; attempt++)); do
        if docker exec "$CONTAINER_NAME" python -c \
            'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()' \
            >/dev/null 2>&1; then
            echo "✅ 容器内健康检查通过: /health"
            return 0
        fi
        if ! docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q '^true$'; then
            echo "⚠️ 容器未处于运行状态"
            return 1
        fi
        echo "→ 等待服务就绪 (${attempt}/${HEALTH_RETRIES})..."
        sleep "$HEALTH_INTERVAL_SECONDS"
    done
    echo "⚠️ 容器内健康检查超时: /health"
    return 1
}

rollback_service() {
    if [ -z "${OLD_IMAGE_ID:-}" ]; then
        echo "❌ 没有可回滚的旧镜像"
        return 1
    fi

    echo "→ 正在回滚到旧镜像: $OLD_IMAGE_ID"
    docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"
    if ! "${COMPOSE[@]}" up -d --force-recreate "$SERVICE_NAME"; then
        echo "❌ 旧镜像容器恢复失败"
        return 1
    fi
    if wait_for_health; then
        echo "✅ 已恢复旧版本服务"
        return 0
    fi
    echo "❌ 旧版本服务健康检查仍未通过"
    return 1
}

echo "=========================================="
echo "准备 ${PROJECT_NAME} 新版本源码..."
echo "=========================================="

if [ -f "$DROP_ZIP_PATH" ]; then
    echo "→ 校验上传包: $DROP_ZIP_PATH"
    unzip -tq "$DROP_ZIP_PATH" >/dev/null
    DEPLOYED_FROM_ZIP=true

    if [ -d "$RUN_APP_DIR" ]; then
        backup_run_app
        echo "→ 清理旧源码..."
        rm -rf -- "$RUN_APP_DIR"
    fi
    echo "→ 解压新包到 ${RUN_APP_DIR}..."
    mkdir -p "$RUN_APP_DIR"
    if ! unzip -q -o "$DROP_ZIP_PATH" -d "$RUN_APP_DIR"; then
        echo "❌ 新源码解压失败"
        restore_source || true
        exit 1
    fi
elif [ ! -d "$RUN_APP_DIR" ]; then
    echo "❌ 未找到可部署的源码。请将新包放到: ${DROP_ZIP_PATH}"
    echo "   或确保运行目录已存在: ${RUN_APP_DIR}"
    exit 1
else
    echo "→ 未上传新包，使用已有源码: ${RUN_APP_DIR}"
fi

if [ ! -f "${RUN_APP_DIR}/Dockerfile" ]; then
    echo "❌ 未找到 Dockerfile: ${RUN_APP_DIR}/Dockerfile"
    restore_source || true
    exit 1
fi

echo "→ 运行目录已就绪: $(ls -ld "$RUN_APP_DIR")"

mkdir -p "${ASSETS_ROOT}/data"
mkdir -p "${ASSETS_ROOT}/logs/${PROJECT_NAME}"

# 记录当前运行容器的镜像。重新构建只会移动 latest 标签，不会影响
# 已经启动的旧容器，因此依赖下载和镜像构建期间服务仍可访问。
OLD_CONTAINER_ID="$("${COMPOSE[@]}" ps -q "$SERVICE_NAME" 2>/dev/null || true)"
OLD_IMAGE_ID=""
if [ -n "$OLD_CONTAINER_ID" ] && \
   docker inspect -f '{{.State.Running}}' "$OLD_CONTAINER_ID" 2>/dev/null | grep -q '^true$'; then
    OLD_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$OLD_CONTAINER_ID")"
    docker image tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"
    echo "→ 原服务保持运行，旧镜像已保留为: $ROLLBACK_IMAGE"
else
    echo "→ 当前没有运行中的旧服务，本次部署无法自动回滚"
fi

echo "=========================================="
echo "构建 ${PROJECT_NAME} 新镜像（原服务继续运行）..."
echo "=========================================="

if ! "${COMPOSE[@]}" build "$SERVICE_NAME"; then
    echo "❌ 新镜像构建失败，原服务未停止"
    restore_source || true
    exit 1
fi

NEW_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE_NAME")"
echo "✅ 新镜像构建完成: $NEW_IMAGE_ID"

echo "=========================================="
echo "替换 ${PROJECT_NAME} 服务（此处会有短暂中断）..."
echo "=========================================="

if ! "${COMPOSE[@]}" up -d --force-recreate "$SERVICE_NAME"; then
    echo "❌ 新容器创建失败"
    rollback_service || true
    restore_source || true
    exit 1
fi

if ! wait_for_health; then
    echo "❌ 新版本健康检查失败，输出最近日志："
    "${COMPOSE[@]}" logs --tail=200 "$SERVICE_NAME" || true
    rollback_service || true
    restore_source || true
    exit 1
fi

if [ "$DEPLOYED_FROM_ZIP" = true ]; then
    echo "→ 删除已成功部署的上传包: $DROP_ZIP_PATH"
    rm -f -- "$DROP_ZIP_PATH"
fi

echo "=========================================="
echo "✅ ${PROJECT_NAME} 部署成功"
echo "=========================================="
"${COMPOSE[@]}" ps "$SERVICE_NAME"
