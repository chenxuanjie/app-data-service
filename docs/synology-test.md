# 群晖测试部署与验收

2026-10-21：本地完整 Docker 验证和折日测试 APK 已生成；API 数据服务 0.2.0 更新包及配套 Compose 已重新构建，尚未部署到真实 NAS。
本轮在隔离测试账本验证固定支出、物品与分类的离线及双向同步；不迁移现有折日数据，也不连接旧 WebDAV 数据。

## 交付文件

交付文件直接放在 `release/` 根目录，使用三个简短名称；校验报告集中放在metadata子目录：

| 文件 | 用途 |
| --- | --- |
| fastapi-0.2.0.tar.gz | API与启动前迁移工具，后续业务更新主要替换它 |
| postgresql-17.11.tar.gz | PostgreSQL 17.11；首次安装或数据库版本升级时导入 |
| powersync-1.27.0.tar.gz | PowerSync 1.27.0；首次安装或同步服务版本升级时导入 |
| docker-compose.yml | 三服务完整配置；内含随机密码及私钥，私下保存 |
| metadata/release.json / checksums.sha256 | 每个分包的标签、镜像ID、大小、SHA-256及来源记录 |
| metadata/validation.json | 从分包重新导入后的完整本地链路验证，DSM为严格TLS替身 |
| README.md | 本文的随包副本 |

fastapi-0.2.0中的0.2.0是本项目API服务版本；FastAPI框架依赖自身为0.148.0，两者独立。
后续升级后只保留当前交付记录对应的一套包，清理校验值匹配的旧版本包。

每个tar.gz都是一个可独立导入的完整Docker镜像，不能当成三个文件分卷，也不需要先合并。
同一镜像解压后与原包的镜像ID一致，配置中的密码、密钥和端口保留。
本次以服务端分支 `feature/multi-entity-sync` 的 revision `9b95241` 工作区源码构建 API 0.2.0；源码含未提交改动，镜像标签保留 `dirty`：
`app-data-service:0.2.0-9b95241-dirty-20261021112414`。PostgreSQL 与 PowerSync 镜像标签保持
`postgres:17.11-bookworm`、`journeyapps/powersync-service:1.27.0`。
配套 Compose 已切换 API 镜像并内嵌当前 PowerSync 规则，其他部署凭据与连接设置沿用原配置。
`metadata/release.json` 的 `updatedArchives` 仅列 `fastapi-0.2.0.tar.gz`，`realNasDeployed` 为 false。
旧的一体TAR、reports和冗余交付目录已清理。包名使用英文组件名和软件版本；镜像架构、基础系统变体、完整构建标签及摘要记录在metadata中。
在release根目录执行 `sha256sum -c metadata/checksums.sha256` 可核对三个文件。

折日 APK 位于 `C:/Study/MyCode/MyProject/zheri/release/zheri-0.11.0+60.apk`。
本次仅生成本地文件，没有上传到 WebDAV 或更新远程 OTA。

## 环境与部署

用户已指定参照 board-game-agent。复用环境为 linux/amd64、DSM
`https://cznas.pp.ua:5001`、NAS 内网 `192.168.1.55`。
当前数据服务项目目录按用户截图使用 `/volume1/docker/PostgreSQL`，不沿用其他工程的数据卷路径。
请求地址保留 HTTPS，API 容器通过 extra_hosts 映射 DSM 域名至内网地址，仍验证证书。
身份命名空间固定为历史地址 `https://cznas.dpdns.org:5001`，不要跟随访问域名修改。

data子目录是否已创建、目录权限和18280/18281端口尚未远程核实；操作前检查。

1. 在 NAS 手动创建 `/volume1/docker/PostgreSQL/data`。
   File Station中进入docker共享文件夹 → PostgreSQL → 新建data文件夹，大小写须一致。
   Container Manager的项目目录选 `/volume1/docker/PostgreSQL`，数据库数据单独放在data子目录；
   不把项目根目录直接挂给数据库，因为其中可能已有docker-compose.yml等文件。
   这是新的空目录；不要指向旧数据库、桌游或折日文件目录。配置禁止自动创建宿主目录，路径错误会明确失败。
2. 在 Container Manager 的镜像页依次导入三个 `.tar.gz`，确认上述三个标签都存在。
   Docker引擎支持直接加载gzip包；若当前DSM界面的文件选择器不接受该后缀，先解压出各自的`.tar`再导入，
   或在NAS终端逐个执行 `docker load -i <分包路径.tar.gz>`。无需重新拉取镜像。
3. 新建项目 `app-data-service-test`，使用配套 `docker-compose.yml`。
   YAML 自包含，不需要 `.env`、源码目录或 NAS Python；不要重新生成 YAML 覆盖这一份随机密码和签名密钥。
4. 启动项目。预期postgres、api、powersync三个服务全部运行且健康。
   API容器先执行数据库迁移，成功后直接运行API；升级过程及失败原因查看API日志。
   首次初始化需要一些时间，不删除数据目录重试。
5. 在 DSM 反向代理增加来源 `HTTPS cznas.pp.ua:18281`，
   目的地 `HTTP 127.0.0.1:18280`。来源 HTTPS 与 Docker 的宿主 HTTP 端口分开，避免监听冲突。
   配套 YAML 已使用 `APP_DATA_ISSUER=https://cznas.pp.ua:18281`、
   `APP_DATA_SYNC_ENDPOINT=https://cznas.pp.ua:18281/powersync/`，不要改身份命名空间或签名密钥。
6. 使用匹配该域名的证书，从手机网络访问 `https://cznas.pp.ua:18281/health`，应返回健康状态。
   `/health` 只证明 API 和数据库可用，不代表 DSM 登录或复制链路通过。需要时开放对应路由/NAS防火墙端口。

常驻内存上限为PostgreSQL 512 MiB、API 384 MiB、PowerSync 512 MiB；迁移复用API容器，不额外启动容器。
这些是容器上限，不是实测 NAS 内存消耗；初次验证观察 DSM 资源与容器日志。
用户截图确认NAS内核不支持CPU CFS配额，群晖配置不设置cpus/cpu_quota/cpu_period，
由宿主机正常调度CPU；内存限制与PowerSync进程内存上限保留。
数据库和 PowerSync 不直接发布宿主端口。不需要给 Flutter 提供数据库密码。
DSM 用户需要可调用 FileStation.Info 的权限，通常为 File Station 使用权限；无需提供管理员账号。
若现有账号启用 OTP，在测试登录页输入当前验证码。

## 启动时报绑定源目录不存在

如果日志包含 `invalid mount config for type "bind": bind source path does not exist`，
说明Docker在创建容器前就找不到宿主机源目录，数据库尚未启动。
项目目录与数据库数据目录是两个设置，选择项目目录不会自动建立YAML中指定的绑定源。

1. 检查项目中的YAML，postgres.volumes的source应为 `/volume1/docker/PostgreSQL/data`；
   target保留 `/var/lib/postgresql/data`。不要更改已有密码、私钥和身份配置。
2. 在File Station创建上面的空data子目录，使用当前配套YAML更新原项目并重新构建。
   使用在线编辑器时需同步修改NAS上的YAML；本机文件改动不会自动传到NAS。
3. 不需要重新导入三个镜像，不需要删除项目、数据目录或修改数据库密码。
4. 预期postgres、api、powersync均健康且运行，再执行下方同步验收。

此次用户截图显示创建阶段因源目录不存在而失败；尚未证明真实DSM登录和NAS完整运行通过。

## 启动时报NanoCPUs或CPU CFS不支持

如果创建容器时提示 `NanoCPUs can not be set`、`kernel does not support CPU CFS`，
说明当前NAS内核不支持Docker请求的CPU配额。`cpus: 1.0`不是服务运行所必需，
也不能改成cpu_quota/cpu_period解决，因为这些同样依赖CFS配额能力。

本项目的群晖配置及生成器不设置cpus；新版只包含postgres、api、powersync三个服务。
更新NAS项目中的YAML后重新构建；只修复postgres会让其他服务后续出现相同错误。
不需要重新导入镜像、删除数据或修改NAS内核。内存上限继续保留，CPU用量按实际运行观察。

## 旧项目显示“仅部分正在运行”

旧版把数据库迁移单列为migrate服务，成功退出也会让群晖把项目显示为部分运行。
新版将迁移放进API的启动命令，成功后替换为持续运行的API进程，失败则停止API启动。
项目只保留postgres、api、powersync三个持续运行服务，正常时应显示全部运行。

对已有NAS项目只更新配套YAML并重新构建，不需要重新导入三个镜像。
如果旧的 `postgresql-database-migrate-1` 仍留在容器列表，确认新API已健康后，
仅删除这个已停止的旧迁移容器；它没有数据库数据挂载。不要删除数据库容器、数据目录或共享的API镜像。
不要为消除灰色状态而把已完成的任务改成长期空转进程。

迁移连接来自私有YAML，迁移子进程使用app_migrator；API业务仍使用app_runtime。
启动成功后从API主进程环境清除迁移连接变量，Docker配置本身仍由可信NAS管理员管理。

## Web 登录提示无法连接：核对浏览器来源

2026-10-21 已确认：当前折日 Web 来源为 `http://127.0.0.1:8082`，
NAS API 对这个来源的登录预检返回 400 `Disallowed CORS origin`；
对原有 `http://127.0.0.1:8080` 返回 200。其他端同账号可登录。
这是浏览器来源白名单不匹配，`/health` 可访问不能证明浏览器登录预检通过。

本地交付 `release/docker-compose.yml` 的 `services.api.environment` 已改为：

```yaml
APP_DATA_ALLOWED_ORIGINS: http://127.0.0.1:8080,http://localhost:8080,http://127.0.0.1:8082
```

1. 在 Container Manager 中编辑现有项目 YAML，只合并上面这一项，保留 NAS 上其他配置。
   若 NAS 配置与本地交付完全一致，也可使用更新后的配套 YAML 替换。
2. 保存后重新构建项目，使 API 容器按新环境变量重新创建；仅重启旧容器不会更新环境变量。
   不需要导入新镜像、重新构建 Web 或删除数据目录。
3. 保持 Web 地址为 `http://127.0.0.1:8082/`，刷新后重试登录。
   用下面的无凭据预检确认 NAS 已使用新配置：

```powershell
curl.exe --noproxy "*" -i -X OPTIONS "https://cznas.pp.ua:18281/v1/auth/login" -H "Origin: http://127.0.0.1:8082" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type"
```

预期状态 200，包含 `access-control-allow-origin: http://127.0.0.1:8082`。
本次只修改本地交付配置，尚未部署到 NAS；远端更新后的预检、登录及同步仍需验收。
本地使用交付环境配置验证登录与同步路由的预检：三个列明来源返回 200，未列明来源返回 400；
ruff 检查、格式检查及真实 PostgreSQL 上的完整 86 项测试通过。
8080/8082 是这份配置当前允许的来源，并非折日固定端口约定；换地址时按实际来源维护白名单，
协议、主机和端口均须匹配，不改为通配符。

## 用全新测试数据验收

安装新 APK，进入设置 → **群晖同步测试**，服务地址填上面配置后的 HTTPS 来源地址。
先用账号 A 在两台设备登录，再用不同账号 B 验证隔离。初次应显示空的“同步测试账本”。
同一 DSM UID 对应同一服务器测试数据集；本地库另按服务地址、账号和数据集分区。
若需要更换测试服务 URL，本地将使用另一个作用域，请先处理旧地址的未上传修改。

| 操作 | 应看到的结果 |
| --- | --- |
| A 设备 1 新增“同步测试-手机套餐”，59.10 元/月 | 本地立即保存，联网后 A 设备 2 出现同条记录 |
| 设备 1 暂停同步或断网，连续改金额与备注，关闭重开 | 离线仍可读写，数据及待上传队列保留 |
| 恢复联网/同步 | 两端最终一致，待上传状态消退，不产生重复记录 |
| 两端都暂停，编辑同条记录为不同内容，再恢复 | 服务器保留冲突候选，在冲突入口可查看双方并选择；选择后两端一致 |
| 同步完成后删除记录 | 两端均消失；服务端保留软删除版本 |
| B 登录（单独设备，或 A 退出后切换） | 看不到 A 的记录；B 新增后 A 也看不到 |
| A 有未上传修改时退出，再登录 B | A 数据被隐藏；B 看不到 A 的记录或队列 |
| 再登录 A | 恢复 A 的本地记录和未上传更改并继续同步 |
| 已登录后断网，或访问令牌过期 | 缓存仍可离线使用；需要验证身份时只影响同步 |
| 重启 API 和 PowerSync，重建 PostgreSQL 容器但保留同一目录 | 服务恢复后记录仍在；已有会话可继续使用 |

主动退出使 API 会话立即撤销，已签发的 PowerSync 令牌最多还有五分钟有效期；客户端同时断开同步。
Web 刷新令牌仅在内存，刷新页面后需重新登录恢复同步，持久离线数据不因此清空。
客户端测试入口只实现固定支出名称、金额、周期、备注的编辑；原业务其他页面尚未接入。

遇到问题记录：客户端版本、设备/系统、服务来源地址、操作顺序、脱敏日志及两端内容。
不要发送密码、OTP、刷新令牌或完整 Compose。首版按用户要求没有备份功能；
测试只用可丢弃的新数据。删除容器时保留挂载目录，不使用删除数据的清理操作。

真实 Android、Windows、Web 三端均完成验收后才进入正式账本迁移；
Windows 安装包和浏览器实际运行尚待后续验证，本轮不能据此切换正式数据。

## 后续更新方式

- 首次安装：导入三个分包，再用同目录Compose新建项目。
- 只更新API业务：仍使用同一个release目录，传输当前版本的 `fastapi-<API版本>.tar.gz`、匹配 Compose 和必要说明即可。
  本地保留未变化的PostgreSQL和PowerSync分包，不复制第二套目录；metadata/release.json的updatedArchives列出本次更新文件。
  NAS已有的两个依赖镜像标签须与记录一致，无需重复传输。
- 导入新的API分包后，用配套Compose更新原项目并重建api，启动时自动执行迁移；
  用界面确认迁移退出0、api健康及powersync恢复。不要勾选删除数据库数据或重建空目录。
- 保留已有部署的密码、签名密钥、身份命名空间与数据目录。开发机必须使用相同私有Compose生成更新包；
  NAS端若已自行调整配置，更新时只合并对应image标签和必要迁移，不能用另一套随机凭据覆盖。
- 数据库/PowerSync版本升级单独验证后交付对应分包，不将数据库跨主版本升级作为普通API更新处理。

## 本地复现

只重新压缩当前已验证的镜像，不重新构建：

```powershell
.venv\Scripts\python tools/build_docker_bundle.py --compose release/docker-compose.yml --reuse-existing release/metadata/release.json --docker wsl --dart C:/Users/win10/tools/flutter/bin/dart.bat
```

后续构建新API并只交付API更新包（Docker已有匹配的两个依赖镜像）：

```powershell
.venv\Scripts\python tools/build_docker_bundle.py --compose release/docker-compose.yml --api-only --docker wsl --dart C:/Users/win10/tools/flutter/bin/dart.bat
```

不传`--reuse-existing`会构建当前源码；不传`--api-only`交付完整三个分包。
工具逐包压缩、核对标签/架构/镜像ID、实际docker load，再验证完整本地同步链路，全部成功后替换交付文件。
失败不替换已有交付。验证器仅清理自己创建的随机容器/卷/网络，不访问NAS；不能替代真实部署验收。
