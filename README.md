# STRM Bridge

把 NAS 上通过 CloudDrive2 挂载的网盘视频，生成到 Emby / Jellyfin / Plex 等媒体库的 STRM 文件。提供中文管理界面、目录选择器、执行预览、字幕整理与可恢复的小文件清理。

## 功能

- 浏览容器已挂载的网盘目录和媒体库目录，递归生成，保留目录结构。
- 电影 / 电视剧：复制或移动外挂字幕，以影片文件名为前缀，保留语言及 forced 等标签。支持季集编号匹配和 VobSub `.idx + .sub` 配对。匹配不唯一时提示并跳过。
- JAV：可选清理**单文件小于 100,000,000 字节**的其他文件，默认关闭。所有识别为视频的文件均保留，即使小于 100MB。清理只作用于直接含有视频的目录，同步移入源目录 `.strm-trash/<plan-id>/`；任务记录可恢复。
- 支持 70 多种常见及旧式视频扩展名，大小写不限，可自行补充；字幕支持 SRT、ASS、SSA、VTT、SUP、IDX、SUB、SMI、TTML 等。具体列表在界面高级选项中展示。
- 同名视频容器不会互相覆盖；已有不同内容的 STRM / 字幕报告冲突，保留原文件。
- 预览后文件发生变化则拒绝处理；清理前若生成失败，本次剩余清理停止。
- 后台执行、取消、任务记录、重启中断提示；不自动全盘扫描或自动删除。
- 独立管理密码、HttpOnly 会话、跨站请求检查、路径范围校验、带 HMAC 的长期播放入口。
- Python 标准库实现，无第三方运行依赖；Docker 镜像适用于 amd64 / arm64。

## 播放链路

```text
媒体库中的 .strm
  → http://NAS:18642/play/<id>/<signature>/<filename>
  → CD2 GetDownloadUrlPath(get_direct_url=true)
  → HTTP 302 Location: 当前网盘直链
```

STRM 不保存易过期的网盘链接，CD2 凭据不会写进 STRM。扫描和生成不读取视频内容，只有字幕会复制到媒体库。

先在“连接设置”填写 CD2 API 令牌，或填写 CD2 账号密码。API 令牌只需要目标范围的文件读取能力；目录路径需和所配置令牌根目录一致。CD2 对应云盘需允许直接下载链接。API 参考：[CD2 官方开发者指南](https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html)。

部分网盘直链要求固定 User-Agent 或 Cookie 等请求头。HTTP 302 无法替播放器附加 Cookie；遇到此类链接，工具会明确报错。需要特定 User-Agent 时，按错误提示配置播放器。不会将视频代理流伪装成直链。

媒体服务器也可能将远程视频重新代理，或因解码能力触发转码。是否客户端完全绕过 NAS，应在实际客户端验证，不能仅凭 STRM 或单个 302 响应判断。ISO / IMG 等可生成入口，但是否可播放由媒体服务器和播放器决定；本工具不转码，也不解析光盘菜单。

## NAS Docker 部署

1. 修改 `compose.yaml` 中源挂载路径、目标媒体库路径，以及 `PUBLIC_URL` / `CD2_URL`。端口默认 **18642**，部署前检查冲突。
2. `docker compose up -d --build`，或使用 Docker Hub 镜像 `michu0126/strm-bridge:latest`。
3. 浏览器访问 `http://NAS-IP:18642`。首次管理密码在应用数据目录 `data/admin-password.txt`，也可预先设置 `ADMIN_PASSWORD` 环境变量。
4. “连接设置”填写实际地址及路径映射。例如容器 `/cloud/115` 对应 CD2 `/115open`。
5. 选择媒体类型、源目录及媒体库目录，点击“扫描并预览”，核对字幕和清理清单后执行。
6. 在媒体服务器中扫描目标目录。用“测试真实 302 直链”先测试一个实际视频。

`/data` 必须持久保存，包含路径映射、CD2 凭据、任务记录、播放映射、签名密钥与管理密码。丢失 `/data` 会导致已生成的播放链接失效。不要把数据目录打包进镜像或上传仓库。

CD2 使用 FUSE 挂载时，宿主机必须能看见挂载后的文件；容器卷建议 `rslave` 传播。若 CD2 的挂载只存在于其容器命名空间，需要修正挂载传播或共享同一挂载命名空间。源目录需要可写才能移动字幕或回收小文件；只生成 STRM / 复制字幕时可只读挂载。目标文件系统需支持硬链接以保证完整写入且不覆盖已有文件，群晖本地 Btrfs / ext4 支持。

源挂载与媒体库不要互相嵌套，不要选到 `.strm-trash`、快照或媒体库自身作为源。路径模式会把容器内路径原样写入 STRM，需保证媒体服务器看到相同路径，或由你现有的媒体库 302 服务处理路径映射。

复制字幕保留源文件；移动字幕在写入完成、内容及源文件状态检查后移除源字幕。移动不是自动回收操作。清理功能只在 JAV 下提供；回收站不会自动永久清空。

## 发布镜像

GitHub Actions 执行测试后构建 `linux/amd64,linux/arm64`。在仓库中配置：

- Repository variable `DOCKERHUB_USERNAME`
- Repository secret `DOCKERHUB_TOKEN`（具有目标镜像推送权限）

手动运行 Publish 工作流，或推送 `v*` 标签，发布 `latest` 和版本标签。授权凭据由仓库 Secrets 管理，不写入源码。详见 [Docker 官方多架构发布说明](https://docs.docker.com/build/ci/github-actions/multi-platform/)。

## 本地开发与测试

```bash
python -m unittest discover -s tests -v
DATA_DIR=./data SOURCE_ROOTS='["/mnt/cloud"]' TARGET_ROOTS='["/mnt/library"]' python -m bridge.server
```

默认监听 `0.0.0.0:18642`，可通过 `BIND_HOST` 与 `PORT` 修改。管理入口建议只对家庭局域网开放；如需反向代理外网访问，使用 HTTPS 并让 `PUBLIC_URL` 与实际访问地址一致。

本工具只操作你选择并授权的挂载目录。不同网盘对于软删除、重命名、并发读取以及直链的具体行为，以网盘和 CD2 的实际实现为准。
