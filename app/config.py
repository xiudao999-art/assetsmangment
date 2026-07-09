"""集中配置(pydantic-settings,从环境变量/.env 读)。前缀 AM_。"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AM_", env_file=".env", extra="ignore")

    # 用假实现还是真阿里云(默认假:本地/CI 无需密钥)
    use_fakes: bool = True

    # 阿里云 OSS
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_url_expire_seconds: int = 3600

    # 百炼 DashScope(Qwen-VL + multimodal-embedding)
    dashscope_api_key: str = ""
    qwen_vl_model: str = "qwen3-vl-plus"
    embedding_model: str = "multimodal-embedding-v1"
    embedding_dim: int = 1024
    parse_fps: float = 2.0
    parse_max_frames: int = 512

    # 内容安全
    content_safety_access_key_id: str = ""
    content_safety_access_key_secret: str = ""
    content_safety_region: str = "cn-shanghai"

    # 数据库(RDS PostgreSQL + pgvector)
    database_url: str = "postgresql://user:pass@localhost:5432/assets"


settings = Settings()
