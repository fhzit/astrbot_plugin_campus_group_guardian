# CHANGELOG

## v0.1.0（首个可部署版本）

### 数据
- 上游 `lexicon.db`（v2.8.7 / meta v1.3）逐字节复制为内置词库：66,993 词条 / 24,531 条启用正则；默认加载原词库，短词保留，由 `moderation.lexicon_path` 显式覆盖。
- 新增 `LEXICON_ATTRIBUTION.md` 记录词库来源、哈希、schema 与许可边界；逐来源许可未审计前不把词库称为本项目原创或 MIT 内容。

### 新增
- 平台适配层：`platforms/{base,registry,onebot,qq_official,wecom,dingtalk}.py`，使用 `PlatformCapability`、`PlatformLimits`、`RecallPolicy`；不支持的操作返回 `UNSUPPORTED`，不抛 `NotImplementedError`、不使整条处置失败。
- 统一消息模型 `GuardianMessage` / `GroupKey`（`platform + adapter_id + group_id`）。
- 家校群虚假缴费反诈 `School Fraud Guard`：本地快筛、金额提取、同群同发送者上下文、多消息组合加权、弱信号封顶、快速通道；群主/管理员 `ANTI_FRAUD_BYPASS`，可信名单 `TRUSTED_SENDER_BYPASS`。
- 内容审核引擎：纯 Python Aho-Corasick + 正则 +（可选）AI 复核。
- OCR：UApiPro + AstrBot 视觉模型，四种组合策略、去重缓存、并发限制；AI 只使用 AstrBot Provider ID。
- 处置引擎：能力检查 → 权限检查 → 平台调用 → `ActionResult` → 落库。
- 存储：`aiosqlite` + `schema_version` 迁移 + 迁移前备份 + 失败回滚，数据保存在插件数据目录。
- 定时任务：到点解禁、OCR/AI 缓存清理、上下文清理、按保留天数清理历史数据。
- 指令：`/守护状态`、`/守护统计`、`/反诈状态`、`/反诈开启`、`/反诈关闭`、`/反诈测试 <文本>`、`/禁言`、`/解禁`、`/踢人`。
- LLM 工具：`antifraud_test_text`、`ban_group_member`、`unban_group_member`、`kick_group_member`、`recall_group_message`；后端强制权限校验。
- AstrBot Plugin Page：总览、反诈事件、平台能力、测试器、诊断、动作日志。
- Web API：总览、能力矩阵、反诈事件及复核、配置、日志、诊断、测试、可信操作者、旧版迁移。
- 迁移助手：检测原插件数据库、词库及审核日志，备份后导入。
- `scripts/astrbot_deploy_check.py`：使用真实 AstrBot 官方 `PluginManager` 和临时数据目录验证加载。

### 旧版配置映射

| 原插件键（示例） | 新键 |
| --- | --- |
| `auto_moderate_enabled` / 审核总开关 | `moderation.enabled` |
| `moderation_admin_exempt` | `moderation.admin_exempt` |
| 敏感词库路径 | `moderation.lexicon_path` |
| 审核记录保留 | `storage.violation_retention_days` |
| 群白名单 / 黑名单 | `general.group_white_list` / `general.group_black_list` |
| 触发次数阈值、刷屏设置 | `anti_flood.*` |
| 群级个性化配置 | 数据库 `group_config_overrides` |

未覆盖的旧键会在迁移助手中列出；缺少旧键不会阻止启动。

### 验证
- 单元 + 集成测试：**109 项通过、2 项跳过**（需真实 AstrBot 源码的测试）；提供 AstrBot v4.28.1 源码后真实冒烟另有 3/3 通过。
- 真实 AstrBot v4.28.1 API 冒烟：**3/3 通过**（真实 API 导入、装饰器注册、配置 Schema）。
- 官方 `PluginManager` 真实加载：全部通过（metadata、指令/LLM 工具、数据库迁移与备份、12 张表、12 组配置、6 个平台画像、`terminate()`）。
- 覆盖撤回策略、反诈样例、管理员放行、OCR 失败降级、权限、存储迁移、QQ API 请求构造、真实 `main.py` 端到端 Mock 流程。
- 词库与上游 v2.8.7 的 `lexicon.db` 逐字节一致；SHA-256 与来源文件相同，SQLite `integrity_check=ok`。加载保留全部 66,993 条关键词（包括单字项），正则按单次上限加载 800 条并记录跳过数。

### 已知限制
- 企业微信 / 钉钉当前无撤回、禁言、踢人接口：降级为记录 + 预警 + 通知。
- QQ 官方群成员 / 黑名单接口可能需要白名单权限，未获批时相关动作返回 `UNSUPPORTED`。
- 长期禁言（>30 天）按平台上限分段提交；续期能力依赖平台限制。
- 入群审核自动审批仅在平台提供 `REVIEW_JOIN_REQUEST` 能力时执行。
