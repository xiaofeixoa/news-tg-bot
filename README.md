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
|  单元测试 | 82 个用例全部通过（与开发机同一结果） |
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
.venv/bin/python -m pytest            # 610 个用例
```

覆盖：RSS/Atom 解析、空源、超时、HTTP 500、XML 损坏、单个坏源不影响整体；
`tests/data/category_eval.yaml` + `tests/test_classifier_eval.py` 是规则分类的评测集：
53 条线上真实标题（带来源名），允许多个可接受答案，门槛 0.88，2026-09-26 实测 92.5%；
`tests/data/dedup_eval.yaml` + `tests/test_dedup_eval.py` 是跨来源去重的评测集：
25 对线上真实标题，要求真重复 100% 合并、不同事 0 误并；
URL 与标题去重（tracking 参数、同标题、改写标题、跨来源、版本号差异）；
评分（权重可配置、可信度分级、热度归一、兴趣加权、突发门槛）；
突发判定（规则模式的事件词/一手来源/时效三重门槛，AI 模式仍按分数，🔥⭐🔹 阶梯按模式给）；
突发补发（冷却/日上限挡下的行留标记、下一轮重问门禁，终局理由立刻清标记，多读者共享行的优先级）；
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
| 单元测试 | 610 个用例：开发机 Windows 全绿，GitHub Actions 在 Python 3.12 与 3.13 双矩阵 `success`（每次推送都跑）；两台服务器跑的是同一份代码树（md5 一致） |
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

### v1.34 启动路径：`--collect` 以前会把常驻服务偷偷起来，SIGTERM 关停有个能跳过 shutdown() 的守卫洞（2026-09-27）
覆盖率排完序，最薄的可跑代码是 `app/main.py 24%` —— 这个文件决定 systemd 怎么起进程、`--self-check`
告诉他什么、以及收到 SIGTERM 之后走不走 `shutdown()`。它坏掉的形态不是报错，而是
"服务看起来是活的"。补 13 个用例（`tests/test_main_paths.py`），挖出两处：

1. **`--collect` / `--types` 不带 `--once` 时被解析完就丢掉**，代码直接落到 `run_forever()`：
   他敲 `python -m app.main --collect` 想采一轮，实际把**整个常驻服务（含 Bot 轮询）拉起来了**，
   而且退出码 0、什么也不说。现在这两个参数隐含一次性运行，并打一行提示说明等价于加了 `--once`。
2. **关停扫描 `if task is not stop_task and task.exception():` 是会炸的**：
   `Task.exception()` 对**已被取消**的任务不是返回 None，而是**抛出 `CancelledError`**，
   而它不是 `Exception` 的子类 —— 一旦有被取消的任务落进 `done`（停轮询与停止信号同一拍完成），
   这行就会把下面的 `shutdown(jobs, scheduler, sender)` 整个跳过（调度器不关、发送会话不关），
   而 `main()` 的 `except Exception` 也接不住它。实测确认过：`t.cancel()` 之后再 `t.exception()`
   → `raises CancelledError | isinstance Exception: False`。守卫改成先 `task.cancelled()` 跳过，
   真失败的轮询任务照旧记 ERROR。

`app/main.py 24% → 72%`，总覆盖 88% → 89%，494 passed（+13）。四处变异全红：
守卫退回原样 / 守卫连真报错一起咽 / `--collect` 不再隐含一次性 / 去掉那行提示。
**测的过程中我自己差点记错一个数**：第一次跑 `--self-check | tail -8` 后拿 `$?` 读退出码，
读的是 `tail` 的 0 而不是程序的 1；去掉管道重测才是 `exit=1`。已按正确的数写在这里。

线上（两台 `stamp=20260927T051706Z`，active、`schema=ok`）：
- `python -m app.main --collect --types hackernews` → 打印提示、跑一轮（`fetched=24 stored=0 dup=24`）、
  **进程自己退出 exit=0**，不再常驻；
- `--self-check` 真实退出码 `1`，并如实列出三项 unset（这次是以 `news` 身份跑、没带 env 文件，
  所以 token/allowlist 报未设置是**这条命令的正确行为**，不代表服务缺配置）；
- 关停路径另起一个 `--no-bot` 进程、22 秒后 `kill -TERM` → `exit=0`、stderr **0 个 Traceback**、
  最后一行是 `shutdown requested`（即 `shutdown()` 走到了）。那个额外进程没有新花配额：它报的是
  "匿名配额已用完…约 6 分钟后恢复"，也就是 v1.5 持久化的耗尽窗口被子进程继承了。

### v1.35 /sources 那排按钮从来不存在，而它背后的回调是坏的（2026-09-27）
`bot/handlers/news.py` 60.9% → 补到 **80%**（`keyboards/inline.py` 79% → **96%**），
过程中挖到的不是"少测了几行"，而是一个**从来没存在过的功能**和一个**能往订阅表里写假人**的缺省值：

1. **`sources_keyboard` 全仓库没有任何调用点。** `/sources` 实际渲染的是一页纯文本状态表
   （🟢/🔴/⚪️ + 上次成功时间 + 错误行），而这份"每行一个来源按钮"的键盘只被
   `keyboards/__init__.py` 导出、从未挂到任何消息上。也就是说它不是"坏了"，是**根本没接上**。
2. **唯一能触发它的那条回调还是炸的**：`cb_back` 里 `await cmd_sources(callback.message, news)`
   少传 `cmd_sources` 的第三个参数 `app_config` → 一旦有键盘真发出 `b:sources` 就是
   `TypeError`；而且这一行原本挂着 `# type: ignore[arg-type]` —— **把类型检查注释掉
   而不是把参数传对**，这就是那类"被忽略的错误提示"。
   结论与 v1.28 的 `send_many` 一样：**删**，不是修。留下"看起来有按钮、点下去炸"的代码
   只会诱使下一个人去接它。`b:topics` 同理（没有键盘发它），未知标签现在改成
   "回退到新闻列表 + 日志一行 `unknown back tag`"，既不静默也不炸。
3. **三处 `chat_id = callback.message.chat.id if callback.message else 0`**（`cb_topic`、
   `cb_page`、`cb_back`）+ `cb_article` 回落 `from_user.id`：前者会让 `user_for(0)`
   **往订阅者表里写一行假订阅者**（正是我在真库看到的那行 `chat_id=111111111` 的成因类别），
   后者把**用户号当聊天号**用（群聊里两者不是一回事）。现在统一走 `_chat_of()`：
   没有可写回的消息就回一句"这条消息已经不可用了，请再用 /news 打开一份"，一行都不写。

500 passed（+6）。四处变异全红：退回 `chat 0` 顶替 / 未知标签不报告 / 把死键盘再挂回来 /
翻页不看 `page`。我自己的一处测试数据错误也要记：第一条用例我拿 `t:Robots` 当"真存在的栏目键"，
结果 `category_label("Robots")` 原样回显被我断言成中文缺陷 —— 查了配置与真库
（库里 8 个栏目全在当前 taxonomy 内，**没有孤儿分类**）才确认是我编了个不存在的键；
测试改用真的 `t:Research`，并把"未知栏目会露出英文键"这条**降级记录**在下面，不当 bug 修。

线上（两台 `stamp=20260927T054312Z`，active、`schema=ok`、Traceback 数不变 `db=4/scheduler=4`），
真库 + 已部署代码 + 假 Telegram 对象（不发消息）：
- `/sources` 首行 `🔌 信息来源`、`挂了按钮吗: False`；
- 点 `b:sources` → 回退到 `🤖 最新 AI 新闻`，日志出现
  `unknown back tag 'sources' from chat …; showing the news list instead`；
- 点 `t:AI Models` → 标题行 `模型发布 分类`，正文里**不再出现英文键**；
- 第 2 页按钮 `['a:753','a:751','a:752','a:749']` 与库里第 11–14 条一致；
- 三个回调在 `message=None` 时全部回提示且**库里 chat 0/42 的行数为 0**；
  探针订阅者删除后剩下的是他真实那行与那行测试残留（`[111111111, 1985298804]`）。
顺带被这轮探针照出来的一条：`translate route mymemory is out of free quota; retrying in 60 min,
untranslated lines stay in English` —— 免密钥翻译今早又额度用尽，未译行按他定的规则留英文。

### v1.36 /免费：选了"近 90 天"再点工具，窗口被悄悄改回 30 天（2026-09-27）
`cb_free`（`/免费` 面板上那排筛选按钮的回调）整块 46-67 之前一行没测。补 5 个用例之后
第一条就撞出真问题：

- **时间窗与工具不能叠加。** 点 `f:t:<工具>` 那一支写的是 `days = 默认(30)`，
  完全不看用户当前选的是哪一档。而面板顶部的 ✅ 是按 `days` 画的 —— 于是
  "选了 90 天 → 点 Qoder"之后，列表变成 30 天的结果，**✅ 却还标在"近 90 天"上**。
  修法：把这一 chat 当前选的窗口记住（`ContextStore.days` + `remember_days/saved_days`），
  工具按钮只加工具、不改窗口。
- **回调数据是客户端可伪造的输入**：`f:d:99999` 旧代码直接接受（`int(value)` 之后
  一路传给查询）。现在只认面板上真有的 `(7, 30, 90)`，不在其中就用默认值**并写一行 WARNING**；
  没见过的负载同样回落到默认窗口 + 日志，不静默。
- `message is None` 时不再算 tz（旧写法 `user_for(... else 0)` 因为外层三元其实走不到，
  但结构上就是一个"再改一次就会写假订阅者"的陷阱），统一改成先回提示再返回。

`free.py 61% → 74%`、`context.py → 81%`，**505 passed（+5）**。五处变异验证全红：
点工具回到默认窗口 / 不校验三档 / 不记住窗口 / 完整退回旧的 `chat 0` 顶替写法 / 未知负载不报告。
（第一次跑第四处时我改成"只替换 tz 那一行"，可前面的守卫还在 → 那不是等价变异，测试当然抓不到；
把守卫整块删掉重跑才变红 —— 变异检查要改回**旧行为整体**，不是改回某个片段。）

线上复测（anr-vps `stamp=20260927T062939Z`、active、`schema=ok`；假 Telegram 对象 + 真库，
不发消息；探针订阅者行测完删除）：

```
f:d:90       -> '✅ 近 90 天'  saved_days=90
f:t:Qoder    -> '✅ 近 90 天'  saved_days=90   面板提到 Qoder=True
7 天→点工具  -> '✅ 近 7 天'   saved_days=7    ← 旧代码在这里会跳回 30 天
伪造 f:d:99999 -> WARNING free callback asked for days='99999', not one of (7, 30, 90); using 30
未知 f:z:xx    -> WARNING unknown free callback payload 'z:xx' from chat …; using the default window
message=None   -> 「这条消息已经不可用了，请再用 /免费 打开一份」×2
库里 chat 0/42 的行数: 0        他真实那一行逐列比对: 未变
```

两条 WARNING 也确认能落到 `logs/telegram.log`（生产格式，带北京时间的 `2026-09-27 14:31:36`
+ `WARNING [news.telegram]`），这次探针带上了 `TZ=Asia/Shanghai` —— 上一轮就是因为漏了它而误判过
"日志没写进去"。**至今真实流量里没有伪造回调**（`free callback` 的行数就是我探针写的那 2 行），
所以这条告警是"下次有人乱点/客户端被改"时才会长出来。

**基础设施变动（2026-09-27 14:31 检查）**：`192.168.8.99` 这台（anr-jump，原本以 `--no-bot`
只跑采集）今天 13:58 重启过，`/opt/ai-news-radar` 与 `ai-news-radar.service` **都不在了**
（`Unit ai-news-radar.service could not be found`，`/root/.ssh/authorized_keys` 里只有刚重新授权
的这一行 key）。表现是采集端少了一台：`anr-vps` 仍 active、照常采集与投递，但 README §6 里
"两台代码树哈希一致"这条现在只对 anr-vps 成立，`deploy.sh` 不带参数会在 anr-jump 上失败。
要么那台被重装/换机（需要重新供给：venv、`/etc/ai-news-radar/env`、systemd 单元、`data/` 里的库），
要么 IP 现在指向了另一台机器 —— 这件事只有他能确认，我没有去装任何东西。

### v1.37 一条会说谎的告警：已经送到的早报被报成"错过窗口"（2026-09-27）
部署 v1.36 之后两分钟，`scheduler.log` 自己冒出来这么一行：

```
2026-09-27 14:31:56 WARNING jobs.py:331 - morning digest for 1985298804 missed its 08:00
    window - no check ran within 45 minutes of it, so it will not be sent today
```

而这份早报**当天 08:01:39 就送到了**（§v1.31 闭环那节有三条见证：`delivered 2603 chars`、
`jobs.py:281 morning digest delivered`、`push_logs #15`）。也就是说 v1.26 精心加的"漏发要说话"
这条告警，会在**发成功之后的每一次过期检查**上谎报漏发。

根因是判定顺序：`_digest_due()` 先看 `窗口是否已过`，再看 `今天是否已发`。一旦时间越过
`slot + 45 分钟`，账本那一支永远走不到，返回值就固定是 `window closed`，
而 `_note_miss()` 把它打成 WARNING 并附一句"今天不会再发"。
修法是**先查账本再判窗口**：今天已发 → `already sent today`（INFO，且说的是真话）；
今天没发且窗口过了 → 仍然是 `window closed`（WARNING，该响还是要响）。

506 passed（+1 条按今天这个时刻写死的用例：14:31 + 一条 08:01:39 的账本行）。
变异验证：把顺序退回旧写法 → `AssertionError: window closed` —— 正好是线上那句谎报，
这条测试现在是踩着真实事故写的。（顺带记一次我自己的错误：第一次跑这个变异时锚点顺序
写成了 `窗口 + 账本`，与文件里的实际顺序相反，`replace` 静默无效，输出"NOT CAUGHT"，
差点又被我当成"测试没抓到"。这回加了 `assert mutated != src`。）

线上（`stamp=20260927T063903Z`、active、`schema=ok`；只读判定，不发消息）：

```
他真实的订阅者   morning slot=08:00 -> due=False note=already sent today   ← 修前是 window closed
他真实的订阅者   evening slot=20:00 -> due=False note=not yet due
测试残留行       morning slot=08:00 -> due=False note=window closed        ← 真漏发时照样报
```

两行对照说明这刀切对了：**告警没有被弄哑，只是不再说谎**。

### v1.38 一台机器被快照回滚之后：重建 it，并补上"能不能重建"这条测试（2026-09-27）
`192.168.8.99`（anr-jump，采集侧）被误回滚到 **09-25 13:04 的快照** —— 正好在安装之前，
所以 `/opt/ai-news-radar`、`/etc/ai-news-radar/env`、uv、systemd 单元全没了，只有
`news` 用户（uid 9）留着。这一轮把它重建回原状：

- **先量再动手**：那台**连不上 github**（`curl https://github.com` → `000`），
  官方 pypi 15 秒拉不完，而 **TUNA 1.17 秒 200** → 结论是"能重建，但代码只能靠 scp、
  依赖必须走镜像"。磁盘 26 GB 空闲、内存 3.5 GB 可用，够用。
- **env 里不放任何密钥**：这台跑 `--no-bot`，所以新写的 `/etc/ai-news-radar/env`（root:root 0600）
  只有 `DATABASE_URL/DATA_DIR/LOG_DIR/TIMEZONE` 与两个空占位（`TELEGRAM_BOT_TOKEN=`、
  `GITHUB_TOKEN=`）。**没有**把 VPS 上那份拷过来 —— 那里面有他的 chat id 与 token，
  一台只需要采集的机器不该带着它们。
- `deploy/ai-news-radar.service` + `ai-news-radar.service.d/no-bot.conf` 装上，
  `systemctl enable --now` → `active / enabled`，`curl_cffi 0.16.3` 在位（`browser_tls` 的
  中文源需要它），实测一轮：OpenAI 50/50 新、Google AI 20/20 新、DeepMind 46/50 新，
  库内 120 条并开始处理；无 Traceback。
- **供给缺口**：`scripts/deploy.sh` 的打包清单里**没有 `deploy/`**。所以第一次装到
  "依赖装完、`init_db` 跑完"时才 `cp: cannot stat 'deploy/ai-news-radar.service'` ——
  而这台机器无 github，除了这个 tar 别无来源。清单已补 `deploy`，并加了一条测试
  `test_the_deploy_payload_carries_everything_a_fresh_box_needs`（清单 ⊇ 七个必需要素，
  且不得包含 `.venv/data/logs/.env`；两个单元文件必须在位）。变异验证：把 `deploy`
  从清单里删掉 → 该测试红。

同一轮还顺手去掉一个重复了八次的手工步骤：**`README.md` 本来不在部署包里**
（清单只有 `app config scripts tests requirements.txt docs deploy`），所以文档只能一次次
`scp` 上去 —— 一旦忘了，服务器上的 README 就和仓库漂移，而"两台 md5 一致"这条自检
恰好照不到它。现在清单含 `README.md`，测试的必需要素集合也跟着加上；这一轮的三台
`tree=e6caf74e…` / `readme=f4d003e8…` 是**纯靠 `deploy.sh` 一次跑出来的**，没有任何手工 scp。

**507 passed**（用例数没变，是那条断言的覆盖面变宽了 —— 我先把 508 写进去，量了才发现是 507，按量出来的改回来）。**已知这台机器的差异**：`huggingface.co` 从这条线路连不上
（`collector Hugging Face failed: ConnectTimeout`），所以它的库里没有 HF 行 —— 那部分仍由
Bot 机覆盖，不是代码问题。另外它的库是**全新的空库**：09-25 之前的采集历史随快照一起没了，
两边的 SQLite 本来就是各自独立的（不是副本），所以没有任何"他收到的东西变少"的后果。

### v1.39 采集机换到 Clash 网关出口：HF 从"必挂"变成 1.1 秒（2026-09-27）
重建 8.99 之后它的出网是**移动直连**（`myip.ipip.net` 报 2409:8a1e:… 上海移动），表现是
`huggingface.co` 必挂、`github.com` 时通时不通、`api.github.com` 403 —— 我一开始把这三件事
误读成"这台连不上 github"，其实是**被墙 + 被限流混在一起**。他提示可以走 `192.168.8.21`。

先量 `.21` 是什么再动手：它开着 `22/80/443/7890/9090/53` —— 一台 **Clash 网关盒**
（7890 是 mixed 口、9090 是面板）。直接 `-x http://192.168.8.21:7890` 试代理是**拒绝中继**
（4 毫秒 000，不是超时），所以正确用法就是你说的那个：**把它设成默认网关**做透明转发。

用**会自我回滚**的方式先测（只换 `ip route`，不碰配置文件；测完手动换回 + 留了 `systemd-run --on-active`
兜底），结果决定性地好：

| 目标 | 走 `.1`（移动直连） | 走 `.21`（Clash） |
| --- | --- | --- |
| `huggingface.co` | `000`（连不上） | **200 / 1.0s** |
| `api.github.com` | 403（出口 IP 被限流） | **200 / 0.59s** |
| `github.com` | 200 / 卡满 10s | 200 / 1.2s |
| `pypi.org/simple` | 200（慢） | `000`（代理拉不动，但 TUNA 直连可用） |

于是按你的话做成持久：`/etc/network/interfaces` 的 `gateway` 改为 `192.168.8.21`
（**备份在 `/etc/network/interfaces.bak-20260927T151551`**），同时 `ip route replace` 立即生效，
不用重启网络。之后 `scripts/test_sources.py` 在这台**全量扫 20 个源：17 OK / 3 FAIL**，
`Hugging Face 1.1s/50 条`、`The Verge AI 975ms/10 条`（15:18 那条 Verge 超时是抖动，不是系统性问题），
唯一失败的三个全是同一原因：**GitHub 匿名配额 60/小时用完**（约 35 分钟恢复）——
`GITHUB_TOKEN` 现在是这台机器剩下的唯一一条硬瓶颈，它同时也解释了我今天读 CI 读不到的那几次。

**两个我自己制造的尾巴，都清掉了，但记在这儿免得再犯**：
1. `systemd-run --on-active=150s` 建的**是 timer**；我第一次 `systemctl stop` 的是同名 `.service`，
   于是那个"把默认路由换回 `.1`"的定时器还在 `active waiting` —— 留着它就会在某一刻看到网关
   自己变回去。正确的是 `systemctl cancel/stop <name>.timer` + 删 `/run/systemd/transient/*`。
2. 那条兜底命令本身写错了（`ip route replace via 192.168.8.1 …` 少了 `default`，因为我在
   `$( )` 里把 `default` 吃掉了）：**自我回滚的脚本必须先证明它会成功**，否则它只是看起来很安全。

顺带用这个新出口把之前因限流读不到的三个 commit 的 CI 全部读到：**`cae0839`、`bd11ef6`、`e9160ca`
在 pytest 3.12 / 3.13 与"仓库内无密钥"三项检查全 `success`**（今天 08:00 之后一直没确认的那部分补齐了）。

### v1.40 "agent" 不能一刀切：按 AI 搭配改，给"外国代理人"留豁免（2026-09-27）
v1.33 我量过 `agent→代理人` 之后**故意没做**，理由是"只看英文词分不出真人代理与 AI 智能体"。
这句今天被真库证明是对的一半、保守过头是另一半：把那 7 行逐行读英文原文之后——

| 行 | 英文原句 | 库里中文 | 处理 |
| --- | --- | --- | --- |
| #206 #199 #184 | `Agentic AI` / `agentic AI` | 代理人工智能 | 自主智能体（v1.33 的规则已覆盖） |
| #484 | `OpenAI's agent swarms` | 代理人群 | **本轮修**：智能体群 |
| #394 | `AI safety ... agents may treat oversight…` | 代理人可能会… | **本轮修**：智能体可能会… |
| #323 | `Feds Target AI Critics as "Foreign Agents"` | “外国代理人” | **不动**：这是法律术语，是对的 |

所以规则从"见 agent 就改"改成**要有 AI 语境搭配**（`ai agent(s)`、`agentic`、`agent swarm(s)`、
`coding agent(s)`、`llm agent(s)`、`multi-agent`、`ai safety`），并且给人类代理的说法留一张豁免表
（外国/境外/保险/房产/专利/货运代理人、委托代理人 等）；顺序上 `代理人工智能` 必须排在通用的
`代理人` 之前，否则会写成"智能体工智能"。

**这条豁免一开始是"没人看着的"**：我第一版测试拿 #323 当断言，但那行的英文里根本没有 AI 搭配，
规则压根不会触发 —— 把豁免整块删掉测试照样全绿。补了一条**构造**的对照（同一行里既有
`AI agents` 又有"外国代理人"）之后，三种破坏方式（删判断 / 豁免表留空 / 只留一项）全部变红。
这是本会话里第 N 次"我的测试测不到我以为它测的东西"，所以每次新增守卫都要问一句：
**把它删掉，有没有一条测试会红？**

线上（两台 `stamp=20260927T075122Z`、active、`schema=ok`、Traceback 数不变）拿这 7 行真库数据做前后
对照：`#484 代理人群 → 智能体群`、`#394 代理人可能会 → 智能体可能会`、`#206/#199/#184 代理人工智能 →
自主智能体`，而 `#323 “外国代理人”` 原样保留。**库里一行都没改**（修的是渲染层，四条出口一起变对）。
`#236` 这行要说清楚：第一次扫描它在命中列表里，8 分钟后再查它两个中文列都已不含"代理人" ——
中间免密钥翻译轮重试过一次（`mymemory` 退避 60 分钟正好到点），所以它是**自己变好的**，不计入本轮成果；
这个时间差我只是推断，没拿到那一轮前后的值。

509 passed（+2）。**顺带结案一件我上一轮挂起的事**：`cb_page`/`cb_back` 每次翻页都逐 id `by_id`
的 N+1，实测 30 行 **55 ms**，一次 `in_()` 批量是 3 ms —— 不可感知，**不改**，把数字写在这里，
以后谁再看到这条不要凭"看起来该优化"去动它。

### v1.41 那个"防止轮询被带走"的处理器，自己一被触发就抛 TypeError（2026-09-27）
`app/bot/bot.py` 的 `on_error` 从写下那天起就是 `on_error(event, exception)` 两个参数。
aiogram 3.15 派发错误时只给**一个** `ErrorEvent`（`update` + `exception` 在里面），
所以真出异常时的调用栈是：

```
TypeError: on_error() missing 1 required positional argument: 'exception'
```

也就是说：**安全网本身是死的**。它只在"确实有处理器崩了"的时候才被调用，而那一次它会二次抛错，
原来的异常与"保住轮询"两件事一起落空。文档字符串还写着 "A handler crash must never take the
polling loop down with it" —— 一句一直没人验证过的承诺。

顺带把另一件事补上：以前处理器崩了只进 `logs/telegram.log`，他那边是**按钮转圈 60 秒、屏幕上一个字都没有**。
现在 `on_error` 会：回调 → `⚠️ 这一步失败了：<原因>`（先停转圈）；命令 → 一段带回异常类型的中文回话。
上报本身包在自己的 `try/except` 里（被屏蔽、限流、没有 bot 上下文时不能再炸一次）。
写这个处理器时还踩到第二个版本坑：`Update.effective_message` 在 3.15 上不存在，
直接用它等于让错误处理器第二次自己坏掉，所以取目标改成 `callback_query or message or getattr(...)`。

**最重要的一条是方法论**：上面那组用例先按"我以为的签名"直接调 `on_error(...)`，全绿；
把更新喂给**真的 `Dispatcher`**（`dp.errors.register(on_error)` + `feed_update`）才当场露出 TypeError。
现在这个文件里有 `test_the_real_dispatcher_wires_the_error_handler_at_all` 专门走真调用约定，
签名一退回旧写法它和另外三条一起红 —— **测契约要经过契约本身，不能测我对契约的想象**。
（我自己的另一次错误也记在这儿：中途用 `head + 新尾部` 重写文件时把先追加的 5 条白名单边界用例
整段删掉了，是 `grep -c` 归零才发现的，已补回；套件数从 513 回到 517。）

同时补的白名单边界（`middleware.py 63% → 100%`）：陌生人点按钮也会拿到一次 `未授权` 而不再转圈；
拒绝文案里带上他自己的 chat id（fail-closed，空名单拒绝所有人）但**每 5 分钟只说一次**；
提醒记录表加了上限（那只是"最近提醒过谁"，不该跟着扫描流量无限长大）。
第一条版本还写糟过：我直接改 `get_config()` 的缓存单例把白名单清空，
结果全量跑时打挂了 `test_sender.py` 一条依赖白名单的用例（单模块跑看不出来）——
现在换成替身 config，并在 `conftest` 里加了 settings 快照/还原的护栏，这类污染以后不会互相埋。

517 passed（净 +8：4 条错误路径 + 4 条白名单边界，另有 1 条被我自己删掉后补回）。
变异：签名退回旧写法 / `effective_message` 不加保护 / 上报整块关掉 / 陌生人回调不 answer /
通知表无上限 / 日志改回整段正文 → **全部变红**。
线上（两台 `stamp=20260927T084745Z`、active、Traceback 数不变）拿真 `Dispatcher` + 真白名单 +
**捕获用的假 session**（不发任何 Telegram 请求）：崩溃的回调 → `⚠️ 这一步失败了：sqlite 读不到这一行`；
崩溃的命令 → `⚠️ 这条消息处理失败了：ValueError · 服务临时不可用 …`；两次 `feed_update` 都正常返回。
另外这台机器没带 env 文件时探针被白名单挡住（只回 `未授权`），顺手把 fail-closed 也验了一遍。

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
WAL checkpoint；v1.21 已经让它不再拖垮整轮，根因仍未查 —— 而且 v1.36 检查时发现 anr-jump
那台的安装已经不在了，要复查只能等那台恢复或改在 Bot 机上复现）。
覆盖率仍有薄处（v1.36 实测总 89%，语句口径）：`scheduler/jobs.py 65.7%`、
`bot/handlers/free.py` 已在 v1.36 补到 74%；`bot/middleware.py` 已在 v1.41 补到 100%（回调那块已全覆盖，剩下的
是 `_live` 与回落分支），
`bot/handlers/news.py` 已在 v1.35 补到 80%、`app/main.py` 已在 v1.34 补到 72%、
`bot/handlers/settings.py` 已在 v1.29 补到 90%、`bot/sender.py` 已在 v1.28 补到 86%。
`services/llm.py 25.6%`、`collectors/youtube.py 27.9%` 属于**当前模式跑不到的代码**
（没 LLM key、YouTube 源 disabled），按"先量真实产出再决定"排在后面。
`collectors/youtube.py 19.8%` 与 reddit 采集器对应的是**当前 disabled 的源**，
真要用之前需要先补测试（v1.27 只补了 reddit 的纯函数部分）。`bot/sender.py` 已在 v1.28 补到 86%。
v1.24 只关住了新水：库里**已经存下的 30 条 `meta.stars=0` 仓库行还在**（它们会出现在
`/搜索` 的 14-30 天结果里，只是再也挤不进简报，因为都过了 24 小时窗口）。
把它们统一标 `filtered_out=1` 是一次批量写他库的操作，没有替他做决定 —— 一句话就能做，等他发话。
**anr-jump（192.168.8.99）曾在 09-27 被误回滚到 09-25 的快照**（应用与 env 全没），v1.38 已重建回 `--no-bot` 采集角色：`active/enabled`、无 Traceback、一轮入库 120 条。出口已换到 Clash 网关 `192.168.8.21`（v1.39：HF 1.1s 可通，20 个源 17 OK / 3 个 GitHub 配额失败），`interfaces` 备份在同机 `/etc/network/interfaces.bak-20260927T151551`。
**没被接上的功能与"被注释掉的类型检查"是同一类信号**：v1.35 删掉 `sources_keyboard` 之前，
它唯一的回调带着 `# type: ignore[arg-type]`。以后在任何文件里看到 `type: ignore` 遮住的
**参数数量/类型**不匹配，先当作缺陷查，不要当作噪声略过。删掉这条之后**全仓库只剩一处** `type: ignore`（`app/config.py` 的 `ignore[prop-decorator]`，是 pydantic 计算字段的写法问题，不是被遮住的参数不匹配），已经逐条数过。
**未知栏目键会露出英文内部键**（v1.35 查出为潜在、非现状）：`category_label()` 对不在
taxonomy 里的键原样回显，所以一旦 `sources.yaml`/分类表被改名而库里还有旧类，`/topics`
的按钮与空分类提示就会带上英文。今天实测**库里 8 个栏目全在当前 taxonomy 内**，没有孤儿，
所以没动它 —— 改名分类表之后要回来复查这一条。
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
**08:00 那次早报到底送没送**：这一条现在拿到了，三个独立见证（2026-09-27 08:01 北京）：
`sender.py:74 delivered 2603 chars to chat_id=… as HTML`、`jobs.py:281 morning digest delivered …
in 1 message(s)`，以及 `push_logs` #15（00:01:39 UTC = 08:01:39 北京）。内容层面 11 条里
**11/11 标题与摘要都是中文**，分数 47.9–65.8。仍然缺的只有 v1.28 那条多页/半份路径的线上样本。

### v1.42 每六小时一次的体检，46 小时里只跑了 1 次（2026-09-27）
`run_maintenance` 是唯一一个**没有** `next_run_time` 的作业：只有 `IntervalTrigger(hours=6)`，
而 APScheduler 的间隔作业是在注册那一刻排下一次，也就是说它要求**连续 6 小时不重启**。
这台机器的实际节奏不是这样：

```
logs/scheduler.log 覆盖 2026-09-25 20:55 → 09-27 18:45（46 小时）
"scheduler configured" 出现 135 次       ← 调度器启动了 135 遍
维护作业的输出出现 1 次                    ← 2026-09-26 19:39:48，唯一一次跑满 6 小时
"processing backlog" 0 次，"MB free" 0 次
```

而那唯一一次运行说了什么：

```
19:39:48 WARNING source Linux.do 最新话题 has failed 5 times in a row (HTTP 403)
19:39:48 WARNING source Linux.do 福利分类 has failed 29 times in a row (要求降速)
19:39:48 WARNING source Reddit ChatGPTCoding has failed 5 times in a row (HTTP 429)
19:39:48 WARNING source VentureBeat AI has failed 114 times in a row (HTTP 429)
```

**我第一版把这四行写成了"四个源现在还挂着"，那是错的**，是部署后自己被数据打脸的：新的启动体检
19:08:06 打印的是 `0 failing source(s)`。查了才知道四个源在 `config/sources.yaml` 里**都已经是
`enabled: false`**（VentureBeat 那段注释记着原因：数据中心 IP 拿到的 429 连 Retry-After 都没有，
六小时失败 69 次），而 v1.26 的 `sync_sources` 在关掉源时顺手清了 `error_count`/`last_error`——
所以 114 这个数字今天既不存在也不该存在。**这个更正本身就是本条的论点**：一份一天只出不来一次的报告，
内容已经过期了也没人知道；它说过的话（无论是真故障还是当时的旧状态）都没有第二个读者。

真正一直有效、且此刻仍然成立的两个告警目标是：
- **磁盘**（`alerts.min_free_mb`）：写"满了之后采集会静默失败"的那句，触发条件是 `free <= floor`，
  而作业本身跑不到——线上 46 小时里 `"MB free"` 出现 **0 次**。当前 1975MB 对 1024MB 告警线，
  离出事只差 900MB，而唯一的哨兵一天响不了一次。
- **积压**（`processing backlog`）：突发新闻的门禁在发布 24 小时后过期，队列排到 6 小时以上就该报警。
  46 小时里 `"processing backlog"` 同样 **0 次**；这次部署后 27 秒它就报了 `2 row(s), oldest waiting 0.0h`
  ——不是新故障，是终于有人开口。

三处改动：
1. 维护作业加 `MAINT_STARTUP_DELAY = 20` 秒的启动延迟——每活一次就至少体检一次，6 小时节拍照旧。
2. `run_maintenance` 现在**排队拿 `_write_lock`**：它会 `archive_old()` 并提交事务，此前是唯一一个
   在锁外写库的作业。启动即体检如果不排队，换来的就是 `database is locked`——那条恰恰是它自己要报的故障。
   （20 秒落在 6 个采集作业 5–23 秒的起跑之后，所以它会先在锁上排队；实测 27 秒后打印。）
3. 补一行无条件的心跳：`health check: N article(s) in db, N unprocessed, N failing source(s), NMB free`。
   原来这个函数**每条日志都挂在 if 上**，全好时就一个字不说——于是"跑了但没事"和"根本没跑"在日志里
   长得一模一样。我这次是靠"数日志行数"才发现的，这个发现方式本身就该被消掉。

顺带修掉同一文件里另一句谎话：简报生成为空时，日志是
`evening digest for … skipped: due` —— 它把 `_digest_due` 刚刚给出的**判定结果**当成了**失败原因**
打印（`note` 在那个分支里恒等于 `"due"`）。空简报现在走 `_note_miss(…, "nothing to send")`，
内容是 `found nothing to send at 20:00: no stored article cleared the score floor in the briefing
window`，并且和另外三种一样按天去重（观察器每 5 分钟一轮，不去重会刷 9 条）。

4 个反向验证（逐一撤销后必须有用例红）：去掉 `next_run_time`、去掉写锁、删掉心跳行、把空简报改回
`log.info(… note)` —— 全部 CAUGHT。`app/scheduler/jobs.py` 覆盖率 66%→67%（307 句，新增的是维护
与空简报两条路径），全量 **523 通过**（517+6）。

线上验证（两台最终 `stamp=20260927T113619Z`，`service=active`、`schema=ok`）。anr-vps 本轮连续
两次启动，正好是一次修复的前后对照——**19:26:22 那次还是时区未修的构建**（`stamp=…112608Z`），
维护作业在 **19:26:45** 开口，距启动 23 秒：

```
19:26:22 INFO jobs.py:527 - scheduler configured with 7 job(s)
19:26:37 INFO jobs.py:124 - collect[rss] done: ...      ← 采集先跑
19:26:45 INFO jobs.py:394 - health check: 813 article(s) in db, 2 unprocessed,
                              0 failing source(s), 1975MB free (告警线 1024MB)
19:28:22 INFO jobs.py:352 - morning digest for …        ← 启动 +120s
```

同一台机器修复之前（19:13 那次启动）维护作业同样在 +26 秒开口——**它在生产上从来不是坏的**，
原因见下面第 5 条。

---

**本条真正的收获是第 4、5 两段，而且它们是我推上去把 CI 跑红之后才掉出来的。**

第一次推送（`d9486ca`）CI **两个 Python 版本全红**，红的正是我新加的两条用例：

```
AssertionError: 维护任务 -28780 秒后才跑；启动即体检才有效
AssertionError: 采集作业启动延迟 -28795s，本应几秒内就开始
```

-28780 秒 ≈ **-8 小时**，恰好是上海的偏移。

**4. `create_scheduler` 用的是系统时钟，作业排的是配置时区。**
`next_run_time=datetime.now() + timedelta(seconds=…)` 里那个 `datetime.now()` 是**无时区**的
系统本地时间，而 `AsyncIOScheduler(timezone="Asia/Shanghai")` 会把它按上海去解释。
只要进程的系统时区不是上海（CI 的 ubuntu runner 是 UTC，测试跑在部署机上也是 UTC），
七个作业的首次执行就全被排到 **8 小时前的过去**。
本地为什么看不出来：开发机的系统时区就是上海，两边重合。

**5. 生产上没错，是运气：单元文件里那一行 `Environment=TZ=Asia/Shanghai`。**
`deploy/ai-news-radar.service:32` 给服务进程设了 TZ，而宿主本身是 `Etc/UTC`
（`timedatectl show -p Timezone` = `Etc/UTC`，`date` = UTC；日志时间戳是北京时间就是这个原因）。
所以服务进程里的 naive `datetime.now()` 恰好等于上海墙上时钟，120/150 秒的台阶一直是对的——
**靠一个没人测的环境变量撑着**。修好之后：`now = datetime.now(tz)`，任何进程、任何系统时区
得到的台阶都一样；修复后实测启动 19:29:00 → 体检 19:29:20（+20s）、简报观察者 19:31:00（+120s）、
AI 处理 19:31:34（+154s）。

**6. 顺带：部署机上有一个陈旧的 `scripts/__pycache__/test_sources.cpython-313-pytest-8.3.3.pyc`。**
`scripts/test_sources.py`（查源是否活的诊断脚本）和 `tests/test_sources.py` **同名**，一旦哪个
跑法把两个目录一起收集，pytest 会先记住 `test_sources` 来自 `scripts/`，再收集 tests/ 那个真测试时
报 "import file mismatch"，**整个套件在收集阶段就中断**。这个不是猜测，在开发机上做了 A/B：

| pyproject 里 `norecursedirs` | `pytest .` 的结果 |
| --- | --- |
| 没有 | 收集到 `scripts/test_sources.py` 1 个 → `ERROR tests/test_sources.py` → `Interrupted: 1 error during collection`，0 个用例执行 |
| 有 | 收集到 `scripts/` 文件 **0 个**，套件正常 |

处理：清掉部署机上的陈旧 `scripts/__pycache__`，在 `pyproject.toml` 里加
`norecursedirs = ["scripts", "deploy", "docs", …]`；同时 `deploy.sh` 的载荷加上 `pyproject.toml`，
这样"在 Linux 上跑一遍套件"才真的等价于 CI——**本轮之前我一直没做这一步，CI 才能红**：
以后凡是改了调度/时钟/路径的用例，先在 anr-jump 上 `pytest` 全绿再推（hermetic：conftest 自带
临时 DATA_DIR，碰不到生产库；实测 523 passed / exit=0，Linux + Python 3.13）。

奇偶校验：`tree=88ced5a4efe4f091a57bc5ccf4aa2cee`（三台一致）。配方是
`find app config scripts tests deploy requirements.txt pyproject.toml ! -path "*__pycache__*" | LC_ALL=C sort | xargs md5sum | awk "{print \$1}" | md5sum`，
**只取哈希列，且不含 README.md**：开发机的 md5sum 是 MSYS 版，输出路径前带 `*`（二进制模式），
拿整行做摘要会让 100 个文件全部"不一致"；而把 README 算进去又会让文档一改、校验值就自指失效。

同一份日志在这之前的 46 小时里，这类行只有 2026-09-26 19:39 那一条。anr-jump（只采集、不起 bot）
同样在启动后打印了体检行。维护作业等 `_write_lock` 时没有产生 `database is locked`（部署后
`grep -c "database is locked" logs/db.log` 与本轮日志均为 0 新增）。

### v1.43 晚报 8/8 是同一个源的星数行：上限写得明明白白，输出却没遵守（2026-09-27）
20:03 那份晚报推送成功后（`sender.py:74 delivered 1636 chars` + `jobs.py:287 evening digest
delivered … in 1 message(s)` + 账本 `本地 09-27 20:03:39`），我把它实际包含的 8 条拉出来看：

```
78.0  GitHub Trending  kodustech/kodus-ai 收获 1,422 星
78.0  GitHub Trending  MIgHTy-alIeN/ai-trader-bot 收获 2,699 星
… 8 条全部如此，源分布 = {'GitHub Trending': 8}
```

而 `config/settings.yaml` 里明明白白写着 `digest.max_per_source: 3`。**配置承诺过的东西，
交付的那条消息里没有兑现**——这就是这一条要修的，跟中文无关（8 条全是中文，语言层没坏）。

为什么会这样：`_briefing()` 只捞 `top_items * 4 = 32` 条候选，而当天 24 小时池子里

```
分数恰好 78.0 的行数: 41   ← 全部来自 GitHub Trending（星数驱动，规则分打平在天花板）
全池第二高:          76.0   （The Verge 65.8 / Reddit 70.6 / HN 63.9 都在下面）
```

32 条候选于是**全是那一个源**。`select_briefing()` 的限额把前 3 条留下、其余 29 条丢进
`overflow`，可它接着看到 `len(chosen) < top_items`，于是"薄了就放宽"的兜底把溢出项按源轮询
再请回来——补足的 5 条还是同一个源。限额不是没生效，是**它选择的池子里根本没有别人**：
`max_per_source` 要能兑现，候选窗口必须比洪水宽。

改法只有一行判断：`depth = max(top_items * BRIEFING_DEPTH, digest.candidate_limit)`，
新键 `candidate_limit: 200`（当天整个池子 109 行，读 200 行的代价在 `_views` 那一层，
一次简报最多两次，实测不影响投递）。兜底逻辑一个字没动——它针对的"某天真的只有 3 条好稿"
仍然成立，只是现在它拿到的是**看完全部池子之后**的"薄"。

线上同一个池子、同一个 chat 的对照（`generate` 干跑，不发送）：

| | 修复前（20:03 实发） | 修复后（20:11 干跑） |
| --- | --- | --- |
| 晚报源数 | 1（GitHub Trending） | **3** |
| 单源最大占比 | 8/8 | **3/8**，正好等于 `max_per_source` |
| 早报（10 条档） | — | 4 个源，3/10 |

用例 `test_one_tied_source_cannot_own_the_whole_briefing` 把 45 条并列 78.0 + 5 条别家稿塞进
临时库，**旧代码跑出来就是线上那句 `['GitHub Trending'] * 8`**（反向验证 CAUGHT，改回即绿）。
全量 524 通过（523+1）：开发机 Windows 与 anr-jump（Linux/3.13，推送前先跑，见 v1.42 第 6 条）都是 exit=0。两台 `stamp=20260927T121039Z`、`service=active`，`tree=cdb064ae6a8ae940d1f0b83dfd93519b` 三台一致。

**顺带记下一个新的义项错译线索**（还没动）：干跑里 `OpenAI 担心黑客新闻中可能出现的"光学"内容`
—— 英文原文实测是 `OpenAI Feared "Optics" of what might appear on Hacker News`（optics = 观感/形象），
免费 MT 给了物理光学；**全库 `title_zh LIKE '%光学%'` 只有这一行**，所以它现在只是一条记账，不值得为它单独发版。这和 v1.33 的 `model→模特` 同类，
下一轮先量出现率再按 `SENSE_FIXES` 的规矩加（必须有英文触发词才动手）。

### v1.44 76,847 星的仓库和 330 星的仓库同分：热度分量被原始计数灌满
v1.43 修完"晚报 8/8 同一个源"之后，池子里那批并列 78.0 的行仍然在——那才是根因。量出来是这样：

```
带 community_heat 的已处理行: 667   | 其中 >100 的: 76 行   | 最大: 390,594.0
分布: <=100 有 591 | 101-500 有 37 | 501-1000 有 8 | >1000 有 31
超 100 的源: GitHub Trending 46、Hacker News 29、Hacker News Free 1
钉在档位上限(78/100/68)的行: 53
```

`scorer` 的文档写着 `community_heat*.10`、分量量程 0..100，而 `community_heat()` 就是专门
把"GitHub 星数 / HN 点数 / Reddit 赞"压进 0..100 的（`HEAT_WEIGHTS`、log 缩放，注释原话
"Squash wildly different signals"）。但 `compute_scores` 取的是
`article.get("community_heat") or community_heat(article)` —— **采集器一交数字，那个专门的
压缩函数就永远轮不到**。而三个采集器交的都是原始计数：`github.py:444` 交 `stars`、
`hackernews.py:72` 交 `points`、`reddit.py:96` 交 `score`。10% 权重下 heat=1,000 一项就贡献
约 110 分，于是任何 HN/GitHub 行必然撞穿档位上限、原样被 `min(cap, ...)` 拍成 78.0。

**第二层是我自己的用例抓出来的**：把原始计数挡住之后，`test_a_bigger_repo_is_not_tied_with_a_smaller_one`
还是红的——`[49.1, 51.5, 51.5, 51.5]`。原因是那个压缩用的上限是 `community_heat_max_signal: 500`，
500 是"Hacker News 点数"的量级（配置注释自己就这么写），套到星数上：1,422 星就已经 99.9，
7,415 星和 76,847 星同样顶格 100。**修好一层，另一层继续在并列**。所以改成逐字段上限
（`scoring.community_heat_ceilings`，星数 2 万饱和、点数/赞/评论 2 千，共享值退回作默认）。

只读地把 820 条已入库行的分量原样喂回新打分器重算（不改库）：

| | 修复前 | 修复后 |
| --- | --- | --- |
| 钉在档位上限的行 | 53 | **2** |
| heat 分量 >100 的行 | 76（库里存的就是原始计数） | **0**，最大正好 100.0 |
| 全库前 20 名里的 GitHub Trending | 13 | 5 |
| 前 20 名里的厂商博客 | **0 条** | AWS ML 4、NVIDIA Developer 3、GitHub Releases 2 |

部署后的构建在真库上复核（同样只读）：六条库里都是 78.0 的 trending 行，重算得到
`43.9 / 45.9 / 49.7 / 52.7 / 58.9 / 60.0`，heat `52.4→100.0`（390,594 星那条现在正好封顶）。
排序不再等于"谁的星数大"，而是回到文档写的那个加权和——`tier_cap` 的注释要的就是这件事
（"A random trending repo must not outrank an official announcement"，修复前 13/20 都是随机仓库）。

**三点要说在前面**：
1. 这只改**以后新打分**的行。库里已有的 `final_score` 不动（改它是一次批量写他的库，等他发话），
   所以 24 小时窗口要自然滚一晚上，**明天 20:00 的晚报**才是这份修复第一次完整生效的样本。
2. 上面那张表里我**没有**引用"最大同分簇 145 → 60"这个数：那 145 条是 `score_detail` 缺失的行，
   我的重算给它们补了默认输入，属于我探针的产物而不是代码的行为。同理，`低于门槛 45 的行数`
   也不作数。
3. 如果他觉得"仓库趋势本来就该多上简报"，那要动的是档位上限/权重（`sources_quality.tier_caps`、
   `scoring.weights`），不是这条 bug——这条 bug 的症状是"星数差 54 倍判定为同一条"。

验证：新增 7 条用例（5 条星数阶梯 + 并列必须破开 + 无信号字段时只准夹紧不准采信），
两处反向验证（恢复原始计数直用 / 恢复共用 500 上限）全部 CAUGHT；本地 531 passed（524+7）、anr-jump Linux 531 passed `exit=0`（推送前先跑，见 v1.42 第 6 条）；
两台 `stamp=20260927T124938Z`、`service=active`，奇偶校验 `tree=4bb314f47c4726c9abbc654b9b1a90cc` 三台一致。

### v1.45 🔥 的真实含义是"这条来自 GitHub 趋势榜"：分线是照着 bug 的产物定的
v1.44 把打分修好之后，回头一看简报的四层标记整个错位。`breaking.emoji_bars()` 的注释写着
它的设计依据是"measured top score: 78"，配置注释也说"规则模式实测最高 78"——**而 78 正是
v1.44 那个 bug 的产物**（46 条钉死在档位上限、全部来自 GitHub Trending）。照着它定的
`hot_score: 72` 因此只有两种命中方式：要么真的是趋势仓库，要么根本没有。

线上 24 小时窗口 110 行按 v1.44 之后的构建重算（只读，不改库），分位数是：

```
max 78.0 · p90 61.8 · p75 57.6 · p60 54.4 · p50 52.7 · p40 51.1 · p30 50.8 · p20 47.9 · min 45.9
一手/媒体稿能到的最高分：65.8（The Verge）—— 低于 72 这条线，永远进不了 🔥
```

| 分线 | 🔥 | ⭐ | 🔹 | ▫️ |
| --- | --- | --- | --- | --- |
| 旧 72/62/52 | 2% | 6% | 43% | **47%** |
| 新 62/54/49（p90/p60/p25） | 9% | 33% | 34% | 22% |

旧线下**近一半入选池子的稿子被渲染成 ▫️"不重要"**，而 ⭐ 只有 6%。改完之后部署构建实测
（`score_emoji`，同一台机器）：`65.8 → 🔥`（旧：⭐）、`61.8 → ⭐`（旧：🔹）、`49.7 → 🔹`（旧：▫️）。

顺带修掉一个我自己刚写的假测试：`test_the_code_fallbacks_match_the_configured_bars` 第一版拿
**带着配置的** config 去比 `emoji_bars()`，而那三个数永远是从配置读出来的，兜底值改成 72 也测不出
差别——反向验证直接 NOT CAUGHT。改成用一个真正没有这些键的 `AppConfig(raw={})` 走兜底那条路，
并在测试里断言"这条路确实返回 None"，否则检查本身是空的。三处反向（🔥 退回 72 / 兜底漂移 /
🔹 抬到 60）现在全部 CAUGHT。

**存量行仍带着 78.0 的旧分**，所以今天的 🔥 还是会偏向趋势仓库；和 v1.44 一样，等 24 小时窗口
自然换完（明天 20:00 的晚报是第一个干净样本）。用例 +2（其中 1 条是把上面那条假测试修成真的），
全量 533 通过：Windows 与 anr-jump Linux 都是 `exit=0`（推送前跑的）。两台
`stamp=20260927T132656Z`、`service=active`，`tree=b5989b48754d5d578b9e5813f1b581fe` 三台一致。

**下一轮的起点已经量好了：突发新闻连续 7 天一条没出。** 48 小时窗口 239 行按 `breaking.gate`
逐条走一遍：`社区源被挡 163 / 标题无事件词 70 / 超时效 6 / 通过 0`；拉到 7 天（525 行）是
`296 / 209 / 17 / 分数不够 3 / 通过 0`。而 `breaking.rule.min_community_heat: 250` 那条"社区
渠道也能破例"的门**不是没人排队**：7 天里 heat≥250 的有 68 行，可它们全卡在事件词或
`exclude_sources` 之前——比如 `OpenAI Feared "Optics" of what might appear on Hacker News`
（heat 372）、`Gemini 3.8 text-to-speech`（heat 330）、`DeepSeek Elastic Compute (DSec)`
（heat 277）三条都是"某个具名产品发布了"，判的却是 `no event in the headline`。
配置注释写着"实测近 7 天有 4 行"靠热度破例进突发，**今天同一把尺子量出来是 0 行**：
要么事件词表与真实标题用词脱节了，要么 `event_trigger` 的匹配方式（整词/大小写/短语）
和这批标题对不上。v1.44 与这条无关，已单独排除：同一批行按新旧两套分数过门禁，
判定翻转 0 条。

> **这一段里"超时效 6 / 17"这两个数是坏的**（v1.46 复查时发现）：那次把 `at` 缺省成了
> "现在"，于是七天前的稿子全被判成超时。按每行**自己**的处理时刻重跑，结论变成
> "严格只有 0/130 合格、按入库时刻 8/539 合格"，而且真正的凶手是同轮冷却，见 v1.46。

### v1.46 同轮第二条突发被自己刚发的那条冷却掉了（8 条里只发出去 1 条）
顺着上一段"突发 7 天 0 条"的记账量下去，先把自己写错的结论纠正掉：那条**"按每行自己被处理的时刻
4 行合格"**用的是 `processed_at or updated_at or created_at`，而 `updated_at` 会被翻译/补写不断往前推
（`run_maintenance` 里关于积压的注释明明就写着 `updated_at` 会被后面的翻译/补写一路往前推，我自己写的这句话我这次却没照做）。全库实测：`is_processed=True` 的 844 行里
**只有 130 行带 `processed_at`**，714 行为空（且空的那批最晚到 09-27 14:26 发的稿，不是历史遗留）。
按**严格** `processed_at` 判定：130 行里 0 行合格；按 `created_at`（入库时刻）判定：7 天里 8 行合格
（Anthropic 付 Akamai 116 亿、法院裁定、OpenAI 智能体黑进 Hugging Face 的细节、Nscale $3.36B……）。
也就是说"突发到底有没有触发过"这个问题，**被缺时间戳这件事本身挡住了**——这一条记在下一轮。

真正抓到并且修掉的缺陷在这里：`send_breaking` 是
`for article_id: for chat_id: can_send_breaking(...)`，而 `can_send_breaking` 的冷却是
**对照上一条已发出的 `push_logs` 行**算 `elapsed < cooldown_minutes(60)`。于是同一轮里第一条大新闻
发出去之后，同一批的第二条立刻被判 `cooldown 59 min left`——**冷却把自己这一轮的后半截吃掉了**。
用例 `test_two_breaking_stories_in_one_round_are_both_sent`（临时库，抓取式 sender，不碰生产库、
不发他任何消息）先红：`一轮里两条突发只发出去 1 条：[1]`。

修法：`can_send_breaking(..., respect_cooldown=True)`，同一轮里已经给这个 chat 发过一条之后传
`False`；**每天上限（max_per_day=5）与"同一事件已发过"两道闸照旧生效**，跨轮的冷却一行没动
（`test_breaking_guard_respects_cooldown_and_daily_cap` 在两次反向验证里都保持绿色）。
两处反向验证：把参数写死 True、把 digest 里的 `and respect_cooldown` 去掉 —— 全部 CAUGHT。

部署后的线上重放（VPS，`tempfile` 独立 DATA_DIR，抓取式 sender）：把 09-26 那一轮真实合格的标题
喂进去，**送出 2 条 breaking**（修复前同一段代码只会送出 1 条）。第一次重放我把自己缩写过的标题
也喂了进去，第三条被门禁以"没有事件词"拒绝——那是我构造的标题不像原文，改用库里的真标题后消失。

影响要说清楚：这一改动让**一轮最多能连发 `max_per_day` 条**突发提醒（他手机上可能一次弹 2-3 条），
这是修好之后的正确行为；如果嫌吵，该调的是 `breaking.max_per_day`（现在 5），不是把不同事件互相
冷却掉。全量 534 通过（533+1），Windows 与 anr-jump Linux 双绿 `exit=0`，两台 `stamp=20260927T160841Z`、`service=active`，`tree=589a433cb4a51babfa1455274ae2457a` 三台一致。

### v1.47 `is_processed=True` 却不写结案时刻：两条路径在库里留洞
v1.46 那句"714/844 行没有 `processed_at`"查到底，是**两条路径只翻标志不写时刻**：

1. **去重孪生行**（`_merge_into_event`，第二家媒体报道同一事件时生成的那一行）直接以
   `is_processed=True` 落库，从不写 `processed_at`，也不动 `process_attempts`——线上特征就是
   `attempts=0 + 有 final_score + processed_at IS NULL`，而且同一个标题会成对出现（实测
   `Imbalanced VRAM usage between two GPUs…` 两条、分数都是 51.1）。
2. **三次失败后放弃的行**：失败处理器写 `is_processed = attempts >= 3`，同样不写时刻；这一行
   从此再不会被任何队列看到，NULL 就是**永久**的洞。

第三条相反方向的谎也顺手补了：`enrich.requeue_stubs` 把行退回队列（`is_processed=False`、
`attempts=0`）却留着 `processed_at`，于是"待处理的行"声称自己已经结案。现在退回时清空，
`processed_at IS NOT NULL` 才真正等价于"这行已经落定"。

`_settle_filtered`（规则过滤）和 `_apply`（正常处理）本来就写时刻，四个出口现在齐了。

线上验证（部署 `stamp=20260927T231653Z` 之后，只读）：
**部署之后新入库且已结案的行里，缺 `processed_at` 的 0 条**；另有 2 行仍为 NULL，`created_at`
分别是 09-26 11:40 与 09-27 22:19，属于修复前入库、之后被翻译改写才把 `updated_at` 推过部署点的
历史遗留。全库历史遗留共 721 行。

**这 721 行我没有回填**，也不建议在没搞清楚之前回填：`created_at` 是入库时刻、`updated_at` 会被
翻译/补写往前推（这正是 `run_maintenance` 的积压告警只能用 `created_at` 的原因），拿任何一个去填
"处理完成时刻"都是编数据。要填的话得明确口径（比如统一按入库时刻并在 meta 里标
`processed_at_backfilled`），那是一次批量写库，等他定。

3 条新用例（孪生行 / 放弃行 / 退回清空），三处反向验证全部 CAUGHT——其中第一次跑反向时我把
删掉赋值后的 `if` 块留空，结果 `rc=4` 的 IndentationError 被误判成"用例抓住了"；重跑时先
`compile()` 确认语法仍然成立，才拿到真正的 `rc=1 FAILED`。
全量 537 通过（534+3），Windows 与 anr-jump Linux 均 `exit=0`；两台 `service=active`、`stamp=20260927T231653Z`，`tree=9a14a5cd607ba43708235797d5651539` 三台一致。

### v1.48 早报里有一行是英文：model 的第三个义项，加上 MT 根本翻不了的仓库名
先记 08:04 那份早报（`delivered 2319 chars` + `morning digest delivered … in 1 message(s)` +
08:09 观察者正确地说"今天这份已送达"）：10 条来自 **4 个源**，最大单源 3 条，标记
🔥6 / ⭐4 —— v1.43/1.44/1.45 三件第一次同时生效，混合度和分线都对了。但其中**一行是纯英文**：

```
#883  Reddit LocalLLaMA  LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF
```

查下去是两件不同的事：

**1. MT 对光杆 `owner/repo` 一个字都不返回。** 直接问部署中的翻译器：
`translate_many(["LuffyTheFox/Swift-…"])` → `{}`（**不是回显**，是空）。而
`needs_translation()` 判定它"值得翻"（拉丁字母多、CJK 少），于是这条每次排队、每次白花时间，
最后 `display_title` 回落成英文整行。仓库名本来就翻不动——它需要的是**中文框架**，不是翻译。
`localize_title()` 里已经有 GitHub 发布/趋势两个模板，补第三个：`项目：<owner>/<repo>`；
`needs_translation` 因为"模板能给出中文"自动改判 False，**免费额度也不再为它花**。
存量那行由正常翻译轮自己补上，我没有写库：部署后实测
`title_zh = 项目：LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF, translated_by=template`。
正则要求两侧都以字母开头，`12/34`、`2024/01 revenue report…` 不会被误接（用例钉住）。

**2. `model` 的第三个义项错译：车型 / 机型。** v1.33 只挡了 `模特`。全库量：`车型` 2 行、
`机型` 2 行，**四行的英文里都确实有 model/models**（`Which Local Models are the least
'Claude' sounding`→"哪些本地车型"，就是今早那条；`tiny models`→"微型机型"）。加进
`SENSE_FIXES` 同一条英文触发词门槛。`型号` **故意不加**：库里那行英文是 `Model: Phoenix 2
from Aleph Alpha?`，中文"型号："在这个语境里是对的，见了就改反而是我把对的改成错的。
`模特` 那两条仍然要留着：库里从 v1.33 的 2 行涨到 3 行，说明免费 MT 还在生产它。

部署后实测（只读，走 `fix_wrong_sense` 即渲染层用的同一个函数）：四行全部渲染成"模型"，
`仍错=[]`。

用例 +4（正例、**英文门槛负例**、slug 模板、slug 不吃散文/数字）。四处反向验证全部 CAUGHT，
其中第一版我把"去掉英文门槛"错做成"把 keys 清空"——`term_in("")` 永远不匹配，那次替换在
行为上等价，`rc=0` 暴露了它；改成把 `if any(term_in(...))` 换成 `if True` 才拿到真的
`FAILED：这款新模型的风阻更低`。又一次"反向验证自己也得被验证"。

全量 541 通过（537+4），Windows 与 anr-jump Linux 均 `exit=0`；两台 `service=active`、`stamp=20260928T002219Z`，`tree=1cc49e299d1d89d1e3ebf5487a5f09e6` 三台一致。

### v1.49 特工、光学、"黑客新闻"：三个错义，和一次"声明的命令有没有处理器"的自查
先把两件**没有**问题的查掉（都留了证据，免得下轮再猜）：
- **16 个声明的命令全都有处理器**：`COMMANDS`（Telegram 菜单里那些）与 `app/bot/handlers/*.py`
  里注册的命令逐个对过，两边集合一致（`start` 走 `CommandStart()`）。没有"菜单里有、点了没反应"的项。
- **孪生行并不比原件少内容**：27 个孪生行里 22 个缺"要点/why_it_matters"，但把它们各自的原件拉出来
  看，原件同样是 0 要点、无 why——规则模式（没有 LLM key）本来就不产这两个字段（全库 153/784 行有
  中文要点）。所以不是孪生路径漏拷，别去"修"一个不存在的缺陷。

真正修的是 `fix_wrong_sense` 的三条新规则，全部按英文触发词收窄，逐条从真库量出来：

| 错写 | 行数 | 英文触发 | 应写作 | 证据 |
| --- | --- | --- | --- | --- |
| `特工` | 8 | agent/agents | 智能体 | 8 行逐条查过原文：全是 OpenAI/前沿实验室的 AI agent（`There are no "rogue" AI agents`→"没有"流氓"人工智能特工"），**没有一条是真特工报道** |
| `光学` | 1 | optics | 观感 | `OpenAI Feared "Optics" of what might appear on Hacker News` |
| `黑客新闻` | 1 | hacker news | Hacker News | 同一行：站点名被译成了中文 |

`特工` 这条我上一轮（v1.33 时）是**故意没加**的，理由是"只看英文有 agent 分不出人还是智能体"；
这次把 8 行原文逐条读完，确定这个语料里没有间谍报道，才加进来——并且把原来只管 `代理人` 的
"指人搭配让开"收窄条件**推广到 `特工`**（`外国特工`/`双重特工`/`特工组织`…），所以哪天真来一条
"foreign agents" 的起诉报道，`外国特工` 不会被改成"外国智能体"。这条负向用例专门钉住它。
`型号`（`Model: Phoenix 2 from Aleph Alpha?`→"型号："）仍然不动：那个语境里它是对的。

部署后实测（渲染层，走 `display_title`/`display_summary`）：库里仍含这三个词的 **9 行，渲染后
仍错的 0 行**；`#792` 现在是 `OpenAI 担心Hacker News中可能出现的"观感"内容`（一条里两个修复同时
生效），`#852` `OpenAI智能体试图"蛮力"联合国网站`，`#853` `没有"流氓"人工智能智能体`。

3 条新用例 + 3 处反向验证全部 CAUGHT（每次替换先 `compile()` 过语法）。全量 544 通过（541+3），
Windows 与 anr-jump Linux 均 `exit=0`，两台 `service=active`、`stamp=20260928T030222Z`，`tree=42328d8af6efac67aabb650643cae67e` 三台一致。今晚 20:00 的晚报会是
第一批"打分、分线、突发同轮、中文渲染"四项修复同时生效的样本。

### v1.50 突发新闻"一个人收过就全体闭嘴"：去重问错了对象
`articles.is_breaking` 是**共享行上的一个标记**，而 `send_breaking` 是逐读者发的。第一位读者
收到之后 `record_delivery` 把它置为 True，于是第二位读者走到同一个函数时拿到的是
`already sent as breaking`——**同一条大新闻，第二个人永远收不到**。紧接着的"同一事件去重"
更是拿这个全局标记去查别的文章，跨读者互相屏蔽。这和 v1.26 修过的"账本是全局的所以第二个
订阅者永远收不到简报"是同一族缺陷，只是换了张表。

问对了地方就行：`push_logs` 本来就按读者存，还带着 `article_id`/`event_id`。新增
`repo.breaking_already_sent(user=…, article_id=…, event_id=…)`，两道去重都改读账本；
`is_breaking` 继续写（作为"这条曾被当突发发过"的审计位，渲染层没人读它）。

部署构建上的实测（VPS，`tempfile` 独立 DATA_DIR，两个假 chat，不写生产库、不发消息）：

```
读者 A 第一次问: (True, 'ok')
标记位 is_breaking: True            ← 全局标记确实被置上了，正是它以前会挡住 B
读者 A 再问同一篇: (False, 'already sent to this reader')
读者 B 第一次问同一篇: (True, 'ok')  ← 修复前这里是 (False, 'already sent as breaking')
```

2 条新用例（第二读者收取、同事件按读者去重）；反向验证把两道检查还原成全局标记后
**两条全红**，报的正是线上那句"第二个订阅者被别人的收取记录挡住了：already sent as breaking"。
写用例时自己也踩了一次：第二条用例的标题先用了 "launches"，而事件词表里没有这个词，
于是门禁在"没有事件词"就返回、根本走不到要去重的地方——换成 "announces" 才是真的在测那件事。

线上今天只有一个白名单 chat，所以这是**尚未在他手机上产生差别**的修复；但它决定的是"以后加
第二个订阅者时突发是不是只发一个人"。全量 546 通过（544+2），Windows 与 anr-jump Linux 均 `exit=0`；两台
`service=active`、`stamp=20260928T083044Z`，`tree=26e636da4aa98bef0d8bfbcb451eb0db` 三台一致。

### v1.51 翻不动的那几行，每 10 分钟把免费额度重新吃一遍
08:04 那份早报里有 23/200 条摘要是英文、342 条要点里 225 条没中文——一开始像是"额度不够"，
量下去是**队列自己把自己饿死**。`untranslated_articles` 的条件是
`title_zh IS NULL OR summary_zh IS NULL`，而 `translate_pending` 里**没有任何"问过但没用"的出口**：
一条 MT 永远翻不出来的行会永远留在队列里，每 10 分钟 × 2 轮地被重新发一遍。线上抓到的真实队列
里就有这种行：`Estuve analizando el último inform…`（西班牙语标题）、
`Naive-N0.5-Flash - 309B-A15.5B`（模型名当标题）。我直接问部署中的翻译器要这些串：
`translate_many(["LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF"])` 回的是 `{}`——**不是回显，是空**；
而按长度、按批量（1/3/6/10/20 条）分别测，成功率都是 100%，所以不是大小或长度问题，是这些行本身翻不动。
它们占着的额度，正是让别的能翻的行留在英文的那部分。

改法：一条**只在"真的问出去了、 provider 也确实答了别的东西"时**才累计的拒绝计数
（`meta.zh_misses`，累计 `translate.give_up_after` = 3 次就把原文结案、退出队列，
`translated_by="source"`；卡片本来就会把原文标成"以下为原文"，不改语义）。两个必须区分的场景：
- **额度耗尽/线路退避不算拒绝**：`provider_calls` 只在拿到可用应答（非 429、非
  `MYMEMORY WARNING`、非 HTTP≥400）时才 +1，所以"我们没问出去"永远不会被当成"这行被拒了"。
  一次退避就结案会把整个队列永久冻成英文——那是最糟的一天做的事。
- **已经结案的标题不再重问**：`titles`/`summaries` 只收还缺那个字段的行。成功翻过的行有
  `Translator.cache` 挡着，但**模板结案**（GitHub 趋势/发布那一类）和原文结案的行不在缓存里，
  没有这道过滤就每轮再花一次额度。

两处我自己制造又自己拆掉的问题，都写在这儿：
1. 我先写了 `reachable()`（额度 + 退避）作为第二个闸门，结果反向验证 M2 `NOT CAUGHT`——两个
   闸门重叠，只有其中一个在起作用，等于一条没人验证过的保险。现在**只留一个**机制
   （`provider_calls`），`reachable()` 删掉，两条退避用例（429 与本轮额度为 0）都能抓到它。
2. 我为了"确认锚点唯一"跑过一次 `git checkout app/processing/pipeline.py`，那是**未提交**的
   v1.51 改动，被整文件回滚了一次。已重做并用 `cp` 备份代替。规则记住：撤销只对自己刚做的、
   已知的改动做，且永远先 `git status`。

线上验证（两台 `stamp=20260928T091829Z`、`service=active`）：部署后一轮 10 分钟里
**待翻译队列 10 行 → 4 行**，其中 748/841/863 三行已经带上 `zh_misses={'title': 2}`——
下一轮就到 3 次、退出队列。之前同一台机器的 `translated 7/34` 那类分母（每轮 34 行重问）
现在是 `7/11`。

4 条新用例（拒绝结案、429 不算、额度 0 不算、模板结案不重问；546 → 550），
3 处反向验证 CAUGHT（M3 那次锚点缩进写错、`rc=4` 直接暴露，修正后 CAUGHT）。
全量 550 通过（546+4），Windows 与 anr-jump Linux 均 `exit=0`；`tree=ad7908fcb4e6bb8bb7ac3f7c0c273e6d` 三台一致。

### v1.52 简报里 8 条全是 🔥：绝对分数线在这一层根本站不住
09-28 20:00 的晚报本身是健康的——`sender.py:74 delivered 1774 chars`、8 条来自 6 个源、
8/8 中文、待翻译队列只剩 1 行。但把它自己的分数排出来看：

```
78.0 78.0 78.0 73.9 73.5 68.9 65.6 65.6      ← 这一页 8 条的 final_score
标记分布（修复前）: {'🔥': 8}
```

**8 条全🔥**。`hot_score` 是 62，而简报按构造就是池子的最上面那一层——v1.45 那次我是拿
**全库**分位数把这四道线定到 62/54/49 的，可"全库的 90 分位"和"这一页的最低分"不是一回事，
于是 🔥 的门槛低到整个页面都跨在上面。同一个标记人人都有，就等于没有；更糟的是它和上面三行
的"🔥 今日重点"标题顶着干——标题说这是重点，标记说这也是重点。

改成**在这一页之内排名**：`score_emoji(score, config, *, cohort=None)` 把本页分数排序，
前 1/3 给 🔥、中间 1/3 给 ⭐、其余给 🔹。两道设计上的约束：
- **`dot_score` 保持绝对**：低于简报地板的行，无论排第几都是 ▫️。不然一条软页照样会把自己的
  第一名加冕成 🔥——那正是"用相对档位掩盖绝对质量"。有专门一条用例锁住它。
- **一两条的页面不排名**，回落绝对线；`len(cohort) >= 3` 才启用。同分共享最好的那一档
  （`ordered.index(score)`），所以今晚那三个并列 78.0 会一起是 🔥，这是对的。

`section_blocks` 传的是**整份简报**一个 cohort，不是每个分区各自一个——否则每个分区都会重新
加冕自己的第一名，跨分区的档位就又不互通了。`news_list`（/stats 那一类紧凑列表）传自己那页。

反向验证在这里踩了个坑，值得记下：我第一版把 M1/M2 两处锚点写成同一个字符串
（`marker = f" {score_emoji(...)}"` 在两个函数里长得一样），结果替换命中了两处、
测试却报 `NOT CAUGHT`。**不是缺测试，是锚点写得不对**——改成按站点分别定位、并对每个锚点断言
`count == 1` 之后，两处各自独立 CAUGHT。写错的锚点比没测更危险，它会给出"测过了"的假证据。

线上用部署构建回放今晚**实际发出的那 8 行**（VPS，只读生产库、不发消息）：

```
标记分布: {'🔥': 2, '⭐': 2, '🔹': 4}
section_blocks 里出现的标记: {'🔥', '⭐', '🔹'}
GitHub Trending · 09-27 12:06 · 🔥78
Linux.do 福利分类 · 09-28 10:06 · ⭐74
NVIDIA Developer · 09-28 08:56 · 🔹66
```

要诚实说清代价：标记从此是**相对**的，同一篇文章在不同日子/不同页面上可能显示不同档位——这是
这一层想要的语义（"这页里它排哪儿"），但分数本身仍是绝对值、就印在标记旁边。今晚 20:00 那条已经
发出去了，改不动；差别从明早 08:00 早报开始生效。

6 条新用例（分层、同分不同页、地板、1–2 行回落、`section_blocks` 与 `news_list` 各一条真渲染），
全量 **556 通过**（550+6），Windows 与 anr-jump Linux 均 `exit=0`；两台 `stamp=20260928T135928Z`、
`service=active`，`tree=e81c40bf9260d8c00d109bcd2fd96720` 三台一致。

### v1.53 被冷却挡下的那条突发，永远不会有人再来问它
22:12 日志里那一行看着像正常节流：

```
2026-09-28 22:12:32,733 INFO jobs.py:245 - breaking #1101 skipped for 1985298804: cooldown 29 min left
```

冷却 60 分钟、每 10 分钟一轮处理，听起来"下一轮再发就是了"。查库不是：**3 行过门禁、2 行送达、
1 行永久消失**。`send_breaking` 只拿到本轮 `process_pending` 新处理出来的 id，而 #1101 那时已经
`is_processed=True`——它再也不会进入任何一轮。它 14:00 UTC 发布，时效窗口开到次日 22:00，
门禁这 24 小时里一直会认它，只是没人再去问。

改法：被**时间类**门禁挡下时，在行上留一个 `meta.breaking_defer`；处理轮结束时读这张队列，
把它和本轮新合格行一起交给 `send_breaking`。三条约束：
- **只重试会自己重新打开的门禁**（`cooldown` / `daily cap`）。`already sent to this reader`、
  `not breaking: published 30h ago`、"突发已关闭"每轮都长一样，留着标记只是白花一次查询。
  理由串由 `digest._COOLDOWN`/`_DAILY_CAP` 两个常量同时生成和匹配，不靠字符串前缀碰运气。
- **门禁继续重算**：补发时分数、时效、事件词都以**当下**为准，不是"当初过过就该发"。
- **按读者的优先级**：标记写在共享行上、冷却却按读者算，所以规则是"只要还有一个读者在等，
  就不清标记"。这条差点被我写反（下面第 1 点）。

两个我自己的错，都留在这儿，因为它们各自藏了很久：
1. 我第一版写的是 `keep = deferred - delivered`，**和它自己的文档注释矛盾**（注释说"任何读者还在等
   就重订"）。那一写法下：A 这一轮收到、B 还在冷却 → 标记被 A 的成功清掉 → B 永远收不到——正是
   v1.50 修过的"共享行上的一个标记"换了个位置。是反向验证 M3b 逼出来的：我先把 M4 的锚点改成
   `clear = settled | delivered`，它报了 **NOT CAUGHT**，查下去发现代码里有**两道重复的保险**
   （集合运算 + 循环里 `if article_id in keep: continue`），两道同时存在时任何一道单独改动都看不出来。
   改成"两个互斥集合 + 两个循环"后，M3b 与 M4 各自被独立抓到，也为 M3b 补了一条真用例
   `test_a_row_one_reader_got_stays_booked_for_the_reader_in_cooldown`。
2. 读标记我先用了最顺手的 `Article.meta["key"].isnot(None)`。它在 sqlite 上编译成
   `JSON_QUOTE(JSON_EXTRACT(...)) IS NOT NULL`，而 `json_quote(NULL)` 是文本 `'null'` 不是 NULL——
   实测**每一行都命中**，包括 `meta={}` 和 `meta IS NULL` 的行。真按这个写，每轮都会把分数最高的
   若干**未标记**行当"待补发"重新问一遍门禁，合格就发出去。改用 `func.json_extract` 并加了
   `test_the_retry_queue_reads_only_marked_rows` 钉住它。

顺手修了一处会说谎的文案：`/设置` 里"标题里有大事件 + 一手来源 + **24** 小时内"的 24 是写死的，
把 `breaking.rule.max_age_hours` 调成 6 它照样说 24。现在从配置读（`MAX_AGE_DEFAULT` 单点定义，
门禁、文案、重试窗口共用），并有用例锁住。

同一轮还量了一件挂着没结案的事：分类名会不会漏英文。生产库 8 个分类**全部**渲染成中文
（开源生态 261 / 其他 242 / 模型发布 233 / 论文与方法 113 / 智能体 89 / 算力与推理 84 /
产品与应用 49 / 公司动态 33，`category_label(None)` = 其他）。所以"未知分类键回显英文内部名"
这条**目前只是代码里的兜底路径，不是他手机上正在发生的事**——记为阴性结论，不动它。

线上验证用的是生产库的**副本**、假读者 chat `900000003`、抓取型 sender（不发任何真实消息），
复刻 21:41 那次真实送达后：

```
第一轮（22:12 那一次）: delivered=[] 标记={'tries': 1, 'reason': 'cooldown 60 min left'}
（第 3 轮）           : breaking #1101 已连续 3 轮被推迟，仍未送达：cooldown 60 min left
冷却结束            : delivered=[1101] 标记=None is_sent=True sent_at=14:41:46
队列里剩下的标记数  : 0
该读者突发账本      : [(1083, 13:40), (1101, 14:41)]
```

回放本身我搞砸过两次，也写下来：第一次 `record_push` 顺手把 `event_id` 填了，于是被
`same event already sent to this reader` 挡下——**这条路径的表现其实是对的**（终局理由不留标记）；
第二次推老账本时按 `PushLog.user_id == telegram_chat_id` 过滤，而那里存的是内部 id，一条没改到，
`tries` 一路涨到 2。修好过滤才看到真正的补发。

还没生效的一件事，留给他决定：#1101 是**在修复之前**被丢的，行上没有标记，所以它不会被自动补发。
现在手动补也就是一则迟到 60 分钟的突发——要不要补、还是就这样过去，一句话的事。

7 条新用例（556 → **563**）、7 处反向验证全部 CAUGHT（锚点唯一性逐条断言）；
全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；两台 `stamp=20260928T143856Z`、`service=active`，
`tree=52dbd4f660d074982fe41bb2092feea6` 三台一致；部署窗口之后 `scheduler.log` 新增 0 条 Traceback、
0 条 ERROR，健康行 1106 篇 / 1 未处理 / 0 故障源 / 2056MB 空闲。

### v1.54 核心内容回补被自己做完的高分行堵死：窗口 LIMIT 问错了对象
09-28 那条突发修完，顺手量了一下"读者能点开的卡片有没有中文要点"：

```
00:16  有要点的行 754 | 已有中文要点 163 | 队列里还剩 687 行没翻
       要点回补的扫描窗口（按分数取前 120 行）：120 行里 95 行已完成、0 行还能问
```

`translate.points_scan_rows: 120` 的语义是"每轮看 120 行"，但 SQL 里只筛了
`key_points IS NOT NULL`，"已经翻好的 / 两次都翻不动的"是**取完 LIMIT 之后**才在 Python 里剔掉的。
于是分数最高那 120 行做完之后，窗口就再也吐不出任何可问的行——下面 687 行（从分数 59~61 起步）
**结构上永远进不来**。当时简报第 2 名 #1059（73.9）那两条英文要点就是这个形状。

三处一起改：
- **排除写进 SQL**：`json_extract(meta,'$."key_points_zh"') IS NULL` 且
  `key_points_tried < POINTS_ATTEMPTS`（用 `func.json_extract`，不用 `Article.meta["key"]`——
  v1.53 刚量过那个写法会命中所有行）。
- **失败计数只在"真的被拒"时累加**：只有 provider 这轮确实答过话（`provider_calls` 增加）、
  且**这一行自己的字符串在本次送出的 `points_per_run` 里**，才算一次失败。旧写法把所有
  `got` 里没有中文的行一律 +1，包括根本没被送出去的行、以及线路挂掉的轮次——两轮就把一行的
  核心内容永久静音，正是 v1.51 给标题修过的"把退化当拒绝"。
- **无需翻译的行要结案退出队列**（要点本来就是中文、或没有要点），否则它每轮占一个名额，
  和旧 LIMIT 堵死是同一个效果。

我这次先写出了一个**错的证据**，必须更正：最初那句"三天里 `translated key points` 出现过 0 次"
是 grep `scheduler.log` 得来的，而 pipeline 用的是 `get_logger("app")`，它落在 **`logs/app.log`**。
换对文件后：app.log 里这条从 09-27 起出现过 **51 次**。所以这不是"功能从来没生效"，而是
**吞吐被窗口堵死**——结论仍然成立（00:16 那组数字是直接调 repository 量的，不依赖日志），
但范围比我先写的窄。代码文档与用例注释里那句已经改掉，并把"pipeline 的 `[news]` 日志在 app.log、
判断'某段代码没跑过'之前先 grep 全部 logs/*.log"记进了运维笔记。

另外两个自己造的坑：
1. 第一版同时写了 `if not batch: 整窗结案` 和逐行 `elif not texts: 结案`——两道重复保险。
   反向验证 MD 因此报 **NOT CAUGHT**（拆掉逐行那道，另一道照样兜住）。合成一条路径后 MD 才真被抓到；
   而 `translate_many([])` 本来就返回 `{}`，那条 batch 守卫属于死代码，删掉。
2. 四条新用例最初都从 `translate_pending` 驱动，**测的并不是要点这一层**：共享测试库里其他用例留下的
   英文标题先把翻译器打进退避，要点层提前返回，断言是以错误的理由失败的。改成直接调被测函数
   `_translate_key_points` 之后，四条各自对应一处真实缺陷。

线上部署后（生产库、真实免费额度、00:32 那两轮）：

```
translated key points for 20 article(s), 40 of 40 string(s) asked
settled 14 article(s) whose 核心内容 needs no translation
translated key points for 8 article(s), 16 of 40 string(s) asked
settled 5 article(s) whose 核心内容 needs no translation
756 有要点 | 191 已有中文（原 163）| 队列剩 642（原 687）
```

分数 59~61 那批第一次被翻出来，就是窗口不再被顶替的直接证据。

4 条新用例（563 → **567**），4 处反向验证各被自己那条用例抓到（MA 窗口不排除已完成、
MB 线路挂算拒绝、MC 没进批也记账、MD 不需翻的行不结案）；全量 Windows 与 anr-jump Linux/UTC 均
`exit=0`；两台 `stamp=20260928T163840Z`、`service=active`，`tree=6f0420d413f007983455abc8904772e1`
三台一致（第二次部署只带注释更正，逻辑与首次 16:29 那次相同）。
剩下的账：642 行按每轮 ~28 行的速度在未来几小时内陆续翻完（额度仍排在标题/摘要之后），
26 行是两次真实拒绝后停问的，属于"免费 MT 就是翻不出"，卡片会如实标为原文。

### v1.55 额度保护只盯着没在干活的那条路由：Google 一整晚一次账都没记过
v1.54 修完之后先看了一夜战果——要点回补 **37 轮翻出 556 行**，`有要点 801 / 已有中文 747 /
结案 21 / 队列剩 0`。但同一份日志里有个数字对不上：

```
00:38 → 06:30  标题/摘要按路由：google 23 轮 / 81 条，mymemory 3 轮 / 7 条
               要点回补送翻字符串：1044 条
               `translation budget exhausted` 出现次数：0
```

配置写着 `per_run_limit: 60`（每轮）、`daily_budget: 400`（每天），1044 条字符串跑了一夜，
而"额度用完"这句话一次都没说过。读代码就明白为什么：`Budget` 只在 `_via_mymemory` 里被
`spend()` / `available()` 碰过，**`_via_google` 从头到尾不认识 budget**——而夜里 26 轮里有 23 轮
是 Google 在答题。也就是说额度保护专门盯着那条几乎没干活的路由，真正扛流量的那条**没有上限**。
第二个洞连着：`Budget.reset_run()` 本来就是为"每轮清零"写的，但全工程**没有任何地方调用它**，
于是 `used_run` 只增不减，`per_run_limit` 的实际含义变成"每个进程"——跑满 60 条之后 MyMemory
当天再也不被问，而每次部署重启都像是"修好了"。

改三处：
- `_via_google` 先查 `available()`（用完就一条请求都不发，并落下 `budget exhausted` 日志），
  发请求前 `spend(1)`——**一次批处理请求记一次**，不是一批字符串记一次。
- `run_translation` 每轮开头调用 `get_translator(...).budget.reset_run()`，让 `per_run_limit`
  真等于配置上写的"每轮"；`per_day` 不动。
- 单位写清楚：`Budget` 文档与 `settings.yaml` 注释现在都说**单位是请求次数**（MyMemory 一条
  一个请求、Google 一批一个请求）。旧注释一句里混着"每轮翻多少条"和"每天多少次请求"两种单位，
  正是这个洞能被长期忽略的原因。

代价要说在前面：**400 次/天从这次起真的会生效**。以前它只约束 MyMemory（昨夜只占 3 轮），
等于没约束。爆料密集的一天，翻译会在当天某个时刻停下、日志写
`translation budget exhausted; N item(s) stay in English`，之后的卡片按他给的规矩显示英文原文。
现在稳态需求很小（队列已被 v1.54 清空），但这是明确的取舍，不是"更保守所以更好"。

三个我自己犯的错，都是这次量出来的：
1. 第一条用例的断言是**空断言**：我写 `used_day == saved_day`，而当时 `saved_day` 是 0，
   "连每天一起清"的变异也把它置 0 → 两边仍相等 → M4 报 NOT CAUGHT。改成显式 `used_day = 7`
   再断言它还在，M4 才真的被抓到。
2. 测试替身不忠实：`ChainClient` 拼多行答案时**没带换行**，于是"两行标题一次请求"这种批量在
   测试里永远不可能成功，Google 的记账也就测不到。补上真实端点的形状（每段以 `\n` 结尾）之后，
   顺带发现原来那条"批量错位"用例是靠这个缺陷碰巧通过的——改成显式 `google_parts=["…"]`
   才是真的在测错位。
3. 线上探针我自己写错一行：先 `used_day = 0` 再调 `reset_run()`，然后打印"每天也被清了"——
   那是我自己设的值。重做（`used_run=500, used_day=77`）后确认只清每轮。

线上验证（部署构建，VPS，假 HTTP 客户端、零真实请求、临时 DATA_DIR）：

```
配置的每轮/每天上限: 60 400
两条字符串一次请求 -> used_run=1 used_day=1 | 发出的请求数: 1 | 答案: 2
把每天额度打满后 available(): False → 再问一次：请求数 0、返回 {}
reset_run 之后: used_run=0（每轮清零）used_day=77（每天不动）| 每天还剩 323 次
```

3 条新用例（567 → **570**）；4 处反向验证各自被抓到（M1 不记账、M2 打满还发请求、
M3 每轮不清零、M4 把每天也清了）；全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；
两台 `stamp=20260928T223913Z`、`service=active`，`tree=b63a18a1f0b6071783f1e6f8a1314e78` 三台一致；
部署后 0 条新 Traceback（`db.log`/`scheduler.log` 里那 4 条仍是 09-26 的旧账）。

### v1.56 每天 400 次的额度挡不住重启：一重启就当今天是新的一天
v1.55 把上限做成真的之后，第一件事就是查它会不会被绕过去。查出来的是：

```
今天(UTC 09-29)进程启动：00:30、00:39、06:39 —— 同一个 UTC 日里 3 次
今天真实用量（日志数出来的）：要点 1333 条字符串 / 62 次调用，google 标题 186 条，mymemory 67 条
而 `Budget.used_day` 只活在内存里
```

`daily_budget: 400` 是**按天**的保护，可计数器是进程内的整数——**重启一次就送回 400 次**。
一天里我部署几次，免费端点就被我们从同一个 IP 打几次额度，而保护数字看着一直"没超"。
这和当初 GitHub 配额要落盘（`data/github_rate.json`）是同一个洞，只是换了对象。

改法照同一个模式：`Budget` 增加 `path`，`Translator` 建立时 `load()`，每批翻译结束后
`save()` 把 `{day, used_day}` 写进 `data/translate_budget.json`。三条约束：
- **只在同一天内继承**：文件里的 `day` 不是今天就当 0，昨天用完的 400 次不该压住今天。
- **写盘不能变成故障源**：目录、只读、坏 JSON 都安静返回（`save()` → False、`load()` → 0），
  翻译照跑。额度记录是附属信息，不该让一轮翻译因为它挂掉。
- **原子写**：先 `.tmp` 再 `os.replace`——机器人点开卡片和定时轮可能同时在写这一个文件。

顺带挖出一个测试隔离问题，也是这次改动**自己**造成的：计数器一旦落盘，本模块所有用例
共享同一个 `DATA_DIR`，于是邻居用例花掉的 2 次请求会把"额度用完就不该发请求"那条直接弄红
（第一版就是这么挂的）。修法是在测试辅助 `translator_with()` 里给每个实例换一个独立的
budget 文件并把计数归零——这是测试替身的改动，不是产品的豁免。

线上验证（部署构建，生产 `DATA_DIR`，只读探针另起进程）：

```
data/translate_budget.json = {"day": "2026-09-29", "used_day": 4}   ← 应用自己写的
新进程读到的每天用量: used_day=4 / 上限 400 → available(): True，今天还剩 396 次
13:51 一轮 translated 17/18 via google，计数从 0 涨到 4（请求数，不是条目数）
两台部署后 0 条新 Traceback
```

要说清没做什么：两台机器各写各的文件，这正确——它们的出口 IP 不同（`anr-jump` 走 `.21`
网关，VPS 是自己的 IPv6），配额本来就是按 IP 算的。

6 条新用例（570 → **576**）；5 处反向验证全部 CAUGHT（M1 不写盘、M2 忽略文件里的日期、
M3 读回却不应用、M4 本轮不保存、M5 坏文件抛异常）。全量 Windows 与 anr-jump Linux/UTC 均
`exit=0`；两台 `stamp=20260929T054906Z`、`service=active`，
`tree=849aaeb9d786b6f65899b5f5010d4be4` 三台一致。

### v1.57 词表里没有 "model"：17% 的被丢新闻只靠这一个词
先说结论之前的一次数数。24 小时窗口里读者可见的行**中文覆盖率是 100%**（英文标题 0、
英文摘要 0），所以问题不在翻译，而在更前面一层：**有些新闻根本没入库**。

把 7 天内 `filtered_out=True` 的行拉出来看标题，一眼就不对劲：

```
#1099 TechCrunch AI   Modulate raises $25M for its voice models and analysis suite
#121  Hugging Face    Accelerating vision-language models with LFM2.5-VL-DSpark
#722  Reddit LocalLL  Best open-source coding model for a laptop with 4GB VRAM?
#1066 Reddit LocalLL  AGI definition
7 天内没有任何关键词命中而被丢的行：183
其中只需要 "model/models" 或 "agi" 一个信号就能救回：31（17%）
```

`filters.keywords` 有 48 个词，**里面从来没有 `model`**（也没有 `agi`）——而
`AI Models` 是这套 taxonomy 的一个栏目。也就是说"模型"类新闻能不能入库，全靠标题里
有没有别的词（openai/llm/gpt…）顺手撞上来。

顺手清掉一个我自己的误判：我一开始读 `classifier._matcher` 的正则
`(?<![a-z0-9])kw(?![a-z0-9])`，据此认定"复数永远不命中"（agents / GPUs / prompts）。
真去量才发现门槛用的是另一个函数 `normalize.keyword_hits`（词边界更松，`" model"`
这种前缀匹配能命中 `models`），`_matcher` 只在分类打分里用。**量出来"只差复数"的行是 0**，
所以我没有动匹配器——只是按证据补词。

改动只有数据：`filters.keywords` 加 `model`、`agi`（实测 50 词）。为什么不加
`token`(9)/`generative`(2)/`weights`(2)：命中数太少、歧义更大，还没到"值得加"的证据。
代价也写清楚：泛用的 "model" 动词会放行个别无关标题（评测集里就收了一条
"How to model retirement savings in a simple spreadsheet" 当**已知误放行**样本），
门槛只决定入库，能不能上简报仍由分数（晚报 45 线）、每源上限与排序决定。

新增评测集 `tests/data/gate_eval.yaml`（19 条线上真实标题：12 条该入库、6 条该挡住、
1 条已知误放行）+ `tests/test_gate_eval.py`：
- 逐条断言门槛判定，判错就指名是哪条；
- 第二条用例把 `model/agi` 从词表里临时抽掉，断言 **12/12 全部落回被丢**——
  以后谁删这两个词，测试会直接说出代价。

线上（部署构建、生产库、只读重放 7 天的被丢行）：

```
线上词表 50 个，含 model / 含 agi：True / True
7 天被丢 183 行 → 加词之后会入库 31 行（与离线测量完全一致）
其中标题本身就带 model/agi 的 13 行：voice models 融资、vision-language models、
本地模型选型帖、AGI definition……
```

2 条新用例（576 → **578**），3 处反向验证（M1 词表去掉 model、M2 去掉 agi、
M3 门槛不读词表）全部 CAUGHT；全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；
两台 `stamp=20260929T091234Z`、`service=active`。
已经落库为 `filtered_out` 的历史行不会自动翻身（183 行里那 31 行仍不可见）——要不要
把它们重新过一遍门槛，是一次批量写库，等他点头。

### v1.58 中文不变量：内部键名一次都不该出现在他屏幕上（并把它做成断言）
这次是先审计、后修改。把 `format.py` 的每个渲染面都喂一遍"内容已是中文"的数据，扫输出
里的拉丁词，结果有两类：

- **误报（不改）**：`example.com`、`href`、`/help` 里的命令名、`UTC+8`、品牌与模型 ID
  （`claude-sonnet-5-5`、`v1.9.0`、`anthropic-sdk-python`）。用部署构建把今晚的晚报真渲染
  一遍也确认了这一点：1843 字符里所有"可疑行"都是来源名（GitHub Trending、Reddit
  LocalLLaMA RSS）和发布版本号——那是专有名词，不是我们的 chrome。
- **真问题（改）**：`category_label()` 的最后一行是 `return name`，也就是**词表里没有的
  分类键会把它内部的英文标识直接印进中文简报**；`source_type_label()` 同样会原样回显
  `newsletter` 这类取值。查过线上：当前 8 个分类、6 种来源类型全都有中文标签，所以这条
  **今天是潜在的、还没发生**——但"都是中文"是硬要求，而潜在路径正是配置一改就会踩到的
  那条（这套 taxonomy 历史上就长过好几次）。

改法：两者都不再回显键名，而是落到中文兜底（分类→`其他`，来源类型→`其它来源`），同时
**给运维提醒一次**（`_warn_missing_label`，按 `类型=值` 去重）：这是配置缺口，该在
`config/categories.yaml` / `labels.source_types` 补一条。安静地告诉他"其他"、又让他一辈子
不知道标签丢了，是另一种坏。

顺手量化了两个行为，都写进用例：
- 两个未登记的分类键**合并成同一个「其他」小节**，不会渲染出两个同名小节；
- 重复调用只提醒一次（实测两个缺失键共 2 条日志）。

`tests/test_chinese_surfaces.py`（5 条）把这次人工审计固化：未登记键不出现、8 个在线分类
的中文名逐个钉住、未登记来源类型不原样显示、提醒恰好一次、以及"内容全中文时输出里不该
有成句英文（品牌/URL/HTML 属性除外）"。

4 处反向验证全部 CAUGHT：N1 回显键名、N2 不提醒、N3 来源类型原样回显、N4 每轮重复提醒
（N1 同时被两条用例抓到）。全量 583 通过（578+5）。

写上线上探针之后又发现自己一处小谎并修掉：提醒文案对来源类型说的是"按「其他」显示"，
而代码真正返回的是「其它来源」——日志把兜底文案写错，就是把人往错的文件上引。改成把
兜底值作为参数传进 `_warn_missing_label`（`SOURCE_TYPE_FALLBACK` 单点定义，返回值与提醒
共用），并给用例加了这句断言；第五处反向验证 N5（把提醒里的文案改回「其他」）被它抓到。
最终 5 处全部 CAUGHT。

线上（部署构建 `stamp=20260929T121459Z`，只读探针）：

```
已登记分类 -> 模型发布            未登记分类 -> '其他'
已登记来源 -> GitHub              未登记来源 -> '其它来源'
界面没有 来源类型 'mastodon' 的中文名，按「其它来源」显示；请在 settings.yaml 的 labels.source_types 补一条
渲染里是否还出现内部键: False
```

同时把今晚的简报也量了：20:04:55 发出 1843 字符 / 1 条消息，8 个条目的标记分布
`🔥×3 ⭐×2 🔹×4`（v1.52 的页内排名在真实一页上确实是分层的，并列 65/64 共享最高档），
整行无中文的只剩来源名与版本号那类专有名词。v1.57 新收的 3 行 arXiv 论文进了 `/最新`
前 200、被分到 `论文与方法`（分数 47.9，够不上简报前 8——它们的收益是"可被查到"，
不是"上头版"，这点不说大）。

全量 583 通过（578+5），Windows 与 anr-jump Linux/UTC 均 `exit=0`；两台
`service=active`，`tree=2c53cf8148385d186449a23dc66e70d4` 三台一致；按时间戳归因，
部署后 0 条新 Traceback。

### v1.59 `/summary 第二条` 只回一句用法——而那句话正是机器人自己教他说的
先记两条**否定结论**，因为它们让我没有去改不该改的东西：
- 我原以为序数表停在「八」，实际两处都到「十」——看错了，没动；
- 我也怀疑过 `keyword_hits`（门槛）和 `_matcher`（分类）两套匹配器的分叉会让 v1.57
  新收的行分错栏目。把 3 行拿部署构建重算：`Research`、置信度 0.72-0.83，没错。

真问题在 `/summary`：它自己抄了一份序数解析，只认**裸的**「一..十」，而自然语言那半边
（`search._index_from`）认「第…条」。于是：

```
第二条 / 第三条 / 第十一条  ->  命令：None（回用法提示）    自然语言：2 / 3 / 11
第2条                       ->  命令：新闻编号 2              自然语言：第 2 条
```

`/summary` 的提示语原文是「也可以先 /news，再直接说『第二条详细说说』」——它教的那种说法，
自己在命令里不通。`store.nth` 也说明这不是理论问题：列表会记住 20 条（日报）甚至 30 条，
「第二十条」是一句真话，而旧表连 二十 都不认。

改法：序数解析只留一份。`app/services/search.py` 导出 `ordinal_to_int`（先整串匹配
「第?N条?」，再句内搜「第N条」，数字走 `int`，中文走新的 `_cn_to_int` 覆盖 一..九十九），
`_index_from` 与 `/summary` 的 `_resolve_id` 都改成调它，`_resolve_id` 里那份 digits 表删掉。
顺带修了顺序 bug：带「第/条」壳的输入先按序数读，否则 `第2条` 会被抓成新闻编号 2。

两处我自己制造的测试问题（不写出来就等于没发生）：
1. 变异 P1（把「第/条」分支去掉）起初 **NOT CAUGHT**——因为 `seeded[1]` 恰好等于 2，
   「第2条 被当成编号 2」这条断言永远为真。换成 90001.. 的合成 id 之后 P1 立刻被抓到
   （`assert 2 == 90002`）。这是本仓库第二次踩"小 id 撞上小序号"，已经当成惯例记住。
2. 我一开始给测试写了「第二十条 → 20」的期望，两个解析器其实都不认 二十。我先怀疑是测试
   越界，查了 `store.nth` 与 `/news`、`/日报` 的列表长度（20/30）才确认这是**真缺口**，
   于是把 `_cn_to_int` 写成完整的 一..九十九，而不是把断言削矮。

线上验证（部署构建，读生产库的真实 20 条列表，不发任何消息）：

```
第三条 -> 1394 OK      第3条 -> 1394 OK      第十一条 -> 1377 OK
第十九条 -> 1371 OK    第二十条 -> 1368 OK   第三条详细说说 -> 1394 OK
```

3 个新用例函数（参数化那条 13 个短语）＝新增 16 条，583 → **599**；变异 P1/P2/P3 全部 CAUGHT。
全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；两台 `stamp=20260929T123601Z`、`service=active`，
`tree=429a1a89d628f033e017ce6f6bd0e408` 三台一致；`app.log` 0 条 Traceback。

### v1.60 卡片上的「相关来源」有 21 条在说谎：那正是你正在读的那一家
量了一下事件分组（这块账本一直没查过）：1157 个事件、33 个 `member_count>1`，
其中 **`member_count` 与实际行数不符 0 处、`source_names` 含重复 0 处**——去重和计数都是好的。
但按"来源"再看一次：**33 个多行事件里有 21 个其实只有一个来源**（Linux.do 的转载、同一 feed
的第二条、Reddit 同一板的跟进帖）。而卡片的渲染条件是 `event_members > 1`，
列表取自事件的全部来源名，于是：

```
读的是 TechCrunch AI 的稿子
   相关来源：
    • TechCrunch AI            ← 它说"另有来源"，列的却是你正在看的这一家
```

同一个 bug 的第二处表现：`member_count` 数的是**行**，而 `summarizer` 早就知道这一点——
它算覆盖度时先剔掉了本条自己的来源（`others = [n for n in source_names if n != 本条来源]`），
所以"另有 N 家报道"那句话一直是准的。只有卡片漏了这一步。这不是新发明，是把卡片对齐到
仓库里已有的正确口径。

改两处 + 一处同类兜底：
- `news._view`：`event_sources` = 事件里**除本条来源之外**的去重来源；`event_members` =
  去重后的**来源数**（不再用行数）。同源转载 → 空列表 → 那一行整条消失。
- `format.article_card`：渲染条件改为"有别家才渲染"，分隔符从 `, ` 换成中文顿号「、」。
- `breaking_card` 的代码默认标题原本是 `"🚨 AI BREAKING NEWS"`。线上配置写的是
  「🚨 AI 突发新闻」，所以他**今晚收到的那条 #1191 是中文的**——这条不是已发生的错误，
  而是和 v1.58 同一类：配置少一行时，默认值就会替他说英文。改成中文默认。

4 个新用例（599 → **603**）：同源转载不出该行、两家媒体时只列另一家、默认标题不出英文、
顿号分隔。变异 Q1（不剔本条来源）、Q2（覆盖度按行数算）、Q4（英文默认标题）、
Q5（拉丁分隔符）各自被抓到。另记：Q3（把渲染条件从"有别家"改回"来源数 > 1"）**测不出差异**，
因为在新口径下两者恒等，所以我没有为它写断言，也没有留那道重复保险。

线上（部署构建，生产库真实事件，只读）：

```
同源多行事件 21 | 多来源事件 12
  #91  (Google DeepMind, 2 行) 相关来源=[] 卡片里有该行=False
  #138 (Hugging Face,   2 行) 相关来源=[] 卡片里有该行=False
  #64  本条=Google AI      -> 列出 ['Google DeepMind']  members=2
  #73  本条=Google DeepMind -> 列出 ['Hacker News']      members=2
```

另外记一笔今天的成绩单，免得明天惊讶：额度落盘后 `translate_budget.json` 显示
**今天已用 248/400 次请求**（以前这个数字根本不涨，因为主力路由不记账）；0 条 ERROR、
0 条 Traceback，队列全空，今天推送 breaking 3 / free_offer 2 / 早报 1 / 晚报 1。
全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；两台 `stamp=20260929T151121Z`、`service=active`，
`tree=bfe3111d349969a57d7f26f64c3e8324` 三台一致。

### v1.61 把那六件积压的写库一次做完，顺手抓出卡片同族的第二处"行数当来源数"
他批准了积压的批量写库。动手前先备份：`/root/anr-backup-20260929T152853Z.db`（sqlite backup API，
8.3 MB / 1436 篇）。然后逐项核对——**第一项就把一个同族 bug 挖了出来**：

`summarizer.compose_why_it_matters` 写的是"同一事件另有 `{member_count - 1}` 家来源报道"，
`member_count` 是**行数**，不是家数。线上 #659 就是一个事件 7 行、实际只有 2 家媒体，
它写着「另有 6 家来源报道」，而且 `others` 那份列表还允许同一家出现多次。这与 v1.60 卡片
那次同族，只是藏在"为什么值得关注"那一行里。若直接回填，61 行错数字就永久写进库了。
所以顺序是：先修口径（`outlets` 去重、按"别家媒体数"计），部署，再回填。修完 #659 →
「同一事件另有 1 家来源报道（Linux.do 福利分类）」，全库再无 >3 家的夸大句。

工具落在 `scripts/reconcile_db.py`（**默认干跑**，`--apply` 才写，`--only` 挑步骤），六步：

| 步骤 | 干跑量到 | 写库结果（VPS / 采集机） |
| --- | --- | --- |
| `regate` 词表加词后被丢的行重过门槛 | 7 天内 190 被丢 → 34 可放行 | 34 / 22 行放回，由正常轮重处理 |
| `spam` GitHub Trending `(0 stars)` 新仓库 | 25 行 | 25 / 0 行标 `filtered_out`（星数 0 说明"上榜"信号不成立） |
| `matters` 回填"为什么值得关注" | 缺 1181 → 有内容 61 | 修正口径后 31 行有、30 行不再挂这句 |
| `processed` 补 `processed_at` | 721 行为空 | 用入库时刻补，`meta.processed_at_backfilled` 标明是补的 |
| `ghost` 测试留下的假订阅者 | chat 111111111，0 条账本 | 删除；有账本就跳过不删 |
| `lost` 修复前丢掉、又补不回的突发 | #1101 | 写 `breaking_lost`：`published 26h ago, older than 24h` |

`#1101` 的结论是**补不回来也不该补**：它发布已满 26 小时，时效门禁（24h）现在就不接受，
硬塞一条迟到的突发等于用规则之外的口子。它带着原因留在库里，下一个 Agent 看得见为什么
`is_sent=False`。采集机上同样有两行（#804/#822）做了这个记录。

验证（写完再看，全部只读查询）：34 行放行后 **34 行已重处理、32 行拿到中文标题、0 行被再次过滤**，
其中 #121「使用 LFM2.5-VL-DSpark 加速视觉语言模型」53.3 分、#395「水下 C3-JEPA…」47.1 分——
这些是 v1.57 之前根本进不了库的新闻；假订阅者残留 0；夸大条数 0；
`spam_dropped` 25 行。23:46 那轮 `scanned=37 processed=37 filtered=0 failed=0`、翻译 60 篇。

过程里我自己犯的两个错，都留在文档里：① `spam` 步骤第一版用 `int(meta.get("stars") or -1)`，
把"0 星"读成"没有这个键"，干跑报 0（本仓库 `as_int` 的注释早就写着这个坑）；② 验证脚本用了
`Article.meta["regated"].isnot(None)`——就是 v1.53 记下的 `json_quote(NULL)='null'` 陷阱，
它命中全库 1440 行，差点让我以为"所有文章都被放行过"。

新增 5 条用例（605 → **610**）：`_stars_of` 对 `"0"/0/0.0` 与缺键/坏值的判断、
`--apply` 四种账目一次验（放行/标掉/补时刻/删假订阅者）、以及"默认必须是干跑"。
2 处反向验证 CAUGHT（`{members - 1}` 回去、`outlets` 不去重）。
全量 Windows 与 anr-jump Linux/UTC 均 `exit=0`；两台 `stamp=20260929T154220Z`、`service=active`，
`tree=48b7ff7a5e725747afce7da553a2d2ec` 三台一致。
