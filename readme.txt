AI News Radar —— 使用说明（面向使用者）
本文件是 §25 要求的"面向用户的说明"。变更历史在 README.md §11，架构在 框架.txt，
需求与决策在 设计.md，逐轮开发记录在 codex.txt。命令行与配置项都取自当前源码。

==================================================
一、它是什么
==================================================
一个自托管的 AI 新闻机器人：定时采集多个来源 → 去重归并成事件 → 打分 → 生成中文摘要 →
按你自己的时区推送早报/晚报，大新闻即时"突发"推送；还能在 Telegram 里直接问它。
所有输出默认是中文；免费翻译额度用完时，未翻译的那一行会显示原文（这是刻意选择，不是故障）。

==================================================
二、安装（生产机上的实际做法）
==================================================
前提：Debian/Ubuntu 系、root、Python 3.12+、systemd。

1) 取代码
   git clone https://github.com/xiaofeixoa/news-tg-bot.git /opt/ai-news-radar
   cd /opt/ai-news-radar

2) 依赖
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt

3) 密钥：只写在 env 文件里，不要写进 YAML 或代码
   /etc/ai-news-radar/env（0600，root 所有）至少需要：
     BOT_TOKEN=…                 Telegram 机器人令牌
     TELEGRAM_CHAT_ID=…         或者由机器人自己 /start 后成为订阅者
     DATABASE_URL=sqlite:////opt/ai-news-radar/data/news.db
   可选（不开也能跑，系统工作在规则模式）：
     LLM_BASE_URL / LLM_API_KEY / LLM_MODEL      提升摘要与"AI 深度分析"
     GITHUB_TOKEN                                把 GitHub 配额从 60 次/小时提到 5000
   注意：env 里同一个键写两次会踩坑（后者生效，但手工跑的脚本可能取到前者）。

4) 调参：config/settings.yaml（阈值、节奏、静默时段…）与 config/sources.yaml（来源开关）

5) 建库 + 自检
   .venv/bin/python scripts/init_db.py
   .venv/bin/python -m app.main --self-check      # 一行看清配置/库/来源健康

6) systemd
   单元名：ai-news-radar.service
   必须项：EnvironmentFile=/etc/ai-news-radar/env、Environment=TZ=Asia/Shanghai、
           WorkingDirectory=/opt/ai-news-radar
   TZ 必须与 settings.yaml 的 timezone 一致（有用例守着这条，因为日志时间戳靠它）。

7) 部署到远端机器（本仓库自带的脚本）
   bash scripts/deploy.sh anr-vps anr-jump
   成功标志：service=active schema=ok stamp=<UTC 时间戳>；再用 md5sum 核对三端代码树一致。

==================================================
三、日常命令（Telegram）
==================================================
/start         注册这个聊天为订阅者
/help          命令一览
/latest        最近一批新闻（分页）
/today         今天（本地日）的新闻，共 N 条 · 分页；/today 2 直接看第 2 页
/news          按分类浏览
/search 关键词  中文或英文都能搜（中文查询已被专门修过；品牌别名见 settings.yaml）
/summary       对某条做摘要；未配置 LLM 时不会出现"AI 深度分析"按钮
/free [关键词]  免费/限免信息，标注可信度；已确认的 0 价模型单列
/digest        立刻取一份简报（不等定时窗口）
/settings      你的开关：简报时间、突发开关、静默时段、**当日突发名额已用/上限**
/sources       各来源健康（🟢🟡🟠🔴 的含义与 /stats、日志完全一致）
/stats         库内数量、待处理积压、磁盘余量与变化方向、失败来源
/pause /resume 暂停全部推送 / 恢复
/setinterest   设置你的兴趣方向

==================================================
四、手动跑一轮（调试、不想等定时器）
==================================================
.venv/bin/python -m app.main --once        采集 + 处理一轮后退出，不启动 Bot
.venv/bin/python -m app.main --collect     只采集（隐含 --once）
.venv/bin/python -m app.main --no-bot      只跑采集与调度（采集机专用，省内存）
.venv/bin/python scripts/test_sources.py   逐个来源连通性测试
.venv/bin/python scripts/preview.py        本地渲染面板/卡片，看真实输出长什么样
.venv/bin/python scripts/delivery_report.py --kind morning   简报送达证据
.venv/bin/python scripts/log_incidents.py --since 6          按根因分组的日志摘要
.venv/bin/python scripts/reconcile_db.py                     库内数据修复（默认 dry-run，--apply 才写）

==================================================
五、FAQ
==================================================
Q：为什么今天一条突发都没有？
A：看 /settings 的"今日突发名额"。上限是 max_per_day（默认 5），用完就要等本地日切；
    冷却是 cooldown_minutes（默认 60）。夜里 23:00-07:00 到达的大新闻不会丢，
    会排队在窗口结束后补发（重试队列每轮再问一次）。

Q：为什么有的新闻是英文？
A：免费翻译额度用完后新行会显示原文，这是预期行为。要么提高 translate.daily_budget，
    要么配 LLM_*。已入库的行不会因为"字段非空"而重翻——这是历史遗留，清理需要人工确认。

Q：某个来源一直报错？
A：/sources 会点名并给出连续失败次数与最后错误；"对方要求降速"和"我们自己配额用尽"
    不算来源故障，不会计成失败 streak。机房 IP 被 Cloudflare/Reddit 拦是常态。

Q：GitHub 新闻少了？
A：无 token 时每 IP 60 次/小时是硬约束。系统靠条件请求（304 不计费）省额度；
    配额账本和验证器缓存在 data/ 里，重启不会重新花掉。补 GITHUB_TOKEN 是唯一的量级提升。

Q：磁盘快满了？
A：/stats 的磁盘行给"最近窗口 ±MB"和方向；只有在真的在变少时才给"约几天写满"。
    低于 alerts.min_free_mb 会明确告警。

Q：想改推送时间/语言风格？
A：改 config/settings.yaml（digest.*、labels.*、breaking.*、quiet_hours），
    重启生效；个人开关用 /settings（覆盖全局默认）。

Q：测试怎么跑？
A：.venv/bin/python -m pytest（当前 772 条用例）。改动呈现/语言规则后，
    必须枚举每一个渲染面再看一遍真实输出（scripts/preview.py），只跑单测不够。

==================================================
六、卸载
==================================================
systemctl disable --now ai-news-radar.service
rm /etc/systemd/system/ai-news-radar.service
数据都在 /opt/ai-news-radar/data/（news.db + 若干 *.json 状态文件），删目录即清干净。
先备份 news.db 再动手。
