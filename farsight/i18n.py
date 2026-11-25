"""Messages in the two UI languages.

The source language is Chinese. `T("…{field}…", field=value)` builds a message that is an ordinary `str` (its
Chinese rendering) and remembers its template and fields, so the HTTP layer can render the same message in
English when a client asks for it. `EN` maps every source template to its English template.
"""

LANGS = ("zh", "en")


class Text(str):
    """A message: a `str` holding the Chinese text, renderable in another language with `render(lang)`."""

    def __new__(cls, template, **fields):
        self = str.__new__(cls, template.format(**fields) if fields else template)
        self.template = template
        self.fields = fields
        return self

    def render(self, lang):
        if lang == "zh":
            return str.__str__(self)
        template = EN.get(self.template, self.template)
        if not self.fields:
            return template
        return template.format(**dict((name, render(value, lang)) for name, value in self.fields.items()))


class Joined(str):
    """Several messages in a row; the separator differs per language (Chinese sentences need no space)."""

    def __new__(cls, parts, zh="", en=" "):
        parts = list(parts)
        self = str.__new__(cls, zh.join(parts))
        self.parts = parts
        self.separators = {"zh": zh, "en": en}
        return self

    def render(self, lang):
        return self.separators.get(lang, self.separators["en"]).join(render(part, lang) for part in self.parts)


T = Text


def render(value, lang):
    return value.render(lang) if isinstance(value, (Text, Joined)) else value


def localize(value, lang):
    """Copy of a JSON-like value with every message rendered in `lang`. Chinese is the stored form: no copy."""
    if lang == "zh":
        return value
    if isinstance(value, (Text, Joined)):
        return value.render(lang)
    if isinstance(value, dict):
        return dict((key, localize(item, lang)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [localize(item, lang) for item in value]
    return value


def pick(explicit, accept_language=""):
    """Language of a request: the page's explicit choice, else the browser's first preference, else Chinese."""
    if explicit in LANGS:
        return explicit
    first = (accept_language or "").split(",")[0].strip().lower()
    if first and not first.startswith("zh"):
        return "en"
    return "zh"


EN = {
    # ssh.py: target validation
    "主机地址无效：只接受主机名、IPv4 或 IPv6 地址": "Invalid host: only host names, IPv4 and IPv6 addresses are accepted",
    "用户名无效": "Invalid user name",
    "端口必须是 1-65535 的整数": "The port must be an integer from 1 to 65535",
    "私钥路径无效": "Invalid private key path",
    "私钥文件不存在：{path}": "Private key file not found: {path}",
    # ssh.py: connection failures
    "主机密钥已变化": "Host key has changed",
    "该地址的 SSH 主机密钥与上次记录的不一致，可能是节点重装，也可能是中间人。确认无误后删除旧记录再重试。":
        "The SSH host key of this address differs from the one recorded earlier. The node may have been reinstalled, "
        "or the connection may be intercepted. Once you are sure, delete the old record and retry.",
    "私钥无法使用": "Private key cannot be used",
    "私钥文件格式不对、权限过宽（应为 600）或路径不存在。":
        "The private key file has the wrong format, permissions that are too open (they should be 600), or does not exist.",
    "节点缺少 Python 3": "No Python 3 on the node",
    "采集器需要节点上有 python3（3.6 及以上）。": "The collector needs python3 (3.6 or later) on the node.",
    "节点的 Python 不完整": "Python on the node is incomplete",
    "节点上的 Python 缺少标准库模块。Debian / Ubuntu 上请安装 python3，而不是只有 python3-minimal。":
        "Python on the node lacks standard library modules. On Debian / Ubuntu install python3, not only python3-minimal.",
    "主机名无法解析": "Host name cannot be resolved",
    "检查主机名拼写，或直接填写 IP 地址。": "Check the spelling of the host name, or enter the IP address.",
    "连接被拒绝": "Connection refused",
    "该地址的 SSH 端口没有服务在监听。检查端口号，或确认节点上 sshd 已启动。":
        "Nothing is listening on the SSH port of this address. Check the port number, or make sure sshd is running on the node.",
    "网络不可达": "Network unreachable",
    "到该地址没有路由。检查 IP 是否正确、是否在同一网络。":
        "There is no route to this address. Check that the IP is right and on a network this machine can reach.",
    "连接超时": "Connection timed out",
    "在限定时间内没有连上。节点可能关机、IP 不对，或被防火墙拦截。":
        "No connection within the time limit. The node may be powered off, the IP may be wrong, or a firewall may be blocking it.",
    "连接被对端关闭": "Connection closed by the node",
    "SSH 握手阶段被节点断开，可能是连接数限制或访问控制。":
        "The node dropped the connection during the SSH handshake, possibly because of a connection limit or access control.",
    "采集器异常退出": "Collector exited with an error",
    "采集脚本在节点上报错，详情见原始输出。": "The collector script failed on the node; see the raw output for details.",
    "连接中断": "Connection lost",
    "与节点的 SSH 会话断开，正在自动重连。": "The SSH session to the node ended; reconnecting automatically.",
    "节点没有响应": "No response from the node",
    "SSH 已连上，但采集器在限定时间内没有返回数据。": "SSH connected, but the collector returned no data within the time limit.",
    "需要输入密码": "Password required",
    "密码只保存在内存里，面板重启后需要重新输入。":
        "The password is kept in memory only and has to be entered again after the dashboard restarts.",
    "连接失败": "Connection failed",
    "未能识别的失败原因，详情见原始输出。": "The cause was not recognized; see the raw output for details.",
    "认证失败：密码未被接受": "Authentication failed: password not accepted",
    "密码错误，或该节点不允许密码登录。": "The password is wrong, or the node does not allow password login.",
    "认证失败：密钥未被接受": "Authentication failed: key not accepted",
    "确认用户名正确，且本机公钥已加入节点的 authorized_keys。":
        "Check that the user name is right and that this machine's public key is in the node's authorized_keys.",
    "该节点也允许密码登录，可改用密码方式。": "The node also allows password login; you can switch to password authentication.",
    "节点允许的认证方式：{methods}。": "Authentication methods the node allows: {methods}.",
    "无法启动 ssh：{error}": "Cannot start ssh: {error}",
    # events.py
    "已恢复：{title}": "Recovered: {title}",
    "GPU {index} 温度过高": "GPU {index} is too hot",
    "GPU {index} 温度偏高": "GPU {index} is running hot",
    "GPU {index} 空转": "GPU {index} is spinning idle",
    "利用率 {util}%，但显存 {memory} MiB 且没有进程。常见于服务异常停止后的残留，可能需要重置该 GPU。":
        "{util}% utilization with {memory} MiB of memory and no process. Usually left behind by a service that stopped "
        "abnormally; the GPU may need a reset.",
    "GPU {index} 显存已满": "GPU {index} memory is full",
    "主机内存紧张": "Host memory is running low",
    "已用 {percent}%（含共享内存 {shm} GiB）": "{percent}% used (including {shm} GiB of shared memory)",
    "/dev/shm 接近写满": "/dev/shm is almost full",
    "已用 {percent}%。新启动的服务可能因共享内存不足而失败。":
        "{percent}% used. A service started now may fail for lack of shared memory.",
    "系统盘接近写满": "System disk is almost full",
    "已用 {percent}%": "{percent}% used",
    "存在僵尸 SGLang 进程": "Zombie SGLang processes",
    "{count} 个已退出但未被回收的 sglang 进程": "{count} sglang processes have exited but were not reaped",
    "服务降级": "Service degraded",
    "部分组件不健康": "Some components are unhealthy",
    "请求错误率偏高": "High request error rate",
    "首 Token 延迟偏高": "High time to first token",
    "调度队列持续积压": "Scheduler queue keeps backing up",
    "worker 侧等待 {count} 个请求": "{count} requests waiting on the workers",
    # services.py
    "{role}（{node}）无响应": "{role} ({node}) is not responding",
    "{role}（{node}）不再就绪": "{role} ({node}) is no longer ready",
    "{role}（{node}）启动中": "{role} ({node}) is starting",
    "尚无 {role} worker 注册，服务还不能接收请求": "No {role} worker has registered yet; the service cannot take requests",
    "frontend 已启动，尚无 worker 注册": "The frontend is up; no worker has registered yet",
    "发现服务": "Service discovered",
    "服务运行中": "Service is running",
    "服务重新出现": "Service is back",
    "服务已就绪": "Service is ready",
    "服务启动中": "Service is starting",
    "服务状态未知": "Service status unknown",
    "服务状态变化": "Service status changed",
    "所在节点离线，状态未知": "Its node is offline; status unknown",
    "服务已消失": "Service is gone",
    "所有组件进程均已退出": "All component processes have exited",
    "进程已退出": "Processes have exited",
    # hub.py
    "面板": "Dashboard",
    "面板已启动": "Dashboard started",
    "演示数据": "Demo data",
    "{count} 个节点": "{count} node(s)",
    "节点已连接": "Node connected",
    "节点连接中断": "Node connection lost",
    "节点连接中断：{title}": "Node connection lost: {title}",
    "连接失败：{title}": "Connection failed: {title}",
    "请填写至少一个主机地址": "Enter at least one host",
    "一次最多添加 {count} 个节点": "At most {count} nodes can be added at once",
    "认证方式无效": "Invalid authentication method",
    "请选择密码方式时填写密码": "Enter a password when using password authentication",
    "密码格式无效": "Invalid password format",
    "{host}：{reason}": "{host}: {reason}",
    "节点已存在：{hosts}": "Node already exists: {hosts}",
    "已存在相同地址的节点": "A node with the same address already exists",
    "请填写密码": "Enter the password",
    "节点已移除": "Node removed",
    "状态汇总出错": "State aggregation failed",
    "未知": "Unknown",
    "成本设置无效：{field}": "Invalid cost setting: {field}",
    "无法保存设置：{error}": "Cannot save the settings: {error}",
    "成本设置已更新": "Cost settings updated",
    "演示模式下不保存设置": "Settings are not saved in demo mode",
    # server.py
    "请求无效": "Invalid request",
    "请求体过大": "Request body too large",
    "请求必须是 JSON": "The request must be JSON",
    "JSON 格式错误": "Malformed JSON",
    "请求体必须是对象": "The request body must be an object",
    "需要访问令牌": "Access token required",
    "跨站请求被拒绝": "Cross-site request refused",
    "不存在": "Not found",
    "服务端错误，详见面板日志": "Server error; see the dashboard log",
    "window 无效": "Invalid window",
    "limit 无效": "Invalid limit",
    # topology.py
    "推理请求（OpenAI API）": "Inference requests (OpenAI API)",
    "Dynamo 请求面": "Dynamo request plane",
    "Dynamo 响应流（worker 回连 Frontend）": "Dynamo response stream (worker dials the frontend)",
    "Dynamo 事件面（KV 事件与负载）": "Dynamo event plane (KV events and load)",
    "服务发现与注册": "Service discovery and registration",
    "etcd 成员同步": "etcd peer replication",
    "KV 传输握手（Bootstrap）": "KV transfer handshake (bootstrap)",
    "KV 传输控制": "KV transfer control",
    "KV 缓存传输": "KV cache transfer",
    "FlexKV 跨节点缓存（radix-server）": "FlexKV cross-node cache (radix-server)",
    "KV 事件上报": "KV event reporting",
    "引擎进程间通信": "Engine inter-process communication",
    "引擎各 rank 之间的连接": "Connections among the engine's ranks",
    "多卡协调（torch.distributed）": "Multi-GPU coordination (torch.distributed)",
    "监控端点（/metrics、/health）": "Monitoring endpoint (/metrics, /health)",
    "面板采集（只读）": "Dashboard collection (read-only)",
    "其他连接": "Other connections",
    "传输后端未知": "transfer backend unknown",
}
