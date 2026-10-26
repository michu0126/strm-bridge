# STRM Bridge

直接添加 **115、百度网盘、阿里云盘、123 云盘、天翼云盘**，浏览云端文件夹并生成媒体库 STRM。内置网盘接口，不需要 OpenList、CD2、FUSE 或宿主机网盘挂载。也支持原有本地文件夹模式。

## 功能

- “我的网盘”管理多个账号，支持连接测试、编辑授权和移除；源目录选择器同时显示网盘账号和本地目录。递归生成并保留目录结构。
- 电影 / 电视剧：复制或移动外挂字幕，以影片文件名为前缀，保留语言及 forced 等标签。支持季集编号匹配和 VobSub `.idx + .sub` 配对。匹配不唯一时提示并跳过。
- JAV：可选清理**单文件小于 100,000,000 字节**的所有类型文件，默认关闭。包含视频广告、字幕、图片、压缩包、可执行文件、STRM、元数据及无扩展名文件；待清理的小视频不生成 STRM。恰好 100MB 及以上的文件保留。清理只作用于直接含有视频的目录，同步移入源目录 `.strm-trash/<plan-id>/`；任务记录可恢复。
- 支持 70 多种常见及旧式视频扩展名，大小写不限，可自行补充；字幕支持 SRT、ASS、SSA、VTT、SUP、IDX、SUB、SMI、TTML 等。具体列表在界面“支持格式”中展示。
- 同名视频容器不会互相覆盖；已有不同内容的 STRM / 字幕报告冲突，保留原文件。
- 预览后文件发生变化则拒绝处理；清理前若生成失败，本次剩余清理停止。
- 后台执行、取消、任务记录、重启中断提示；不自动全盘扫描或自动删除。
- 可选目录监控：在生成页面选择源目录和目标媒体库，点击“启用监控”。默认每 5 分钟递归检查所选目录，可设置 1 分钟至 24 小时，支持暂停、立即检查和移除，设置在重启后保留。云端使用网盘目录 API 定期检查，本地模式兼容没有文件变更事件的 FUSE 挂载。
- 外观设置：8 组配色（松石绿、海盐蓝、暮光紫、蔷薇粉、琥珀金、青瓷色、星夜靛、石墨灰），每组均支持深色、浅色、跟随系统；实时调整面板透明度。背景可选 Bing 每日壁纸、手动上传或渐变。Bing 图片按日缓存在 NAS，上传图片持久保存于 `/data/appearance`，支持 JPG/PNG/WebP/GIF/AVIF，最大 10MB。SB 字母标识与配色联动；主题、颜色、透明度和背景选择自动保存在当前浏览器。
- 监控只补充不存在的 STRM 和字幕，字幕一律复制，不自动移动、清理、覆盖或删除文件。最近 1 分钟内修改的文件延后到下一轮，挂载暂时不可用时记录错误并重试。新增和失败任务进入任务记录；无变化不会不断生成空任务。
- 独立管理密码、HttpOnly 会话、跨站请求检查、路径范围校验、带 HMAC 的长期播放入口。
- Python 标准库实现，无第三方运行依赖。Dockerfile 支持 amd64 / arm64 构建；发布标签的平台以镜像清单为准。

## 添加网盘

进入“我的网盘 → 添加网盘”，选择类型并填写授权，点击“保存并连接”。连接成功后选择云端文件夹，再选择 NAS 媒体库目录。

| 网盘 | 授权方式 | 自动续期 |
| --- | --- | --- |
| 115 | 默认 Cookie / 内置扫码登录；可选开放平台 Token | Cookie 失效后重新扫码；开放平台 Refresh Token 自动轮换 |
| 百度网盘 | 默认从 OpenList 授权页获取 Refresh Token，无需自建应用 | 通过 OpenList 官方授权服务续期；可选自有应用 |
| 阿里云盘 | 默认从 OpenList 授权页扫码获取 Refresh Token，无需自建应用 | 通过 OpenList 官方授权服务续期；可选自有应用 |
| 123 云盘 | 默认手机号 / 邮箱和密码；可选网页登录令牌或开放平台 | 账号密码可重新登录；开放平台可自动续期 |
| 天翼云盘 | `cloud.189.cn` 网页登录 Cookie | Cookie 失效后在“编辑”中更新 |

接入流程参考 OpenList 官方文档：[115 Cookie](https://doc.oplist.org/guide/drivers/115)、[百度授权](https://doc.oplist.org/guide/drivers/baidu)、[阿里扫码授权](https://doc.oplist.org/guide/drivers/aliyundrive_open)、[123 账号登录](https://doc.oplist.org/guide/drivers/123)、[天翼 Cookie](https://doc.oplist.org/guide/drivers/189)。并非所有网盘都使用 Cookie；界面按网盘提供相应登录方式。

115：粘贴 Cookie（包含 UID、CID、SEID），或者点“扫码登录”，在 115 App 扫码确认后保存。二维码可刷新；登录同类型客户端可能使原网页登录下线。支持粘贴带 `Cookie:` 前缀和末尾分号的请求头，工具自动整理格式。Cookie 失效后编辑更新。

百度、阿里：点击“打开授权页面”，在 `api.oplist.org` 选择对应网盘并使用 OpenList 提供的参数，授权后复制 Refresh Token。选择此登录方式代表使用 OpenList 官方授权服务 `api.oplist.org` 续期，刷新令牌将发送至该服务；列目录、下载链接和文件操作仍直接请求网盘接口，**无需运行 OpenList 容器或挂载目录**。自有开放平台方式直接请求服务商，默认不展示开发者密钥字段。阿里网页版 refresh_token 不适用于开放平台授权。

123：填写手机号或邮箱和密码，或者从官网请求头 Authorization 复制网页登录令牌。遇到短信验证等账号风控，需要在官网手动完成验证；工具不代过验证码。开放平台模式仍可选择。

起始目录可留默认值。阿里 Drive ID 留空使用资源库。保存时密码类字段留空表示保留原值；切换登录方式会清除不兼容的旧授权。凭据保存在 `/data/cloud-accounts.json`，账号查询接口隐藏 Cookie、密码和令牌，STRM 不包含这些凭据。扫码取得的 Cookie 仅用于当前添加表单，保存后由服务器管理。

## 播放与字幕

```text
媒体库 .strm → STRM Bridge /play/... → 内置网盘接口 → 当前下载地址的 HTTP 302
```

云端源自动使用直连模式，不需要在“连接设置”选择 OpenList 或 CD2。请将“STRM 服务地址”设置为播放器可访问的地址。播放时重新获取网盘链接，STRM 不保存短期链接。扫描和生成只读取文件列表，**不会下载视频正片**；外挂字幕按需下载到媒体库并匹配改名，单份上限 512MB。

内网和外网共用同一份 STRM 时，填写两边都可访问的域名地址，内网 IP 不能用于外网直连。使用现有反向代理时，只需将该域名的 `/play/` 路径原样转发到 STRM Bridge，管理页面和 `/api/` 不必开放。更改服务地址后，需要更新已有 STRM 中的地址。115 直链按播放器实际 User-Agent 获取；未发送该请求头时也保留原样，避免 Emby 读取影片时因请求标识变化而收到 403。

网盘的会员、应用权限、下载额度、API 频率限制仍然适用；添加账号成功不等于一定具有视频下载权限。百度可能要求播放器使用 `User-Agent: pan.baidu.com`；123 网页下载接口可能要求 Referer。普通 302 无法替播放器设置这些请求头。需要额外请求头时明确提示，可选择对应网盘开放平台模式；工具不代理视频流。115 Cookie 接口也可能受网盘的会员或风控策略限制。媒体服务器是否转码或再次代理，由媒体服务器和播放器决定。

### Emby 影片请求也走 302

部分播放器使用 Emby 的 `original.mp4` / `stream` 接口，此时仅让 STRM 返回 302，Emby 仍可能自行下载并中转视频。工具内置了可选的 Emby 影片转发，无需再部署容器：

1. 在“连接设置 → Emby 直链播放”填写 Emby 的内网服务地址，例如 `http://NAS-IP:8097/emby`，保存。
2. 在现有反向代理中增加一条子规则，把域名下的 `/emby/videos`、`/emby/Videos`、`/videos`、`/Videos` 前缀转发到 `http://STRM-IP:18642/emby-play`。使用会去掉匹配前缀的反向代理时，后端也须保留 `/emby-play`。其他 Emby 请求继续转发到原 Emby 后端。
3. 播放器内外网都使用这个 Emby 域名。工具使用播放器已有的 Emby 登录信息检查影片访问权限；本工具生成的 STRM 原片请求会直接 302 到网盘，按播放器实际 User-Agent 获取链接。不新增或保存 Emby API 密钥。

本地影片、其他来源的 STRM、字幕和明确请求转码的播放保持 Emby 原有处理方式。“查看播放状态”显示本次启动后的直连、中转和错误次数。直接访问原 Emby 内网端口会绕过该反向代理，播放仍可能由 Emby 中转。

云端 JAV 清理也包含小于 100MB 的所有文件类型，默认关闭。只处理直接包含视频的目录，移动到所选云端目录下的 `.strm-trash/<任务>/<文件标识>/`，不永久删除；“任务记录”可恢复。云端字幕选择“移动”时，成功下载后也将原字幕移到该回收目录。恢复遇到原位置同名文件时停止，不覆盖。自动监控一律只新增、复制字幕，不移动或清理源文件。

本地文件夹仍可使用“挂载路径”直接生成 STRM；媒体服务器需要能访问相同路径。本地目录的可选 302 服务兼容 OpenList / AList、HTTP 地址模板、CD2，属于旧部署兼容功能，**内置网盘不依赖这些连接**。路径默认自动对应，特殊子目录可在折叠的高级设置中调整，无需编辑 JSON。

## 群晖项目部署

1. 在 NAS 建立项目目录（例如 `/volume1/docker/strm-bridge`），将仓库 `compose.yaml` 复制为 `docker-compose.yml`，复制 `.env.example` 为 `.env`。
2. 在 `.env` 填写 `LIBRARY_DIR`（NAS 的 Emby 媒体库绝对路径）、`PUBLIC_URL`（例如 `http://NAS-IP:18642`）。`HOST_PORT` 默认 18642，部署前检查端口冲突。
3. Container Manager → **项目 → 新增**，项目名 `strm-bridge`，路径选择上述项目目录，使用已有 Compose 文件并启动。
4. 访问 `http://NAS-IP:18642`。默认管理密码在项目 `data/admin-password.txt`，家庭局域网可在 `.env` 设置 `AUTH_DISABLED=1`。
5. 添加网盘，选择云端目录与媒体库目录，扫描预览后生成；需要自动入库时启用目录监控。

后续在项目中修改 YAML、环境变量或卷映射，再重新构建/启动项目。命令行等价操作：

```sh
cd /你的项目目录
docker compose -p strm-bridge pull
docker compose -p strm-bridge up -d
```

默认 Compose **只挂载应用数据和媒体库，不挂载网盘目录**。媒体库路径留在 `.env` 中填写，端口配置不绑定某个本地 IP。添加多个媒体库时增加卷并更新 `TARGET_ROOTS`。继续使用已有本地源时，可按 Compose 注释增加 `/cloud` 卷和 `SOURCE_ROOTS`。

已有安装迁移到项目时，复用原 `/data` 和目标目录路径；先备份项目文件与数据，再停止旧容器并释放同名容器和端口。不要删除数据目录。`/data` 包含网盘授权、任务、监控、播放记录和签名密钥，丢失它会使已有播放链接失效。源目录与媒体库不可互相嵌套。目标文件系统需支持硬链接（群晖 Btrfs / ext4 支持）。

本地源码构建：`docker build -t michu0126/strm-bridge:local .`，然后在项目 Compose 中选择此标签。

希望直接修改 NAS 上的源码时，可把仓库文件放进项目的 `source` 文件夹，在原项目的 `volumes` 下增加 `./source/bridge:/app/bridge:ro` 和 `./source/static:/app/static:ro`。修改 `source` 中的 Python 或页面文件后重启该项目即可，数据仍保存在原来的 `data` 文件夹。

## 接口依据与验证范围

网盘适配器使用 Python 标准库，没有内嵌或启动 OpenList 程序。接口字段核对参考：[115 开放接口 SDK](https://github.com/OpenListTeam/115-sdk-go)、[百度网盘开放平台](https://pan.baidu.com/union/doc/)、[阿里云盘开放平台](https://www.alipan.com/developer)、[123 开放平台](https://www.123pan.com/developer)、[OpenList 驱动的协议使用](https://github.com/OpenListTeam/OpenList/tree/main/drivers)。115 加密传输格式适配自 MIT 许可的 [SheltonZhu/115driver](https://github.com/SheltonZhu/115driver)，声明保存在 `bridge/licenses/115driver-LICENSE.txt`，随镜像发布。

自动测试覆盖五种接口的请求参数、分页、授权续期和错误处理，以及无挂载源的生成、字幕、清理恢复和监控。协议测试使用模拟网盘响应；每个真实账号仍需在界面测试连接与实际播放器验证。不会把模拟测试当成五家真实网盘播放已验证。

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

本工具只操作你选择的网盘账号目录和本地允许目录。网盘接口变化或授权到期会显示错误，不把扫描失败当作空目录。
