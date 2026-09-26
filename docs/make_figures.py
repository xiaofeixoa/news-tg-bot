"""Render the handbook figures from the real captured deployment output.

    python docs/make_figures.py

Docs tooling only - Pillow is not a runtime dependency of the bot.
Figures are drawn from the actual command output recorded during the deploy on
the US VPS, so every number and log line in them is real.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "images"
FONTS = Path("C:/Windows/Fonts")
MONO = str(FONTS / "consola.ttf")
SANS = str(FONTS / "msyh.ttc")

BG = (18, 22, 28)
PANEL = (24, 29, 37)
FG = (223, 229, 237)
DIM = (140, 150, 163)
ACCENT = (96, 165, 250)
GOOD = (74, 222, 128)
BAD = (248, 113, 113)
WARN = (251, 191, 36)
TITLEBAR = (37, 43, 53)

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d\U0001F1E6-\U0001F1FF\u2B00-\u2BFF]"
)


def font(size: int, *, mono: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(MONO if mono else SANS, size)


def is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def measure(draw, text, f) -> int:
    return int(draw.textlength(text, font=f))


def terminal(name: str, title: str, lines: list[str], *, width: int = 1180,
             caption: str = "") -> None:
    """A dark terminal window containing real command output."""
    pad, line_h, header_h = 26, 26, 52
    f_body = font(17)
    f_body_m = font(17, mono=True)
    f_head = font(18)

    probe = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(probe)
    # wrap long lines to the window width before sizing the image
    rendered: list[tuple[str, tuple]] = []
    for raw in lines:
        text = EMOJI_RE.sub("", raw).rstrip()
        f = f_body_m if is_ascii(text) else f_body
        limit = width - pad * 2 - 12
        if not text:
            rendered.append(("", f))
            continue
        words = text.split(" ")
        chunk = ""
        for word in words:
            trial = word if not chunk else chunk + " " + word
            if measure(d, trial, f) > limit and chunk:
                rendered.append((chunk, f))
                chunk = word
            else:
                chunk = trial
        rendered.append((chunk, f))

    height = header_h + len(rendered) * line_h + pad + (34 if caption else 0)
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=14, fill=PANEL, outline=(45, 52, 63))
    d.rectangle([0, 0, width, header_h - 6], fill=TITLEBAR)
    for index, colour in enumerate((BAD, WARN, GOOD)):
        d.ellipse([22 + index * 26, 18, 38 + index * 26, 34], fill=colour)
    d.text((width // 2 - measure(d, title, f_head) // 2, 15), title, font=f_head, fill=DIM)

    y = header_h + 8
    for text, f in rendered:
        colour = FG
        if text.startswith("$") or text.startswith("#"):
            colour = ACCENT
        elif "OK" in text or "PASS" in text or "已送达" in text or "passed" in text:
            colour = GOOD
        elif "FAIL" in text or "Error" in text or "error" in text or "Killed" in text or "denied" in text:
            colour = BAD
        d.text((pad, y), text, font=f, fill=colour)
        y += line_h
    if caption:
        d.text((pad, height - 32), caption, font=font(15), fill=DIM)
    img.save(OUT / name)
    print(f"{name}: {width}x{height}")


def card(name: str, title: str, rows: list[tuple[str, str, str]], *, width: int = 1180,
         note: str = "") -> None:
    """A labelled two-column panel used for flows and checklists."""
    f_title, f_key, f_val = font(22), font(18), font(17)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    row_h = 46
    height = 88 + len(rows) * row_h + (44 if note else 20)
    img = Image.new("RGB", (width, height), (15, 19, 25))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=16, fill=PANEL, outline=(48, 56, 68))
    d.text((28, 26), title, font=f_title, fill=FG)
    d.rectangle([28, 66, 28 + 64, 70], fill=ACCENT)
    y = 86
    for label, value, tone in rows:
        colour = {"ok": GOOD, "bad": BAD, "warn": WARN, "info": ACCENT, "": FG}.get(tone, FG)
        d.text((28, y), label, font=f_key, fill=DIM)
        d.text((420, y), value, font=f_val, fill=colour)
        y += row_h
    if note:
        d.text((28, height - 38), note, font=font(15), fill=DIM)
    img.save(OUT / name)
    print(f"{name}: {width}x{height}")


def flow(name: str, title: str, blocks: list[tuple[str, list[str]]], *, width: int = 1180) -> None:
    """Horizontal pipeline diagram with a note box under each stage."""
    f_head, f_body = font(20), font(15)
    height = 300
    img = Image.new("RGB", (width, height), (15, 19, 25))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=16, fill=PANEL, outline=(48, 56, 68))
    d.text((28, 22), title, font=font(22), fill=FG)
    d.rectangle([28, 60, 92, 64], fill=ACCENT)

    gap = 18
    box_w = (width - 56 - gap * (len(blocks) - 1)) // len(blocks)
    x = 28
    for index, (head, notes) in enumerate(blocks):
        d.rounded_rectangle([x, 92, x + box_w, 172], radius=10, fill=(32, 39, 50), outline=(64, 74, 89))
        tw = measure(d, head, f_head)
        d.text((x + box_w // 2 - tw // 2, 120), head, font=f_head, fill=ACCENT)
        ny = 184
        for line in notes:
            colour = GOOD if line.startswith("✓") else (BAD if line.startswith("✗") else DIM)
            d.text((x + 4, ny), line, font=f_body, fill=colour)
            ny += 22
        if index < len(blocks) - 1:
            ax = x + box_w + 3
            d.line([ax, 132, ax + gap - 6, 132], fill=(90, 102, 120), width=2)
            d.polygon([(ax + gap - 6, 126), (ax + gap + 2, 132), (ax + gap - 6, 138)], fill=(90, 102, 120))
        x += box_w + gap
    img.save(OUT / name)
    print(f"{name}: {width}x{height}")


def _wrap(draw, text: str, f, limit: int) -> list[str]:
    """Greedy wrap that also works for Chinese, which has no spaces to break on."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        chunk = ""
        for token in re.findall(r"\S+\s*|\s+", paragraph):
            for piece in ([token] if measure(draw, token, f) <= limit else
                          [ch for ch in token]):
                trial = chunk + piece
                if measure(draw, trial, f) > limit and chunk:
                    lines.append(chunk.rstrip())
                    chunk = piece.lstrip() if piece.strip() else ""
                else:
                    chunk = trial
        lines.append(chunk.rstrip())
    return lines


def phone(name: str, bubbles: list[str], *, footer: str = "", width: int = 760) -> None:
    """Telegram-style chat mock rendered from the message text actually sent."""
    f = font(16)
    left, right = 90, width - 26
    inner_left, limit = left + 16, right - left - 32
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    wrapped = [_wrap(probe, EMOJI_RE.sub('', text), f, limit) for text in bubbles]
    gap, pad_v = 18, 13
    height = 84 + sum(len(lines) * 24 + pad_v * 2 for lines in wrapped) + gap * len(bubbles) + 56
    img = Image.new("RGB", (width, height), (17, 22, 29))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=22, fill=(21, 27, 36), outline=(52, 61, 74))
    d.rectangle([0, 0, width, 64], fill=(30, 38, 49))
    d.text((24, 18), "新闻机器人  ·  @your_news_bot", font=font(19), fill=FG)
    d.text((24, 43), "online", font=font(13), fill=GOOD)

    y = 84
    for lines in wrapped:
        bubble_h = len(lines) * 24 + pad_v * 2
        d.rounded_rectangle([left, y, right, y + bubble_h], radius=14, fill=(43, 66, 92))
        # little tail on the right edge, Telegram outgoing style
        d.polygon([(right, y + bubble_h - 12), (right + 12, y + bubble_h), (right, y + bubble_h)],
                  fill=(43, 66, 92))
        ty = y + pad_v
        for line in lines:
            d.text((inner_left, ty), line, font=f, fill=(233, 240, 248))
            ty += 24
        y += bubble_h + gap
    if footer:
        d.text((24, height - 40), footer, font=font(14), fill=DIM)
    img.save(OUT / name)
    print(f"{name}: {width}x{height}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    flow("01-architecture.png", "AI News Radar 数据流（本机实测）", [
        ("数据源", ["28 个配置源", "17 个已启用", "rss/hn/github", "arxiv/reddit/yt"]),
        ("Collector", ["插件式注册", "超时+重试", "单源失败隔离"]),
        ("去重", ["URL SHA-1 唯一", "标题相似度", "事件合并"]),
        ("SQLite", ["412 条入库", "245 条已处理", "WAL + 写锁"]),
        ("AI Pipeline", ["关键词门 22 条", "规则模式（无 key）", "评分/摘要/标签"]),
        ("Telegram", ["long polling", "早报/突发", "15 个命令"]),
    ])

    terminal("03-ssh-ipv6.png", "第一步：连不上 —— IPv6 直连被中间链路切断", [
        "# 本机直连（TCP 能通，但对方不发 SSH banner 就关闭）",
        "$ ssh -vv root@2001:db8:9a40:9210:4::e5 true",
        "debug1: Connecting to 2001:db8:9a40:9210:4::e5 [2001:db8:9a40:9210:4::e5] port 22.",
        "kex_exchange_identification: Connection closed by remote host",
        "Connection closed by 2001:db8:9a40:9210:4::e5 port 22",
        "",
        "# 从局域网那台 Debian 服务器试，握手完全正常（说明是链路问题，不是对方 sshd 问题）",
        "root@192.0.2.10:~$ ssh -o BatchMode=yes root@2001:db8:9a40:9210:4::e5 true",
        "Warning: Permanently added '2001:db8:9a40:9210:4::e5' (ED25519) to the list of known hosts.",
        "root@2001:db8:9a40:9210:4::e5: Permission denied (publickey,password).   <- 到达了真 sshd",
        "",
        "# 解决：把局域网机器当跳板，写进 ~/.ssh/config",
        "Host anr-vps",
        "    HostName 2001:db8:9a40:9210:4::e5",
        "    User root",
        "    IdentityFile ~/.ssh/ai_news_radar_deploy",
        "    StrictHostKeyChecking accept-new",
        "    ProxyCommand ssh -i ~/.ssh/ai_news_radar_deploy -o BatchMode=yes \\",
        "        -o StrictHostKeyChecking=accept-new -W [2001:db8:9a40:9210:4::e5]:22 root@192.0.2.10",
        "",
        "$ ssh anr-vps 'hostname; cat /etc/debian_version'",
        "PROXYJUMP_OK",
        "news-vps",
        "11.2",
    ], caption="首次用密码把公钥写进 authorized_keys（paramiko 经 direct-tcpip 通道），之后全程密钥认证。")

    terminal("04-facts.png", "第二步：装机前的体检（结论：能装，但内存是硬约束）", [
        "$ ssh anr-vps '... 系统 + 外发探测 ...'",
        "=== OS ===            Debian GNU/Linux 11 (bullseye) 11.2   x86_64",
        "=== CPU/MEM/DISK ===  2 核   Mem: total 475  used 238  available 154   /  30G used 1.4G",
        "=== SWAP ===          （空） -> NO SWAP",
        "=== PYTHON ===        Python 3.9.2   /   ModuleNotFoundError: No module named 'ensurepip'",
        "=== TOOLS ===         git ✓  curl ✓  systemctl ✓   rsync ✗   sqlite3 ✗   docker ✗",
        "=== LOCATION ===      Salt Lake City, US   (AS13335 Cloudflare)",
        "",
        "=== OUTBOUND: telegram ===",
        "api.telegram.org -> 302 in 0.409122s            <- 关键：这台能直连 Telegram",
        "=== OUTBOUND: pypi + feeds ===",
        "https://pypi.org/simple/feedparser/             200 0.109250s",
        "https://openai.com/news/rss.xml                 200 0.330009s",
        "https://hn.algolia.com/api/v1/search?...        200 0.353907s",
        "http://export.arxiv.org/api/query?...           301 0.124705s",
        "https://huggingface.co/blog/feed.xml            200 0.300985s",
        "https://venturebeat.com/category/ai/feed/       429 0.176882s   <- 对方限流，隔离处理",
    ], caption="对比：局域网那台 Debian 13 上 openai.com/hn.algolia.com/api.telegram.org 全部不通；这台美西 VPS 全通。")

    terminal("05-python.png", "第三步：Python 版本坑（apt 假匹配 -> uv 独立构建）", [
        "# 需求：代码用了 SQLAlchemy 的 Mapped[str | None]，运行时要求 Python >= 3.10",
        "$ apt-cache policy python3.13",
        "  Candidate: 13.16-0+deb11u1        <- 看起来有 3.13，其实是 PostgreSQL 13 的 plpython 扩展",
        "",
        "$ apt-get install -y python3.13",
        "Setting up postgresql-plpython3-13 (13.16-0+deb11u1) ...",
        "$ python3.13 -V",
        "bash: line 1: python3.13: command not found",
        "$ apt-get remove -y postgresql-plpython3-13          <- 立刻回滚误装",
        "",
        "$ apt-get install -y python3.11 python3.11-venv      # backports 里也没有",
        "E: Unable to locate package python3.11-venv",
        "",
        "# 改用 uv 拉一份独立 CPython（不动系统 Python，也免编译）",
        "$ curl -LsSf https://astral.sh/uv/install.sh | sh",
        "uv 0.12.19 (x86_64-unknown-linux-gnu)",
        "$ UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install 3.12",
        "Installed Python 3.12.14 in 5.11s",
        " + cpython-3.12.14-linux-x86_64-gnu",
        "$ cd /opt/ai-news-radar && uv venv --python 3.12 .venv",
        "Creating virtual environment at: .venv",
        "$ .venv/bin/python -V",
        "Python 3.12.14",
    ], caption="把 /opt/uv-python 设为全局可读（chmod -R a+rX），否则 systemd 里的 news 用户找不到解释器。")

    terminal("06-online.png", "第四步：部署 + systemd 拉起，Bot 上线", [
        "$ sudo -u news .venv/bin/python scripts/init_db.py",
        "database ready : sqlite:////opt/ai-news-radar/data/news.db",
        "tables         : article_tags, articles, events, push_logs, sources, tags, user_interests, users",
        "sources        : 28 registered, 17 enabled",
        "",
        "$ cp deploy/ai-news-radar.service /etc/systemd/system/",
        "$ systemctl daemon-reload && systemctl enable --now ai-news-radar",
        "Created symlink /etc/systemd/system/multi-user.target.wants/ai-news-radar.service",
        "",
        "$ systemctl show ai-news-radar -p ActiveState -p SubState -p MemoryCurrent",
        "ActiveState=active  SubState=running  MemoryCurrent=178397184",
        "",
        "$ journalctl -u ai-news-radar -n 4",
        "Added job \"collect arxiv\" to job store \"default\"",
        "Scheduler started",
        "Start polling",
        "Run polling for bot @your_news_bot id=1234567890 - '新闻机器人'   <- 真连上 Telegram 了",
        "",
        "$ grep -E 'collect\\[|AI processing done' logs/scheduler.log",
        "collect[rss] done: fetched=320 stored=317 dup=3 blocked=0 errors=1",
        "collect[hackernews] done: fetched=24 stored=22 dup=2 blocked=0 errors=0",
        "collect[github] done: fetched=34 stored=34 dup=0 blocked=0 errors=0",
        "collect[arxiv] done: fetched=34 stored=34 dup=0 blocked=0 errors=0",
        "AI processing done: scanned=240 processed=218 filtered=22 breaking=0 failed=0",
    ], caption="注意 errors=1：VentureBeat 429 被隔离成一行日志，其余源照常入库——这正是设计文档 §25 要求的行为。")

    phone("07-telegram.png", [
        "部署自检：AI News Radar 已在 Debian 11 VPS 上通过 systemd 稳定运行，"
        "采集/去重/AI 处理/Telegram 推送链路全部打通。给 Bot 发 /news 即可看今天的 AI 新闻。",
        "☀️ AI Morning Briefing\\n📅 2026-09-25\\n\\n"
        "🔥 今日重点\\n"
        "1. Serve OpenAI-compatible vLLM inference of DeepSeek-V4.1-Flash optimized for 2x DGX Sparks "
        "[https://github.com/.../DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks]\\n"
        "   GitHub Trending · 09-25 19:02 · ⭐77\\n"
        "2. New Features - Added GPT-6 Sol and Luna, including Amazon Bedrock support "
        "[https://github.com/openai/codex/releases/tag/rust-v0.157.0]\\n"
        "   GitHub Releases · 09-25 10:31 · 🔹61\\n\\n"
        "🧠 AI Agent\\n"
        "5. Microsoft thinks its new Copilot 'super app' will be as influential as Office "
        "[https://theverge.com/news/1000532/...]\\n"
        "   The Verge AI · 09-25 20:00 · ▫️56\\n\\n"
        "覆盖 8 条重点 · 最近 24 小时 · 共 412 条入库",
    ], footer="真实送达回执：chat 999999999: 已送达 / 已发送 1/1 条简报消息（气泡内容取自实发文本）")

    terminal("08-oom.png", "事故一：pytest 被 OOM 杀掉（exit=137）", [
        "$ .venv/bin/python -m pytest -q",
        "bash: line 1: 431903 Killed    .venv/bin/python -m pytest -q > /tmp/pytest.log 2>&1",
        "exit=137        0 /tmp/pytest.log",
        "",
        "$ free -m",
        "               total        used        free      shared  buff/cache   available",
        "Mem:             475         238          39          70         197         154",
        "Swap:              0           0           0",
        "$ dmesg | tail",
        "Out of memory: Killed process 431903 (python) total-vm:188616kB, anon-rss:147760kB",
        "$ ps -eo rss,comm --sort=-rss | head -5",
        "143760 mmwx        39296 sing-box        24612 cloudflared    <- 机器上还有你自己的代理业务",
        "",
        "# 处理：加 2G swap 并写进 fstab（不是调大内存，而是给小机器一个缓冲）",
        "$ fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile",
        "$ echo '/swapfile none swap sw 0 0' >> /etc/fstab",
        "$ free -m | tail -1",
        "Swap:           2047           0        2047",
        "$ .venv/bin/python -m pytest -q",
        "........................................................................ [ 82%]",
        "...............                                                         [100%]",
        "87 passed",
    ], caption="结论：这台机器可用，但 475MB 内存 + 已有代理业务，必须留 swap；服务稳态占用约 170-190MB。")

    terminal("09-perm.png", "事故二：我给配置加\"便利\"，反而把服务打进重启循环", [
        "# 为了让手动跑 scripts/*.py 也能读到密钥，我让 pydantic-settings 顺带读",
        "# /etc/ai-news-radar/env —— 但那个文件是 root:root 0600：",
        "#   systemd 的 EnvironmentFile= 由 PID 1(root) 读，没问题；",
        "#   而应用进程以 news 用户运行，自己去 open 就会被拒。",
        "",
        "$ journalctl -u ai-news-radar",
        "ai-news-radar[432293]: 启动失败：[Errno 13] Permission denied: '/etc/ai-news-radar/env'",
        "Main process exited, code=exited, status=2/INVALIDARGUMENT",
        "Scheduled restart job, restart counter is at 3.",
        "ActiveState=activating  SubState=auto-restart",
        "",
        "# 正确做法：只把\"当前进程真能读\"的文件交给 pydantic-settings",
        "def _env_files():",
        "    candidates = (PROJECT_ROOT / \".env\", Path(\"/etc/ai-news-radar/env\"))",
        "    return tuple(str(p) for p in candidates if p.is_file() and os.access(p, os.R_OK))",
        "",
        "$ sudo -u news .venv/bin/python -c \"...get_config()...\"",
        "token visible to news user: False chats: []      <- 不再崩溃；值由 systemd 注入",
        "$ .venv/bin/python -c \"...get_config()...\"      # 以 root 手动跑脚本",
        "token: True chats: [999999999]",
        "$ systemctl reset-failed && systemctl restart ai-news-radar",
        "ActiveState=active  SubState=running",
        "Run polling for bot @your_news_bot id=1234567890 - '新闻机器人'",
    ], caption="教训：改\"配置读取顺序\"这种基础逻辑，一定要同时验证 systemd 用户与 root 两条路径。")

    terminal("11-chinese.png", "中文输出：改造前后的同一条 /news", [
        "# 改造前（规则模式：摘要直接取英文正文首句）",
        "1️⃣ Serve OpenAI-compatible vLLM inference of DeepSeek-V4.1-Flash optimized",
        "   for 2x DGX Sparks, enabling fast, efficient deployment.  ⭐77",
        "2️⃣ Microsoft thinks its new Copilot 'super app' will be as influential as",
        "   Office  ▫️56",
        "",
        "# 改造后（显示时按需翻译 + 后台翻译轮补齐，写回 title_zh / summary_zh）",
        "$ .venv/bin/python scripts/preview.py news",
        "1️⃣ 提供针对2x DGX Sparks优化的DeepSeek-V4.1-Flash的OpenAI兼容vLLM推断，实现快速、高效的部署。 ⭐77",
        "   GitHub Trending · 09-25 19:02 · AI Agent",
        "2️⃣ 在上个月推出其新的Copilot “超级应用”之后，微软今天正式推出它。 ▫️56",
        "   The Verge AI · 09-25 20:00 · AI Agent",
        "3️⃣ 精心策划的可再现AI模型和工具箱库。 ▫️54",
        "   GitHub Trending · 09-25 19:52 · AI Agent",
        "",
        "# 覆盖率与额度（免费 MT 有每日上限，所以是\"边看边补 + 后台慢慢补\"）",
        "$ sqlite3 data/news.db 'select count(title_zh) from articles'",
        "中文标题覆盖: 67 / 可显示 357",
        "",
        "# 接上 LLM 后走批量翻译（一次请求几十条），质量与覆盖率都会明显更好",
        "$ grep LLM_ /etc/ai-news-radar/env",
        "LLM_BASE_URL=  LLM_API_KEY=  LLM_MODEL=      <- 填这三行即可切换到 llm 路线",
    ], caption="provider=auto：有 LLM 用 LLM（批量、术语准确），没有则用免密钥的 MyMemory，并按 per_run_limit / daily_budget 控额。")

    card("02-timeline.png", "本次部署的真实顺序与耗时", [
        ("0  跳板连通（直连失败 → ProxyJump）", "≈1 分 30 秒", "warn"),
        ("1  体检：OS / 内存 / 外发可达性", "≈1 分", "info"),
        ("2  Python 3.12（apt 误匹配 → uv）", "≈2 分", "bad"),
        ("3  上传代码 73 个文件 + uv pip 安装依赖", "≈1 分 30 秒", "info"),
        ("4  news 用户 + /etc/ai-news-radar/env + init_db", "≈40 秒", "info"),
        ("5  systemd enable --now → Bot 开始 polling", "≈30 秒", "ok"),
        ("6  首轮采集 412 条 + AI 处理 240 条", "≈2 分 30 秒", "ok"),
        ("7  Telegram 实发验证：自检消息 + 真实早报", "≈1 分", "ok"),
        ("8  两次事故复盘（OOM / 文件权限）+ 修复回归", "≈4 分", "warn"),
        ("合计（含排障）", "≈25 分钟", "ok"),
    ], note="每一行都对应本手册后面的小节；命令可以逐条复制执行。")

    card("10-verify.png", "验收清单（全部在这台 VPS 上实测）", [
        ("依赖安装（Debian 11 + Python 3.12）", "requirements.txt 全绿", "ok"),
        ("单元测试", "87 passed（加 swap 后）", "ok"),
        ("开机自启", "systemctl is-enabled = enabled", "ok"),
        ("Telegram 登录", "Run polling @your_news_bot", "ok"),
        ("真实投递（管理员 chat 999999999）", "已送达 / 1 条简报", "ok"),
        ("采集入库", "412 条（RSS 317 + HN 22 + GitHub 34 + arXiv 34）", "ok"),
        ("AI 处理", "scanned 240 / processed 218 / filtered 22 / failed 0", "ok"),
        ("去重", "dup 3+2，重复运行 stored=0", "ok"),
        ("简报入库记录", "push_logs: morning=1；articles.is_sent=10", "ok"),
        ("故障隔离", "VentureBeat 429 只留一行日志，其余源正常", "ok"),
        ("内存占用", "稳态约 170-190MB（机器总内存 475MB，已加 2G swap）", "warn"),
        ("LLM 质量", "未验证：本次没有 API Key，运行在规则模式", "bad"),
    ], note="最后一行是当前唯一未闭环项：填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启即可。")

    flow("12-free-flow.png", "/免费 的两条数据链路：一条查接口，一条读新闻", [
        ("定价接口", ["OpenRouter /api/v1/models", "免密钥、免注册", "✓ 机房 IP 也通"]),
        ("筛选", ["pricing 全为 0 才算免费", "剔除 router/preview/alpha", "按上架时间排序"]),
        ("限免资讯", ["已入库的新闻正文", "config/free_offers.yaml", "信号 + 主体，且相邻"]),
        ("合并渲染", ["⚡ 实时免费模型", "🎁 近期限免", "按钮：7/30/90 天、按工具"]),
    ])

    def _lines(*rows):
        # real newlines only - no escape sequences to survive the generator
        return chr(10).join(rows)

    phone("13-free-chat.png", [
        "/免费",
        _lines("🎁 近期免费 / 限免（30 天内 2 条）",
               "",
               "⚡ OpenRouter 现在免费可用的模型（共 16 个，列出最新 8 个）",
               "1. 🧩 Qwen: Qwen3.8 27B (free)",
               "   qwen/qwen3.8-27b:free · 上下文 262K · Qwen",
               "2. 🧩 NVIDIA: Nemotron 3.5 Lightning (free)",
               "   nvidia/nemotron-3.5-lightning:free · 上下文 1M",
               "…",
               "1. 💻 Qoder",
               "   Qoder 向上海交通大学全校师生",
               "   Linux.do 福利分类 · 09-25 10:27 · 免费 / 免费开放",
               "",
               "判断依据：正文里同时出现「免费信号」和「具体工具/模型」。促销随时变动，用之前请以官网为准。"),
        "/free qoder",
        _lines("🎁 近期免费 / 限免（30 天内 1 条 · Qoder）",
               "",
               "1. 💻 Qoder",
               "   Qoder 向上海交通大学全校师生",
               "   Linux.do 福利分类 · 09-25 10:27 · 免费 / 免费开放",
               "",
               "判断依据：正文里同时出现「免费信号」和「具体工具/模型」。"
                        "促销随时变动，用之前请以官网为准。"),
    ], footer="美西 VPS 实测输出，节选（@your_news_bot → chat 999999999）")

if __name__ == "__main__":
    main()
