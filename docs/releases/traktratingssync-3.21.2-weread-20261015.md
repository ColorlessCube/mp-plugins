# TraktRatingsSync 3.21.2：微信读书 Skill 1.0.5

## 状态与范围

2026-10-15 用户确认实施微信读书适配升级。本记录对应**本地候选 3.21.2**，
尚未提交、推送、安装生产或触发业务同步；生产仍为 3.21.1。
本次不修改 Trakt 授权逻辑、ASS 凭据来源、定时表达式、写入额度、数据库结构或浏览器扩展。

上一轮生产同步收到 `upgrade_info` 而停止微信读书读取。旧 Helper 固定上报 1.0.4，
并把读取失败与空书架都返回为 `[]`，主插件未将协议升级问题标记到来源状态。
新版本修正适配和状态提示，不要求用户仅因协议升级更换现有 API Key。

## 官方协议依据

实际读取并审查腾讯官方仓库固定提交 `d98e66ee60a8c7bcd35da549d0643c4470439643` 的：

- [SKILL.md](https://github.com/Tencent/WeChatReading/blob/d98e66ee60a8c7bcd35da549d0643c4470439643/skills/SKILL.md)
- [shelf.md](https://github.com/Tencent/WeChatReading/blob/d98e66ee60a8c7bcd35da549d0643c4470439643/skills/shelf.md)
- [book.md](https://github.com/Tencent/WeChatReading/blob/d98e66ee60a8c7bcd35da549d0643c4470439643/skills/book.md)

该提交的 SKILL.md 为 **1.0.5**，不能使用 README 中滞后的 1.0.4。
主要变更为版本上报和 `readerLink` / `deepLink` 分工；
整数百分比与完成时间的规则不是声称在 1.0.5 才新增，本次同时修正原适配中的推测逻辑。
文档属于待核对的外部契约，不作为执行远端代码或升级指令的授权。

## 实现

### 请求与凭据

- 固定 POST 网关，`api_name`、`skill_version=1.0.5`、业务参数平铺，沿用现有 Skill API Key。
- 继续通过 MoviePilot `RequestUtils`；独立 Session、TLS 校验、禁跳转、无环境代理/`.netrc` 注入。
- 传输异常在到达 RequestUtils 日志前脱敏；不输出远端 `errmsg`、`upgrade_info.message`、
  原始响应、API Key 或异常正文，也不自动执行服务端返回的升级指令。

### 数据契约

- `/shelf/sync` 的 `books[]` 仍按最近阅读时间排序、按原配置数量读取；不新增其他接口或扩展至有声书。
  这里的数量是电子书记录数量，不是包含 `albums[]` 和文章收藏的书架总数。
- 来源链接优先采用 `readerLink`（打开阅读），其次 `deepLink`（查看详情）；
  只接受受限的微信读书 HTTPS / 应用链接，缺失时留空，不再根据 ID 拼接。
  当前详情页仍显示原有豆瓣链接，没有新增列。
- `/book/getprogress` 使用 `book.progress` 的 0–100 整数百分比；1 表示 1%，
  不从 `readingProgress` 或小数比例猜测。时长优先使用 `recordReadingTime`（秒）。
- 书架 `finishReading=1` 的显式读完标记保留；否则只有 100% 且存在有效完成时间才标读完。
  0% 且未开始阅读为未读，不新建豆瓣在读状态。已有豆瓣短评、星级保护仍由原流程负责。
- 返回书籍 ID 与请求不一致或进度/书架结构不合法时停止，不将错误数据当作成功。

### 故障与恢复

- 每次最近书籍读取分别记录 `upgrade`、`auth`、`rate_limit`、`temporary` 或 `response` 错误。
  任意 `upgrade_info` 字段（包括空对象/null）都阻断，不能只检查其真值。
- 书架或任一本进度读取失败，整批候选不提交给主插件；不继续后续进度请求、不覆盖原书单、
  映射或成功记录，也不根据半成品状态生成新的豆瓣目标。旧已确认 pending 仍遵循原队列机制。
- 主插件使用现有 `notification_issues`、来源错误和统一通知冷却，详情页明确展示
  「微信读书 Skill 协议需要更新」，运行标记部分完成；通知关闭时页面故障仍保留。
- 协议升级、网络与限流提示不误导为 Key 失效；只有真实鉴权失败才提示检查 Key。
- 其他平台继续原同步流程。下一轮完整成功读取（包括合法空书架）才能解除来源错误。
  合法空书架可以更新最近来源列表为空，但不删除映射、成功历史或待处理队列。

## 本地验证

从插件仓库目录：

```bash
conda run -n movie-pilot pytest
```

从 MoviePilot 根目录：

```bash
conda run -n movie-pilot pylint local-plugins/mp-plugins/plugins/traktratingssync/weread_helper.py local-plugins/mp-plugins/plugins/traktratingssync/__init__.py --errors-only
```

- 全插件 pytest：**458 passed**；新增/扩展测试在旧实现先失败。
- Pylint 错误级检查通过；diff 与新增文档链接检查通过。
- 覆盖版本/顶层参数、官网链接优先级、整数百分比/完成证据、错误分类及脱敏、
  中途升级丢弃半成品、停止后续请求、状态持久化/通知关闭/恢复、正常豆瓣状态编排和原记录保留。
- 自动测试全部使用合成数据、请求桩和网络阻断；没有连接真实服务。

## 真实只读验收

在生产容器中使用独立短生命周期进程，从 stdin 内存加载候选 WereadHelper，
不替换已安装文件、不初始化主插件、不运行 `run()`；从既有配置只读取当前 WeRead Key。
额外限制仅 `/shelf/sync` 和随后一本 `/book/getprogress`，不接受写接口。

- 三次验证（实现过程中及最终候选）各发出 2 次只读业务 POST，总计 6 次；
  响应上限每次 2 MiB、整体读取期限 20 秒，TLS/禁跳转、日志静默。
- 三轮的书架和单本进度均 HTTP 200、无 `upgrade_info`、无业务错误；
  返回电子书列表、官方阅读器/详情链接与整数进度，候选完整格式化一本记录成功，错误分类为空。
- 原 API Key 可用，无需换 Key；没有输出书名、书籍 ID、账号、正文或凭据。
- 每轮前后比较 `last_run` 与完整生产配置均未变化；没有发送通知、提交豆瓣状态或触发同步。
- 仅验收了一本书的进度，不能当作所有书籍或完整业务同步验收；临时运维程序已删除。

## 发布边界

按[插件发布方法](../plugin-release.md)另行获准后提交、推送并通过正常安装流程升级，
核对版本、运行文件、配置与调度保留；需要执行同步时再明确授权。
无新依赖、数据库迁移或 Key 轮换。回退 3.21.1 会恢复 1.0.4 上报，可能重新遇到协议升级阻断，
且无法撤销已完成的豆瓣写入；不要以自动切换 Cookie 来源作为回退方式。
