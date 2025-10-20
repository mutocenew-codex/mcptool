"""
Simple MCP stdio <-> WebSocket pipe with optional unified config.
Version: 0.2.0

Usage (env):
    export MCP_ENDPOINT=<ws_endpoint>
    # Windows (PowerShell): $env:MCP_ENDPOINT = "<ws_endpoint>"

Start server process(es) from config:
Run all configured servers (default)
    python mcp_pipe.py

Run a single local server script (back-compat)
    python mcp_pipe.py path/to/server.py

Config discovery order:
    $MCP_CONFIG, then ./mcp_config.json

Env overrides:
    (none for proxy; uses current Python: python -m mcp_proxy)
"""

import asyncio
import websockets
import subprocess
import logging
import os
import signal
import sys
import json
from dotenv import load_dotenv
from datetime import datetime
import threading
import random
from logging.handlers import RotatingFileHandler

# Auto-load environment variables from a .env file if present
load_dotenv()

# 彩色日志配置
class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器"""
    COLORS = {
        'DEBUG': '\033[36m',    # 青色
        'INFO': '\033[32m',     # 绿色
        'WARNING': '\033[33m',  # 黄色
        'ERROR': '\033[31m',    # 红色
        'CRITICAL': '\033[35m', # 紫色
    }
    RESET = '\033[0m'
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, '')
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)

# 配置彩色日志
logger = logging.getLogger('MCP_PIPE')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# also log to file for later inspection
LOG_DIR = os.path.join(os.getcwd(), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
file_handler = RotatingFileHandler(os.path.join(LOG_DIR, 'mcp_pipe.log'), maxBytes=2_000_000, backupCount=5, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 状态文件路径
STATUS_FILE = os.path.join(os.getcwd(), ".mcp_status.json")

# 状态管理
def init_status_file():
    """初始化状态文件"""
    try:
        if not os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "endpoints": {},
                    "tools": {}
                }, f, indent=2)
    except Exception as e:
        logger.error(f"初始化状态文件失败: {e}")

def update_tool_status(server_name, status, error="", tools=None):
    """更新工具状态"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        data = {"endpoints": {}, "tools": {}}
    # If server_name encodes endpoint info (e.g. "server::endpoint"), merge under base server
    base_name = server_name
    if isinstance(server_name, str) and '::' in server_name:
        base_name = server_name.split('::', 1)[0]

    existing = data.get("tools", {}).get(base_name, {})
    existing_tools = existing.get("tools", []) if isinstance(existing.get("tools", []), list) else []
    # Merge tools by name (avoid duplicates)
    new_tools = tools or []
    merged = {t.get('name'): t for t in existing_tools}
    for t in new_tools:
        merged[t.get('name')] = t

    merged_list = list(merged.values())
    data["tools"][base_name] = {
        "status": status,
        "error": error,
        "tools": merged_list,
        "last_updated": datetime.now().isoformat()
    }

    # Also write tools under the endpoint entry if target encoded as server::endpoint
    try:
        if isinstance(server_name, str) and '::' in server_name:
            endpoint_name = server_name.split('::', 1)[1]
            data.setdefault('endpoints', {})
            ep = data['endpoints'].get(endpoint_name, {})
            ep_tools = ep.get('tools', [])
            # Replace or merge per-endpoint tools for this server
            # We'll attach a mapping of server -> tools for clarity
            ep.setdefault('server_tools', {})
            ep['server_tools'][base_name] = merged_list
            ep['last_updated'] = datetime.now().isoformat()
            data['endpoints'][endpoint_name] = ep
    except Exception:
        # Don't fail the whole update if per-endpoint write errors out
        pass
    
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"更新工具状态失败: {e}")


def update_endpoint_status(endpoint_name, connected=False, error="", url=None):
    """Update endpoint status in the shared status file."""
    if not endpoint_name:
        return
    try:
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"endpoints": {}, "tools": {}}

        data.setdefault('endpoints', {})
        data['endpoints'][endpoint_name] = {
            'connected': bool(connected),
            'error': error or '',
            'url': url or data['endpoints'].get(endpoint_name, {}).get('url', ''),
            'last_heartbeat': datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat()
        }

        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"更新端点状态失败: {e}")

async def get_tools_from_server_async(process, target):
    """异步从MCP服务器获取工具列表"""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        from mcp import StdioServerParameters
        
        # 重新启动服务器进程来获取工具
        server_params = StdioServerParameters(
            command=process.args[0] if hasattr(process, 'args') else "python",
            args=process.args[1:] if hasattr(process, 'args') and len(process.args) > 1 else ["-m", "calculator"]
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                # 初始化
                await session.initialize()
                
                # 获取工具列表
                tools_result = await session.list_tools()
                
                tools = []
                for tool in tools_result.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema
                    })
                
                logger.info(f"[{target}] 成功获取到 {len(tools)} 个工具")
                return tools
                
    except Exception as e:
        logger.warning(f"[{target}] 获取工具列表失败: {e}")
        return []

def get_tools_from_server(process, target):
    """从MCP服务器获取工具列表（同步包装）"""
    try:
        return asyncio.run(get_tools_from_server_async(process, target))
    except Exception as e:
        logger.warning(f"[{target}] 获取工具列表失败: {e}")
        return []

# Reconnection settings
INITIAL_BACKOFF = 1  # Initial wait time in seconds
MAX_BACKOFF = 600  # Maximum wait time in seconds
# Limit concurrent connection attempts to avoid overwhelming network/resources
MAX_CONCURRENT_CONNECTIONS = int(os.environ.get('MCP_MAX_CONCURRENT_CONNECTIONS', '8'))

async def connect_with_retry(uri, target, semaphore: asyncio.Semaphore = None):
    """Connect to WebSocket server with retry mechanism for a given server target."""
    reconnect_attempt = 0
    backoff = INITIAL_BACKOFF
    while True:  # Infinite reconnection
        try:
            if reconnect_attempt > 0:
                logger.info(f"[{target}] Waiting {backoff}s before reconnection attempt {reconnect_attempt}...")
                await asyncio.sleep(backoff)

            # Throttle concurrent connection attempts if semaphore provided
            if semaphore is not None:
                await semaphore.acquire()
            try:
                # Attempt to connect
                await connect_to_server(uri, target)
            finally:
                if semaphore is not None:
                    try:
                        semaphore.release()
                    except Exception:
                        pass

        except Exception as e:
            reconnect_attempt += 1
            logger.warning(f"[{target}] Connection closed (attempt {reconnect_attempt}): {e}")
            # Calculate wait time for next reconnection (exponential backoff with jitter)
            backoff = min(backoff * 2, MAX_BACKOFF)
            jitter = random.uniform(0, min(1.0, backoff))
            backoff = backoff + jitter

async def connect_to_server(uri, target):
    """Connect to WebSocket server and pipe stdio for the given server target."""
    process = None
    try:
        logger.info(f"[{target}] Connecting to WebSocket server...")
        async with websockets.connect(uri) as websocket:
            logger.info(f"[{target}] Successfully connected to WebSocket server")

            # determine endpoint name if target encoded as server::endpoint
            endpoint_name = None
            if isinstance(target, str) and '::' in target:
                endpoint_name = target.split('::', 1)[1]
            update_endpoint_status(endpoint_name or uri, connected=True, error='', url=uri)

            # Start server process (built from CLI arg or config)
            cmd, env = build_server_command(target)
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
                text=True,
                env=env
            )
            logger.info(f"[{target}] Started server process: {' '.join(cmd)}")

            # 更新状态为运行中
            update_tool_status(target, "运行中")

            # 延迟获取工具列表，给服务器时间初始化
            async def get_tools_delayed():
                await asyncio.sleep(3)  # 等待3秒让服务器完全初始化
                try:
                    tools = await get_tools_from_server_async(process, target)
                    if tools:
                        logger.info(f"[{target}] 发现 {len(tools)} 个工具: {[tool.get('name', 'unknown') for tool in tools]}")
                        update_tool_status(target, "运行中", tools=tools)
                    else:
                        logger.warning(f"[{target}] 未发现工具")
                        update_tool_status(target, "运行中", tools=[])
                except Exception as e:
                    logger.warning(f"[{target}] 获取工具列表时出错: {e}")
                    update_tool_status(target, "运行中", tools=[])

            # 在后台任务中获取工具
            asyncio.create_task(get_tools_delayed())

            # Create two tasks: read from WebSocket and write to process, read from process and write to WebSocket
            await asyncio.gather(
                pipe_websocket_to_process(websocket, process, target),
                pipe_process_to_websocket(process, websocket, target),
                pipe_process_stderr_to_terminal(process, target)
            )
    except websockets.exceptions.ConnectionClosed as e:
        code = getattr(e, 'code', None)
        reason = getattr(e, 'reason', None)
        logger.warning(f"[{target}] WebSocket connection closed: code={code} reason={reason}")
        update_tool_status(target, "错误", f"WebSocket连接关闭: code={code} reason={reason}")
        # update endpoint status as disconnected with error
        endpoint_name = target.split('::', 1)[1] if ('::' in target) else uri
        update_endpoint_status(endpoint_name, connected=False, error=f"code={code} reason={reason}", url=uri)
        raise  # Re-throw exception to trigger reconnection
    except Exception as e:
        logger.error(f"[{target}] Connection error: {e}")
        update_tool_status(target, "错误", f"连接错误: {e}")
        endpoint_name = target.split('::', 1)[1] if ('::' in target) else uri
        update_endpoint_status(endpoint_name, connected=False, error=str(e), url=uri)
        raise  # Re-throw exception
    finally:
        # Ensure the child process is properly terminated
        if process:
            logger.info(f"[{target}] Terminating server process")
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            logger.info(f"[{target}] Server process terminated")
            update_tool_status(target, "已停止")

async def pipe_websocket_to_process(websocket, process, target):
    """Read data from WebSocket and write to process stdin"""
    try:
        while True:
            # Read message from WebSocket
            message = await websocket.recv()
            logger.debug(f"[{target}] << {message[:120]}...")
            
            # Write to process stdin (in text mode)
            if isinstance(message, bytes):
                message = message.decode('utf-8')
            process.stdin.write(message + '\n')
            process.stdin.flush()
    except Exception as e:
        # Often this is a ConnectionClosed error from server (e.g., code 4004). Log details at warning level.
        if isinstance(e, websockets.exceptions.ConnectionClosed):
            code = getattr(e, 'code', None)
            reason = getattr(e, 'reason', None)
            logger.warning(f"[{target}] Error in WebSocket to process pipe: Connection closed code={code} reason={reason}")
        else:
            logger.error(f"[{target}] Error in WebSocket to process pipe: {e}")
        raise  # Re-throw exception to trigger reconnection
    finally:
        # Close process stdin
        if not process.stdin.closed:
            process.stdin.close()

async def pipe_process_to_websocket(process, websocket, target):
    """Read data from process stdout and send to WebSocket"""
    try:
        while True:
            # Read data from process stdout
            data = await asyncio.to_thread(process.stdout.readline)
            
            if not data:  # If no data, the process may have ended
                logger.info(f"[{target}] Process has ended output")
                break
                
            # Send data to WebSocket
            logger.debug(f"[{target}] >> {data[:120]}...")
            # In text mode, data is already a string, no need to decode
            await websocket.send(data)
    except Exception as e:
        # If websocket was closed while writing, log at warning with code/reason when available
        if isinstance(e, websockets.exceptions.ConnectionClosed):
            code = getattr(e, 'code', None)
            reason = getattr(e, 'reason', None)
            logger.warning(f"[{target}] Error in process to WebSocket pipe: Connection closed code={code} reason={reason}")
        else:
            logger.error(f"[{target}] Error in process to WebSocket pipe: {e}")
        raise  # Re-throw exception to trigger reconnection

async def pipe_process_stderr_to_terminal(process, target):
    """Read data from process stderr and print to terminal"""
    try:
        while True:
            # Read data from process stderr
            data = await asyncio.to_thread(process.stderr.readline)
            
            if not data:  # If no data, the process may have ended
                logger.info(f"[{target}] Process has ended stderr output")
                break
                
            # Print stderr data to terminal (in text mode, data is already a string)
            sys.stderr.write(data)
            sys.stderr.flush()
    except Exception as e:
        logger.error(f"[{target}] Error in process stderr pipe: {e}")
        raise  # Re-throw exception to trigger reconnection

def signal_handler(sig, frame):
    """Handle interrupt signals"""
    logger.info("Received interrupt signal, shutting down...")
    sys.exit(0)

def load_config():
    """Load JSON config from $MCP_CONFIG or ./mcp_config.json. Return dict or {}."""
    path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config {path}: {e}")
        return {}


def build_server_command(target=None):
    """Build [cmd,...] and env for the server process for a given target.

    Priority:
    - If target matches a server in config.mcpServers: use its definition
    - Else: treat target as a Python script path (back-compat)
    If target is None, read from sys.argv[1].
    """
    if target is None:
        assert len(sys.argv) >= 2, "missing server name or script path"
        target = sys.argv[1]

    # If target encoded as 'server::endpoint', use server part for command lookup
    if isinstance(target, str) and '::' in target:
        target = target.split('::', 1)[0]
    cfg = load_config()
    servers = cfg.get("mcpServers", {}) if isinstance(cfg, dict) else {}

    if target in servers:
        entry = servers[target] or {}
        if entry.get("disabled"):
            raise RuntimeError(f"Server '{target}' is disabled in config")
        typ = (entry.get("type") or entry.get("transportType") or "stdio").lower()

        # environment for child process
        child_env = os.environ.copy()
        for k, v in (entry.get("env") or {}).items():
            child_env[str(k)] = str(v)

        if typ == "stdio":
            command = entry.get("command")
            args = entry.get("args") or []
            if not command:
                raise RuntimeError(f"Server '{target}' is missing 'command'")
            return [command, *args], child_env

        if typ in ("sse", "http", "streamablehttp"):
            url = entry.get("url")
            if not url:
                raise RuntimeError(f"Server '{target}' (type {typ}) is missing 'url'")
            # Unified approach: always use current Python to run mcp-proxy module
            cmd = [sys.executable, "-m", "mcp_proxy"]
            if typ in ("http", "streamablehttp"):
                cmd += ["--transport", "streamablehttp"]
            # optional headers: {"Authorization": "Bearer xxx"}
            headers = entry.get("headers") or {}
            for hk, hv in headers.items():
                cmd += ["-H", hk, str(hv)]
            cmd.append(url)
            return cmd, child_env

        raise RuntimeError(f"Unsupported server type: {typ}")

    # Fallback to script path (back-compat)
    script_path = target
    if not os.path.exists(script_path):
        raise RuntimeError(
            f"'{target}' is neither a configured server nor an existing script"
        )
    return [sys.executable, script_path], os.environ.copy()

# ------------------------------
# Minimal Web UI (non-intrusive)
# ------------------------------
def create_app():
    """Create a minimal Flask app that only reads status/config and can request restart.
    It does not start any background threads or connect to endpoints.
    """
    from flask import Flask, jsonify, request, render_template_string

    app = Flask(__name__)

    INDEX_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8" />
        <title>MCP 管理面板</title>
        <style>
            /* 基础与背景 */
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: #333;
                min-height: 100vh;
            }
            h1 {
                color: #ffffff;
                text-align: center;
                margin: 20px 0 30px;
                text-shadow: 0 2px 4px rgba(0,0,0,0.3);
                font-size: 2.2em;
                font-weight: 300;
            }
            /* 卡片/区块 */
            .card {
                background-color: #ffffff;
                margin-bottom: 16px;
                padding: 20px;
                border-radius: 16px;
                box-shadow: 0 8px 25px rgba(0,0,0,0.08);
                transition: all 0.25s ease;
                border: 1px solid rgba(255,255,255,0.2);
            }
            .card:hover { transform: translateY(-2px); box-shadow: 0 12px 32px rgba(0,0,0,0.12); }
            h3 { color: #34495e; margin: 10px 0 12px; font-size: 1.1em; font-weight: 600; }
            /* 表单控件 */
            input, textarea, select {
                width: 100%;
                padding: 12px 16px;
                margin: 8px 0 12px;
                box-sizing: border-box;
                border: 2px solid #e1e5e9;
                border-radius: 10px;
                font-size: 14px;
                transition: all 0.25s ease;
                background: #fafbfc;
            }
            input:focus, textarea:focus, select:focus {
                border-color: #667eea;
                outline: none;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.12);
                background: #ffffff;
            }
            textarea { resize: vertical; min-height: 80px; font-family: 'Courier New', monospace; }
            /* 按钮样式 */
            button {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: #fff;
                padding: 10px 18px;
                border: none;
                border-radius: 10px;
                cursor: pointer;
                transition: all 0.2s ease;
                font-size: 14px;
                font-weight: 500;
                margin: 4px 8px 4px 0;
                box-shadow: 0 4px 14px rgba(102, 126, 234, 0.28);
            }
            button:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(102,126,234,0.36); }
            button.stop { background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%); box-shadow: 0 4px 14px rgba(231, 76, 60, 0.28); }
            button.backup { background: linear-gradient(135deg, #f39c12 0%, #d35400 100%); box-shadow: 0 4px 14px rgba(243, 156, 18, 0.28); }
            /* 布局辅助 */
            .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
            .server-card { background:#fff; border:1px solid #eef2f7; border-radius:14px; padding:14px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
            .server-card h4 { margin: 0 0 8px; font-size: 15px; color:#2c3e50; font-weight: 600; }
            .server-meta { font-size:12px; color:#64748b; margin-bottom: 8px; display:flex; justify-content: space-between; }
            .tool-chip { display:inline-block; padding:6px 10px; background:#f8fafc; border:1px solid #e6ebf1; border-radius: 16px; font-size:12px; color:#475569; margin: 2px; }
            pre { background: #f8fafc; padding: 12px; border-radius: 8px; overflow: auto; border: 1px solid #eef2f7; }
            .small { font-size: 12px; color: #64748b; }
            /* 状态徽章 */
            .status-badge { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: 700; margin-left: 8px; }
            .status-connected { background-color: #d5f5e3; color: #27ae60; }
            .status-disconnected { background-color: #fadbd8; color: #e74c3c; }
            .status-running { background-color: #e8f8ff; color: #2a7fb8; }
            .status-stopped { background-color: #f5e8c8; color: #d35400; }
            .status-error { background-color: #fde2e2; color: #c0392b; }
            /* 工具标签 */
            .tool-tag { display:inline-block; padding:6px 12px; background:#fff; border:1px solid #dee2e6; border-radius: 20px; font-size:13px; color:#495057; margin: 2px; }
        </style>
    </head>
    <body>
        <h1>mcptool v2</h1>
        <div class="card">
            <div class="row">
                <button onclick="refreshStatus()">刷新状态</button>
                <button onclick="doRestart()">重启服务</button>
                <button onclick="toggleService()" id="btn-toggle">加载中...</button>
            </div>
        </div>
        <div class="card">
            <h3 style="margin-bottom:10px;">仪表盘</h3>
            <div id="dashboard" class="grid" style="grid-template-columns: repeat(3, minmax(160px, 1fr));">
                <div class="server-card" style="text-align:center;">
                    <div class="small" style="margin-bottom:6px;">服务器数量</div>
                    <div id="metric-servers" style="font-size:28px; font-weight:700; color:#2c3e50;">-</div>
                </div>
                <div class="server-card" style="text-align:center;">
                    <div class="small" style="margin-bottom:6px;">接入点数量</div>
                    <div id="metric-endpoints" style="font-size:28px; font-weight:700; color:#2c3e50; cursor:pointer;" onclick="openEndpointsModal()" title="点击设置接入点">-</div>
                </div>
                <div class="server-card" style="text-align:center;">
                    <div class="small" style="margin-bottom:6px;">工具总数</div>
                    <div id="metric-tools" style="font-size:28px; font-weight:700; color:#2c3e50;">-</div>
                </div>
            </div>
        </div>
        <div class="card">
            <div class="row" style="justify-content: space-between;">
                <h3 style="margin:0;">服务器与工具</h3>
                <div>
                    <button onclick="openAddTypeModal()">按类型+</button>
                    <button onclick="openAddJsonModal()">JSON+</button>
                </div>
            </div>
            <div id="server-grid" class="grid" style="margin-top:8px;">(点击上方“刷新状态”)</div>
        </div>
        <script>
            async function refreshStatus(){
                const [st, mt, svc] = await Promise.all([
                    fetch('/api/status').then(r=>r.json()),
                    fetch('/api/metrics').then(r=>r.json()),
                    fetch('/api/service').then(r=>r.json())
                ]);
                renderDashboard(mt);
                renderServerCards(st);
                renderServiceToggle(svc);
            }
            async function doRestart(){
                const res = await fetch('/api/restart', { method: 'POST' });
                const data = await res.json();
                alert(data.message || '已触发重启');
            }

            function renderDashboard(metrics){
                if(!metrics || metrics.error){ return; }
                document.getElementById('metric-servers').textContent = metrics.num_servers ?? '-';
                document.getElementById('metric-endpoints').textContent = metrics.num_endpoints ?? '-';
                document.getElementById('metric-tools').textContent = metrics.total_tools ?? '-';
            }

            function renderServerCards(status){
                const wrap = document.getElementById('server-grid');
                const toolsMap = (status && status.tools) || {};
                const serverNames = Object.keys(toolsMap).sort();
                if(serverNames.length === 0){ wrap.textContent = '(暂无工具信息)'; return; }
                const frag = document.createDocumentFragment();
                serverNames.forEach(name => {
                    const s = toolsMap[name] || {};
                    const tools = s.tools || [];
                    const card = document.createElement('div');
                    card.className = 'server-card';
                    const h4 = document.createElement('h4');
                    h4.textContent = name;
                    const gear = document.createElement('span'); 
                    gear.textContent='⚙'; 
                    gear.title='设置'; 
                    gear.style.float='right'; 
                    gear.style.cursor='pointer';
                    gear.onclick = async ()=>{
                        // 获取当前配置并打开编辑弹窗
                        const cfg = await (await fetch('/api/config')).json();
                        const entry = (cfg && cfg.mcpServers && cfg.mcpServers[name]) || {};
                        openEditServerModal(name, entry);
                    };
                    const meta = document.createElement('div');
                    meta.className = 'server-meta';
                    const st = document.createElement('span'); st.textContent = s.status || '未知';
                    const ct = document.createElement('span'); ct.textContent = '工具: ' + tools.length;
                    meta.appendChild(st); meta.appendChild(ct);
                    const list = document.createElement('div');
                    tools.forEach(t => { const chip = document.createElement('span'); chip.className='tool-chip'; chip.textContent=(t.name||'unknown'); list.appendChild(chip); });
                    card.appendChild(gear); card.appendChild(h4); card.appendChild(meta); card.appendChild(list);
                    frag.appendChild(card);
                });
                wrap.innerHTML=''; wrap.appendChild(frag);
            }


            async function toggleService(){
                const btn = document.getElementById('btn-toggle');
                btn.disabled = true; btn.textContent = '处理中...';
                try{
                    const svc = await (await fetch('/api/service')).json();
                    if(svc.running){ await fetch('/api/stop', { method: 'POST' }); }
                    else { await fetch('/api/start', { method: 'POST' }); }
                    setTimeout(refreshStatus, 800);
                } finally {
                    setTimeout(()=>{ btn.disabled=false; }, 900);
                }
            }

            function renderServiceToggle(svc){
                const btn = document.getElementById('btn-toggle');
                btn.textContent = (svc && svc.running) ? '关闭服务' : '打开服务';
            }

            // 弹窗功能
            function openEditServerModal(name, entry){
                const modal = document.getElementById('modal-edit');
                modal.style.display = 'block';
                document.getElementById('edit-old-name').value = name;
                document.getElementById('edit-name').value = name;
                document.getElementById('edit-type').value = entry.type||'';
                document.getElementById('edit-command').value = entry.command||'';
                document.getElementById('edit-args').value = (entry.args||[]).join(' ');
                document.getElementById('edit-url').value = entry.url||'';
                document.getElementById('edit-env').value = entry.env?JSON.stringify(entry.env):'';
                document.getElementById('edit-disabled').value = String(!!entry.disabled);
            }
            
            async function saveEditServer(){
                const oldName = document.getElementById('edit-old-name').value.trim();
                const newName = document.getElementById('edit-name').value.trim();
                const type = document.getElementById('edit-type').value.trim();
                const command = document.getElementById('edit-command').value.trim();
                const args = document.getElementById('edit-args').value.trim();
                const url = document.getElementById('edit-url').value.trim();
                const envStr = document.getElementById('edit-env').value.trim();
                const disabledStr = document.getElementById('edit-disabled').value.trim();
                let env={}; if(envStr){ try{ env=JSON.parse(envStr); }catch(e){ alert('env需为JSON'); return; } }
                const payload = { type, command, args, url, env, disabled: (disabledStr||'').toLowerCase()==='true' };
                if(newName && newName!==oldName){
                    const addRes = await fetch('/api/servers', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ name:newName, ...payload }) });
                    const addData = await addRes.json(); if(addData.status!=='success'){ alert(addData.message||'改名失败'); return; }
                    await fetch('/api/servers/'+encodeURIComponent(oldName), { method:'DELETE' });
                } else {
                    const up = await fetch('/api/servers/'+encodeURIComponent(oldName), { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                    const d = await up.json(); if(d.status!=='success'){ alert(d.message||'保存失败'); return; }
                }
                closeModal('modal-edit');
                await refreshStatus();
            }
            
            async function deleteEditServer(){
                const oldName = document.getElementById('edit-old-name').value.trim();
                const res = await fetch('/api/servers/'+encodeURIComponent(oldName), { method:'DELETE' });
                const d = await res.json(); if(d.status!=='success'){ alert(d.message||'删除失败'); return; }
                closeModal('modal-edit'); 
                await refreshStatus();
            }

            // 添加类型弹窗
            function openAddTypeModal(){ document.getElementById('modal-add-type').style.display='block'; }
            async function saveAddType(){
                const name = document.getElementById('add-name').value.trim();
                const type = document.getElementById('add-type').value.trim();
                const command = document.getElementById('add-command').value.trim();
                const args = document.getElementById('add-args').value.trim();
                const url = document.getElementById('add-url').value.trim();
                const envStr = document.getElementById('add-env').value.trim();
                const disabledStr = document.getElementById('add-disabled').value.trim();
                if(!name || !type){ alert('名称与类型必填'); return; }
                let env={}; if(envStr){ try{ env=JSON.parse(envStr); }catch(e){ alert('env需为JSON'); return; } }
                const payload={ name, type, command, args, url, env, disabled:(disabledStr||'').toLowerCase()==='true' };
                const res = await fetch('/api/servers', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                const d = await res.json(); if(d.status!=='success'){ alert(d.message||'添加失败'); return; }
                closeModal('modal-add-type'); 
                await refreshStatus();
            }

            // 添加JSON弹窗
            function openAddJsonModal(){ document.getElementById('modal-add-json').style.display='block'; }
            async function saveAddJson(){
                const txt = document.getElementById('add-json').value;
                let obj={}; try{ obj=JSON.parse(txt||'{}'); }catch(e){ alert('JSON解析失败'); return; }
                const res = await fetch('/api/servers/json', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ mcpServers: obj }) });
                const d = await res.json(); if(d.status!=='success'){ alert(d.message||'合并失败'); return; }
                closeModal('modal-add-json'); 
                await refreshStatus();
            }

            function closeModal(id){ document.getElementById(id).style.display='none'; }

            // 接入点设置功能
            function openEndpointsModal(){ 
                document.getElementById('modal-endpoints').style.display='block';
                loadEndpoints();
            }
            
            async function loadEndpoints(){
                try {
                    const res = await fetch('/api/config');
                    const data = await res.json();
                    const endpoints = data.mcpEndpoints || {};
                    const container = document.getElementById('endpoints-list');
                    container.innerHTML = '';
                    
                    Object.keys(endpoints).forEach(name => {
                        const ep = endpoints[name] || {};
                        const div = document.createElement('div');
                        div.style.border = '1px solid #e5e7eb';
                        div.style.borderRadius = '8px';
                        div.style.padding = '12px';
                        div.style.margin = '8px 0';
                        
                        const title = document.createElement('div');
                        title.innerHTML = '<strong>' + name + '</strong>';
                        
                        const row = document.createElement('div');
                        row.className = 'row';
                        
                        const nameInput = document.createElement('input');
                        nameInput.value = name;
                        nameInput.placeholder = '名称';
                        nameInput.id = 'ep-name-' + name;
                        
                        const urlInput = document.createElement('input');
                        urlInput.value = ep.url || '';
                        urlInput.placeholder = 'WebSocket URL';
                        urlInput.id = 'ep-url-' + name;
                        
                        const enabledInput = document.createElement('input');
                        enabledInput.value = String(!!ep.enabled);
                        enabledInput.placeholder = 'enabled true|false';
                        enabledInput.id = 'ep-enabled-' + name;
                        
                        row.appendChild(nameInput);
                        row.appendChild(urlInput);
                        row.appendChild(enabledInput);
                        
                        const btns = document.createElement('div');
                        btns.className = 'row';
                        btns.style.marginTop = '8px';
                        
                        const saveBtn = document.createElement('button');
                        saveBtn.textContent = '保存';
                        saveBtn.onclick = () => saveEndpoint(name, nameInput.value, urlInput.value, enabledInput.value);
                        
                        const delBtn = document.createElement('button');
                        delBtn.textContent = '删除';
                        delBtn.className = 'stop';
                        delBtn.onclick = () => deleteEndpoint(name);
                        
                        btns.appendChild(saveBtn);
                        btns.appendChild(delBtn);
                        
                        div.appendChild(title);
                        div.appendChild(row);
                        div.appendChild(btns);
                        container.appendChild(div);
                    });
                } catch(e) {
                    alert('加载接入点失败: ' + e);
                }
            }
            
            async function saveEndpoint(oldName, newName, url, enabledStr){
                try {
                    const res = await fetch('/api/config');
                    const data = await res.json();
                    const endpoints = data.mcpEndpoints || {};
                    
                    if(newName !== oldName) {
                        // 改名：先添加新的，再删除旧的
                        endpoints[newName] = {
                            url: url,
                            enabled: (enabledStr || '').toLowerCase() === 'true'
                        };
                        delete endpoints[oldName];
                    } else {
                        // 更新现有
                        endpoints[oldName] = {
                            url: url,
                            enabled: (enabledStr || '').toLowerCase() === 'true'
                        };
                    }
                    
                    const updateRes = await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ...data, mcpEndpoints: endpoints })
                    });
                    
                    const result = await updateRes.json();
                    if(result.status !== 'success') {
                        alert(result.message || '保存失败');
                        return;
                    }
                    
                    alert('已保存');
                    loadEndpoints();
                    refreshStatus();
                } catch(e) {
                    alert('保存失败: ' + e);
                }
            }
            
            async function deleteEndpoint(name){
                if(!confirm('确定要删除接入点 "' + name + '" 吗？')) return;
                
                try {
                    const res = await fetch('/api/config');
                    const data = await res.json();
                    const endpoints = data.mcpEndpoints || {};
                    delete endpoints[name];
                    
                    const updateRes = await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ...data, mcpEndpoints: endpoints })
                    });
                    
                    const result = await updateRes.json();
                    if(result.status !== 'success') {
                        alert(result.message || '删除失败');
                        return;
                    }
                    
                    alert('已删除');
                    loadEndpoints();
                    refreshStatus();
                } catch(e) {
                    alert('删除失败: ' + e);
                }
            }
            
            async function addNewEndpoint(){
                const name = document.getElementById('new-ep-name').value.trim();
                const url = document.getElementById('new-ep-url').value.trim();
                const enabledStr = document.getElementById('new-ep-enabled').value.trim();
                
                if(!name || !url) {
                    alert('名称和URL必填');
                    return;
                }
                
                try {
                    const res = await fetch('/api/config');
                    const data = await res.json();
                    const endpoints = data.mcpEndpoints || {};
                    
                    if(endpoints[name]) {
                        alert('接入点 "' + name + '" 已存在');
                        return;
                    }
                    
                    endpoints[name] = {
                        url: url,
                        enabled: (enabledStr || '').toLowerCase() === 'true'
                    };
                    
                    const updateRes = await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ...data, mcpEndpoints: endpoints })
                    });
                    
                    const result = await updateRes.json();
                    if(result.status !== 'success') {
                        alert(result.message || '添加失败');
                        return;
                    }
                    
                    alert('已添加');
                    document.getElementById('new-ep-name').value = '';
                    document.getElementById('new-ep-url').value = '';
                    document.getElementById('new-ep-enabled').value = '';
                    loadEndpoints();
                    refreshStatus();
                } catch(e) {
                    alert('添加失败: ' + e);
                }
            }

            // 初始化：进入页面即加载状态
            document.addEventListener('DOMContentLoaded', ()=>{ refreshStatus(); });

        </script>

        <!-- 弹窗：编辑服务器 -->
        <div id="modal-edit" style="display:none; position:fixed; inset:0; background: rgba(0,0,0,0.35); z-index:1000;">
            <div class="card" style="max-width:720px; margin: 8% auto;">
                <h3 style="margin-top:0;">编辑服务器</h3>
                <input id="edit-old-name" type="hidden" />
                <div class="row"><input id="edit-name" placeholder="名称" /></div>
                <div class="row"><input id="edit-type" placeholder="类型 stdio|sse|http" /></div>
                <div class="row"><input id="edit-command" placeholder="command" /></div>
                <div class="row"><input id="edit-args" placeholder="args (空格分隔)" /></div>
                <div class="row"><input id="edit-url" placeholder="url" /></div>
                <div class="row"><input id="edit-env" placeholder="env JSON" /></div>
                <div class="row"><input id="edit-disabled" placeholder="disabled true|false" /></div>
                <div class="row"><button onclick="saveEditServer()">保存</button><button class="stop" onclick="deleteEditServer()">删除</button><button onclick="closeModal('modal-edit')">关闭</button></div>
            </div>
        </div>

        <!-- 弹窗：按类型添加 -->
        <div id="modal-add-type" style="display:none; position:fixed; inset:0; background: rgba(0,0,0,0.35); z-index:1000;">
            <div class="card" style="max-width:720px; margin: 10% auto;">
                <h3 style="margin-top:0;">按类型添加服务器</h3>
                <div class="row"><input id="add-name" placeholder="名称" /></div>
                <div class="row"><input id="add-type" placeholder="类型 stdio|sse|http" /></div>
                <div class="row"><input id="add-command" placeholder="command (stdio)" /></div>
                <div class="row"><input id="add-args" placeholder="args (空格分隔，stdio)" /></div>
                <div class="row"><input id="add-url" placeholder="url (sse/http)" /></div>
                <div class="row"><input id="add-env" placeholder="env JSON 可选" /></div>
                <div class="row"><input id="add-disabled" placeholder="disabled true|false" /></div>
                <div class="row"><button onclick="saveAddType()">保存</button><button onclick="closeModal('modal-add-type')">关闭</button></div>
            </div>
        </div>

        <!-- 弹窗：按 JSON 合并添加/修改 -->
        <div id="modal-add-json" style="display:none; position:fixed; inset:0; background: rgba(0,0,0,0.35); z-index:1000;">
            <div class="card" style="max-width:720px; margin: 10% auto;">
                <h3 style="margin-top:0;">按 mcpServers JSON 合并添加/修改</h3>
                <textarea id="add-json" rows="10" placeholder="{\n  \"serverName\": { \n    \"type\": \"stdio\", \n    \"command\": \"python\", \n    \"args\": [\"server.py\"] \n  }\n}"></textarea>
                <div class="row"><button onclick="saveAddJson()">保存</button><button onclick="closeModal('modal-add-json')">关闭</button></div>
            </div>
        </div>

        <!-- 弹窗：接入点设置 -->
        <div id="modal-endpoints" style="display:none; position:fixed; inset:0; background: rgba(0,0,0,0.35); z-index:1000;">
            <div class="card" style="max-width:800px; margin: 5% auto; max-height: 80vh; overflow-y: auto;">
                <h3 style="margin-top:0;">接入点设置</h3>
                <div id="endpoints-list">
                    <!-- 现有接入点列表将在这里动态生成 -->
                </div>
                <div style="border-top: 1px solid #e5e7eb; margin-top: 20px; padding-top: 20px;">
                    <h4>添加新接入点</h4>
                    <div class="row"><input id="new-ep-name" placeholder="接入点名称" /></div>
                    <div class="row"><input id="new-ep-url" placeholder="WebSocket URL (如: wss://api.example.com/mcp)" /></div>
                    <div class="row"><input id="new-ep-enabled" placeholder="enabled true|false" value="true" /></div>
                    <div class="row"><button onclick="addNewEndpoint()">添加接入点</button></div>
                </div>
                <div class="row" style="margin-top: 20px;">
                    <button onclick="closeModal('modal-endpoints')">关闭</button>
                </div>
            </div>
        </div>
        
        <!-- 页脚版权信息 -->
        <div style="text-align: center; margin-top: 30px; padding: 20px; color: rgba(255,255,255,0.8); font-size: 14px;">
            &copy;2025 bxb@pku
        </div>
    </body>
    </html>
    """

    @app.get('/')
    def index():  # type: ignore
        return render_template_string(INDEX_HTML)

    @app.get('/api/status')
    def api_status():  # type: ignore
        try:
            if not os.path.exists(STATUS_FILE):
                return jsonify({"endpoints": {}, "tools": {}})
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get('/api/config')
    def api_config_get():  # type: ignore
        try:
            cfg = load_config()
            return jsonify(cfg)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post('/api/config')
    def api_config_post():  # type: ignore
        try:
            data = request.get_json(force=True, silent=True) or {}
            if not isinstance(data, dict):
                return jsonify({"status": "error", "message": "配置必须是 JSON 对象"}), 400
            # 仅保留已知字段，防止写入多余键
            safe = {
                "mcpServers": data.get("mcpServers", {}),
                "mcpEndpoints": data.get("mcpEndpoints", {})
            }
            path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(safe, f, indent=2, ensure_ascii=False)
            return jsonify({"status": "success", "message": "配置已保存"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post('/api/restart')
    def api_restart():  # type: ignore
        try:
            # 非阻塞地调用 stop/start（仅影响后端 mcp_pipe 子进程，不影响本 Web 进程）
            subprocess.Popen([sys.executable, 'mcptool.py', 'stop'])
            # 稍作延时后再启动
            subprocess.Popen([sys.executable, 'mcptool.py', 'start'])
            return jsonify({"status": "success", "message": "已触发重启（stop->start）"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.get('/api/metrics')
    def api_metrics():  # type: ignore
        try:
            status = {}
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                    status = json.load(f)
            tools_map = status.get('tools', {}) if isinstance(status, dict) else {}
            endpoints_map = status.get('endpoints', {}) if isinstance(status, dict) else {}
            num_servers = len(tools_map)
            num_endpoints = len(endpoints_map)
            total_tools = 0
            for s in tools_map.values():
                arr = (s or {}).get('tools', [])
                if isinstance(arr, list):
                    total_tools += len(arr)
            return jsonify({
                'num_servers': num_servers,
                'num_endpoints': num_endpoints,
                'total_tools': total_tools
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.get('/api/service')
    def api_service():  # type: ignore
        try:
            # 重用 mcptool.py 的 pid 文件约定
            pid_path = os.path.join(os.getcwd(), '.mcp_pid')
            running = False
            if os.path.exists(pid_path):
                try:
                    with open(pid_path, 'r') as f:
                        pid = int(f.read().strip())
                    import psutil
                    psutil.Process(pid)
                    running = True
                except Exception:
                    running = False
            return jsonify({ 'running': running })
        except Exception as e:
            return jsonify({ 'error': str(e) }), 500

    @app.post('/api/start')
    def api_start():  # type: ignore
        try:
            subprocess.Popen([sys.executable, 'mcptool.py', 'start'])
            return jsonify({ 'status': 'success', 'message': '已请求启动' })
        except Exception as e:
            return jsonify({ 'status': 'error', 'message': str(e) }), 500

    @app.post('/api/stop')
    def api_stop():  # type: ignore
        try:
            subprocess.Popen([sys.executable, 'mcptool.py', 'stop'])
            return jsonify({ 'status': 'success', 'message': '已请求停止' })
        except Exception as e:
            return jsonify({ 'status': 'error', 'message': str(e) }), 500

    @app.put('/api/servers/<name>')
    def api_servers_update(name):  # type: ignore
        try:
            if not name:
                return jsonify({"status": "error", "message": "缺少名称"}), 400
            body = request.get_json(force=True, silent=True) or {}
            cfg = load_config() or {}
            servers = dict(cfg.get('mcpServers', {}))
            if name not in servers:
                return jsonify({"status": "error", "message": f"服务器 {name} 不存在"}), 404
            entry = servers[name] or {}
            # 允许更新的字段
            if 'type' in body: entry['type'] = (body.get('type') or '').strip()
            if 'command' in body: entry['command'] = (body.get('command') or '').strip()
            if 'args' in body:
                raw_args = body.get('args')
                if isinstance(raw_args, list):
                    entry['args'] = raw_args
                elif isinstance(raw_args, str):
                    entry['args'] = raw_args.strip().split() if raw_args.strip() else []
            if 'url' in body: entry['url'] = (body.get('url') or '').strip()
            if 'env' in body and isinstance(body.get('env'), dict): entry['env'] = body.get('env')
            if 'disabled' in body: entry['disabled'] = bool(body.get('disabled'))
            servers[name] = entry
            new_cfg = {
                'mcpServers': servers,
                'mcpEndpoints': (cfg.get('mcpEndpoints') or {})
            }
            path = os.environ.get('MCP_CONFIG') or os.path.join(os.getcwd(), 'mcp_config.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            return jsonify({"status": "success", "message": f"服务器 {name} 已更新"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.get('/api/servers')
    def api_servers_get():  # type: ignore
        try:
            cfg = load_config() or {}
            return jsonify({"servers": cfg.get("mcpServers", {})})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post('/api/servers')
    def api_servers_add():  # type: ignore
        try:
            body = request.get_json(force=True, silent=True) or {}
            name = (body.get('name') or '').strip()
            typ = (body.get('type') or '').strip().lower()
            if not name or not typ:
                return jsonify({"status": "error", "message": "name 与 type 必填"}), 400
            cfg = load_config() or {}
            servers = dict(cfg.get('mcpServers', {}))
            if name in servers:
                return jsonify({"status": "error", "message": f"服务器 {name} 已存在"}), 400
            entry = {"type": typ}
            if 'disabled' in body: entry['disabled'] = bool(body.get('disabled'))
            env = body.get('env') or {}
            if isinstance(env, dict) and env: entry['env'] = env
            if typ == 'stdio':
                cmd = (body.get('command') or '').strip()
                if not cmd:
                    return jsonify({"status": "error", "message": "stdio 类型需提供 command"}), 400
                entry['command'] = cmd
                raw_args = body.get('args')
                if isinstance(raw_args, list):
                    entry['args'] = raw_args
                elif isinstance(raw_args, str) and raw_args.strip():
                    entry['args'] = raw_args.strip().split()
                else:
                    entry['args'] = []
            elif typ in ('sse', 'http', 'streamablehttp'):
                url = (body.get('url') or '').strip()
                if not url:
                    return jsonify({"status": "error", "message": f"{typ} 类型需提供 url"}), 400
                entry['url'] = url
            else:
                return jsonify({"status": "error", "message": f"不支持的类型: {typ}"}), 400
            servers[name] = entry
            new_cfg = {
                "mcpServers": servers,
                "mcpEndpoints": (cfg.get('mcpEndpoints') or {})
            }
            path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            return jsonify({"status": "success", "message": f"服务器 {name} 已添加"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post('/api/servers/json')
    def api_servers_add_json():  # type: ignore
        try:
            body = request.get_json(force=True, silent=True) or {}
            incoming = body.get('mcpServers') or {}
            if not isinstance(incoming, dict):
                return jsonify({"status": "error", "message": "mcpServers 必须为对象"}), 400
            cfg = load_config() or {}
            servers = dict(cfg.get('mcpServers', {}))
            servers.update(incoming)
            new_cfg = {
                "mcpServers": servers,
                "mcpEndpoints": (cfg.get('mcpEndpoints') or {})
            }
            path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            return jsonify({"status": "success", "message": "mcpServers 已合并"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.delete('/api/servers/<name>')
    def api_servers_delete(name):  # type: ignore
        try:
            if not name:
                return jsonify({"status": "error", "message": "缺少名称"}), 400
            cfg = load_config() or {}
            servers = dict(cfg.get('mcpServers', {}))
            if name not in servers:
                return jsonify({"status": "error", "message": f"服务器 {name} 不存在"}), 404
            del servers[name]
            new_cfg = {
                "mcpServers": servers,
                "mcpEndpoints": (cfg.get('mcpEndpoints') or {})
            }
            path = os.environ.get("MCP_CONFIG") or os.path.join(os.getcwd(), "mcp_config.json")
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            return jsonify({"status": "success", "message": f"服务器 {name} 已删除"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return app

if __name__ == "__main__":
    # 初始化状态文件
    init_status_file()
    
    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    # Get endpoint from environment variable (optional). If missing, we'll try config-driven mode.
    endpoint_url = os.environ.get('MCP_ENDPOINT')
    
    # Determine target: default to all if no arg; single target otherwise
    target_arg = sys.argv[1] if len(sys.argv) >= 2 else None

    async def _main():
        if not target_arg:
            cfg = load_config()
            servers_cfg = (cfg.get("mcpServers") or {})
            endpoints_cfg = (cfg.get("mcpEndpoints") or {})

            enabled_servers = [name for name, entry in servers_cfg.items() if not (entry or {}).get("disabled")]
            enabled_endpoints = {name: ep for name, ep in endpoints_cfg.items() if (ep or {}).get('enabled', True) and ep.get('url')}

            # Control whether to connect to all endpoints or only a primary one
            # Priority: explicit flag in mcp_config.json, then environment variable MCP_CONNECT_ALL
            cfg_for_flag = cfg if isinstance(cfg, dict) else {}
            connect_all_flag = None
            try:
                # allow several key names for backward/forward compatibility
                if 'connect_all_endpoints' in cfg_for_flag:
                    connect_all_flag = bool(cfg_for_flag.get('connect_all_endpoints'))
                elif 'connectAllEndpoints' in cfg_for_flag:
                    connect_all_flag = bool(cfg_for_flag.get('connectAllEndpoints'))
                elif isinstance(cfg_for_flag.get('mcpSettings'), dict) and 'connect_all_endpoints' in cfg_for_flag.get('mcpSettings'):
                    connect_all_flag = bool(cfg_for_flag.get('mcpSettings').get('connect_all_endpoints'))
            except Exception:
                connect_all_flag = None

            if connect_all_flag is None:
                MCP_CONNECT_ALL = os.environ.get('MCP_CONNECT_ALL', '0').strip().lower() in ('1', 'true', 'yes')
            else:
                MCP_CONNECT_ALL = bool(connect_all_flag)

            if not enabled_servers:
                raise RuntimeError("No enabled mcpServers found in config")
            if not enabled_endpoints:
                # Fall back to MCP_ENDPOINT env var behavior when no endpoints configured
                if endpoint_url:
                    logger.info(f"Starting servers on single endpoint (env): {endpoint_url}")
                    tasks = [asyncio.create_task(connect_with_retry(endpoint_url, t)) for t in enabled_servers]
                    await asyncio.gather(*tasks)
                else:
                    raise RuntimeError("未在 mcp_config.json 启用任何端点，且未设置 MCP_ENDPOINT。请在配置中添加 mcpEndpoints，或设置 MCP_ENDPOINT。")

            # Create connection tasks
            tasks = []
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
            if not MCP_CONNECT_ALL:
                # Use only the first enabled endpoint to avoid spawning duplicate server instances
                first_ep = next(iter(enabled_endpoints.items()), None)
                if not first_ep:
                    raise RuntimeError("No enabled mcpEndpoints found in config and MCP_ENDPOINT not set")
                ep_name, ep = first_ep
                url = ep.get('url')
                logger.info(f"MCP_CONNECT_ALL disabled: connecting all servers to single endpoint '{ep_name}'")
                for server_name in enabled_servers:
                    target = f"{server_name}::{ep_name}"
                    tasks.append(asyncio.create_task(connect_with_retry(url, target, semaphore)))
            else:
                # Connect every server to every enabled endpoint when connect_all_endpoints=true
                ep_names = list(enabled_endpoints.keys())
                if not ep_names:
                    raise RuntimeError("No enabled mcpEndpoints found in config and MCP_ENDPOINT not set")
                # Stagger connections in small batches to avoid overload
                batch_size = int(os.environ.get('MCP_CONNECT_BATCH_SIZE', '4'))
                delay_between_batches = float(os.environ.get('MCP_CONNECT_BATCH_DELAY', '0.8'))
                pending = []
                for server_name in enabled_servers:
                    for ep_name in ep_names:
                        url = enabled_endpoints[ep_name].get('url')
                        target = f"{server_name}::{ep_name}"
                        pending.append((url, target))

                async def _schedule_in_batches():
                    for i in range(0, len(pending), batch_size):
                        batch = pending[i:i+batch_size]
                        for url, target in batch:
                            tasks.append(asyncio.create_task(connect_with_retry(url, target, semaphore)))
                        if i + batch_size < len(pending):
                            await asyncio.sleep(delay_between_batches)

                await _schedule_in_batches()
            logger.info(f"Starting {len(tasks)} server-endpoint connections")
            await asyncio.gather(*tasks)
        else:
            if os.path.exists(target_arg):
                # Prefer env endpoint; if missing, try the first enabled endpoint in config
                if not endpoint_url:
                    cfg = load_config()
                    endpoints_cfg = (cfg.get("mcpEndpoints") or {})
                    first_enabled = next((ep.get('url') for ep in endpoints_cfg.values() if (ep or {}).get('enabled', True) and ep.get('url')), None)
                    if not first_enabled:
                        raise RuntimeError("未找到可用端点：未设置 MCP_ENDPOINT 且配置中无启用端点")
                    endpoint_url_to_use = first_enabled
                else:
                    endpoint_url_to_use = endpoint_url
                await connect_with_retry(endpoint_url_to_use, target_arg)
            else:
                logger.error("Argument must be a local Python script path. To run configured servers, run without arguments.")
                sys.exit(1)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Program interrupted by user")
    except Exception as e:
        logger.error(f"Program execution error: {e}")