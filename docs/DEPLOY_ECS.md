# 部署到 ECS(云服务器 + Docker)

架构:GitHub Actions 测试→打镜像→推 **ACR** → SSH 进 **ECS** → `docker pull` + `docker run`。
(不用 ACK/K8s;要更高并发时,多台 ECS 挂 **阿里云 SLB 负载均衡**做水平扩展。)

## 一、ECS 一次性初始化(SSH 进服务器跑一次)
```bash
scp deploy/ecs-setup.sh root@<ECS公网IP>:/root/
ssh root@<ECS公网IP> "sudo bash /root/ecs-setup.sh"
# 然后编辑 /opt/assets/.env 填真密钥;并在 ECS【安全组】放行 80 端口
```

## 二、GitHub 仓库 Secrets(Settings → Secrets and variables → Actions)
| Secret | 值 |
|---|---|
| `ACR_USERNAME` | 阿里云账号邮箱(ACR 登录名) |
| `ACR_PASSWORD` | ACR 访问凭证固定密码 |
| `ECS_HOST` | ECS 公网 IP |
| `ECS_USER` | 登录用户,一般 `root` |
| `ECS_SSH_KEY` | 登录 ECS 的 **SSH 私钥整段**(见下) |

### 怎么拿 SSH 私钥
- 若你创建 ECS 时用的是**密钥对**:用那把私钥文件内容(`~/.ssh/xxx.pem` 整段)。
- 若用的是**密码登录**:把 CI 里 `key:` 换成 `password: ${{ secrets.ECS_SSH_PASSWORD }}`,加个 `ECS_SSH_PASSWORD` secret(我可帮你改)。

## 三、发布
`git push` 到 `main` → CI 自动:sensors 全绿 → build → 推 ACR → SSH 进 ECS 拉新镜像、重启 `assets-api` 容器。
访问:`http://<ECS公网IP>/`(前端)、`http://<ECS公网IP>/health`。

## 四、线上密钥怎么进容器
容器用 `--env-file /opt/assets/.env` 读密钥(在 ECS 上,不进仓库)。改密钥后重跑一次 CI 或手动 `docker restart assets-api`。

## 五、扩展到 500+ 并发
- 单台:容器 4 workers,升配 ECS(4vCPU/8G 起)通常够(重活 AI 已外包给百炼 API)。
- 水平:多台 ECS 跑同一镜像 → 挂 **阿里云 SLB**,把 80 端口流量分发到各 ECS。
