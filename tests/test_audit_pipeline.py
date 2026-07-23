"""审核引擎单测:各素材链路 + 关键词快筛 + 大模型兜底 + 报告持久化。"""
from app.service.audit_pipeline import AuditPipelineService
from app.domain.models import (
    MaterialType, AuditStatus, AuditRule, TextSourceType, Material,
)
from app.infrastructure.fakes import (
    FakeTranscriber, FakeVisionDescriber, FakeLlm, InMemoryAuditRuleRepo,
    InMemoryAuditReportRepo, FakeStorage, InMemoryMaterialRepo, FakeEmbedder,
    InMemoryVectorIndex,
)


def _svc(llm=None, rules=(), repo=None, auditor=None, blockwords=None, archiver=None, tavily=None):
    rr = InMemoryAuditRuleRepo()
    for r in rules:
        rr.add(r)
    repo = repo or InMemoryMaterialRepo()
    reports = InMemoryAuditReportRepo()
    bw = (lambda: set(blockwords)) if blockwords else None
    svc = AuditPipelineService(FakeTranscriber(), FakeVisionDescriber(), llm or FakeLlm(),
                               rr, reports, FakeStorage(), repo, FakeEmbedder(), InMemoryVectorIndex(),
                               auditor, blockwords=bw, archiver=archiver, tavily=tavily)
    return svc, repo, reports


class _StubTavily:
    """联网搜索桩:返回固定简报,记录查询词。"""
    def __init__(self, brief="这首歌是治愈系民谣,适合温情旅行、回忆类短视频。") -> None:
        self.brief = brief
        self.calls: list[str] = []

    def search(self, query: str) -> str:
        self.calls.append(query)
        return self.brief


def _findings(*items):
    """构造语义审核返回:items 为 (rule_idx, segment_idx|None, reason) 三元组。"""
    return FakeLlm(response={"findings": [
        {"rule": r, "segment": s, "reason": why} for (r, s, why) in items]})


def _corpus_segs(text):
    from app.domain.models import TextSegment as _TS, TextSourceType as _TST
    return [_TS(_TST.ORIGINAL_TEXT, text)]


def test_compile_regex_service():
    # 建规则时:大模型把自然语言编译成 {关键词, 正则}(去重归一)
    llm = FakeLlm(response={"keywords": ["躺赚", "日入", "日入", "稳赚"],
                            "regex": r"躺.{0,2}赚|日\s*入\s*\d+"})
    svc, _, _ = _svc(llm=llm)
    out = svc.compile_regex("拦真钱躺赚:日入过百、稳赚不赔")
    assert out["regex"] == r"躺.{0,2}赚|日\s*入\s*\d+"
    assert out["keywords"] == ["躺赚", "日入", "稳赚"]       # 去重
    assert llm.calls                                       # 确实调了大模型(仅编译时)


def test_regex_rule_matches_without_llm():
    # 审核时:正则规则纯正则命中,完全不调大模型
    r = AuditRule(id="rx", source_type="any", keywords=["躺赚", "日入"], condition="",
                  action="review", match_level="regex", regex=r"躺.{0,2}赚|日\s*入\s*\d+")
    llm = FakeLlm()
    svc, _, _ = _svc(llm=llm, rules=[r])
    report = svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("今天日入 500 爽"))
    assert report.verdict == AuditStatus.REVIEW
    assert any("正则" in t.get("reason", "") for t in report.triggered)
    assert report.triggered[0]["rule_id"] == "rx" and report.triggered[0]["action"] == "review"
    assert llm.calls == []                                 # 审核判定零大模型调用


def test_regex_fallbacks():
    # regex 为空 → 退化成 keywords 字面 OR;命中
    r1 = AuditRule(id="rf1", source_type="any", keywords=["躺赚"], action="review",
                   match_level="regex", regex="")
    svc, _, _ = _svc(rules=[r1])
    assert svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("宣传躺赚项目")).verdict == AuditStatus.REVIEW
    # 不含关键词/正则 → 放行(不联想)
    assert svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("躺床上听歌金币自己涨")).verdict == AuditStatus.PASS
    # 非法正则「[」→ 退化子串匹配、不抛
    r2 = AuditRule(id="rf2", source_type="any", keywords=["躺赚"], action="review",
                   match_level="regex", regex="[")
    svc2, _, _ = _svc(rules=[r2])
    assert svc2._evaluate(svc2.submit(MaterialType.CORPUS), _corpus_segs("躺赚广告")).verdict == AuditStatus.REVIEW


def test_regex_honors_source_type():
    # source_type=transcript 的正则规则不该命中原文(ORIGINAL_TEXT)段
    r = AuditRule(id="rt", source_type=TextSourceType.TRANSCRIPT.value, keywords=["躺赚"],
                  action="review", match_level="regex", regex="躺赚")
    svc, _, _ = _svc(rules=[r])
    assert svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("躺赚广告")).verdict == AuditStatus.PASS


def test_regex_and_semantic_coexist():
    # 正则规则(扫描命中)+ 语义规则(大模型命中)同项目 → 两条都在;LLM 只被语义规则调用一次
    rx = AuditRule(id="rx", source_type="any", keywords=["躺赚"], action="review",
                   match_level="regex", regex="躺赚")
    sem = AuditRule(id="sm", source_type="any", condition="宣扬躺赚", action="review",
                    match_level="metaphor")
    llm = FakeLlm(response={"findings": [{"rule": 1, "segment": 1, "reason": "语义命中"}]})
    svc, _, _ = _svc(llm=llm, rules=[rx, sem])
    report = svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("宣传躺赚"))
    ids = {t["rule_id"] for t in report.triggered}
    assert ids == {"rx", "sm"}                             # 两条都命中
    assert len(llm.calls) == 1                             # 大模型只被语义规则调用一次


def test_semantic_finding_maps_by_no():
    # 大模型返回的规则编号按稳定 no 映射(不是按位置),triggered 带 rule_no
    r7 = AuditRule(id="a", source_type="any", condition="规则甲", action="review", match_level="metaphor", no=7)
    r3 = AuditRule(id="b", source_type="any", condition="规则乙", action="review", match_level="metaphor", no=3)
    llm = FakeLlm(response={"findings": [{"rule": 3, "segment": 1, "reason": "命中乙"},
                                         {"rule": 999, "segment": 1, "reason": "幻觉号"}]})
    svc, _, _ = _svc(llm=llm, rules=[r7, r3])
    # 规则清单文本用各自的 no 标号
    doc = svc._pack_rules([r7, r3])
    assert "7." in doc and "3." in doc
    report = svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("随便一段"))
    hits = [t for t in report.triggered if t.get("rule_id") in ("a", "b")]
    assert len(hits) == 1                          # 幻觉号 999 被丢弃
    assert hits[0]["rule_id"] == "b" and hits[0]["rule_no"] == 3   # 编号 3 → no==3 的乙,不是位置


def test_regex_trigger_has_rule_no():
    r = AuditRule(id="rx", source_type="any", keywords=["躺赚"], action="review",
                  match_level="regex", regex="躺赚", no=12)
    svc, _, _ = _svc(rules=[r])
    report = svc._evaluate(svc.submit(MaterialType.CORPUS), _corpus_segs("宣传躺赚"))
    assert report.triggered and report.triggered[0]["rule_no"] == 12


def test_text_audit_pass_without_rules():
    svc, _, _ = _svc()
    job = svc.submit(MaterialType.CORPUS)
    report = svc.run(job, text="今天天气很好")
    assert report.verdict == AuditStatus.PASS
    assert report.segments[0].source_type == TextSourceType.ORIGINAL_TEXT


def test_semantic_judge_flags_review_and_maps():
    # 第三波:大模型 findings 命中规则 1 → 机器统一转 review;finding 保留规则动作作严重度提示,定位到段落
    rule = AuditRule(id="r1", source_type="any", condition="宣传赌博网站", action="block")
    svc, _, _ = _svc(llm=_findings((1, 1, "文案在推广赌博")), rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="这是一个赌博网站广告")
    assert report.verdict == AuditStatus.REVIEW          # 机器永不 block,发现问题→待人工复核
    hit = [t for t in report.triggered if t["rule_id"] == "r1"]
    assert hit and hit[0]["action"] == "block" and hit[0]["reason"] == "文案在推广赌博"  # 动作=严重度提示
    assert hit[0]["text"] == "这是一个赌博网站广告"      # 段落文本回填,供报告标红定位
    assert hit[0]["rule_desc"] == "宣传赌博网站"          # 报告显示「因哪条规则」


def test_pack_rules_includes_guidance_and_exceptions():
    r = AuditRule(id="r1", source_type="any", condition="宣传赌博", action="block",
                  guidance="仅当明确诱导下注才算",
                  exceptions=[{"text": "只是提到扑克牌桌游、不诱导", "note": "", "by": "admin", "ms": 1}])
    out = AuditPipelineService._pack_rules([r])
    assert "尺度说明:仅当明确诱导下注才算" in out
    assert "已确认可放行的例外" in out and "扑克牌桌游" in out


def test_pack_rules_tags_match_level():
    """每条规则按严格程度打标(字面判定/隐喻判定);老规则缺字段 → 默认按隐喻。"""
    lit = AuditRule(id="r1", source_type="any", condition="字面规则", action="block", match_level="literal")
    meta = AuditRule(id="r2", source_type="any", condition="隐喻规则", action="block", match_level="metaphor")
    out = AuditPipelineService._pack_rules([lit, meta])
    assert "字面判定" in out and "隐喻判定" in out
    # 默认值即 metaphor → 打「隐喻判定」标
    old = AuditRule(id="r3", source_type="any", condition="老规则", action="block")
    assert "隐喻判定" in AuditPipelineService._pack_rules([old])


def test_judge_sys_defines_both_levels():
    from app.service.audit_pipeline import _RULE_JUDGE_SYS
    assert "字面判定" in _RULE_JUDGE_SYS and "隐喻判定" in _RULE_JUDGE_SYS


def test_rule_store_roundtrip_and_old_rule_defaults_metaphor(tmp_path):
    """持久化:match_level 存取一致;手写缺该键的旧规则档 → 载入默认 metaphor,不报错。"""
    import json
    from app.infrastructure.jsonstore import Store, JsonAuditRuleRepo
    p = str(tmp_path / "s.json")
    JsonAuditRuleRepo(Store(p)).add(
        AuditRule(id="a", source_type="any", condition="字面", action="block", match_level="literal"))
    assert next(r for r in JsonAuditRuleRepo(Store(p)).list() if r.id == "a").match_level == "literal"
    d = json.load(open(p, encoding="utf-8"))
    d["rules"].append({"id": "old", "source_type": "any", "keywords": [], "condition": "老", "action": "block",
                       "enabled": True, "created_by": "", "project_id": "", "guidance": "", "exceptions": []})
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    assert next(r for r in JsonAuditRuleRepo(Store(p)).list() if r.id == "old").match_level == "metaphor"


def test_semantic_review_action_from_rule():
    rule = AuditRule(id="r3", source_type="any", condition="出现导流到站外的联系方式", action="review")
    svc, _, _ = _svc(llm=_findings((1, 1, "疑似引流")), rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="加我微信领福利")
    assert report.verdict == AuditStatus.REVIEW
    assert any(t["rule_id"] == "r3" and t["action"] == "review" for t in report.triggered)


def test_semantic_no_rules_passes():
    svc, _, _ = _svc(llm=_findings((1, 1, "不该被调用")))   # 无规则 → 语义不跑,直接放行
    report = svc.run(svc.submit(MaterialType.CORPUS), text="随便一段文字")
    assert report.verdict == AuditStatus.PASS


def test_semantic_drops_out_of_range_rule_index():
    rule = AuditRule(id="r1", source_type="any", condition="x", action="block")
    svc, _, _ = _svc(llm=_findings((9, 1, "越界规则号")), rules=[rule])   # rule=9 不存在
    report = svc.run(svc.submit(MaterialType.CORPUS), text="内容")
    assert report.verdict == AuditStatus.PASS                            # 幻觉规则号被丢弃


def test_semantic_missing_findings_passes():
    rule = AuditRule(id="r1", source_type="any", condition="x", action="block")
    svc, _, _ = _svc(llm=FakeLlm(response={"garbage": True}), rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="内容")
    assert report.verdict == AuditStatus.PASS   # 无 findings 字段 → 当作没判出违规


def test_semantic_exception_falls_back_to_review():
    class _BoomLlm:
        calls = []
        def chat_json(self, system, user):
            if "审核引擎" in system:            # 语义判定时抛错 → 转人工
                raise RuntimeError("boom")
            return {}
    rule = AuditRule(id="r1", source_type="any", condition="x", action="block")
    svc, _, _ = _svc(llm=_BoomLlm(), rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="内容")
    assert report.verdict == AuditStatus.REVIEW   # 语义审核异常不放行,转人工


def test_blockword_flags_review_and_short_circuits():
    # 第一波:绝对禁词命中 → 转 review(机器不直接拦),且短路不调用大模型做语义判
    rule = AuditRule(id="r1", source_type="any", condition="x", action="review")
    llm = _findings((1, 1, "不该被调用"))
    svc, _, _ = _svc(llm=llm, rules=[rule], blockwords={"某禁词"})
    report = svc.run(svc.submit(MaterialType.CORPUS), text="这里有某禁词出现")
    assert report.verdict == AuditStatus.REVIEW
    assert any(t["rule_id"] == "blockword" and "某禁词" in t["reason"] for t in report.triggered)
    assert llm.calls == []                       # 短路:第一波拦下,不进第三波语义


def test_image_chain_uses_vision():
    svc, _, _ = _svc()
    job = svc.submit(MaterialType.IMAGE, oss_key="img/x.png")
    report = svc.run(job)
    assert report.segments[0].source_type == TextSourceType.IMAGE_CONTENT
    assert report.verdict == AuditStatus.PASS


def test_audio_chain_transcribes():
    svc, _, _ = _svc()
    report = svc.run(svc.submit(MaterialType.AUDIO, oss_key="a/x.mp3"))
    assert all(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert len(report.segments) >= 1


def test_video_chain_saves_frames_as_materials():
    llm = FakeLlm(response={"moments_ms": [1000, 3000]})
    svc, repo, _ = _svc(llm=llm)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    # 转写段 + 帧段都在报告里
    assert any(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)
    # 帧顺带自动存成物料(owner u1)
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert len(saved) >= 1


def test_report_persisted_and_material_updated():
    repo = InMemoryMaterialRepo()
    m = Material(id="m1", type=MaterialType.CORPUS, thumb="", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", description="含赌博字样")
    repo.save(m)
    rule = AuditRule(id="r1", source_type="any", condition="含赌博相关内容", action="block")
    svc, repo2, reports = _svc(llm=_findings((1, 1, "含赌博")), rules=[rule], repo=repo)
    job = svc.submit(MaterialType.CORPUS, material_id="m1")
    report = svc.run(job, text="含赌博字样")
    assert repo.get("m1").audit_status == AuditStatus.REVIEW   # 机器命中→待人工复核(不 block)
    assert repo.get("m1").audit_report_id  # 报告 id 写回
    assert reports.get(repo.get("m1").audit_report_id).verdict == AuditStatus.REVIEW


def test_content_safety_flags_review():
    from app.infrastructure.fakes import FakeBlockAuditor
    # 内容安全发现问题 → 机器转 review(不直接 block),命中项进报告
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png"))
    assert report.verdict == AuditStatus.REVIEW
    assert any(t["rule_id"] == "content-safety" for t in report.triggered)


def test_content_safety_pass_is_noop():
    from app.infrastructure.fakes import FakePassAuditor
    # 内容安全放行(如未开通的假实现)→ 不影响,仍按规则(此处无规则)pass
    svc, _, _ = _svc(auditor=FakePassAuditor())
    report = svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png"))
    assert report.verdict == AuditStatus.PASS


def test_audit_generates_summary_and_tags():
    repo = InMemoryMaterialRepo()
    m = Material(id="m1", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="img/x.png", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo)
    svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png", material_id="m1"))
    got = repo.get("m1")
    assert got.ai_summary and got.ai_emotions and got.ai_atmosphere and got.ai_scenarios  # 多值非空
    assert "测试" in got.tags   # 档案返回的标签


def test_summarize_material_on_demand():
    repo = InMemoryMaterialRepo()
    m = Material(id="m2", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="img/y.png", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo)
    svc.summarize_material(m)
    assert repo.get("m2").ai_summary and repo.get("m2").ai_emotions


def test_apply_summary_uses_archiver_for_media():
    # 图片/视频物料 → 走豆包档案器(直接看媒体),而非 qwen 文本
    from app.infrastructure.fakes import FakeArchiver
    arch = FakeArchiver(response={"summary": "档案", "emotions": ["得意", "调侃"],
                                  "scenarios": ["群里炫耀想回怼时", "转场搞笑停顿时"],
                                  "atmosphere": "轻松", "tags": ["表情", "梗图"]})
    repo = InMemoryMaterialRepo()
    m = Material(id="mi", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="img/x.png", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo, archiver=arch)
    svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png", material_id="mi"))
    got = repo.get("mi")
    assert got.ai_emotions == ["得意", "调侃"] and got.ai_scenarios[0] == "群里炫耀想回怼时"
    assert arch.calls and arch.calls[0][0] == "image"        # 档案器真被调用(媒体走豆包)
    # 语料(无媒体)→ 不走档案器,走 qwen 文本兜底
    arch2 = FakeArchiver()
    svc2, repo2, _ = _svc(archiver=arch2)
    svc2.run(svc2.submit(MaterialType.CORPUS, material_id="c1"), text="一段文字")
    assert arch2.calls == []                                 # 纯文本不喂豆包


def test_material_from_dict_migrates_old_scene_emotion():
    from app.infrastructure.jsonstore import Store
    old = {"id": "o1", "type": "image", "thumb": "", "source_timecode": 0.0, "embedding": [],
           "audit_status": "pass", "source_job": "", "ai_scene": "开场", "ai_emotion": "欢快"}
    m = Store._mat_from_dict(old)
    assert m.ai_scenarios == ["开场"] and m.ai_emotions == ["欢快"]   # 老单值 → 单元素列表
    assert not hasattr(m, "ai_scene")


def test_doubao_archiver_parses_and_fails_safe():
    from app.infrastructure.doubao_ark import parse_archive, DoubaoArchiver
    got = parse_archive('```json\n{"summary":"s","emotions":["得意","得意","无语"],'
                        '"scenarios":["A时","B时"],"atmosphere":"轻松","tags":["x"]}\n```')
    assert got["emotions"] == ["得意", "无语"] and got["scenarios"] == ["A时", "B时"]  # 去重 + 多值
    assert parse_archive("非 JSON 垃圾") == {}
    # tag() 打不通真 ARK(假 key/地址)→ 兜底返回 {},不抛
    assert DoubaoArchiver("k", "m", "http://127.0.0.1:9/none").tag("image", media_url="http://x") == {}


def test_song_name_from_oss_key():
    f = AuditPipelineService._song_name_from
    assert f("materials/" + "a" * 32 + "-晴天.mp3") == "晴天"          # 去 uuid 前缀 + 扩展名
    assert f("audit/" + "b" * 32 + "-successful-mix.mp3") == "successful-mix"  # 歌名含 '-' 也 OK
    assert f("materials/" + "c" * 32 + "-无扩展名歌") == "无扩展名歌"
    assert f("") == ""


def test_music_uses_tavily_archive():
    # 音乐物料 → 用歌名联网搜(Tavily)→ 大模型合成情绪/场景多值档案
    tav = _StubTavily()
    llm = FakeLlm(response={"summary": "治愈民谣", "emotions": ["治愈", "温暖", "怀旧"],
                            "scenarios": ["旅行日落空镜", "情侣回忆杀"], "atmosphere": "温情",
                            "tags": ["民谣", "治愈"]})
    repo = InMemoryMaterialRepo()
    m = Material(id="mu", type=MaterialType.MUSIC, thumb="", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="",
                 oss_key="materials/" + "d" * 32 + "-晴天.mp3", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(llm=llm, repo=repo, tavily=tav)
    svc.run(svc.submit(MaterialType.MUSIC, oss_key=m.oss_key, material_id="mu"))
    got = repo.get("mu")
    assert got.ai_emotions == ["治愈", "温暖", "怀旧"]                  # 情绪来自联网合成
    assert got.ai_scenarios[0] == "旅行日落空镜"                       # 场景来自联网合成
    assert tav.calls and "晴天" in tav.calls[0]                        # 确实用歌名联网搜过


def test_music_without_tavily_falls_back_to_text():
    # 未配 tavily → 音乐不联网,退回 qwen 文本档案(转写),不报错、仍出档案
    repo = InMemoryMaterialRepo()
    m = Material(id="mu2", type=MaterialType.MUSIC, thumb="", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="",
                 oss_key="materials/" + "e" * 32 + "-歌.mp3", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo, tavily=None)
    svc.run(svc.submit(MaterialType.MUSIC, oss_key=m.oss_key, material_id="mu2"))
    assert repo.get("mu2").ai_emotions                                # 有档案(qwen 文本兜底)


def test_rule_repo_list_for():
    rr = InMemoryAuditRuleRepo()
    rr.add(AuditRule(id="a", source_type="any", keywords=["x"]))
    rr.add(AuditRule(id="t", source_type=TextSourceType.TRANSCRIPT.value, keywords=["y"]))
    ids = {r.id for r in rr.list_for(TextSourceType.TRANSCRIPT.value)}
    assert ids == {"a", "t"}
    ids2 = {r.id for r in rr.list_for(TextSourceType.IMAGE_CONTENT.value)}
    assert ids2 == {"a"}  # transcript 规则不适用图像


# ── 视频分「物料 / 作品」两条链路 ──
from app.service.audit_pipeline import _MOMENT_RULE_SYS, _MOMENT_SYS, _WORK_MAX_FRAMES


def test_material_moments_tiers():
    M = AuditPipelineService._material_moments
    assert M(4000) == [2000]                       # ≤5s → 1 帧(中)
    assert len(M(8000)) == 2                        # ≤10s → 2 帧
    assert M(16000) == [4000, 8000, 12000]          # ≤20s → 3 帧(25/50/75%)
    assert M(0) == [500] and M(None) == [500]       # 拿不到时长 → 1 帧


def test_safety_net_sparse_and_capped():
    N = AuditPipelineService._safety_net
    pts = N(120000)                                 # 2 分钟 → ~6 帧均匀
    assert 3 <= len(pts) <= _WORK_MAX_FRAMES and all(0 < p < 120000 for p in pts)
    assert N(0) == [1000]
    assert len(N(600000)) <= _WORK_MAX_FRAMES        # 10 分钟也封顶


def test_material_path_saves_frames():
    svc, repo, _ = _svc()
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))  # 默认 material
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert len(saved) >= 1                          # 物料:帧存成素材


def test_work_path_does_not_save_frames():
    llm = FakeLlm(response={"moments_ms": [1000, 3000]})
    svc, repo, _ = _svc(llm=llm)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)  # 帧仍被核验
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert saved == []                              # 作品:帧不入库


def test_work_moment_prompt_includes_rules():
    # 作品抽帧点按「画面拦截规则」反推:规则清单要出现在给 LLM 的提示里
    rule = AuditRule(id="rv", source_type=TextSourceType.VIDEO_FRAME.value,
                     keywords=["刀", "血"], condition="出现血腥暴力画面", action="review")
    llm = FakeLlm()   # 默认对含「时间段」的 system 返回 {"moments_ms":[]},但会记录调用
    svc, _, _ = _svc(llm=llm, rules=[rule])
    svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(sys == _MOMENT_RULE_SYS and ("血腥暴力" in usr or "刀" in usr) for sys, usr in llm.calls)


def test_work_no_transcript_uses_net_only():
    class _EmptyTx:
        def transcribe(self, url): return []
    llm = FakeLlm()
    rr = InMemoryAuditRuleRepo(); repo = InMemoryMaterialRepo()
    svc = AuditPipelineService(_EmptyTx(), FakeVisionDescriber(), llm, rr,
                               InMemoryAuditReportRepo(), FakeStorage(), repo,
                               FakeEmbedder(), InMemoryVectorIndex(), None)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)   # 安全网仍抽到帧
    assert not any(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert not any(sys in (_MOMENT_RULE_SYS, _MOMENT_SYS) for sys, _ in llm.calls)     # 无转写→不问 LLM 时刻


# ── 命中详情:定位到具体文本片段 / 帧图片(报告标红用)──
def test_content_safety_pinpoints_text_segment():
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="某段可疑文字")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety"]
    assert cs and cs[0]["source_type"] == "original_text" and cs[0]["text"] == "某段可疑文字"


def test_content_safety_pinpoints_frame():
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    fr = [t for t in report.triggered if t["rule_id"] == "content-safety" and t["source_type"] == "video_frame"]
    assert fr and fr[0].get("frame_oss_key") and fr[0]["begin_ms"] is not None


# ── 内容安全命中词 riskWords 入报告(供标红 + 一键加白)──
class _WordAuditor:
    """审核器:可选实现 audit_detail 交出命中词(文本才有,图片无)。"""
    def audit(self, content) -> str:
        return "review"
    def audit_detail(self, content):
        return ("review", "杀人犯,枪") if getattr(content, "description", "") else ("review", "")


def test_content_safety_surfaces_risk_words():
    svc, _, _ = _svc(auditor=_WordAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="我本可以成为杀人犯")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety" and t.get("risk_words")]
    assert cs and "杀人犯" in cs[0]["risk_words"]


def test_content_safety_no_audit_detail_still_works():
    # 审核器只有 audit()(如 fakes)→ getattr 兜底,不报错,risk_words 缺省
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="某段文字")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety"]
    assert cs and cs[0].get("risk_words", "") == ""


# ── recheck:只重判,画面用当前 vision 提示词重新反解,不重抽帧/转写/不重复生成素材 ──
class _ToggleAuditor:
    def __init__(self):
        self.verdict = "block"
    def audit(self, content) -> str:
        return self.verdict


def test_recheck_reevaluates_without_reextraction():
    """重审不重转写/抽帧/入库帧素材;但画面反解(VIDEO_FRAME/IMAGE_CONTENT)用当前 vision 重跑。"""
    from app.domain.models import TextSegment
    class _RecTx:
        def __init__(self): self.n = 0
        def transcribe(self, url):
            self.n += 1
            return [TextSegment(TextSourceType.TRANSCRIPT, "对白", begin_ms=0)]
    class _RecVision:
        def __init__(self): self.n = 0
        def describe_image(self, url, hints: str = ""):
            self.n += 1
            return f"画面内容-第{self.n}次"
    tx, vi, aud = _RecTx(), _RecVision(), _ToggleAuditor()
    repo = InMemoryMaterialRepo()
    svc = AuditPipelineService(tx, vi, FakeLlm(response={"moments_ms": [1000]}),
                               InMemoryAuditRuleRepo(), InMemoryAuditReportRepo(), FakeStorage(),
                               repo, FakeEmbedder(), InMemoryVectorIndex(), aud)
    rep = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    assert rep.verdict == AuditStatus.REVIEW   # 内容安全发现问题 → 机器转人工(不 block)
    n_mats, tx_calls, vi_calls = len(repo.list()), tx.n, vi.n
    assert n_mats >= 1                                    # 首审:帧已存成素材

    aud.verdict = "pass"                                  # 模拟"加白后放行"
    rep2 = svc.recheck(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"), rep)
    assert rep2.verdict == AuditStatus.PASS               # 用当前策略重判 → 翻成通过
    assert tx.n == tx_calls                               # 未重新转写
    assert vi.n > vi_calls                                # 画面用当前 vision 重新反解(提示词可能已调整)
    assert len(repo.list()) == n_mats                     # 未新增帧素材(不重复入库)
    # 口播转写段不变,画面段文字已刷新
    tx_segs = [s for s in rep2.segments if s.source_type == TextSourceType.TRANSCRIPT]
    vi_segs = [s for s in rep2.segments if s.source_type == TextSourceType.VIDEO_FRAME]
    assert tx_segs and all(s.text == "对白" for s in tx_segs)
    assert vi_segs and all("画面内容" in s.text for s in vi_segs)
    assert any("第" in s.text for s in vi_segs)           # 确认是重跑后的新结果


def test_vision_prompt_avoids_negative_safety_conclusions():
    from app.infrastructure.qwen_vl import QwenVLVisionDescriber

    prompt = QwenVLVisionDescriber._PROMPT
    assert "请详细描述这张图片的画面内容" in prompt
    assert "任何可能涉及违规的风险点" in prompt
    assert "严禁以任何否定句式罗列不存在的内容" in prompt
    assert "未见" in prompt
