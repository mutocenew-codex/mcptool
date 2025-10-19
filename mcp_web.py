from flask import Flask, render_template_string, request, jsonify
import json
import os
import subprocess
import psutil
import signal
import asyncio
import websockets
from datetime import datetime
import threading
from collections import defaultdict

app = Flask(__name__)

# 配置文件路径
CONFIG_PATH = os.path.join(os.getcwd(), "mcp_config.json")
PID_FILE = os.path.join(os.getcwd(), ".mcp_pid")
BACKUP_DIR = os.path.join(os.getcwd(), "config_backups")
STATUS_FILE = os.path.join(os.getcwd(), ".mcp_status.json")  # 状态共享文件

# 确保目录存在
os.makedirs(BACKUP_DIR, exist_ok=True)

# 状态缓存与锁
status_lock = threading.Lock()
TOOL_STATUSES = defaultdict(lambda: {"status": "未运行", "error": "", "last_check": None})
ENDPOINT_STATUSES = defaultdict(lambda: {"connected": False, "error": "", "last_check": None})


# ------------------------------
# 状态文件管理
# ------------------------------
def init_status_file():
    """初始化状态共享文件，确保结构正确"""
    try:
        if not os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "endpoints": {},  # 端点心跳状态
                    "tools": {}       # 工具运行状态
                }, f, indent=2)
        else:
            # 检查现有文件结构，修复可能的损坏
            with open(STATUS_FILE, "r+", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                    if "endpoints" not in data:
                        data["endpoints"] = {}
                    if "tools" not in data:
                        data["tools"] = {}
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()
                except json.JSONDecodeError:
                    # 文件损坏，重新初始化
                    f.seek(0)
                    json.dump({"endpoints": {}, "tools": {}}, f, indent=2)
                    f.truncate()
    except Exception as e:
        print(f"初始化状态文件失败: {e}")


def read_status():
    """读取状态文件，确保返回有效结构"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 确保返回结构完整
            return {
                "endpoints": data.get("endpoints", {}),
                "tools": data.get("tools", {})
            }
    except Exception as e:
        print(f"读取状态文件失败: {e}")
        return {"endpoints": {}, "tools": {}}


def update_status(category, name, data):
    """更新状态文件"""
    status = read_status()
    if category not in status:
        status[category] = {}
    # 合并现有数据与新数据
    status[category][name] = {** status[category].get(name, {}), **data, 
                             "last_updated": datetime.now().isoformat()}
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except Exception as e:
        print(f"更新状态文件失败: {e}")


# ------------------------------
# 端点心跳检测
# ------------------------------
async def endpoint_heartbeat(url, name):
    """持续发送心跳检测端点连接状态"""
    while True:
        try:
            async with websockets.connect(url, ping_interval=10, ping_timeout=5) as websocket:
                update_status("endpoints", name, {
                    "connected": True,
                    "error": "",
                    "last_heartbeat": datetime.now().isoformat()
                })
                # 保持连接并定期发送心跳
                while True:
                    await asyncio.sleep(5)
                    try:
                        await websocket.ping()
                        await asyncio.wait_for(websocket.pong(), timeout=5)
                        update_status("endpoints", name, {
                            "connected": True,
                            "last_heartbeat": datetime.now().isoformat()
                        })
                    except Exception as e:
                        raise e  # 触发重连
        except Exception as e:
            update_status("endpoints", name, {
                "connected": False,
                "error": str(e),
                "last_heartbeat": datetime.now().isoformat()
            })
            await asyncio.sleep(5)  # 重试间隔


def start_endpoint_monitors():
    """启动所有启用端点的心跳检测线程"""
    config = load_config()
    endpoints = config.get("mcpEndpoints", {})
    enabled_endpoints = {k: v for k, v in endpoints.items() if v.get("enabled", True)}
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    for name, ep in enabled_endpoints.items():
        loop.create_task(endpoint_heartbeat(ep["url"], name))
    
    threading.Thread(target=loop.run_forever, daemon=True).start()


# ------------------------------
# 工具状态监控
# ------------------------------
def update_tool_statuses():
    """从共享状态文件更新工具状态"""
    global TOOL_STATUSES
    while True:
        status = read_status()
        with status_lock:
            TOOL_STATUSES = {
                name: {
                    "status": tool.get("status", "未运行"),
                    "error": tool.get("error", ""),
                    "last_check": tool.get("last_updated", "")
                } for name, tool in status["tools"].items()
            }
        threading.Event().wait(5)


# ------------------------------
# 配置管理
# ------------------------------
def load_config():
    """加载完整配置（服务器+端点），确保返回有效结构"""
    try:
        if not os.path.exists(CONFIG_PATH):
            return {"mcpServers": {}, "mcpEndpoints": {}}
        
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            # 确保配置结构完整，避免键缺失
            return {
                "mcpServers": config.get("mcpServers", {}),
                "mcpEndpoints": config.get("mcpEndpoints", {})
            }
    except Exception as e:
        print(f"加载配置失败: {e}")
        return {"mcpServers": {}, "mcpEndpoints": {}}


def save_config(config):
    """保存配置文件"""
    try:
        # 确保配置结构完整
        safe_config = {
            "mcpServers": config.get("mcpServers", {}),
            "mcpEndpoints": config.get("mcpEndpoints", {})
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(safe_config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"保存配置失败: {e}")
        return False


# ------------------------------
# 配置备份
# ------------------------------
def backup_config_file():
    if not os.path.exists(CONFIG_PATH):
        return None, "配置文件不存在"
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"mcp_config_{timestamp}.json.bak"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)
        
        with open(CONFIG_PATH, "rb") as src, open(backup_path, "wb") as dst:
            dst.write(src.read())
        return backup_filename, None
    except Exception as e:
        return None, f"备份失败: {str(e)}"


def get_backup_history():
    try:
        backup_files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".bak")]
        backup_files.sort(
            key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
            reverse=True
        )
        
        history = []
        for f in backup_files:
            file_path = os.path.join(BACKUP_DIR, f)
            mod_time = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y-%m-%d %H:%M:%S")
            history.append({"filename": f, "time": mod_time})
        return history
    except Exception as e:
        print(f"获取备份记录失败: {e}")
        return []


# ------------------------------
# 服务状态管理
# ------------------------------
def is_server_running():
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        psutil.Process(pid)
        return True
    except (psutil.NoSuchProcess, ValueError, IOError):
        return False


# ------------------------------
# Web界面模板（修正变量引用）
# ------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>MCP工具管理中心</title>
    <style>
        /* 基础样式 */
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 1200px; 
            margin: 0 auto; 
            padding: 20px; 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            min-height: 100vh;
        }

        /* 标题样式 */
        h1 { 
            color: #ffffff; 
            text-align: center; 
            margin: 20px 0 30px; 
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
            font-size: 2.5em;
            font-weight: 300;
        }
        h2 { 
            color: #2c3e50; 
            border-bottom: 2px solid #e0e0e0; 
            padding-bottom: 10px; 
            margin: 0 0 20px; 
            font-size: 1.4em;
            font-weight: 500;
        }
        h3 { 
            color: #34495e; 
            margin: 15px 0; 
            font-size: 1.1em;
            font-weight: 500;
        }

        /* 工作区容器 */
        .section { 
            background-color: #ffffff;
            margin-bottom: 25px; 
            padding: 30px; 
            border-radius: 16px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.1);
            transition: all 0.3s ease;
            border: 1px solid rgba(255,255,255,0.2);
        }
        .section:hover { 
            transform: translateY(-3px); 
            box-shadow: 0 12px 35px rgba(0,0,0,0.15);
        }

        /* 输入与选择器 */
        input, textarea, select { 
            width: 100%; 
            padding: 12px 16px; 
            margin: 8px 0 15px; 
            box-sizing: border-box; 
            border: 2px solid #e1e5e9;
            border-radius: 10px;
            font-size: 14px;
            transition: all 0.3s ease;
            background: #fafbfc;
        }
        input:focus, textarea:focus, select:focus {
            border-color: #667eea;
            outline: none;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            background: #ffffff;
        }
        textarea { 
            resize: vertical; 
            min-height: 80px; 
            font-family: 'Courier New', monospace;
        }

        /* 按钮样式 */
        button { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; 
            padding: 12px 24px; 
            border: none; 
            border-radius: 10px; 
            cursor: pointer; 
            transition: all 0.3s ease;
            font-size: 14px;
            font-weight: 500;
            margin: 5px 8px 5px 0;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
        }
        button:hover { 
            transform: translateY(-2px); 
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
        }
        button.stop { 
            background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
            box-shadow: 0 4px 15px rgba(231, 76, 60, 0.3);
        }
        button.stop:hover { 
            box-shadow: 0 6px 20px rgba(231, 76, 60, 0.4);
        }
        button.backup { 
            background: linear-gradient(135deg, #f39c12 0%, #d35400 100%);
            box-shadow: 0 4px 15px rgba(243, 156, 18, 0.3);
        }
        button.backup:hover { 
            box-shadow: 0 6px 20px rgba(243, 156, 18, 0.4);
        }
        button:active { 
            transform: translateY(0px); 
        }

        /* 配置项样式 */
        .config-entry { 
            margin: 15px 0; 
            padding: 18px; 
            background: #f9f9f9; 
            border-radius: 8px; 
            border: 1px solid #f0f0f0;
        }
        .entry-header { 
            display: flex; 
            align-items: center; 
            gap: 10px; 
            margin-bottom: 15px; 
        }
        .entry-header h3 { margin: 0; flex-grow: 1; }

        /* 状态标签 */
        .status-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            margin-left: 10px;
        }
        .status-connected { background-color: #d5f5e3; color: #27ae60; }
        .status-disconnected { background-color: #fadbd8; color: #e74c3c; }
        .status-running { background-color: #d5f5e3; color: #27ae60; }
        .status-stopped { background-color: #f5e8c8; color: #d35400; }
        .status-error { background-color: #fadbd8; color: #c0392b; }

        /* 按钮组与间距 */
        .button-group { 
            margin-top: 20px; 
            padding-top: 15px;
            border-top: 1px dashed #f0f0f0;
        }

        /* 消息提示 */
        .message { 
            margin: 12px 0; 
            padding: 12px; 
            border-radius: 6px; 
            font-size: 14px;
            display: none;
        }
        .success { background-color: #eafaf1; color: #27ae60; border: 1px solid #d5f5e3; display: block; }
        .error { background-color: #fdedeb; color: #e74c3c; border: 1px solid #fadbd8; display: block; }

        /* 错误日志区域 */
        .error-log {
            margin: 10px 0;
            padding: 10px;
            background-color: #fdf2f2;
            border-left: 3px solid #e74c3c;
            border-radius: 4px;
            font-size: 13px;
            color: #5d0e0e;
            max-height: 100px;
            overflow-y: auto;
        }

        /* 备份记录 */
        .backup-history {
            margin-top: 10px;
            padding: 10px;
            background: #f9f9f9;
            border-radius: 6px;
            max-height: 150px;
            overflow-y: auto;
        }
        .backup-item {
            font-size: 13px;
            padding: 4px 0;
            border-bottom: 1px dashed #eee;
        }

        /* 复选框优化 */
        input[type="checkbox"] {
            width: auto;
            margin-right: 8px;
            transform: scale(1.1);
        }
        label { color: #555; font-size: 14px; }

        /* 工具列表样式 - 标签式布局 */
        .tools-section {
            margin: 15px 0;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
            border-left: 4px solid #4a90e2;
        }
        .tools-section h4 {
            margin: 0 0 15px 0;
            color: #2c3e50;
            font-size: 16px;
            font-weight: 600;
        }
        .tools-list {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            max-height: 200px;
            overflow-y: auto;
        }
        .tool-tag {
            display: inline-block;
            padding: 6px 12px;
            background: #ffffff;
            border: 1px solid #dee2e6;
            border-radius: 20px;
            font-size: 13px;
            color: #495057;
            font-weight: 500;
            transition: all 0.2s ease;
            cursor: default;
        }
        .tool-tag:hover {
            background: #e9ecef;
            border-color: #adb5bd;
            transform: translateY(-1px);
        }
        .tool-tag.has-description {
            position: relative;
        }
        .tool-tag.has-description:hover::after {
            content: attr(data-description);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            white-space: nowrap;
            z-index: 1000;
            margin-bottom: 5px;
        }
        .tool-tag.has-description:hover::before {
            content: '';
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            border: 4px solid transparent;
            border-top-color: #333;
            z-index: 1000;
        }
        
        /* 导入区域样式 */
        .import-section {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            border: 1px solid #e1e5e9;
        }
        
        .import-controls {
            margin-bottom: 15px;
        }
        
        .import-controls textarea {
            font-family: 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.4;
            background: #ffffff;
            border: 2px solid #e1e5e9;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 10px;
        }
        
        .import-controls textarea:focus {
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .import-buttons {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        
        .import-buttons button {
            margin: 0;
            padding: 10px 20px;
            font-size: 13px;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        
        .btn-warning {
            background: linear-gradient(135deg, #f39c12 0%, #e67e22 100%);
        }
        
        .btn-secondary {
            background: linear-gradient(135deg, #95a5a6 0%, #7f8c8d 100%);
        }
        
        .import-result {
            margin-top: 15px;
            padding: 12px;
            border-radius: 6px;
            font-weight: 500;
            display: none;
        }
        
        .import-result.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
            display: block;
        }
        
        .import-result.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
            display: block;
        }
        
        .import-result.warning {
            background: #fff3cd;
            color: #856404;
            border: 1px solid #ffeaa7;
            display: block;
        }
        
        /* 服务统计样式 */
        .stats-section {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 15px;
        }
        .stat-item {
            text-align: center;
            padding: 15px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            backdrop-filter: blur(10px);
        }
        .stat-number {
            font-size: 28px;
            font-weight: bold;
            margin-bottom: 5px;
        }
        .stat-label {
            font-size: 14px;
            opacity: 0.9;
        }
    </style>
</head>
<body>
    <h1>MCP工具管理中心</h1>
    
    <!-- 服务统计区域 -->
    <div class="stats-section">
        <h2 style="margin: 0 0 10px 0; color: white;">平台服务概览</h2>
        <div class="stats-grid">
            <div class="stat-item">
                <div class="stat-number" id="total-servers">0</div>
                <div class="stat-label">MCP服务器</div>
            </div>
            <div class="stat-item">
                <div class="stat-number" id="total-tools">0</div>
                <div class="stat-label">可用工具</div>
            </div>
            <div class="stat-item">
                <div class="stat-number" id="running-servers">0</div>
                <div class="stat-label">运行中</div>
            </div>
            <div class="stat-item">
                <div class="stat-number" id="total-endpoints">0</div>
                <div class="stat-label">连接端点</div>
            </div>
        </div>
    </div>
    
    <div class="section">
        <h2>服务状态</h2>
        <p id="status-display">当前状态: {{ status }}</p>
        <button id="service-button" onclick="startServer()">启动服务</button>
    </div>
    
    <!-- 多MCP端点配置 -->
    <div class="section">
        <h2>MCP端点配置</h2>
        <div id="endpoints">
            {% for endpoint_name, endpoint in endpoints.items() %}
            <div class="config-entry">
                <div class="entry-header">
                    <label>
                        <input type="checkbox" class="endpoint-enabled" data-name="{{ endpoint_name }}" {% if endpoint.enabled %}checked{% endif %}>
                        启用
                    </label>
                    <h3>
                        {{ endpoint_name }}
                        <span class="status-badge {% if endpoint_status.get(endpoint_name, {}).connected %}status-connected{% else %}status-disconnected{% endif %}">
                            {% if endpoint_status.get(endpoint_name, {}).connected %}已连接{% else %}未连接{% endif %}
                        </span>
                    </h3>
                    <button onclick="removeEndpoint('{{ endpoint_name }}')">删除</button>
                </div>
                
                <label>WebSocket地址:</label>
                <input type="text" class="endpoint-url" data-name="{{ endpoint_name }}" value="{{ endpoint.url }}">
                
                {% if not endpoint_status.get(endpoint_name, {}).connected %}
                <div class="error-log">
                    错误: {{ endpoint_status.get(endpoint_name, {}).error or '未尝试连接' }}
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        
        <div class="button-group">
            <h3>添加新端点</h3>
            <input type="text" id="new-endpoint-name" placeholder="端点名称（如：主服务器）">
            <input type="text" id="new-endpoint-url" placeholder="WebSocket地址（如：ws://localhost:8080/mcp）">
            <button onclick="addEndpoint()">添加端点</button>
            <button onclick="saveEndpointsConfig()">保存端点配置</button>
            <div id="endpoints-config-message" class="message"></div>
        </div>
    </div>
    
    <div class="section">
        <h2>服务器配置</h2>
        
        <!-- 快速导入区域 -->
        <div class="import-section">
            <h3>快速导入服务器</h3>
            <div class="import-controls">
                <textarea id="importData" placeholder="粘贴JSON格式的服务器配置数据，例如：&#10;{&#10;  &quot;mcpServers&quot;: {&#10;    &quot;fetch&quot;: {&#10;      &quot;type&quot;: &quot;sse&quot;,&#10;      &quot;url&quot;: &quot;https://mcp.api-inference.modelscope.net/ba2d328c5a9c44/sse&quot;&#10;    }&#10;  }&#10;}" rows="8"></textarea>
                <div class="import-buttons">
                    <button onclick="importServers()" class="btn btn-primary">导入服务器</button>
                    <button onclick="importServersForce()" class="btn btn-warning">强制导入（覆盖）</button>
                    <button onclick="clearImportData()" class="btn btn-secondary">清空</button>
                </div>
            </div>
            <div id="importResult" class="import-result"></div>
        </div>
        
        <!-- 配置备份区域 -->
        <div class="button-group">
            <h3>配置备份</h3>
            <button class="backup" onclick="backupConfig()">备份当前配置</button>
            <div id="backup-message" class="message"></div>
            
            <div class="backup-history">
                <strong>最近备份:</strong>
                <div id="backup-list">
                    <!-- 备份记录将通过JS动态加载 -->
                </div>
            </div>
        </div>
        
        <div id="servers">
            {% for server_name, config in servers.items() %}
            <div class="config-entry">
                <div class="entry-header">
                    <label>
                        <input type="checkbox" class="server-enabled" data-name="{{ server_name }}" {% if not config.disabled %}checked{% endif %}>
                        启用
                    </label>
                    <h3>
                        {{ server_name }}
                        <span class="status-badge 
                            {% if tool_status.get(server_name, {}).status == '运行中' %}status-running
                            {% elif tool_status.get(server_name, {}).status == '错误' %}status-error
                            {% else %}status-stopped{% endif %}">
                            {{ tool_status.get(server_name, {}).status or '未运行' }}
                        </span>
                    </h3>
                    <button onclick="removeServer('{{ server_name }}')">删除</button>
                </div>
                
                <label>类型:</label>
                <select class="server-type" data-name="{{ server_name }}">
                    <option value="stdio" {% if config.type == "stdio" %}selected{% endif %}>stdio</option>
                    <option value="sse" {% if config.type == "sse" %}selected{% endif %}>sse</option>
                    <option value="http" {% if config.type == "http" %}selected{% endif %}>http</option>
                </select>
                
                {% if config.type == "stdio" %}
                <label>命令:</label>
                <input type="text" class="server-command" data-name="{{ server_name }}" value="{{ config.command or '' }}">
                <label>参数:</label>
                <input type="text" class="server-args" data-name="{{ server_name }}" value="{{ ' '.join(config.args or []) }}">
                {% else %}
                <label>URL:</label>
                <input type="text" class="server-url" data-name="{{ server_name }}" value="{{ config.url or '' }}">
                {% endif %}
                
                <label>环境变量 (JSON格式):</label>
                <textarea class="server-env" data-name="{{ server_name }}" rows="2">{{ config.env | default({}) | tojson }}</textarea>
                
                <!-- 工具列表显示 -->
                {% if tool_status.get(server_name, {}).tools %}
                <div class="tools-section">
                    <h4>可用工具 ({{ tool_status.get(server_name, {}).tools | length }}个):</h4>
                    <div class="tools-list">
                        {% for tool in tool_status.get(server_name, {}).tools %}
                        <span class="tool-tag {% if tool.description %}has-description{% endif %}" 
                              {% if tool.description %}data-description="{{ tool.description }}"{% endif %}>
                            {{ tool.name }}
                        </span>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
                
                {% if tool_status.get(server_name, {}).status == '错误' %}
                <div class="error-log">
                    错误: {{ tool_status.get(server_name, {}).error or '未知错误' }}
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        
        <div class="button-group">
            <h3>添加新服务器</h3>
            <input type="text" id="new-server-name" placeholder="服务器名称">
            <select id="new-server-type">
                <option value="stdio">stdio</option>
                <option value="sse">sse</option>
                <option value="http">http</option>
            </select>
            <button onclick="addServer()">添加服务器</button>
        </div>
        
        <div class="button-group">
            <button onclick="saveServersConfig()">保存服务器配置</button>
            <div id="servers-config-message" class="message"></div>
        </div>
    </div>

    <script>
        const serviceButton = document.getElementById('service-button');
        
        // 显示消息提示
        function showMessage(elementId, text, isError = false) {
            const element = document.getElementById(elementId);
            element.textContent = text;
            element.className = 'message ' + (isError ? 'error' : 'success');
            setTimeout(() => { element.className = 'message'; }, 3000);
        }
        
        // 加载备份记录
        function loadBackupHistory() {
            fetch('/backup-history')
                .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                .then(data => {
                    const list = document.getElementById('backup-list');
                    list.innerHTML = data.backups.length ? 
                        data.backups.slice(0,5).map(b => `<div class="backup-item">${b.time} - ${b.filename}</div>`).join('') :
                        '<div class="backup-item">暂无备份记录</div>';
                })
                .catch(err => console.error('备份记录加载失败:', err));
        }
        
        // 更新服务状态显示
        function updateStatusDisplay(status) {
            document.getElementById('status-display').textContent = `当前状态: ${status}`;
            if (status === "运行中") {
                serviceButton.textContent = "停止服务";
                serviceButton.classList.add('stop');
                serviceButton.onclick = stopServer;
            } else {
                serviceButton.textContent = "启动服务";
                serviceButton.classList.remove('stop');
                serviceButton.onclick = startServer;
            }
        }
        
        // 更新端点状态显示
        function updateEndpointStatus() {
            fetch('/endpoint-status')
                .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                .then(data => {
                    Object.entries(data).forEach(([name, status]) => {
                        const badge = document.querySelector(`.endpoint-url[data-name="${name}"]`).closest('.config-entry')?.querySelector('.status-badge');
                        const errorLog = document.querySelector(`.endpoint-url[data-name="${name}"]`).closest('.config-entry')?.querySelector('.error-log');
                        
                        if (badge) {
                            badge.className = `status-badge ${status.connected ? 'status-connected' : 'status-disconnected'}`;
                            badge.textContent = status.connected ? '已连接' : '未连接';
                        }
                        if (errorLog) {
                            errorLog.textContent = `错误: ${status.error || '未尝试连接'}`;
                        }
                    });
                })
                .catch(err => console.error('端点状态更新失败:', err));
        }
        
        // 更新工具状态显示
        function updateToolStatus() {
            fetch('/tool-status')
                .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                .then(data => {
                    Object.entries(data).forEach(([name, status]) => {
                        const entry = document.querySelector(`.server-type[data-name="${name}"]`).closest('.config-entry');
                        if (!entry) return;
                        
                        const badge = entry.querySelector('.status-badge');
                        const errorLog = entry.querySelector('.error-log');
                        const toolsSection = entry.querySelector('.tools-section');
                        
                        if (badge) {
                            badge.className = `status-badge 
                                ${status.status === '运行中' ? 'status-running' : 
                                  status.status === '错误' ? 'status-error' : 'status-stopped'}`;
                            badge.textContent = status.status;
                        }
                        if (errorLog) {
                            errorLog.textContent = `错误: ${status.error || '未知错误'}`;
                        }
                        
                        // 更新工具列表
                        updateToolsList(entry, status.tools || []);
                    });
                })
                .catch(err => console.error('工具状态更新失败:', err));
        }
        
        // 更新工具列表显示
        function updateToolsList(entry, tools) {
            let toolsSection = entry.querySelector('.tools-section');
            
            if (tools.length > 0) {
                if (!toolsSection) {
                    // 创建工具列表区域
                    toolsSection = document.createElement('div');
                    toolsSection.className = 'tools-section';
                    toolsSection.innerHTML = '<h4>可用工具:</h4><div class="tools-list"></div>';
                    
                    // 插入到环境变量输入框之后
                    const envTextarea = entry.querySelector('.server-env');
                    envTextarea.parentNode.insertBefore(toolsSection, envTextarea.nextSibling);
                }
                
                const toolsList = toolsSection.querySelector('.tools-list');
                toolsList.innerHTML = tools.map(tool => `
                    <span class="tool-tag ${tool.description ? 'has-description' : ''}" 
                          ${tool.description ? `data-description="${tool.description}"` : ''}>
                        ${tool.name || '未知工具'}
                    </span>
                `).join('');
                
                // 更新工具数量
                const h4 = toolsSection.querySelector('h4');
                h4.textContent = `可用工具 (${tools.length}个):`;
            } else if (toolsSection) {
                // 如果没有工具，移除工具列表区域
                toolsSection.remove();
            }
        }
        
        // 更新统计信息
        function updateStats() {
            // 获取配置中的服务器数量
            fetch('/config-info')
                .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                .then(configData => {
                    const totalServers = configData.servers || 0;
                    const totalEndpoints = configData.endpoints || 0;
                    
                    // 获取工具状态
                    fetch('/tool-status')
                        .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                        .then(data => {
                            let totalTools = 0;
                            let runningServers = 0;
                            
                            Object.entries(data).forEach(([name, info]) => {
                                if (info.tools && Array.isArray(info.tools)) {
                                    totalTools += info.tools.length;
                                }
                                if (info.status === '运行中') {
                                    runningServers++;
                                }
                            });
                            
                            // 更新显示
                            document.getElementById('total-servers').textContent = totalServers;
                            document.getElementById('total-tools').textContent = totalTools;
                            document.getElementById('running-servers').textContent = runningServers;
                            document.getElementById('total-endpoints').textContent = totalEndpoints;
                        })
                        .catch(err => console.error('工具状态获取失败:', err));
                })
                .catch(err => console.error('配置信息获取失败:', err));
        }
        
        // 服务控制
        function getStatus() {
            fetch('/status')
                .then(res => res.ok ? res.json() : Promise.reject('获取失败'))
                .then(data => updateStatusDisplay(data.status))
                .catch(err => console.error('服务状态获取失败:', err));
        }
        
        function startServer() {
            fetch('/start', { method: 'POST' })
                .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
                .then(() => {
                    getStatus();
                    setTimeout(getStatus, 1000);
                })
                .catch(err => {
                    alert('启动失败: ' + err);
                    getStatus();
                });
        }
        
        function stopServer() {
            fetch('/stop', { method: 'POST' })
                .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
                .then(() => {
                    getStatus();
                    setTimeout(getStatus, 1000);
                })
                .catch(err => {
                    alert('停止失败: ' + err);
                    getStatus();
                });
        }
        
        // 导入服务器功能
        function importServers() {
            const importData = document.getElementById('importData').value.trim();
            if (!importData) {
                showImportResult('请输入要导入的JSON数据', 'error');
                return;
            }
            
            try {
                const data = JSON.parse(importData);
                if (!data.mcpServers) {
                    showImportResult('JSON格式错误：缺少mcpServers字段', 'error');
                    return;
                }
                
                fetch('/import-servers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                })
                .then(res => res.json())
                .then(result => {
                    if (result.status === 'success') {
                        showImportResult(result.message, 'success');
                        clearImportData();
                        // 刷新页面以显示新导入的服务器
                        setTimeout(() => {
                            location.reload();
                        }, 1500);
                    } else if (result.status === 'warning') {
                        showImportResult(`${result.message} 点击"强制导入"按钮覆盖现有配置`, 'warning');
                    } else {
                        showImportResult(result.message, 'error');
                    }
                })
                .catch(err => {
                    showImportResult('导入失败: ' + err, 'error');
                });
            } catch (e) {
                showImportResult('JSON格式错误: ' + e.message, 'error');
            }
        }
        
        function importServersForce() {
            const importData = document.getElementById('importData').value.trim();
            if (!importData) {
                showImportResult('请输入要导入的JSON数据', 'error');
                return;
            }
            
            try {
                const data = JSON.parse(importData);
                if (!data.mcpServers) {
                    showImportResult('JSON格式错误：缺少mcpServers字段', 'error');
                    return;
                }
                
                fetch('/import-servers-force', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                })
                .then(res => res.json())
                .then(result => {
                    if (result.status === 'success') {
                        showImportResult(result.message, 'success');
                        clearImportData();
                        // 刷新页面以显示新导入的服务器
                        setTimeout(() => {
                            location.reload();
                        }, 1500);
                    } else {
                        showImportResult(result.message, 'error');
                    }
                })
                .catch(err => {
                    showImportResult('强制导入失败: ' + err, 'error');
                });
            } catch (e) {
                showImportResult('JSON格式错误: ' + e.message, 'error');
            }
        }
        
        function clearImportData() {
            document.getElementById('importData').value = '';
            document.getElementById('importResult').style.display = 'none';
        }
        
        function showImportResult(message, type) {
            const resultDiv = document.getElementById('importResult');
            resultDiv.textContent = message;
            resultDiv.className = `import-result ${type}`;
            resultDiv.style.display = 'block';
        }
        
        // 服务器配置管理
        function saveServersConfig() {
            const servers = {};
            let isValid = true;
            
            document.querySelectorAll('.server-type').forEach(el => {
                const name = el.dataset.name;
                const entry = el.closest('.config-entry');
                const type = el.value;
                const enabled = entry.querySelector('.server-enabled').checked;
                const envText = entry.querySelector('.server-env').value;
                
                servers[name] = { type, disabled: !enabled };
                
                // 环境变量验证
                try {
                    servers[name].env = envText ? JSON.parse(envText) : {};
                } catch (e) {
                    showMessage('servers-config-message', `服务器 ${name} 环境变量格式错误: ${e.message}`, true);
                    isValid = false;
                    return;
                }
                
                // 类型特有配置验证
                if (type === 'stdio') {
                    servers[name].command = entry.querySelector('.server-command').value || '';
                    servers[name].args = entry.querySelector('.server-args').value.split(' ').filter(x => x);
                    if (!servers[name].command) {
                        showMessage('servers-config-message', `服务器 ${name} 命令不能为空`, true);
                        isValid = false;
                    }
                } else {
                    servers[name].url = entry.querySelector('.server-url').value || '';
                    if (!servers[name].url) {
                        showMessage('servers-config-message', `服务器 ${name} URL不能为空`, true);
                        isValid = false;
                    }
                }
            });
            
            if (!isValid) return;
            
            fetch('/save-servers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(servers)
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => showMessage('servers-config-message', '服务器配置已保存'))
            .catch(err => {
                showMessage('servers-config-message', '保存失败: ' + err, true);
                console.error('保存服务器配置错误:', err);
            });
        }
        
        function addServer() {
            const name = document.getElementById('new-server-name').value.trim();
            const type = document.getElementById('new-server-type').value;
            if (!name) {
                alert('请输入服务器名称');
                return;
            }
            
            fetch('/add-server', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, type })
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => window.location.reload())
            .catch(err => alert('添加失败: ' + err));
        }
        
        function removeServer(name) {
            if (!confirm(`确定删除服务器 "${name}"?`)) return;
            
            fetch('/remove-server', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => window.location.reload())
            .catch(err => alert('删除失败: ' + err));
        }
        
        // 端点配置管理
        function saveEndpointsConfig() {
            const endpoints = {};
            document.querySelectorAll('.endpoint-url').forEach(el => {
                const name = el.dataset.name;
                const entry = el.closest('.config-entry');
                const url = el.value.trim();
                const enabled = entry.querySelector('.endpoint-enabled').checked;
                endpoints[name] = { url, enabled };
            });
            
            fetch('/save-endpoints', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(endpoints)
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => {
                showMessage('endpoints-config-message', '端点配置已保存，正在重新连接...');
                // 重新启动端点监控
                fetch('/restart-endpoint-monitors', { method: 'POST' })
                    .then(() => {
                        // 延迟更新状态，给监控时间重新连接
                        setTimeout(() => {
                            updateEndpointStatus();
                            updateStats();
                        }, 2000);
                    })
                    .catch(err => console.error('重启端点监控失败:', err));
            })
            .catch(err => {
                showMessage('endpoints-config-message', '保存失败: ' + err, true);
                console.error('保存端点配置错误:', err);
            });
        }
        
        function addEndpoint() {
            const name = document.getElementById('new-endpoint-name').value.trim();
            const url = document.getElementById('new-endpoint-url').value.trim();
            if (!name || !url) {
                alert('名称和URL不能为空');
                return;
            }
            
            fetch('/add-endpoint', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, url })
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => window.location.reload())
            .catch(err => alert('添加失败: ' + err));
        }
        
        function removeEndpoint(name) {
            if (!confirm(`确定删除端点 "${name}"?`)) return;
            
            fetch('/remove-endpoint', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            })
            .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
            .then(() => window.location.reload())
            .catch(err => alert('删除失败: ' + err));
        }
        
        // 备份配置
        function backupConfig() {
            fetch('/backup-config', { method: 'POST' })
                .then(res => res.ok ? res.json() : res.json().then(err => Promise.reject(err.message)))
                .then(data => {
                    showMessage('backup-message', `配置已备份: ${data.filename}`);
                    loadBackupHistory();
                })
                .catch(err => showMessage('backup-message', '备份失败: ' + err, true));
        }
        
        // 初始化与定时更新
        function init() {
            getStatus();
            loadBackupHistory();
            updateEndpointStatus();
            updateToolStatus();
            updateStats();
            
            // 定时刷新状态（5秒一次）
            setInterval(() => {
                getStatus();
                updateEndpointStatus();
                updateToolStatus();
                updateStats();
            }, 5000);
        }
        
        window.onload = init;
    </script>
</body>
</html>
"""


# ------------------------------
# 路由配置
# ------------------------------
@app.route('/')
def index():
    try:
        config = load_config()
        servers = config.get("mcpServers", {})
        endpoints = config.get("mcpEndpoints", {})
        status_data = read_status()  # 从共享文件读取状态
        
        # 确保配置结构完整
        for name in servers:
            servers[name].setdefault("env", {})
            servers[name].setdefault("disabled", False)
            servers[name].setdefault("type", "stdio")  # 默认类型
        for name in endpoints:
            endpoints[name].setdefault("enabled", True)
            endpoints[name].setdefault("url", "")  # 默认空URL
        
        return render_template_string(
            HTML_TEMPLATE,
            servers=servers,
            endpoints=endpoints,
            endpoint_status=status_data["endpoints"],
            tool_status=status_data["tools"],
            status="运行中" if is_server_running() else "已停止"
        )
    except Exception as e:
        # 捕获所有异常，返回友好错误信息
        print(f"首页渲染失败: {e}")
        return f"服务器内部错误: {str(e)}", 500


@app.route('/status')
def status():
    return jsonify({"status": "运行中" if is_server_running() else "已停止"})


@app.route('/start', methods=['POST'])
def start_server():
    try:
        if not is_server_running():
            cmd = ["python", "mcp_pipe.py"]
            process = subprocess.Popen(cmd)
            with open(PID_FILE, "w") as f:
                f.write(str(process.pid))
        return jsonify({"status": "started"})
    except Exception as e:
        print(f"启动服务失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/stop', methods=['POST'])
def stop_server():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            os.remove(PID_FILE)
        return jsonify({"status": "stopped"})
    except Exception as e:
        print(f"停止服务失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# 服务器配置接口
@app.route('/save-servers', methods=['POST'])
def save_servers():
    try:
        servers = request.get_json()
        if not isinstance(servers, dict):
            return jsonify({"status": "error", "message": "无效的配置格式"}), 400
            
        for name in servers:
            servers[name].setdefault("env", {})
            servers[name].setdefault("disabled", False)
            servers[name].setdefault("type", "stdio")
                
        config = load_config()
        config["mcpServers"] = servers
        save_config(config)
        return jsonify({"status": "saved"})
    except Exception as e:
        print(f"保存服务器配置失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/add-server', methods=['POST'])
def add_server():
    try:
        data = request.get_json()
        if not data or "name" not in data or "type" not in data:
            return jsonify({"status": "error", "message": "缺少名称或类型参数"}), 400
            
        config = load_config()
        servers = config.get("mcpServers", {})
        
        if data["name"] in servers:
            return jsonify({"status": "error", "message": "服务器名称已存在"}), 400
        
        servers[data["name"]] = {
            "type": data["type"],
            "disabled": False,
            "env": {}
        }
        config["mcpServers"] = servers
        save_config(config)
        
        return jsonify({"status": "added"})
    except Exception as e:
        print(f"添加服务器失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/remove-server', methods=['POST'])
def remove_server():
    try:
        data = request.get_json()
        if not data or "name" not in data:
            return jsonify({"status": "error", "message": "缺少名称参数"}), 400
            
        config = load_config()
        servers = config.get("mcpServers", {})
        
        if data["name"] in servers:
            del servers[data["name"]]
            config["mcpServers"] = servers
            save_config(config)
        
        return jsonify({"status": "removed"})
    except Exception as e:
        print(f"删除服务器失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# 端点配置接口
@app.route('/save-endpoints', methods=['POST'])
def save_endpoints():
    try:
        endpoints = request.get_json()
        if not isinstance(endpoints, dict):
            return jsonify({"status": "error", "message": "无效的配置格式"}), 400
            
        for name in endpoints:
            endpoints[name].setdefault("enabled", True)
            endpoints[name].setdefault("url", "")
                
        config = load_config()
        config["mcpEndpoints"] = endpoints
        save_config(config)
        return jsonify({"status": "saved"})
    except Exception as e:
        print(f"保存端点配置失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/add-endpoint', methods=['POST'])
def add_endpoint():
    try:
        data = request.get_json()
        if not data or "name" not in data or "url" not in data:
            return jsonify({"status": "error", "message": "缺少名称或URL参数"}), 400
            
        config = load_config()
        endpoints = config.get("mcpEndpoints", {})
        
        if data["name"] in endpoints:
            return jsonify({"status": "error", "message": "端点名称已存在"}), 400
        
        endpoints[data["name"]] = {
            "url": data["url"],
            "enabled": True
        }
        config["mcpEndpoints"] = endpoints
        save_config(config)
        
        return jsonify({"status": "added"})
    except Exception as e:
        print(f"添加端点失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/remove-endpoint', methods=['POST'])
def remove_endpoint():
    try:
        data = request.get_json()
        if not data or "name" not in data:
            return jsonify({"status": "error", "message": "缺少名称参数"}), 400
            
        config = load_config()
        endpoints = config.get("mcpEndpoints", {})
        
        if data["name"] in endpoints:
            del endpoints[data["name"]]
            config["mcpEndpoints"] = endpoints
            save_config(config)
        
        return jsonify({"status": "removed"})
    except Exception as e:
        print(f"删除端点失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# 状态查询接口
@app.route('/endpoint-status')
def endpoint_status():
    status = read_status()
    return jsonify({
        name: {
            "connected": ep.get("connected", False),
            "error": ep.get("error", ""),
            "last_check": ep.get("last_heartbeat", "")
        } for name, ep in status["endpoints"].items()
    })


@app.route('/tool-status')
def tool_status():
    status = read_status()
    return jsonify({
        name: {
            "status": tool.get("status", "未运行"),
            "error": tool.get("error", ""),
            "last_check": tool.get("last_updated", ""),
            "tools": tool.get("tools", [])
        } for name, tool in status["tools"].items()
    })


@app.route('/server-tools')
def server_tools():
    """获取所有服务器的工具列表"""
    status = read_status()
    return jsonify({
        name: {
            "status": tool.get("status", "未运行"),
            "tools": tool.get("tools", []),
            "error": tool.get("error", ""),
            "last_check": tool.get("last_updated", "")
        } for name, tool in status["tools"].items()
    })


# 备份接口
@app.route('/backup-config', methods=['POST'])
def backup_config():
    filename, error = backup_config_file()
    if error:
        return jsonify({"status": "error", "message": error}), 500
    return jsonify({"status": "success", "filename": filename})


@app.route('/backup-history')
def backup_history():
    history = get_backup_history()
    return jsonify({"backups": history})


@app.route('/restart-endpoint-monitors', methods=['POST'])
def restart_endpoint_monitors():
    """重启端点监控"""
    try:
        # 停止现有监控
        global endpoint_monitor_threads
        if 'endpoint_monitor_threads' in globals():
            for thread in endpoint_monitor_threads:
                if thread.is_alive():
                    # 这里可以添加停止逻辑，但简单起见我们重新启动
                    pass
        
        # 重新启动监控
        start_endpoint_monitors()
        return jsonify({"status": "restarted"})
    except Exception as e:
        print(f"重启端点监控失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/config-info')
def config_info():
    """获取配置信息统计"""
    try:
        config = load_config()
        servers = config.get("mcpServers", {})
        endpoints = config.get("mcpEndpoints", {})
        
        return jsonify({
            "servers": len(servers),
            "endpoints": len(endpoints)
        })
    except Exception as e:
        print(f"获取配置信息失败: {e}")
        return jsonify({"servers": 0, "endpoints": 0})

@app.route('/import-servers', methods=['POST'])
def import_servers():
    """导入MCP服务器配置"""
    try:
        data = request.get_json()
        if not data or 'mcpServers' not in data:
            return jsonify({"status": "error", "message": "无效的导入数据格式"}), 400
        
        # 加载现有配置
        config = load_config()
        if 'mcpServers' not in config:
            config['mcpServers'] = {}
        
        # 导入新服务器
        imported_count = 0
        for server_name, server_config in data['mcpServers'].items():
            if server_name not in config['mcpServers']:
                config['mcpServers'][server_name] = server_config
                imported_count += 1
            else:
                # 如果服务器已存在，询问是否覆盖
                return jsonify({
                    "status": "warning", 
                    "message": f"服务器 '{server_name}' 已存在，是否覆盖？",
                    "conflict": server_name
                }), 409
        
        # 保存配置
        save_config(config)
        
        return jsonify({
            "status": "success", 
            "message": f"成功导入 {imported_count} 个服务器",
            "imported_count": imported_count
        })
        
    except Exception as e:
        print(f"导入服务器失败: {e}")
        return jsonify({"status": "error", "message": f"导入失败: {str(e)}"}), 500

@app.route('/import-servers-force', methods=['POST'])
def import_servers_force():
    """强制导入MCP服务器配置（覆盖已存在的）"""
    try:
        data = request.get_json()
        if not data or 'mcpServers' not in data:
            return jsonify({"status": "error", "message": "无效的导入数据格式"}), 400
        
        # 加载现有配置
        config = load_config()
        if 'mcpServers' not in config:
            config['mcpServers'] = {}
        
        # 强制导入所有服务器（覆盖已存在的）
        imported_count = 0
        for server_name, server_config in data['mcpServers'].items():
            config['mcpServers'][server_name] = server_config
            imported_count += 1
        
        # 保存配置
        save_config(config)
        
        return jsonify({
            "status": "success", 
            "message": f"成功导入 {imported_count} 个服务器",
            "imported_count": imported_count
        })
        
    except Exception as e:
        print(f"强制导入服务器失败: {e}")
        return jsonify({"status": "error", "message": f"导入失败: {str(e)}"}), 500


# ------------------------------
# 启动应用
# ------------------------------
def start_monitor_threads():
    """启动所有监控线程"""
    init_status_file()
    start_endpoint_monitors()
    threading.Thread(target=update_tool_statuses, daemon=True).start()


if __name__ == '__main__':
    # 启动前确保状态文件初始化
    init_status_file()
    start_monitor_threads()
    app.run(host='0.0.0.0', port=6789, debug=True)