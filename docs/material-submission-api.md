# 素材提报接口文档

> 用于外部系统对接，创建素材提报记录。基础地址：`http://8.149.247.100:8088`

## 1. 登录获取 Token

**先调用此接口获取认证令牌，后续所有接口都要带。**

```
POST /users/login
Content-Type: application/json

{
  "name": "admin",
  "password": "admin123"
}
```

**成功响应**（200）：

```json
{
  "token": "eyJ...",
  "user": {
    "id": "...",
    "name": "admin",
    "role": "admin"
  }
}
```

之后的请求在 Header 中携带：`Authorization: Bearer <token>`

---

## 2. 上传视频文件

```
POST /admin/uploads/file
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | File | 是 | 视频文件 |
| `scope` | String | 否 | 上传目录，默认 `uploads`，建议传 `submissions` |

**成功响应**（200）：

```json
{
  "oss_key": "submissions/abc123-我的视频.mp4",
  "file_name": "我的视频.mp4"
}
```

---

## 3. 创建素材提报

```
POST /admin/material-submissions
Authorization: Bearer <token>
Content-Type: application/json
```

**请求体**（全部字段均为可选，不传即为空）：

```json
{
  "team_name": "一组团队",
  "delivery_time": "2026-08-01 12:00",
  "drama_name": "短剧名称",
  "oss_key": "submissions/abc123-我的视频.mp4",
  "video_file_name": "傅总，你家灵宝会仙法 1-3集",
  "title_name": "标题A",
  "episode_range": "1-10"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `team_name` | String | 否 | 团队名称 |
| `delivery_time` | String | 否 | 视频交付时间，格式自由（如 `2026-08-01 12:00`） |
| `drama_name` | String | 否 | 剧名 |
| `oss_key` | String | 否 | 视频文件 OSS Key，来自上传接口返回 |
| `video_file_name` | String | 否 | 视频文件名称 |
| `title_name` | String | 否 | 标题名 |
| `episode_range` | String | 否 | 集数区间（如 `1-10`） |

**成功响应**（200）：

```json
{
  "id": "1234567890123456789",
  "team_name": "一组团队",
  "delivery_time": "2026-08-01 12:00",
  "drama_name": "短剧名称",
  "oss_key": "submissions/abc123-我的视频.mp4",
  "video_file_name": "傅总，你家灵宝会仙法 1-3集",
  "title_name": "标题A",
  "episode_range": "1-10",
  "revision_comment": "",
  "can_upload_status": null,
  "upload_account_name": "",
  "upload_date": "",
  "publish_status": null,
  "platform_reject_reason": "",
  "platform_reject_attachments": []
}
```

| 返回字段 | 类型 | 说明 |
|----------|------|------|
| `id` | String | 提报 ID（雪花算法生成） |
| `team_name` | String | 团队名称 |
| `delivery_time` | String | 视频交付时间 |
| `drama_name` | String | 剧名 |
| `oss_key` | String | 视频文件 OSS Key |
| `video_file_name` | String | 视频文件名称 |
| `title_name` | String | 标题名 |
| `episode_range` | String | 集数区间 |
| `revision_comment` | String | 修改意见（管理端填写） |
| `can_upload_status` | Int\|Null | 可上传状态：`1`=可上传，`2`=不可上传，`null`=未设置 |
| `upload_account_name` | String | 上传账号名称（管理端填写） |
| `upload_date` | String | 上传日期（`YYYY-MM-DD`） |
| `publish_status` | Int\|Null | 发布状态：`1`=成功，`2`=失败，`null`=未设置 |
| `platform_reject_reason` | String | 平台拒审理由（管理端填写） |
| `platform_reject_attachments` | [String] | 拒审附件 OSS Key 列表（管理端填写） |

---

## 完整对接流程

```
① POST /users/login          → 拿到 token
② POST /admin/uploads/file   → 上传视频，拿到 oss_key
③ POST /admin/material-submissions → 创建提报，传入 oss_key
```

## 错误码

| 状态码 | 说明 |
|--------|------|
| 401 | 未登录或 Token 过期 |
| 403 | 无权限（需 admin 角色） |
| 400 | 参数校验失败 |
| 500 | 服务器内部错误 |

## curl 示例

```bash
# 1. 登录
TOKEN=$(curl -s -X POST http://8.149.247.100:8088/users/login \
  -H "Content-Type: application/json" \
  -d '{"name":"admin","password":"admin123"}' | sed 's/.*"token":"\([^"]*\)".*/\1/')

# 2. 上传视频
OSS_KEY=$(curl -s -X POST http://8.149.247.100:8088/admin/uploads/file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/video.mp4" \
  -F "scope=submissions" | sed 's/.*"oss_key":"\([^"]*\)".*/\1/')

# 3. 创建提报
curl -X POST http://8.149.247.100:8088/admin/material-submissions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"team_name\": \"一组团队\",
    \"delivery_time\": \"2026-08-01 12:00\",
    \"drama_name\": \"短剧一号\",
    \"oss_key\": \"$OSS_KEY\",
    \"video_file_name\": \"成片A.mp4\",
    \"title_name\": \"标题A\",
    \"episode_range\": \"1-10\"
  }"
```
