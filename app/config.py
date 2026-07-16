"""集中配置(pydantic-settings,从环境变量/.env 读)。前缀 AM_。"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AM_", env_file=".env", extra="ignore")

    # 用假实现还是真阿里云(默认假:本地/CI 无需密钥)
    use_fakes: bool = True

    # 数据持久化目录(设了就把物料/用户/收藏落到 JSON,容器重启不丢;空=纯内存)
    data_dir: str = ""

    # 会话 token 签名密钥(HMAC)。生产必须用 AM_TOKEN_SECRET 覆盖为强随机值。
    token_secret: str = "dev-insecure-token-secret-change-me"
    token_ttl_seconds: int = 86400  # token 有效期(默认 1 天)

    # 阿里云 OSS
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_url_expire_seconds: int = 3600

    # 百炼 DashScope(Qwen-VL + multimodal-embedding + 审核用 LLM/ASR)
    dashscope_api_key: str = ""
    qwen_vl_model: str = "qwen3-vl-plus"
    qwen_llm_model: str = "qwen-plus"           # 规则判定/挑重点时间段
    asr_model: str = "paraformer-v2"            # 语音转写(带时间轴)
    embedding_model: str = "multimodal-embedding-v1"
    embedding_dim: int = 1024
    parse_fps: float = 2.0
    parse_max_frames: int = 512
    # 火山方舟 ARK(豆包 pro 2.1):物料档案「情绪/场景标签」解析(图片/视频直接看;审核仍用 Qwen-VL)
    ark_api_key: str = ""                                        # .env: AM_ARK_API_KEY
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_vl_model: str = ""                                    # .env: AM_DOUBAO_VL_MODEL(豆包视觉模型 id/endpoint)
    # Tavily 联网搜索:音乐物料按歌名联网搜「情绪/场景」,合成检索档案(只 music 用)
    tavily_api_key: str = ""                                     # .env: AM_TAVILY_API_KEY
    # 审核并发/健壮性:有界工作池上限(单条+批量都提交到它,超出排队=背压);AI 调用超时+重试
    audit_concurrency: int = 6      # 同时在审的最大条数(总线程≈本值×帧池5,别调太高)
    ai_timeout_s: int = 60          # 单次 AI 调用(Qwen-VL/LLM/ASR轮询)超时秒数,到点降级
    ai_retries: int = 2             # 偶发失败的重试次数(不含首次)
    # 搜索:向量近邻的相关度阈值(余弦距离,越小越像)。超过此距离的语义近邻视为无关、不返回,
    # 避免"搜一个词却搜出不相关物料"。关键词命中始终优先返回。
    search_max_distance: float = 0.35   # multimodal-embedding-v1:相关≈0.25-0.31、无关≈0.39+,0.35 干净分开

    # 内容安全(增强版 green20220302)。需在阿里云控制台开通「内容安全(增强版)」并授权 RAM。
    enable_content_safety: bool = False   # 开通后置 true 即接真机器审核;否则走人工审核
    content_safety_access_key_id: str = ""      # 留空则复用 OSS 的 AccessKey
    content_safety_access_key_secret: str = ""
    content_safety_region: str = "cn-beijing"
    # 内容安全严格度:strict(严重类全硬拦)/ balanced(适中:只色情政治暴恐硬拦,其余转人工)/ loose(从不硬拦,全转人工)
    content_safety_mode: str = "balanced"

    # 数据库(RDS PostgreSQL + pgvector)
    database_url: str = "postgresql://user:pass@localhost:5432/assets"


settings = Settings()
