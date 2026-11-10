# QuarkDisk：MoviePilot V3.2.5 兼容候选

基于 xijin285 的 QuarkDisk 1.1.0 做的独立适配。版本为 **2.0.0**，保留原作者信息，但不是上游官方版本。来源、修改与 GPLv3 许可处理见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

## 安装与身份

- 仅从本仓库 `package.v3.json` / `plugins.v3/quarkdisk` 安装到 **MoviePilot V3**，不安装到 V2。
- 最低版本元数据为 `>=3.2.5,<4`，实际核对宿主为官方 `v3.2.5`；未宣称所有 V3 版本均已验证。
- 插件 ID/class 仍为 `QuarkDisk`；配置前缀 `quarkdisk_`；存储 type/name 仍为 `夸克网盘`。
- 沿用插件 `enabled`、`cookie` 字段、插件配置和私有数据身份。不会自动切换到 `VueQuarkDisk`，不会迁移或清空认证。不要同时启用两个接管同一存储类型的插件。
- 不附带账号、Cookie 或运行实例数据。`release: false` 保持宿主从源码安装的策略；GitHub Release 源码归档供固定版本核验，不改变索引安装方式。安装和生产验收属于单独步骤。

## V3 行为

### 严格查询

`QuarkApi.get_item_strict` 逐级获取目录，严格校验成功状态、条目结构和分页总数，遍历到总数一致且无重复才确认未命中。认证、超时、异常、缺少总数、条目损坏、重复/截断分页均抛宿主 `StorageQueryError`，不返回“确认不存在”。根路径也通过实际列表确认。该插件同时声明 `get_file_item_strict` 并返回官方 `FileItemQueryResult`。

总数缺失或目录在分页间变化会保守失败，请重试；上游 API 未提供事务快照令牌，无法保证并发修改中的目录具备原子一致性。旧非严格查询方法保留其历史容错行为，不应被当成严格删除/覆盖凭据。

### 快照

签名与 V3 链一致：

```python
snapshot_storage(storage, path, last_snapshot_time=None, max_depth=5, previous_snapshot=None)
```

返回 `path -> {size, fileid, type, modify_time?}`，不包含目录。已遍历目录中确认消失的文件/子树会从旧快照移除；达到深度上限的现存子目录保留其旧基线；输入旧快照不被修改。旧版 `path -> size` 基线迁为仅含 `size` 的字典，不捏造未扫描文件的时间或 ID。本轮已扫描但缺少上游 mtime 的文件省略 `modify_time` 并标记 `mtime_unknown: true`，而不是写 `None`、当前时间或假 mtime；宿主 `SnapshotStore.save` 可按其缺省游标规则持久化。旧字典中显式 `modify_time: None` 同样规范化。缺少时间时只能依赖大小/路径及可用文件 ID 的变化，不能证明同大小、同 ID 的内容未变。任一严格读取故障返回 `None`，由宿主保留旧基线；仅确认根路径不存在或完整空目录才返回 `{}`。

`last_snapshot_time` 为增量游标：已实际核对、时间早于游标且元数据完全相同的文件可复用旧元数据；夸克目录 mtime 未证明会随所有嵌套变化推进，因此不会凭它跳过目录读取。`max_depth` 根目录为 0。首次快照无旧基线时，深度外文件不可知，不伪造完整覆盖。官方快照 ABI 无进度/取消回调参数，本插件不臆造额外关键字。

### 存储管理

| 动作 | 行为 |
| --- | --- |
| `usage` | 使用既有容量读取，返回标准 `success/data`；故障明确失败，不伪造零容量 |
| `support_transtype` | 返回 `data.transtype = {"move": "移动"}` |
| `save_config` / `reset_config` | 明确失败并提示在 QuarkDisk 插件页维护认证，绝不覆盖/清空旧 Cookie |
| 登录与其他动作 | 不支持，明确失败；非本插件存储返回 `None` 供宿主继续路由 |

上游 `copy` 本就未实现，本候选仍返回 `False`，不声明网盘内复制能力。浏览、上传、删除、重命名、移动、创建目录等旧实现保留，但本次没有真实云端读写验收，不能理解为它们已全部验证。原 `StorageOperSelection` 事件入口保留。

### 本地下载

既有目录会被解释为“目录 + 已验证远端文件名”；非目录路径表示完整的新文件路径。**空参数每次在宿主 `TEMP_PATH` 下独占创建一个 `0700` 的 `.quark-download-<随机标识>` 目录，目录内保留真实远端文件名与扩展名**，不会复用未经验证的旧缓存。重复请求、并发请求和不同远端目录的同名文件各自独立；随机目录名已存在（含符号链接）时保守失败，不进入或复用它。显式路径的已有目标仍一律拒绝，不因默认路径变化而开放覆盖。路径回退、不安全远端文件名、父目录符号链接和任何已有目标（含目录/悬空链接）均拒绝，不覆盖不相关文件。下载使用目标文件系统内的 **Linux `O_TMPFILE` 匿名 inode**；全部内容、大小和取消检查通过后，通过 `/proc/self/fd/<fd>` 绑定已打开 inode，原子硬链接到新目标。不使用需要额外 capability 的 `AT_EMPTY_PATH`。失败只关闭描述符，**不对任何暂存名字执行 unlink**，因此没有“身份检查后清理却删到旁文件”的窗口。

官方 V3.2.5 `storage.download` 返回延迟读取的 `FileResponse`，`storage.image` 读取文件内容后返回 `image/jpeg` 响应；两者都不删除下载结果。插件不提前移除返回的文件、不创建后台清理线程，也不清理其他请求或宿主临时文件。默认结果与失败可能留下的空私有目录交由宿主现有 `clear_temp` / `SystemUtils.clear` 回收：启动清理按 `TEMP_FILE_DAYS` 递归删除过期文件，再删除空目录；不是插件承诺的实时或周期回收。应按磁盘容量维护宿主保留策略，长时间未启动清理时结果会累积。显式目录仍由调用者管理，真实 `TransHandler` 的 `TemporaryDirectory` 会在移动完成后按其上下文退出清理。

发布后逐级以 `O_NOFOLLOW` 重新打开目标目录、核对目录和文件 inode。观察到目录/文件被替换则返回 `None`，不按路径回滚删除；已完整发布的本次 inode 可能留在被移走的目录内，现存外来文件保持不动。此检查不是命名空间锁：V3 下载 ABI 只返回 `Path`，无法向后续消费者传递持有的 inode/目录句柄；若同 UID 或其他有权限的进程在最终核对后、返回后或宿主使用前改名/替换父目录或文件，任何基于 `Path` 的插件都不能提供绝对路径身份保证。因此要求目标目录及祖先命名空间在发布至宿主消费期间由可信协作进程管理；对同 UID 任意恶意篡改不作保证。需要更强合同必须先修改宿主消费者，而不是再加一次 stat。

下载通过宿主 `RequestUtils`，显式校验证书、禁止重定向；非 `quark.cn` 子域下载地址不携带账号 Cookie。按宿主 `runtime_stop_state.consume_transfer_stop(源路径)` 消费取消，通过 `ProgressHelper` 报进度；失败不会写 100% 完成。安全落地需要 **Linux、可用 procfs、`dir_fd` / `O_NOFOLLOW`、支持 `O_TMPFILE` 和硬链接的目标文件系统**。不支持的文件系统/挂载、macOS 和 Windows 返回失败，不回退到存在竞态的命名暂存。部署前应在可丢弃的同类持久存储目录验证这些文件系统原语，并单独验证媒体目标路径的 ACL、挂载选项与消费流程；文件系统类型相同不等同于端到端兼容。

共用云盘 API 的只读方法保留有界重试；POST 等无幂等凭据的请求只发送一次。超时后的写回执仍未知，不自动追发；调用者应先人工核对云端状态，不应将失败理解为肯定未执行。容量解析异常仅记录固定错误和异常类别，不输出原响应字段。

## 离线验证

在满足上述文件系统要求的隔离 Linux 测试环境、仓库根目录，使用已具备 pytest 和宿主依赖的 Python（不在生产环境安装测试包）：

```bash
python -m pytest tests/test_quarkdisk_v3*.py -q
python -m pytest tests -q
```

新增独立反例保持原始断言与 positive controls；固定宿主原源码和 SHA-256 收录于 `tests/test_quarkdisk_v3_host_sources.py`，不依赖开发者绝对路径或联网取源码。严格回执与快照保存测试执行实际宿主 schema/消费者方法；HTTP 测试加载未改写的真实 `RequestUtils`、保留 requests 准备流程，只在末端 `HTTPAdapter.send` 回放。Linux 测试优先使用镜像真实 `httpx2`，仅开发机缺少它时为未使用的异步声明提供 httpx 别名。

用例只在 pytest 临时目录写入合成文件，宿主存储/配置/缓存均为隔离边界，HTTP 为明确回放替身；公共测试 fixture 禁止真实出站。测试不会使用真实账号、真实 Cookie、云盘或生产数据库，不发送通知，也不会上传、删除或移动真实媒体。

离线回归不等于真实登录、云端接口、限流行为、大文件上传、真实刮削或生产验收；这些必须在后续授权的隔离环境分别确认。旧上传/移动等业务没有在本次范围内重写或扩展；共用 API 和 OSS 数据请求仅迁移宿主传输边界，OSS 数据保留单次发送。不能把新增严格查询当成整套历史 API 的安全审计。
