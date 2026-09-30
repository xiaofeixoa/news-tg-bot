"""中文检索（2026-09-26 之前的空白区：纯中文问句在活库上全部 0 命中）。

覆盖三条路径：`repo.query_articles(search=)` 要匹配译文库列，长词要拆成二字窗口，
中文品牌词要靠 `search.aliases` 走到库里实际使用的英文写法。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_config
from app.database.models import Article
from app.processing.normalize import article_hash, url_hash
from app.services.search import SearchService, MAX_ALIASES, _subterms, build_pool, clean_query


def add(session, *, title: str, title_zh: str | None = None, summary: str | None = None,
        summary_zh: str | None = None, score: float = 60, hours_ago: float = 2) -> Article:
    url = f"https://example.com/{title}"
    article = Article(
        source_name="Example", source_type="rss", title=title, url=url, normalized_url=url,
        url_hash=url_hash(url), hash=article_hash(title, url),
        content=f"{title} body text", summary=summary, title_zh=title_zh, summary_zh=summary_zh,
        final_score=score, language="en",
        published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago),
    )
    session.add(article)
    session.commit()
    return article


def service() -> SearchService:
    return SearchService(get_config())


# ------------------------------------------------------------------- the bug
def test_a_chinese_query_reaches_the_translated_columns(session):
    add(session, title="Meta releases an open-weight model for agents",
        title_zh="Meta 发布面向 Agent 的开放权重模型", summary_zh="参数规模 700 亿，可商用。")
    found = service().search("开放权重模型", days=7, limit=5)
    assert [a.title_zh for a in found] == ["Meta 发布面向 Agent 的开放权重模型"], \
        "用户读的是中文，检索却只看英文列"


def test_a_query_only_present_in_the_english_source_still_hits(session):
    add(session, title="Nvidia GPU prices rise again", title_zh="英伟达 GPU 价格再次上涨")
    assert len(service().search("GPU", days=7, limit=5)) == 1


def test_chinese_asking_about_an_english_only_row_does_not_lie(session):
    """没有译文的旧行，中文问句仍然找不到它——这是显示英文的前提，不是静默漏掉。"""
    add(session, title="A paper about routing", title_zh=None)
    assert service().search("路由", days=7, limit=5) == []


# ------------------------------------------------------- compound fallback
def test_a_compound_term_falls_back_to_two_character_windows(session):
    """译文里不会出现连写的「模型发布」，但会分别出现「模型」和「发布」。"""
    add(session, title="Alibaba ships Qwen 3", title_zh="阿里发布新一代 Qwen 大模型",
        summary_zh="上下文窗口扩大一倍。")
    found = service().search("模型发布", days=7, limit=5)
    assert [a.title_zh for a in found] == ["阿里发布新一代 Qwen 大模型"]


def test_rows_matching_more_windows_rank_first(session):
    both = add(session, title="Both", title_zh="本周发布了两款推理模型", summary_zh="推理成本下降。")
    add(session, title="One", title_zh="开源模型许可证争议", summary_zh="条款不明确。")
    found = service().search("模型发布", days=7, limit=5)
    assert [a.id for a in found][:1] == [both.id], "命中两个字窗的行应该排在只命中一个的前面"


def test_the_windows_cannot_invent_a_hit(session):
    """只命中一个二字窗口的行不算答案：活库里「量子隧穿」曾靠"量子"捞回三条量子计算新闻。"""
    add(session, title="A quantum computing milestone", title_zh="量子计算进入新阶段")
    assert service().search("量子隧穿", days=7, limit=5) == []


def test_two_windows_together_do_count(session):
    add(session, title="Tunnelling demonstrated", title_zh="在量子硬件上演示了隧穿效应")
    assert len(service().search("量子隧穿", days=7, limit=5)) == 1


def test_english_queries_keep_the_single_pass_path(session):
    add(session, title="Mistral raises 1.7B for robotics", title_zh="Mistral 为机器人方向融资")
    found = service().search("robotics", days=7, limit=5)
    assert len(found) == 1


# ---------------------------------------------------------------- aliases
def test_a_chinese_brand_name_reaches_the_latin_spelling_the_library_uses(session):
    """译文里 NVIDIA 按设计不译：他打「英伟达」时得靠对照表走过去，而不是回答没有。"""
    add(session, title="NVIDIA ships a 48GB workstation card",
        title_zh="NVIDIA 推出 48GB 工作站显卡", score=70)
    found = service().search("英伟达", days=7, limit=5)
    assert [a.title_zh for a in found] == ["NVIDIA 推出 48GB 工作站显卡"]


def test_an_english_word_reaches_the_chinese_spelling(session):
    add(session, title="A new silicon package for laptops", title_zh="面向笔记本的新款芯片封装")
    assert len(service().search("chip", days=7, limit=5)) == 1


def test_a_row_carrying_both_spellings_outranks_one_that_only_has_the_alias(session):
    both = add(session, title="NVIDIA 英伟达 announces B200 supply",
               title_zh="NVIDIA（英伟达）宣布 B200 供货", score=50)
    alias_only = add(session, title="NVIDIA data centre revenue grows",
                     title_zh="NVIDIA 数据中心营收增长", score=90)
    found = service().search("英伟达", days=7, limit=5)
    assert [a.id for a in found] == [both.id, alias_only.id], \
        "只命中对照词的行分数再高，也不该排在真正含用户原词的那行前面"


def test_a_two_concept_query_has_to_answer_both_concepts(session):
    """「芯片涨价了吗」的第一条曾经是一行只讲价格的新闻——那是半个问题的答案。"""
    add(session, title="The price tag is bigger than expected",
        title_zh="价格标签明显高于先前的价格", score=90)
    add(session, title="A new chip package", title_zh="新的芯片封装工艺", score=95)
    both = add(session, title="Chip prices keep climbing", title_zh="芯片与内存价格继续上涨")
    found = service().search("芯片涨价了吗", days=7, limit=5)
    assert [a.id for a in found] == [both.id]


def test_a_single_concept_query_is_not_held_to_that_bar(session):
    """只有一个概念时，命中它就够——否则「英伟达」又要退回 0 条。"""
    add(session, title="NVIDIA buys a networking startup", title_zh="NVIDIA 收购一家网络初创公司")
    assert len(service().search("英伟达", days=7, limit=5)) == 1


# ------------------------------------------------------------------- units
def test_pool_weights_prefer_what_was_actually_typed():
    pool = build_pool(["模型发布"], "模型发布", get_config())
    assert pool["模型发布"] == 3, "整句与原词同形时按整句算"
    assert pool["模型"] == 1 and pool["发布"] == 1
    question = build_pool(["模型发布"], "最近有哪些模型发布？", get_config())
    assert question["模型发布"] == 2 and question["模型"] == 1
    assert "NVIDIA" not in pool, "没碰到的对照组不该进候选"


def test_alias_expansion_is_capped():
    """一次检索的 LIKE 次数是有界的，否则 VPS 上搜索会自己变成负载来源。"""
    query = "英伟达 芯片 机器人 显卡 融资 价格"
    terms = clean_query(query)
    pool = build_pool(terms, query, get_config())
    expansions = [text for text, weight in pool.items() if weight == 2]
    assert len(expansions) - (len(terms) + 1) <= MAX_ALIASES


def test_windows_are_only_cut_where_a_word_can_start_and_end():
    assert _subterms(["芯片"]) == [], "两字词本身就是完整候选，不该再切"
    assert _subterms(["模型发布"]) == ["模型", "型发", "发布"]
    assert _subterms(["大模型推理"]) == ["大模", "模型", "型推", "推理"]
    assert _subterms(["deepseek pricing"]) == []


def test_de_is_the_only_splitter():
    """「和/与/了」看着像助词，切下去会造出「饱」「相」这种假词。"""
    assert _subterms(["行业的定价"]) == ["行业", "定价"]
    assert _subterms(["行业饱和度"]) == ["行业", "业饱", "饱和", "和度"]
    assert _subterms(["AI芯片"]) == ["芯片"], "混排词里中文那一半是能用的候选"


def test_clean_query_strips_chinese_noise_words():
    assert clean_query("最近有哪些模型发布？") == ["模型发布"]
    assert clean_query("最近 AI Agent 有什么值得关注的") == ["AI", "Agent"]


def test_clean_query_drops_question_tails():
    assert clean_query("开源模型有哪些") == ["开源模型"]
    assert clean_query("芯片涨价了吗") == ["芯片涨价"]
    assert clean_query("饱和度") == ["饱和度"], "结尾的度不是语气词"


def test_a_row_stored_with_the_wrong_sense_is_still_found_by_the_right_word(session):
    """v1.33 在渲染层改字，库里存量仍写着"模特"；检索读的是库列，不能跟着变瞎。

    这行故意让中文列里**只有**"模特"、一个"模型"都没有：如果测试数据顺手写上"模型"，
    它就在验证 LIKE 命中而不是验证别名表 —— 第一版就是这样假绿的。
    """
    add(session, title="OpenAI pauses training of its most capable ones",
        title_zh="OpenAI暂停其“最有能力模特”的训练",
        summary_zh="有关它失控的报道越堆越多，该公司决定暂停……")

    found = service().search("模型", days=7, limit=5)
    assert [a.title for a in found] == ["OpenAI pauses training of its most capable ones"], \
        "用他看到的正确写法搜，要能捞到库里存了错译的那行（靠 search.aliases 的 model 组）"
    # 反过来：他照着旧简报里的"模特"去搜，也该走到 AI 行的方向
    assert [a.title for a in service().search("模特", days=7, limit=5)] == \
        ["OpenAI pauses training of its most capable ones"]


# ------------------------------------ 标题要说"命中几条"，不是"这一页有几条"
def _rich_nvidia(session, *, n: int) -> None:
    """每一条都同时含 NVIDIA / GPU / 模型，保证权重过 `MIN_MATCH_WEIGHT` 那道槛。"""
    for i in range(n):
        add(session, title=f"Nvidia releases GPU model {i} for agents",
            title_zh=f"Nvidia 发布第 {i} 个 GPU 模型",
            summary="Nvidia says the new GPU model lowers inference cost.",
            summary_zh=f"Nvidia 表示这款 GPU 模型降低了推理成本，第 {i} 版。",
            hours_ago=0.5 + i * 0.1)          # i 越大越早


def test_search_result_keeps_the_answer_and_the_page_apart(session):
    from app.services.search import SearchResult

    _rich_nvidia(session, n=5)
    result = service().search_result("NVIDIA", days=7, limit=2)
    assert isinstance(result, SearchResult)
    assert result.matched == 5, f"命中数被页大小截断了：{result.matched}"
    assert len(result.items) == 2, result.items
    assert result.pool_capped is False, result
    # 权重与分数相同的这一批，页取的仍是"最新的那两条"（先按 published_at 排过）
    assert [a.title for a in result.items] == [
        "Nvidia releases GPU model 0 for agents",
        "Nvidia releases GPU model 1 for agents"], [a.title for a in result.items]
    assert len(service().search("NVIDIA", days=7, limit=2)) == 2, "老契约不能变"


def test_a_wide_query_is_no_longer_counted_only_within_one_page(session):
    """/search 用 limit=2 时旧实现每词只读 6 行，标题最多也就敢说 6 条。"""
    _rich_nvidia(session, n=9)
    result = service().search_result("NVIDIA", days=7, limit=2)
    assert result.matched == 9, result.matched
    assert len(result.items) == 2, result.items


def test_a_pool_that_fills_up_says_so_instead_of_looking_final(session):
    from app.config import AppConfig
    from app.services.search import SearchService

    _rich_nvidia(session, n=6)
    cfg = AppConfig(settings=get_config().settings,
                    raw={"search": {"pool_per_term": 3}}, sources=[])
    result = SearchService(cfg).search_result("NVIDIA", days=7, limit=2)
    assert result.pool == 3 and result.pool_capped is True, result
    assert result.matched < 6, f"候选被截断却没承认：{result.matched}"


def test_the_search_title_separates_the_hit_count_from_the_page():
    from app.services import format as fmt

    whole = fmt.search_title("Claude", matched=245, shown=20, days=30)
    assert "命中 245 条" in whole and "这里列出前 20 条" in whole, whole
    exact = fmt.search_title("英伟达", matched=3, shown=3, days=30)
    assert "命中 3 条" in exact and "列出" not in exact, exact
    capped = fmt.search_title("AI", matched=400, shown=20, days=30,
                              pool_capped=True, pool=400)
    assert "命中 400 条" in capped and "关键词越宽这个数越保守" in capped, capped
