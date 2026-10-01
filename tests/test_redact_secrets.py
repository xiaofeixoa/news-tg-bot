"""别人泄露的账号密码不能出现在任何渲染面上（v1.88）。

真机 2026-10-01 只读探针：1946 行里有 3 行是"邮箱 + 紧跟的 token 串"这个形状，
其中 #1720 一份同时躺在 `summary`、`summary_zh`、`content` 三个字段里——
也就是说 `/免费` 的描述行和卡片正文都可能是它。下面的用例用的是**替身字符串**，
不是他库里那条的真实内容。
"""

from __future__ import annotations

import re

from app.config import get_config
from app.services import format as fmt
from app.services.news import ArticleView

# 替身：形状与真机一致（邮箱 + `----%token@N----token`），内容不是真的
LEAK = "还有半小时就过期 someone1k+001@icloud.com----%T3sTk3n%XV4@N----6KJ6ABCDEF 速蹬兄弟们"
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _view(**kwargs) -> ArticleView:
    base = dict(id=1, title="GPT free share", title_zh="还有半小时就过期",
                summary=None, summary_zh=None, url="https://example.org/1",
                source_name="Linux.do 福利分类", source_type="rss",
                category="AI", subcategory=None)
    base.update(kwargs)
    return ArticleView(**base)


def test_a_leaked_account_survives_no_render_surface():
    config = get_config()
    rendered = [
        fmt.esc(LEAK),
        fmt.link(LEAK[:120], "https://example.org/1"),
        fmt.news_list([_view(summary_zh=LEAK)], config=config, title="🎁 近期免费 / 限免"),
    ]
    for text in rendered:
        assert "someone1k" not in text, text
        assert "T3sTk3n" not in text, text
        assert "6KJ6ABCDEF" not in text, text
        assert "icloud.com" in text, f"域名要留着，读者才知道那是个账号：{text}"


def test_truncation_cannot_dodge_the_redaction():
    """面板是**先截断**再交给 `esc()` 的，所以遮凭据必须在取字段的那一层做。

    第一版我只在 `esc()` 里遮，这条用例当场就红了：`LEAK[:20]` 截到域名中间，
    邮箱正则拼不出来，`someone1k` 原样留在那里。
    """
    for cut in (20, 40, 60, 80, 120):
        head = (_view(summary_zh=LEAK).display_summary or "")[:cut]
        title = _view(title=LEAK, title_zh=LEAK).display_title[:30]
        text = fmt.esc(head) + fmt.esc(title)
        assert "someone1k" not in text and "T3sTk3n" not in text, (cut, text)
        assert "icloud.com" in text, (cut, text)


def test_an_ordinary_email_keeps_reading_like_a_contact():
    """不是所有邮箱都是泄露的凭据：只遮本地部分，别把整句中文一起吞掉。"""
    text = fmt.esc("有问题联系 support@openai.com 就好，谢谢")
    assert "support" not in text, text
    assert "***@openai.com" in text, text
    assert "就好，谢谢" in text, text          # 中文句子必须活着
    assert "有问题" in text, text


def test_a_bare_domain_or_non_text_still_escapes_normally():
    assert fmt.esc(42) == "42"
    assert fmt.esc(None) == ""
    assert fmt.esc("没有邮箱的一行 <b>") == "没有邮箱的一行 &lt;b&gt;"
    assert fmt.esc("at 例 like this@example") == "at 例 like this@example", "缺 TLD 的点不该被当成邮箱"
