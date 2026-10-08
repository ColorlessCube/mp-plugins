# mp-plugins

MoviePilot 自用插件库，用于在插件市场中展示和安装。

## 仓库结构

- **package.json**：插件市场列表与元数据（必填，且每个插件需带 `"v2": true` 才会显示）
- **plugins/**：插件代码目录，子目录名为插件 ID 的小写形式（如 `traktratingssync`）
- **icons/**：插件图标（可选）

## 当前插件

| 插件 ID | 说明 |
|--------|------|
| TraktRatingsSync | 从 Trakt 读取用户电影评分，匹配豆瓣条目并同步为「看过」及评分 |

## 在 MoviePilot 中使用

1. 设置 → 插件 → 插件市场，添加仓库地址：`https://github.com/ColorlessCube/mp-plugins`
2. 刷新后可在市场中找到并安装「Trakt 评分同步豆瓣」
3. 安装后配置 Trakt 用户名、Client ID 及有效的豆瓣 Cookie。

## Trakt 新应用授权（3.15.0 起）

Trakt 新应用可能只提供 Client ID，不再提供 Client Secret。插件支持官方
[PKCE 授权](https://developer.trakt.tv/docs/pkce)，旧应用仍可使用设备码授权。

1. **3.15.1 起推荐自动回跳。** 先在浏览器登录 MoviePilot 管理员，在 Trakt 后台登记 `https://你的MoviePilot域名/api/v1/plugin/TraktRatingsSync/oauth/callback`。若服务使用独立端口，域名后包含该端口。不要填写未注册的 `/api/v1/trakt-callback` 路径，也不要使用 localhost、私有 IP 或旧版 OOB 地址。
2. 在插件配置中填写新 Client ID，Client Secret 留空，授权方式选择 PKCE，填写完全相同的回跳地址。
3. 打开「生成新的 PKCE 授权链接」开关并保存，再重新打开插件配置页。
4. 在同一浏览器点击「打开 Trakt 授权页面」，完成登录和授权。回跳接口会自动校验并交换令牌，返回包含中文结果消息的标准 JSON 响应，不需要复制地址。整个过程需在 10 分钟内完成。
5. 重新打开配置页确认「Trakt 授权成功」。令牌自动保存和续期，不需要手动填写 Access Token。

自动回跳使用 MoviePilot 已有的管理员资源 Cookie 鉴权，不要求在 URL 中附加 API key。回跳域名应与登录 MoviePilot 的域名相同，否则浏览器不会携带登录 Cookie；Cookie 缺失或非管理员身份时接口返回 403，应先在该域名登录管理员再重新授权。错误 state、过期请求或已消费请求不会交换令牌，授权成功不会启动同步。

旧版手动回跳仍兼容：授权后复制包含 `code` 和 `state` 的完整地址，粘贴到「手动回跳地址」并保存。仅登记一个没有接收接口的地址始终会显示 404，重新生成链接不会让该地址变成真实接口。

更换 Client ID 会清除旧 Trakt 授权，保留已同步记录和其他平台配置。生成新的授权链接不会启动同步。授权完成且豆瓣 Cookie 有效后，再手动运行同步。
如授权或公开接口仍返回 403，请检查应用是否存在及其访问权限；PKCE 不能恢复已删除应用或绕过访问限制。

## 默认分支

请确保 GitHub 仓库默认分支为 **main**，否则市场无法拉取 `package.json`。
