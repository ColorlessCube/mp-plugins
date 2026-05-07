# ChineseSubtitle v1.2.8 版本说明

## 更新内容

- 新增 OpenSubtitles 每日下载额度限制，默认每天最多 5 次。
- 仅在调用 OpenSubtitles `/download` 获取下载链接前占用额度；普通搜索不计数。
- 登录失败、下载链接获取失败或返回空链接时会回滚本地额度计数。
- 配置页新增「OpenSubtitles 每日下载上限」，可按账号额度调整；设置为 0 表示不限制。
- 下载额度按本地日期每日自动重置，并持久化保存，避免目录扫描反复触发耗尽免费额度。

## 使用说明

免费额度场景建议保持默认值：

```text
OpenSubtitles 每日下载上限 = 5
```

达到上限后，插件会跳过 OpenSubtitles 下载并输出日志：

```text
OpenSubtitles 今日下载额度已用完：5/5，跳过下载
```

ASSRT 和 SubDL 不受这个限制影响。

## 验证

使用新的 API Key 调用 OpenSubtitles 搜索接口验证通过；未调用 `/download`，不会消耗下载额度。
