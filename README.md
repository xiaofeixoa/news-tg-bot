# AI News Radar

个人 AI 新闻雷达 / Telegram AI News Agent。
运行在一台 Debian 服务器上：持续采集 RSS·Atom、Hacker News、GitHub、arXiv（Reddit / YouTube 可选），
标准化、多层去重、AI 分类与评分、中文摘要，再通过 Telegram Bot 推送早报、晚报、突发新闻，
并支持 `/news`、`/search`、`/summary`、以及自然语言提问。

> 📘 **想直接照做一次部署？看 [部署实战手册](docs/DEPLOYMENT_HANDBOOK.md)** ——
> 里面是真实服务器上的一次完整部署：每一步命令、真实输出、两个线上事故的复盘与修复，图文并茂。
> 本页更偏产品与配置说明。

它不是一个 RSS 转发器：所有信息都会经过「规则过滤 → 轻量模型 → 强模型」三层处理，
只有值得看的新闻才会到你面前。

- Python 3.12+ · aiogram 3 · SQLAlchemy 2 + SQLite · feedparser · httpx · APScheduler · Pydantic Settings
- 无 Redis / Kafka / PostgreSQL / 前端，单机内存占用 < 300MB

---

## 1. 快速开始（Debian 12）

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip rsync

# 1) 放置代码
sudo mkdir -p /opt/ai-news-radar
sudo rsync -a --exclude .venv --exclude data --exclude logs ./ /opt/ai-news-radar/
cd /opt/ai-news-radar

# 2) 虚拟环境 + 依赖
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt

# 3) 配置密钥
sudo cp .env.example .env
sudo nano .env          # 必填：TELEGRAM_BOT_TOKEN、ALLOWED_CHAT_IDS；建议填 LLM_*
.venv/bin/python -m app.main --self-check

# 4) 建库并注册数据源
.venv/bin/python scripts/init_db.py

# 5) 先跑一轮采集 + AI 处理（不需要 Bot，验证数据链路）
.venv/bin/python -m app.main --once
.venv/bin/python scripts/preview.py news          # 终端里看 /news 会输出什么
.venv/bin/python scripts/preview.py digest        # 看早报长什么样

# 6) 验证 Telegram 通路（给你的手机发一条测试消息）
.venv/bin/python scripts/telegram_smoke.py

# 7) 交给 systemd 常驻
sudo useradd -r -s /usr/sbin/nologin -d /opt/ai-news-radar news
sudo install -o news -g news -m 600 -D .env /etc/ai-news-radar/env
sudo mkdir -p /opt/ai-news-radar/{data,logs} && sudo chown -R news:news /opt/ai-news-radar/{data,logs}
sudo cp deploy/ai-news-radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-news-radar
journalctl -u ai-news-radar -f
```

拿到 Chat ID：给自己常用的 Telegram 账号给 `@userinfobot` 发一条消息，它会把数字 ID 回给你。
拿到 Bot Token：找 `@BotFather` 执行 `/newbot`，复制 token。

### 本机直接运行（调试）

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\
.venv/bin/python -m app.main            # Bot + 调度器（前台运行，Ctrl-C 停止）
.venv/bin/python -m app.main --no-bot   # 只跑采集与推送
.venv/bin/python -m app.main --once     # 采集 + 处理一轮就退出
```

---

## 2. 配置

密钥只从环境变量 / `.env` 读取，永不写进代码或 YAML；行为参数放在 `config/*.yaml`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 空 | BotFather 颁发 |
| `ALLOWED_CHAT_IDS` | 空 | 逗号分隔白名单。**为空时拒绝所有人**（安全默认值） |
| `TELEGRAM_PROXY` | 空 | Bot API 被墙时填 HTTP 代理，如 `http://127.0.0.1:7890` |
| `HTTPS_PROXY` / `HTTP_PROXY` | 空 | 采集侧代理（httpx 自动读取），可救回 arXiv / Hugging Face 等源 |
| `DATABASE_URL` | `sqlite:///data/news.db` | 相对路径按项目根解析 |
| `LLM_BASE_URL` | 空 | 任意 OpenAI 兼容端点，如 `https://api.openai.com/v1`、OpenRouter、DeepSeek、GLM、硅基流动、`http://127.0.0.1:11434/v1`(Ollama) |
| `LLM_API_KEY` | 空 | 只写入环境变量，日志里会被脱敏 |
| `LLM_MODEL` | 空 | 默认模型 |
| `LLM_LIGHT_MODEL` / `LLM_STRONG_MODEL` | 回落到 `LLM_MODEL` | 分层成本控制：分类/摘要用轻量，深度分析用强模型 |
| `TIMEZONE` | `Asia/Shanghai` | 早报晚报按此时区 |
| `RSS_FETCH_INTERVAL` | `600` | RSS 抓取间隔（秒）；HN/GitHub/Reddit/arXiv/YouTube 同理 |
| `PROCESS_INTERVAL` | `600` | AI 处理批次间隔 |
| `DAILY_DIGEST_TIME` / `EVENING_DIGEST_TIME` | `08:00` / `20:00` | 用户可用 `/settings` 覆盖 |
| `MIN_ARTICLE_SCORE` | `45` | 低于该分不进入列表和简报 |
| `BREAKING_NEWS_ENABLED` / `_THRESHOLD` / `MAX_BREAKING_NEWS_PER_DAY` / `BREAKING_COOLDOWN_MINUTES` | `true` / `90` / `5` / `60` | 突发新闻开关、**AI 模式**阈值、每日上限、冷却；没配 `LLM_*` 时走 `breaking.rule.*` 的"事件词 + 一手来源 + 时效"三重门槛，见 §11 v1.9 |
| `GITHUB_TOKEN` | 空 | **强烈建议填**：releases 模式每个仓库每轮一次请求，27 个仓库 ≈ 54 次/小时，而匿名上限就是 60 次/小时。没有它 GitHub 源会长期空转（日志现在会直接说明是配额问题，而不是伪装成"今天没新闻"）。用一个无 scope 的 classic token 即可 |
| `LOG_LEVEL` | `INFO` | |

配置文件：

| 文件 | 作用 |
| --- | --- |
| `config/sources.yaml` | 数据源清单（名称、类型、URL、可信度等级、开关、类型专属参数） |
| `config/settings.yaml` | 评分权重、关键词、去重阈值、事件窗口、简报与突发新闻规则、日志 |
| `config/categories.yaml` | 分类体系（7 大类 + 子类 + 关键词），同时是 AI 的分类词表 |
| `config/prompts.yaml` | 全部 Prompt：分类、评分、摘要、去重、深度分析、简报、对话、兴趣、意图 |
| `config/free_offers.yaml` | `/免费` 的词表：免费信号、工具/模型别名、误报黑名单、有效期写法 |

`/免费` 的行为参数在 `config/settings.yaml` 的 `free:` 块里，改完重启生效：

```yaml
free:
  days_default: 30      # 不带参数时看多少天
  alert:                # 主动推送（见 §3.1）
    enabled: true
    min_confidence: 0.6
    require_title_subject: true   # 主体只在正文里露过面的不推送
    cooldown_minutes: 90          # 0 = 不冷却（0 会被当成 0，不会退回默认值）
    max_per_day: 4
  limit: 12
  score_bonus: 6        # 识别到限免资讯时加一点分，让它更容易进早报
  models:               # 实时免费模型（定价接口）
    enabled: true
    name: OpenRouter
    url: https://openrouter.ai/api/v1/models
    ttl_minutes: 360    # 缓存多久
    max_items: 8        # 一次展示几条
    exclude: [openrouter/free, preview, alpha]
```

`models.url` 可以换成任意返回 `{data:[{id,name,created,context_length,pricing}]}`
结构的 OpenAI 兼容端点；接口不通时实时块自动消失，命令照常返回采集到的限免资讯。

**新增一个 RSS 源只改 YAML**，重启后生效（或等下一次调度）：

```yaml
  - name: My AI Blog
    type: rss
    url: https://example.com/feed.xml
    enabled: true
    category: media
    quality: B          # A 官方 / B 主流媒体 / C 社区 / D 个人
```

RSSHub 同理：把 `url` 指向你自己的 RSSHub 实例即可（`config/sources.yaml` 里有示例）。

---

## 3. Telegram 命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 初始化，写入用户配置，显示状态 |
| `/help` | 命令与用法 |
| `/news` | 最新 AI 新闻，编号 + Inline Keyboard（数字按钮看详情） |
| `/latest` | 最近 24 小时 |
| `/today` / `/yesterday` | 今日 / 昨日（按你的时区） |
| `/digest` / `/digest evening` | 立即生成早报 / 晚报 |
| `/search 关键词` | 搜索历史新闻，如 `/search MCP` |
| `/summary 123` | 对第 123 号新闻做深度分析（调用强模型） |
| `/topics` | 分类入口，点按钮看该方向新闻 |
| `/free` / `/免费` | 现在哪些 agent / 模型 / API 免费，见 §3.1 |
| `/sources` | 数据源健康状态：🟢 正常 🔴 最近出错 ⚪️ 已禁用 🟡 还没跑过 |
| `/settings` | 早报/晚报时间、突发新闻开关、评分门槛、暂停开关 |
| `/setinterest` | 自然语言设置兴趣：`/setinterest 我主要关注 AI Agent、开源模型、GPU 和 Claude` |
| `/pause` / `/resume` | 暂停 / 恢复自动推送 |

也可以直接说话：`最近 AI Agent 有什么值得关注的？`、`今天有哪些重要的 AI 新闻？`、
`第二条详细说一下`（Bot 会记住你上一次看到的列表）。

推送内容遵循设计文档的格式：一句话总结 → 核心内容 → 为什么值得关注 → 评分 → 来源 → 阅读原文。

### 3.1 `/免费`：现在什么能白嫖

Telegram 的官方命令名只允许 `a-z0-9_`，所以菜单里放的是 `/free`，
但直接打 `/免费`、`/白嫖`、`/mianfei` 也一样命中，说
`最近有什么可以白嫖的模型？`、`opencode 免费吗` 也会走到同一条逻辑。

答案由两部分拼成，缺一不可：

| 部分 | 来源 | 回答的问题 | 实时性 |
| --- | --- | --- | --- |
| ⚡ 实时免费模型 | `free.models.url` 指向的公开定价接口（默认 OpenRouter `/api/v1/models`） | **此刻**哪些模型 0 价，模型 id 直接填进 agent 就能用 | 每次拉取后缓存 `ttl_minutes`；只需一个免密钥公开接口 |
| 🎁 限免资讯 | 已采集的新闻正文（`config/free_offers.yaml` 词表判定）。中文主力源是 linux.do 福利分类，靠 `browser_tls` 过 Cloudflare | 某个 **agent**（Qoder / Zcode / opencode / Claude Code…）什么时候搞限免、送到什么额度 | 没被任何源写到的促销 |

只有"免费信号 + 具体主体"同时出现才算一条限免：正文里的 "free software""camera-free"
这类误报由 `false_friends` 挡掉，正文信号还必须先出现在标题里、或者与主体相邻
（`body_max_distance`），否则不认——不然"我自己跑着不要钱"也会被当成官方公告。

用法：

```
/free                 # 默认近 30 天
/free 7               # 近 7 天
/free qoder           # 只看某个工具（工具名来自 config/free_offers.yaml）
/free deepseek 90     # 组合：关键词 + 天数
```

按钮可切换 7/30/90 天与按工具筛选。**识别范围不止词表里的工具**。标题里出现"此刻免费的模型"或"某站发放兑换码"时，
也会成为主体：

- 定价接口的模型清单会在每次快照时**回灌给检测词表**（`register_model_names`），
  所以 `hermes 官方提供了免费的 stealth/space-bunny-alpha` 这种句子不用改 YAML 就能认出来；
- 中文社区习惯把站点名写成 `XX站`，于是"标题里有发放/兑换码/免费信号 + `XX站`"也算主体
  （`站内合适的公益站` 这种没有免费信号的仍然不响）。

**准确率/召回是被测出来的，不是感觉出来的**：`tests/data/free_eval.yaml` 有 50 条标注
样本（23 正 / 27 负），中文部分逐条取自线上真实语料，英文负例同样是语料原文
（`Training-Free`、`camera-free`、`encoder-free`、`Freedom...`、`F-Droid` 这些就是
误报制造机），英文正例按厂商真实促销措辞合成 —— 采样那天语料里没有英文促销，
硬等不如把话说清楚，这一点在文件里注明了。
`tests/test_free_eval.py` 断言 **precision = 1.0**（推送里冒出一条假限免比漏十条更糟）、
**recall ≥ 0.85**，并且**按语言分别卡线**，免得中文的高分把英文的退步盖住。
当前：中/英各自 precision 1.00、recall 1.00。加信源、改词表时先看这几个数。

**加一个新的 agent 名字不用改代码**：
往 `config/free_offers.yaml` 的 `tools:` 里补一行（含别名），重启后会自动回扫
历史新闻（`backfill_free_offers`，不花 token，也会撤掉收紧后不再成立的旧标记）。

**还会告诉你「变了什么」**。`/免费` 底部有一块 📈 变化：对比上一次快照，哪些模型**刚开始免费**、哪些**已经结束免费**。第一次快照只登记不播报（否则刚部署就会把整个免费层当成「新增」）。带关键词查询时不显示这块 —— 你问的是 `glm`，把别的模型的变化端上来是噪音。

带工具名查询而确实没找到限免时（例如 `/free qoder`），回答会说明「新闻里没采到 + 定价接口里也没有同名模型」，而不是含糊的一句「没有」；关键词回落出来的相关新闻改用 📰 标记，不会伪装成限免。
限免本身也可能被标成「推断自正文」：主体（Qoder / 智谱 / Kiro…）只出现在正文而不是标题里时，列表会这么注明，并且默认**不会**为它发推送（`alert.require_title_subject`）——一篇周报顺带提到某个工具免费，不值得打断你。

定价接口挂了不会连累命令：实时块自动消失，仍然返回采集到的限免资讯，
日志里留一条 `live free-model check failed`。

**不用问也会告诉你**：`free.alert` 打开时，每轮 AI 处理结束后会把「刚入库、置信度达标、以前没推过」的限免主动推一次（默认一条消息最多 3 条，90 分钟冷却、每人每天最多 4 条，`/pause` 的用户不发）。
定价接口里**新出现**的 0 价模型也走同一条通知，首次快照只登记不播报，免得刚部署就刷 16 条。是否推过记在 `articles.free_offer_sent_at`，所以重启不会把老限免再推一遍。

---

## 4. 数据流与实现要点

```
sources.yaml ─▶ Collector(插件) ─▶ Normalizer ─▶ 去重(URL / 标准化URL / 标题相似度 / AI) ─▶ SQLite
                                                                                    │
                                   Telegram ◀─ Scheduler ◀─ AI Pipeline(关键词门→分类→评分→摘要→标签)
```

**插件化 Collector**：`app/collectors/base.py` 定义 `fetch()` / `normalize()` / `collect()`，
用 `@register` 注册类型；`build_collectors()` 从 YAML 实例化。
已实现 `rss`、`hackernews`、`github`(search / releases / org)、`reddit`、`arxiv`、`youtube`。
每个 Collector 独立运行，单个源失败只写一条 `logs/collector.log`，不影响其他源。

**去重（多层）**
1. URL 标准化后取 SHA-1，数据库 `UNIQUE(url_hash)` 兜底 —— 去掉 `utm_*`、`fbclid`、`gclid`、`ref` 等参数与 fragment，小写 host；
2. 标题相似度：词序无关的 token 序列比 + 字符序列比 + 覆盖率取最大值；版本号冲突（`4.5` vs `4.6`）直接减半，避免误合并；
3. 可选 AI 判定（`dedup.ai_review_enabled`），只处理边界样本并限制每轮调用次数；
4. 命中重复时仍写入行（保留来源线索），但挂到同一个 `events`，继承评分，**不再消耗 token**，列表与简报里只出现一次。

**评分**（`config/settings.yaml` 的 `scoring.weights`，非硬编码）

```
final = importance*0.35 + relevance*0.30 + novelty*0.15 + source_quality*0.10 + community_heat*0.10
```

`community_heat` 缺失时（官方博客没有点赞数）其余权重自动归一化，否则重大公告永远到不了 90 分；
`/setinterest` 的兴趣词会加权，`exclude` 类型反向扣分。

**Token 成本控制（三层）**
1. 规则：URL/标题去重 → 关键词命中（`filters.keywords`）→ 标题黑名单，未命中直接落库标记 `filtered_out`，**一次模型都不调**；
2. 轻量模型（`LLM_LIGHT_MODEL`）：相关性判断、分类、评分、中文摘要，每篇一次请求，`llm.max_content_chars` 截断正文；
3. 强模型（`LLM_STRONG_MODEL`）：`/summary` 深度分析、简报总体判断、自然语言问答。
`llm.process_batch_size` 限制每轮处理条数。`llm.enabled: false` 可整体关掉 AI，此时新闻仍会采集、去重并按规则分类推送。

**容错**
所有 HTTP 请求都有超时与指数退避重试；LLM 4xx 不重试、5xx/429 重试；
模型不可用时用正文首句生成兜底摘要（`meta.pipeline` 标记 `rule` / `ai_degraded`）；
Telegram 发送失败重试，被拉黑则跳过；任务异常被 `guarded()` 包住，调度器不会退出。
日志：`logs/app.log`、`collector.log`、`telegram.log`、`llm.log`、`scheduler.log`（滚动，token 自动脱敏）。

**数据库**：`articles`、`sources`、`tags`、`article_tags`、`users`、`user_interests`
（设计文档 §18），另加 `events`（多来源合并）与 `push_logs`（突发新闻冷却与每日上限）。
SQLite 开启 WAL 与外键。

---

## 5. 换 LLM 供应商

改 `.env` 三行并重启即可，**不需要动任何业务代码**：

```bash
# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini

# DeepSeek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=x-ai/grok-4

# 本地 Ollama
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

Prompt 全在 `config/prompts.yaml`（占位符是 `$name` 形式，所以 JSON 示例里的花括号无需转义）。

---

## 6. 运维

```bash
# 数据源健康检查（不写库）
.venv/bin/python scripts/test_sources.py -v
.venv/bin/python scripts/test_sources.py --only hackernews,github

# 一条命令同步到所有已部署机器并自检（服务状态 + 迁移是否落库）
scripts/deploy.sh
scripts/deploy.sh anr-vps

# 手动补一轮 / 看效果
.venv/bin/python -m app.main --once
.venv/bin/python scripts/preview.py ask "最近有什么值得关注的开源 AI 项目？"
.venv/bin/python scripts/preview.py free --days 30        # /免费 会输出什么
.venv/bin/python scripts/telegram_smoke.py --free         # 把 /免费 的真实结果发给自己
.venv/bin/python scripts/telegram_smoke.py --free --query glm

# 备份：整个状态都在一个文件里
sqlite3 data/news.db ".backup '/backup/news-$(date +%F).db'"

# 重建库（会清空新闻）
.venv/bin/python scripts/init_db.py --reset
```

systemd 常用：`systemctl status|restart ai-news-radar`、`journalctl -u ai-news-radar --since "1 hour ago"`。
Docker：`docker compose up -d`（`DATABASE_URL` 已在 compose 里指向挂载卷）。

### 真实服务器实测（Debian 13 / Python 3.13，中国大陆机房）

部署目标机上实测过一轮，结论如下：

| 检查 | 结果 |
| --- | --- |
| 依赖安装 | `pip install -r requirements.txt` 全绿，Debian 13 + Python 3.13.5 无需改代码 |
| 单元测试 | 82 个用例全部通过（与开发机同一结果） |
| 数据源可达性 | **17 个启用源里 14 个可直连**：OpenAI、Google AI、DeepMind、NVIDIA、Microsoft Research、AWS ML、GitHub Blog、TechCrunch、The Verge、Ars Technica、MIT Tech Review、Hacker News、GitHub Trending / Releases |
| 不可达 | Hugging Face blog（连接超时）、VentureBeat（429 限流）、arXiv（超时）；`api.telegram.org` 不通 |
| 失败影响 | 不可达的源只在 `logs/collector.log` 留一行告警，其余源照常入库（实测 270 条入库） |
| 资源占用 | 常驻内存峰值约 153MB（Bot 模式）；只采集不跑 Bot 时约 85MB，见手册事故六 |

**Telegram 不通时怎么办**（这就是那台机器的情况）：

1. 先让采集侧跑起来，新闻照常积累：
   `sudo cp deploy/ai-news-override-no-bot.conf /etc/systemd/system/ai-news-radar.service.d/no-bot.conf`
   然后 `daemon-reload && restart`。这个覆盖只是把启动命令换成 `--no-bot`，删掉即恢复。
2. Bot 侧需要一条能连 `api.telegram.org` 的出路，任选其一：
   - 有 HTTP 代理：`.env` 里填 `TELEGRAM_PROXY=http://127.0.0.1:7890`（Bot 走 aiogram 的代理会话），
     同时填 `HTTPS_PROXY=http://127.0.0.1:7890` 让 httpx 采集也走代理（Hugging Face / arXiv 会一起复活）；
   - 没有代理：把 Bot 进程放到能出海的小 VPS 或家里机器上，服务器只负责采集；
   - 删掉 `no-bot.conf` 并填入 `TELEGRAM_BOT_TOKEN` + `ALLOWED_CHAT_IDS` 后 `restart`。

**注意启动行为**：缺 `TELEGRAM_BOT_TOKEN` 时进程以退出码 3 结束，systemd 单元里
`RestartPreventExitStatus=3` 会让它**停在 failed 状态并留下一行中文原因**，而不是每 5 秒重启刷满日志。
填好 token 后 `systemctl reset-failed ai-news-radar && systemctl restart ai-news-radar` 即可。

**未知 Chat ID 怎么拿**：白名单为空时 Bot 拒绝所有人，但拒绝消息里会直接回给你自己的
Chat ID（日志里也会记一份），复制进 `ALLOWED_CHAT_IDS` 重启就能用了。


---

## 7. 测试

```bash
.venv/bin/python -m pytest            # 336 个用例
```

覆盖：RSS/Atom 解析、空源、超时、HTTP 500、XML 损坏、单个坏源不影响整体；
`tests/data/category_eval.yaml` + `tests/test_classifier_eval.py` 是规则分类的评测集：
53 条线上真实标题（带来源名），允许多个可接受答案，门槛 0.88，2026-09-26 实测 92.5%；
`tests/data/dedup_eval.yaml` + `tests/test_dedup_eval.py` 是跨来源去重的评测集：
25 对线上真实标题，要求真重复 100% 合并、不同事 0 误并；
URL 与标题去重（tracking 参数、同标题、改写标题、跨来源、版本号差异）；
评分（权重可配置、可信度分级、热度归一、兴趣加权、突发门槛）；
突发判定（规则模式的事件词/一手来源/时效三重门槛，AI 模式仍按分数，🔥⭐🔹 阶梯按模式给）；
中文输出（免费路由与配额、退避、品牌名占位符守护与还原、要点翻译入库、显示层中文优先）；
Pipeline（关键词门、分类、中文摘要、AI 故障兜底、事件合并、简报渲染、冷却与上限）；
Bot 层（`/start` `/help` `/news` `/search` `/summary` `/digest` `/topics` `/sources`
`/settings` `/pause` `/setinterest` `/free`、Inline Keyboard 回调、未授权 Chat 拒绝、自然语言问答）。
`/免费` 单独一组：词表命中与误报黑名单、正文邻近度、标题/正文分级、实时免费模型解析
（0 价判定、exclude、排序、缓存 TTL、状态文件写不进去也不炸）、接口故障降级、关键词回落标注。

真实 Telegram 往返用 `scripts/telegram_smoke.py`，真实网络抓源用 `scripts/test_sources.py`。

### 当前验证状态

已经在本机对真实网络跑通（无需任何密钥的部分）：

| 验证项 | 结果 |
| --- | --- |
| `scripts/test_sources.py` | OpenAI 50 条 / TechCrunch 20 / Hacker News 24 / arXiv 34 / GitHub Releases 9 全部 OK |
| `python -m app.main --once` | 采集 403 条 → 入库 398、去重 5、规则过滤 22、AI 处理 218；重复运行 `stored=0 dup=403`（幂等） |
| 故障隔离 | VentureBeat 返回 HTTP 429，仅记录一条告警，其余 17 个源照常完成 |
| `scripts/preview.py digest / news / card / ask` | 早报分栏、编号列表、深度分析卡片、自然语言检索均有真实输出 |
| 调度器 | rss/hn 10 分钟、github 30 分钟、arxiv 3 小时、AI 处理 10 分钟、简报检查 5 分钟、维护 6 小时（禁用的源不注册任务） |
| Bot 装配 | 21 个 handler + 错误处理器全部注册；无 token 时按 fail-closed 抛错 |
| **中文输出** | `/news`、`/digest` 输出中文标题与摘要（LLM 优先，无 Key 时走免密钥 MT + 每日额度），见手册 §6.5 |
| **真实 Telegram 投递** | 美西 VPS 上 `Run polling @your_news_bot` 成功，自检消息与中文早报均"已送达"到管理员 chat |
| **`/免费` 实时免费模型** | 本机与美西 VPS 均从 OpenRouter 定价接口取到 17 个当前 0 价模型（含 `z-ai/glm-5.2:free`、`qwen/qwen3.8-27b:free`），`data/free_models.json` 记录首次观测时间，用于显示"已免费 N 天" |
| **`/免费` 端到端投递** | VPS 上 `scripts/telegram_smoke.py --free` 已送达管理员 chat（816 字，含切换按钮）；`--free --query glm` 走关键词回落——条目改用 📰 而不是礼物标记，并写明「下面只是相关新闻，别当成限免」；`--free --query qoder` 会说明「新闻里没采到 + 定价接口里也没有同名模型」 |
| 中文限免信源 | linux.do 的 `.rss` 被 Cloudflare 按 TLS 指纹拦（同一台机器同一时刻 curl 200、httpx 403）。解法是 `browser_tls: true` + 可选依赖 `curl_cffi`（复刻 Chrome 握手）：实测一轮入库 24 条福利帖，`/免费` 立刻出现真实限免（如“Qoder 向上海交通大学全校师生开放”）。Reddit `search.rss` 仍按 IP 限流，默认关闭 |
| 内存占用 | `aiogram` 单独占 **+106MB** 匿名内存（它要为整套 Bot API 建 pydantic 模型）。现在只有真正跑 Bot 的进程才加载它：`--no-bot` 采集机 `VmRSS 201MB → 85MB`（`RssAnon 177MB → 63MB`）；`import app.main` 的常驻匿名内存从 146MB 降到 47MB（进程内 `/proc/self/status` 实测）；带 Bot 的机器仍约 175MB，因为它确实要用 aiogram。回归用例：`test_collection_mode_does_not_import_aiogram` |
| 单元测试 | 251 个用例在 Windows / Debian 13 / Debian 11 三处全绿 |
| **`/免费` 识别质量** | 50 条标注集：中/英各自 precision 1.00、recall 1.00（英文召回本轮从 0.73 补起）；线上最近 600 条全源扫描稳定命中 9 条，放宽英文信号后**没有新增误报**，逐条人工复核 |
| **`/免费` 主动推送** | VPS 实测：一次 `delivered: 1` 推了 3 条限免（Qwen / 智谱 / RelayFor-DeepSeek），紧接着再跑是 `delivered: 0`；账本 `push_logs(kind=free_offer)` 现在是 **(user, article) 逐条**记录，所以第二个订阅者 / 以后新订阅的人不会被第一个人的已读记录吞掉 |
| **定时推送的 HTML 渲染** | `TelegramSender.send` 之前没带 `parse_mode`，早报/突发里的 `<b>`、`<a>` 会被当纯文本发出去（字面标签）。现在统一按 HTML 发送，Telegram 拒绝解析时自动降级为纯文本重发，不会整条丢掉 |

**免密钥翻译链路（`translate.provider: auto`）**现在真的会"链"下去：

| 顺序 | 路由 | 说明 |
| --- | --- | --- |
| 1 | LLM（配了 key 才启用） | 质量最好，一次请求翻整批 |
| 2 | MyMemory | 免密钥，但按 IP 限额；返回 `MYMEMORY WARNING`/429 时**记入 60 分钟退避**，不再空转 |
| 3 | Google 网页端点 `translate.google.com/translate_a/t` | 机房 IP 也能通；`translate.googleapis.com` 那个反而返回空载荷 |

配套的两个小改动，都是实测里抓出来的：

- **模板标题不再送翻译**：`v1.2 released in owner/repo`、`owner/repo (N stars)`
  由 `localize_title()` 直接组成中文（`owner/repo 发布 v1.2`），既省额度又不会出现
  "owner/repo中发布的v1.2" 这种机翻语序；启动时 `repair_template_titles()` 会把
  历史行一并修好，不花 token。
- **品牌名还原**：机翻爱用音译（克劳德 / 迪普西克），`PROPER_NOUNS` 把常见厂商
  换回拉丁原文；`&amp;#128064;` 这类双重转义的实体在入库和展示两处都会被解码。

实测（美西 VPS，MyMemory 当日额度已耗尽的情况下）：一轮 `translate_pending` 译出 60 行，
早报 10 条标题 **10/10 为中文**，`translated_by` 如实记录实际服务它的路由（`google`）。

剩下唯一与质量相关的未闭环项：**LLM Key**。填 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 后，
中文摘要与分类会从"机器翻译级"提升到"编辑级"，并且不再受免费翻译每日额度限制。
在此之前系统以规则模式 + 免费翻译运行，新闻不丢、语言保持中文。

---

## 8. 目录结构

```
ai-news-radar/
├── app/
│   ├── main.py                # 入口：Bot + 调度器 / --once / --self-check
│   ├── config.py              # Pydantic Settings + YAML 配置
│   ├── logging_setup.py       # 分模块滚动日志 + 密钥脱敏
│   ├── bot/                   # aiogram handlers / keyboards / middleware / sender
│   ├── collectors/            # base + rss / hackernews / github / reddit / arxiv / youtube
│   ├── processing/            # normalize · deduplicate · classifier · scorer · summarizer · tagger · pipeline
│   ├── services/              # news · search · digest · llm · format
│   ├── database/              # models · database · repository
│   └── scheduler/jobs.py      # APScheduler 任务
├── config/                    # sources · settings · categories · prompts
├── scripts/                   # init_db · test_sources · preview · telegram_smoke
├── tests/                     # pytest
├── deploy/ai-news-radar.service
├── Dockerfile · docker-compose.yml · requirements.txt · .env.example · LICENSE
```

---

## 9. 尚未实现（按设计文档的排期留在后面）

| 项目 | 状态 |
| --- | --- |
| Telegram Channel 采集（§4.7） | 未做，需要 Telethon 用户会话，属于第二阶段 |
| YouTube 视频转写摘要（§4.6） | 只做频道 RSS 标题层，不转写视频 |
| Event 趋势分析、新闻时间线、向量检索 / RAG（§42） | 未做；`events` 表已经为事件聚合打好底 |
| Web 管理后台 | 明确不在 MVP 内 |

## 10. 安全须知

- `.env` 已被 `.gitignore` / `.dockerignore` 排除；密钥不进 Git、不进 YAML、不进日志（`RedactingFormatter` 会屏蔽 `sk-...`、`Bearer ...`、Bot Token 形态的字符串）。
- `ALLOWED_CHAT_IDS` 为空 = 拒绝所有人；请勿把 Bot 加入群后放开白名单，那样群成员都能消耗你的 LLM 额度。
- systemd 单元以非 root 用户运行，开启 `ProtectSystem=full`、`PrivateTmp`，只有 `data/` 与 `logs/` 可写。
- 所有外部请求都有超时；GitHub 匿名限流 60 次/小时（本仓库已用条件请求把它降到每轮几次）。
- **文档与配图里的地址、主机名、Bot 用户名、chat id 全是占位符**（`2001:db8::/32`、`192.0.2.0/24`、`@your_news_bot`、`999999999`）；真实值只存在于服务器上的 `/etc/ai-news-radar/env`(0600)，不进 Git、不进 YAML、不进日志。

---

## 11. 变更日志

> 从第一版到现在的完整记录。**本仓库没有 git 提交历史可依赖**，版本号是 2026-09-26 整理时补的分段，
> 只在有可核对证据处标日期（数据库首批入库 2026-09-25、部署戳、日志时间戳）。
> 每条的详细现象 / 根因 / 修复过程见 [docs/CHANGELOG.md](docs/CHANGELOG.md) 与
> [部署手册](docs/DEPLOYMENT_HANDBOOK.md) 的事故章节。

### v1.0 初版实现
按设计文档一次做完：6 类 collector（rss / hackernews / github 三模式 / reddit / arxiv / youtube）插件式注册 →
URL 归一化 + 标题指纹 + 事件聚合的多层去重 → 可配置权重打分（`importance·.35 + relevance·.30 + novelty·.15 +
source_quality·.10 + community_heat·.10`）→ LLM 摘要与规则兜底双路 → APScheduler 定时采集/处理/简报/维护 →
aiogram Bot（`/新闻 /来源 /订阅 /设置 /免费`）→ SQLite(WAL) + systemd + `env`(0600) + 日志脱敏。
设计原则：一个源或一个提供方失败不拖垮整轮；无 LLM Key 时新闻照样入库。

### v1.1 部署联调（2026-09-25）
两台服务器上线（一台带 Bot、一台仅采集），跑通真实 Telegram 投递；写出图文并茂的部署实战手册；
`scripts/deploy.sh` 取代手工 `tar|scp|ssh`，重启后打印 `service=… schema=… stamp=…` 并校验迁移真的生效。
两个坑进了脚本注释：`systemctl is-active` 对 failed 单元返回非零，在 `set -e` 下会让自愈永不执行
（**曾造成约 90 秒真实停机**）；`$HOSTS` 只展开第一个元素，第二台机器会被静默跳过。

### v1.2 中文输出改造（硬要求）
- 免费机器翻译改为**多路由降级**（MyMemory → Google 免 key 端点），不再因为一个 IP 配额耗尽就整天英文；
- **模板标题本地化**：`v1.2 released in owner/repo` → `owner/repo 发布 v1.2`、`(0 stars)` → `收获 0 星`，
  零 token 零配额，并幂等回扫旧数据；
- 显示层永远中文优先，简报发送前对**将要显示的那几条**当场补齐；
- 修掉标题里出现 `&#128064;` 的双转义残留；`clean_text` 保持 URL 安全（不做 NFKC）。

### v1.3 `/免费` 限免检索
问"现在哪些 agent / 哪个模型免费"。两个互补事实源：OpenRouter 公共定价接口（免 key、权威，全零价才算免费）
+ 新闻语料里的限免事件（规则检测器），输出里视觉分离、网关条目带"未核实"标记；新限免按用户主动推送并记 `push_logs`；
**检测器质量做成可度量** —— 50 条人工标注（中英双语）门禁：precision 1.0 / recall ≥ 0.85。
顺带修：`for free` 从强信号剔除、配置里的合法 `0` 被 `x or default` 吃掉（新增 `as_int/as_float`）、
`exclude: [preview, alpha]` 曾把新闻标题点名的模型也挡掉（收窄为只挡元路由）。

### v1.4 成本与稳定性
- **Cloudflare 拦的是 TLS 指纹**：同机同秒 `curl` 200、httpx 403。加 `browser_tls`（`curl_cffi` + Chrome 指纹），
  一个论坛源从 0 条变 24/25 入库；手册里"数据中心 IP 打不开 Cloudflare 源"的旧结论被证据推翻并改正。
- **省内存**：aiogram 单独 import 约 106MB（475MB 机器上致命）→ 惰性导入 + `TYPE_CHECKING` 注解，
  并加**子进程回归测试**断言 `sys.modules` 里没有 aiogram；采集专用机实测降约 114MB。
- 定时推送漏传 `parse_mode` 导致简报出现字面 `<b>` → 默认 HTML、仅在 "can't parse" 时降级纯文本重发。

### v1.5 GitHub 配额：从静默空转到几乎免费
- 现象是几天 `0 new of 0 fetched`，真相是匿名 60 次/小时被 27 个仓库吃光、而 403 被 `log.debug` 咽掉
  （**静默的空转比报错危险**）。补了配额守卫（配额为 0 时一个请求都不发）、失败路径去问**不计配额**的
  `/rate_limit`、启动时按仓库数算给他看要多少次/小时。
- **条件请求是真正的解**：`data/github_etags.json` 存 etag + 响应体，下轮带 `If-None-Match`；
  GitHub 对 304 不扣配额。生产实测单轮 `18 metered + 0 free` → `3 metered + 15 free 304`（26 → 5-7 次），
  **重启不再等于重烧一轮配额**；每轮花销记进日志 `N metered + M free 304 + K known-empty`。
- 404 也算答案（默认 6 小时后重问），但只有真 404 会被记，超时/403 绝不缓存。
- **`GitHub Trending` 一直在查三个 topic 的交集**（GitHub 限定词是 AND）：交集两周只有 4 个仓库，
  `topic:ai` 单查有 26 个；另一条查询按 stars 排序导致每轮同样 25 个全被去重，长期 `0 new of 25`。
  改为每条目独立查询（`max_search_calls` 控成本）+ 新建仓库 `min_new_stars` 门槛 + pushed 按 `updated` 轮转
  → 上线第一轮 `25 new of 25 fetched`。
- **通用 429 退避**：429 原先走"可重试"分支，一轮连打三次、下轮再来。某商业源六小时失败 69 次且
  **历史上从未产出过一条**（浏览器 UA / curl / curl_cffi 实测同样 429 → IP 级），于是按主机退避
  （读 `Retry-After` 秒数或 HTTP date、`x-ratelimit-remaining: 0` + reset），200 但 remaining=0 也提前冷却，
  冷却期内一个包都不发；该源直接停用。

### v1.6 简报：从"按时发"到"发得对"
- **08:00 早报静默丢发**：interval job 第一次执行在启动 5 分钟后，部署反复重启让每个进程活不到首查；
  且"已发过 / 错过窗口"完全不打日志。改为启动后 120 秒即检查（按本地日去重本来就防重复发送）、
  错过窗口发 WARNING、已发过发 INFO，并按 (用户, 种类, 日期) 去重。
- **简报排的是"最新"不是"最重要"**：`_briefing()` 取的是 `latest(...)[:N]` 而 `latest()` 写死按时间排序。
  按真实参数实测：8 个名额里 4 个是论坛帖标题，同窗口分数更高的 17 条一条没进。
  改为**按分数选 + 每源限量**（`digest.max_per_source`，纯函数 `select_briefing()`，
  当天内容不够时回填而不是少发）：

  ```
  OLD newest-first → Reddit 4, GH Releases 2, TechCrunch 1, Ars 1   分数 56-72
  NEW best-first   → AWS ML 3, GH Releases 2, TechCrunch 2, Reddit 1 分数 62-72
  ```

### v1.7 正文提取与摘要质量
- 摘要与标题的 `[:160]` / `[:150]` 硬切让生产简报出现过 `…with a wall of `、`Topics: ai` 这种半句。
  统一改为 `normalize.shorten()`：中文标点或空格处断开、加 `…`、切点不早于限制 60%、不会切坏 HTML 实体；
  **产生摘要的地方与显示层同时改**，并对已入库旧行做幂等启动修补（实测 re-cut 79 + 2 行）。
- **官方公告结构上进不了简报**：某两家 7 天各 50 条，最高分 54.2 / 50.9，**永远够不到 55 的门槛**，
  而 3.7KB 的论坛帖轻松过关 —— 因为它们每条只给 36–288 字符，而规则打分靠关键词命中数。
  于是补上设计文档早写了、此前只用来清洗 feed 描述的 **正文提取**（BeautifulSoup 取正文段落、
  丢 nav/aside/script 与订阅噪声；每轮预算 + 复用主机退避表 + **只加分不减分**）。
  真页实测 46–71 字符 → 4.7k–12k，分数 41.7 → 78.6；存量靠每轮重新排队最接近门槛的短正文行消化。
- 连带修两个 bug：① `attach_tags()` 不幂等，任何"第二次处理同一行"都崩在 `article_tags` 联合主键上；
  ② 翻译队列只按 `title_zh IS NULL` 过滤，导致 **377 行有英文摘要、永远不再被翻译**
  （模板标题分支还 `continue` 掉了摘要翻译，所以每条 GitHub release 都中招）。
  两处一起修，并让"缺标题的行排在只缺摘要的行前面"—— 标题与摘要共用每日免费配额，
  摘要库存不该饿死新文章的标题。实测每轮 `translated 1/1` → `60/60 + 60/60`，待译摘要 150 → 7。
- **Reddit 页壳当标题**：链接帖的 RSS 正文只有 `submitted by /u/x [link] [comments]`×3，它被编成摘要、
  花了翻译配额、再当成头条。三层修复（stripper 认中英文页壳并折叠重复段、切句前先清洗、
  显示层让存量行自动回落真标题）。线上：`由 /u/pmv143 提交 [链接] [评论]…` → `此时，OpenAI应该国有化。`

### v1.8 让数字与显示都说真话（2026-09-26）
- `/stats` 的 `数据源：37 个` 数的是配置文件 —— 其中 **17 个是关着的**。改为
  `20 启用 / 37 配置 · 近 24 小时出过新闻 20 个 [· N 个正在报错]`，启动日志同步；
  根因是 `stats()` 此前**零测试覆盖**，现在补了断言。
- **免费翻译额度用完就直接显示英文**：原先没有中文摘要时整行被隐藏，简报只是更安静而不是更中文。
  现在未翻译的照原样显示，但**头条位仍只给中文**（`display_line = 中文摘要 > 标题`，
  第二行放"没当上标题的那一条"）—— 未翻译的英文聊天句不配抢标题。
  配额耗尽的告警只在状态跃迁时打一次并写明"未译部分保持英文"，退避跳过降到 debug。

### v1.9 突发新闻真的能用了，晚报不再被一个 subreddit 承包（2026-09-26）
- **突发此前是死功能。** 阈值 90 是给 AI 打分写的，规则模式打不出来：线上 7 天 565 篇未过滤文章
  实测最高 78 分，`sum(is_breaking)=0`，而且**一行日志都没有**，所以"0 条突发"长得像"今天没新闻"。
  规则模式现在改用三重门槛：**标题里有事件词**（announced / acquires / lawsuit / breach /
  `now available` / `$11.6 billion`）+ **一手来源**（source quality ≥ 80）+ **24 小时内**，
  命中理由写进 `meta.breaking_reason` 并 INFO 落盘。同一份语料回放只命中 4 条
  （Nscale 33.6 亿可转债、Anthropic 付 Akamai 116 亿、法院裁定 Anthropic 可被列黑名单、
  Crusoe 放弃 12.5 亿涡轮方案），四条都是真事件；而"把分数线降到 72"这个更简单的方案
  选出来的是 claude-code 版本号刷新和 AWS 部署教程。
  **真实投递已验证**：`push_logs` 第 14 行 `kind=breaking article_id=578` @ 2026-09-26 13:54:06 UTC，
  `is_breaking=1`，`logs/app.log` 有 `breaking candidate #578 … event "Court" from a first-hand source`。
  诚实说明：这条是我把昨天的稿子重新入队跑出来的，代码路径与新稿完全一致，只是我按了提前键。
- **晚报被一个 subreddit 承包。** 20:00 的 12 小时窗口 = 00:00–12:00 UTC，美国媒体在睡觉：
  实测窗口里 37 条只来自 Reddit/HN/Linux.do 四个源，8 个槽位 6 个给了 r/LocalLLaMA，
  而当天 116 亿与法院裁定一条没进。晚报窗口 12h → **24h**，并新增 `skip_sent`
  ——早报发过的不再发，两条简报才能安全重叠；回填改成**按来源轮转**，不再把溢出的高分整股塞回；
  `min_score` 55 → 45（55 会把 54 分的 116 亿新闻挡在门外，而 Reddit 靠关键词命中轻松过线）。
- **`🔥` 再也点不亮。** 沿用 AI 模式的 90 分线时规则模式永远没有 🔥，而 ⭐ 的 75 分比 🔥 更稀有。
  阶梯按模式给：规则模式 72 / 62 / 52。
- **中文收尾。** `☀️ AI Morning Briefing`、`🌙 AI Evening Briefing`、`🚨 AI BREAKING NEWS`
  和栏目名 `🤖 AI Models` 都是我们自己的文案，不是待翻译的新闻：改成 `☀️ AI 早报 / 🌙 AI 晚报 /
  🚨 AI 突发新闻`，taxonomy 新增 `label:`（模型发布 / 智能体 / 算力与推理 / 开源生态 / 论文与方法 /
  公司动态 / 产品与应用 / 其他），英文键仍留给 prompt 与数据库列。
  `/settings`、`/start`、`--self-check` 里那句"阈值 90"换成当前模式真正生效的判断。

本地全量 329 passed；两台服务器代码树 md5 一致（`97ea034a…`）。
**改了 2 处既有测试断言**：`test_digest_renders_sections…` 原本断言英文标题 `AI Morning Briefing`
——那正是硬要求要消掉的，现改为断言中文且不出现英文；`test_breaking_guard…` 需补
`source_quality=95`，因为来源等级现在是规则模式门槛之一，而那条测试关心的是冷却与日上限。

### v1.10 卡片里的英文要点，和被翻错的专名（2026-09-26）
- **每张卡片的"核心内容"都是英文。** 规则模式的 key_points 就是正文前几句：线上 439 条有要点的行
  **100% 没有一个中文字**，其中 29 条已经推送给他看过。标题和摘要走的是中文优先，要点没人管。
  现在 `display_key_points` 只渲染中文条目（AI 模式本来就输出中文，照旧显示），
  并且 `ensure_chinese` 在真正要展示的那几条上把要点也翻掉、存进 `meta.key_points_zh`
  ——**排在标题和摘要之后**，免费额度先给头条，剩下的才轮到这里，付一次就存下来。
- **免费机器把公司名当英文单词翻译。** 线上实测：`Gemini`→双子座、`Hugging Face`→拥抱人脸、
  `Claude`→克劳德，今天那条突发还把 Anthropic 写成了"人类技术"。570 条中文标题里 13 条丢了品牌名。
  送翻前把 `translate.keep_terms` 里的名字换成⑴⑵占位符、回来还原；
  **只列没有公认中文名的名字**（微软/亚马逊/英伟达是正确译名，守护它们反而会拒掉好结果）。
  引擎吞掉占位符（MyMemory 会）就当翻译失败、退给下一条路由，全失败就保持英文。
  真实线路验证：Google 4/4 原样带回品牌名，MyMemory 把 ⒆ 变成"锘洪噾"→被拒→Google 接管。
- 相邻/多词名字合并成一个占位符：`Amazon SageMaker` 拆成两个 token 时，机器会把词塞中间，
  实测产出 `在 Amazon 上使用 Qwen3-TTS 部署实时个性化语音 SageMaker AI`；
  合并后是 `在Amazon SageMaker AI上使用Qwen3-TTS部署实时个性化语音`。

336 passed；两台服务器代码树一致。改动过程中我另外犯过三次错，都已修正并说明：
临时脚本里 `import app.database.session`（模块名记错）、`p.n` 忘了起别名、
以及一次 Edit 误删了 `test_output_language_switch_is_respected` 的函数体（下一刀就补回来了）。
**改了 3 处既有测试**：`StubClient`/`ChainClient` 现在像真引擎一样处理占位符（原来按原文精确匹配请求）；
`test_a_row_missing_only_its_summary_is_still_queued` 的假译文补上了品牌名——
真实的引擎会把主语留在译文里，原先那句"摘要已经换成新的英文句子"是我编得不像。

### v1.11 Bot 剩下的英文栏目名，和一条通用"品牌不许丢"的清扫规则（2026-09-26）
- **`/topics`、分类按钮、分类页标题、`/news` 每行末尾的分类**全都直接打印 taxonomy 的英文键
  （`AI Models`、`Open Source`、`Companies`…）。v1.10 只改了简报的栏目标题，漏了 Bot 这几个面。
  现在统一走 `config.category_label()`（`news.topics()` 多返回一个 `label`，
  回调 payload 仍是英文键，数据库列和 prompt 不动）。
- **为什么测试没抓到**：`test_topics_and_sources_commands` 过去只断言"分类"两个字出现过，
  从没检查语言。现在断言 `模型发布` 在文本与按钮里、`AI Models` 在文本/按钮/回调页里都不许出现。
- **品牌翻译不能用坏词表。** 清扫时发现同一个 Hugging Face 在库里至少有
  拥抱人脸/拥抱面部/拥抱脸/拥抱脸部 四种写法，Anthropic 有 人类技术 和 人为因素 两种——
  v1.10 手写的四条黑名单只覆盖了其中一部分。改成通用不变式：
  **英文原文里出现 `translate.keep_terms` 的名字、中文里却没有 → 这条译文作废、重新翻译**。
  线上扫出 9 行（576 行有中文标题的里 1.6%），重译后违规数 0、待补中文标题 0，
  `/news` 里那条"上诉法院维持将人为因素指定为供应链风险"已变回"…将 Anthropic 指定为…"。
  写入侧已经由占位符 + `kept_intact` 兜住，所以这次是一次性数据修复，不需要常驻代码。

336 passed。上一轮没能确认的 CI 徽章这次从 anr-jump 的出口 IP 查到了：`148ee9c` = success
（anr-vps 的出口 IP 匿名配额被采集器用光了，换一台即可）。

### v1.12 栏目归属：从 36.7% 到 92.5%（2026-09-26）
之前"栏目名是中文了，但归属不准"这条是有数字的：**53 条线上真实标题的评测集上，
规则模式原来只对 18/49（36.7%）**——GitHub Trending 的仓库 0/4 全错，法务/监管新闻
几乎全进了 AI Applications 或"其他"，厂商博客的部署文乱窜。现在 **49/53（92.5%）**。

评测集：`tests/data/category_eval.yaml`（每条都带来源名，标题原文照抄线上）。
允许 `expect` 列多个可接受答案——一条 SageMaker 部署文既算算力也算应用，
硬要单一答案就是在教分类器猜我的口味，不是在测它。

四处改动，按贡献排：
1. **整词匹配**。原子串匹配下 `ipo` 命中 pivot、`app` 命中 happen、`ban` 命中 company，
   而词表本来就该有 ban/ipo 这种词。
2. **分数按词表长度归一**（除以 √len）：AI Models 有 22 个词、AI Infrastructure 有 40 个，
   原写法等于让词多的栏目更容易赢，跟内容无关。
3. **标题命中算两倍**：栏目是标题决定的，正文前 1200 字符里一个闲词不该翻盘。
4. **`source_hint` 终于参与规则模式**，并分两档：场域型来源（arXiv 每条都是论文、
   GitHub 榜单/发布页每条都是仓库）权重 1.2，厂商博客只是 0.35 的倾向——
   0.8 的单一权重实测会把 NVIDIA 博客上的 agent 教程塞进算力栏。
   另外两条结构性先验：标题里的参数量（`27B`/`0.8B`，`$11.6B` 这种钱不算）判 AI Models，
   `owner/name` / `(12 stars)` / `v1.2.3 released in` 判开源。
- Reddit 原来被硬提示成 "AI Agent"，把 r/LocalLLaMA 一族的分数全带跑，已取消。
- 词表补齐：Companies 之前**一个**法务/监管/钱的词都没有（court/ruling/feds/antitrust/
  billion/layoffs…），AI Infrastructure 缺 training/compute/latency（只在子分类里），
  AI Agent 缺复数 agents 与 claude code/codex。"github" 从开源词表里删掉了——
  它让每条 GitHub 来源的新闻都算开源。

线上 574 条已处理行按新规则重算，**311 条（54%）换了栏目**：AI Models 217→151（它原先是
垃圾场）、开源生态 22→117、论文与方法 33→68、公司动态 4→26、智能体 131→64、其他 60→34。
点名的两条已归位：*U.S. appeals court upholds designation of Anthropic…* → 公司动态，
*Revealing how OpenAI agents hacked Hugging Face* → 智能体。
`/topics` 现在显示 模型发布 99 · 开源生态 89 · 智能体 49 · 论文与方法 52 · 公司动态 24。

341 passed。剩下 4 条错的是关键词真分不了的（"Meta's AI Tamagotchi bet is...working?"
标题里没有任何栏目词；"Feds Target AI Critics as Foreign Agents" 里 "Agents" 又被智能体抢走）。

### v1.13 跨来源去重在没 key 的机器上从来就没生效过（2026-09-26）
**症状**：20:00 晚报里"OpenAI 特工入侵 Hugging Face"出现了两遍（Hacker News 一条、
The Verge 一条）。**根因**不是相似度算法：`dedup` 的 0.60–0.79 灰区**只在接了 LLM 时**才合并
（`ai_review_enabled` + `llm.enabled`），本机没有 key，于是灰区整段永不合并。
实测 3 天内 9 对灰区标题里 7 对就是同一件事，全被当成两条新闻推给了用户。

**做法**（延续 v1.12 的评测先行）：`tests/data/dedup_eval.yaml` 25 对线上真实标题，
我逐对判断"同一件事/两件不同的事"。规则：**灰区里两条标题共享 ≥3 个"故事词"**
（去掉介词冠词等停用词、以及 model/ai/released 这类到处都是的通用词）就判为同一件事。
在这 25 对上：`same: true` 10/10 合并，`same: false` 0 误并。
为什么不用阈值下降解决：假阳性对 "0.157.1 released in openai/codex" vs "两个codex邀请码自取"
打到 **0.700**，而真重复对只有 0.61–0.73——**光调阈值分不开**，必须换信号。
也不引入语料词频（试过 rare(df≤8)，召回 8/9 但会把 RAPID/Radix 那种错并），
最简的 V1 反而是全对。`rule_merge_shared_terms: 0` 可以整条关掉，退回只靠 AI 的旧行为。

顺带修掉两处相关缺陷：
- `query_articles` 早就是**按 event_id 在读取时折叠**多来源的，但 event_id 来自
  `sha1(title_key)` —— 标题一改写就不是同一个 key，所以这套折叠对转述稿永远无效。
  现在入站判定会命中，兄弟行会挂到同一事件上。
- 回填：把 3 天内已入库的 6 条转述稿并到各自事件的原始行上（保留原始行，
  `event_id` 可随时用 `make_event_key(title)` 复原，不销毁任何数据）；
  事件入选规则从"分数最高"改成"**一手来源优先 → 最早 → 分数**"，
  所以 GPT-6 那条留的是 OpenAI 的发布而不是 AWS 的上架文。
  同时清掉 5 个被搬空/统计失真的事件行。真实验证：多来源事件从改前 9 个 → 回填后 13 个
  → 统计校正后 12 个，其中最大一组 4 成员跨 3 个来源（澳大利亚入侵政府网站那条）；
  两份简报里 Hugging Face 事件各占 1 条，10 条与 8 条链接全部互不相同。

已知边界：标题写得毫无重合的同一事件（"Revealing how OpenAI agents hacked Hugging Face"
vs "Irregular: rogue AI cyberattacks…"）任何标题法都合不上，那需要 `same_event` 的模型判断。
345 passed。

### v1.14 问答路径的两处死路（2026-09-26）
没有配 LLM 时，Bot 的自由文本问答完全走 `rule_intent` + 列表兜底。那两条路各有致命一步：

1. **"关注"一个词把新闻问句判成设置请求。** `rule_intent` 的 settings 触发词里有裸的
   "关注"，而"最近 AI Agent 有什么值得**关注**的？"是他最自然的问法——结果只回一句
   "请使用 /settings…"，问新闻问不出新闻。现在问句标记（？/什么/哪些/最近/值得…）优先，
   只有不带问句的"我想关注 NVIDIA""把推送时间改成早上七点"才判 settings。
   新增 `tests/test_intent.py`：16 种真实说法逐个断言意图（这个文件第一次跑就抓出
   "改成/推送时间"没有被认出来，是我原先漏掉的）。
2. **列表回答打印英文标题，而中文就在上一行刚取到。** `answer()` 先调
   `ensure_chinese(results)` 再渲染 `F.link(a.summary or a.title, …)` —— 花钱翻好的中文
   被扔掉，用的是英文原文。三处渲染点改成 `display_line`（与简报同一套中文优先规则）：
   无模型的列表、AI 调用失败后的列表、以及 `article_card` 里限免工具名的回落。
   `digest_header` 的兜底字符串也从 "AI Briefing" 改成 "AI 简报"。

线上真实验证（生产库、无模型路径）："最近 AI Agent 有什么值得关注的？"→ search，12 条，
中文标题且品牌名保留（"最近，Anthropic 宣布未来的 Claude 模型…"）；"我想关注 NVIDIA"
仍然正确落到 settings。362 passed（+17）。

已知遗留：列表行用的是 `display_line`（中文摘要优先），所以对"播客/自述式"稿件会显示第一
人称的句子而不是标题——这是"头条用摘要还是标题按来源区分"那条待决设计问题，需要他定夺，
本次改动没有换字段（原来也是摘要），只把英文换成了中文。

### 仍未解决
头条"摘要 vs 标题"是否按来源类型区分未定；**`LLM_*` 仍未配置**（所有摘要都是规则式首句，
这是唯一未动的质量杠杆；专名译错已由占位符挡住，但句子仍有机翻味）；
`GITHUB_TOKEN` 在 304 之后已非必需但 27 个仓库仍贴着实名上限跑；限免"已推送"账本是全局而非每订阅者；
免费翻译日配额几乎每天入夜用尽；突发门槛的事件词是正则，换语种标题（如纯中文来源）需要另配词表；
`key_points_zh` 只给真正要展示的那几条补翻（后台翻译轮不处理要点），所以 /搜索 的历史结果里
仍可能看到英文要点；含品牌名的句子会先被 MyMemory 拒一次再由 Google 接手，多一次往返；
规则模式栏目仍有约 7% 判错（标题里没有栏目词的那些，见 v1.12），要再往上走只能靠 `LLM_*`。
