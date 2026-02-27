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
3. 安装后配置 Trakt 用户名、Client ID 及豆瓣 Cookie（可留空使用 CookieCloud）

## 默认分支

请确保 GitHub 仓库默认分支为 **main**，否则市场无法拉取 `package.json`。
