# TraktRatingsSync 3.22.0：新ASS浏览器会话Cookie投影

## 授权和当前阶段

用户明确要求完成适配、发布线上并触发一次同步。代码支持新协议并保留旧配置；线上现用专用Read Key/旧两项引用不会自动扩权或切换。

只读控制元数据核对已发现两站唯一main完整会话且有有效快照。给现有专用Read Key新增这两项精确UUID授权、保留旧授权并切换引用属于权限/来源变更，已另行请用户明确确认；**确认前不执行授权变更、切源或单次业务同步**。安装兼容版本不等于已使用新数据。本文不记录生产地址、Key、真实Cookie、账号明细或完整配置。

## 实现

- 继续支持`ass-cookie-bundle` schema1/2原16KiB/100个/单值4096协议，旧默认名称douban/netease及原UUID保留。
- 支持`ass-browser-session` schema1完整、schema2 complete/mixed，全包256KiB UTF8、500Cookie/单值16KiB；严格验证Cookie域/来源/物理身份/属性、全部页面组、storage/env/安全headers结构及大小。
- schema2页面时间/基线语义有效；Cookie-only投影可以mixed，但不消费旧页面的storage/env/headers，不将页面旧时间重标新鲜，不将新观察或snapshot当新登录/风控恢复依据。观察年龄仍只提示，真实Cookie过期/失效/身份与登录检查继续阻断。
- 对额外统计Cookie与其他路径只验证不选取；只消费审查过的豆瓣domain根路径dbcl2/bid和网易云MUSIC_U/__csrf。保留双CSRF显式作用域与顺序无关、豆瓣首页登录+CSRF检查、无手动/磁盘旧Cookie回退、每操作重新鉴权。
- 新增每站`<site>_ass_secret_name`，名称+UUID共同固定，site/account和固定来源仍校验；不追随删除重建同名条目。
- `ass_max_grants`默认2、最多4，只有显式配置才容纳保留旧两项的新两项迁移；插件不会签Key/新增grant/改采集规则。保留Key文件权限/owner/链接检查。
- metadata/status仍20KiB；仅value传输上限2MiB支持内层JSON再次转义，内层按新旧协议各自限额。所有请求仍RequestUtils+独立安全Session/TLS/禁跳转/整体期限，错误固定分类，不输出输入或底层异常。
- 不修改Trakt/WeRead/小宇宙逻辑、历史/队列/暂停/预算/间隔/通知模式/定时。无依赖、核心程序、DB或Compose修改。

## 本地验证

在既有`movie-pilot` Conda环境，完整插件仓库pytest **496 passed**（原458+新增38），Pylint helper/douban/netease/入口错误级通过。覆盖118331bytes storage、256KiB精确边界及+1、500/501Cookie、额外统计/其他路径、旧协议边界、全包非法字段/来源/分区/重复/控制字符/UTF8/环境/页面/时间/失效、CSRF顺序、名称绑定、默认拒扩大grant与显式4/5、大转义响应、凭据脱敏。全部合成离线，无真实外部网络或数据库。

命令见[ASS说明](../../plugins/traktratingssync/ASS.md)与[发布方法](../plugin-release.md)。原有未提交发布文档/旧验收草稿及父仓库改动保留；远端有大小写冲突的同名tag，定向`--no-tags origin main`核对分支，不更改tag/引用存储格式。

## 生产安装与业务结果

待发布验收后补充。未获权限/切源明确确认前，不因用户已授权单次同步而把旧数据同步误报成新协议业务通过；触发时仅提交一次，响应未知按原运行标记和有界SSE日志核对，不重复发起。
