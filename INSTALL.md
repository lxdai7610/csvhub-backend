# Windows 一键安装

适用于 64 位 Windows 10 / Windows Server 2016 及更新系统。首次安装需要访问 Python 官网与 Python 包源；安装目录和配置的数据目录须可写。

1. 把项目完整解压到服务器目录，例如 `D:\CSVHub`。
2. 双击 `install.cmd`，等待出现 `Installation completed`。没有合适的 Python 时会自动下载并安装，有 Python 时直接复用。
3. 双击 `start.cmd`，浏览器会打开登录页面。本机地址默认为 `http://127.0.0.1:8000/`；其他电脑使用 `http://服务器实际IP:8000/`。
4. 首次管理员展开「管理员令牌登录」，使用数据目录中的 `api-token.txt` 登录，然后在系统设置里创建自定义账号和密码。默认数据目录为项目下的 `data`。
5. 后续用 `restart.cmd` 重启、`stop.cmd` 停止。重新安装或更新依赖前先停止服务，安装器会保留配置、数据库、账号、上传文件和原管理员令牌。

## 自动准备的内容

| 内容 | 处理方式 |
|---|---|
| Python | 检测标准 64 位 Python 3.12+；缺少时安装官方 Python 3.13.16，校验文件哈希与签名。自动安装使用当前 Windows 用户，并配置用户 PATH |
| 项目环境 | 创建或复用 `.venv`，无需手工激活；无效旧环境保留到 `.venv.backup-*` 后重建 |
| 运行依赖 | 安装 FastAPI、Uvicorn、Excel 读取库、XML 防护库、进程管理库及其间接依赖，使用预编译包 |
| 配置 | 仅首次从 `.env.example` 生成 `.env`；端口、监听地址、存储目录和缓存配置集中在该文件 |
| 存储和登录 | 创建数据目录；没有指定管理员令牌且文件不存在时生成随机令牌，已有令牌不覆盖 |
| 安装检查 | 检查依赖兼容性、后端模块导入、SQLite 版本与功能、配置有效性 |

SQLite 由 Python 提供，前端由后端提供，无需安装独立数据库、Node 或前端开发工具。安装脚本仅为当前进程放行自带的安装辅助脚本，不更改 Windows 的全局脚本执行策略。安装完成后使用服务脚本启动，安装器本身不会启动服务。

## 修改配置

用记事本打开项目中的 `.env`，例如：

```dotenv
CSVHUB_HOST=0.0.0.0
CSVHUB_PORT=8000
CSVHUB_DATA_DIR=data
CSVHUB_BATCH_ROWS=5000
CSVHUB_BATCH_BYTES=8388608
CSVHUB_CACHE_BYTES=134217728
```

保持 `0.0.0.0` 即可接收各网卡上的连接，不需要随着服务器 IP 变更修改配置。客户端网址要使用新的服务器实际 IP。修改端口后重启服务；修改数据目录前先停止服务。路径可以写成 `CSVHUB_DATA_DIR="D:\CSVHub Data"`，相对路径以项目根目录为基准。

已有系统或当前会话的 `CSVHUB_*` 环境变量会覆盖 `.env`。不要设置空的 `CSVHUB_API_TOKEN`：删除该配置项即可使用系统自动生成的令牌。

## 迁移和失败处理

- 迁移时先停止原服务，再复制项目和需要保留的 `data`；新机器重新运行 `install.cmd`。不要依赖复制过来的 `.venv`。自定义绝对数据路径需要改为新机器上的路径。
- 下载失败或提示没有可用的预编译依赖包时，检查网络、代理和 Python 包源后重新运行；企业网络可以通过 pip 标准配置指定内部源。安装进度日志在 `.install\install.log`，窗口保留具体失败信息。
- 已有 Python 可以通过 `install.cmd -PythonPath "C:\Python313\python.exe"` 指定。安装器正常使用当前用户权限；若目录受保护，应移到有写权限的目录。
- 安装后可运行 `install.cmd -CheckOnly` 仅检查环境，不下载、安装或修改数据。自动执行可加 `-NoPause`；`install.cmd -Help` 显示参数。
- 若提示服务正在使用环境，先运行 `stop.cmd`。若提示 Python 签名验证失败，检查系统时间、证书更新与网络后重试。
- 如果本机能访问、其他电脑不能访问，需要允许客户端访问配置的 TCP 端口，并检查服务器防火墙与网络规则。安装器不会自动修改防火墙。

官方安装选项参考：[Python Windows 安装说明](https://docs.python.org/3.13/using/windows.html#installing-without-ui)。固定下载版本和校验值来自 [Python 3.13.16 官方发布页](https://www.python.org/downloads/release/python-31316/)。
