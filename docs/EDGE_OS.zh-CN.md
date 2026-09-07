# RiverBank Edge OS 镜像与升级路线

## 定义与当前状态

**RiverBank Edge OS** 是 RiverBank 为 RiverBank Edge 整机维护、验证、签名并发布的设备操作系统。它可以使用 Linux 内核、Debian、Raspberry Pi 固件和开源软件作为上游；“自己的系统”指 RiverBank 掌握整机镜像、硬件适配、安全策略、升级、恢复、版本和支持责任，不要求从零重写内核。

当前 `v0.26.4 beta` 的分发模式是 `managed-linux-system-layer`：先安装受支持的 Debian/Raspberry Pi OS，再部署 RiverBank 服务、配置和资源。它已经是统一版本和封存的 Edge OS 软件栈，但**目前还不是可直接写入 SD/NVMe 的独立镜像**。

内部版本清单继续使用历史键 `edge_system`，以兼容现有工具和已部署设备；对外名称、产物 ID、文件名和新标签统一使用 `RiverBank Edge OS`、`riverbank-edge-os` 与 `edge-os/`。

## 能不能直接安装

可以。完成镜像流水线后，用户或工厂可使用 Raspberry Pi Imager、balenaEtcher 或 RiverBank 刷写器，把一个签名的 `.img.xz` 文件直接写入 SD 卡或 NVMe，首次开机后通过手机扫码完成 Wi-Fi、设备命名、管理员账号和模型密钥配置。

正式镜像产物命名为：

```text
RiverBank-Edge-OS-0.26.4-beta-rpi5-arm64.img.xz
RiverBank-Edge-OS-0.26.4-beta-rpi5-arm64.img.xz.sha256
RiverBank-Edge-OS-0.26.4-beta-rpi5-arm64.manifest.json
RiverBank-Edge-OS-0.26.4-beta-rpi5-arm64.spdx.json
RiverBank-Edge-OS-0.26.4-beta-rpi5-arm64.sig
```

`.img.xz` 是可烧录镜像，`manifest.json` 描述硬件兼容、分区、版本和构建来源，SPDX 文件是软件物料清单，`.sig` 是 RiverBank 发布签名。源码候选包与可烧录镜像必须在名称和下载页面中明确区分。

## 推荐实现路线

### 阶段 A：受管系统层（当前）

- 继续支持在指定的 64 位 Raspberry Pi OS/Debian 基线上安装；
- 将 RiverBank 服务、模型接口、硬件规则和 UI 作为一个 Edge OS 版本封存；
- 保留 SHA-256 漂移检测、服务自检和一键恢复；
- 固定并记录内核、固件、HailoRT、PipeWire、Wayland 和关键系统包的兼容范围。

这一阶段适合快速研发，但基础系统仍可能受人工安装差异影响。

### 阶段 B：可复现的工厂镜像

近期建议基于 Raspberry Pi 官方 `pi-gen` 或 Debian 镜像工具生成 arm64 镜像，不急于迁移 Yocto。构建必须在干净环境中从锁定清单生成，不从某台开发机直接克隆磁盘。

镜像流水线应完成：

1. 固定上游快照、内核、固件和软件包版本；
2. 安装 RiverBank 服务、系统用户、权限、udev、systemd、Wayland、音视频和 Hailo 支持；
3. 清除构建机密钥、SSH Host Key、日志、账号和设备身份；
4. 生成每台设备首次启动时唯一的身份与密钥；
5. 运行离线测试、启动测试、硬件在环测试和升级/回滚测试；
6. 生成镜像、哈希、SBOM、来源证明并由发布密钥签名。

这样即可形成“像 Raspberry Pi OS 一样可直接烧录”的 RiverBank Edge OS。

### 阶段 C：设备级原子 OTA

量产前建议采用 A/B 系统槽，而不是在线覆盖正在运行的根文件系统：

```text
boot/firmware  引导文件、兼容清单
root-a         当前只读或受校验的 Edge OS
root-b         下一版本安装与验证槽
data           用户账号、Chat、报告、音乐、工坊应用和设备配置
recovery       恢复工具与出厂重置入口
```

设备先把新版本写入非活动槽，验证签名、型号、硬件 Revision、磁盘空间和数据迁移后再切换启动槽。启动健康检查失败时自动回到旧槽。更新系统不得擦除 `data`，用户私有数据不上传到公司 OTA 服务。

### 阶段 D：更深的发行版控制（按需）

当硬件型号固定、设备数量扩大且需要更小攻击面时，再评估 Yocto。Yocto 能精确控制每个包、许可证、启动时间和只读根文件系统，但会显著增加 BSP、内核、CVE 和构建维护成本。Buildroot 更适合功能单一的嵌入式设备，不适合当前包含桌面 UI、Python、音视频、AI 工具和用户工坊的丰富运行环境。

现阶段建议顺序是：

```text
Raspberry Pi OS/Debian 受管层
        ↓
pi-gen/Debian 可复现整机镜像
        ↓
A/B 原子 OTA + 恢复分区
        ↓
需要时再迁移 Yocto/BSP
```

## 首次启动与隐私边界

可烧录版本首次启动进入未配置状态，只开放本机蓝牙或临时配网页面。手机扫码传入 Wi-Fi、设备名、首个管理员账号和可选模型密钥后，临时入口立即关闭。

- 用户账号、Chat、报告、附件和工坊数据只保存在设备 `data` 分区；
- 用户创建或导入的工坊应用属于用户行为，不进入 RiverBank GitHub 仓库、Edge OS 镜像、发布清单或 OTA；
- 公司服务器只保存 OTA 所需的设备型号、硬件 Revision、匿名升级状态和签名清单，除非用户主动开启云服务；
- 每台设备拥有独立设备证书和可撤销更新资格，不共享万能配对密钥；
- 出厂重置默认清除 `data` 和设备密钥，不影响恢复镜像；
- 社区、账号云服务和付费服务与本地设备账号分域，不得成为设备离线运行的强制依赖。

## 安全与维护要求

- 所有系统镜像和 OTA 清单必须使用离线根密钥或 KMS/HSM 管理的发布密钥签名；
- 设备内只保存公钥，签名校验失败时拒绝安装；
- 根文件系统优先只读并使用 dm-verity 或等价完整性保护，运行数据写入独立分区；
- Hailo、摄像头、麦克风、工坊和网络服务继续按最小权限隔离；
- 保留串口/本地恢复路径，但生产设备默认关闭密码 SSH 和开发调试入口；
- 公开第三方许可证、版权通知和对应源码获取方式，履行 Linux 与 Debian 包的开源许可证义务；
- 每个 `stable` 镜像都要有明确支持周期、CVE 修复策略和可回滚的数据库迁移。

## 版本与发布边界

- 硬件：`RiverBank Edge G1 · Rev A`；
- 设备操作系统：`RiverBank Edge OS v0.26.4 beta`；
- 上游基础：按设备实际报告显示 Debian/Raspberry Pi OS、Linux 内核和固件版本；
- 手机和桌面客户端：继续使用独立版本，只通过兼容清单与 Edge OS 对齐；
- 发布标签：`edge-os/v0.26.4-beta`；
- 发布列车：记录一组联合验收的 Edge OS 与客户端版本，不是一个安装包。

从 `managed-linux-system-layer` 切换为 `flashable-system-image` 属于分发能力升级。只有镜像可复现构建、签名、首次启动、升级、回滚、恢复和真实硬件验收全部通过后，清单才可以宣告后者。

## 近期落地清单

1. 锁定支持的 Raspberry Pi 5、显示屏、摄像头、六麦、Hailo 与 NVMe 硬件矩阵；
2. 建立 `image/` 工程和可复现构建容器，产出开发镜像；
3. 将当前部署脚本拆成构建期配置与首次启动配置；
4. 设计 A/B + data + recovery 分区和断电恢复测试；
5. 建立设备身份、镜像签名、OTA 签名和密钥轮换体系；
6. 在真实设备上验证从空盘烧录、扫码开通、升级失败回滚和出厂重置；
7. 通过后再发布首个标记为 `flashable-system-image` 的 Edge OS beta。
