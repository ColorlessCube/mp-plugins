# ChineseSubtitle v1.2.7 版本说明

## 更新内容

- 目录扫描模式新增同级 NFO 读取能力。
- 支持从 `视频名.nfo`、`movie.nfo`、`tvshow.nfo` 以及同目录其它 `.nfo` 文件读取媒体 ID。
- 支持解析 `<imdbid>`、`<tmdbid>`、`<tvdbid>` 以及 `<uniqueid type="imdb|tmdb|tvdb">`。
- OpenSubtitles 检索会优先使用 NFO 中的 IMDb ID，其次使用 TMDB ID，最后才退回标题检索。
- OpenSubtitles 带 moviehash 未命中时会自动去掉 moviehash 重试，避免本地文件 hash 过度过滤候选。
- OpenSubtitles 非 200 响应会输出状态码、查询参数和返回摘要，便于区分“无字幕”和“接口被拒绝”。
- 多个 NFO 同时存在时，优先使用带 IMDb ID 的 NFO，提升中文文件名场景下的字幕命中率。

## 使用说明

开启“目录扫描”后无需新增配置。只要视频同级目录存在有效 NFO，插件会自动读取媒体 ID 并用于字幕检索。

NFO 示例：

```xml
<movie>
  <title>A Man Called Ove</title>
  <year>2015</year>
  <imdbid>tt4080728</imdbid>
  <uniqueid type="tmdb">348678</uniqueid>
</movie>
```

扫描日志中会出现类似记录：

```text
中文字幕扫描从 NFO 读取媒体ID：一个叫欧维的男人决定去死 (2015) - 1080p.mkv imdb=tt4080728 tmdb=348678
```

## 兼容性

- 不影响整理完成事件原有逻辑。
- NFO 缺失、解析失败或没有媒体 ID 时，会继续使用原有文件名/标题检索流程。
