"""API 请求/响应模型(Pydantic)。"""
from __future__ import annotations
from pydantic import BaseModel, Field
from app.domain.models import MaterialType


class MaterialCreate(BaseModel):
    type: MaterialType = MaterialType.IMAGE
    oss_key: str
    # 归属由 token 里的当前用户决定,不接受客户端指定(防越权伪造归属)。


class VideoUpload(BaseModel):
    oss_key: str
    size_bytes: int = 0


class RegisterIn(BaseModel):
    name: str
    password: str


class LoginIn(BaseModel):
    name: str
    password: str


class GrantIn(BaseModel):
    role: str
    permission: str


class AuditSet(BaseModel):
    status: str  # pass / review / block
    reason: str = ""  # 人工退回(block)原因,记入作品退回历史


class RuleIn(BaseModel):
    source_type: str = "any"        # any 或 TextSourceType 值
    keywords: list[str] = []        # 关键词快筛
    condition: str = ""             # 自然语言条件(交大模型判)
    action: str = "block"           # block / review
    project_id: str = ""            # 空=标准/全局规则;非空=该项目的作品规则
    guidance: str = ""              # 尺度说明(到什么程度算违规、好/坏例子)
    match_level: str = "metaphor"   # 严格程度:literal=字面 | metaphor=隐喻(默认) | regex=正则(不走大模型)
    regex: str = ""                 # 正则模式的已编译正则(match_level=="regex" 时审核用它精确匹配)


class RuleExceptionIn(BaseModel):
    text: str = ""                  # 被审核员判定「可忽略」的命中内容(记为该规则的例外)
    note: str = ""                  # 备注(通常填 AI 当时的判违规原因)


class ProjectIn(BaseModel):
    name: str                       # 作品项目名(如「汽水音乐」)


class RuleParseIn(BaseModel):
    text: str = ""                  # 管理员粘贴的整篇「卡审/审核标准」文案
    project_id: str = ""            # 解析结果将归属的项目(必填,作品规则都属于某项目)


class RegexCompileIn(BaseModel):
    text: str = ""                  # 正则规则的自然语言描述 → 大模型编译成关键词+正则(预览用)


class RuleDraft(BaseModel):
    category: str = ""              # 分类(展示用,不落库)
    source_type: str = "any"        # any 或 TextSourceType 值
    keywords: list[str] = []        # 关键词快筛
    condition: str = ""             # 自然语言条件
    action: str = "review"          # block / review
    match_level: str = "metaphor"   # 严格程度:literal=字面 | metaphor=隐喻(解析时由大模型定,缺省隐喻)


class RulesBulkIn(BaseModel):
    rules: list[RuleDraft] = []     # 预览确认后要批量落库的规则草案
    project_id: str = ""            # 落到哪个项目作用域


class TagsIn(BaseModel):
    tags: list[str] = []            # 物料标签(项目分类)


class UserCreate(BaseModel):
    name: str
    password: str                   # 管理员创建的账号默认为普通用户


class UserPermsIn(BaseModel):
    permissions: list[str] = []     # 给某用户设置的功能权限(整套替换)


class WhitelistIn(BaseModel):
    words: list[str] = []           # 内容安全白名单:加入这些词(命中即便阿里云判违规也放行)


class BlockwordIn(BaseModel):
    words: list[str] = []           # 绝对禁词:加入这些词(审核第一波,命中即拦)


class IdsIn(BaseModel):
    ids: list[str] = []


class RequirementIn(BaseModel):
    description: str = Field(min_length=1, max_length=10000)
    urgency: str = "medium"
    status: str = "not_started"
    reply: str = Field(default="", max_length=10000)
    attachments: list[str] = Field(default_factory=list, max_length=50)


class MaterialSubmissionIn(BaseModel):
    team_name: str = ""
    delivery_time: str = ""
    drama_name: str = ""
    oss_key: str = ""
    video_file_name: str = ""
    title_name: str = ""
    episode_range: str = ""
    upload_date: str | None = None


class MaterialSubmissionUpdateIn(BaseModel):
    team_name: str = ""
    delivery_time: str = ""
    drama_name: str = ""
    oss_key: str = ""
    video_file_name: str = ""
    title_name: str = ""
    episode_range: str = ""
    revision_comment: str = ""
    can_upload_status: int | None = None
    designated_upload_account_name: str = ""
    upload_account_name: str = ""
    upload_date: str | None = None
    publish_status: int | None = None
    platform_reject_reason: str = ""
    platform_reject_attachments: list[str] = []


class MaterialSubmissionPermissionGrantIn(BaseModel):
    user_id: str
    permission_type: str  # read / read_edit


class MaterialSubmissionPermissionsIn(BaseModel):
    grants: list[MaterialSubmissionPermissionGrantIn] = []


class MaterialSubmissionBatchPermissionsIn(BaseModel):
    submission_ids: list[str] = []
    user_ids: list[str] = []
    permission_type: str


class MaterialSubmissionUserGrantIn(BaseModel):
    submission_id: str
    permission_type: str  # read / read_edit


class MaterialSubmissionUserPermissionsIn(BaseModel):
    grants: list[MaterialSubmissionUserGrantIn] = []


class MaterialSubmissionUnselectedQueryIn(BaseModel):
    selected_submission_ids: list[str] = Field(default_factory=list, max_length=1000)
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    drama_name: str = ""
    title_name: str = ""
    can_upload_status: str = ""
    designated_upload_account_name: str = ""
    upload_account_name: str = ""
    created_by: str = ""
    publish_status: str = ""


class MaterialSubmissionProcessIn(BaseModel):
    revision_comment: str = ""
    can_upload_status: int | None = None
    designated_upload_account_name: str = ""
    upload_account_name: str = ""
    upload_date: str | None = None
    publish_status: int | None = None
    platform_reject_reason: str = ""
    platform_reject_attachments: list[str] = []


# ── 规则训练 ──
class VideoEditingTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=10000)
    reference_oss_key: str = Field(default="", max_length=2000)
    narration_voice: dict = Field(default_factory=dict)
    bgm_oss_key: str = Field(default="", max_length=2000)
    config: dict = Field(default_factory=dict)
    status: str = "active"


class VideoEditingTemplateUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=10000)
    reference_oss_key: str | None = Field(default=None, max_length=2000)
    narration_voice: dict | None = None
    bgm_oss_key: str | None = Field(default=None, max_length=2000)
    config: dict | None = None
    status: str | None = None


class TrainingExampleIn(BaseModel):
    material_id: str                        # 被标注的物料 ID
    expected_rule_ids: list[str] = []       # 该物料应该命中的规则 ID 列表
    source_note: str = ""                   # 人工标注备注


class TrainingExampleUpdateIn(BaseModel):
    expected_rule_ids: list[str] | None = None  # 该物料应该命中的规则 ID 列表(None=不修改)
    source_note: str | None = None              # 人工标注备注(None=不修改)


class TrainingConfigIn(BaseModel):
    max_fp_ratio: float = 0.20              # 可接受的最大多判率(0~1)
    max_iterations: int = 10                # 最大重审迭代次数(0~50,0=仅校验不调AI)
