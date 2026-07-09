// C4 模型(闭环②:哪些组件 + 组件如何交互)—— Structurizr DSL
// 校验/出图:c4-model-skill 或 structurizr/mcp(validate / inspect / export-mermaid)
workspace "物料管理系统" "图/表情包/视频/风格/语料管理 + 视频反解物料 + 搜索/审核/权限" {

    model {
        user = person "运营用户" "上传物料、搜索、管理"
        admin = person "管理员" "用户与功能权限后台"

        system = softwareSystem "物料管理系统" {
            web = container "前端 SPA" "shadcn/ui + Magic UI" "React/TS"
            api = container "API 服务" "REST 接口、鉴权、路由" "FastAPI"
            worker = container "异步 Worker" "视频反解、批量索引" "Celery"
            db = container "主库 + 向量库" "关系数据 + 物料向量检索" "PostgreSQL + pgvector"
            cache = container "缓存/队列" "会话、任务队列" "Redis"
        }

        oss = softwareSystem "阿里云 OSS" "物料文件存储" "External"
        qwen = softwareSystem "百炼 Qwen-VL" "视频/图片反解 + multimodal-embedding" "External"
        audit = softwareSystem "阿里云内容安全" "图/视频/文本自动审核" "External"

        // 交互关系
        user -> web "使用"
        admin -> web "管理权限"
        web -> api "HTTPS/JSON"
        api -> db "读写元数据/向量" "SQLAlchemy"
        api -> cache "会话/派发任务"
        api -> oss "上传/签名 URL"
        api -> worker "投递反解任务" "Celery/Redis"
        worker -> oss "拉取视频"
        worker -> qwen "反解→物料 + 生成向量" "DashScope"
        worker -> audit "反解内容送审" "内容安全 API"
        worker -> db "写回物料 + 向量 + 审核结果"
    }

    views {
        systemContext system "Context" { include *; autolayout lr }
        container system "Containers" { include *; autolayout lr }
        theme default
    }
}
