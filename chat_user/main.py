"""FastAPI entrypoint that provides Swagger UI for the chat user service."""

from datetime import datetime
import logging
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from dialogue_service import (
    append_dialogue_turn_by_dialogue_id,
    count_all_users_turns,
    count_user_turns,
    create_dialogue,
    delete_dialogue,
    get_dialogue_turns_with_validate_status,
    get_user_dialogue_by_id,
    list_dialogues,
    update_dialogue_title,
)
from minio_service import delete_attachment, upload_attachment
from user_service import change_password, create_user, get_user_by_id, get_user_by_username, verify_user
from validate_service import (
    create_validate,
    create_validates_batch,
    delete_validate,
    get_validate_by_id,
    list_validates_by_filters,
    update_validate,
)

app = FastAPI(
    title="Chat User API",
    description="",
    version="1.1.0",
    openapi_tags=[
        {"name": "users", "description": "User related APIs"},
        {"name": "dialogues", "description": "Dialogue related APIs"},
        {"name": "validates", "description": "Validation related APIs"},
        {"name": "attachments", "description": "MinIO attachment APIs"},
    ],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("chat_user_api")


def _mask_sensitive_fields(data: dict) -> dict:
    """Mask sensitive fields before writing request params to logs."""
    masked = dict(data)
    if "password" in masked:
        masked["password"] = "***MASKED***"
    if "old_password" in masked:
        masked["old_password"] = "***MASKED***"
    if "new_password" in masked:
        masked["new_password"] = "***MASKED***"
    return masked


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100, description="Username")
    password: str = Field(..., min_length=6, max_length=100, description="Plain password for signup")


class UserLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100, description="Username")
    password: str = Field(..., min_length=1, max_length=100, description="Login password")


class ChangePasswordRequest(BaseModel):
    user_id: int = Field(..., gt=0, description="User ID")
    old_password: str = Field(..., min_length=1, max_length=100, description="Old password")
    new_password: str = Field(..., min_length=6, max_length=100, description="New password")


class DialogueCreateResponseRequest(BaseModel):
    response_title: str = Field(..., min_length=1, max_length=200, description="Response title")
    response_content: str = Field(..., min_length=1, description="Model response content")
    response_svgs: List[str] = Field(default_factory=list, description="SVG list for the response")


class DialogueCreateRequest(BaseModel):
    user_id: int = Field(..., gt=0, description="User ID")
    title: str = Field(..., min_length=1, max_length=200, description="Dialogue title")
    request_content: str = Field(..., min_length=1, description="User request content")
    responses: Optional[List[DialogueCreateResponseRequest]] = Field(default=None, description="Response list")
    response_title: Optional[str] = Field(default=None, description="Legacy single response title")
    response_content: Optional[str] = Field(default=None, description="Legacy single response content")
    response_svgs: List[str] = Field(default_factory=list, description="Legacy single response SVG list")

    @model_validator(mode="after")
    def validate_compatible_payload(self):
        has_new = bool(self.responses)
        has_old = bool(self.response_title or self.response_content or self.response_svgs)

        if not has_new and not has_old:
            raise ValueError("responses or top-level response fields are required")
        if has_old and (not self.response_title or not self.response_content):
            raise ValueError("Top-level response_title and response_content cannot be empty")
        return self

    def normalized_responses(self) -> List[dict]:
        if self.responses:
            return [item.model_dump() for item in self.responses]
        return [
            {
                "response_title": self.response_title,
                "response_content": self.response_content,
                "response_svgs": self.response_svgs,
            }
        ]


class DialogueUpdateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Updated dialogue title")


class DialogueTurnAppendRequest(BaseModel):
    user_id: Optional[int] = Field(default=None, description="Compatible field, ignored for append")
    title: Optional[str] = Field(default=None, description="Compatible field, ignored for append")
    request_content: str = Field(..., min_length=1, description="Updated user request content")
    responses: Optional[List[DialogueCreateResponseRequest]] = Field(default=None, description="Response list")
    response_title: Optional[str] = Field(default=None, description="Top-level response title")
    response_content: Optional[str] = Field(default=None, description="Top-level response content")
    response_svgs: List[str] = Field(default_factory=list, description="Top-level response SVG list")

    @model_validator(mode="after")
    def validate_compatible_payload(self):
        has_new = bool(self.responses)
        has_old = bool(self.response_title or self.response_content or self.response_svgs)

        if not has_new and not has_old:
            raise ValueError("responses or top-level response fields are required")
        if has_old and (not self.response_title or not self.response_content):
            raise ValueError("Top-level response_title and response_content cannot be empty")
        return self

    def normalized_responses(self) -> List[dict]:
        if self.responses:
            return [item.model_dump() for item in self.responses]
        return []


class UserResponse(BaseModel):
    user_id: int
    username: str
    created_at: Optional[datetime] = None


class UserCreateResponse(BaseModel):
    message: str
    user_id: int


class LoginResponse(BaseModel):
    message: str
    user_id: int


class ChangePasswordResponse(BaseModel):
    message: str
    user_id: int


class DialogueSummaryResponse(BaseModel):
    dialogue_id: int
    user_id: int
    title: str
    turn_count: int
    created_at: datetime
    updated_at: datetime


class DialogueTurnResponseItem(BaseModel):
    id: int
    turn_id: int
    resp_no: int
    response_title: str
    response_content: str
    response_svgs: List[str] = Field(default_factory=list)
    created_at: datetime


class DialogueTurnValidateResponseItem(DialogueTurnResponseItem):
    validate_added: bool = False
    validate_id: Optional[int] = None


class DialogueTurnResponse(BaseModel):
    turn_id: int
    dialogue_id: int
    request_content: str
    response_title: Optional[str] = None
    response_content: Optional[str] = None
    response_svgs: List[str] = Field(default_factory=list)
    responses: List[DialogueTurnResponseItem] = Field(default_factory=list)
    svg_count: int = 0
    created_at: datetime
    updated_at: datetime


class DialogueCreateResponse(BaseModel):
    message: str
    dialogue_id: int
    turn_id: int
    svg_count: int


class DialogueUpdateResponse(BaseModel):
    message: str
    dialogue: DialogueSummaryResponse


class DialogueDeleteResponse(BaseModel):
    message: str
    dialogue_id: int


class DialogueTurnAppendResponse(BaseModel):
    message: str
    turn: DialogueTurnResponse


class UserTurnCountResponse(BaseModel):
    user_id: int
    total_turns: int


class AllUsersTurnCountItem(BaseModel):
    user_id: int
    username: str
    total_turns: int


class DialogueDetailResponse(BaseModel):
    dialogue_id: int
    user_id: int
    title: str
    turn_count: int
    created_at: datetime
    updated_at: datetime
    turns: List[DialogueTurnResponse]


class DialogueTurnValidateResponse(BaseModel):
    turn_id: int
    dialogue_id: int
    request_content: str
    response_title: Optional[str] = None
    response_content: Optional[str] = None
    response_svgs: List[str] = Field(default_factory=list)
    responses: List[DialogueTurnValidateResponseItem] = Field(default_factory=list)
    svg_count: int = 0
    created_at: datetime
    updated_at: datetime


class DialogueValidateDetailResponse(BaseModel):
    dialogue_id: int
    user_id: int
    title: str
    turn_count: int
    created_at: datetime
    updated_at: datetime
    turns: List[DialogueTurnValidateResponse]


class ValidateCreateRequest(BaseModel):
    turn_id: int = Field(..., gt=0, description="Turn ID")
    response_id: int = Field(..., gt=0, description="Response ID")


class ValidateBatchCreateItem(BaseModel):
    turn_id: int = Field(..., gt=0, description="Turn ID")
    response_ids: List[int] = Field(..., min_length=1, description="Response ID list")


class ValidateBatchQueryRequest(BaseModel):
    status: Optional[str] = Field(default=None, description="Validate status")
    judge_conclusion: Optional[int] = Field(
        default=None,
        description="Judge conclusion: 1=推断正确, 0=推断错误, -1=无法判断",
    )
    startTime: Optional[datetime] = Field(default=None, description="Start time for update_at filter")
    endTime: Optional[datetime] = Field(default=None, description="End time for update_at filter")
    keywords: Optional[str] = Field(
        default=None,
        description="Optional fuzzy-match keyword for request_content, response_title, or response_content",
    )


class ValidateUpdateRequest(BaseModel):
    judge_conclusion: Optional[int] = Field(
        default=None,
        description="Judge conclusion: 1=推断正确, 0=推断错误, -1=无法判断",
    )
    judge_content: Optional[str] = Field(default=None, description="Judge content")
    attachment_urls: List[str] = Field(default_factory=list, description="Attachment URLs stored in MinIO")


class ValidateResponse(BaseModel):
    id: int
    turn_id: int
    response_id: int
    request_content: Optional[str] = None
    response_title: Optional[str] = None
    response_content: Optional[str] = None
    status: str
    judge_conclusion: Optional[int] = None
    judge_content: Optional[str] = None
    attachment_urls: List[str] = Field(default_factory=list)
    create_at: datetime
    update_at: datetime


class ValidateBatchQueryResponse(ValidateResponse):
    pass


class AttachmentUploadResponse(BaseModel):
    message: str
    attachment_url: str


class AttachmentDeleteRequest(BaseModel):
    attachment_url: str = Field(..., min_length=1, description="Attachment URL to delete")


class AttachmentDeleteResponse(BaseModel):
    message: str
    attachment_url: str


class ValidateDeleteResponse(BaseModel):
    message: str
    id: int


class ValidateBatchCreateResponse(BaseModel):
    message: str
    records: List[ValidateResponse]


# @app.get("/", summary="Service home")
# def home():
#     return {
#         "message": "Chat User API is running.",
#         "swagger_url": "/docs",
#         "redoc_url": "/redoc",
#     }


@app.post(
    "/users",
    response_model=UserCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create user",
    tags=["users"],
)
def create_user_api(payload: UserCreateRequest):
    try:
        logger.info("create_user_api payload=%s", _mask_sensitive_fields(payload.model_dump()))
        user_id = create_user(payload.username, payload.password)
        return {"message": "User created successfully", "user_id": user_id}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("create_user_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# @app.get("/users/by-username/{username}", response_model=UserResponse, summary="Get user by username")
# def get_user_by_username_api(username: str):
#     try:
#         user = get_user_by_username(username)
#         if not user:
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
#         return user
#     except RuntimeError as exc:
#         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post("/auth/login", response_model=LoginResponse, summary="Verify login", tags=["users"])
def login_api(payload: UserLoginRequest):
    try:
        logger.info("login_api payload=%s", _mask_sensitive_fields(payload.model_dump()))
        user_id = verify_user(payload.username, payload.password)
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        return {"message": "Login successful", "user_id": user_id}
    except RuntimeError as exc:
        logger.exception("login_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/users/change-password",
    response_model=ChangePasswordResponse,
    summary="Change user password",
    tags=["users"],
)
def change_password_api(payload: ChangePasswordRequest):
    try:
        logger.info("change_password_api payload=%s", _mask_sensitive_fields(payload.model_dump()))
        changed = change_password(payload.user_id, payload.old_password, payload.new_password)
        if not changed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User not found or old password is incorrect",
            )
        return {"message": "Password changed successfully", "user_id": payload.user_id}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("change_password_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/dialogues",
    response_model=DialogueCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create dialogue",
    tags=["dialogues"],
)
def create_dialogue_api(payload: DialogueCreateRequest):
    try:
        logger.info("create_dialogue_api payload=%s", payload.model_dump())
        user = get_user_by_id(payload.user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        result = create_dialogue(
            payload.user_id,
            payload.title,
            payload.request_content,
            payload.response_title,
            payload.response_content,
            payload.response_svgs,
            payload.normalized_responses(),
        )
        return {
            "message": "Dialogue created successfully",
            "dialogue_id": result["dialogue_id"],
            "turn_id": result["turn_id"],
            "svg_count": result["svg_count"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("create_dialogue_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.put(
    "/dialogues/{dialogue_id}",
    response_model=DialogueUpdateResponse,
    summary="Update dialogue title",
    tags=["dialogues"],
)
def update_dialogue_api(dialogue_id: int, payload: DialogueUpdateRequest):
    try:
        logger.info(
            "update_dialogue_api dialogue_id=%s payload=%s",
            dialogue_id,
            payload.model_dump(),
        )
        dialogue = update_dialogue_title(dialogue_id, payload.title)
        if not dialogue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialogue not found")
        return {"message": "Dialogue title updated successfully", "dialogue": dialogue}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("update_dialogue_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.delete(
    "/dialogues/{dialogue_id}",
    response_model=DialogueDeleteResponse,
    summary="Delete one dialogue and its related dialogue data",
    tags=["dialogues"],
)
def delete_dialogue_api(dialogue_id: int):
    try:
        logger.info("delete_dialogue_api dialogue_id=%s", dialogue_id)
        deleted = delete_dialogue(dialogue_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialogue not found")
        return {"message": "Dialogue deleted successfully", "dialogue_id": dialogue_id}
    except RuntimeError as exc:
        logger.exception("delete_dialogue_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.get(
    "/users/{user_id}/dialogues/{dialogue_id}",
    response_model=DialogueDetailResponse,
    summary="Get one user dialogue by dialogue id",
    tags=["dialogues"],
)
def get_user_dialogue_by_id_api(user_id: int, dialogue_id: int):
    try:
        logger.info(
            "get_user_dialogue_by_id_api user_id=%s dialogue_id=%s",
            user_id,
            dialogue_id,
        )
        user = get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        dialogue = get_user_dialogue_by_id(user_id, dialogue_id)
        if not dialogue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialogue not found")
        logger.info(
            "get_user_dialogue_by_id_api result dialogue_id=%s turn_count=%s turns_size=%s",
            dialogue.get("dialogue_id"),
            dialogue.get("turn_count"),
            len(dialogue.get("turns", [])),
        )
        return dialogue
    except RuntimeError as exc:
        logger.exception("get_user_dialogue_by_id_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("get_user_dialogue_by_id_api unexpected failed")
        raise


@app.get(
    "/users/{user_id}/dialogues",
    response_model=List[DialogueSummaryResponse],
    summary="List user dialogues",
    tags=["dialogues"],
)
def list_dialogues_api(user_id: int):
    try:
        logger.info("list_dialogues_api user_id=%s", user_id)
        user = get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return list_dialogues(user_id)
    except RuntimeError as exc:
        logger.exception("list_dialogues_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/dialogue-turns/{dialogue_id}",
    response_model=DialogueTurnAppendResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a new dialogue turn by dialogue id",
    tags=["dialogues"],
)
def append_dialogue_turn_api(dialogue_id: int, payload: DialogueTurnAppendRequest):
    try:
        logger.info(
            "append_dialogue_turn_api dialogue_id=%s payload=%s",
            dialogue_id,
            payload.model_dump(),
        )
        turn = append_dialogue_turn_by_dialogue_id(
            dialogue_id,
            payload.request_content,
            payload.response_title,
            payload.response_content,
            payload.response_svgs,
            payload.normalized_responses(),
        )
        if not turn:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialogue not found")
        return {"message": "Dialogue turn appended successfully", "turn": turn}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("append_dialogue_turn_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.get(
    "/dialogue-turns/{dialogue_id}/validate-status",
    response_model=DialogueValidateDetailResponse,
    summary="Get dialogue turns and mark responses whether they are in zhiling_validate",
    tags=["dialogues"],
)
def get_dialogue_turns_with_validate_status_api(dialogue_id: int):
    try:
        logger.info("get_dialogue_turns_with_validate_status_api dialogue_id=%s", dialogue_id)
        dialogue = get_dialogue_turns_with_validate_status(dialogue_id)
        if not dialogue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialogue not found")
        return dialogue
    except RuntimeError as exc:
        logger.exception("get_dialogue_turns_with_validate_status_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/validates",
    response_model=ValidateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create one validate record",
    tags=["validates"],
)
def create_validate_api(payload: ValidateCreateRequest):
    try:
        logger.info("create_validate_api payload=%s", payload.model_dump())
        validate_id = create_validate(payload.turn_id, payload.response_id)
        validate = get_validate_by_id(validate_id)
        if not validate:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Validate record created but not found")
        return validate
    except ValueError as exc:
        logger.exception("create_validate_api validation failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("create_validate_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/validates/batch-create",
    response_model=ValidateBatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Batch create validate records",
    tags=["validates"],
)
def create_validates_batch_api(payload: List[ValidateBatchCreateItem]):
    try:
        logger.info("create_validates_batch_api payload=%s", [item.model_dump() for item in payload])
        created_ids = create_validates_batch([item.model_dump() for item in payload])
        records = []
        for validate_id in created_ids:
            validate = get_validate_by_id(validate_id)
            if validate:
                records.append(validate)
        return {"message": "Validate records created successfully", "records": records}
    except ValueError as exc:
        logger.exception("create_validates_batch_api validation failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("create_validates_batch_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.get(
    "/validates/{validate_id}",
    response_model=ValidateResponse,
    summary="Get validate record by id",
    tags=["validates"],
)
def get_validate_api(validate_id: int):
    try:
        logger.info("get_validate_api validate_id=%s", validate_id)
        validate = get_validate_by_id(validate_id)
        if not validate:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Validate record not found")
        return validate
    except RuntimeError as exc:
        logger.exception("get_validate_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/users/{user_id}/validates/batch-query",
    response_model=List[ValidateBatchQueryResponse],
    summary="Batch query validate records by optional filters",
    tags=["validates"],
)
def batch_query_validates_api(user_id: int, payload: ValidateBatchQueryRequest):
    try:
        logger.info("batch_query_validates_api user_id=%s payload=%s", user_id, payload.model_dump())
        user = get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        return list_validates_by_filters(
            user_id,
            payload.status,
            payload.judge_conclusion,
            payload.startTime,
            payload.endTime,
            payload.keywords,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("batch_query_validates_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.put(
    "/validates/{validate_id}",
    response_model=ValidateResponse,
    summary="Update validate record by id",
    tags=["validates"],
)
def update_validate_api(validate_id: int, payload: ValidateUpdateRequest):
    try:
        logger.info("update_validate_api validate_id=%s payload=%s", validate_id, payload.model_dump())
        validate = update_validate(
            validate_id,
            payload.judge_conclusion,
            payload.judge_content,
            payload.attachment_urls,
        )
        if not validate:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Validate record not found")
        return validate
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("update_validate_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.delete(
    "/validates/{validate_id}",
    response_model=ValidateDeleteResponse,
    summary="Delete validate record by id",
    tags=["validates"],
)
def delete_validate_api(validate_id: int):
    try:
        logger.info("delete_validate_api validate_id=%s", validate_id)
        deleted = delete_validate(validate_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Validate record not found")
        return {"message": "Validate record deleted successfully", "id": validate_id}
    except RuntimeError as exc:
        logger.exception("delete_validate_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post(
    "/attachments/upload",
    response_model=AttachmentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload attachment to MinIO",
    tags=["attachments"],
)
async def upload_attachment_api(file: UploadFile = File(...)):
    try:
        logger.info("upload_attachment_api filename=%s content_type=%s", file.filename, file.content_type)
        content = await file.read()
        attachment_url = upload_attachment(file.filename or "attachment.bin", content, file.content_type)
        return {"message": "Attachment uploaded successfully", "attachment_url": attachment_url}
    except ValueError as exc:
        logger.exception("upload_attachment_api validation failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("upload_attachment_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.delete(
    "/attachments",
    response_model=AttachmentDeleteResponse,
    summary="Delete attachment from MinIO",
    tags=["attachments"],
)
def delete_attachment_api(payload: AttachmentDeleteRequest):
    try:
        logger.info("delete_attachment_api payload=%s", payload.model_dump())
        deleted = delete_attachment(payload.attachment_url)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
        return {"message": "Attachment deleted successfully", "attachment_url": payload.attachment_url}
    except ValueError as exc:
        logger.exception("delete_attachment_api validation failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("delete_attachment_api failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# @app.get(
#     "/users/{user_id}/dialogues/count",
#     response_model=UserTurnCountResponse,
#     summary="Count one user's dialogue turns",
# )
# def count_user_turns_api(user_id: int):
#     try:
#         user = get_user_by_id(user_id)
#         if not user:
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
#         total_turns = count_user_turns(user_id)
#         return {"user_id": user_id, "total_turns": total_turns}
#     except RuntimeError as exc:
#         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# @app.get(
#     "/dialogues/stats/users",
#     response_model=List[AllUsersTurnCountItem],
#     summary="Count all users' dialogue turns",
# )
# def count_all_users_turns_api():
#     try:
#         return count_all_users_turns()
#     except RuntimeError as exc:
#         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
