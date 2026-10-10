# 豆瓣书影音同步 3.21.0 生产发布记录

## 范围与源版本

用户明确要求将新插件部署到线上，允许尝试 MCP，并总结后续发布方法。
本次只提交/推送插件仓库、生产安装和加载验证；不切换 ASS 来源、不签发/挂载 Read Key、
不主动触发真实同步或改变调度、恢复验证码暂停。MoviePilot 核心程序和容器未升级。

- 插件：`ColorlessCube/mp-plugins` / `TraktRatingsSync`（豆瓣书影音同步）。
- 旧版本：`3.20.0`；仓库基线 `d246001e0ac7c3a0bc9fcb5a62b6161088bc9fa9`。
- 应用发布提交：`d28823215d82b848c8a90f087fd4e8ae2f665625`，已推送 `main`。
- 目标版本：`3.21.0`，含 Douban/NetEase ASS 只读适配、独立来源选择与无旧凭据回退、作用域校验和凭据诊断脱敏。
- 实际安装窗口：2026-10-10 16:40:08–16:40:17（UTC+08:00）。
- 父 MoviePilot 仓库既有用户改动未暂存/提交；原 ASS 错插件草稿未修改。

## 本地验证

- `conda run -n movie-pilot pytest`：**373 passed**，含 87 项 ASS 测试；真实出站被测试 fixture 阻止。
- 4 个修改的 Python 模块 Pylint `--errors-only`：通过。
- `git diff --check`：通过。
- 本仓库没有此次提交的 GitHub Actions 运行；以上是本地验证，不能称作 CI 通过。
- 无新运行依赖、宿主框架改动或数据库迁移。

具体命令和阶段边界见 [插件发布方法](../plugin-release.md)。

## 生产连接与调用通道

- 从已授权本机私有记录找到 HTTPS 最终入口，证书校验开启、禁跳转。
- 匿名 `/api/v1/mcp` 返回 `405`；携已有 MoviePilot API Key 的 `initialize` 返回 `200`、
  服务名 `MoviePilot`、协商协议 `2025-11-25`。未向原内网 HTTP 地址发送密钥。
- 完成 `notifications/initialized`、`tools/list`，生产确有插件市场、安装、已安装及能力查询工具。
- Pi 私有配置已独立改到 HTTPS，权限 `0600`，没有 imports 或 Codex 动态引用，Codex 文件未改。
- **当前 Pi 会话网关仍是旧目录**，`connect moviepilot-production` 提示找不到服务器。
  本次实际使用本机受控、固定插件/工具白名单的标准 **HTTPS MCP JSON-RPC** 客户端，
  通过宿主 `RequestUtils` 调用，并非宣称网关已重载。
- 后续 Pi 会话需 `/reload` 或重启加载配置；发布窗口结束移除安装写工具，只保留元数据查询及确认门禁。
  同一全局 API_TOKEN 仍不是独立只读服务端身份。
- 管理员 REST 仅由可信本机程序检查配置、页面、调度和文件；明文只在进程内存，
  原始配置、Cookie、Key、账号和日志未进入会话或本记录。

## 市场与单次安装

1. 升级前 MCP `query_installed_plugins`：`3.20.0`、已安装/启用、仓库正确。
2. 生产旧 `__init__.py` SHA-256 为
   `fd932dd6642a5c2b4344755d5110a2af52313e1483d906d4668ef6b69326fff4`，与 `d246001` 文件一致。
3. 发布后最初市场仍显示 `3.20.0`；只做安全刷新查询，不提前安装。
4. 后续 `query_market_plugins(force_refresh=true)` 显示 **3.21.0**，仓库正确、`has_update=true`。
5. 安装前再次核对远端 `main=d288232…`、线上仍为旧源码，插件页面未运行。
   原 job `TraktRatingsSync_trakt_ratings_sync` 为等待，距下次执行约 17 小时。
6. **仅调用一次** `install_plugin`：

   ```json
   {"plugin_id":"TraktRatingsSync","force":true,"force_refresh_market":true}
   ```

7. MCP 明确 ACK：`success=true`、`force=true`、`refreshed_only=false`、运行快照 **3.21.0 / state=true**。
   ACK 的市场对象仍携带安装前 `has_update=true`；再次查询后已为 `false`，没有因此重复安装。

未调用安装 REST 端点、任意远程命令、卸载/重置、手工复制运行目录或核心重启。

## 加载与状态验证

- 后续 MCP 查询：运行 **3.21.0**、已安装/启用、`has_update=false`、仓库正确。
- 线上 `__init__.py` SHA-256：
  `5bd9041ee2bc5f63f12847771e3c170b44d4436a8085679374a387b5839f55ea`。
- **8 个运行 `.py` 文件**逐个与本地已发布应用提交比较，全部一致：
  `__init__.py`、`ass_cookie_helper.py`、`douban_helper.py`、`matching_helper.py`、
  `netease_helper.py`、`trakt_helper.py`、`weread_helper.py`、`xiaoyuzhou_helper.py`。
- 表单和详情页 `200`；表单包含两个来源选择及网易云 CSRF 范围字段。
- 原有配置所有字段在同一可信进程内逐项比较，**全部保留**；仅新增默认来源/引用配置。
  原 Cookie、启用状态、其他平台凭据及业务设置未覆盖。新增默认字段导致整体配置指纹改变，
  不能把它误判为原配置被改写。
- 豆瓣/网易云仍为 **manual**；未配置 ASS 连接、Read Key 或实际条目引用。
- 原定时表达式 **`0 10 * * *`** 不变；运行实例暴露同一 cron 服务，目标 job 为等待，
  核对时下次执行约 17 小时后。保留了原有调度，未主动触发或新启用任务。
- 最近运行标记不变，页面未运行。前后白名单计数完全一致：

  | 项目 | 前 | 后 |
  | --- | ---: | ---: |
  | 原最近一轮写入 | 1 | 1 |
  | 原最近一轮未变化跳过 | 60 | 60 |
  | 原最近一轮提交失败 | 0 | 0 |
  | 待写入 | 0 | 0 |
  | 未匹配 | 1 | 1 |

  这些是**原有记录**，不是本次同步结果；未导出全部私有数据或历史条目。
- 有界读取两个日志文件，HTTP 均为 `200`；发布窗口主日志筛出 13 行，其中插件相关 7 行、
  1 个加载标记，未发现目标 ERROR/CRITICAL/加载失败或 traceback 标记；插件专属日志窗口无新增行。
  SSE 流在有界超时后关闭，保留已读片段；这不是完整日志证明或后续业务健康验收。

## 结论与剩余阶段

**仓库发布、生产安装和加载验证完成。** 没有真实 Cookie 来源切换、网站账号/CSRF 或同步写入验收。

后续另行授权：

1. 专用、精确条目范围的 ASS Read Key 与受保护只读挂载，核对实际 tenant/UUID/site/account。
2. 保持业务写入不触发，验证豆瓣/网易云网站账号及双 CSRF 的正确作用域。
3. 获准后才做小范围写入、评估原 pending/共享额度/验证码状态，再决定调度变更。

本次未额外导出生产配置/数据库或进行恢复演练。回退须新的正常仓库提交与安装，不重写历史。
若以后已切 ASS，旧 3.20.0 不理解来源字段且可能恢复使用手动 Cookie，必须先审查并按授权暂停，
不能直接带运行任务回退。见 [发布方法的回退边界](../plugin-release.md#8-日志读取与回退)。
