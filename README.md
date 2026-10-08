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

1. 在 Trakt 开发者后台登记自己域名下的 HTTPS 回跳地址。使用会保留查询参数的地址；不要使用 localhost、私有 IP 或旧版 OOB 地址。
2. 在插件配置中填写新 Client ID，Client Secret 留空，授权方式选择 PKCE，填写完全相同的回跳地址。
3. 打开「生成新的 PKCE 授权链接」开关并保存，再重新打开插件配置页。
4. 复制并打开「Trakt 授权链接」，完成登录和授权。复制浏览器地址栏中包含 `code` 和 `state` 的完整回跳地址，粘贴到插件对应输入框，再保存。整个过程需在 10 分钟内完成。
5. 重新打开配置页确认「Trakt 授权成功」。授权码输入会自动清除，令牌自动保存和续期，不需要手动填写 Access Token。

回跳地址用于接收浏览器跳转，不要求额外部署接收接口；即使显示 404，只要浏览器地址栏保留 `code` 和 `state`，仍可复制该完整地址完成授权。不要使用会跳转并丢失参数的地址。

更换 Client ID 会清除旧 Trakt 授权，保留已同步记录和其他平台配置。生成新的授权链接不会启动同步。授权完成且豆瓣 Cookie 有效后，再手动运行同步。
如授权或公开接口仍返回 403，请检查应用是否存在及其访问权限；PKCE 不能恢复已删除应用或绕过访问限制。

## 默认分支

请确保 GitHub 仓库默认分支为 **main**，否则市场无法拉取 `package.json`。
