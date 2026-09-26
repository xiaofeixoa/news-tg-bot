# AI News Radar 变更日志

> 依据：设计文档 `../index.html`、`docs/DEPLOYMENT_HANDBOOK.md` 的事故记录、代码内注释、
> 生产库与 `logs/*.log` 的实测数据。
> **注意：本仓库没有 git 历史**，版本号是 2026-09-26 整理时补的分段，只在有可核对证据处标日期
> （数据库首批入库 = 2026-09-25，部署戳、日志时间戳、`push_logs`）。

---

## v1.0 初版实现（2026-09-24 前）

按设计文档 §4–§27 一次性搭起来的可运行系统：

- **采集**：`rss` / `hackernews` / `github`（search·releases·org 三模式）/ `reddit` / `arxiv` / `youtube`
  六类 collector，插件式注册（新数据源 = 一个子类 + `@register`，核心流水线不动）。
- **归一与去重**：URL 归一化 + `url_hash` 唯一约束、标题指纹、事件聚合（`event_id`）把多源同一新闻折成一条。
- **打分**：`importance·0.35 + relevance·0.30 + novelty·0.15 + source_quality·0.10 + community_heat·0.10`，
  权重进配置不进代码；无社区指标的官方博客自动 renormalize，不让它白丢 10%。
- **摘要与标签**：LLM（OpenAI 兼容）优先，**规则模式**兜底（关键词分类 + 正文首句摘要），LLM 挂了新闻照样入库。
- **中文输出**：翻译服务 + `title_zh`/`summary_zh` 字段。
- **调度与推送**：APScheduler `AsyncIOScheduler`，采集/处理/简报/维护四组作业；`guarded()` 包住每个作业，
  一个源或一个提供方失败不拖垮整轮；写作业串行化（`_write_lock`）避免 SQLite `database is locked`。
- **Bot**：aiogram 3，`/新闻 /来源 /订阅 /设置 /免费` 等；`ALLOWED_CHAT_IDS` 默认拒绝所有人；
  推送对被封/已删的会话按永久错误处理，不卡住调度。
- **存储与部署**：SQLite（WAL + `busy_timeout`）、systemd unit、`/etc/ai-news-radar/env`(0600) 管密钥、
  `RedactingFormatter` 挡日志泄密。

## v1.1 部署联调（2026-09-25，两台服务器）

- 部署到 `anr-jump`（192.0.2.10，仅采集）与 `anr-vps`（IPv6 VPS，带 Bot），跑通真实 Telegram 投递。
- 写出**图文并茂的实战部署手册** `docs/DEPLOYMENT_HANDBOOK.md`（含事故复盘）。
- `scripts/deploy.sh` 取代手工 `tar|scp|ssh`：打包→分发→重启→打印 `service=… schema=… stamp=…`
  并校验迁移真的生效。踩过的两个坑都写进脚本注释：
  `systemctl is-active` 对 failed 单元返回非零，在 `set -e` 下会让自愈逻辑永不执行（**造成约 90 秒真实停机**）；
  `$HOSTS` 只展开第一个元素，第二台机器会被静默跳过。

## v1.2 中文输出改造（2026-09-25，硬要求）

触发点是他的一句「新闻获取的怎么都是英文啊…我希望都是中文的啊」。

- 免费机器翻译链路改为**多路由降级**：MyMemory → `translate.googleapis.com`，
  不再因为一个 IP 级配额耗尽就整天留英文；配额状态带小时退避。
- **模板标题本地化**（`localize_title`）：`v1.2 released in owner/repo` → `owner/repo 发布 v1.2`、
  `(0 stars)` → `收获 0 星`，零 token、零配额；并把启动时回扫旧行（`repair_template_titles`）做成幂等。
- 显示层永远中文优先（`display_title` / `display_summary` / `display_line`），
  简报发送前 `ensure_chinese()` 对**将要显示的少数几条**当场补齐。
- 实体解码修到"标题里出现 `&#128064;` 乱码"的问题（feed 双重转义，需 unescape 两次再清残体）。
- `clean_text` 保持 URL 安全（不做 NFKC、不动 `&b=2`），解码只作用于文本路径。

## v1.3 `/免费` 限免检索（2026-09-25）

需求：`/免费` 能问"现在哪些 agent / 哪个模型免费"（例：opencode 最近 DeepSeek 免费）。

- **两个互补事实源**：OpenRouter 公共 `/api/v1/models`（免 key、权威定价，`_all_zero` 才算免费）
  + 新闻语料里的限免事件（规则检测器）。两者在输出里**视觉分离**，网关来的模型条目带"未核实"标记。
- 新限免**主动推送**（`free_alerts`）：按订阅用户逐个 `_ensure_user` → 频控 → 排除 `push_logs` 已推过的
  → 记录推送。快照落盘（`data/free_models.json`）以支撑"新上架/已结束"趋势块。
- 检测器质量**做成可度量**：`tests/data/free_eval.yaml` 50 条人工标注（中英双语、来源写清），
  门禁 = 总体与分语言 precision 1.0 / recall ≥ 0.85；网关模型名在运行时并入词典。
- 规则修复若干：`for free` 从强信号里剔除（误报）；版本号后缀允许 `xxx v1.2` 紧邻；
  中文"X站"主语识别；`冷却/日上限` 等配置项走 `as_int/as_float`
  （旧写法 `int(cfg.get("cooldown_minutes", 90) or 90)` 会把配置里合法的 `0` 吃成 90）。
- 配置里的 `exclude: [preview, alpha]` 收窄到只挡元路由 —— 原先它把**新闻标题里点名的那个模型**也挡掉了。

## v1.4 成本与稳定性（2026-09-26 凌晨）

- **Cloudflare 是按 TLS 指纹拦的**：`curl` 200 而 httpx 403 同机同秒。给采集器加
  `browser_tls`（`curl_cffi` + `impersonate=chrome`），linux.do 福利分类从 0 条变 24/25 入库；
  手册里"数据中心 IP 打不开 Cloudflare 源"的旧结论被**证据推翻并改正**。
- **省内存**：aiogram 单独 import 约 106MB（475MB 的机器上很致命）。发送器改成惰性导入 +
  `TYPE_CHECKING` 注解，回归测试 `test_collection_mode_does_not_import_aiogram` 在**子进程**里断言
  `sys.modules` 无 aiogram —— 以后谁再往共享模块顶层加一句 import 就会红。采集专用机实测降约 114MB。
- **HTML 解析降级不再丢消息**：`TelegramSender.send()` 默认 `parse_mode="HTML"`，
  遇到 "can't parse" 才降级为纯文本重发 —— 一条没转义的标题不该毁掉整个早报。
- 修复定时推送漏传 `parse_mode` 导致简报里出现字面 `<b>`；修复 `preview.py free` 在新签名下崩在服务器上
  （并补了一个真的跑该脚本的子进程测试）。

## v1.5 GitHub 配额：从"静默空转"到几乎免费

- **现象**：日志连着几天 `0 new of 0 fetched`，看着像"今天没新闻"。真相是 `GITHUB_TOKEN` 那行是注释掉的
  （`grep -c` 数得到，第一次排查因此误判已配置），27 个仓库 × 每轮 1 次请求把匿名 60 次/小时吃光，
  而 `_releases()` 把 403 当"这仓库没 release"用 `log.debug` 咽掉了。
- **守卫 + 诚实**：`GitHubCollector.get()` 读 `x-ratelimit-remaining/-reset`，配额为 0 时一个请求都不发；
  403 是从异常里抛的（拿不到 header），失败路径去问**不计配额**的 `/rate_limit` 确认，结果缓存 60 秒；
  启动时若不配 token 就按仓库数算给他看要多少次/小时。
- **条件请求（真正的解）**：`data/github_etags.json` 存 etag + 响应体，下轮带 `If-None-Match`；
  GitHub 对 304 **不扣配额**。生产同机连跑三轮：`18 metered + 0 free` → `3 metered + 15 free 304`，
  单轮 26 → 5-7 次，**重启不再等于重烧一轮配额**。每轮花销记进 `logs/collector.log`：
  `github <源>: N metered + M free 304 + K known-empty, quota left=…`。
- 404 也算答案（`missing_until`，默认 6 小时后重问），但**只有真 404** 会被记 —— 超时/403 绝不缓存。
- **`GitHub Trending` 一直在查三个 topic 的交集**：GitHub 限定词是 AND，
  原代码把 `searches:` 拼成 `topic:ai topic:llm topic:agents`。实测交集近两周 `stars>=10` 只有 4 个仓库，
  而 `topic:ai` 单查有 26 个；另一条 `pushed` 查询按 stars 排序 → 每轮同样 25 个全被去重，
  长期 `0 new of 25`。改为**每个条目独立成查询**（`max_search_calls` 控成本）、
  新建仓库加 `min_new_stars: 10` 门槛（此前入库全是 `xxx (0 stars)`）、pushed 查询按 `updated` 轮转。
  上线第一轮：`25 new of 25 fetched`。
- **通用 429 退避**：429 原来走"可重试"分支，一轮连打三次、下轮再来 —— VentureBeat AI 六小时失败 69 次
  且**历史上从未产出过一条**（浏览器 UA / curl / curl_cffi 实测同样 429 → IP 级），
  于是 `BaseCollector` 加主机级退避（读 `Retry-After` 秒数或 HTTP date、`x-ratelimit-remaining: 0` + reset），
  200 但 remaining=0 也提前进入冷却，冷却期内**一个包都不发**。VentureBeat 置 `enabled: false`。

## v1.6 简报：从"按时发"到"发得对"

- **08:00 早报静默丢发**（2026-09-26）：`digest watcher` 是 interval job，**第一次执行在启动 5 分钟后**，
  而部署反复重启使每个进程活不到第一次检查；且"已发过/错过窗口"完全不打日志，看起来和"今天没新闻"一样。
  改为启动后 120 秒即检查（重复发送本来就被"按本地日去重"挡住），错过窗口发 WARNING，
  已发过发 INFO，都按 (用户, 种类, 日期) 去重。裸写的 `45` 提成 `DIGEST_GRACE_MINUTES`。
- **简报排的是"最新"不是"最重要"**：`_briefing()` 取 `latest(...)[:top_items]` 而 `latest()` 写死
  `order_by_score=False`。按今晚真实参数实测：8 个名额里 4 个是 Reddit 帖标题，
  同窗口分数更高的 17 条一条没进。改为**按分数选 + 每源限量**（`digest.max_per_source: 3`，
  纯函数 `select_briefing()`，当天内容不够时回填而不是少发）：

  ```
  OLD newest-first → Reddit 4, GH Releases 2, TechCrunch 1, Ars 1   分数 56-72
  NEW best-first   → AWS ML 3, GH Releases 2, TechCrunch 2, Reddit 1 分数 62-72
  ```

## v1.7 正文提取与摘要质量

- **摘要断在半句**：`head[:150]`、`first[:160]` 这类硬切让生产简报出现过 `…with a wall of `、
  `Topics: ai`、`选择“CLAUD`。统一改为 `normalize.shorten()`：在中文标点或空格处断开、加 `…`、
  切点不早于限制 60%、不会把 HTML 实体切成一半；产生摘要的地方（`summarizer.fallback_summary`）
  与显示层同时改，并对**已入库的旧行**做幂等启动修补（`repair_truncated_summaries`，实测 re-cut 79 + 2）。
- **官方公告结构上进不了简报**：按源统计发现 Google DeepMind（7 天 50 条，最高 54.2）与
  Hugging Face（50 条，最高 50.9）**永远够不到 55 的门槛**，而 3.7KB 的 Reddit 帖轻松过关。
  根因是这些 feed 每条只给 36–288 字符，而规则模式的 importance/relevance 来自关键词命中数，
  `source_quality` 95 分却只占 10% 权重。于是补上设计文档 §6 早写了、此前只用来清洗 feed 描述的
  **正文提取**（`app/processing/enrich.py`：BeautifulSoup 取 `<article>/<main>` 的段落，
  丢掉 nav/aside/script 与 "Subscribe/Related posts/All rights reserved"）：
  每轮预算 + 复用主机退避表 + **只加分不减分**（抓到零命中的文档页也不会把已入库新闻撤回）。
  真页实测 46–71 字符 → 4.7k–12k，分数 41.7 → 78.6 / 48.2 → 75.4；
  存量靠"每轮重新排队最接近门槛的短正文行"消化（**注意：一开始挂在启动时是假补种，一次只消化 5 条**）。
- **两个连带 bug**：① 重新处理会在 `INSERT INTO article_tags` 联合主键上崩（`attach_tags` 不幂等），
  降级行补跑/重排队都会炸；② 翻译队列只按 `title_zh IS NULL` 过滤，
  于是**377 行有英文摘要、永远不再被翻译**（模板标题分支还 `continue` 掉了摘要翻译，
  所以每条 GitHub release 都逃不过）。两处一起修，并加"缺标题的行排在只缺摘要的行前面"
  —— 标题与摘要共用每日免费配额，几百行摘要库存不该饿死新文章的标题。
  实测：每轮从 `translated 1/1` 变成 `60/60 + 60/60`，待译摘要 150 → 7。
- **Reddit 页壳当标题**：链接帖的 RSS 正文只有 `submitted by /u/x [link] [comments]`×3，
  它被编成摘要、**花了翻译配额**、再当成 `/新闻` 的头条；原有 `strip_feed_boilerplate()` 只认识 linux.do 的两种页壳，
  而中文形式永远命中不了（清洗发生在显示时，那时文本已被翻译）。三层修复：
  stripper 认英文/中文页壳并折叠重复段、`fallback_summary` 切句前先清洗、显示层让存量行自动回落真标题。
  线上：`由 /u/pmv143 提交 [链接] [评论]…` → `此时，OpenAI应该国有化。`

## v1.8 让用户看到的数字说真话

- `🔌 数据源：37 个` 数的是配置文件 —— 其中 **17 个是关着的**。改为
  `20 启用 / 37 配置 · 近 24 小时出过新闻 20 个 [· N 个正在报错]`，
  启动日志同步为 `20/37 sources enabled, 20 delivered in 24h`。根因层面：`stats()` 此前**零测试覆盖**。
- **免费翻译额度用完就直接显示英文**（2026-09-26，他的决定）：原先没有中文摘要时整行摘要被隐藏，
  简报"更安静"但没更多信息；现在未翻译的摘要按原样显示，同时**头条仍优先中文标题**
  （未翻译的英文聊天句不该抢标题位：`display_line = 中文摘要 > 标题`，第二行放"没当上标题的那一条"）。
  配额耗尽的 WARNING 只在状态跃迁时打一次，并写明"未译部分保持英文"；退避跳过降到 debug。

---

## 仍未解决 / 下一步

1. **今晚 20:00 的晚报是调度与选择改动后第一次真实投递**，需人工/日志确认（`push_logs` 的 `kind=evening`）。
2. **头条用摘要还是标题**目前折中为"中文摘要优先、否则标题"；是否**按来源类型**区分（论坛/HN 用标题、
   新闻室用摘要）还没决定。
3. **`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` 仍未配置** —— 所有摘要都是规则式正文首句，
   品牌名转写质量（"拥抱脸部模型"这类）上限就在这里；这是唯一未动的质量杠杆。
4. `GITHUB_TOKEN` 在 304 之后已非必需（匿名额度够用），填了更稳。
5. 小项：限免"已推送"账本是全局而非每订阅者；中国大陆侧的中文促销源只能被美国那台访问；
   免费翻译日配额（`translate.daily_budget: 400`）几乎每天入夜用尽，剩几行等次日。
