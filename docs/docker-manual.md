# 从空目录手动部署（Linux + Docker Compose v2）

不运行一键安装脚本。镜像只含程序和依赖，不含旧数据库、模型Key、管理员Token或已登记资产。你自己准备文件，随后一条Compose命令启动。

## 1. 获取代码并准备文件

```sh
git clone https://github.com/hewenze11/ai-ops.git
cd ai-ops
cp .env.example .env
sudo install -d -m 700 -o 10001 -g 10001 state secrets connector
# 自己生成管理员登录Token；不会打印到终端。
sudo sh -c 'umask 077; openssl rand -hex 32 > secrets/admin_token'
sudo chown 10001:10001 secrets/admin_token
sudo chmod 600 secrets/admin_token
```

运行账号为容器UID/GID **10001:10001**，因此宿主的挂载文件需要上面的权限。

文件用途：
- `.env`：镜像、监听地址、端口及可选后台开关。先不用改模型配置。
- `secrets/admin_token`：控制台登录密码式Token；自行查看使用，不提交Git。
- `state/`：SQLite、角色独立模型Key、托管SSH凭据和图片，启动时创建；换空目录即为新环境。
- `connector/`：仅供旧式SSH凭据文件引用；你通过UI填密码/私钥时可保持为空。

## 2. 拉取并启动

当前GHCR镜像需要授权拉取（公开代码仓库不等于镜像包公开）。先在这台Linux登录一次：

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

在密码提示中输入你自己的、具有 `read:packages` 且能访问该包的GitHub Token，不是GitHub登录密码，不要把Token粘在命令行或发给助手。然后：

```sh
docker compose pull && docker compose up -d --no-build
docker compose ps
curl -fsS http://127.0.0.1:8765/healthz
```

如果想亲自用本仓库Dockerfile构建，而不是拉镜像：

```sh
AI_OPS_IMAGE=ai-ops-service:manual docker compose up -d --build
```

默认监听Linux宿主 `127.0.0.1:8765`。在你自己的电脑开SSH隧道：

```sh
ssh -N -L 8765:127.0.0.1:8765 your-user@CONTROL_SERVER_IP
```

然后浏览器打开 `http://127.0.0.1:8765`，用 `secrets/admin_token` 内容登录。也可在私网测试时自行将 `.env` 的 `AI_OPS_BIND` 改为指定内网IP或 `0.0.0.0`，重新 `docker compose up -d`；远程正式访问使用HTTPS反向代理，不把管理员Token通过公网HTTP发送。

## 3. 从空白控制台开始

1. 新建角色，亲自填OpenAI兼容模型的Base URL、模型名、API Key；独立模型Key由服务保存到state目录，不需要把你的Key写进Dockerfile。
2. 在“资产”里选择下面一种接入方式，登记允许的执行账号。
3. 回到角色聊天，选择资产和执行账号，先用 **confirm（逐条确认）** 模式测试 `id`、`hostname`、`pwd`。
4. 核对命令实际在哪个环境执行，再测试你的业务操作。账号登记与备注不等于本轮授权。

### 方式A：Agent主动连接主服务

使用独立仓库 [ai-ops-agent 容器说明](https://github.com/hewenze11/ai-ops-agent/blob/main/docs/docker-manual.md)。先在主服务注册Agent类型资产，复制返回的资产ID与一次性Token，填入Agent的 `config.json`。

**默认Agent容器管理的是容器自身的 `aiops` 账号及挂载工作目录，不是宿主Linux。** 如果你要Agent原生管理宿主用户/服务，使用Agent仓库README的手工venv + systemd方式；不需要一键安装脚本。不提供默认特权容器、Docker socket或宿主根目录挂载来伪装原生Agent。

Agent跨机器连接需要可验证证书的HTTPS。无域名的测试可用Agent宿主上的SSH隧道，将远程主服务转成该宿主的loopback地址，具体命令见Agent说明。

### 方式B：主服务通过SSH管理Linux宿主

目标机不安装Agent，只需要现有sshd和你准备的登录账号：

1. 在你可信的目标机终端查看主机公钥：`cat /etc/ssh/ssh_host_ed25519_key.pub`。复制 `ssh-ed25519 AAAA...` 整行，**不是SHA256指纹，也不是私钥**。
2. 主服务“资产 → 注册资产 → SSH”中填IP、端口、SSH登录用户名、密码或无口令Ed25519私钥、主机公钥。
3. 登记目标机真实存在的允许执行账号。第一次先让执行账号等于SSH登录账号。
4. 选择该资产/账号发出测试命令，在confirm模式批准，核对结果。切换其他执行账号时目标机须预先配置对应 `sudo -n` 权限；平台不会用SSH密码自动回答sudo。

SSH是主服务容器主动连接目标机，Agent Token只用于Agent方式，二者无需同时配置。不要对同一主机无意登记两条并行执行路径。

## 4. 常用操作

```sh
docker compose logs --tail=100 control
docker compose restart control
docker compose down
```

`down` 不删你手动创建的state/配置目录。升级用 `docker compose pull && docker compose up -d --no-build`。若需要固定版本，将 `.env` 中镜像改成已发布的 `sha-...` 标签或 `@sha256:...` 摘要。

通知、记忆归档、本机local连接默认关闭。需要时自行修改 `.env` 对应值为1并重建容器；local指控制容器而非宿主。普通角色模型聊天与SSH/Agent接入默认可用，不用打开local。镜像不会自带你的模型配置或自动外发通知。
