# MoviePilot 插件发布方法

本方法适用于本仓库插件，不是 MoviePilot 核心程序、容器镜像或数据库的发布方法。
参考 Alex 的集成工作区 `moviepilot/docs/local-mp-plugins.md`；真实部署工具与参数以生产 MCP 的 `tools/list` 为准。
发布必须区分 **仓库发布 → 生产安装 → 加载验证 → 凭据只读验收 → 业务验收**，不能只看安装接口成功。

## 1. 明确授权边界

- 确认插件 ID、目标版本、仓库、生产实例及允许操作。
- 安装插件、修改配置、改变调度、执行同步是不同操作，不自动打包为一次授权。
- 「升级插件」不意味着允许签发 ASS Key、配置 Cookie、切换凭据来源、恢复验证码暂停或立即同步。
- 插件会重新注册原有调度。升级前核对启用状态、执行中状态及下一次时间；
  已启用的原有定时任务不是此次发布主动触发的业务验收。
- 需要暂停/恢复调度时明确记录并获准；若不能确认任务空闲或升级窗口安全，先停止发布。

## 2. 本地候选与回退准备

1. 检查插件仓库与 MoviePilot 父仓库 Git 状态，不把父仓库用户改动一起提交。
2. 修改 `plugins/<id>/`，不改 `app/plugins/` 运行副本。
3. 同批维护 `plugin_version`、`package.json` 版本/history、配置说明与测试。
4. 使用既有 Conda 环境运行相关测试；公共 helper、初始化与凭据变更扩到插件仓库全套测试。
5. 执行 Pylint 错误级检查和 diff 检查，排除真实凭据、临时生产载荷及无关文件。
6. 记录升级前线上版本、源码 SHA、运行文件 hash，以及该版本对应的仓库提交。

本仓库当前验证命令（从插件仓库目录运行 pytest）：

```bash
conda run -n movie-pilot pytest
```

对 `TraktRatingsSync`，从 MoviePilot 根目录运行：

```bash
conda run -n movie-pilot pylint local-plugins/mp-plugins/plugins/traktratingssync/ass_cookie_helper.py local-plugins/mp-plugins/plugins/traktratingssync/douban_helper.py local-plugins/mp-plugins/plugins/traktratingssync/netease_helper.py local-plugins/mp-plugins/plugins/traktratingssync/__init__.py --errors-only
```

本地单元测试禁止真实出站、真实配置及数据库读写。离线通过不代表生产环境依赖或实际网站鉴权已通过。

## 3. 安全接入生产

- MCP 路径为 `/api/v1/mcp`，使用 MoviePilot `X-API-KEY`，不是管理员短期 Bearer。
- 仅使用已经确认的 HTTPS 最终入口，校验证书且不跟随携带凭据的跳转。
  内网 IP 的 HTTP 本身不提供加密；不能因为 Codex 能连接就默认安全。
- Pi 可独立管理本机私有 MCP 配置，权限 `0600`；只复制指定服务器，不全量导入或动态引用 Codex。
  复用同一全局 API_TOKEN 不产生独立服务端只读身份，轮换要分别更新客户端。
- 白名单只开放本次需要的工具，并保留确认门禁。发布窗口结束撤下安装等写工具。
- 不直接把 `query_plugin_config`、`query_plugin_data` 或完整日志结果送进模型：只读工具也可能返回 Cookie/令牌。
  确需检查时，由本机可信程序读取并仅输出白名单布尔、计数、版本或比较结果。
- 管理员 REST 用本机已授权的忽略文件保存凭据，短期登录后保留 Cookie-aware Session；
  代码、文档、命令参数及会话里不出现真实 URL、密码、API Key、Cookie 或原始响应。

连接验收是 `initialize` → `notifications/initialized` → `tools/list` → 安全只读调用。
工具注册或本机配置存在不代表已连接。

若 Pi 会话尚未 `/reload`，网关可能仍显示旧目录或找不到新服务器。
优先重载后使用网关；本次发布也可在明确授权下使用受控、固定插件/工具白名单的标准 MCP JSON-RPC 客户端。
必须记录实际调用通道，不把 HTTP 协议调用写成已加载 Pi 网关。网关拒绝或用户否决时不得绕过门禁；
禁止因此扩大系统权限、关闭 TLS 或安装未经确认的通用代理。

## 4. 仓库发布与市场核对

1. 从插件仓库提交并推送 `main`，记录应用提交 SHA；不强推、不重写共享历史。
2. 确认远端 `main` 包含候选，且默认分支为 `main`。
3. MCP 查询 `query_market_plugins`：

   ```json
   {"query":"TraktRatingsSync","force_refresh":true,"max_results":5}
   ```

4. 核对精确 ID、自己的仓库、目标版本、系统兼容性和来源；不要升级成同名的其他作者插件。
5. 市场缓存仍旧时先刷新并重新查询，不盲目安装。缺失条目不是“可安装成功”的证据。

`install_plugin` 不支持按 Git SHA、分支或历史版本固定安装；调用前再次检查远端状态，
避免另一发布者在市场核对和安装之间改变候选。

## 5. MCP 安装

先查询并描述生产工具参数。`TraktRatingsSync` 当前升级形态：

```json
{
  "plugin_id": "TraktRatingsSync",
  "force": true,
  "force_refresh_market": true
}
```

- `force=true` 表示下载/更新；`force=false` 对已有插件可能只是刷新加载。
- 只发起一次安装，记录调用时间和是否获得明确 ACK。
- 超时、连接中断或响应丢失属于 **结果未知**，不等于安装失败。先查询线上源码、插件版本、运行和日志，
  再决定是否重试；不自动连续安装或通过重启绕过。
- 如果有明确 ACK 但源码仍旧，先确认市场/下载来源及加载问题，再做有理由的受限重试。
- 不调用 `run_scheduler`、`/sync`、重置/卸载或凭据更新作为安装验收。

## 6. 生产加载验证

安装 ACK 之后至少检查：

1. 再次调用 `query_installed_plugins`，运行版本与目标一致、`has_update=false`。
   安装 ACK 内的市场对象可能仍带安装前的 `has_update=true`，不能据此认定升级失败或重复安装。
2. REST `/api/v1/plugin/file/<PluginClassName>/__init__.py` 的 `plugin_version` 一致。
3. 尽可能逐个比较本次运行 `.py` 与发布提交的 SHA-256，防止新入口配旧 helper 或混合版本。
4. 插件表单、详情页和既有 API 注册可用；只检查结构，不输出配置值。
5. MCP `query_plugin_capabilities` 核对运行实例的服务定义；调度器核对目标 job，而不是完整列表的截断预览。
6. 本机内存比较升级前后的原有配置，确认凭据、启用状态、定时表达式和业务设置未被覆盖；
   新增默认字段单独核对，不能把默认字段增加误报为原配置损坏。
7. 比较已有记录/队列计数及最后运行标记，区分版本重载与真正的业务执行；不要读取/导出所有插件私有数据。
8. 查看发布窗口内加载相关日志是否报错，不复述完整日志、正文或旧版凭据诊断。

此阶段可称为「生产安装并加载成功」，不能称为「ASS Cookie / 网站 / 同步业务验收成功」。
不允许主动运行业务时，到此明确报告其余阶段未执行。

## 7. 另行授权的凭据和业务验收

ASS 接入先保持来源为原手动模式。另行获准后配置专用 Read Key/受保护只读挂载和固定条目引用，
执行不含豆瓣状态提交的账号/历史只读检查；网易云双 CSRF 先验证再固定作用域，不按值相同或顺序选取。

小范围业务验收须再次确认共享额度、目标范围、原有 pending 及验证码状态。
`TraktRatingsSync.run()` 会处理既有队列，不能把它当作只读连接测试。
只触发一次，再按调度状态与插件日志开始/汇总标记验收，不因等待超时重复触发。
通过后才按授权恢复/启用调度。

## 8. 日志读取与回退

日志：管理员 Bearer + Cookie-aware Session，先请求 `/api/v1/plugin/`，再读取
`/api/v1/system/logging?length=<n>&logfile=plugins/<plugin_log>.log`。
采用有界窗口并解析 `data: 【...】` SSE，超时保留已收到片段；调度器回到等待而日志流没关闭时，
先缩小窗口，不据此重跑任务。输出固定分类与计数，不输出旧凭据、响应正文或第三方 URL。

回退通过新的仓库提交恢复旧代码、维护可识别版本并按同一路径重新安装。
不要 `reset --hard` 共享历史、强推、直接覆盖运行目录或假设安装器自动留了可靠备份。
回退代码不会撤销数据库变化、凭据轮换、队列变化或已完成的外部写入；这些需要独立恢复方案。

对 ASS 接入版本尤其注意：旧版 3.20.0 不认识 ASS 来源字段，可能直接使用仍保存的手动 Cookie。
一旦已切换到 ASS，不能未经审查回退旧版并让原任务继续运行，否则破坏「无旧凭据回退」边界。
先按授权暂停任务，确认兼容代码或显式重新配置来源后再恢复；不要把代码回退当作凭据撤销。

## 9. 发布记录

每次记录在 `docs/releases/`：授权范围、旧/新版本、源提交、测试命令及结果、
市场/安装 ACK、线上文件比较、配置保留、调度和日志检查、明确未执行环节及异常处理。
只保存元数据，不记录生产地址、用户名、凭据、原始配置或日志。

实录：[TraktRatingsSync 3.21.0 生产发布](releases/traktratingssync-3.21.0-20261010.md)。
