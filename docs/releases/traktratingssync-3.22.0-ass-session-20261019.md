# TraktRatingsSync 3.22.0：新ASS浏览器会话Cookie投影

## 授权和当前阶段

用户明确要求完成适配、发布线上并触发一次同步。代码支持新协议并保留旧配置；线上现用专用Read Key/旧两项引用不会自动扩权或切换。

只读控制元数据核对发现两站唯一main完整会话且有有效快照。权限/来源变更曾另行等待确认；用户随后明确“允许”，已按下方记录完成现有Key两项精确授权增补、引用切换及唯一一次同步。安装阶段和新数据业务阶段分别验收。本文不记录生产地址、Key、真实Cookie、账号明细或完整配置。

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

在既有`movie-pilot` Conda环境，完整插件仓库pytest **496 passed**（原458+新增38），另挂socket解析/连接阻断器重跑496通过且零网络尝试；Pylint helper/douban/netease/入口错误级通过。覆盖118331bytes storage、256KiB精确边界及+1、500/501Cookie、额外统计/其他路径、旧协议边界、全包非法字段/来源/分区/重复/控制字符/UTF8/环境/页面/时间/失效、CSRF顺序、名称绑定、默认拒扩大grant与显式4/5、大转义响应、凭据脱敏。全部合成离线，无真实外部网络或数据库。

命令见[ASS说明](../../plugins/traktratingssync/ASS.md)与[发布方法](../plugin-release.md)。原有未提交发布文档/旧验收草稿及父仓库改动保留；远端有大小写冲突的同名tag，定向`--no-tags origin main`核对分支，不更改tag/引用存储格式。

## 生产安装与业务结果

源码提交 **`f1f21e1e0413a5d35d55a5a0f1b05a1e047ad251`**已push main。市场最初仍缓存3.21.2，正常刷新后确认精确仓库3.22.0且可更新，再进行一次管理员HTTPS REST标准install（force=true）。2026-10-19 **10:53:56–10:54:07 +08**明确ACK成功，8个运行py SHA256与候选逐个一致；MCP随后确认已安装3.22.0、has_update=false、兼容/启用。

同一受信进程内比较安装前后完整配置，原字段/凭据逐一一致；详情页数据摘要一致，未初始化运行/清历史或队列。表单可读取，原每日10点任务等待、下次约23小时后；没有配置PUT/调度触发/网站请求/服务重启。保护性本地install.once和0600脱敏回执留存，不输出实际地址/凭据/私有页面内容。

管理员cookie-aware会话prime后读取有界200行SSE日志，保留超时前片段：发布窗口插件无新记录，平台12条无WARNING/ERROR，无同步开始/结束。SSE保持开放导致超时是传输现象，不据此重复安装或触发。

安装并加载完成时尚未改权限或触发；随后用户明确“允许”，以下为新增授权、切源及业务验收，不将前一阶段冒充新数据业务通过。

## 已批准的精确读授权和切源

- 实时核对任务空闲，无requires_verification或冷却。发现旧两项已被删除（非本次删除），原两条UUID授权仍在，导致当天10:00旧来源运行未取得豆瓣凭据；不恢复旧项或绕过删除状态读其值。
- 常规Key更新会验证所有目标仍active，无法同时保留已删除项的原授权。写入前检查阻止该路径，未产生配置写入/回执。随后按批准范围进行**一次增量维护事务**：沿用tenant context/RLS、tenant/key锁、新两项validate_grants及控制状态检查、OperationContext幂等回执和共享audit，同事务仅新增两条精确对象grant并令现有Key revision1→2。审计操作`secrets.add_session_read_grants`、显式operator身份；未新增对外REST/MCP工具、未签Key或用客户端绕过权限。
- 保留旧两行授权，合计4条（旧2对应已删除项，实际有效2）；scopes仍metadata/read+value/read、rest、原到期时间和名称/描述不变，bootstrap文件未替换。新两项对应用户已批准的douban/netease main，不授租户或写权限。
- 通过生产已安装helper真实新鲜读取两站：实际是`ass-browser-session` schema1完整包，约67.5小时旧观察仍仅提示；未重标时间。schema2/mixed的契约此前离线覆盖，本次未将实际schema1描述为真实mixed验收。
- 豆瓣新Cookie用户标识与原配置仅内存比对一致，首页登录+新CSRF通过；网易云新Cookie以原domain CSRF成功调用账号只读接口。旧网易云手动凭据已失效，无法独立复核旧账号连续性，已告知用户，使用其明确批准的main来源；不输出/保存账号UID，也不声称与旧账号独立比对成功。只读验证未写插件业务状态或提交豆瓣状态。
- **11:14:21–11:14:24 +08**管理员HTTPS配置PUT一次ACK：只变化两项secret_name、两项secret_id、ass_max_grants=4，共5字段。原site/account已匹配无需改变；原Cookie来源ASS、CSRF domain、age3600、每天10点、每轮10写/10–20秒、其他凭据/配置均保留，持久状态/历史/队列摘要前后不变。配置保存并重载，不重启服务/容器。

## 唯一一次同步

**11:14:39–11:21:09 +08**，标准管理员REST runscheduler指向原job。仅发一次，受本地sync.once排他保护。同步接口阻塞超过35秒而响应未知；未重试，独立核查last_run.started_at精确匹配、任务正在运行。结束后同一运行finished_at与日志1个开始/1个完成、汇总逐项一致，调度回到等待。

| 结果 | 数量 |
| --- | --- |
| 成功写入 | 10 |
| 未变化跳过 | 89 |
| 提交失败 | 0 |
| 待处理 | 8 |
| 未匹配 | 5 |

最终status为pending、paused=false、source_errors=[]。8条pending均无last_attempt_at/last_error，已达到原每轮10次额度，留待后续既有调度；不主动补第二轮、不提高预算或绕过未匹配检查，也未人工恢复验证锁/冷却。

有界500行SSE取本次71条记录：0 ERROR、3 WARNING（2条观察陈旧提示、1条匹配未命中），未见ASS格式/权限错误。保留超时前片段，SSE流不关闭不影响已确认结果。

同步中小宇宙按原逻辑自动续期并写回配置，故整体配置hash变化；新引用、原连接/CSRF、10次预算/间隔/阈值/cron均复核保持，8个运行源码hash仍一致。不把配置hash变化误报成ASS引用丢失，也不宣称全部凭据逐项未变。没有更换Read Key或修改浏览器Profile/采集规则/自动开关，正常同步通知沿原配置，不额外发测试消息。

脱敏回执与install/cutover/sync.once保留于受限本机临时目录；包含连接引用的阶段文件和临时生产操作脚本收尾删除。仅保存布尔、计数、时刻、源码hash，不保存Cookie/Key/UID、完整配置/页面/日志。
