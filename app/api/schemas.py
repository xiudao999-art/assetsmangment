"""API 请求/响应模型(Pydantic)。"""
from __future__ import annotations
from pydantic import BaseModel
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


# ── 规则训练 ──
class TrainingExampleIn(BaseModel):
    material_id: str                        # 被标注的物料 ID
    expected_rule_ids: list[str] = []       # 该物料应该命中的规则 ID 列表
    source_note: str = ""                   # 人工标注备注


class TrainingExampleUpdateIn(BaseModel):
    expected_rule_ids: list[str] | None = None  # 该物料应该命中的规则 ID 列表(None=不修改)
    source_note: str | None = None              # 人工标注备注(None=不修改)


class TrainingConfigIn(BaseModel):
    max_fp_ratio: float = 0.20              # 可接受的最大多判率(0~1)
    max_iterations: int = 10                # 最大重审迭代次数(1~50)
