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
| `/settings` | 早报/晚报各自的开关与时间、突发新闻开关、评分门槛（点一下告诉他这一档还剩几条）、暂停开关 |
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

# 投递对账（只读）：某个订阅者的简报到底发没发、账上有没有、发送层日志怎么说
.venv/bin/python scripts/delivery_report.py --hours 168 --kind morning
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
.venv/bin/python -m pytest            # 481 个用例
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
| 单元测试 | 481 个用例：开发机 Windows 全绿，GitHub Actions 在 Python 3.12 与 3.13 双矩阵 `success`（每次推送都跑）；两台服务器跑的是同一份代码树（md5 一致） |
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

### v1.15 把剩下的渲染面全部扫了一遍（2026-09-26）
前五轮都在单点修"我们自己的文案是英文"，这次改成**穷举所有渲染出口**：在生产库上把
`news_list / section_blocks / article_card / breaking_card / free_offer_list / status_line /
help_text / /sources / 问答 answer / deep_summary` 全部渲染出来，剥掉标签和 URL 后统计
中日字符与拉丁长串。结果：除 `/sources` 外全部干净。

- **`/sources` 打印内部取值**：`rss · A 级` 这种拼接把采集器类型码和分级字母直接给了用户。
  新增 `labels.source_types` / `labels.quality`（settings.yaml），
  `AppConfig.source_type_label()` / `quality_label()` 读取，渲染改为
  `RSS 新闻源 · A 级（一手官方）`；`--self-check` 的采集器列表同步。
  键仍是数据库里的真实取值，只改说法。
- 线上真实输出核对：`🟢 OpenAI / RSS 新闻源 · A 级（一手官方） · 上次成功 … · 采集 51 条`。
- 其余 surfaces 的审计数字（去标签后中文 vs 拉丁）：news_list 319/216、section_blocks 411/378、
  help 179/179、breaking_card 81/44、free_offer_list 170/236 —— 拉丁部分全部是品牌名与
  来源名（Hugging Face、Amazon SageMaker HyperPod），没有第三处内部取值泄漏。

362 passed。过程里我自己写错了一次断言（把整串 `.lower()` 后再找 " rss"，结果匹配到
正确标签 "RSS 新闻源" 自己），已改成匹配旧格式的 `r"\brss\b ·"`；另外两次 Edit 误删了
`category_label` 的 docstring 和 `subcategories` 的函数体，都在下一步补回并跑全量确认。

### v1.16 数据源登记表与配置同步，顺手接上一个即将发生的磁盘故障（2026-09-26）
- **`sources.enabled` 是创建时的快照，配置改了不会跟。** 采集循环里 `get_or_create_source`
  永远以 `enabled=True` 建行，之后再没人更新过：VentureBeat AI 在配置里 2026-09-26 就关了
  （WAF 对机房 IP 回 429，六小时失败 69 次），库里却仍是 `enabled=1, error_count=114`，
  于是每六小时维护就播一次"这个源坏了 114 次"——一个他已经关掉的源。
  新增 `repo.sync_sources(session, config.sources)`：每轮采集开头把 enabled / url / quality /
  type 对齐配置，关掉的源同时清掉失败计数（重新打开时不该一上来就带着旧账），
  配置里删掉的行也置为关闭；`run_maintenance` 只报仍然启用的源。
  线上核对：两台库的 VentureBeat AI 与 Linux.do 最新话题 都变 `en=0 err=0`，
  库里"启用"数从 22 变 20，与 `/stats` 早就算出的配置值终于一致。
- **同步自身要幂等，否则每轮都在"修"同一批行。** 部署后日志连着两回合报
  `25` 与 `14` 个字段被更新——采集循环会把**每条新闻自己的 URL** 写进 `sources.url`，
  同步再把配置里的 feed URL 写回去，两边每轮互相覆盖。这是同步暴露出来的老 bug
  （`sources.url` 一直是某篇文章的地址），修法是让条目路径不再写 url。
  回归用例先证明会失败：把那一行改回去，新测试立刻红；改回来则收敛为 0。
  线上复验：部署后 rss/hackernews/github/arxiv 四回合采集（fetched=395）都没有再打印同步行。
- 数据源登记这块**原先一个测试都没有**（`/stats` 的源数量、维护告警都读它）。这次补 9 个。
- **磁盘：09-26 实测只剩 769MB，而 `/var/log/syslog` 与 `daemon.log` 是同一条流的两份拷贝，
  各自 877MB、约 131MB/天，合计 ~260MB/天 ≈ 3 天写满。** 写满的表现不是报错，
  而是 SQLite 静默拒绝写入、"新闻突然不采了"。日志量来自他另一台服务 `mmwx`
  （INFO 级逐请求计时，占 syslog 行数的 74%）——改它的日志级别或 rsyslog  duplicating
  属于另一个服务的决定，所以这里**没有动手**，只把风险变成会说话的东西：
  `stats()` 现在带 `disk_free_mb`，低于 `alerts.min_free_mb`（默认 1024）时
  `/stats` 末尾出现 `⚠️ 磁盘只剩 0.8GB（低于 1GB 告警线）…`，维护日志同步告警。
  线上已实际触发。

371 passed。过程记录：我用 heredoc 写多行 f-string 时把 `disk_warning` 的返回语句写成了
语法错误（9 个用例在 collection 阶段就红），当场改回；第一次"验证测试能抓 bug"用了错误的
`-k` 过滤词（选中 0 个用例却看起来像通过），换成点名单个用例后确认旧代码确实失败。

### v1.17 重启不再让 GitHub 配额从头烧一遍（2026-09-26）
排查采集覆盖面时先排除了两个嫌疑：抓来的条目没有一条是"库里没有的"
（OpenAI / Hugging Face / NVIDIA / arXiv 各 42-50 条全在库），全站 `url_hash` 复用为 0，
所以去重没吃掉新闻，那几个官方源确实是安静的。真正的洞在配额上：

- `_rate`（匿名 60 次/小时，按 IP 共享）只活在内存里。每次进程重启都以"以为还能调"开始，
  把 27 个仓库重新问一遍，才在 403 里重新发现窗口已经用完。当晚为了部署重启了九次，
  两个 GitHub 源的错误栏就是这句"匿名配额只有 60 次/小时，已用完"。
- 现在把 `{remaining, reset}` 落到 `data/github_rate.json`（每轮写一次，且数值没变就不写），
  新进程在真正发请求前继承未过期的窗口；窗口一过期就重新探测，不会永久封着。
  已有的每分钟一次 `/rate_limit` **不计费**探测仍然在位，所以"继承来的封锁"最长只维持 60 秒
  ——真实配额回来时它立刻解封（17:12:45 那轮就是这样恢复到 quota left=9 的）。
- 线上直接验证：把文件写成 `remaining=0, reset=now+20min` 并以空内存启动，
  判定函数返回"约 N 分钟后恢复"且没有发出任何请求。
- 三个防御性用例：未过期窗口被继承、过期窗口不继承、JSON 坏了绝不阻塞采集。

顺手修了自己埋的两处：`save_rate` 一开始挂在 `if self._etag_dirty:` 里面（纯 304 的一轮学不到
配额数就不落盘），移到轮末；而它从不记录"自己写过什么"，那个去重判断其实永远不生效。

374 passed。

### v1.18 中文问句终于能搜到东西了（2026-09-26）
上一版把渲染面全扫成中文之后，检索仍然只认英文——线上实测（01:35 北京时间，705 行 /
其中 588 行有中文标题）：`开源模型`、`芯片`、`融资`、`最近有哪些模型发布`、`英伟达`、
`机器人`、`大模型推理成本`、`开放权重模型` **八条全是 0 命中**，而 `agent`、`GPU 涨价`
这种带英文的词各回 10 条。两个独立的洞：

1. `repo.query_articles(search=)` 的 `or_()` 里只有 `title/content/summary/why_it_matters`，
   全是英文列——译文存在 `title_zh/summary_zh`，从来没进过 WHERE。
2. `clean_query` 按空白切词，中文没有空白；`LIKE '%模型发布%'` 要求四个字连写，
   而译文写的是"阿里**发布**了新的大**模型**"。

改法不是加个分词器（SQLite 没有，也不想为 700 行引一个依赖），而是把一次查询变成
**一组带权重的 LIKE**：整句 3 分、用户实际打的词与对照表另一头的写法 2 分、
二字窗口 1 分，一行得到的分数是它命中的所有模式之和，再按分数→重要性→时间排序。
这样"连着写"依然最优，但不要求它必须连着写。

- 二字窗口：`模型发布` → 模型/型发/发布。`_subterms` 只在 `的` 处断开——
  `和/与/了` 看着像助词，切下去会把"饱和"切成"饱"+"和"造出假词。
- `search.aliases` 中英对照表（11 组，配置可改）：品牌与术语在译文里按设计**不译**，
  所以中文词在库里根本不存在。活库实测 `英伟达` 0 行 vs `NVIDIA` 34 行、
  `芯片` 0 行 vs `chip` 14 行、`涨价` 0 行 vs `价格` 6 行——不打对照表就只能回答"没有"。
  对照展开上限 6 条，窗口上限 6 条，一次 `/搜索` 最多 11 次 LIKE。
- 精度地板 `MIN_MATCH_WEIGHT = 2`：只命中一个二字窗口的行**不算答案**。
  线上加地板前实测 `量子隧穿` 靠"量子"两个字捞回 3 条量子计算新闻——那是最有把握的错。
  代价也如实记录：`最近有哪些模型发布` 从 10 条变成 1 条，被砍掉的 9 条是
  "google/adk-python 发布 v2.10.0" 这类只含"发布"不含"模型"的仓库发版条目。
- `/免费` 里两处工具过滤原本只拼 `title/summary/content`，现在走 `_haystack()` 一起看
  译文列；否则卡片上明写的名字，过滤器说没有。

线上复测（同一台机器、同一套库、部署后）：13 条问句里 11 条命中，
`量子隧穿`、`区块链挖矿` 如实返回 0；最慢一条 0.434s（第一条，冷缓存），
其余 ≤0.08s。

过程记录：改动前 `tests/` 里没有任何一处调用 `SearchService.search()`（只有一行无关的
`re.search`），所以这次先补 `tests/test_search.py` 16 个 + `/免费` 过滤器 1 个。三次变异验证各自确认对应
测试真的会红：删掉译文列两行 → 3 红；删掉窗口 → 3 红；删掉对照展开 → 3 红；去掉精度地板 →
1 红。第一次改 `search()` 时我用 Edit 把函数体写成了半行海森堡语法（`with session_scope() in_scope :=`），
下一步立刻覆盖重写，最终文件的 md5 与备份一致后才继续。

391 passed。

### v1.19 中文问句能答了之后，把"答得慢"和"答半句"修掉（2026-09-26）
v1.18 部署后第一次跑端到端问句，暴露两件事——都是只有线上才看得见的：

**一条问句 9.67 秒。** 检索本身只花 0.04s，慢在 `ensure_chinese()`：它按**整行**判断
"要不要翻"，然后无条件把这一行的标题、摘要、要点全部再问一遍 provider。
库里标题摘要早就翻好、只有要点缺翻的行，每次被列出来都要重问两次（变异验证：
把逐字段过滤去掉，单测里就多出 2 次请求）。12 行的中文问句实测 **9.67s → 0.10s**。
同时补上"这个面渲染什么就翻什么"：`/news` 列表、简报、聊天回答都没有要点块，
改传 `with_points=False`，不再为看不见的东西花免费配额；卡片（点开某条）仍然补全。

**答案只回答半个问题。** `芯片涨价了吗` 第一条是"价格标签明显高于先前的价格"——
一个芯片的影子都没有。原因：`涨价` 命中 price 对照组的 `价格`，一行拿到 2 分就过了地板，
而中文没有空格，`clean_query` 把整句当成一个词，"AND" 这件事 LIKE 说不出口。
现在按**概念**要求：一次问句如果碰到 ≥2 个对照组，一行必须覆盖其中 2 个
（上限就是 2，三个概念的问句很少有一行全含，宁可少说也不空手）。两个配套细节：
- 表里共享同一个词的两组先合并成一个概念——`芯片` 同时挂在 `chip` 和 `semiconductor`
  下面，分开数会让"只讲芯片"的行冒充满足两个概念；
- `clean_query` 去掉句尾语气字（`…了吗`、`…有哪些`），既让窗口切得干净
  （`芯片涨价` 而不是 `芯片涨价了吗` 的 `价了`/`了吗`），也让回答里回显的
  查询不再写成"开源模型有"这种缺半句的样子。

线上复测（同一套库）：`GPU 涨价` 10 → 4 条、`芯片涨价了吗` 5 → 4 条，少掉的那条正是
探针里点名的"为人工智能奠定材料基础"（只提芯片、没提价格）；13 条问句仍然 11 条命中、
`量子隧穿`/`区块链挖矿` 如实 0；回显文案修好；冷启动最慢 0.444s，其余 ≤0.13s。

395 passed（新增 4 个用例：逐字段翻译 1、概念 AND 2、句尾语气字 1）。
这一版有四次变异验证：逐字段过滤、概念覆盖、对照组合并、句尾剥离，各自确认对应测试会红。

### v1.20 卡片里的"核心内容"不再是薛定谔的块（2026-09-26）
顺着"中文还有哪里没覆盖"量了一遍。第一个探针量错了东西：我拿
`needs_translation(原文标题)` 去数，得到"587 行还需要翻译"——那表达的是"上游是英文"，
不是"他看到的是英文"。改按显示层量（近 14 天他真能打开的 423 行）：

- 英文标题 0 行、英文摘要 5 行——翻译这条线基本是好的；
- 但 **295/423 行带着要点却整块不显示**：`display_key_points` 只肯返回中文要点，
  而后台翻译轮从来只处理标题和摘要。线上抓的样例行 #472 卡片长这样：标题、五项分数、
  来源、标签，**没有一句话总结也没有核心内容**。
- 更难看的是 `settings.yaml` 里已经写着"要点排在标题和摘要之后，剩余额度才轮到它"好几周
  ——配置对代码的空头承诺。"文案说的门"和"真正在开的门"不一致，是本项目反复踩的同一脚。

改三处：
1. `translate_pending()` 真的去翻要点：分数优先（能上简报的那几条先受益），每轮最多送
   `translate.points_per_run` 条字符串，翻出来写进 `meta.key_points_zh`；一行问两次还没
   结果就不再问（与正文提取的 `meta.enriched` 同一套路，否则一句 provider 永远翻不动的
   句子会每天吃掉整条预算）。返回计数按"行"取并集——标题段和要点段同时改到同一行只算
   一篇，`translated N article(s)` 那行日志不能说谎。
2. 额度没轮到时不再把块藏起来：核心内容照出，标题写明
   `核心内容（以下为原文，中文翻译还没轮到）`。他在本线程里说过"免费额度用完了直接显示
   英文就行了"；"看起来像卡片坏了"和"看起来像翻错了"都不如一句实话。
3. 空标题队列是常态（跑了一周的机器每轮只有 1-2 行待翻），早退路径也必须走要点段。

线上实测：部署后第一轮就打出 `translated key points for 21 article(s), 40 of 40 string(s) asked`，
连着两轮共 42 行补上中文要点（其中 27 行落在"近 14 天 + 分数≥45"这个可见窗口里，
窗口外的是更旧或更低的行，队列按分数排，所以先到的是他更可能打开的）；
随后 `translation budget exhausted; 21 item(s) stay in English`
——日额度 400 次守住了，没把免费 provider 的 IP 配额打穿。复测同一窗口：前 80 行可见行里
**60 行有中文要点、0 行仍然藏着**，渲染出来是一句中文总结加两条中文要点。
标注英文那条分支这轮线上没触发到（都翻上了），目前只有单测覆盖。

399 passed。新增 4 个用例（背景回补、不重复计数、两次即止、卡片标注）。改了测试自身两处：
`StubTranslator` 补 `budget` 属性（真实 Translator 有，桩没跟上，不是放宽断言）；
原"卡片只显中文要点"的用例保留，因为那是中英混合的行，新策略只在整行都没有中文时回落。
三次变异验证：删掉要点段 → 2 红；还原空队列早退 → 2 红；显示层改回只留中文 → 1 红。

### v1.21 一行处理失败会把整轮带走：两处会话级事故（2026-09-26）
部署 v1.20 之后例行看日志，`logs/scheduler.log` 里有 Traceback。顺着查出来两个真问题，
都在"处理轮"这条最不该断的链路上（统计窗口 = 两台机器各自最近约 30 小时、
各 202/203 轮成功处理之后仍然发生的事故）：

**事故 A：`UNIQUE constraint failed: article_tags`（anr-vps 5 次）。**
`attach_tags()` 判断"这条链接是否已存在"读的是 ORM 的关系集合
（`{tag.id for tag in article.tags}`）。调度器的会话活得比一轮长，中间表被另一个
会话（并发采集/上一轮）写过之后，这个集合就是过期的 —— 于是同一个
`(article_id, tag_id)` 被插第二次，错误在 flush 里爆出来。
现在改成以链接表为准（集合 + `SELECT tag_id FROM article_tags WHERE article_id=?` 取并集）。

**事故 B：`PendingRollbackError` 冒出整个 job（两台各 1 次）。**
`process_pending()` 的 `except` 分支在会话已经被 flush 失败污染之后，第一件事是
`article.process_attempts = (article.process_attempts or 0) + 1` —— 读一个被 expire 的
列又要走数据库，直接抛 PendingRollbackError，异常从 except 里逃出去，
整轮剩下的文章全部没被处理，`finally` 里的 `session.commit()` 同样失败。
代码注释写着"三次之后放弃，不让一颗毒行堵住队列"，但那道保险永远轮不到执行。
anr-jump 上第一次事故的原始异常是 `database is locked`（`busy_timeout` 已经是 30 秒，
说明是长写事务/checkpoint 撞上的，另案），同一个 handler 缺陷把它也放大成整轮失败。

修法：except 里先 `session.rollback()`，再 `session.get()` 重新取那行来记
`process_attempts/process_error/is_processed`；提交从 `finally` 挪出来单独 try/except
（失败发生在 commit 时也是同一类事故），一行最多拖掉它自己。

测试：两个复现用例都先跑出与线上完全相同的错误串再修
（`test_attach_tags_ignores_a_link_another_session_already_wrote` 用第二个会话插链接、
`test_one_poison_row_does_not_abort_the_whole_round` 在 flush 里制造重复键）；
两处修复各做一次变异验证，还原后分别报
`sqlite3.IntegrityError: UNIQUE constraint failed: article_tags...` 和 `PendingRollbackError`。
401 passed。

### v1.22 突发漏掉的正是最大的那条：给"全站都在看"开一道门（2026-09-26）
按 v1.21 立的规矩（部署后先 grep 日志）顺着查突发，把近 7 天的门重新算了一遍：
431 行里事件词命中 21 行，而** heat 最高的四条全被挡在门外** ——

| 标题 | heat | 评分 | 来源质量 | 判定 |
|---|---|---|---|---|
| Revealing the details of how OpenAI agents hacked Hugging Face | 645 | 76 | 65 | ❌ source quality 65 < 80 |
| U.S. appeals court upholds designation of Anthropic as supply-chain risk | 480 | 78 | 65 | ❌ 同上 |
| OpenAI agent hacked Australian government website, PM says | 254 | 70 | 65 | ❌ 同上（且采集时已 34h） |
| Australia says OpenAI agent hacked into government website | 253 | 70 | 50 | ❌ 同上（35h） |

一手媒体发的六条（Akamai $11.6 billion、Court rules…）能过，
唯独"整个社区都在看"的两条大新闻过不了 —— 因为它们是从 Hacker News 进来的。
`min_source_quality: 80` 的设计动机（同样的词出现在 Show HN 自荐里）是对的，
但它把"事件 + 645 赞 + 采集后 1 小时内"这种行一起挡住了。

于是加一道**只破"来源质量/评分"两道门**的例外：`breaking.rule.min_community_heat: 250`。
事件词、24 小时时效、`exclude_sources` 黑名单一概不放松，所以 Reddit/GitHub Trending/arXiv
不会因为高票混进来，Show HN 也进不来（`exclude_titles` 仍然先拦）。定在 250 的依据：
同一周 431 行里 418 行 heat=0、只有 13 行 ≥100，一周多放行 2-4 条，冲不破每天 5 条的上限。

线上复算（同一窗口、同一套数据，只换代码）：**通过门的行 6 → 8**，
新进来的正是上面 heat 645 与 480 两条；两条 34-35 小时后才采集到的澳洲新闻仍然被
时效挡住（正确行为——真到了那个时候再叫醒他没意义）。
`/设置` 里的说明同步改成"标题里有大事件 + 一手来源 + 24 小时内，**或全站热度 ≥250 的大事件
（社区来源也可破例）**"，显示的门与代码的门再次一致。

排查过程中也记一次自己的错判：我先用 `updated_at - created_at` 算"处理延迟"，得出
"322/708 行处理晚于 20 小时"，据此以为突发是被队列积压拖死的。实际量下来
`unprocessed_articles` 当前 0 行、近 48 小时入库的 708 行全部已处理 ——
`updated_at` 会被翻译、正文提取、`is_sent` 等后续写入不断推后，**它不是处理时钟**。
剩下的差额（今天复算 8 条会过门，线上只有 1 条 `is_breaking=1`）方向是清楚的但没法逐条
归因：v1.9 之前处理的那些行当时面对的还是"评分 ≥90"那扇打不开的死门，而库里没有
"这条是什么时候被处理的"这一列。已记进"仍未解决"：要么加 `articles.processed_at`，
要么这类问题永远只能靠日志时间戳反推。

407 passed（新增 6 个门用例：破例放行、无事件词不放行、过期不放行、黑名单不放行、
热度差 1 不放行、/设置 文案与门一致）。两处变异验证：完全去掉破例 → 2 红；
破例忽略门槛值 → 1 红。另修一处自己写出来的重复分支（`if not trigger` 被写了两次）。

### v1.23 卡片上第一次出现"为什么值得关注"，以及一只能证明顺序对的时钟（2026-09-26）
两块内容：

**① `articles.processed_at`。** v1.22 记录过一次错判：我用 `updated_at - created_at`
推断"322/708 行处理晚于 20 小时"，而 `updated_at` 会被翻译、正文提取、`is_sent` 反复推后 ——
它不是处理时钟，于是"这条什么时候被判定为突发"在库里根本没有答案。现在有了：
新列走既有的 `_LATE_COLUMNS` ALTER 路径（两台机器 `PRAGMA table_info` 已确认列在），
`_apply()` 与 `_settle_filtered()` 各写一次。维护轮同时新增积压告警
（`processing backlog: N row(s), oldest waiting X.Xh`，≥6 小时升级为 warning），
因为积压正是突发静默失效的上游原因（门在发布 24 小时后过期）。
线上实测：03:57 那一轮采集到 2 行、处理 2 行，两行都带上了 `processed_at=19:57:15`。

**② 规则模式的 `why_it_matters`。** 实测近 14 天 423 行可见行里 **0 行有值**
（`fallback_summary()` 两条分支都返回 `""`），所以卡片上"为什么值得关注"这块在没 key 的
机器上从来没出现过 —— 和 v1.9 那把打不开的 🔥 是同一类洞。补了一个只从既有事实里
拼句子的 `compose_why_it_matters()`：同一事件的其他来源与名字（来自事件归组）、
社区热度、一手来源自己发布的事件性消息，三句按事实拼，全部命中才写三句。

关键是**锚点规则**：没有"多来源报道"或"标题里有事件"这两个锚点之一就返回空串，
热度只负责加强、不单独成句。理由是在线上真实数据上试跑发现的：
"Show HN: Whiteboard…"（heat 407）和 "yukitorido/short-video-generator-AI（612 stars）"
（heat 717）都会拿到一句"社区热度 717（GitHub Trending）"当"为什么值得关注" ——
那是把排名机制复述一遍冒充理由。收紧之后线上复算，样本第一条变成
"U.S. appeals court upholds designation of Anthropic…"（事件 + heat 481）这类真消息。

顺带第一次在线上看到了 v1.20 那条"原文标注"分支的真实输出：
一条 Reddit 讨论的卡片渲染成"核心内容（以下为原文，中文翻译还没轮到）"，
之前这条分支只有单测覆盖。

413 passed（新增 6 个用例：一手+事件、多来源点名、无信号留空、heat 不单独成句、
事件+热度的中文措辞、processed_at 写入）。变异验证四处：去掉 `compose` 调用 → 2 红；
去掉 `processed_at` 写入 → 1 红；把"无锚点留空"改成硬凑一句 → 1 红；去掉锚点早退 → 1 红。
过程中自己写坏过两次测试文件（Edit 时误删相邻用例的 docstring，当场补回并整轮重跑）。

### v1.24 0 星的仓库不该占一个早报名额（2026-09-26）
预览 08:00 会发出去的内容时看到第 10 条是
"scc0819-cell/agent-budget-router 收获 0 星"（分数 54，门槛 45）。查了下：
`sources.yaml` 里 GitHub Trending 一直写着 `min_new_stars: 10`，采集器也确实把
`stars:>=10` 拼进了查询串 —— 但库里 `meta.stars=0` 的行有 30 条（集中在 09-25 12:55 一轮），
最近 24 小时还有 1 条。原因不在代码里：`stars:>=N` 是 GitHub **搜索索引**的承诺，
而索引会滞后（星数掉下去、仓库改名/重建之后仍会被返回），所以服务端过滤挡不住。

修法是把门槛在客户端再核一遍：每条查询带着自己的 floor
（created 用 `min_new_stars`、pushed 用 `min_stars`），返回的仓库
`stargazers_count < floor` 就丢掉，并打一行
`github <source>: dropped N repo(s) under the star floor` —— 不静默吞掉，
这样"今天又漏进来多少"是可查的。这类"上游过滤要在使用点复查"的规矩，
和 `is_blocked_url` 在入库前二次检查是同一道理。

04:12 那一轮（部署之后）真实跑过：GitHub Trending 正常返回、没有触达丢弃分支，
所以线上只证明了"不打日志=没漏"，过滤逻辑本身是单测证明的
（用一条 11 星、一条 0 星、一条正好压线 10 星的真实形状响应断言 `[11, 10]`）。
写测试时自己踩了个教训：先写的断言是 `"0 stars" not in title`，
而标题 `"edge/ten (10 stars)"` 里就含着 "0 stars" —— 子串断言会假装失败/假装通过，
换成比较结构化的 `meta.stars` 之后才说真话。

414 passed（+1）。变异验证：删掉客户端复查 → 该用例红。

### v1.25 免费机器把 50t/s 译成"50吨/秒"：单位也要过占位符（2026-09-26）
预览 08:00 会发的内容时读到的第 9 条：「速度可达 50吨/秒」。原文是 `50t/s`（tokens/second）。
同一批里还有 `0.9 KB/token` → "0.9 KB/令牌"。数字留着、量纲换成中文里根本不通的词，
这是"对一条性能说了假话"，不是"译得软"。

单位走品牌词同一套占位符机制（`translate.keep_units`，15 个只可能是英文写法的
带斜杠吞吐单位）。**裸词 token/tokens 故意不保护**：`approval token` 译成"批准令牌"
是对的中文，保护它反而把好句子变成中英夹杂 —— 一条规则要能被反驳一次才算想清楚。

实现里逼出两个真 bug（都是加单位之后才暴露的）：
- `_MAX_KEPT=35` 原来截的是**词表**（`sorted(...)[:35]`），按长度降序 →
  最短的那批永远不被保护。品牌表 34 项 + 单位 15 项之后，"t/s" 正好被切掉：
  加了单位等于没加。改成上限只约束**这一条文本里的占位符个数**（圆圈数字到 ㉟ 为止），
  词表规模不再影响谁被保护。
- 边界断言 `(?<![A-Za-z0-9])t/s(?![A-Za-z0-9])` 拒绝 "50t/s" —— 数字算"在词里面"。
  现在带 `/` 的条目用字母边界（数字可以紧邻），品牌与 `GPT-5` 这类仍用原规则：
  让 "Qwen" 在 "Qwen3-TTS" 里被保护是错的（那是另一个产品的名字），
  这条差异是测试 `test_adjacent_and_multiword_names_travel_as_one_placeholder`
  在我把边界一刀切松之后立刻变红教我的。

已经存进库的错译不能靠占位符救，所以补了不变量修复 `repair_lost_units()`：
英文里有受保护单位而中文里没有 → 该字段作废、清回队列重译（与专名清扫同一个道理，
不是手抄一份"坏译法黑名单"）。开机时随其他修复跑一次。

线上验证：部署后启动日志 `dropped 2 Chinese field(s) that lost a guarded unit; requeued`
+ `requeued 2 Chinese line(s)...`；一轮翻译跑完后复扫 14 天可见行，
**9 个带受保护单位的字段：保住 9、丢单位 0、待重译 0**；那条 Reddit 现在写
「…速度为 50 t/s」（另一行 "13-15 tps" 也原样保留），"吨/秒" 全库为 0 命中。

418 passed（+4）。三处变异验证：去掉斜杠单位的宽松边界 → 3 红；
把上限改回截词表 → 1 红；去掉作废重排队那一步 → 1 红。
过程中我自己埋的两个错也记一下：`keep_units` 第一次插进了 `keep_terms` 列表尾部，
把 GitHub/Kubernetes/PyTorch/Triton/CUDA/vLLM/SGLang 七个品牌吞进单位表
（配置结构错位，靠"读一个真实 Translator 的 keep_terms 当测试常量"才发现）；
测试文件被 Edit 打断过一次装饰器配对，修好后整轮重跑。

### v1.26 08:00 那扇门第一次被真的测了：窗口、账本、每个读者（2026-09-26）
早报是他最早遇到的故障（某天 08:00 静默没发）。判定的全部逻辑在
`NewsJobs._digest_due()` 里 —— 而它**一条测试都没有**：已有的用例要么只测
"没发出去时要不要记账"（`_note_miss`），要么直接把 `_digest_due` 桩掉。
也就是说，"08:00 会不会发"这件事只在线上出过一次事故、修了一次，从没被固定下来。

补了 9 个用例（含参数化的窗口矩阵），用一只冻住的钟问它：
`07:59 → 不该发`、`08:00/08:20/08:45 → 该发`（宽限期 45 分钟，边界值也测）、
`08:46 → 窗口已关`、时间串写坏 → `bad time` 而不是抛异常；
昨天发过不影响今天、别人发过不影响我、早报发过不影响今晚。

顺带把这条链上的**账本归属**修干净了：`pushes_since(user=None)` 的语义是"不加用户过滤"，
于是任何一步取不到读者行时，"今天已经发过吗"和"每天最多 5 条突发"就变成查所有人的记录。
现在仓库层直接拒绝这种退化（`user=None` → 0 条 / 无上次推送），
调用点改用 `repo.ledger_user()`（取不到就按该 chat 建一行，账本永远属于某个人）：
`_digest_due`、`can_send_breaking`、`record_delivery`（含 breaking 分支）、
`free_alerts._may_send` 四处。

变异验证说清楚了两层各自挡什么：只回退仓库层 → 全绿（因为调用点现在总给得出用户）；
**两层一起回退 → `test_another_readers_send_does_not_silence_this_one` 变红**，
这正是原始 bug；把宽限期从 45 改成 5 → 矩阵里两格变红。改测试自身两处也是这个原因：
`test_cooldown_and_daily_cap_are_respected` 以前用 `record_push(user=None)` 写账本、
再指望 `_may_send(111)` 看到它——那是把全局计数当冷却，改成写给 111 本身并新增一条
"另一个读者不该被挡住"的断言；`test_new_offer_is_pushed_once_and_marked` 同理，
顺手断言"推送账本必须落在具体读者身上"。

线上预演（不改数据、只把钟拨到明早 08:05 问一次判定）：
真实订阅者 1985298804 → `(True, 'due')`，明晚 20:05 → `(False, 'not yet due')`；
`push_logs` 里 `user_id IS NULL` 的行 = 0。库里还留着一行 chat=111111111 的用户
（我早期探针建的），`chat_ids()` 会按 `.env` 白名单过滤掉它，不会多发。
**"发出去没有"要等明早 08:00 之后看 `push_logs` 才算证**，这一条只能那时结。

427 passed（+9）。

### v1.27 采集层是最没人管、又最容易"静默零产出"的一层（2026-09-26）
用 coverage 把整个套件跑了一遍（本地 `.venv` 里装 `coverage`，不进 requirements、
不上服务器）：总分支覆盖 74.7%，最薄的正是把 API 响应变成条目的那几个采集器 —
`hackernews 24.3% / arxiv 22.1% / reddit 18.8% / youtube 19.8%`。
这一层的危险方式和 v1.9 那扇打不开的突发门一样：**上游改字段不会报错，只会让某一源
变成"今天没什么新闻"**，而他看到的只是条目变少。

先按数据决定投哪里（近 7 天实际产出）：hackernews 58 行、arxiv 42 行、github 74 行、
rss 546 行；reddit 采集器 0 行 — 一查配置，5 个 reddit 源全是 `enabled: false`
（.json 对机房 IP 403，我们走的是 Reddit 的 .rss，那 137 行挂在 "Reddit LocalLLaMA RSS" 下）；
youtube 源同样 `enabled: false`（phase 3 未启用）。所以这轮把力气花在真正在跑的 HN/arXiv，
reddit 只补了纯函数（哪天他打开不至于坏）。

新增 `tests/test_collectors_parse.py` 9 个用例，fixture 的形状来自**部署机上抓的真实响应**
（HN front page 那条 "Revealing the details of how OpenAI agents hacked Hugging Face"
heat 683、`created_at 2026-09-25T21:09:27Z` 就是当天线上数据）：
- HN：外链帖用外链入库（和 RSS 覆盖同一篇文章时才能去重）、Ask HN 无外链回落讨论页、
  同一条出现在两个查询里只留一条、heat 取自 points、
  **并把"我们依赖的服务端过滤"钉住**：请求里必须带 `points>=<min_points>` 与 `created_at_i>`；
- arXiv：Atom 解析（标题/摘要/作者串/arxiv_id/pdf 链接/主领域）、关键词门挡掉非 AI 论文、
  `sortBy=submittedDate&sortOrder=descending` 必须是这个（我实测过默认查询会回 2020 年的论文），
  空 feed 不炸；
- Reddit：置顶/广告/低赞/AutoModerator/跨版推荐帖一律不入，自帖按 permalink、外链帖按文章 URL。

顺带把一层容易踩的契约写进断言：采集器交的是**带时区的 UTC**，`build_article()` 才抹成
naive UTC；库里窗口比较用的都是 naive。我第一版断言直接写成 naive 就红了 — 红得有道理，
于是改成两层都测。

覆盖率变化：`hackernews 24.3% → 84%`、`arxiv 22.1% → 78%`、`reddit 18.8% → 65%`。
四处变异验证全部被抓：去掉 HN 关键词门、把 HN 外链换成讨论页、arXiv 排序改成 relevance、
reddit 不再拦置顶帖。这一轮只动测试与文档，没有改运行时行为，所以**没有部署**
（每次重启都会跑一轮采集、花掉共享的 GitHub 匿名配额，并且临近 08:00 不想动窗口）；
`app/` 与 `config/` 树哈希三台机器仍然一致。

436 passed（+9）。

### v1.28 两页早报只送到第一页时，旧代码会说"今天已发"（2026-09-26）
coverage 剩下的薄处里 `bot/sender.py 46.7%` 最扎眼：这是"他到底收没收到"的那一层，
而已有的三条用例只测了 HTML、markup 降级、"其它 API 错误不重试"。
给发送层补了 10 个用例（被屏蔽、限流等待、网络抖动重试、放弃时的日志、空文本、超长截断、
多页简报、以及 jobs 那一层的记账闭环），顺带挖出两个真问题：

1. **半份简报被当成已送达。** `send_digest()` 原来 `if await send(): sent += 1`，
   两页里第一页成功、第二页失败就返回 `1`；而 `_run_digests()` 判断的是 `if sent:` ——
   于是 `record_delivery()` 写下"今天已发"，**当天再也不会重试，他收到的早报永远缺尾巴**，
   且 push_logs 里看不出任何异常。现在任何一页没送到就返回 0 并打 ERROR
   （`digest ... stopped at message 2/2; not marking it delivered`），让 watcher 在
   45 分钟宽限窗内再试；最坏情况是第一页重复一次，比缺一页轻得多。
   jobs 层也补了闭环用例：假 sender 返回 0 → 账本 0 行；返回全量 → 才记 1 行。
2. **`send_many()` 从来没工作过。** `sum(1 for c in ... if await self.send(...))`
   在 `async def` 里是**异步生成器表达式**，`sum()` 直接
   `TypeError: 'async_generator' object is not iterable`。全仓库（含 docs、scripts、测试）
   零调用 → 按"没用就删"处理，不是修：留着一个从没跑通过的方法只会诱使人将来去调它。

另外把超长消息的兜底改成带记号的截断（`[:4096]` 是无声地半句截掉，现在结尾留 `…`），
以及"被屏蔽"只试一次不重试（`TelegramForbiddenError` 直接 `return False`，用例锁住这点）。
aiogram 那几个异常都是 `TelegramAPIError` 的平级子类，所以 except 顺序没有互相吞——
这点也顺手确认过（MRO 打印核对），不是猜的。

覆盖率：`bot/sender.py 46.7% → 86%`，总分支覆盖 `74.7% → 76.9%`。
446 passed（+10）。三处变异验证：把 `send_digest` 改回"数成功条数" → 红；
截断不留记号 → 红；被屏蔽改成继续重试 → 红。
**没法做的事**：给真实订阅者发测试消息来证明这条路径（那会吵到他），
所以线上证明只能等 08:00 的 `push_logs` + `logs/telegram.log`。

### v1.29 /设置 面板点了就地变，早报/晚报开关第一次真的能用（2026-09-27）
coverage 剩下的薄处里 `bot/handlers/settings.py 55.2%` 是他第一时间会撞到的那块：按钮的处理函数
本体一行没测过。补了 15 个用例（每个按钮点完之后既查库里那一行，也查他眼睛看到的那行字和 toast），
顺带挖出六个真问题：

1. **点一次按钮就多一份面板，而旧面板上挂着一排写着假状态的开关。** `cb_settings` 走的是
   `cmd_settings(callback.message)`，也就是 `message.answer()` 再发一条；同一个项目里
   `news.py:56`、`free.py:64` 的回调都是 `edit_text()`。按钮文字带状态（⏸ 暂停推送 / 🚨 突发 开），
   所以点六次之后聊天里前五份面板全是假的——再点最早那份上的"暂停"就把刚恢复的又暂停了。
   现在 `_refresh()` 就地改写同一条消息，`TelegramBadRequest` 只记日志（设置已经落库，toast 照发）。
2. **`daily_enabled` / `evening_enabled` 是只读列。** 面板会打印"☀️ 早报：开/关"，但全仓库没有任何
   一处写它（只有 `jobs.py:261` 读），"关"这一支从上线起就没执行过：他不想每天收早报的手段只有
   `/pause`，而那个连晚报和突发一起停。加了 `x:daily_on` / `x:evening_on` 两个按钮（面板上就是
   "🔔 早报提醒 开 / 🔕 早报提醒 关"），写的正是调度器读的那两列。
3. **🔼 提高门槛是一个无声的自毁按钮。** `min_score` 上限 90，而线上实测**近 24 小时 89 条可入库行里
   最高分只有 65.8**——门槛到 70 以上时 `_briefing()` 拿到空列表 → `Digest(empty=True)` → 那天什么都不发，
   旧代码给他的回应只有一句"已更新"。现在按钮直接把这一档还剩几条说出来：
   `门槛 65：近 24 小时 1 条达标` / `⚠️ 门槛 70：近 24 小时 0 条达标，这样会收不到简报`。
   数数用的是新增的 `repo.count_eligible()`，它逐条复制了 `query_articles` 的同一套门槛
   （非归档 / 未过滤 / 已处理 / `published_at` / `final_score`），所以这个数就是简报真正能挑的池子，
   不是另一套口径。
4. **`callback.message` 为 None 时，这一下点写给了 chat 0。** 原来 `chat_id = ... else 0` 后面紧跟
   `user_for(0)`，等于凭空造一个订阅者并把设置存进那个查不到主人的抽屉。现在提前返回
   "这条设置消息已失效，请再用 /settings 打开一次"，一行都不写。
5. **库里时间不在档位里时，点一下会把 07:15 一把拉回 06:00。** `_next()` 原来
   `except ValueError: return slots[0]`（对早报来说是全天最早的一档），现在取"它之后的第一档"
   （07:15→07:30、07:59→08:00），只有真的走到最后一档才回绕。
6. 兴趣词以前是裸拼进 HTML 面板的（`/setinterest` 的内容正是从这里读出来），一个 `<` 就能让
   Telegram 拒掉整块面板，他连"改回默认"的按钮都点不到 → 现在过 `fmt.esc()`。

覆盖率 `bot/handlers/settings.py 55.2% → 90%`（140 语句剩 14 未覆盖），461 passed（+15）。
七处变异验证：面板改回 `answer()`、去掉 `daily_on` 分支、`_next` 退回 `slots[0]`、不转义兴趣词、
门槛提示不数数、去掉 `message is None` 守卫、面板改写失败直接抛出 → 全部变红。
**过程里我自己错了一次**：`_next("07:15")` 我第一版断言写成 `08:00`，实际下一档是 `07:30`——
代码对、断言错，已改成两条（07:15→07:30、07:59→08:00）把它钉住。

线上：两台部署（`stamp=20260926T221534Z`，`service=active schema=ok`，Traceback 数与部署前一致
vps `db=4/scheduler=4`）。在 anr-vps 上用**真库 + 已部署代码**跑了一遍完整按钮路径（假 Telegram
对象，没发任何消息）：面板按规则模式如实写着"标题里有大事件 + 一手来源 + 24 小时内，或全站热度
≥250"；连点 16 次 → `edits=16、new_msgs=0`；门槛 toast 实测 `45→85 条、50→59、60→7、65→1、70→0`
（70 起带 ⚠️）。探针自己那个订阅者行删掉之后，他真实那一行 8 个设置列逐列比对
`real_rows_unchanged=True`。

### v1.30 设置页上最后一块英文：把 `Asia/Shanghai` 说成"上海 UTC+8"（2026-09-27）
v1.29 之后回头读了一遍真实面板，"☀️ 早报：开 · 08:00（**Asia/Shanghai**）"是那一页上唯一还留给他的
英文标识符——而这句恰好是设置页上唯一说明"按哪个钟"的地方。加了 `format.timezone_label()`：
城市中文名走一张 21 项的小表，**偏移当场用 `zoneinfo` 算**（写死 `UTC+8` 在给一年调两次表的区
说假话），认不出城市名的区只报偏移，只有连区都认不出时才原样回显（那时偏移也无从算起）；
空的时区名不写"未知时区"，因为 `_zone("")` 与卡片/调度器都按 UTC 渲染它，标签必须说同一件事。

线上真库实测（已部署代码，探针订阅者行，测完删除）：
`Asia/Shanghai → 上海 UTC+8`、`Asia/Kolkata → 加尔各答 UTC+5:30`（分钟没被抹成整点）、
`America/Los_Angeles → 洛杉矶 UTC-7`（**当下正是夏令时**，写死 -8 就地露馅）、`UTC → 协调世界时 UTC+0`、
空值 → `UTC+0`。探针行删掉后他真实那一行 8 个设置列逐列比对 identical=True。

463 passed（+2）。五处变异验证：去掉中文名映射 / 偏移写死 UTC+8 / 抹掉 :30 / 认不出时编一个偏移 /
面板退回裸拼时区列 → 全部变红。

**这一轮我自己出的一次事故（如实记）**：变异检查脚本的收尾行写成 `T.unlink()`（本意是删 `.bak`），
把 `app/services/format.py` 和 `app/bot/handlers/settings.py` 两个**源文件**删掉了。脚本开头留的
`.py.bak` 立即用于恢复，`git diff HEAD` 确认只剩本轮预期的改动（format.py +33、settings.py 3 行），
全绿 463 之后才部署。教训：一次性脚本不许对源文件调 `unlink`，恢复路径要用显式文件名而不是"顺手清一下"。

### v1.31 投递要有两只眼睛：账本之外，成功发送也得留下一行（2026-09-27）
08:00 的取证每次都得手工拼三样东西（`push_logs`、订阅者设置行、`logs/telegram.log`），而拼到一半
就会发现第三样**根本没有行**：`TelegramSender.send()` 只在失败时打日志，成功投递一条都不留。
于是「Telegram 到底收没收到」这一问，全凭账本那一条腿站着——而 v1.28 才刚证明这条腿会撒谎。

1. 新增 `scripts/delivery_report.py`（只读，不写库不发消息）：按订阅者 × `morning`/`evening`
   并排给出「窗口内几次 / 最后一次的 UTC 与本地时间 / 计划时间 / 是否被 /pause 或 v1.29 的
   简报开关挡住 / 发送层日志里这个 chat 的最后几行」。四种「今天没有 morning 这一行」的原因
   （没到点、他自己关了、按了暂停、真的该发而没有）在输出里是四句不同的话，不用人来推。
2. `send()` 成功时补一行 `delivered N chars to chat_id=… as HTML`，这条审计才有第二条腿；
   失败路径不会留下这行（用例锁住：`forbidden()` 之后不能有 `delivered`）。
3. 时间口径写在输出第一行（**日志=北京时间，账本=naive UTC，差 8 小时**），因为这个读错一次
   就会把「昨晚 20:01 的晚报」当成「今早」。

473 passed（+10：9 个脚本分支用例 + 1 个发送日志用例）。四处变异验证全红：去掉「是否到点」的
比较、不认 `/pause`、不认简报开关、日志行不按 `chat_id=` 过滤。
线上实测（部署后 `stamp=20260926T224927Z`，两台 active、`schema=ok`、Traceback 数与部署前一致）：
`--hours 168 --kind morning` 对真实订阅者报 `窗口内 3 次 · ⏳ 今天还没到点（计划 08:00）·
上一份是本地 09-26 06:22:50`，对那行测试残留报 `❓ 库里从来没有 morning 的账本行`。

**这条脚本上线十分钟内就把自己该干的活干完了**：它报「上一份早报在本地 06:22:50」，比计划的
08:00 早 1 小时 38 分，看着像"早报提前发"。把 `push_logs` 全 history 拉出来对齐时间线就清楚了
（UTC → 北京）：`morning` 一共三行——09-25 21:01、09-25 22:02、09-26 06:22，而他的订阅者行
创建时间正是 09-25 21:00，`evening` 只有一行 09-26 20:01:51。也就是**三行 morning 全是部署验收
阶段手工发出去的**（那两天在打通投递），真·定时的 08:00 早报**一行都没有**：08:00 漏发这件事
由账本本身第二次确认，不是"提前发"造成的错觉。
**更尖的那一剑**：漏发窗口（北京 07:00-08:59）里 `logs/scheduler.log` 有 255 行，谈到简报决定的
**一行都没有**（按 digest/briefing/morning/skip/already/due 过滤，命中 0）。`_digest_due()` 会返回
`(False, 原因)`，但没人的地方它不落地——所以下一次再漏，仍然只能靠事后翻账本发现。修法很小：
窗口开着而决定是"不发"时至少说一句，窗口关闭而当天没发过再说一句；**但不动这条路径**，
因为今天 08:00 正是这轮改动要观察的第一个真实样本（含 v1.31 那条 `delivered …` 成功日志）。
**「成功投递日志行」目前没有线上样本**（不拿测试消息吵他），第一个真实样本就是今天 08:00。

### v1.31 的线上闭环：08:00 早报第一次带着两只眼睛落地（2026-09-27 08:01）
这台机器上第一次有**直接证据**说明早报真的送到了，而不是"库里应该看得见"：

```
2026-09-27 08:01:39,139 INFO [news.telegram] sender.py:74 - delivered 2603 chars to chat_id=… as HTML
2026-09-27 08:01:39,147 INFO [news.scheduler]  jobs.py:281 - morning digest delivered to … in 1 message(s)
push_logs #15 kind=morning created_at=2026-09-27 00:01:39 UTC（北京 08:01:39）
```

三个独立见证人对上同一秒：发送层成功日志（v1.31 才有的那一行，这是它的第一个真实样本）、
调度器的投递行、账本行。`scripts/delivery_report.py` 自己给出的判定也是对的：
`窗口内 1 次 · ✅ 今天已记账：本地 09-27 08:01:39`，同一条命令对那行测试残留仍报
`❓ 库里从来没有 morning 的账本行`。08:02:11 的第二条 `delivered 262 chars` 是
`kind=free_offer`（另一条限免提醒），不是早报的第二页——**这次简报只有 1 页，所以 v1.28 的
"半份简报不记账"仍未拿到线上样本**，目前只有用例证明它。

送达内容的中文也查了：本次被标为已发的 11 行里 **中文标题 11/11、中文摘要 11/11**，
分数 47.9–65.8（他当前门槛 45，而当天可达上限是 65.8，与 v1.29 那条门槛提示同一口径）。

顺带把 09-26 那次漏发与今天做成 A/B：那天北京 08:00–08:11 的 `scheduler.log` 里 **digest 行 0 条**，
今天 08:01 有一条 `delivered`。也就是"发成功了会说话，决定不发时一声不吭"——剩下的洞就是这一条。

**下一件要修的真问题（就在今天这份早报里抓到的）**：当天最高分那条（65.8）的中文标题是
`OpenAI暂停其"最有能力模特"的培训`——`model` 被翻成**模特**（走秀的那个），不是模型。
这类词义错译和 v1.10 的专名、v1.25 的单位是同一层问题，但要按"AI 语境下的常见多义词"来做，
不能一刀切禁止 模特（真讲时尚行业的新闻要用它）。

### v1.32 唯一还静默的那条路：`daily_time` 被写坏时，那份简报永远不会发而无人开口（2026-09-27）
复查 v1.31 留下的结论时发现我上一轮写错了一句（"简报决定不发时没有日志"，见"仍未解决"里的更正），
顺着代码与真日志重查，`_note_miss()` 其实覆盖着 `already sent today` 与 `window closed` 两种，
`paused` 和简报开关是**故意**不说（那是他自己设的）。真正一声不吭的只剩第三种：
`_digest_due()` 遇到不合法的 `HH:MM` 返回 `bad time …`，而 `_note_miss()` 的白名单里没有它 ——
一条被写坏的 `daily_time`（空串、`8am`、迁移留下的怪值）会让那份简报**永远**不发，
调度器每 5 分钟判一次"还没到点"，日志里连一行痕迹都没有。

改法是一行判定 + 一条与同排风格的告警：`loud = … or note.startswith("bad time")`，
输出带上是哪一份、哪个值坏了、去哪改（`/设置` 或 users 行）；沿用原有的**每天一条**去重，
不因为每 5 分钟判一次就刷屏。

474 passed（`_note_miss` 的用例扩到三种通知 + 一条端到端接线用例，走真的 `_digest_due`，
`_sender=None` 所以绝不联网）。两处变异验证：白名单退回旧的两个值 → 两条用例红；
告警里不放坏值 → 两条用例红。

线上（两台重新部署 `anr-vps stamp=20260927T021725Z`、`anr-jump stamp=20260927T021818Z`，
服务 active、`schema=ok`、Traceback 数不变 `db=4/scheduler=4`）：在 anr-vps 上用**真 logger、
真 `logs/scheduler.log`、一个临时订阅者行**连跑三轮判定 → 文件里**只多了一行**，正是预期的
`WARNING … morning digest for <探针chat> can never be sent: '8am' is not a valid HH:MM …`，
探针行删掉后剩下的是他真实那行与那行测试残留。

**顺手记一条运维陷阱**（这次真踩到了）：手工跑的探针脚本没有 systemd 的 `TZ=Asia/Shanghai`，
它写进 `logs/*.log` 的时间戳是 **UTC**，与服务自己写的北京时间和这条日志混在同一个文件里；
读的时候要么给探针也带上 `TZ=Asia/Shanghai`，要么记住"没有 `2026-… 0x:` 前缀格式的那批行"里
时间可能差 8 小时。今天第一次跑这个探针时就因为忘了它而错判了一次"日志没写进去"。

### v1.33 “最有能力模特”：义项错译要按不变量改，而且要先量过再动手（2026-09-27）
今早那份早报里当天最高分(65.8)那条标题是 `OpenAI暂停其“最有能力模特”的培训`
（英文 `OpenAI pauses training of its ‘most capable models’`）。这是免密钥 MT 的**义项**错误，
不是专名错误，v1.10 的占位符和 v1.25 的单位保护都管不到它。

先量再写规则（真库 620 行可见已处理行，扫一组常见多义词的错义）：
`模特` 2 行、`代理人` 6 行、`令牌` 3 行、`协议` 1 行。逐行读过之后**只修两条**：

1. `model(s)` → 模特：两行都是真错，而这份语料里没有走秀的模特（#697 标题、#691 `Jev风格模特排行榜？`）。
2. `agentic` → “代理人工智能”：#206 的摘要，正确说法是自主智能体。
3. **量过之后故意不动**：`token→令牌` 在 LLM 语境是标准说法（#729），`agent→代理人` 分不出
   真人代理与 AI 智能体（只看英文词），`protocol→协议` 本来就该这么译。自动改这些
   会把对的改成错的 —— 这一条写进代码注释，防止下一个人顺手"优化"。

实现是渲染层的一条不变量（`translate.fix_wrong_sense`）：**英文原文确实带那个词**（走
`term_in` 的词边界，`Supermodel` 里那串 model 不算），中文里的错义写法才被换掉；
`模特儿` 排在 `模特` 前面，否则会剩下 `模型儿`。**不改库**：修正发生在
`display_title / display_summary / display_key_points`，那两行存量数据保持原样，
四条出口（简报、卡片、列表、问答）一起变对。

481 passed（+7：边界、复数、指小形式、agentic、两条"故意不动"的回归护栏、显示层接线、别名表检索双向可达）。
四处变异验证全红：英文侧取消词边界 / 取消英文触发条件 / 两条规则顺序写反 / 显示层退回原样。
**这里我自己差点被骗**：第一次跑变异时 `-k` 过滤词漏了 `boundary`，选中 0 条用例，
两条变异看起来"没抓到"——补全文件跑才有结果。这已经是本会话第二次被"过滤器选中数=0"骗到。

线上（两台 `stamp=20260927T034456Z`，active、`schema=ok`、Traceback 数不变 `db=4/scheduler=4`）：
`#697` 库里仍是 `…最有能力模特…`，`display_title` 出 `…最有能力模型…`；`#691` 同理；
`#206` 摘要 `代理人工智能` → `自主智能体`；近 26 小时 95 行重跑显示层，**含"模特"的 0 行**。

**只修一个出口等于没修**：检索读的是**库里的列**（`repo.query_articles(search=)` 命中的是
`title_zh`/`summary_zh` 原文），渲染层改了字，库里那两行还是"模特"，他用正确的"模型"去搜就搜不到
自己刚读过的那条。所以在 `search.aliases` 里加了 `model: [模型, model, 模特]`，两个方向都通。

**这里我又写了一只假绿的测试**（必须记下来）：第一版把测试数据写成 `summary_zh="有关其模型失控…"` —
中文里出现了"模型"本身，于是**删掉别名组测试照样全绿**，它验证的是 LIKE 命中而不是别名表。
改成"中文列里只有模特、整行没有 model 这个词"之后，去掉别名组立刻变红，这条测试才开始真正护东西。
变异验证补到四处：取消词边界 / 取消英文触发 / 规则顺序写反 / 显示层退回原样 / **删掉 model 别名组**。

线上（两台，`stamp` 分别 `20260927T034456Z`、`20260927T035340Z`；active、`schema=ok`、
Traceback 数不变 `db=4/scheduler=4`）：
- `#697` 库里仍是 `…最有能力模特…`，`display_title` 出 `…最有能力模型…`；`#691` 同理；
  `#206` 摘要 `代理人工智能` → `自主智能体`；近 26 小时 95 行重跑显示层，**含"模特"的 0 行**。
- 真库检索：`搜「模型」` 的前几条里就有 #697、#691 这两行**标题只有"模特"**的存量行；
  `搜「模特」` 也走到同一批。481 passed。

### 仍未解决
头条"摘要 vs 标题"是否按来源类型区分未定；**`LLM_*` 仍未配置**（所有摘要都是规则式首句，
这是唯一未动的质量杠杆；专名译错已由占位符挡住，但句子仍有机翻味）；
`GITHUB_TOKEN` 在 304 之后已非必需但 27 个仓库仍贴着实名上限跑；限免"已推送"账本是全局而非每订阅者；
免费翻译日配额几乎每天入夜用尽；突发门槛的事件词是正则，换语种标题（如纯中文来源）需要另配词表；
要点回补（v1.20）每轮只送 40 条字符串：线上部署后复测，近 14 天 349 行带要点的可见行里
81 行已有中文要点、249 行还在排队、19 行两次问不到已被放弃——按每轮 40 条、日额度 400 次算
还要两三天才追平，追平之前那些行的核心内容以"原文"标注显示；
**规则模式下 `why_it_matters` 仍然一行看不到**——但原因已经不是"没 key 就写不出来"：
写它的那条路已经通（v1.23 的 `compose_why_it_matters`，线上拿真库 300 行带 event 的已处理行重算，
33 行能生成中文句子，例如"由 Google DeepMind 自己发布的事件性消息""同一事件另有 1 家来源报道（Google AI）"）。
剩下的问题是**存量**：近 14 天可见的 477 行里 0 行有值，因为它们在这条修复上线之前就走完了 `_apply`，
而 `unprocessed_articles()` 不会再把它们捞出来。回补是一次批量写他库的操作（纯本地计算，不花翻译
和 LLM 额度），一句话就能做，没有替他决定；
含品牌名的句子会先被 MyMemory 拒一次再由 Google 接手，多一次往返；
规则模式栏目仍有约 7% 判错（标题里没有栏目词的那些，见 v1.12），要再往上走只能靠 `LLM_*`。
检索的中文支持是"带权重的 LIKE 集合"，不是分词器：`search.aliases` 只覆盖手写进去的 11 组同义词，
没进表的说法（例如"矿难"之于"挖矿"）仍然搜不到；精度地板（`MIN_MATCH_WEIGHT=2`）换来的是
`最近有哪些模型发布` 只回 1 条——召回与精度这一次站在精度这边，没有问过他。
概念 AND 的上限故意停在 2：三个概念的问句很少有一行全含，要求全中只会把答案清零。
**`articles.processed_at` 已经在了**（v1.23 加列，线上 `pragma table_info` 实测确认），
"这条什么时候被处理的"现在能回答；但**存量不回补**：线上 584 行已处理可见行的 `processed_at` 仍是
NULL，所以只有新行有时间戳，历史归因还是只能靠日志时间戳反推（见 v1.22）。
`database is locked` 本身还没查（`busy_timeout` 已经是 30 秒，说明撞上的是长写事务或
WAL checkpoint；v1.21 已经让它不再拖垮整轮，但根因还在 anr-jump 上）。
覆盖率仍有薄处（v1.28 实测总 76.9%）：
`bot/handlers/news.py 55.8%`、`scheduler/jobs.py 56.1%`、`main.py 20.3%`——
CLI 启动路径与新闻列表这两类"他第一时间会撞到、出事最难复现"的地方；
`bot/handlers/settings.py` 已在 v1.29 补到 90%。
`collectors/youtube.py 19.8%` 与 reddit 采集器对应的是**当前 disabled 的源**，
真要用之前需要先补测试（v1.27 只补了 reddit 的纯函数部分）。`bot/sender.py` 已在 v1.28 补到 86%。
v1.24 只关住了新水：库里**已经存下的 30 条 `meta.stars=0` 仓库行还在**（它们会出现在
`/搜索` 的 14-30 天结果里，只是再也挤不进简报，因为都过了 24 小时窗口）。
把它们统一标 `filtered_out=1` 是一次批量写他库的操作，没有替他做决定 —— 一句话就能做，等他发话。
**上一轮我写下的"简报决定不发时一声不吭"是错的**（09-27 02:13 复查代码 + 线上日志后更正）：
`_note_miss()` 早就存在，而且今天 08:06:37 真就在 `scheduler.log` 里说了
`morning digest for … skipped: today's 08:00 briefing has already been delivered`。
09-26 那次漏发之所以一行都没有，是因为这个报告函数是 09-26 当天(v1.26)才上线的——我拿一个
"修复之前"的窗口去推"永远如此"的结论，把一次具体的静默当成了机制的缺失。真正还静默的只有一种，
见 v1.32。
**v1.28 的"半份简报不记账"还缺线上样本**：09-27 这份早报只有 1 页，路径只由用例证明过。
真库 `users` 里还留着一行 `chat_id=111111111` 的订阅者（0 次推送，创建于 2026-09-26 18:59 UTC）：
这是测试用的假 chat id 写进了生产库，而且它**不在 `ALLOWED_CHAT_IDS` 里**（线上 env 实测），
所以永远不会收到任何东西，只会让"订阅者数"多 1。删它同样是一次写他库的操作，等他发话。
**08:00 那次早报到底送没送**仍然是这一层唯一没拿到的直接证据：v1.28/v1.29 都只到"库里/日志里应该
看得见"，真正的证明要等推送时刻之后去读 `push_logs` 与 `logs/telegram.log`。
