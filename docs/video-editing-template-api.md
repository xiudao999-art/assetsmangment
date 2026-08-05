# Video-editing template API

The API stores reusable short-drama/video editing templates. Reference products and BGM files are uploaded through the existing OSS upload endpoint; template records store only the returned object keys.

## Authentication

Login with `POST /users/login`, then send `Authorization: Bearer <token>`. The API uses the same accounts as material submissions.

## Upload assets

`POST /admin/uploads/file` as multipart form data with `scope=templates` and `file=<binary>`.

The response is:

```json
{"oss_key":"templates/uuid-file.mp4","file_name":"file.mp4"}
```

Use the returned key in `reference_oss_key` or `bgm_oss_key`. Do not store local paths, `oss://` URIs, public URLs, or signed URLs.

## Endpoints

- `GET /admin/video-editing-templates?name=&status=active&page=1&size=20`
- `GET /admin/video-editing-templates/by-name/{exact_name}`
- `GET /admin/video-editing-templates/{id}`
- `POST /admin/video-editing-templates`
- `PUT /admin/video-editing-templates/{id}`
- `GET /admin/uploads/url?key=<oss_key>&template_id=<id>`

All endpoints require login. Any authenticated user may list, read, upload template assets, and create a template. Only the creator or an administrator may edit it.

## Create body

```json
{
  "name": "high-conflict-ck-v1",
  "description": "Two-minute vertical recap style.",
  "reference_oss_key": "templates/uuid-reference.mp4",
  "narration_voice": {
    "provider": "minimax",
    "model": "speech-2.8-hd",
    "voice_id": "voice-id",
    "post_production_speed": 1.25
  },
  "bgm_oss_key": "templates/uuid-music.mp3",
  "config": {
    "editing": {},
    "captions": {},
    "visual": {},
    "audio": {},
    "quality_control": {},
    "policy": {}
  },
  "status": "active"
}
```

Names are unique case-insensitively among live records. Status is `active` or `inactive`.

## Update body

Updates are partial and require at least one field. Each successful update increments `version`. Send `{"bgm_oss_key":""}` to disable independent BGM.

## Response

A detail response includes `id`, `name`, `description`, both OSS keys, `narration_voice`, `config`, `status`, `version`, audit metadata, and `can_edit`.
