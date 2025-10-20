import click
import subprocess
import os
import sys
import json
import signal
import psutil

PID_FILE = os.path.join(os.getcwd(), ".mcp_pid")
WEB_PID_FILE = os.path.join(os.getcwd(), ".mcp_web_pid")
CONFIG_PATH = os.path.join(os.getcwd(), "mcp_config.json")

def is_server_running():
    """检查服务器是否在运行"""
    if not os.path.exists(PID_FILE):
        return False

def is_web_running():
    """检查Web是否在运行"""
    if not os.path.exists(WEB_PID_FILE):
        return False
    try:
        with open(WEB_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        psutil.Process(pid)
        return True
    except (psutil.NoSuchProcess, ValueError, IOError):
        return False

def start_web_process(port: int):
    """后台启动 Web 界面 (Flask) 并记录PID，避免阻塞。"""
    if is_web_running():
        return None
    # 使用 Flask CLI 运行 mcp_pipe:create_app，避免导入副作用
    env = os.environ.copy()
    env['FLASK_APP'] = 'mcp_pipe:create_app'
    env['FLASK_RUN_HOST'] = '0.0.0.0'
    env['FLASK_RUN_PORT'] = str(port)
    # 关闭自动重载，防止生成子进程难以管理
    env['FLASK_RUN_FROM_CLI'] = 'false'
    cmd = [sys.executable, '-m', 'flask', 'run', '--no-reload']
    proc = subprocess.Popen(cmd, env=env)
    with open(WEB_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    return proc.pid
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        psutil.Process(pid)
        return True
    except (psutil.NoSuchProcess, ValueError, IOError):
        return False

@click.group()
def cli():
    """MCP工具命令行接口"""
    pass

@cli.command()
@click.option('--endpoint', help='MCP WebSocket端点地址')
@click.option('--server', help='指定要启动的服务器名称')
def start(endpoint, server):
    """启动MCP服务"""
    if is_server_running():
        click.echo("MCP服务已在运行中")
        # 尝试补启动 Web（如果未运行）
        port = int(os.environ.get('MCP_WEB_PORT', '6789'))
        if not is_web_running():
            wp = start_web_process(port)
            if wp:
                click.echo(f"Web已启动，PID: {wp}，端口: {port}")
        return
    
    # 如果提供了endpoint，更新环境变量
    if endpoint:
        env_path = os.path.join(os.getcwd(), ".env")
        env_lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                env_lines = f.readlines()
        
        with open(env_path, "w") as f:
            found = False
            for line in env_lines:
                if line.startswith("MCP_ENDPOINT="):
                    f.write(f"MCP_ENDPOINT={endpoint}\n")
                    found = True
                else:
                    f.write(line)
            if not found:
                f.write(f"MCP_ENDPOINT={endpoint}\n")
    
    # 构建启动命令
    cmd = ["python", "mcp_pipe.py"]
    if server:
        cmd.append(server)
    
    # Prepare environment for child process; do not override MCP_CONNECT_ALL here.
    # Whether连接所有端点由 mcp_config.json 的 connect_all_endpoints 决定，
    # 环境变量仅在未配置时作为后备。
    child_env = os.environ.copy()

    # 启动进程
    process = subprocess.Popen(cmd, env=child_env)
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))
    
    click.echo(f"MCP服务已启动，PID: {process.pid}")
    # 自动启动 Web（后台）
    port = int(os.environ.get('MCP_WEB_PORT', '6789'))
    wp = start_web_process(port)
    if wp:
        click.echo(f"Web已启动，PID: {wp}，端口: {port}")

@cli.command()
def stop():
    """停止MCP服务"""
    if not is_server_running():
        click.echo("MCP服务未在运行")
    else:
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            os.remove(PID_FILE)
            click.echo("MCP服务已停止")
        except Exception as e:
            click.echo(f"停止服务失败: {str(e)}")

    # 同时停止 Web
    if is_web_running():
        try:
            with open(WEB_PID_FILE, "r") as f:
                wpid = int(f.read().strip())
            os.kill(wpid, signal.SIGTERM)
            os.remove(WEB_PID_FILE)
            click.echo("Web已停止")
        except Exception as e:
            click.echo(f"停止Web失败: {str(e)}")

@cli.command()
def status():
    """查看MCP服务状态"""
    if is_server_running():
        with open(PID_FILE, "r") as f:
            pid = f.read().strip()
        click.echo(f"MCP服务正在运行，PID: {pid}")
    else:
        click.echo("MCP服务未在运行")

@cli.command()
@click.option('--port', default=6789, help='Web界面端口，默认6789')
def web(port):
    """启动Web管理界面"""
    # Use built-in minimal web from mcp_pipe to avoid side effects
    from mcp_pipe import create_app
    app = create_app()
    click.echo(f"Web管理界面已启动，访问 http://localhost:{port}")
    app.run(host='0.0.0.0', port=port)

@cli.command()
@click.argument('name')
@click.option('--type', type=click.Choice(['stdio', 'sse', 'http']), default='stdio', help='服务器类型')
@click.option('--command', help='命令（适用于stdio类型）')
@click.option('--args', help='参数（适用于stdio类型，空格分隔）')
@click.option('--url', help='URL（适用于sse和http类型）')
@click.option('--env', help='环境变量（JSON格式）')
@click.option('--disabled/--enabled', default=False, help='是否禁用')
def add_server(name, type, command, args, url, env, disabled):
    """添加服务器配置"""
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    
    servers = config.get("mcpServers", {})
    
    if name in servers:
        click.echo(f"服务器 {name} 已存在")
        return
    
    server_config = {
        "type": type,
        "disabled": disabled
    }
    
    if env:
        try:
            server_config["env"] = json.loads(env)
        except json.JSONDecodeError:
            click.echo("环境变量格式错误，必须是JSON格式")
            return
    
    if type == "stdio":
        if not command:
            click.echo("stdio类型服务器必须指定--command")
            return
        server_config["command"] = command
        server_config["args"] = args.split() if args else []
    else:
        if not url:
            click.echo(f"{type}类型服务器必须指定--url")
            return
        server_config["url"] = url
    
    servers[name] = server_config
    config["mcpServers"] = servers
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    
    click.echo(f"服务器 {name} 已添加")

@cli.command()
@click.argument('name')
def remove_server(name):
    """删除服务器配置"""
    if not os.path.exists(CONFIG_PATH):
        click.echo("配置文件不存在")
        return
    
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    servers = config.get("mcpServers", {})
    
    if name not in servers:
        click.echo(f"服务器 {name} 不存在")
        return
    
    del servers[name]
    config["mcpServers"] = servers
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    
    click.echo(f"服务器 {name} 已删除")

if __name__ == '__main__':
    cli()