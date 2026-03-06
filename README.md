# Build 脚本

## 项目简介

本目录提供 **spacemit robot sdk 仓库的统一构建与环境脚本**，用于从源码编译 CMake 包与 ROS2 包、选择目标配置、安装依赖及生成部署目录。

- **做什么**：提供 `envsetup.sh`（环境与快捷命令）、`build.sh`（实际构建逻辑）、以及依赖与目标配置解析；支持全量构建、按类型（CMake/ROS2）构建、单包构建与 rootfs 部署。
- **解决什么问题**：统一入口与布局（output/staging、output/rootfs、target 配置），避免各组件各自写构建脚本；通过 `target/*.json` 选择启用包与选项，通过 `lunch` / `m` / `mm` 快速完成选型与编译。

## 功能特性

| 类别       | 支持                                                                 |
| ---------- | -------------------------------------------------------------------- |
| 构建类型   | CMake 包、ROS2（ament_cmake/ament_python）包；全量 / 仅 CMake / 仅 ROS2 |
| 命令       | `lunch` 选目标，`m` 全量或分类型构建，`mm` 单包构建，`build.sh` 直接调用 |
| 目标配置   | `target/*.json` 指定启用包、包选项、并行数等                          |
| 输出布局   | `output/staging` 安装前缀，`output/rootfs` 部署目录（deploy-rootfs） |
| 依赖       | 系统依赖检查与可选自动安装（apt），包级依赖解析                       |
| Python 环境 | `m_env_build`、`python_env_build.sh` 为应用构建虚拟环境          |

| 类别       | 不支持 / 说明                                                       |
| ---------- | -------------------------------------------------------------------- |
| 其他构建系统 | 仅支持 CMake 与 ROS2 colcon，不负责其他语言/框架的构建               |
| 交叉编译   | 脚本默认本机构建；交叉编译需自行设置工具链与环境                     |

## 环境准备

### 基础要求

- **Shell**：Bash 或 Zsh（脚本兼容两者）。
- **系统**：推荐 Bianbu 2.2 以上、Ubuntu 24.04 以上。
- **必需**：`jq` 用于解析 `target/*.json`。

### 必须依赖

构建前请确保已安装编译工具链与 CMake：

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config jq
```

### ROS2 依赖

若目标包含 ROS2 包（`middleware/ros2/`、`application/ros2/`），需先安装 ROS2 并加载环境，具体安装方式待补充。

## 如何编译

### 完整编译

在**仓库根目录**执行。两种方式等价，`m` 为快捷命令，`build.sh` 适合脚本或 CI。

**方式一：快捷 m 命令**

```bash
# 1. 加载环境与快捷命令
source build/envsetup.sh

# 2. 选择目标（交互菜单或直接指定）
lunch
# 或：lunch k3-com260-minimal

# 3. 全量构建
m                    # CMake + ROS2
m -C                 # 仅 CMake 包
m -R                 # 仅 ROS2 包
m -j8                # 指定 8 并行任务
```

**方式二：build.sh**

```bash
# 需先 lunch 或设置 BUILD_TARGET
source build/envsetup.sh
lunch k3-com260-minimal

# 全量 / 分类型构建
./build/build.sh all        # CMake + ROS2，构建完自动 deploy-rootfs
./build/build.sh cmake      # 仅 CMake 包（同 m -C）
./build/build.sh ros2       # 仅 ROS2 包（同 m -R）

# 选项
./build/build.sh -j8 all    # 指定 8 并行
./build/build.sh -v all     # 详细输出到终端

# 脚本/CI 场景：用环境变量指定目标，可省去 lunch
BUILD_TARGET=k3-com260-minimal ./build/build.sh all

# 清理与部署
./build/build.sh clean all          # 清理全部
./build/build.sh clean cmake        # 仅清理 CMake
./build/build.sh deploy-rootfs      # 从 staging 生成 rootfs
```

### 单组件编译

**方式一：快捷 mm 命令**（需先 `source envsetup.sh` 且 `lunch`，在包目录内执行）

```bash
source build/envsetup.sh
lunch k3-com260-minimal
cd components/peripherals/lidar
mm
# 传递 CMake 参数（仅对 CMake 包生效）
mm -DBUILD_STREAM_DEMO=ON
mm -- -DOPT1=ON -DOPT2=OFF
```

**方式二：build.sh package**（支持相对路径，无需绝对路径）

```bash
source build/envsetup.sh
lunch k3-com260-minimal

# 从仓库根目录
./build/build.sh package components/peripherals/lidar
./build/build.sh package components/peripherals/lidar clean

# 已 cd 进包目录时，可用 .
cd components/peripherals/lidar
./build/build.sh package .
```

`./build/build.sh help` 可查看完整子命令与选项。

## 如何运行

### 运行非 ROS2 应用

构建产物在 `output/staging`。运行前需 `source build/envsetup.sh`（PATH 与 LD_LIBRARY_PATH 已包含 staging）：

```bash
test_lidar_uart YDLIDAR /dev/ttyUSB0 230400   # 示例：test_lidar_uart测试用例（staging/bin 已在 PATH 中）
```


### 运行 ROS2 应用

需先加载 ROS2 与项目环境：

```bash
sros2_setup
ros2 run peripherals_lidar_node lidar_2d_node   # 示例：2D 雷达 ROS2 节点
```

## 详细使用

- 环境变量说明、`build.sh` 子命令、`target/*.json` 字段、依赖与 Python 环境等详见 **官方文档**（链接待补充）。

## 常见问题

待补充

## 版本与发布

本目录为仓库构建脚本，无独立版本号，随仓库一起发布；

| 版本       | 说明 |
| ---------- | ---- |
| 随仓库发布 | 构建与环境脚本（envsetup、build.sh、target 配置等）随仓库版本发布。|

## 贡献方式

欢迎参与贡献：提交 Issue 反馈问题，或通过 Pull Request 提交代码。

### 提交流程

1. **Fork 并克隆仓库**（若为托管平台协作流程）
2. **创建分支**：`git checkout -b fix/xxx` 或 `git checkout -b feat/xxx`
3. **修改代码**，完成后执行 `shellcheck build/*.sh` 等检查
4. **提交**：
   ```bash
   git add <修改的文件>
   git commit -m "build(readme): 简短描述本次修改"
   git push origin <分支名>
   ```
5. **创建 Pull Request**：在托管平台从个人分支向主仓库目标分支发起 MR/PR

### 提交规范

- **编码规范**：本目录 Shell 脚本遵循 [Google Shell 风格指南](https://google.github.io/styleguide/shellguide.html)，请按该规范编写与修改代码。
- **commit message**：遵循 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) 规范，格式为 `<type>(<scope>): <description>`，如 `build(xxx): fix envsetup for zsh`、`docs(readme): add build.sh usage`。
- **提交前检查**：请在提交前运行本仓库的脚本检查，确保通过风格检查：
  ```bash
  # 在仓库根目录执行（检查 build 目录下所有 .sh）
  shellcheck build/*.sh
  ```
  若未安装 `shellcheck`，可先执行：`sudo apt install shellcheck`（或见 [ShellCheck](https://github.com/koalaman/shellcheck) 安装说明）。

## License

本目录源码文件头声明为 Apache-2.0，最终以本目录 `LICENSE` 文件为准。