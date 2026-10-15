# 豆瓣 / 网易云 ASS 只读凭据

适用于 `TraktRatingsSync` 3.21.1（3.21.0 起支持 ASS）。仅改变豆瓣与网易云的 Cookie 来源，不改变 Trakt PKCE、
微信读书 Skill Key、小宇宙刷新令牌、历史缓存、匹配、共享写入额度或调度入口。
升级默认仍使用原有手动 Cookie；初始化、保存配置、打开页面不读取 ASS、不调用网站、不触发同步。

## 权限与 bootstrap

由 ASS 管理员另行签发**专用消费者 Read Key**：

- 仅 `rest`；scope 为 `secrets:metadata:read`、`secrets:value:read`。
- `secret_ids` 精准绑定实际需要的 `douban`、`netease` **现有条目 UUID**，不授予整个租户；
  `allowed_secret_names` 留空（它是采集 Write Key 的授权字段，不能用于 Read Key）。
  本适配器只支持这两个固定名称，拒绝有效授权条目数超过 2 的 Key。
  名称对应的条目删除/重建后不自动扩大授权，必须重新审查。
- 默认有效期 90 天；只有用户明确批准长期有效时才显式设置 `expires_at=null`（永不过期，直至撤销）。
- 不授予 `secrets:write`，不复用 Chrome 采集 Key / producer；设置合理有效期与读额度。
  同一受信插件可使用仅授权这两项的一个 Read Key，浏览器各 Profile 的采集 Key 仍独立。

Read Key 仅放在 MoviePilot 进程可读的文件中，配置页面只填路径，不填或回显 Key。
文件是 ASCII token，允许一个末尾换行；不含 `Bearer ` 前缀。不在聊天、命令参数、日志或工单中传递内容。

- 绝对路径，普通文件，末级不能是符号链接；owner 为进程用户或 root。
- 建议 `0400`，也接受 `0600`；拒绝 group/other 权限和执行位。
- 容器应单文件**只读挂载**到例如 `/run/secrets/ass-cookie-read-key`；示例路径不含真实凭据。
  生产挂载配置修改、Key 签发/替换仍是单独获准的操作，本插件不会修改 Compose 或签发 Key。
- 不写入 Git、插件配置、导出、通知或 Cookie 缓存。每次操作重新打开文件，轮换无需旧 Key 回退。

## 配置

在「ASS 只读凭据连接」填写：

| 字段 | 含义 |
| --- | --- |
| `ass_origin` | 直连 HTTPS origin，例如 `https://ass.example.com:18443`；无认证信息、路径、query 或 fragment |
| `ass_key_file` | MoviePilot **容器内**受保护文件的绝对路径 |
| `ass_tenant_id` | 凭据所属租户 UUID |
| `ass_max_age` | 观察陈旧提示阈值，默认 3600 秒，60–86400；3.21.1 起超过阈值仅提示，不阻断网站登录检查。保留原字段和值，无需重配 |

豆瓣和网易云各自填写：

| 字段 | 含义 |
| --- | --- |
| `<site>_cookie_source` | `manual`（默认）或 `ass`，其中 site 为 `douban` / `netease` |
| `<site>_ass_secret_id` | 固定名称条目的 UUID，不能只按名称追随删除后重建的条目 |
| `<site>_ass_site_id` | 实际策略的 site_id，默认 `douban` / `netease`，必须核对 |
| `<site>_ass_account_id` | 实际策略的 account_id，必填，不假设为 main |

这些标签只证明匹配已配置的 ASS 条目，**不能证明网站实际登录账号**。切换账户或条目前暂停调度并重新验收；
插件不会自动迁移、清空或分账户重建原有业务缓存、待处理与成功记录。
选择 ASS 后不读取或回退手动 Cookie；既有手动字段保留，只有明确切回手动才使用。
从 ASS 读取的明文绝不写回这些字段。

连接须使用直连地址，不使用会跳转到其他主机的入口。ASS 和凭据网站请求都显式校验 TLS、禁止重定向，
使用独立 Session，不继承环境代理或 `.netrc`；不存在忽略证书的选项。网络出口必须支持直连。
网站登录重定向会被拒绝/识别为未通过检查，不跟随跳转发送 Cookie。

## 固定消费范围与 v1/v2

适配器自包含，不导入 ASS 后端，不需要新依赖。固定消费契约如下：

- 豆瓣 source_origins 仅 `https://www.douban.com`；消费 `douban.com` 根路径 `/` 的 **domain** `dbcl2`（必需）、`bid`（可选）。
  原生 `dbcl2` 的平衡外层双引号保留。快照中的同域根路径 `ck` 经过结构验证但不沿用，仍从豆瓣首页刷新 CSRF。
  已选 domain Cookie 适用于已有的 www/movie/book/music 豆瓣操作；不将 host-only 凭据扩大到子域。
- 网易云 source_origins 仅 `https://music.163.com`；消费 `music.163.com` 根路径 `/` 的 **domain** `MUSIC_U`（必需）与 `__csrf`（可选）。
- 每一个 Cookie 都检查结构、名称、域、路径、长度、请求头字符、store `0`、session/expiry 一致性；
  不接受 partitionKey、混合 store、未知字段或超出上述固定范围的 Cookie。不同策略范围需要另行评审，不能自动放宽。
- 完整快照最多 16 KiB UTF-8 / 100 个 Cookie；严格 JSON，不接受重复字段、非有限数或 bool/string 版本。
  v1 的身份是 name + 规范域 + path，v2 再加 hostOnly；同一物理身份重复始终拒绝。
- 按本地配置固定账户与网站来源，要求 captured、观察不早于生成时间，生成/观察时间不能超前本机 60 秒；
  未初始化、invalid、过期必需 Cookie 或不可用 Key 均阻断。生成时间不是网站签发时间。
- **观察时间过旧不等于 Cookie 失效。** 3.21.1 起，超过 `ass_max_age` 只在日志（每轮每站一次）与详情页提示，
  不将来源标记失败、不发送 Cookie 失效通知；仍读取 ASS 当前版本，并由网站检查登录。没有增大阈值、
  重标快照时间、改采集策略或启用本地 Cookie 缓存。浏览器重新刷新观察后，下次成功读取会清除提示。
  这允许浏览器离线时继续同步，但无法仅凭浏览器离线推断退出登录；网站撤销或 ASS 明确 invalid 后停止。
  消费者不能读取仅向采集 Write Key 开放的策略接口，本次也不改变后端对采集上传的新鲜度校验。

### 网易云 `__csrf`

`netease_ass_csrf_scope`：

- `reject_ambiguous`（默认）：无 `__csrf` 时按已有 API 行为使用空 CSRF，单一未过期 `__csrf` 可用；
  两个作用域同时存在时拒绝，即使值相同也不选 first/last。
- `host`：只选择 host-only。
- `domain`：只选择 domain。

显式指定后该作用域必须存在且未过期，否则阻断，不回退另一个作用域或空值。
**不能仅凭浏览器采集成功、值长度相同或 Chrome Cookie 的排列顺序判断正确作用域。**
必须在另外获准的只读网站账号/历史检查中确定后再保存；不要删除真实网站 Cookie 消除歧义。
手动 Cookie/cURL 模式保留原有解析优先级，但重复值和冲突警告不再回显具体值。

## 操作与故障边界

每次 ASS 读取执行 access-status → metadata → value → metadata 四次鉴权 GET。
固定 token 内 Key ID、租户、条目 UUID、名称和前后 value version；观察到变化立即拒绝，不重新请求猜测结果。
TLS、HTTP 状态、JSON 和响应分块检查有超时、整体期限及 20 KiB 响应上限，失败只暴露固定错误分类。

- 豆瓣初始化登录检查、每次凭据搜索、每个待提交目标都重新授权读取。
  ASS 模式复用原首页 GET，同时要求 HTTP 200、已登录页面标记及有效 CSRF。
  当前首页以 `#db-global-nav .nav-user-account` 内 HTTPS 豆瓣站内退出控件证明已登录；
  若旧页面提供脚本 `_USER_ID`，必须与本次 `dbcl2` 用户标识一致，冲突不得回退导航判断。
  仅取得 `ck`、正文中的退出/个人主页链接或观察时间新鲜均不是已登录证明。
  导航只证明登录，不宣称独立确认数字账号；ASS 固定条目身份仍必检，账号标记与 Cookie 仅内存比较。
  明确匿名、账号不符、登录重定向、401/403 仍暂停，确认正常登录并刷新 ASS 后需人工恢复；
  网络异常、5xx、无法识别登录标记或缺少 CSRF 只停本轮，下次定时重查，不制造永久人工验证锁。
  一个目标的首页登录/CSRF 检查 → 已有内容保护 GET → POST 复用同一快照，下一目标重读。
  多平台仍共用原来的额度、间隔和 pending；初始化与额外 CSRF GET 不计为成功写入。
- 网易云账户 UID 与近期播放/专辑查询属于一次操作，共用一次新鲜读取。
  操作结束清空 Cookie，不跨轮复用 Helper。
- ASS 故障/失效/撤销阻断后续相关操作，不从磁盘或手动 Cookie 回退。
  豆瓣凭据失败停止本轮后续豆瓣请求；仅网易云读取失败跳过该来源，其他来源继续。
  既有已确认目标仍遵循原队列处理规则，不因来源失败删除或提前标记成功。
- 已完成授权读取后正在发送的关联请求无法追溯撤销；不承诺撤销立即终止已发出的请求。
- CAPTCHA 暂停不因快照、Cookie、Key 或观察刷新解除，切入 ASS 也保留暂停。
  必须先在浏览器验证，再人工点击已有恢复按钮；429 冷却不能绕过。
- 通知和暂停的 ASS 指纹只基于固定身份引用，不使用 Cookie、Key 或快照内容。
  ASS 故障与网站授权故障分开提示；日志/通知不包含 Cookie、CSRF、正文或底层异常文本。

## 验收与发布

实施和生产分别验收，不能把离线测试或 Chrome 上传成功当作插件业务成功：

1. 本地运行下面的离线测试（全部外部请求必须 mock）。
2. 获得单独授权后再签发 Read Key、只读挂载并安装预期版本；先保持调度关闭。
3. 另外获准的只读检查确认 ASS 条目绑定、真实网站账号及网易云 CSRF 作用域；不提交豆瓣状态。
4. 再获明确小范围业务写入授权，限制共享额度验收，检查实际写入/失败/队列；通过后才启用定时。

不要用 `run()` / 调度任务做只读连接测试，它会处理既有 pending 并可能写入豆瓣。
无明文诊断 API/MCP、通用 CLI 或任意 URL 执行器。返回手动模式必须由管理员明确选择，不能作为自动故障恢复。

从插件仓库目录运行：

```bash
conda run -n movie-pilot pytest tests/test_traktratingssync_*.py
conda run -n movie-pilot pytest
```

从 MoviePilot 项目根目录运行：

```bash
conda run -n movie-pilot pylint local-plugins/mp-plugins/plugins/traktratingssync/ass_cookie_helper.py local-plugins/mp-plugins/plugins/traktratingssync/douban_helper.py local-plugins/mp-plugins/plugins/traktratingssync/netease_helper.py local-plugins/mp-plugins/plugins/traktratingssync/__init__.py --errors-only
```

测试包含 v1/v2、同值/异值双作用域、顺序无关、固定引用/版本竞态、陈旧观察仍可用、Cookie 过期/失效、
匿名 CSRF/账号不符拒绝、网站登录通过后处理 pending、临时登录检查失败下一轮恢复、
bootstrap 权限/轮换、TLS/跳转/限长/异常脱敏、验证码暂停、完整运行阻断及 Helper 清理。
所有测试使用合成 Key/Cookie 和临时文件，禁止真实出站或真实配置/数据库写入。

3.21.1 的本地验证、网站只读证据、生产升级与单次同步结果见
[观察陈旧修正记录](../../docs/releases/traktratingssync-3.21.1-20261015.md)。
