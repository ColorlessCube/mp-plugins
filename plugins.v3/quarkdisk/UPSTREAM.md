# 来源、许可与修改说明

本目录是 **ColorlessCube 维护的 QuarkDisk 兼容分支**，不是 xijin285 的上游发布，不表示原作者为本适配背书。插件 metadata 的作者字段保留原作者 `xijin285`；适配职责由本仓库维护者承担。

## 精确来源

- 上游仓库：<https://github.com/xijin285/MoviePilot-Plugins>
- 固定提交：`18dbf2d752de2f7729d06a532125c6a2d9d24a5b`
- 原版本：`QuarkDisk 1.1.0`
- 原文件：`plugins.v2/quarkdisk/__init__.py`、`plugins.v2/quarkdisk/quark_api.py`
- 通过 GitHub Contents API 按固定提交获取，不包含任何运行实例的配置、账号或私有数据。

原始文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `__init__.py` | `6950bddd8928d39be85f8633e0a7a15129b7cfeae1b6895fc5debef9d3298bff` |
| `quark_api.py` | `2bd3c1c2567ae1e38f23db67f95b20d3fea3130bdf0716644c89a5ecf0d5fe01` |
| 上游根 `LICENSE` | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |

## 许可处理

该历史提交的根 `LICENSE` 为完整 **GNU GPL version 3** 正文，而上游 README 曾声称 MIT，二者存在冲突。本兼容候选不将其改标为 MIT，不据 README 推定宽松授权；保守保留原完整 GPLv3 正文于本目录 `LICENSE`，保留作者及来源，并公开对应修改源码。最终对外发布由仓库维护者审核决定，本文不是对许可冲突的法律裁决。

本目录的许可说明仅作用于这份派生插件，不重新许可本仓库其他插件或根目录代码。未添加第三方依赖或另行嵌入其他插件的实现。

## 兼容修改（2026-11-09）

- 在 `plugins.v3/quarkdisk` 独立提供 `QuarkDisk 2.0.0`，保留 class/插件 ID、`quarkdisk_`、`夸克网盘` 存储 type/name、`enabled`/`cookie` 字段及宿主按类名划分的私有数据身份；不迁入 VueQuarkDisk，不清空认证。
- 两处 `schemas` 导入改为明确 `app.schemas`。
- 共用 API 请求和继承的两处 OSS 数据传输改用宿主 `RequestUtils`，显式 TLS 校验、关闭重定向、释放响应并限制错误输出；OSS 数据仍为原有单次发送，不扩展上传业务。
- 严格查询不调用吞异常的旧 `get_item`/`_path_to_id`：逐级验证全分页、显式成功状态、文件项与总数；未知状态抛 `StorageQueryError`。保留旧非严格方法供历史入口使用。
- 补充 V3 `FileItemQueryResult` 回执、五参快照及完整元数据对账；深度外保留旧基线，失败返回 `None`，确认不存在才为空快照。目录时间不作为跳过嵌套扫描的证据。
- 接入统一 `storage_manage` 的容量与整理方式；认证配置仍由插件页管理，`save_config`/`reset_config` 明确失败、不改数据；不新增扫码登录或云端写操作。复制从能力声明中移除，原 `copy` 返回 `False` 的行为保留。
- 下载接受既有临时目录和新文件路径，验证文件名、逐级拒绝父目录符号链接、独占临时文件并原子无覆盖发布；使用宿主 `RequestUtils`、显式 TLS 校验和禁止重定向，按宿主路径取消与进度接口处理，失败仅清理本次临时文件。
- 新增独立 `package.v3.json`，不改动 V2 索引和其他插件。

## 定向正确性修复（2026-11-10，第 1 轮）

- 容量解析只记录固定错误和异常类别，拒绝通过转换异常输出原响应。
- 旧 size 标量快照保留未扫描子树，迁为仅含 size 的字典；新扫描文件缺 mtime 时显式标记未知、不给 `SnapshotStore.save` 传 None，不伪造时间。
- 严格目录读取按字段存在性/None 选取时间，拒绝 False、空字典和空列表等畸形值，避免误认不存在。
- 共用请求仅对只读方法有界重试；无幂等凭据写请求回执未知时不重放。
- 下载使用 Linux O_TMPFILE 匿名 inode，procfs 描述符硬链接无覆盖发布；失败仅 close，不按暂存名字 unlink。观察到发布目录/文件替换则失败且不删除外来对象。
- 下载现在对缺少 O_TMPFILE/procfs/硬链接的平台或文件系统安全失败；V3 Path 返回值依赖可信、协作的父目录命名空间，不承诺抵御同 UID 在最终核对后的任意替换。完整边界见 README。
- 原两组独立反例、positive controls、固定宿主源码/校验值已移植为可移植 Quark 前缀回归；未修改上游指纹或 GPL 正文。

## 对照宿主

核对官方 `jxxghp/MoviePilot` tag `v3.2.5`、提交 `4ed5fa079897e4b02615abb01b78efc1d1272cae` 的：

- `app/chain/storage.py`、`app/runtime/extensions/module/contracts.py`
- `app/modules/filemanager/module.py`、`app/modules/filemanager/storages/__init__.py`
- `app/modules/filemanager/transhandler.py`、`app/chain/transfer/plan.py`
- `app/schemas/file.py`、`app/schemas/types.py`、`app/runtime/stop.py`、`app/runtime/progress.py`
- `app/adapters/network/http.py`、`app/runtime/compat/manifest.py`

这是接口与离线行为兼容候选，不是对真实云端读写的验收证明。详见本目录 README 的验证边界。
