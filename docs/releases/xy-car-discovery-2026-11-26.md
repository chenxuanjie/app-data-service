# XY-CAR 车辆发现配套记录

日期：2026-11-26。配套候选标签：`xy-car-discovery-2026-11-26-rc.1`。

## 功能源码与配套版本

| 组件 | 功能源码提交 |
| --- | --- |
| app_data_service，声明版本 0.6.2 | `9ba1a0730bb9dc1381c7219ebc520a952f6c68e9` |
| xy-car-ros2 | `79f1cda07362adff0345ffadfa17589bf0e484eb` |
| xy_car_app，声明版本 1.0.1+3 | `92abbc6dcac4488f257151ad1d6aecd234ab0774` |

API 已发布镜像为 `ghcr.io/chenxuanjie/app-data-service:sha-9ba1a0730bb9dc1381c7219ebc520a952f6c68e9`，摘要为 `sha256:5d0764a1ea30bbaf9d2792310b8ab679f9bfb0be02d30a9554b7256d3c189acb`。现场确认现有 NAS API 容器的 revision 标签为上述 API 提交，继续复用原 PostgreSQL、PowerSync 与业务数据。

本记录不修改程序、数据库迁移或镜像，不改变既有功能提交与构建产物的来源关系。

## 已运行架构

- 在现有 app_data_service 中提供 `/v1/vehicles`、`/v1/agent/ws` 和 `/v1/client/ws`。
- 内存车辆注册表由一个 API 进程维护。镜像启动配置固定单 worker 与 `websockets-sansio`；现场 Compose 的启动命令覆盖镜像 CMD，运行单个默认 worker，WebSocket 已实际验证可用。
- Flutter Web 的 `/api/v1/` 由独立 Web Nginx 转发到本服务；Web 镜像不包含另一套 FastAPI。
- 车辆发现接口不接入账号绑定或 device_secret；既有账号、站点数据与同步接口继续沿用原模块契约。
- DSM 的 `18281` 和 Web 的 `8090` 反代已启用 WebSocket 转发头。

## 验证记录

- 功能提交阶段 ruff 检查通过，完整 pytest 为 403 项通过。
- NAS `/v1/vehicles` 及两处 Web 的同源列表接口均返回 `xycar_01` 在线与当前 STA IP。
- 实车经公网 WSS 报到，客户端按 vehicle_id 连接后收到实车 `pong`，RTT 约 45–47 ms。
- 暂停实车网关 17 秒后查询为离线；恢复进程后自动重连并上线。协议配置为 5 秒心跳、15 秒离线阈值。

本次只验证一台实车；多车实机隔离、Wi-Fi 切换及长期运行尚未验收。视频由板端 FFmpeg 与群晖 MediaMTX 承担，API 只转发视频控制报文。
