# astrbot_plugin_campus_group_guardian（校园群聊卫士）

跨平台 AstrBot 群聊治理插件：**统一多平台能力层** + **家校群虚假缴费反诈**（School Fraud Guard）。基于 MIT 许可的 [zcj-ui/astrbot_plugin_group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian) 重构，保留成熟治理能力，并将 OneBot 强耦合改为能力驱动的适配层。

> 本插件是独立插件（ID：`astrbot_plugin_campus_group_guardian`），与原插件可并存，不是原插件升级覆盖版，也不是从零开发。业务层不按平台分支；平台差异通过 `PlatformCapability` / `RecallPolicy` / `PlatformLimits` 表达。

## 功能

- 多平台群管理与能力降级
- 内容审核：文本标准化、Aho-Corasick、正则、上下文、可选 AI 复核
- **内置原版敏感词库**：66,993 条关键词（含单字项）、24,531 条启用正则；来源与许可边界见 `LEXICON_ATTRIBUTION.md`
- 敏感词、广告、违规网址、辱骂、色情、暴恐、危险物品、隐私、诈骗等分类审核
- 刷屏监控、图片审核、OCR、AI 审核
- **家校群虚假缴费反诈**：本地快筛、OCR/Vision、风险评分、自动处置、人工复核
- 违规记录、处罚策略、定时解禁、动作日志、统计、AstrBot Plugin Page
- 原插件配置 / 数据迁移助手

## 平台支持

| AstrBot 平台 | 适配器 | 说明 |
| --- | --- | --- |
| `aiocqhttp` | OneBot v11 | NapCat / LLOneBot 等；高级能力按实际接口降级 |
| `qq_official` | QQ 官方机器人 | 网关模式 |
| `qq_official_webhook` | QQ 官方机器人 | Webhook 模式 |
| `wecom` | 企业微信 | 内容审核、反诈、主动通知 |
| `wecom_ai_bot` | 企业微信 AI Bot | 内容审核、反诈、主动通知 |
| `dingtalk` | 钉钉 | 内容审核、反诈、主动通知 |

跨平台检测一致，但管理能力不保证一致。不支持的动作明确返回 `UNSUPPORTED`，并继续记录、预警、通知。

| 功能 | OneBot | QQ 官方 | 企业微信 / 钉钉 |
| --- | --- | --- | --- |
| 群消息、图片、内容检测、OCR、AI | ✓ | ✓ | ✓ |
| 主动消息（预警 / 通知） | ✓ | ✓ | ✓ |
| 撤回、禁言、解禁、踢人 | ✓ | ✓（受角色 / 接口权限限制） | — |
| 群黑名单 | — | ✓（可能需白名单权限） | — |

## QQ 官方机器人配置

1. 开放平台需开启“可获取全部群消息”，否则可能只能收到 @ 消息；
2. 撤回他人消息、禁言、踢人要求机器人在群内有相应权限；
3. 普通成员身份撤回自己消息有 120 秒窗口；群成员及黑名单接口可能需开放平台白名单；
4. 禁言单次最长 30 天，插件按平台约束处理。

## OneBot、企业微信与钉钉

OneBot 使用 AstrBot `aiocqhttp` 适配器（反向 WebSocket）。公告、群文件、精华等扩展能力取决于协议端实现，缺失时按 `UNSUPPORTED` 降级。企业微信 / 钉钉当前支持内容检测与风险通知，不声明撤回、禁言、踢人、黑名单能力。

## 家校群反诈

- 群主 / 管理员的缴费信息不进入反诈判定（`ANTI_FRAUD_BYPASS`）；这不等于内容审核豁免。
- **管理员内容审核豁免**由 `moderation.admin_exempt` 单独控制，反诈放行与内容审核互不覆盖。
- `UNKNOWN` 角色按普通成员处理；不能把角色未知误当管理员。
- 综合收费语义、金额、付款动作、学生姓名备注、付款截图、催促、教师身份冒充、图片 OCR 等信号；单独二维码或单一弱信号不会直接认定诈骗。
- 同群同发送者的多条消息按上下文联合评估；组合风险快速处置不依赖 AI。
- `SAFE` 放行、`SUSPICIOUS` 留痕、`HIGH` 管理员提醒、`CRITICAL` 按策略撤回 / 临时禁言 / 群内提醒 / 事件记录。
- 群内预警不公开指认个人；不长期保存付款原图，只保留必要文本摘要和信号。

## OCR 与 AI

- OCR 策略：`uapi_only` / `ai_only` / `uapi_then_ai` / `ai_then_uapi`；UApiPro 或 AstrBot 视觉 Provider 可用。
- 只在配置与风险信号满足条件时调用 OCR/AI；OCR 失败不会抵消本地风险信号。
- AI 只选择 AstrBot 已配置的 Provider ID，不另存 API Key；结构化输出解析失败不作为违规结论。

## 安装与初始配置

1. 在 AstrBot 插件市场搜索 `astrbot_plugin_campus_group_guardian`，或安装本插件目录；插件 ID 独立，与原插件可并存。
2. 首次加载后配置群白名单 / 黑名单、审核类别、反诈策略、OCR Provider；凭据通过配置项提供，不写入日志。
3. 打开 Dashboard 插件页“校园群聊卫士”，检查平台能力矩阵与诊断结果。
4. 默认加载包内 `lexicon.db`；配置 `moderation.lexicon_path` 可指定替代词库。
5. 如需旧版数据，使用诊断页迁移：先备份，再导入。

## 词库说明

内置 `lexicon.db` 与上游原项目数据库逐字节相同，共 66,993 条关键词（包括单字项）及 24,531 条启用正则。单条消息审核用纯 Python Aho-Corasick 自动机扫描全词库；默认如此，无需额外二进制依赖。可选 pyahocorasick 后端默认关闭，仅在 `advanced.matcher_use_pyahocorasick` 启用且扩展已安装时切换；否则自动回退纯 Python。正则按配置的单次上限加载。

词库第三方来源未逐项审计，**上游 MIT 许可证不代表词库本身自动取得 MIT 授权**；分发/使用前请阅读 [`LEXICON_ATTRIBUTION.md`](LEXICON_ATTRIBUTION.md)。

## 隐私

- 数据库位于 AstrBot 插件数据目录；更新插件不会清空数据，迁移前自动备份。
- 事件只保留必要文本摘要、风险信号和动作结果；默认不长期保存付款图片。
- 不在日志中输出 UApiPro Key、AstrBot Provider Secret、Bot Secret。

## 部署验证

```bash
# 单元 + 集成测试（默认使用 AstrBot 桩）
.venv/bin/python -m pytest -p no:cacheprovider

# 真实 AstrBot API 冒烟和官方 PluginManager 加载检查
ASTRBOT_SRC=/path/to/AstrBot .venv/bin/python -m pytest tests/integration/test_real_astrbot_smoke.py
ASTRBOT_SRC=/path/to/AstrBot .venv/bin/python scripts/astrbot_deploy_check.py
```
