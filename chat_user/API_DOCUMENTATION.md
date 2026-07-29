# Chat User API Documentation

## Overview

- Base URL: `http://{host}:8000`
- Swagger UI: `/docs`
- ReDoc: `/redoc`

## Users

### Create User

- Method: `POST`
- Path: `/users`

Request body:

```json
{
  "username": "zhangsan",
  "password": "123456"
}
```

Response:

```json
{
  "message": "User created successfully",
  "user_id": 1
}
```

### Verify Login

- Method: `POST`
- Path: `/auth/login`

Request body:

```json
{
  "username": "zhangsan",
  "password": "123456"
}
```

Notes:

- Current password storage uses the new reversible cipher configured by `USER_PASSWORD_KEY`.
- Legacy `bcrypt` users can still log in normally.
- When a legacy `bcrypt` user logs in successfully, the stored password will be upgraded automatically to the new algorithm.

### Change Password

- Method: `POST`
- Path: `/users/change-password`

Request body:

```json
{
  "user_id": 1,
  "old_password": "123456",
  "new_password": "654321"
}
```

Notes:

- If the old password belongs to a legacy `bcrypt` record, verification is still supported.
- After password change succeeds, the stored password will always be rewritten with the new algorithm.

### Decrypt Password

- Method: `POST`
- Path: `/users/decrypt-password`
- This endpoint is intentionally hidden from Swagger and only documented here.

Request body:

```json
{
  "encrypted_password": "gAAAAABp..."
}
```

Response:

```json
{
  "plain_password": "123456"
}
```

curl -X POST "http://36.151.241.14:8083/users/decrypt-password" \
  -H "Content-Type: application/json" \
  -d '{
    "encrypted_password": "gAAAAABqVt4t2k_sfLuQabEXs6lOmtcqM4QIjFv85AzvP9A1fCpudOuuwY3S35dIj0tSFq0bhgp_SX4uxV76rmLfcFFezr8n3w=="
  }'

Usage notes:

- This endpoint is only for decrypting passwords already stored with the current reversible algorithm.
- Legacy `bcrypt` hashes cannot be decrypted by this endpoint.
- Call it like a normal JSON API even though it does not appear in `/docs`.

## Dialogues

### Create Dialogue

- Method: `POST`
- Path: `/dialogues`

Request body:

```json
{
  "user_id": 1,
  "title": "病例讨论",
  "request_content": "请分析这个病例",
  "responses": [
    {
      "response_title": "回答1",
      "response_content": "这是第一条回答",
      "response_svgs": ["<svg>...</svg>"]
    },
    {
      "response_title": "回答2",
      "response_content": "这是第二条回答",
      "response_svgs": []
    }
  ]
}
```

Response:

```json
{
  "message": "Dialogue created successfully",
  "dialogue_id": 1,
  "turn_id": 1,
  "svg_count": 1
}
```

### Update Dialogue Title

- Method: `PUT`
- Path: `/dialogues/{dialogue_id}`

Request body:

```json
{
  "title": "新的对话标题"
}
```

### Get One User Dialogue

- Method: `GET`
- Path: `/users/{user_id}/dialogues/{dialogue_id}`

Description:

- Return one dialogue with all turns, responses, and SVG list.

### List User Dialogues

- Method: `GET`
- Path: `/users/{user_id}/dialogues`

Description:

- Return dialogue summary list for one user.

### Append Dialogue Turn

- Method: `POST`
- Path: `/dialogue-turns/{dialogue_id}`

Request body:

```json
{
  "request_content": "继续分析",
  "responses": [
    {
      "response_title": "补充回答1",
      "response_content": "补充内容1",
      "response_svgs": ["<svg>...</svg>"]
    }
  ]
}
```

### Get Dialogue Validate Status

- Method: `GET`
- Path: `/dialogue-turns/{dialogue_id}/validate-status`

Description:

- Return all turns for one dialogue.
- Each response includes:
  - `validate_added`
  - `validate_id`

## Validates

### Create Validate

- Method: `POST`
- Path: `/validates`

Request body:

```json
{
  "turn_id": 1,
  "response_id": 1
}
```

### Batch Create Validates

- Method: `POST`
- Path: `/validates/batch-create`

Request body:

```json
[
  {
    "turn_id": 1,
    "response_ids": [1, 2]
  },
  {
    "turn_id": 2,
    "response_ids": [3, 4]
  }
]
```

### Get Validate By ID

- Method: `GET`
- Path: `/validates/{validate_id}`

### Batch Query Validates

- Method: `POST`
- Path: `/users/{user_id}/validates/batch-query`

All fields below are optional.

Request body:

```json
{
  "status": "待验证",
  "judge_conclusion": 1,
  "startTime": "2026-07-01T00:00:00",
  "endTime": "2026-07-01T23:59:59",
  "keywords": "阿司匹林"
}
```

Notes:

- `judge_conclusion` value range:
  - `1`: 推断正确
  - `0`: 推断错误
  - `-1`: 无法判断
- `startTime` and `endTime` filter `zhiling_validate.update_at`
- `keywords` fuzzy matches:
  - `dialogue_turns.request_content`
  - `user_dialogue_turns_response.response_title`
  - `user_dialogue_turns_response.response_content`

Response fields include:

- All columns from `zhiling_validate`
- `request_content`
- `response_title`
- `response_content`

### Update Validate

- Method: `PUT`
- Path: `/validates/{validate_id}`

Request body:

```json
{
  "judge_conclusion": 1,
  "judge_content": "判断依据完整",
  "attachment_urls": [
    "http://127.0.0.1:9002/chat-user-attachments/attachments/demo.pdf"
  ]
}
```

### Delete Validate

- Method: `DELETE`
- Path: `/validates/{validate_id}`

Response:

```json
{
  "message": "Validate record deleted successfully",
  "id": 1
}
```

## Attachments

### Upload Attachment

- Method: `POST`
- Path: `/attachments/upload`
- Content-Type: `multipart/form-data`

Form field:

- `file`

Response:

```json
{
  "message": "Attachment uploaded successfully",
  "attachment_url": "http://127.0.0.1:9002/chat-user-attachments/attachments/xxx.xlsx"
}
```

### Delete Attachment

- Method: `DELETE`
- Path: `/attachments`

Request body:

```json
{
  "attachment_url": "http://127.0.0.1:9002/chat-user-attachments/attachments/xxx.xlsx"
}
```

## Notes

- Passwords are stored with the current reversible cipher configured by `USER_PASSWORD_KEY`.
- Legacy `bcrypt` password hashes are still supported for login verification and password change.
- Legacy `bcrypt` records are automatically upgraded to the new algorithm after successful login.
- Database uses MySQL.
- Attachment files are stored in MinIO.
- Validation status default value is `待验证`.
- `judge_conclusion` only supports `1`, `0`, `-1`.
