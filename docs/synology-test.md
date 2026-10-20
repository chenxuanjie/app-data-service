# 群晖测试部署与验收

2026-10-20：本地完整 Docker 验证和折日测试 APK 已生成，尚未部署真实 NAS。
本轮只验证独立测试账本的固定支出，不迁移现有折日数据，也不连接旧 WebDAV 数据。

## 交付文件

在服务仓库 `release/` 中使用以下配套文件，不使用旧的仅 PostgreSQL 配置：

| 文件 | 用途 |
| --- | --- |
| app-data-service-linux-amd64-images.tar | 一包包含 API、PostgreSQL、PowerSync 三个 linux/amd64 镜像 |
| reports/docker-linux-amd64/docker-compose.yml | 完整四服务配置；内含随机密码及私钥，私下保存，不提交或公开 |
| reports/docker-linux-amd64/release.json | 镜像标签、来源树指纹、TAR 大小及 SHA-256 |
| reports/docker-linux-amd64/validation.json | 本地导入和真实同步链路验证结果；DSM 为严格 TLS 替身 |

TAR 大小为 1126362624 字节，SHA-256：
`a18413a092f70d88db58eafb59d99215db4fc8d62e7d6e438dffd204bcea8cb1`。
API 镜像为 `app-data-service:0.1.0-06f014f-dirty-20261020033655`；
其余为 `postgres:17.11-bookworm` 和 `journeyapps/powersync-service:1.27.0`。
构建时功能代码尚未提交，因此 dirty 标记属实；release.json 的 sourceTreeSha256 记录实际镜像源码，提交后不会修改已验证镜像。

折日 APK 位于 `C:/Study/MyCode/MyProject/zheri/release/zheri-0.10.22+57.apk`。
本次仅生成本地文件，没有上传到 WebDAV 或更新远程 OTA。

## 环境与部署

用户已指定参照 board-game-agent。复用环境为 linux/amd64、DSM
`https://cznas.pp.ua:5001`、NAS 内网 `192.168.1.55`、数据卷 `/volume2`。
请求地址保留 HTTPS，API 容器通过 extra_hosts 映射 DSM 域名至内网地址，仍验证证书。
身份命名空间固定为历史地址 `https://cznas.dpdns.org:5001`，不要跟随访问域名修改。

本次选择的新测试目录和 18280/18281 端口尚未远程核实或创建；操作前检查它们未被其他项目使用。

1. 在 NAS 手动创建 `/volume2/friend/apps/app_data_service_test/postgres`。
   这是新的空目录；不要指向旧数据库、桌游或折日文件目录。配置禁止自动创建宿主目录，路径错误会明确失败。
2. 在 Container Manager 的镜像页导入 TAR，确认上述三个镜像标签都存在。
   若 DSM 一次导入只显示一个镜像，使用 NAS 终端 `docker load -i <TAR路径>` 导入整包并确认三个标签，避免重新拉取。
3. 新建项目 `app-data-service-test`，使用配套 `docker-compose.yml`。
   YAML 自包含，不需要 `.env`、源码目录或 NAS Python；不要重新生成 YAML 覆盖这一份随机密码和签名密钥。
4. 启动项目。预期 postgres、api、powersync 为运行且健康，migrate 执行完成后退出码为 0；一次性迁移退出是正常状态。
   首次初始化需要一些时间。若失败，先查看 migrate/API/PowerSync 日志，不删除数据目录重试。
5. 在 DSM 反向代理增加来源 `HTTPS cznas.pp.ua:18281`，
   目的地 `HTTP 127.0.0.1:18280`。来源 HTTPS 与 Docker 的宿主 HTTP 端口分开，避免监听冲突。
   配套 YAML 已使用 `APP_DATA_ISSUER=https://cznas.pp.ua:18281`、
   `APP_DATA_SYNC_ENDPOINT=https://cznas.pp.ua:18281/powersync/`，不要改身份命名空间或签名密钥。
6. 使用匹配该域名的证书，从手机网络访问 `https://cznas.pp.ua:18281/health`，应返回健康状态。
   `/health` 只证明 API 和数据库可用，不代表 DSM 登录或复制链路通过。需要时开放对应路由/NAS防火墙端口。

常驻内存上限为 PostgreSQL 512 MiB、API 384 MiB、PowerSync 512 MiB；迁移期间另有 256 MiB。
这些是容器上限，不是实测 NAS 内存消耗；初次验证观察 DSM 资源与容器日志。
数据库和 PowerSync 不直接发布宿主端口。不需要给 Flutter 提供数据库密码。
DSM 用户需要可调用 FileStation.Info 的权限，通常为 File Station 使用权限；无需提供管理员账号。
若现有账号启用 OTP，在测试登录页输入当前验证码。

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

## 本地复现

使用同一份私有配置构建，不旋转现有凭据：

```powershell
.venv\Scripts\python tools/build_docker_bundle.py --help
.venv\Scripts\python tools/verify_stack.py --help
```

构建工具下载锁定 Linux wheels，使用本机 Docker 构建、导出、重新导入、验证后才替换 TAR。
验证器仅清理自己创建的随机测试容器/卷/网络，不访问 NAS；不能替代上述真实部署验收。
